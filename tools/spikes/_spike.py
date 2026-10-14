#!/usr/bin/env python3
"""Shared harness for the Phase 0 spikes (docs/research/LAKEHOUSE_LLM_LIFECYCLE.md §10, §13).

A spike answers ONE question the documentation could not settle, by creating real AWS
resources, measuring, and deleting them again. Spikes are deploy-class scripts (owner
decision 2026-10-13): they call AWS directly, unlike pipeline work, which is always
dispatched through the state machine and the harnesses.

Every spike follows the same contract, and this module is where it is enforced rather than
hoped for:

  * PASS_CRITERIA are DATA, declared before anything runs. The evidence file prints each one
    as PASS / FAIL / NOT RUN, so a spike cannot quietly redefine success after the fact.
  * plan() is offline. It lists what run() will create, from names alone, so the cost and the
    blast radius are readable before a single API call.
  * Every resource run() creates is registered with track() together with the call that
    deletes it, at the moment it is created. teardown() replays those deleters in reverse.
    Creation and deletion are therefore written next to each other, which is the only way the
    two stay symmetric (the 09_retrieval.teardown lesson).
  * run() is always followed by teardown() in a finally block, unless --keep is given. A crash
    halfway through still deletes whatever already exists.
  * Every resource carries the tags project=llmops-agentic-system and spike=<name>, so
    --sweep can list leftovers from a run that died before teardown (a killed process, a
    closed laptop) by tag, without trusting any local state.
  * The committed evidence file is redacted before it is written: any run of twelve digits
    (an account id) becomes <ACCOUNT_ID>. Raw, unredacted output goes to
    deploy/evidence/*.local.json, which .gitignore already excludes.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import pathlib
import re
import sys
import traceback

REPO = pathlib.Path(__file__).resolve().parents[2]
EVIDENCE_DIR = REPO / "deploy" / "evidence"
PROJECT_TAG = {"project": "llmops-agentic-system"}

#: The same candidate the repo's redaction scanner hashes (tests/redaction_scan.py). Anything
#: matching it in rendered evidence is replaced, so a spike can never commit an account id.
_ACCOUNT_CANDIDATE = re.compile(r"(?<![0-9])[0-9]{12}(?![0-9])")
#: Longer digit runs (Iceberg snapshot ids are 19 digits) are not account ids, but the scanner
#: counts any 12 consecutive digits. They are rendered with thousands separators: still exact and
#: readable, no longer a run.
_LONG_RUN = re.compile(r"(?<![0-9])[0-9]{13,}(?![0-9])")

PASS, FAIL, NOT_RUN = "PASS", "FAIL", "NOT RUN"


def group_digits(value) -> str:
    """1234567 -> 1,234,567 (an Iceberg snapshot id is ~19 digits)."""
    return f"{int(value):,}"


def redact(text: str, also: tuple[tuple[str, str], ...] = ()) -> str:
    """Make evidence safe for a public repo: each (value, placeholder) in `also` is replaced
    first (e.g. the data bucket, whose name embeds the account id), then every 12-digit run
    becomes <ACCOUNT_ID>, then longer runs are grouped with separators."""
    for value, placeholder in also:
        if value:
            text = text.replace(value, placeholder)
    text = _ACCOUNT_CANDIDATE.sub("<ACCOUNT_ID>", text)
    return _LONG_RUN.sub(lambda m: group_digits(m.group(0)), text)


class Criterion:
    """One pass criterion: an id, the sentence a reader checks, and the recorded outcome."""

    def __init__(self, cid: str, description: str):
        self.cid, self.description = cid, description
        self.status, self.detail = NOT_RUN, ""

    def record(self, passed: bool, detail: str = "") -> None:
        self.status, self.detail = (PASS if passed else FAIL), str(detail)


class Spike:
    """Base class. Subclasses set NAME, NUMBER, QUESTION, GATES, PASS_CRITERIA and implement
    plan() and run(). run() must call self.track(...) for every resource it creates."""

    NAME = ""
    NUMBER = 0
    QUESTION = ""
    GATES = ""
    #: (criterion id, description) pairs. Declared, never computed.
    PASS_CRITERIA: tuple[tuple[str, str], ...] = ()
    #: Set to False until the spike's run() has been written and exercised live.
    IMPLEMENTED = False

    def __init__(self, region: str, account_id: str | None = None, session=None):
        self.region, self.account_id = region, account_id
        self._session = session
        self.criteria = {cid: Criterion(cid, desc) for cid, desc in self.PASS_CRITERIA}
        self.created: list[tuple[str, str, object]] = []
        #: (kind, id, error) for every deleter that failed: still running, and the evidence says so.
        self.left: list[tuple[str, str, str]] = []
        self.observations: dict[str, object] = {}
        self.errors: list[str] = []
        #: (value, placeholder) pairs redacted from evidence in addition to account ids.
        self.redactions: list[tuple[str, str]] = []
        self.started_at = self.finished_at = None

    # ── resources ────────────────────────────────────────────────────────────
    @property
    def tags(self) -> dict:
        return {**PROJECT_TAG, "spike": self.NAME}

    def client(self, service: str):
        import boto3  # imported lazily so plan() and the offline tests never need it

        if self._session is None:
            self._session = boto3.Session(region_name=self.region)
        return self._session.client(service, region_name=self.region)

    def track(self, kind: str, ident: str, deleter) -> None:
        """Register a created resource and the zero-argument callable that deletes it."""
        self.created.append((kind, ident, deleter))

    def plan(self) -> list[dict]:
        """Offline list of {kind, name} that run() will create. No network."""
        raise NotImplementedError

    def run(self) -> None:
        raise NotImplementedError(f"{self.NAME}: run() is not implemented yet")

    def record(self, cid: str, passed: bool, detail: str = "") -> None:
        if cid not in self.criteria:
            raise KeyError(f"{self.NAME}: unknown criterion {cid!r}; declare it in PASS_CRITERIA")
        self.criteria[cid].record(passed, detail)

    def observe(self, key: str, value) -> None:
        self.observations[key] = value

    def teardown(self) -> list[str]:
        """Delete everything track() registered, newest first. Failures are collected, not
        raised, so one stuck resource cannot stop the rest from being deleted."""
        problems = []
        while self.created:
            kind, ident, deleter = self.created.pop()
            try:
                deleter()
            except Exception as e:  # noqa: BLE001 -- keep deleting the others
                problems.append(f"{kind} {ident}: {type(e).__name__}: {e}")
                self.left.append((kind, ident, f"{type(e).__name__}: {e}"))
        self.errors.extend(f"teardown: {p}" for p in problems)
        return problems

    def sweep(self) -> list[dict]:
        """List resources still carrying this spike's tag (read-only)."""
        tagging = self.client("resourcegroupstaggingapi")
        found, token = [], None
        while True:
            kw = {"TagFilters": [{"Key": "spike", "Values": [self.NAME]}]}
            if token:
                kw["PaginationToken"] = token
            page = tagging.get_resources(**kw)
            found += [{"arn": r["ResourceARN"]} for r in page.get("ResourceTagMappingList", [])]
            token = page.get("PaginationToken")
            if not token:
                return found

    # ── execution ────────────────────────────────────────────────────────────
    def execute(self, keep: bool = False) -> bool:
        """run() then teardown() in finally. Returns True when every criterion passed."""
        self.started_at = _now()
        try:
            self.run()
        except Exception as e:  # noqa: BLE001 -- the evidence must record the failure
            self.errors.append(f"run: {type(e).__name__}: {e}")
            self.observe("traceback", traceback.format_exc(limit=8))
        finally:
            if not keep:
                self.teardown()
            self.finished_at = _now()
        return all(c.status == PASS for c in self.criteria.values())

    # ── evidence ─────────────────────────────────────────────────────────────
    def evidence_stem(self) -> str:
        return f"SPIKE_{self.NUMBER:02d}_{self.NAME}"

    def render_evidence(self) -> str:
        rows = "\n".join(
            f"| {c.cid} | {c.description} | **{c.status}** | {c.detail} |"
            for c in self.criteria.values())
        obs = "\n".join(f"- **{k}:** {json.dumps(v, default=str) if not isinstance(v, str) else v}"
                        for k, v in self.observations.items() if k != "traceback")
        errs = "\n".join(f"- {e}" for e in self.errors) or "- none"
        verdict = ("PASS" if self.criteria and all(c.status == PASS for c in self.criteria.values())
                   else "FAIL" if any(c.status == FAIL for c in self.criteria.values())
                   else "INCOMPLETE")
        text = (
            f"# {self.evidence_stem()} — {self.QUESTION}\n\n"
            f"- **Verdict:** {verdict}\n"
            f"- **Gates:** {self.GATES}\n"
            f"- **Region:** {self.region}\n"
            f"- **Ran:** {self.started_at or '-'} → {self.finished_at or '-'}\n\n"
            "## Pass criteria\n\n| id | criterion | result | detail |\n|---|---|---|---|\n"
            f"{rows}\n\n## Observations\n\n{obs or '- none'}\n\n## Errors\n\n{errs}\n\n"
            "## Teardown\n\n"
            + ("- all tracked resources deleted\n" if not (self.created or self.left)
               else "".join(f"- LEFT RUNNING: {k} {i}\n" for k, i, _ in self.created)
               + "".join(f"- LEFT RUNNING (delete failed): {k} {i} — {err[:200]}\n"
                         for k, i, err in self.left)))
        return redact(text, tuple(self.redactions))

    def write_evidence(self, directory: pathlib.Path = EVIDENCE_DIR) -> pathlib.Path:
        directory.mkdir(parents=True, exist_ok=True)
        md = directory / f"{self.evidence_stem()}.md"
        md.write_text(self.render_evidence(), encoding="utf-8")
        raw = {"spike": self.NAME, "criteria": {k: vars(v) for k, v in self.criteria.items()},
               "observations": self.observations, "errors": self.errors}
        (directory / f"{self.evidence_stem()}.local.json").write_text(
            json.dumps(raw, indent=2, default=str), encoding="utf-8")
        return md


