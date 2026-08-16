#!/usr/bin/env python3
"""Is production running THIS tree? One read-only command that answers it.

    python3 tools/audit_drift.py --region us-east-1 [--json drift-audit.json]
    python3 tools/audit_drift.py --region us-east-1 --account-id 123456789012 --offline

Every comparator here already existed and already had unit tests. What did not exist was
a way to RUN one without deploying: `state_machine_drift`, `harness_config_drift` and
`show_diff` are all called from inside the write path, on the way to a put, and they raise
`SystemExit` when they find something. So "is production running this tree's code" was a
question only a deploy could ask -- and asking it that way is a write. This is the
read-only door onto the same comparators.

The question is not academic. Before the 2026-08-15 rehearsal, a hand comparison found
live weeks behind `main`: a whole pipeline mode that had never been deployed, a
prerequisite check absent from the live driver, the driver still calling an API a merged PR
had deliberately replaced, 3 of 7 prompts and 4 of 7 Lambdas adrift. Every one of those PRs
read MERGED, the suite was green, and nothing in the repo compared the two sides. That is
the same shape `tools/audit_landed.py` exists for, one step further down the pipe: merged
is not on main, and on main is not deployed.

EXIT CODES (pinned by tests/test_audit_drift.py):

    0   every leg was compared, and every leg is clean
    1   drift: at least one live resource disagrees with this tree
    2   no drift found, but at least one leg could NOT be compared (no credentials,
        AccessDenied, a resource that does not exist) -- a check that cannot answer must
        not report clean, the rule audit_landed.py already follows
    3   usage error

WHAT IT DOES NOT CHECK (also in the JSON, as `not_checked`) -- see NOT_CHECKED below.
The most important entry is the last one: this compares the WORKING TREE against live, so
it cannot tell "edited locally and never merged" apart from "merged and never deployed".
Run it on a clean checkout of the default branch; `repo_head` in the JSON records which
commit answered, and a dirty tree is called out in the report.

The JSON output contains live ARNs and therefore this account's id, which is why
`/drift-audit*.json` is in .gitignore. Nothing here writes to AWS: every call is a
Describe/Get/List, and `--offline` makes none at all.
"""
from __future__ import annotations

import argparse
import importlib.util
import io
import json
import pathlib
import subprocess
import sys
import zipfile
from urllib.request import urlopen

import boto3

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "deploy"))


def _deploy_module(name: str, rel: str):
    """Import a `NN_name.py` deploy script by path -- the digits make it unimportable."""
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


iam_deploy = _deploy_module("iam_deploy", "deploy/01_iam.py")
harness_deploy = _deploy_module("harness_deploy", "deploy/05_harnesses.py")
lambda_deploy = _deploy_module("lambda_deploy", "deploy/07_lambdas.py")

import config_subst  # noqa: E402 — deploy/ is on sys.path from above

#: Values 07_lambdas.py sends literally, restated here because they are inline in its
#: write path (deploy_lambda) and not available as constants. Restating a fact is how the
#: env_keys list drifted for eight days, so `test_the_sent_lambda_shape_matches_the_deploy_script`
#: parses deploy/07_lambdas.py and fails if any of these four stops matching.
RUNTIME = "python3.12"
HANDLER = "handler.handler"
ASYNC_RETRIES = 2
ASYNC_MAX_AGE_S = 300

#: Everything a green run of this tool does NOT license you to believe.
NOT_CHECKED = (
    "whether a READY harness serves what the control plane reports it serves",
    "`memory` wiring -- 04_wire_memory.py owns it, deliberately outside UPDATED_FIELDS",
    "harness tags, and every harness field outside 05_harnesses.py's UPDATED_FIELDS",
    "Lambda CodeSha256 (see audit_lambda_code), layers, reserved concurrency, VPC "
    "config, log group retention",
    "additional managed or inline IAM policies -- only the inline "
    f"`{iam_deploy.INLINE_POLICY_NAME}` document is read",
    "SSM parameters (their values are inputs to the sent side, not outputs compared)",
    "EventBridge rules and targets, including the resume and triage rules",
    "DynamoDB tables, their keys and their throughput",
    "S3 bucket policy, lifecycle and the artifacts themselves",
    "'edited locally but never merged' vs 'merged but never deployed' -- this compares "
    "the WORKING TREE, so run it on a clean checkout of the default branch",
)


