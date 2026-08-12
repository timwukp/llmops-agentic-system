"""Canonical run-report writer + stage_complete normalizer.

Each run publishes its report under its own key, plus a stable alias:
    s3://<bucket>/reports/<run_id>/test-report.json      (durable, per run)
    s3://<bucket>/reports/run-latest/test-report-latest.json  (alias, last run wins)
The alias was once the only key, which meant two concurrent runs destroyed each
other's report — see report_key_for().
The harness driver is the only writer — agents CLAIM results via the
stage_complete inline function, the driver verifies and publishes the
canonical report (orchestrator-side canonical publish; trust but verify).

normalize_stage_complete() absorbs field drift from the LLM side: agents
occasionally emit ``artifacts`` for ``outputs``, ``results`` for ``metrics``,
``summary`` for ``evidence``. Normalizing before consumption keeps one schema
downstream instead of N tolerant readers.

Only stdlib + boto3 (Lambda-safe, no external deps).
"""
from __future__ import annotations

import datetime
import json
from typing import Any

#: The stable "most recent run" alias. Kept because it is a published contract (the
#: driver IAM statement, docs/evidence and this module's own docstring all name it), but
#: it is an ALIAS now, not the only copy -- see report_key_for().
REPORT_KEY = "reports/run-latest/test-report-latest.json"

#: Stage statuses that mean "this stage finished its work", from BOTH writers of
#: manifest["stages"]. The driver writes "completed" (handler.handle_stage_complete);
#: the specialist agents are told to append their own stage entry to the same manifest
#: and they write "complete" under a "<stage>.<task>[.i<n>]" key. Only the driver's
#: spelling was listed here, so on r5 (run-20260811T101948Z-f9d34d27) a run in which
#: every stage succeeded published pass_counts {"total": 14, "passed": 3, "failed": 0}:
#: 7 entries read "complete" and 4 read "launched", and none of the 11 was counted as
#: anything. The console renders that as 3 of 14 passing.
#:
#: "passed"/"succeeded" have never been observed from either writer; they are kept
#: because removing a tolerated spelling can only lose a stage, never gain one.
STAGE_DONE = ("completed", "complete", "passed", "succeeded")
#: Finished and wrong. Distinct from STAGE_IN_FLIGHT: a failure is a finding.
STAGE_FAILED = ("failed", "error")
#: Work handed to something asynchronous (a training job, an endpoint deploy) and not
#: yet reported back. Neither a pass nor a failure -- counting it either way states an
#: outcome that does not exist yet.
STAGE_IN_FLIGHT = ("launched", "running", "in_progress")


def report_key_for(run_id: str) -> str:
    """The per-run report key. One object per run, so two runs cannot overwrite.

    REPORT_KEY was the only key: EVERY run wrote reports/run-latest/test-report-latest.json,
    so with two runs in flight the second silently destroyed the first's report -- the one
    artifact a run exists to produce -- and left no way to tell which run the survivor
    described, because the alias is the same string either way. The platform is meant to
    run parallel tasks (finetune one model while distilling another), so this was a
    correctness bug waiting on a second concurrent run, not a hypothetical.

    A falsy run_id returns the alias rather than minting reports/run-/...: a report filed
    under a blank run id is worse than one filed under the shared alias, because at least
    the alias is a key someone is looking at.
    """
    if not run_id:
        return REPORT_KEY
    return f"reports/{run_id}/test-report.json"

# Accepted aliases, in priority order (canonical name first).
_OUTPUT_KEYS = ("outputs", "artifacts", "output", "artifact_uris", "s3_uris")
_METRIC_KEYS = ("metrics", "results", "measurements", "stats")
_EVIDENCE_KEYS = ("evidence", "summary", "proof", "notes")


def _first_present(args: dict, keys: tuple) -> Any:
    """Return the value of the first key PRESENT in args (even if falsy).

    Presence matters: outputs=[] is a valid, deliberate claim of
    "empty but complete" and must not fall through to a later alias.
    """
    for key in keys:
        if key in args:
            return args[key]
    return None