def boundary_kwargs() -> dict:
    """{"PermissionsBoundary": arn} when the build runner supplies one (tools/runner/runner.py).
    The runner's role may create IAM roles only with that boundary attached, so a spike role can
    never hold more than the boundary allows; on a laptop the variable is unset and nothing changes."""
    import os
    arn = os.environ.get("LLMOPS_SPIKE_BOUNDARY_ARN", "")
    return {"PermissionsBoundary": arn} if arn else {}


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main(spike_cls, argv=None) -> int:
    ap = argparse.ArgumentParser(description=f"{spike_cls.NAME}: {spike_cls.QUESTION}")
    ap.add_argument("action", choices=["plan", "run", "sweep", "cleanup"])
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--account-id", help="only needed by plan() for ARNs")
    ap.add_argument("--keep", action="store_true", help="skip teardown after run (debugging)")
    args = ap.parse_args(argv)
    spike = spike_cls(args.region, args.account_id)
    if args.action == "plan":
        for item in spike.plan():
            print(f"would create {item['kind']}: {item['name']}")
        for cid, desc in spike.PASS_CRITERIA:
            print(f"criterion {cid}: {desc}")
        return 0
    if args.action == "sweep":
        left = spike.sweep()
        for r in left:
            print(redact(f"still tagged spike={spike.NAME}: {r['arn']}"))
        return 1 if left else 0
    if args.action == "cleanup":
        if not hasattr(spike, "cleanup"):
            print(f"{spike.NAME}: no deferred cleanup defined", file=sys.stderr)
            return 2
        for done in spike.cleanup():
            print(redact(str(done)))
        return 0
    if not spike.IMPLEMENTED:
        print(f"{spike.NAME}: run() is not implemented yet; see its docstring", file=sys.stderr)
        return 2
    ok = spike.execute(keep=args.keep)
    print(f"evidence: {spike.write_evidence().relative_to(REPO)}")
    return 0 if ok else 1