# ── plumbing ────────────────────────────────────────────────────────────────
def _error_code(exc) -> str:
    """The API error code if this is a botocore error, else the exception class name."""
    return ((getattr(exc, "response", None) or {}).get("Error", {}).get("Code")
            or type(exc).__name__)


def _absent(exc, *codes) -> bool:
    return _error_code(exc) in codes


def _why(exc) -> str:
    return f"{type(exc).__name__}: {exc}"[:300]


def leg(name: str, drift=None, unknown=None, compared=()) -> dict:
    return {"leg": name, "drift": list(drift or []), "unknown": list(unknown or []),
            "compared": list(compared)}


def repo_head() -> dict:
    """Which commit is answering, and whether the tree is dirty.

    Read-only git, and reported rather than enforced: running this on a dirty tree is
    legitimate (that is how you check a branch before deploying it), it just changes what
    a finding MEANS -- local edit, not deployment drift.
    """
    def git(*args):
        out = subprocess.run(["git", "-C", str(REPO), *args],
                             capture_output=True, text=True)
        return out.stdout.strip() if out.returncode == 0 else None

    sha = git("rev-parse", "HEAD")
    status = git("status", "--porcelain")
    return {"sha": (sha or "")[:12] or None, "branch": git("rev-parse", "--abbrev-ref", "HEAD"),
            "dirty": bool(status) if status is not None else None}


# ── the sent side, rebuilt verbatim ─────────────────────────────────────────
# A sent side that is reconstructed even slightly differently from what the deploy script
# sends makes this tool report drift on a perfectly deployed system, forever -- and the
# third person it wakes at 3am switches it off. So each of these mirrors one deploy path
# line for line, and a test parses that path to keep them honest.

def asl_sent(region: str, account: str) -> str:
    """The ASL 07_lambdas.py would send: two str.replace calls, nothing else."""
    asl = (REPO / "orchestration" / "state_machine.asl.json").read_text()
    driver_arn = f"arn:aws:lambda:{region}:{account}:function:llmops-harness-driver"
    return asl.replace("${HarnessDriverArn}", driver_arn) \
              .replace("${EventBusName}", "llmops-pipeline")


def harness_sent(agent: str, mapping: dict) -> tuple:
    """(harnessName, the dict 05_harnesses.py sends on an UPDATE) for one agent.

    The pops are not cosmetic: `create_or_update` removes harnessName and tags from the
    config before sending, and only UPDATED_FIELDS go on the update call. `ensure_env`
    matters most -- it sets OTEL_TRACES_SAMPLER=always_on, which is in the config nobody
    wrote by hand, so a sent side that skipped it would report environmentVariables drift
    on all seven harnesses.
    """
    cfg = harness_deploy.load_config(agent, False, mapping)
    name = cfg.pop("harnessName")
    cfg.pop("tags", None)
    cfg = harness_deploy.ensure_env(cfg)
    return name, {k: v for k, v in cfg.items() if k in harness_deploy.UPDATED_FIELDS}


def lambda_sent(ssm, region: str, account: str, cfg: dict) -> dict:
    """The configuration 07_lambdas.py would send for one Lambda.

    `env_keys_for` + `env_values` are called, not re-implemented: the first derives the
    required variables from the handler's own `os.environ[...]` reads, and the second is
    where a newly-required variable with no deploy-time value raises. Running them is most
    of the value of --offline.
    """
    keys = lambda_deploy.env_keys_for(cfg)
    return {"Runtime": RUNTIME, "Handler": HANDLER,
            "Timeout": cfg["timeout"], "MemorySize": cfg["memory"],
            "Role": ssm.get_parameter(Name=cfg["role_param"])["Parameter"]["Value"],
            "Environment": lambda_deploy.env_values(ssm, region, account, keys, None)}