def normalize_stage_complete(args: dict | None) -> dict:
    """Coerce a stage_complete toolUse input to the canonical contract shape.

    Returns a dict with exactly: stage, task, outputs (list[str]),
    metrics (dict), evidence (str). Tolerates alias drift and scalar/list
    type drift; never raises on messy-but-parseable input.
    """
    args = dict(args or {})

    outputs = _first_present(args, _OUTPUT_KEYS)
    if outputs is None:
        outputs = []
    if isinstance(outputs, str):
        # A JSON-encoded LIST is parsed before the scalar wrap, for the same reason
        # `metrics` below parses a JSON-encoded dict -- agents emit both. Skipping it here
        # was not a cosmetic gap: `verify_outputs` head_objects every element that
        # `startswith("s3://")`, and a one-element list holding the string
        # '["s3://.../a.jsonl", "s3://.../b.json"]' starts with '[', so it was SKIPPED and
        # the stage passed verification having proved nothing. That is the whole
        # trust-but-verify mechanism defeated by a type: an agent could claim outputs that
        # do not exist and be believed, which is precisely what the head_object exists to
        # stop. Measured live on rehearsal run-20260811T005043Z-320cc47e, whose data-prep
        # entry recorded outputs as ["[\"s3://...generated.jsonl\", \"s3://...manifest.json\"]"]
        # -- those two objects DID exist, so the run was honest and the check was still
        # vacuous. A non-list JSON scalar ("s3://b/x" is valid JSON) must stay a scalar,
        # hence the isinstance check rather than trusting whatever comes back.
        #
        # A JSON-encoded SCALAR is unwrapped for the same reason: '"s3://b/x"' round-trips
        # to a bare URI, whereas keeping the raw text leaves the quote character in front
        # of the scheme and `verify_outputs` skips it -- the identical vacuous check, one
        # layer down. A bare unquoted s3:// URI is not valid JSON, so it lands in the
        # except branch and is wrapped as before; anything else (dict, number) keeps its
        # original text so the claim stays legible in the report instead of being reshaped
        # into something that looks verified.
        try:
            parsed = json.loads(outputs)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list):
            outputs = parsed
        elif isinstance(parsed, str):
            outputs = [parsed]
        else:
            outputs = [outputs]
    elif not isinstance(outputs, (list, tuple)):
        # A dict, int or bool is wrapped as TEXT and deliberately not mined for URIs. It
        # stays unverified, but that is `verify_outputs`' existing and documented rule for
        # any element that is not an s3:// URI -- not the defect above. The defect above was
        # a value that WAS a list of s3:// URIs and got skipped for being spelled as a
        # string; guessing which of a dict's values are meant to be artifacts would invent
        # a claim the agent did not make, and an invented claim that then passes
        # head_object is worse than a legible one that is never checked.
        outputs = [outputs]
    outputs = [str(o) for o in outputs if o is not None]

    metrics = _first_present(args, _METRIC_KEYS)
    if metrics is None:
        metrics = {}
    if isinstance(metrics, str):
        # agents sometimes emit metrics as a JSON string — parse before wrapping
        try:
            parsed = json.loads(metrics)
            metrics = parsed if isinstance(parsed, dict) else {"value": parsed}
        except json.JSONDecodeError:
            metrics = {"value": metrics}
    elif not isinstance(metrics, dict):
        metrics = {"value": metrics}

    evidence = _first_present(args, _EVIDENCE_KEYS)
    if evidence is None:
        evidence = ""
    if not isinstance(evidence, str):
        evidence = json.dumps(evidence, default=str)

    return {
        "stage": str(args.get("stage", "")),
        "task": str(args.get("task", "")),
        "outputs": outputs,
        "metrics": metrics,
        "evidence": evidence,
    }