def bundle_members_sent() -> dict:
    """{member name: bytes} for the zip `bundle()` builds -- the same union for every fn.

    Compared per member rather than by CodeSha256 on purpose. `bundle()` uses
    `zipfile.write`, which stamps each entry with the source file's mtime, so the zip bytes
    (and therefore the sha) are NOT reproducible from a fresh checkout: comparing shas
    would report permanent drift on a perfectly deployed function, which is worse than not
    checking at all.
    """
    return {path.name: path.read_bytes()
            for path in lambda_deploy.vendored_modules().values()}


def _first_divergence(want: bytes, got: bytes) -> tuple:
    """(1-based line, byte offset) where two members first differ."""
    n = min(len(want), len(got))
    at = next((i for i in range(n) if want[i] != got[i]), n)
    return want[:at].count(b"\n") + 1, at


# ── the legs ────────────────────────────────────────────────────────────────
def audit_iam(iam, specs: dict) -> dict:
    """Trust policy + the one inline policy, per role, against deploy/iam/.

    Does its own `get_role` rather than calling `01_iam.py:get_existing`, which returns
    `(None, None)` for BOTH "the role does not exist" and "the role could not be read".
    That conflation is fine on the deploy path -- either way it creates the role -- and
    fatal here, because one is drift and the other is unknown, and this tool's exit code
    turns on telling them apart.
    """
    drift, unknown, compared = [], [], []
    for name, spec in specs.items():
        try:
            role = iam.get_role(RoleName=name)["Role"]
        except Exception as exc:  # noqa: BLE001 — absent is drift, unreadable is unknown
            if _absent(exc, "NoSuchEntity", "NoSuchEntityException"):
                drift.append({"resource": f"role {name}",
                              "problem": "declared in deploy/iam/, ABSENT live"})
            else:
                unknown.append({"resource": f"role {name}", "why": _why(exc)})
            continue
        compared.append(f"role {name}")
        diff = iam_deploy.policy_diff("trust policy", role.get("AssumeRolePolicyDocument"),
                                      spec["trust"])
        if diff:
            drift.append({"resource": f"role {name}", "problem": "trust policy differs",
                          "diff_lines": len(diff), "diff_head": diff[:12]})
        try:
            pol = iam.get_role_policy(
                RoleName=name,
                PolicyName=iam_deploy.INLINE_POLICY_NAME)["PolicyDocument"]
        except Exception as exc:  # noqa: BLE001
            if _absent(exc, "NoSuchEntity", "NoSuchEntityException"):
                drift.append({
                    "resource": f"role {name}",
                    "problem": f"no inline policy {iam_deploy.INLINE_POLICY_NAME} live — "
                               "the role exists with none of its permissions"})
            else:
                unknown.append({"resource": f"role {name} inline policy",
                                "why": _why(exc)})
            continue
        diff = iam_deploy.policy_diff("inline policy", pol, spec["policy"])
        if diff:
            drift.append({"resource": f"role {name}", "problem": "inline policy differs",
                          "diff_lines": len(diff), "diff_head": diff[:12]})
    return leg("iam", drift, unknown, compared)


def audit_state_machine(sfn, region: str, account: str) -> dict:
    """The live definition against this tree's ASL, state by state."""
    name = lambda_deploy.STATE_MACHINE_NAME
    arn = f"arn:aws:states:{region}:{account}:stateMachine:{name}"
    sent = asl_sent(region, account)
    left = [t for t in ("${HarnessDriverArn}", "${EventBusName}") if t in sent]
    if left:
        return leg("state_machine",
                   drift=[{"resource": name,
                           "problem": f"this tree's ASL still carries {left} after "
                                      "substitution — the sent side is unbuildable"}])
    try:
        live = sfn.describe_state_machine(stateMachineArn=arn)
    except Exception as exc:  # noqa: BLE001
        if _absent(exc, "StateMachineDoesNotExist"):
            return leg("state_machine",
                       drift=[{"resource": name, "problem": "does not exist live"}])
        return leg("state_machine",
                   unknown=[{"resource": name, "why": _why(exc)}])
    found = lambda_deploy.state_machine_drift(sent, live["definition"])
    return leg("state_machine",
               drift=[{"resource": name, **f} for f in found],
               compared=[name])


def audit_harnesses(ctl, mapping: dict, agents=None) -> dict:
    """Every UPDATED_FIELD of every agent's harness.json against the live config.

    Containment, because `harness_config_drift` is containment: the service adds
    agentRuntime* keys to `environment` on every healthy harness, so equality would report
    drift forever. That decision is measured and documented at 05_harnesses.py:134.
    """
    agents = list(agents or harness_deploy.AGENTS)
    try:
        live_all = ctl.list_harnesses().get("harnesses", [])
    except Exception as exc:  # noqa: BLE001 — one failure, so report it once
        return leg("harnesses",
                   unknown=[{"resource": "list_harnesses", "why": _why(exc)}])
    by_name = {}
    for h in live_all:
        for key in ("name", "harnessName"):
            if h.get(key):
                by_name.setdefault(h[key], h)
        if h.get("harnessId"):
            by_name.setdefault(h["harnessId"].rsplit("-", 1)[0], h)
    drift, unknown, compared = [], [], []
    for agent in agents:
        name, sent = harness_sent(agent, mapping)
        found = by_name.get(name)
        if not found:
            drift.append({"resource": f"harness {name}",
                          "problem": "agents/ declares it, no live harness has that name"})
            continue
        try:
            live = ctl.get_harness(harnessId=found["harnessId"])["harness"]
        except Exception as exc:  # noqa: BLE001
            unknown.append({"resource": f"harness {name}", "why": _why(exc)})
            continue
        compared.append(f"harness {name}")
        if live.get("status") != "READY":
            drift.append({"resource": f"harness {name}",
                          "problem": f"status is {live.get('status')!r}, not READY"})
        for f in harness_deploy.harness_config_drift(sent, live):
            drift.append({"resource": f"harness {name}", **f})
    return leg("harnesses", drift, unknown, compared)


def audit_lambda_config(lam, ssm, region: str, account: str, only=None) -> dict:
    """Role, runtime, handler, timeout, memory, env and the async invoke policy.

    Environment is compared as EQUALITY, not containment, and the asymmetry with harnesses
    is deliberate: `update_function_configuration` REPLACES the whole Variables map, so a
    key that is live and unsent is a leftover from an older deploy that some
    `os.environ.get()` may still be reading.
    """
    drift, unknown, compared = [], [], []
    for key, cfg in (only or lambda_deploy.LAMBDAS).items():
        fn = cfg["fn"]
        try:
            sent = lambda_sent(ssm, region, account, cfg)
        except Exception as exc:  # noqa: BLE001 — cannot build the sent side
            unknown.append({"resource": f"lambda {fn}",
                            "why": f"sent side unbuildable — {_why(exc)}"})
            continue
        try:
            live = lam.get_function_configuration(FunctionName=fn)
        except Exception as exc:  # noqa: BLE001
            if _absent(exc, "ResourceNotFoundException"):
                drift.append({"resource": f"lambda {fn}",
                              "problem": "deployed by 07_lambdas.py, ABSENT live"})
            else:
                unknown.append({"resource": f"lambda {fn}", "why": _why(exc)})
            continue
        compared.append(f"lambda {fn} config")
        for field in ("Runtime", "Handler", "Timeout", "MemorySize", "Role"):
            if live.get(field) != sent[field]:
                drift.append({"resource": f"lambda {fn}", "field": field,
                              "problem": f"sent {sent[field]!r} != live "
                                         f"{live.get(field)!r}"})
        live_env = (live.get("Environment") or {}).get("Variables") or {}
        for k in sorted(set(sent["Environment"]) | set(live_env)):
            if k not in live_env:
                drift.append({"resource": f"lambda {fn}", "field": f"env.{k}",
                              "problem": "sent, but ABSENT live"})
            elif k not in sent["Environment"]:
                drift.append({"resource": f"lambda {fn}", "field": f"env.{k}",
                              "problem": "live, but this deploy sends no such variable"})
            elif live_env[k] != sent["Environment"][k]:
                drift.append({"resource": f"lambda {fn}", "field": f"env.{k}",
                              "problem": f"sent {sent['Environment'][k]!r} != live "
                                         f"{live_env[k]!r}"})
        try:
            inv = lam.get_function_event_invoke_config(FunctionName=fn)
        except Exception as exc:  # noqa: BLE001
            if _absent(exc, "ResourceNotFoundException"):
                drift.append({
                    "resource": f"lambda {fn}",
                    "problem": "no async invoke config live, so the ACCOUNT defaults "
                               "apply: an async self-invoke may be redelivered up to 6 "
                               "hours later, resuming a turn whose session is gone"})
            else:
                unknown.append({"resource": f"lambda {fn} async policy",
                                "why": _why(exc)})
            continue
        compared.append(f"lambda {fn} async policy")
        for field, want in (("MaximumRetryAttempts", ASYNC_RETRIES),
                            ("MaximumEventAgeInSeconds", ASYNC_MAX_AGE_S)):
            if inv.get(field) != want:
                drift.append({"resource": f"lambda {fn}", "field": field,
                              "problem": f"sent {want} != live {inv.get(field)}"})
    return leg("lambda_config", drift, unknown, compared)