def build_run_report(manifest: dict) -> dict:
    """Derive the ops-console report document from a run manifest."""
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    stages: dict = manifest.get("stages", {}) or {}

    findings = []
    passed = failed = in_flight = 0
    stage_summaries = {}
    for name, entry in stages.items():
        entry = entry or {}
        status = str(entry.get("status", "unknown"))
        stage_summaries[name] = {
            "status": status,
            "started_at": entry.get("started_at"),
            "ended_at": entry.get("ended_at"),
            "outputs": entry.get("outputs", []),
            "metrics": entry.get("metrics", {}),
        }
        if status in STAGE_DONE:
            passed += 1
        elif status in STAGE_IN_FLIGHT:
            in_flight += 1
        elif status in STAGE_FAILED:
            failed += 1
            findings.append({
                "severity": "high",
                "stage": name,
                "title": f"Stage {name} failed",
                "detail": entry.get("evidence") or entry.get("error") or "no detail recorded",
            })
        elif status == "escalated":
            failed += 1
            findings.append({
                "severity": "critical",
                "stage": name,
                "title": f"Stage {name} escalated to human",
                "detail": entry.get("evidence") or "see SNS notification / DDB stage events",
            })

    # The escalation path's own record. The `status == "escalated"` branch above reads
    # manifest["stages"], and nothing writes that status there -- handle_escalate's durable
    # record was runs.status plus a DDB stage event, neither of which this function can see.
    # So r5 escalated to a human at its iteration-1 gate and published "findings": [], the
    # report of a run with nothing to say. The stage branch is kept (a stage entry that DOES
    # say "escalated" still deserves the finding) and this reads the list the driver now
    # appends to, which is why the two do not duplicate: they have different sources.
    escalations = list(manifest.get("escalations") or [])
    for esc in escalations:
        esc = esc or {}
        findings.append({
            "severity": "critical",
            "stage": esc.get("stage", "unknown"),
            "title": f"Stage {esc.get('stage', 'unknown')} escalated to human",
            "detail": esc.get("reason") or "see SNS notification / DDB stage events",
        })

    return {
        "generated_at": now,
        "run_id": manifest.get("run_id", "unknown"),
        "iteration": manifest.get("iteration", 0),
        "trigger_source": manifest.get("trigger_source"),
        "stages": stage_summaries,
        "findings": findings,
        "pass_counts": {
            "total": len(stages),
            "passed": passed,
            "failed": failed,
            "in_flight": in_flight,
            # The four sub-counts reconcile to `total` by construction, so a status
            # spelling neither vocabulary above knows shows up HERE as a non-zero number
            # instead of vanishing into the gap between total and passed+failed. That gap
            # is what hid the "complete"/"launched" bug: 3 of 14 looked like a failing run
            # rather than like a report that could not read its own input.
            "unrecognized": len(stages) - passed - failed - in_flight,
        },
        "models": manifest.get("models", {}),
        # Append-only, one record per driver-verified stage completion, in order. The
        # `stages` map above holds ONE entry per stage name, so iteration 1's finetune
        # result overwrites iteration 0's and a remediation loop destroys the before/after
        # comparison it exists to produce -- measured on r5, whose manifest keeps only
        # iteration-1 metrics for both `finetune` and `eval`. Carried through to the report
        # because the report is the artifact a human opens.
        "stage_history": list(manifest.get("stage_history") or []),
        # Carried verbatim as well as summarised into `findings`: the finding says a human
        # was called, the record says which iteration called them and why.
        "escalations": escalations,
    }


def write_run_report(s3: Any, bucket: str, manifest: dict) -> dict:
    """Build the report from the manifest and publish it, per run AND to the alias.

    Two writes on purpose, in this order:

    1. ``reports/<run_id>/test-report.json`` -- the durable copy. This one is the record;
       it is never overwritten by another run, so a parallel run cannot destroy it.
    2. ``reports/run-latest/test-report-latest.json`` -- the published alias, for whoever
       just wants "the last run". Best effort: if the alias write is denied the run must
       NOT fail, because the durable copy already succeeded and the report exists. The
       reverse order would reintroduce exactly the bug being fixed -- an alias failure
       taking down a run whose report was already safely written.

    The alias failure is reported in the returned document rather than swallowed: a
    silently absent alias reads as "no run has finished", which is a different claim.

    ``s3`` is an injected boto3 S3 client (injectable for tests).
    Returns the report document that was written.
    """
    report = build_run_report(manifest)
    body = json.dumps(report, indent=2, default=str).encode("utf-8")
    run_key = report_key_for(str(manifest.get("run_id") or ""))
    s3.put_object(Bucket=bucket, Key=run_key, Body=body,
                  ContentType="application/json")
    report["report_key"] = run_key
    if run_key != REPORT_KEY:
        try:
            s3.put_object(Bucket=bucket, Key=REPORT_KEY, Body=body,
                          ContentType="application/json")
        except Exception as exc:  # noqa: BLE001 — the durable copy is already written
            report["alias_error"] = f"{type(exc).__name__}: {exc}"
    return report