def audit_lambda_code(lam, only=None, fetch=None) -> dict:
    """Every member of every live bundle against the bytes in this tree.

    Per member, never by sha (see bundle_members_sent). The finding names the member and
    the first line that differs, in the style state_machine_drift established: the useful
    sentence is "handler.py differs from line 2891", not a 26 KB dump.
    """
    fetch = fetch or _fetch_members
    vendored = bundle_members_sent()
    drift, unknown, compared = [], [], []
    for key, cfg in (only or lambda_deploy.LAMBDAS).items():
        fn = cfg["fn"]
        want = {"handler.py": cfg["src"].read_bytes(), **vendored}
        try:
            got = fetch(lam, fn)
        except Exception as exc:  # noqa: BLE001
            if _absent(exc, "ResourceNotFoundException"):
                drift.append({"resource": f"lambda {fn}",
                              "problem": "deployed by 07_lambdas.py, ABSENT live"})
            else:
                unknown.append({"resource": f"lambda {fn} code", "why": _why(exc)})
            continue
        compared.append(f"lambda {fn} code ({len(want)} members)")
        for name in sorted(set(want) | set(got)):
            if name not in got:
                drift.append({"resource": f"lambda {fn}", "member": name,
                              "problem": "bundled by this tree, ABSENT from the live zip "
                                         "— the function dies at cold start on it"})
            elif name not in want:
                drift.append({"resource": f"lambda {fn}", "member": name,
                              "problem": "in the live zip, bundled by nothing in this "
                                         "tree — a leftover from an older deploy"})
            elif got[name] != want[name]:
                line, at = _first_divergence(want[name], got[name])
                drift.append({"resource": f"lambda {fn}", "member": name,
                              "problem": f"differs: this tree {len(want[name])} bytes, "
                                         f"live {len(got[name])}, first at line {line} "
                                         f"(byte {at})"})
    return leg("lambda_code", drift, unknown, compared)


def _fetch_members(lam, fn: str) -> dict:
    """{member: bytes} of the zip Lambda is serving right now.

    Same rule as tools/probe_liveness_resurrection.py:70 -- the presigned URL is never
    printed. Unlike that one, nothing is extracted to disk: this audit needs bytes, and a
    read-only tool should not scatter temp directories.
    """
    url = lam.get_function(FunctionName=fn)["Code"]["Location"]
    with urlopen(url) as resp:  # noqa: S310 — URL comes from the Lambda API
        blob = resp.read()
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        return {n: z.read(n) for n in z.namelist() if not n.endswith("/")}


# ── offline ─────────────────────────────────────────────────────────────────
class _OfflineSSM:
    """Answers the two parameter reads the sent side needs, with derived values.

    Not a stand-in for the account: offline mode compares nothing, so these values are
    never asserted against anything live. They exist so `env_keys_for` and `env_values`
    run for real, and that pair is the part of the sent side most likely to break -- a
    handler that adds `os.environ["NEW"]` makes env_values raise KeyError, which is
    exactly the eight-day ACTUALS_TABLE bug, and catching it in CI is why this mode exists.
    """

    def __init__(self, account: str, region: str, bucket: str):
        self.account, self.region, self.bucket = account, region, bucket

    def get_parameter(self, Name):  # noqa: N803 — boto3's own casing
        if Name == "/llmops/storage/bucket":
            return {"Parameter": {"Value": self.bucket}}
        if Name.startswith("/llmops/iam/"):
            return {"Parameter": {"Value": f"arn:aws:iam::{self.account}:role/OFFLINE"}}
        raise KeyError(f"{Name} is not a parameter the sent side reads")


def build_sent_side(account: str, region: str, bucket=None, memory_id=None) -> dict:
    """Build every sent side and report what is left unresolved. Touches no AWS.

    The findings are repo defects, not deployment drift: a `<PLACEHOLDER>` or `${Token}`
    that survives substitution is a config that AgentCore or Step Functions would ACCEPT,
    version, report healthy, and then fail on at session start.
    """
    derived = bucket or config_subst.default_bucket(account, region)
    ssm = _OfflineSSM(account, region, derived)
    mapping = _mapping(account, region, derived, memory_id)
    out = {"roles": [], "harnesses": [], "lambdas": [], "unresolved": [], "members": []}

    specs = iam_deploy.build_role_specs(mapping, memory_id)
    for name, spec in specs.items():
        out["roles"].append(name)
        for which in ("trust", "policy"):
            left = config_subst.unresolved(spec[which])
            if left:
                out["unresolved"].append({"resource": f"role {name}", "where": which,
                                          "tokens": left})

    for agent in harness_deploy.AGENTS:
        name, sent = harness_sent(agent, mapping)
        out["harnesses"].append(name)
        left = config_subst.unresolved(sent)
        if left:
            out["unresolved"].append({"resource": f"harness {name}", "tokens": left})

    for key, cfg in lambda_deploy.LAMBDAS.items():
        sent = lambda_sent(ssm, region, account, cfg)
        out["lambdas"].append({"fn": cfg["fn"],
                               "env_keys": sorted(sent["Environment"])})

    asl = asl_sent(region, account)
    left = [t for t in ("${HarnessDriverArn}", "${EventBusName}") if t in asl]
    if left:
        out["unresolved"].append({"resource": lambda_deploy.STATE_MACHINE_NAME,
                                  "tokens": left})
    out["members"] = sorted({"handler.py", *bundle_members_sent()})
    return out


# ── run + report ────────────────────────────────────────────────────────────
def _mapping(account: str, region: str, bucket: str, memory_id=None) -> dict:
    mapping = config_subst.mapping_for(account, region, bucket)
    if memory_id:
        mapping["<MEMORY_ID>"] = memory_id
    return mapping


def audit(region: str, account: str, clients: dict, bucket=None, memory_id=None) -> list:
    """Run every leg. `clients` is injected so the tests can drive this with fakes.

    The two buckets are not a mistake. `01_iam.py` DERIVES the bucket name from the
    account id (its `--bucket` is the only override); `05_harnesses.py` prefers the
    PUBLISHED `/llmops/storage/bucket`, because a skill URI pointing at a bucket that does
    not exist fails at session start. The sent side is whatever the deploy script sends,
    so this mirrors the asymmetry rather than smoothing it over -- and when the published
    value cannot be read, the harness leg is UNANSWERED rather than compared against a
    guess that would report drift on all seven.
    """
    derived = config_subst.default_bucket(account, region)
    specs = iam_deploy.build_role_specs(
        _mapping(account, region, bucket or derived, memory_id), memory_id)
    try:
        harness_bucket = bucket or clients["ssm"].get_parameter(
            Name="/llmops/storage/bucket")["Parameter"]["Value"]
    except Exception as exc:  # noqa: BLE001
        harnesses = leg("harnesses", unknown=[{
            "resource": "/llmops/storage/bucket",
            "why": "every skill URI in the sent side embeds this bucket, so it cannot be "
                   f"built without it — {_why(exc)}"}])
    else:
        harnesses = audit_harnesses(
            clients["ctl"], _mapping(account, region, harness_bucket, memory_id))
    return [
        audit_iam(clients["iam"], specs),
        audit_state_machine(clients["sfn"], region, account),
        harnesses,
        audit_lambda_config(clients["lam"], clients["ssm"], region, account),
        audit_lambda_code(clients["lam"]),
    ]


def report(legs: list, head: dict, out=print) -> int:
    """Print the audit and RETURN the exit code -- audit_landed.py's shape."""
    drift = [(lg["leg"], f) for lg in legs for f in lg["drift"]]
    unknown = [(lg["leg"], u) for lg in legs for u in lg["unknown"]]
    out("drift audit of %s (%s%s)"
        % (head.get("sha") or "unknown commit", head.get("branch") or "?",
           ", DIRTY WORKING TREE" if head.get("dirty") else ""))
    for lg in legs:
        out("  %-14s %2d compared, %2d drift, %2d unanswered"
            % (lg["leg"], len(lg["compared"]), len(lg["drift"]), len(lg["unknown"])))
    if drift:
        out("\n%d finding(s) — live does NOT match this tree:" % len(drift))
        for name, f in drift:
            bits = " ".join(f"{k}={f[k]}" for k in ("field", "member", "state", "top_level")
                            if k in f)
            out("  [%s] %s%s: %s" % (name, f.get("resource", "?"),
                                     f" {bits}" if bits else "", f["problem"]))
    if unknown:
        out("\n%d leg(s)/resource(s) could NOT be compared. This is not a pass:"
            % len(unknown))
        for name, u in unknown:
            out("  [%s] %s: %s" % (name, u.get("resource", "?"), u["why"]))
    out("\nNOT checked by this tool:")
    for item in NOT_CHECKED:
        out("  - %s" % item)
    if drift:
        return 1
    if unknown:
        return 2
    out("\nevery leg compared and clean: live matches this tree")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--region", required=True)
    ap.add_argument("--account-id", help="skip STS; REQUIRED with --offline")
    ap.add_argument("--bucket", help="override the data bucket in <DATA_BUCKET>")
    ap.add_argument("--memory-id", help="BYO AgentCore Memory id, if wire_memory.py ran")
    ap.add_argument("--offline", action="store_true",
                    help="build the sent side only and make NO AWS call at all")
    ap.add_argument("--json", dest="json_out", default=None,
                    help="write the full report here (contains live ARNs; gitignored)")
    args = ap.parse_args(argv)
    head = repo_head()

    if args.offline:
        if not args.account_id:
            print("--offline needs --account-id: the sent side embeds the account in "
                  "every ARN and bucket name, and offline means STS is not called.",
                  file=sys.stderr)
            return 3
        sent = build_sent_side(args.account_id, args.region, args.bucket, args.memory_id)
        print(json.dumps({"offline": True, "repo_head": head,
                          "would_compare": {
                              "roles": sent["roles"], "harnesses": sent["harnesses"],
                              "state_machine": lambda_deploy.STATE_MACHINE_NAME,
                              "lambdas": sent["lambdas"],
                              "bundle_members": sent["members"]},
                          "unresolved": sent["unresolved"]}, indent=2))
        if sent["unresolved"]:
            print("\nthe sent side does not fully resolve; AWS would ACCEPT these and "
                  "fail at session start", file=sys.stderr)
            return 1
        return 0

    account = args.account_id
    if not account:
        try:
            account = boto3.client("sts", region_name=args.region) \
                .get_caller_identity()["Account"]
        except Exception as exc:  # noqa: BLE001 — no creds: answer nothing, claim nothing
            print("could not identify the account (%s), so not one leg could be "
                  "compared. Pass --account-id, or --offline for the sent side alone."
                  % _why(exc), file=sys.stderr)
            return 2

    clients = {
        "iam": boto3.client("iam", region_name=args.region),
        "sfn": boto3.client("stepfunctions", region_name=args.region),
        "ctl": boto3.client("bedrock-agentcore-control", region_name=args.region),
        "lam": boto3.client("lambda", region_name=args.region),
        "ssm": boto3.client("ssm", region_name=args.region),
    }
    legs = audit(args.region, account, clients, args.bucket, args.memory_id)
    rc = report(legs, head)
    if args.json_out:
        with open(args.json_out, "w") as fh:
            json.dump({"region": args.region, "repo_head": head, "legs": legs,
                       "not_checked": list(NOT_CHECKED), "exit_code": rc}, fh, indent=2)
    return rc


if __name__ == "__main__":
    sys.exit(main())
