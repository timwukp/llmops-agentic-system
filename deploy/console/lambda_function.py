#!/usr/bin/env python3
"""
LLMOps Admin — AWS Lambda handler (HTTP API Gateway).

Operator dashboard for the llmops-agentic-system pipeline: one Lambda serves the
dashboard HTML (GET /) and the JSON API (GET/POST /api/*). Design and most of the
code are ported from bedrock-agentcore-agent-ops-console
(github.com/timwukp/bedrock-agentcore-agent-ops-console).

Auth model (ported): GET routes are public read-only; every POST except the three
session routes (/api/login, /api/refresh, /api/refresh/revoke) requires a Cognito access
token (validated server-side via cognito-idp GetUser). Those three are unauthenticated
because they establish, restore and end a session -- demanding a live token to recover
from having lost one is circular. Reload survival rides on an httpOnly refresh cookie
scoped to Path=/api/refresh, which page script cannot read.

The frontend ships as frontend.html in the same zip and is read ONCE at cold start
into a module global — no giant inline HTML string in this file.

Env: CONSOLE_TABLE, RUNS_TABLE, EVENTS_TABLE, DATA_BUCKET (optional — falls back to
     SSM /llmops/storage/bucket), STATE_MACHINE (name, default llmops-pipeline),
     START_FN (default llmops-start-pipeline), COGNITO_POOL_ID, COGNITO_CLIENT_ID,
     JUDGE_MODEL, SPANS_SINCE, OPTIMIZE_HARNESS (default llmops_orchestrator).
"""
import base64
import hashlib
import json
import os
import secrets
import time
from datetime import datetime, timedelta, timezone

import boto3
from boto3.dynamodb.conditions import Key

REGION = os.environ.get("AWS_REGION", "us-east-1")
ACCOUNT_ID = os.environ.get("ACCOUNT_ID") or boto3.client("sts", region_name=REGION).get_caller_identity()["Account"]

CONSOLE_TABLE = os.environ.get("CONSOLE_TABLE", "LlmopsAdminRuns")   # optimization drafts (opt- prefix)
RUNS_TABLE = os.environ.get("RUNS_TABLE", "llmops-pipeline-runs")    # pipeline runs (PK run_id)
EVENTS_TABLE = os.environ.get("EVENTS_TABLE", "llmops-stage-events") # stage events (PK run_id, SK sk)
ESTIMATES_TABLE = os.environ.get("ESTIMATES_TABLE", "llmops-cost-estimates")  # PK id
ACTUALS_TABLE = os.environ.get("ACTUALS_TABLE", "llmops-cost-actuals")        # PK project, SK sk
FINOPS_FN = os.environ.get("FINOPS_FN", "llmops-finops-reconcile")
PROJECT = os.environ.get("PROJECT", "llmops-agentic-system")
#: Budget references, compared two independent ways: this run's worst case against the
#: single-run figure, and project-to-date actual + this estimate against the cumulative
#: one, because a stream of $5,000 runs is the same $20,000 exposure as one $20,000 run.
#:
#: These literals must equal cost_model's DEFAULT_*_LIMIT_USD, and they cannot be imported
#: from it here: cost_model is loaded lazily by `_cost_model()` (the zip may be built
#: without it) and that needs `_HERE`, defined further down. So the agreement is pinned by
#: a test instead -- test_console_cost.py::
#: test_the_consoles_fallback_limits_equal_the_canonical_ones. Two copies of a number with
#: nothing comparing them is exactly how one falsified figure survived in four files; the
#: fix is not to trust the copy, it is to make a disagreement fail loudly. deploy.sh now
#: also SETS both env vars, read out of cost_model, so on a deployed function these
#: fallbacks are never what is in force.
APPROVAL_LIMIT_USD = float(os.environ.get("APPROVAL_LIMIT_USD", "20000"))
CUMULATIVE_LIMIT_USD = float(os.environ.get("CUMULATIVE_LIMIT_USD", "20000"))
#: advisory (default) = report the overage, launch anyway. blocking = the old gate.
#: The platform owner is the only approver here, so a gate could only ever ask them to
#: approve their own run; the budget is a reference instead. Overages are still named
#: and numbered on every surface -- advisory is not silent.
BUDGET_MODE = os.environ.get("BUDGET_MODE", "advisory")
#: Cognito group whose members may approve. Membership is checked server-side on every
#: approval call; hiding the button in the UI is not a control.
APPROVER_GROUP = os.environ.get("APPROVER_GROUP", "llmops-approver")
STATE_MACHINE = os.environ.get("STATE_MACHINE", "llmops-pipeline")
SM_ARN = f"arn:aws:states:{REGION}:{ACCOUNT_ID}:stateMachine:{STATE_MACHINE}"
START_FN = os.environ.get("START_FN", "llmops-start-pipeline")
SELF_FUNCTION = os.environ.get("AWS_LAMBDA_FUNCTION_NAME", "")
COGNITO_POOL_ID = os.environ.get("COGNITO_POOL_ID", "")
COGNITO_CLIENT_ID = os.environ.get("COGNITO_CLIENT_ID", "")
JUDGE_MODEL = os.environ.get("JUDGE_MODEL", "global.anthropic.claude-opus-5")
SPANS_SINCE_ENV = os.environ.get("SPANS_SINCE", "")
OPTIMIZE_HARNESS = os.environ.get("OPTIMIZE_HARNESS", "llmops_orchestrator")
ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "")  # same-origin by default — leave empty

TASKS_TABLE = os.environ.get("TASKS_TABLE", "llmops-tasks")            # PK id (task- prefix)
DS_GROUP = os.environ.get("DS_GROUP", "llmops-datascience")            # may create/chat tasks
APPROVAL_KEY = os.environ.get("APPROVAL_KEY", "alias/llmops-approval") # KMS signing key
LLMOPS_SNS_TOPIC = os.environ.get("LLMOPS_SNS_TOPIC", "")

ctl = boto3.client("bedrock-agentcore-control", region_name=REGION)
data = boto3.client("bedrock-agentcore", region_name=REGION)
s3 = boto3.client("s3", region_name=REGION)
lam = boto3.client("lambda", region_name=REGION)
sfn = boto3.client("stepfunctions", region_name=REGION)
sm = boto3.client("sagemaker", region_name=REGION)
ssm = boto3.client("ssm", region_name=REGION)
cw = boto3.client("cloudwatch", region_name=REGION)
logsc = boto3.client("logs", region_name=REGION)
brt = boto3.client("bedrock-runtime", region_name=REGION)
cognito = boto3.client("cognito-idp", region_name=REGION)
_ddb = boto3.resource("dynamodb", region_name=REGION)
console_tbl = _ddb.Table(CONSOLE_TABLE) if CONSOLE_TABLE else None
runs_tbl = _ddb.Table(RUNS_TABLE) if RUNS_TABLE else None
events_tbl = _ddb.Table(EVENTS_TABLE) if EVENTS_TABLE else None
estimates_tbl = _ddb.Table(ESTIMATES_TABLE) if ESTIMATES_TABLE else None
actuals_tbl = _ddb.Table(ACTUALS_TABLE) if ACTUALS_TABLE else None
tasks_tbl = _ddb.Table(TASKS_TABLE) if TASKS_TABLE else None
kms = boto3.client("kms", region_name=REGION)
sns = boto3.client("sns", region_name=REGION)
# Chat turns stream for minutes; the default 60s read timeout kills them and an
# auto-retry silently re-runs a whole turn — same hard-won config as the driver's.
from botocore.config import Config as _BotoConfig
agentcore_chat = boto3.client(
    "bedrock-agentcore", region_name=REGION,
    config=_BotoConfig(read_timeout=870, connect_timeout=30, retries={"max_attempts": 0}))

try:
    import conductor_tools  # vendored into the zip beside cost_model.py
except ImportError:  # repo layout (unit tests)
    import sys as _sys
    _sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "..", "..", "orchestration"))
    import conductor_tools  # type: ignore

EVAL_RESULTS_LG_PREFIX = "/aws/bedrock-agentcore/evaluations/results/"

# The 7 harnesses. Runtime name = "harness_" + harnessName. llmops_finops is the audit
# runtime: it sits beside llmops_orchestrator above the state machine rather than inside
# it, so it appears in the fleet view but never in a run's stage sequence.
HARNESS_NAMES = ["llmops_data_prep", "llmops_finetune", "llmops_eval",
                 "llmops_deploy", "llmops_monitor", "llmops_orchestrator",
                 "llmops_finops"]
WATCHED_RUNTIMES = [f"harness_{n}" for n in HARNESS_NAMES]

# ── frontend: read ONCE at cold start (replaces the source repo's inline string) ─
_HERE = os.path.dirname(os.path.abspath(__file__))
try:
    with open(os.path.join(_HERE, "frontend.html"), encoding="utf-8") as _f:
        FRONTEND_HTML = _f.read()
except Exception as _e:  # zip built without frontend.html — fail visibly, not blank
    FRONTEND_HTML = f"<h1>frontend.html missing from bundle: {_e}</h1>"

# ── architecture diagrams: bundled in the zip and served same-origin, because the
# CSP is connect-src 'self' — the Architecture tab fetches these instead of GitHub ─
ARCH_SVGS = {}
for _name in ("architecture-high-level.svg", "architecture-low-level.svg",
              "architecture-console.svg"):
    try:
        with open(os.path.join(_HERE, _name), encoding="utf-8") as _f:
            ARCH_SVGS[f"/docs/{_name}"] = _f.read()
    except Exception as _e:  # missing from the bundle — 404 with the reason, not a blank panel
        ARCH_SVGS[f"/docs/{_name}"] = None

# ── the Introduction tab's page and its narration audio ──────────────────────
# The page is small, so it is read once like frontend.html. The 35 MP3s total ~11 MB, so
# they are NOT held in memory: cold start only enumerates which (lang, scene) pairs the
# bundle actually contains, and each clip is read from the local filesystem on request.
#
# That enumeration is also the security boundary. `/intro/audio/<lang>/<scene>.mp3` puts
# two request-controlled segments into a filename, which is a directory traversal waiting
# to happen — and one that a regex over the path is easy to get subtly wrong. An
# allowlist built from what is on disk cannot be traversed: a pair the walk below did not
# find is a 404 before any path is joined.
try:
    with open(os.path.join(_HERE, "intro.html"), encoding="utf-8") as _f:
        INTRO_HTML = _f.read()
except Exception as _e:
    INTRO_HTML = None
    INTRO_ERR = str(_e)

INTRO_AUDIO_DIR = os.path.join(_HERE, "intro_audio")
#: {(lang, scene)} present in the deployed zip. Empty is a legitimate state, not a
#: failure: the page falls back to browser speech synthesis per missing clip, so a bundle
#: built without the audio speaks in a robot voice rather than showing a silent page.
INTRO_CLIPS = set()
try:
    for _lang in sorted(os.listdir(INTRO_AUDIO_DIR)):
        if not os.path.isdir(os.path.join(INTRO_AUDIO_DIR, _lang)):
            continue
        for _mp3 in sorted(os.listdir(os.path.join(INTRO_AUDIO_DIR, _lang))):
            if _mp3.endswith(".mp3"):
                INTRO_CLIPS.add((_lang, _mp3[:-4]))
except Exception:
    pass

_BUCKET_CACHE = None


def data_bucket():
    """S3 data bucket: env DATA_BUCKET, else SSM /llmops/storage/bucket (cached)."""
    global _BUCKET_CACHE
    if _BUCKET_CACHE:
        return _BUCKET_CACHE
    b = os.environ.get("DATA_BUCKET", "")
    if not b:
        b = ssm.get_parameter(Name="/llmops/storage/bucket")["Parameter"]["Value"]
    _BUCKET_CACHE = b
    return b


_SPANS_SINCE_CACHE = None


def spans_since():
    """The cutoff before which no session has spans: env, else SSM, else "" (no filter).

    Was a hardcoded `2026-07-28T12:00:00Z` in both this file and console/deploy.sh -- the
    hour always_on tracing came up in THIS account. That is a fact about one deployment,
    and it was wrong for every other in the direction that hides itself: a fresh
    deployment's runs are all NEWER than that date, so they pass the filter, get scored
    against spans that were never sampled, and come back as "failed sessions" with nothing
    pointing at the cutoff as the cause.

    The value now comes from the step that MAKES it true: 05_harnesses.py sets
    OTEL_TRACES_SAMPLER=always_on and stamps /llmops/observability/spans_since on its
    first deploy. Empty is the deliberate fallback rather than a date, because filtering
    nothing leaves unscoreable sessions visibly unscoreable, while a guessed date makes
    real sessions silently vanish from batch eval -- and a missing row prompts no
    questions, where a bad score does.
    """
    global _SPANS_SINCE_CACHE
    if _SPANS_SINCE_CACHE is not None:
        return _SPANS_SINCE_CACHE
    val = SPANS_SINCE_ENV
    if not val:
        try:
            val = ssm.get_parameter(
                Name="/llmops/observability/spans_since")["Parameter"]["Value"]
        except Exception:  # noqa: BLE001 — not deployed yet: filter nothing, never guess
            val = ""
    _SPANS_SINCE_CACHE = val
    return val


def _resolve_harness_id(name_or_id):
    """Accept a harnessId, a harnessName, or a short name; resolve via SSM first
    (/llmops/harness/<short-name>), then by listing."""
    if not name_or_id:
        name_or_id = OPTIMIZE_HARNESS
    short = name_or_id.replace("llmops_", "").replace("_", "-")
    try:
        return ssm.get_parameter(Name=f"/llmops/harness/{short}")["Parameter"]["Value"]
    except Exception:
        pass
    for h in ctl.list_harnesses().get("harnesses", []):
        hid = h.get("harnessId", "")
        if name_or_id in (hid, h.get("harnessName")) or hid.rsplit("-", 1)[0] == name_or_id:
            return hid
    return name_or_id  # last resort: assume it already is an id


def session_id(run_id, stage, task):
    """Deterministic, >=33 chars — ported from orchestration/harness_driver so the
    console reconstructs the exact session ids the pipeline used."""
    base = f"{run_id}-{stage}-{task}"
    if len(base) >= 33:
        return base[:100]
    return (base + "-" + hashlib.sha256(base.encode()).hexdigest())[:64]


# ── overview: fleet + runs + executions ───────────────────────────────────────
def list_fleet():
    out = []
    for h in ctl.list_harnesses().get("harnesses", []):
        if not str(h.get("harnessName", "")).startswith("llmops_"):
            continue
        item = {"name": h["harnessName"], "id": h["harnessId"], "status": h["status"],
                "version": h.get("harnessVersion")}
        try:
            d = ctl.get_harness(harnessId=h["harnessId"])["harness"]
            item["model"] = d.get("model", {}).get("bedrockModelConfig", {}).get("modelId")
            item["skillsCount"] = len(d.get("skills", []) or [])
            item["maxIterations"] = d.get("maxIterations")
            item["timeoutSeconds"] = d.get("timeoutSeconds")
            item["status"] = d.get("status", item["status"])
        except Exception as e:
            item["detailError"] = str(e)[:120]
        out.append(item)
    out.sort(key=lambda x: HARNESS_NAMES.index(x["name"]) if x["name"] in HARNESS_NAMES else 99)
    return out


def recent_runs(limit=10):
    if not runs_tbl:
        return []
    try:
        items = runs_tbl.scan(Limit=60).get("Items", [])
        items.sort(key=lambda x: str(x.get("created_at", "")), reverse=True)
        return items[:limit]
    except Exception:
        return []


def list_executions():
    """RUNNING executions first, then recent terminal ones."""
    out, seen = [], set()
    try:
        for status in (["RUNNING"], [None]):
            kw = {"stateMachineArn": SM_ARN, "maxResults": 10}
            if status[0]:
                kw["statusFilter"] = status[0]
            for e in sfn.list_executions(**kw).get("executions", []):
                if e["executionArn"] in seen:
                    continue
                seen.add(e["executionArn"])
                out.append({"name": e["name"], "arn": e["executionArn"], "status": e["status"],
                            "startDate": str(e.get("startDate", "")),
                            "stopDate": str(e.get("stopDate", ""))})
    except Exception as e:
        return {"error": str(e)[:200]}
    return out


def overview():
    return {"harnesses": list_fleet(), "runs": recent_runs(10), "executions": list_executions()}


# ── /api/pipeline: SFN execution history → logical stage flow ─────────────────
STAGE_FLOW = [
    ("data-prep-generate", "Data Prep · Generate"),
    ("data-prep-curate", "Data Prep · Curate"),
    ("finetune-launch", "Finetune · Launch"),
    ("finetune-analyze", "Finetune · Analyze"),
    ("eval-gate", "Eval Gate"),
    ("deploy", "Deploy"),
    ("smoke", "Smoke Test"),
    ("teardown", "Teardown"),
    ("complete", "Complete"),
]
# SFN state name -> logical stage (same mapping pattern as the source repo's
# GitHub step-name -> stage table). RemediateFinetune re-enters finetune-launch.
STATE_TO_STAGE = {
    "DataPrepGenerate": "data-prep-generate", "DataPrepCurate": "data-prep-curate",
    "FinetuneLaunch": "finetune-launch", "FinetuneAnalyze": "finetune-analyze",
    "RemediateFinetune": "finetune-launch", "EvalGate": "eval-gate",
    "Deploy": "deploy", "SmokeTest": "smoke", "Teardown": "teardown",
    "Complete": "complete",
}
TERMINAL_FAIL_STATES = {"EscalateFail", "Fail"}

#: The error a stage reports when it stopped to ASK a human, set by the driver's
#: handle_escalate (send_task_failure error="EscalatedToHuman").
#:
#: Reaching EscalateFail is NOT that signal, and treating it as one was wrong: the
#: state is the Catch target of 9 of the 11 stages, so every crash goes through it
#: too. A first pass at this used `escalated = name in TERMINAL_FAIL_STATES` and
#: painted genuine crashes amber; its negative-control test only passed because the
#: hand-written crash history omitted EscalateFail, which no real crashed run does.
#: The error string is the one place the two are actually distinguishable.
ESCALATION_ERRORS = {"EscalatedToHuman"}


def _exec_arn(execution):
    if execution.startswith("arn:"):
        return execution
    return f"arn:aws:states:{REGION}:{ACCOUNT_ID}:execution:{STATE_MACHINE}:{execution}"


#: Static per-stage config for the hover card, read from the DEPLOYED definition once
#: per container. Read, not hardcoded: the whole point of the card is to answer "which
#: AgentCore runtime is behind this box, and with what timeout" -- a second copy in
#: this file would answer for the version the console was packaged with, which is
#: exactly the kind of confidently-wrong answer the operator cannot detect.
_STAGE_CFG_CACHE = {}

#: Resolved AgentCore identity per LOGICAL harness id (llmops_data_prep -> the real
#: suffixed harness + the runtime that serves it). Separate cache: the ASL walk below
#: is pure parsing, this needs three AWS calls, and a failure in one must not blank
#: the other.
#:
#: TTL'd, unlike _STAGE_CFG_CACHE, and the difference is the point. That cache holds the
#: deployed state machine definition, which cannot change without a redeploy. This one
#: holds STATUS -- runtimeStatus/harnessStatus. Caching health forever means a warm
#: container keeps rendering "READY" for a harness that has since gone UPDATING or
#: failed: a stale read presented as a live one, which is the exact bug class this
#: function was written to remove. A guessed name and a 40-minute-old status are the
#: same lie told two ways.
_HARNESS_ID_CACHE = {}
#: 60s: the flow diagram polls every 30s (frontend.html PIPE_TIMER), so this still
#: collapses the fan-out (4 distinct harness ids x 9 calls each) while keeping any
#: displayed status at most one poll stale.
_HARNESS_ID_TTL_S = 60.0

#: Account-wide listings shared across the harness ids resolved in one pass.
_FLEET_WIDE_CACHE = {}


def _fleet_wide(key, fetch):
    """One account-wide listing per TTL, not one per harness id.

    ListAgentRuntimes and list_fleet() answer for the whole account, so calling them
    once per stage box asks the same question four times and bills for four. list_fleet()
    is the expensive one: ListHarnesses plus a GetHarness per harness, so ~8 calls, and
    the pipeline has 4 distinct harness ids across its 9 boxes -> ~40 calls where 10 do.
    On a 30s poll that is a self-inflicted throttling risk on the operator's only live
    view of the pipeline.

    Shares _HARNESS_ID_TTL_S for the same reason that cache has one at all: these
    listings carry status, and a cached status is a claim about right now.
    """
    hit = _FLEET_WIDE_CACHE.get(key)
    if hit and time.time() - hit[0] < _HARNESS_ID_TTL_S:
        return hit[1]
    val = fetch()
    _FLEET_WIDE_CACHE[key] = (time.time(), val)
    return val


def harness_identity(harness_id):
    """Resolve what is ACTUALLY behind a stage box, the way the driver resolves it.

    This used to be `f"harness_{harness_id}"` -- a string built by concatenation. It
    happens to equal the runtime's *display name*, but nothing checked that, and the
    name is the one part of the identity that is NOT unique. Live:

        agentRuntimeName: harness_llmops_data_prep                 <- what we printed
        agentRuntimeId  : harness_llmops_data_prep-D8SPwm7Kog      <- the real identity
        SSM harness id  : llmops_data_prep-KuSKXUaxyP              <- what the driver invokes

    So the card showed the least specific of three strings, unverified, and omitted the
    id and ARN an operator needs to grep CloudWatch or match an ARN in the console. It
    would also have kept printing that name after the runtime was deleted or renamed.
    Every field here is READ; anything that fails to resolve says so rather than falling
    back to a guess.

    The suffix derivation is copied from the driver's _resolve_harness_arn on purpose:
    if the two ever disagree, the card is naming a harness the pipeline does not invoke.
    """
    hit_cache = _HARNESS_ID_CACHE.get(harness_id)
    if hit_cache and time.time() - hit_cache[0] < _HARNESS_ID_TTL_S:
        return hit_cache[1]
    out = {}
    agent = harness_id.removeprefix("llmops_").replace("_", "-")
    try:
        out["harnessFullId"] = ssm.get_parameter(
            Name=f"/llmops/harness/{agent}")["Parameter"]["Value"]
    except Exception as exc:  # noqa: BLE001 — fail soft, per field
        out["harnessIdError"] = f"{type(exc).__name__}: {exc}"

    # Report the runtime only if a live one matches. Live-verified shapes:
    #   agentRuntimeName = "harness_llmops_data_prep"              (NOT unique)
    #   agentRuntimeId   = "harness_llmops_data_prep-D8SPwm7Kog"   (the identity)
    # So match on the name -- which is what "harness_<logical id>" actually equals --
    # but report the id, version and ARN, which are what an operator can act on. The
    # lookup is the point: a name that is merely constructed keeps being printed after
    # the runtime is renamed or deleted.
    try:
        want = {f"harness_{harness_id}"}
        if out.get("harnessFullId"):
            want.add(f"harness_{out['harnessFullId']}")
        runtimes = _fleet_wide("runtimes",
                               lambda: ctl.list_agent_runtimes().get("agentRuntimes", []))
        hit = next((r for r in runtimes
                    if r.get("agentRuntimeName") in want
                    or r.get("agentRuntimeId") in want
                    or str(r.get("agentRuntimeId", "")).rsplit("-", 1)[0] in want), None)
        out["runtime"] = hit["agentRuntimeName"] if hit else "unresolved"
        if hit:
            for src, dst in (("agentRuntimeId", "runtimeId"),
                             ("agentRuntimeVersion", "runtimeVersion"),
                             ("status", "runtimeStatus")):
                if hit.get(src):
                    out[dst] = hit[src]
    except Exception as exc:  # noqa: BLE001
        out["runtime"] = "unresolved"
        out["runtimeError"] = f"{type(exc).__name__}: {exc}"

    # Health/model/version, reusing the fleet listing the overview tab already does --
    # not a second listing path. On a red box the operator's next question is "is that
    # harness even READY, and on which model", and that used to need another window.
    try:
        for h in _fleet_wide("fleet", list_fleet):
            if h.get("name") == harness_id or h.get("id") == out.get("harnessFullId"):
                for src, dst in (("status", "harnessStatus"), ("model", "model"),
                                 ("version", "harnessVersion")):
                    if h.get(src):
                        out[dst] = h[src]
                break
    except Exception as exc:  # noqa: BLE001
        out["fleetError"] = f"{type(exc).__name__}: {exc}"

    _HARNESS_ID_CACHE[harness_id] = (time.time(), out)
    return out


def stage_config():
    if _STAGE_CFG_CACHE:
        return _STAGE_CFG_CACHE
    try:
        sm = sfn.describe_state_machine(
            stateMachineArn=f"arn:aws:states:{REGION}:{ACCOUNT_ID}:stateMachine:{STATE_MACHINE}")
        states = json.loads(sm["definition"])["States"]
    except Exception as exc:  # noqa: BLE001 — a hover card must never break the flow
        _STAGE_CFG_CACHE["_error"] = f"{type(exc).__name__}: {exc}"
        return _STAGE_CFG_CACHE
    for name, st in states.items():
        key = STATE_TO_STAGE.get(name)
        if not key:
            continue
        pay = (st.get("Parameters") or {}).get("Payload") or {}
        cfg = _STAGE_CFG_CACHE.setdefault(key, {})
        # RemediateFinetune maps onto finetune-launch, so do not let the second state
        # silently overwrite the first: record both state names it can run as.
        cfg.setdefault("states", []).append(name)
        for src, dst in (("harness_id", "harnessId"), ("stage", "stage"), ("task", "task")):
            if pay.get(src):
                cfg.setdefault(dst, pay[src])
        if st.get("TimeoutSeconds"):
            cfg.setdefault("timeoutSeconds", st["TimeoutSeconds"])
        fn = (st.get("Parameters") or {}).get("FunctionName") or ""
        if fn:
            cfg.setdefault("lambda", fn.rsplit(":", 1)[-1])
        if st.get("Retry"):
            cfg.setdefault("maxAttempts", st["Retry"][0].get("MaxAttempts"))
        cfg.setdefault("catch", [c.get("Next") for c in st.get("Catch", [])])
        # The AgentCore runtime, harness and health actually behind this box -- resolved
        # from SSM + AgentCore, never assembled from the logical name. See
        # harness_identity() for why the old concatenation was a bug and not a shortcut.
        if cfg.get("harnessId"):
            for fld, val in harness_identity(cfg["harnessId"]).items():
                cfg.setdefault(fld, val)
    return _STAGE_CFG_CACHE


def pipeline_detail(execution=None):
    if not execution:
        exs = list_executions()
        if isinstance(exs, dict) or not exs:
            # config included even with no execution: the hover card is documentation of
            # the deployed pipeline, useful before the first run rather than only after.
            return {"execution": None, "iteration": 0,
                    "stages": [{"key": k, "label": l, "status": "pending",
                                "config": stage_config().get(k, {})}
                               for k, l in STAGE_FLOW]}
        execution = exs[0]["arn"]
    arn = _exec_arn(execution)
    d = sfn.describe_execution(executionArn=arn)
    exec_status = d["status"]  # RUNNING | SUCCEEDED | FAILED | TIMED_OUT | ABORTED
    run_id = ""
    try:
        run_id = json.loads(d.get("input", "{}")).get("run_id", "")
    except Exception:
        pass

    entered, exited = {}, {}  # logical stage -> counts
    iteration, escalated, last_entered = 0, False, None
    # Per-stage forensics for the hover card: which SFN state, when, and how it ended.
    # Collected in the same single pass over the history -- the history is already the
    # most expensive call in this handler, and a second walk would double it.
    detail = {}          # logical stage -> dict
    cur_state = None     # state name of the most recent StateEntered, for error attribution
    token = None
    while True:
        kw = {"executionArn": arn, "maxResults": 1000}
        if token:
            kw["nextToken"] = token
        h = sfn.get_execution_history(**kw)
        for ev in h.get("events", []):
            det = ev.get("stateEnteredEventDetails")
            if det:
                name = det.get("name", "")
                cur_state = name
                if name == "IncrementIteration":
                    iteration += 1
                st = STATE_TO_STAGE.get(name)
                if st:
                    entered[st] = entered.get(st, 0) + 1
                    last_entered = st
                    d0 = detail.setdefault(st, {})
                    d0["state"] = name
                    d0.setdefault("enteredAt", str(ev.get("timestamp", "")))
                    d0["attempts"] = entered[st]
            det = ev.get("stateExitedEventDetails")
            if det:
                st = STATE_TO_STAGE.get(det.get("name", ""))
                if st:
                    exited[st] = exited.get(st, 0) + 1
                    detail.setdefault(st, {})["exitedAt"] = str(ev.get("timestamp", ""))
            # How a task ended, attributed to the stage whose state was last entered.
            # TaskTimedOut carries error States.Timeout; a driver-reported failure
            # carries the driver's own error string, which is the only place an
            # escalation is distinguishable from a crash.
            for fld in ("taskFailedEventDetails", "taskTimedOutEventDetails"):
                td = ev.get(fld)
                if not td:
                    continue
                err = td.get("error", "")
                if err in ESCALATION_ERRORS:
                    escalated = True
                st = STATE_TO_STAGE.get(cur_state or "")
                if st:
                    d0 = detail.setdefault(st, {})
                    d0["error"] = err
                    d0["cause"] = str(td.get("cause", ""))[:400]
        token = h.get("nextToken")
        if not token:
            break

    terminal = exec_status != "RUNNING"

    def _stop_status(key):
        """"Waiting on a human" vs "broken", decided per STAGE from its own error.

        Live: "Data Prep · Generate failed" was the first thing the operator saw. What
        the stage actually did was stop -- but not to ask anything. It finished teacher
        generation at its approved cap, called stage_complete twice (19:23:49, 19:26:20),
        and the driver died on an S3 AccessDenied writing the canonical report BEFORE it
        settled the task token, so the token parked until States.Timeout at 7200s.

        So amber would be wrong here too, and the run-wide flag that produced it was
        wrong in a second way: `escalated` was set from reaching EscalateFail, which is
        the Catch target of 9 of the 11 stages. Every crash lands there. Only the
        driver's error string separates "I asked you something" from "I broke".
        """
        return "escalated" if detail.get(key, {}).get("error") in ESCALATION_ERRORS \
            else "failed"

    stages = []
    for key, label in STAGE_FLOW:
        n_in, n_out = entered.get(key, 0), exited.get(key, 0)
        if n_in == 0:
            status = "skipped" if terminal else "pending"
        elif n_out >= n_in:
            status = "succeeded"
        elif not terminal:
            status = "running"
        else:  # entered but never exited on a terminal execution
            status = _stop_status(key)
        stages.append({"key": key, "label": label, "status": status,
                       "config": stage_config().get(key, {}),
                       **detail.get(key, {})})
    if terminal and exec_status != "SUCCEEDED" and last_entered:
        for st in stages:  # make the stop location explicit even if the state "exited" into Fail
            if st["key"] == last_entered and st["status"] not in ("running",):
                st["status"] = _stop_status(st["key"])
    return {"execution": {"arn": arn, "name": arn.rsplit(":", 1)[-1], "status": exec_status,
                          "startDate": str(d.get("startDate", "")),
                          "stopDate": str(d.get("stopDate", ""))},
            "runId": run_id, "stages": stages, "iteration": iteration,
            "escalated": escalated, "terminal": exec_status if terminal else None}


#: The sk prefix the driver parks human/conductor verdicts under (its DIRECTIVE_SK).
#: Duplicated deliberately rather than imported: the driver ships in a different
#: bundle, and the guard in tests/ asserts the two constants still agree, so drift
#: fails a test instead of silently splitting the timeline.
DIRECTIVE_SK = "directive#"

#: Upper bound of the stage-timeline sk range. Stage-event sks are ISO timestamps, so
#: they always begin with a digit; every non-event row in this table uses a named
#: `word#` prefix instead. In ASCII digits sort below every letter, so "A" is the
#: boundary between "a moment in this run" and "a record filed against this run".
#:
#: Bounding on "A" rather than on DIRECTIVE_SK is the difference between a fix and the
#: next instance of this bug: `lt(DIRECTIVE_SK)` would also exclude any prefix that
#: sorts after `directive#` (`finding#`, `note#`, ...), so the next prefix added would
#: vanish from BOTH lists with nothing to notice it. This bound excludes by *shape* --
#: unknown prefixes stay out of the stage timeline, which is what they are, and they
#: can never displace an event.
TIMELINE_SK_MAX = "A"


def _directive_view(d):
    """A parked verdict, projected for display.

    `deliverable` and `delivered` are carried deliberately: they are the whole point of
    the undeliverable-verdict fix. A verdict written against a run that can never read
    it must not render identically to one an agent acted on -- that indistinguishability
    is exactly how the data-prep escalation read as answered for three days.
    """
    return {"sk": str(d.get("sk", "")),
            "decision": str(d.get("decision", "")),
            "rationale": str(d.get("rationale", ""))[:500],
            "actor": str(d.get("actor", "")),
            "deliverable": str(d.get("deliverable", "")),
            "delivered": str(d.get("delivered", "")),
            "run_status_at_put": str(d.get("run_status_at_put", ""))}


def _timeline(run_id, limit=100):
    """(stage events, directives) for a run -- as two queries, not one filtered list.

    The driver's DIRECTIVE_SK comment claims the prefix keeps directives "out of the
    timeline the console renders". It did not: neither reader filtered on sk, and the
    prefix makes it *worse* than harmless. `"d" > "2"`, so every `directive#` row sorts
    AFTER every ISO-timestamped event, which puts them exactly where the frontend looks
    (`evs.slice(-25)`) -- and a directive row carries no `detail` attribute, so it
    renders as a blank line. Ten parked verdicts on a busy run therefore showed ten
    blank rows and pushed the ten newest real events off the screen.

    Splitting the query rather than filtering in Python is the load-bearing part: a
    single `Limit`-ed query spends its budget on directives before the events reach us,
    so filtering afterwards would leave a short timeline with no indication anything
    was dropped. Two `begins_with` ranges each get their own budget.

    Directives are RETURNED, not discarded -- a verdict is the audit record of what was
    decided, and #16 is the case study in what unread records cost. They are just kept
    out of the stage timeline so a reader can tell an event from an answer.
    """
    if not events_tbl:
        return [], []
    evs = events_tbl.query(
        KeyConditionExpression=Key("run_id").eq(run_id) & Key("sk").lt(TIMELINE_SK_MAX),
        Limit=int(limit)).get("Items", [])
    dirs = events_tbl.query(
        KeyConditionExpression=(Key("run_id").eq(run_id)
                                & Key("sk").begins_with(DIRECTIVE_SK)),
        ScanIndexForward=False, Limit=int(limit)).get("Items", [])
    return evs, dirs


# ── /api/run: manifest + stage events + training job + gate verdict ──────────
def run_detail(run_id):
    out = {"runId": run_id, "manifest": None, "events": [], "directives": [], "gates": [],
           "gateVerdict": None, "trainingJob": None}
    if not run_id:
        return {"error": "run_id required"}
    try:
        obj = s3.get_object(Bucket=data_bucket(), Key=f"runs/{run_id}/manifest.json")
        out["manifest"] = json.loads(obj["Body"].read())
    except Exception as e:
        out["manifestError"] = str(e)[:150]
    if events_tbl:
        try:
            evs, dirs = _timeline(run_id)
            out["events"] = evs
            out["directives"] = [_directive_view(d) for d in dirs]
        except Exception as e:
            out["eventsError"] = str(e)[:150]
    # gate verdict: thresholds from params.gates vs eval stage metrics
    man = out["manifest"] or {}
    gates_cfg = (man.get("params") or {}).get("gates") or {}
    eval_metrics = ((man.get("stages") or {}).get("eval") or {}).get("metrics") or {}
    for gname, threshold in gates_cfg.items():
        actual = eval_metrics.get(gname)
        passed = None
        try:
            if actual is not None:
                passed = float(actual) >= float(threshold)
        except Exception:
            pass
        row = {"name": gname, "threshold": threshold,
               "actual": actual, "passed": passed}
        # judge_score is decided by its Wilson interval, not the point estimate
        # (agents/eval gate bullet) -- surface the bounds so a "passed: None"
        # borderline row is legible as an escalation, not a rendering gap.
        if gname == "judge_score":
            for b in ("judge_score_ci_low", "judge_score_ci_high", "judge_n"):
                if eval_metrics.get(b) is not None:
                    row[b] = eval_metrics[b]
        out["gates"].append(row)
    gp = eval_metrics.get("gate_passed")
    out["gateVerdict"] = ("passed" if gp is True else "failed" if gp is False else None)
    # OOD layer: measured and reported, never gated -- rendered as its own
    # read-only block so it cannot be mistaken for a gate row.
    if isinstance(eval_metrics.get("ood"), dict):
        out["oodReport"] = eval_metrics["ood"]
    # training job (tolerate missing): job_name lives on the runs-table item
    job_name = ""
    if runs_tbl:
        try:
            job_name = str(runs_tbl.get_item(Key={"run_id": run_id}).get("Item", {}).get("job_name", ""))
        except Exception:
            pass
    if job_name:
        try:
            tj = sm.describe_training_job(TrainingJobName=job_name)
            out["trainingJob"] = {"name": job_name, "status": tj.get("TrainingJobStatus"),
                                  "secondaryStatus": tj.get("SecondaryStatus"),
                                  "failureReason": (tj.get("FailureReason") or "")[:200]}
        except Exception as e:
            out["trainingJob"] = {"name": job_name, "status": "UNKNOWN", "error": str(e)[:120]}
    return out


# ── observability (ported; watched = all 6 runtimes; + SageMaker tile) ───────
def observability(hours=24):
    """Per-harness runtime metrics from the AWS/Bedrock-AgentCore service namespace, plus
    account-level gen_ai token usage EMF metrics, plus a SageMaker jobs/endpoints tile."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=int(hours))
    watched = set(WATCHED_RUNTIMES)
    runtimes = {r["agentRuntimeName"]: r for r in ctl.list_agent_runtimes().get("agentRuntimes", [])
                if r["agentRuntimeName"] in watched}
    queries, order = [], []
    for i, (rname, r) in enumerate(runtimes.items()):
        arn = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT_ID}:runtime/{r['agentRuntimeId']}"
        dims = [{"Name": "Name", "Value": f"{rname}::DEFAULT"},
                {"Name": "Operation", "Value": "InvokeAgentRuntime"},
                {"Name": "Resource", "Value": arn}]
        for j, (metric, stat) in enumerate([("Invocations", "Sum"), ("Latency", "Average"),
                                            ("Sessions", "Sum"), ("SystemErrors", "Sum"),
                                            ("UserErrors", "Sum"), ("Throttles", "Sum")]):
            queries.append({"Id": f"m{i}_{j}", "MetricStat": {
                "Metric": {"Namespace": "AWS/Bedrock-AgentCore", "MetricName": metric, "Dimensions": dims},
                "Period": 3600 * int(hours), "Stat": stat}, "ReturnData": True})
            order.append((rname, metric, stat))
    # token usage: EMF gen_ai.client.token.usage — dims vary, list then sum input/output
    tok_metrics = cw.list_metrics(Namespace="bedrock-agentcore",
                                  MetricName="gen_ai.client.token.usage").get("Metrics", [])
    for k, m in enumerate(tok_metrics):
        ttype = next((d["Value"] for d in m["Dimensions"] if d["Name"] == "gen_ai.token.type"), "?")
        queries.append({"Id": f"t{k}", "MetricStat": {
            "Metric": m, "Period": 3600 * int(hours), "Stat": "Sum"}, "ReturnData": True})
        order.append(("_tokens", ttype, "Sum"))
    out = {rn: {} for rn in runtimes}
    tokens = {"input": 0, "output": 0}
    if queries:
        res = cw.get_metric_data(MetricDataQueries=queries, StartTime=start, EndTime=end)
        for q, (owner, metric, stat) in zip(res["MetricDataResults"], order):
            val = sum(q["Values"]) if stat == "Sum" else (sum(q["Values"]) / len(q["Values"]) if q["Values"] else 0)
            if owner == "_tokens":
                tokens[metric] = tokens.get(metric, 0) + val
            else:
                out[owner][metric] = round(val, 1)
    for rn in out:
        inv = out[rn].get("Invocations", 0)
        errs = out[rn].get("SystemErrors", 0) + out[rn].get("UserErrors", 0)
        out[rn]["ErrorRatePct"] = round(100.0 * errs / inv, 1) if inv else 0.0
    # daily invocation series for the trend chart (1 bucket/day)
    dq, dorder = [], []
    for i, (rname, r) in enumerate(runtimes.items()):
        arn = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT_ID}:runtime/{r['agentRuntimeId']}"
        dims = [{"Name": "Name", "Value": f"{rname}::DEFAULT"},
                {"Name": "Operation", "Value": "InvokeAgentRuntime"},
                {"Name": "Resource", "Value": arn}]
        dq.append({"Id": f"d{i}", "MetricStat": {
            "Metric": {"Namespace": "AWS/Bedrock-AgentCore", "MetricName": "Invocations",
                       "Dimensions": dims}, "Period": 86400, "Stat": "Sum"}, "ReturnData": True})
        dorder.append(rname)
    if dq:
        dres = cw.get_metric_data(MetricDataQueries=dq, StartTime=start, EndTime=end,
                                  ScanBy="TimestampAscending")
        for q, rname in zip(dres["MetricDataResults"], dorder):
            out[rname]["daily"] = [{"d": t.strftime("%m-%d"), "v": v}
                                   for t, v in zip(q["Timestamps"], q["Values"])]
    # SageMaker tile: last 10 llmops- training jobs + llmops-student endpoints
    sagemaker = {"trainingJobs": [], "endpoints": []}
    try:
        for tj in sm.list_training_jobs(NameContains="llmops-", SortBy="CreationTime",
                                        SortOrder="Descending", MaxResults=10).get("TrainingJobSummaries", []):
            sagemaker["trainingJobs"].append({"name": tj["TrainingJobName"],
                                              "status": tj["TrainingJobStatus"],
                                              "createdAt": str(tj.get("CreationTime", ""))})
    except Exception as e:
        sagemaker["trainingJobsError"] = str(e)[:150]
    try:
        for ep in sm.list_endpoints(NameContains="llmops-student", SortBy="CreationTime",
                                    SortOrder="Descending", MaxResults=10).get("Endpoints", []):
            sagemaker["endpoints"].append({"name": ep["EndpointName"],
                                           "status": ep["EndpointStatus"],
                                           "createdAt": str(ep.get("CreationTime", ""))})
    except Exception as e:
        sagemaker["endpointsError"] = str(e)[:150]
    return {"windowHours": int(hours), "harnesses": out,
            "tokens": {k: int(v) for k, v in tokens.items()},
            "tokenScope": "account-wide agent runtimes (EMF gen_ai.client.token.usage)",
            "sagemaker": sagemaker,
            "source": "AWS/Bedrock-AgentCore service metrics + DEFAULT log group EMF + SageMaker"}


# ── evaluations (ported unchanged: all configs, tolerant score extraction) ────
def evaluations():
    """Online evaluation configs + recent scores from the results log group."""
    cfgs = [c for c in ctl.list_online_evaluation_configs().get("onlineEvaluationConfigs", [])
            if str(c.get("onlineEvaluationConfigName", "")).startswith("llmops_")]
    # telemetry isolation: ui_qa_* configs belong to the agent-cicd-admin (CI/CD)
    # dashboard — showing them here conflates two different platforms' scores
    out = []
    for c0 in cfgs:
        cid = c0.get("onlineEvaluationConfigId")
        item = {"id": cid, "name": c0.get("onlineEvaluationConfigName"),
                "status": str(c0.get("status")), "executionStatus": str(c0.get("executionStatus", ""))}
        try:
            d = ctl.get_online_evaluation_config(onlineEvaluationConfigId=cid)
            cfg = d.get("onlineEvaluationConfig", d)
            item["evaluators"] = [e.get("evaluatorId") for e in cfg.get("evaluators", [])]
            item["insights"] = [i.get("insightId", "").replace("Builtin.Insight.", "")
                                for i in cfg.get("insights", [])]
            item["frequencies"] = (cfg.get("clusteringConfig") or {}).get("frequencies", [])
            item["sampling"] = cfg.get("rule", {}).get("samplingConfig", {}).get("samplingPercentage")
            item["logGroups"] = cfg.get("dataSourceConfig", {}).get("cloudWatchLogs", {}).get("logGroupNames", [])
            item["status"] = str(cfg.get("status", item["status"]))
        except Exception as e:
            item["detailError"] = str(e)[:150]
        # recent scores from results log group (honest: empty until evaluator has scored traffic).
        # filter_log_events without startTime can return an empty page + nextToken even when
        # events exist — pass an explicit window AND follow the token (bounded) or scores
        # that are really there render as "awaiting traffic".
        scores = []
        try:
            now_ms = int(time.time() * 1000)
            kw = {"logGroupName": EVAL_RESULTS_LG_PREFIX + cid, "limit": 50,
                  "startTime": now_ms - 14 * 24 * 3600 * 1000, "endTime": now_ms + 60_000}
            for _ in range(10):  # bounded pagination
                ev = logsc.filter_log_events(**kw)
                for e0 in ev.get("events", []):
                    try:
                        scores.append(json.loads(e0["message"]))
                    except Exception:
                        pass
                tok = ev.get("nextToken")
                if not tok or len(scores) >= 50:
                    break
                kw["nextToken"] = tok
            if not scores:
                item["scoresNote"] = "evaluator ACTIVE — awaiting scored traffic (runs on next harness invocation)"
        except logsc.exceptions.ResourceNotFoundException:
            item["scoresNote"] = "no results log group yet — evaluator has not scored any traffic"
        except Exception as e:
            item["scoresNote"] = f"results read error: {str(e)[:120]}"
        item["recentScores"] = scores[-20:]
        # aggregate per evaluator — tolerant extraction, the preview result schema may drift
        agg = {}
        for j in scores:
            att = j.get("attributes") or {}
            name = (att.get("gen_ai.evaluation.name") or j.get("evaluatorId") or j.get("evaluator")
                    or j.get("evaluatorName") or j.get("metricName") or "")
            if name == "gen_ai.evaluation.result":
                name = att.get("gen_ai.evaluation.name", "")
            val = att.get("gen_ai.evaluation.score.value")
            val = float(val) if isinstance(val, (int, float)) else None
            for k in ("score", "value", "result", "metricValue"):
                if val is not None:
                    break
                v = j.get(k)
                if isinstance(v, (int, float)):
                    val = float(v)
                    break
                if isinstance(v, dict):
                    for kk in ("value", "score"):
                        if isinstance(v.get(kk), (int, float)):
                            val = float(v[kk])
                            break
                if val is not None:
                    break
            if name and val is not None:
                a = agg.setdefault(str(name).replace("Builtin.", ""), {"n": 0, "sum": 0.0})
                a["n"] += 1
                a["sum"] += val
        item["scoreStats"] = {k: {"count": v["n"], "avg": round(v["sum"] / v["n"], 3)}
                              for k, v in agg.items() if v["n"]}
        out.append(item)
    return {"configs": out,
            "note": "Online evaluations score live harness traces (preview). Scores also surface in "
                    "CloudWatch → AgentCore Observability."}


# ── batch evaluations + insights (DATA-plane ops, ported) ─────────────────────
# Session-id source differs from the source repo: the pipeline's session ids are
# deterministic <run_id>-<stage>-<task>, so we reconstruct them from recent run ids.
STAGE_TASKS = [("data-prep", "generate"), ("data-prep", "curate"),
               ("finetune", "launch"), ("finetune", "analyze"), ("finetune", "remediate"),
               ("eval", "gate"), ("deploy", "deploy"), ("deploy", "smoke"), ("deploy", "teardown")]


def _recent_session_ids(cap=20, stage_filter=None):
    """Session ids for stage/tasks that ACTUALLY RAN, reconstructed from the
    stage-events table (fabricating ids for never-executed stage/task combos
    just produces 'failed sessions' — no spans exist for them).

    stage_filter: sessions live on the harness that RAN them, so batch-eval
    fan-out sends each runtime only its own stage's session ids."""
    sids = []
    for it in recent_runs(30):
        rid = str(it.get("run_id", ""))
        if not rid:
            continue
        cutoff = spans_since()
        if cutoff and str(it.get("created_at", "")) < cutoff:
            continue
        ran = set()
        try:
            for ev0 in events_tbl.query(KeyConditionExpression=Key("run_id").eq(rid)).get("Items", []):
                stage = str(ev0.get("sk", "")).split("#")[1] if "#" in str(ev0.get("sk", "")) else ""
                task = ""
                try:
                    task = json.loads(ev0.get("detail", "{}")).get("task", "")
                except Exception:
                    pass
                if stage:
                    ran.add((stage, task))
        except Exception:
            pass
        # A stage that outran AgentCore's 8h session lifetime rolled to `-e<N>` session
        # ids, which no (run, stage, task) formula reproduces. The driver records each
        # one on the run row; those are real sessions with real spans, so score them.
        rolled = [s for s in (it.get("rolled_session_ids") or [])
                  if not stage_filter or f"-{stage_filter}-" in str(s)]
        for stage, task in STAGE_TASKS:
            if stage_filter and stage != stage_filter:
                continue
            # include only combos this run actually executed (task match when known)
            if ran and not any(s == stage and (t == task or not t) for s, t in ran):
                continue
            sids.append(session_id(rid, stage, task))
            if len(sids) >= cap:
                return sids
        for s in rolled:
            sids.append(str(s))
            if len(sids) >= cap:
                return sids
    return sids


def _pipeline_runtimes():
    """The five pipeline-stage runtimes (orchestrator sessions never match the
    reconstructed pipeline session ids, so it is excluded from batch scoring)."""
    names = [n for n in WATCHED_RUNTIMES if "orchestrator" not in n]
    out = []
    for r in ctl.list_agent_runtimes().get("agentRuntimes", []):
        if r["agentRuntimeName"] in names:
            out.append({"name": r["agentRuntimeName"],
                        "lg": f"/aws/bedrock-agentcore/runtimes/{r['agentRuntimeId']}-DEFAULT",
                        "svc": f"{r['agentRuntimeName']}.DEFAULT"})
    return out


def _eval_data_source_for(rt, sids):
    """One runtime per call — live-verified API constraint: serviceNames caps at
    ONE entry (and logGroupNames at five), so multi-runtime scoring fans out."""
    return {"cloudWatchLogs": {"serviceNames": [rt["svc"]], "logGroupNames": [rt["lg"]],
                               "filterConfig": {"sessionIds": sids}}}


def start_batch_eval():
    """Fan out one batch evaluation per pipeline runtime (serviceNames caps at 1)."""
    started, errors = [], []
    for rt in _pipeline_runtimes():
        stage = rt["name"].replace("harness_llmops_", "")[:12]
        ddb_stage = stage.replace("_", "-")  # runtime names use _, stage names use -
        sids = _recent_session_ids(cap=20, stage_filter=ddb_stage)
        if not sids:
            continue  # no sessions for this stage in the window
        try:
            r = data.start_batch_evaluation(
                batchEvaluationName=f"llmops_be_{stage}_{secrets.token_hex(3)}",  # [a-zA-Z][a-zA-Z0-9_]{0,47}
                evaluators=[{"evaluatorId": "Builtin.Correctness"},
                            {"evaluatorId": "Builtin.GoalSuccessRate"},
                            {"evaluatorId": "Builtin.ToolSelectionAccuracy"}],
                dataSourceConfig=_eval_data_source_for(rt, sids),
                clientToken=secrets.token_hex(20),
                description=f"LLMOps admin dashboard — {stage} stage sessions")
            started.append({"stage": stage, "id": r.get("batchEvaluationId"),
                            "status": str(r.get("status"))})
        except Exception as e:  # collect per-runtime failures, don't abort the fan-out
            errors.append({"stage": stage, "error": str(e)[:200]})
    if not started and not errors:
        return {"error": "no recent pipeline runs after SPANS_SINCE to score — start a run first"}
    return {"started": started, "errors": errors}


def list_batch_evaluations():
    """Batch evaluations via the DATA-plane SDK. Returns {'error':...} on upstream
    failure so the caller can tell 'API down' apart from 'none yet'."""
    try:
        out = []
        evals = [b for b in data.list_batch_evaluations().get("batchEvaluations", [])
                 if str(b.get("batchEvaluationName", "")).startswith("llmops_be_")]
        # llmops_be_ prefix isolates this platform's scores (ui_qa_* = CI/CD stack)
        # and excludes llmops_ins_* (insights render in their own panel)
        for be in evals[:10]:
            item = {"id": be.get("batchEvaluationId"), "name": be.get("batchEvaluationName"),
                    "status": str(be.get("status")), "createdAt": str(be.get("createdAt", ""))}
            try:
                d = data.get_batch_evaluation(batchEvaluationId=item["id"])
                bd = d.get("batchEvaluation", d)
                res = bd.get("evaluationResults", {})
                item["sessions"] = {"total": res.get("totalNumberOfSessions", 0),
                                    "completed": res.get("numberOfSessionsCompleted", 0),
                                    "failed": res.get("numberOfSessionsFailed", 0)}
                item["evaluatorSummaries"] = [
                    {"evaluator": e0.get("evaluatorId", "").replace("Builtin.", ""),
                     "avg": (e0.get("statistics") or {}).get("averageScore"),
                     "evaluated": e0.get("totalEvaluated", 0), "failed": e0.get("totalFailed", 0)}
                    for e0 in res.get("evaluatorSummaries", [])]
            except Exception:
                pass
            out.append(item)
        out.sort(key=lambda x: x.get("createdAt", ""), reverse=True)
        return out
    except Exception as e:
        return {"error": str(e)[:200]}


def start_insights_report():
    """Fan out one insights report per pipeline runtime (serviceNames caps at 1)."""
    started, errors = [], []
    # Service quota: max 5 CONCURRENT batch evaluations account-wide — insights
    # runs per-stage too, so cap the fan-out at the two most informative stages
    # when others are running (data-prep + finetune carry most failure signal).
    for rt in _pipeline_runtimes():
        stage = rt["name"].replace("harness_llmops_", "")[:12]
        ddb_stage = stage.replace("_", "-")
        sids = _recent_session_ids(cap=20, stage_filter=ddb_stage)
        if not sids:
            continue
        try:
            r = data.start_batch_evaluation(
                batchEvaluationName=f"llmops_ins_{stage}_{secrets.token_hex(3)}",
                insights=[{"insightId": "Builtin.Insight.FailureAnalysis"},
                          {"insightId": "Builtin.Insight.UserIntent"},
                          {"insightId": "Builtin.Insight.ExecutionSummary"}],
                dataSourceConfig=_eval_data_source_for(rt, sids),
                clientToken=secrets.token_hex(20),
                description=f"LLMOps admin dashboard insights — {stage} stage")
            started.append({"stage": stage, "id": r.get("batchEvaluationId"),
                            "status": str(r.get("status"))})
        except Exception as e:
            errors.append({"stage": stage, "error": str(e)[:200]})
    if not started and not errors:
        return {"error": "no recent pipeline runs after SPANS_SINCE to analyze — start a run first"}
    return {"started": started, "errors": errors}


def get_insights_report(bid):
    d = data.get_batch_evaluation(batchEvaluationId=bid)
    be = d.get("batchEvaluation", d)
    return {"id": bid, "status": str(be.get("status")),
            "failures": (be.get("failureAnalysisResult") or {}).get("failures", []),
            "intents": (be.get("userIntentResult") or {}).get("userIntents", []),
            "summaries": (be.get("executionSummaryResult") or {}).get("executionSummaries", [])}


def list_insights_reports():
    out = []
    try:
        for be in data.list_batch_evaluations().get("batchEvaluations", []):
            if str(be.get("batchEvaluationName", "")).startswith("llmops_ins_"):
                out.append({"id": be.get("batchEvaluationId"), "name": be.get("batchEvaluationName"),
                            "status": str(be.get("status")), "createdAt": str(be.get("createdAt", ""))})
        out.sort(key=lambda x: x.get("createdAt", ""), reverse=True)
    except Exception:
        pass
    return out[:5]


# ── optimizations: AWS-native recommendations + Bedrock-drafted prompts ───────
def _runtime_for(harness_name):
    return next((r for r in ctl.list_agent_runtimes().get("agentRuntimes", [])
                 if r["agentRuntimeName"] == f"harness_{harness_name}"), None)


def list_native_recommendations():
    """AWS-native Optimizations Recommendations (data-plane SDK)."""
    try:
        out = []
        recs = [r for r in data.list_recommendations().get("recommendationSummaries", [])
                if str(r.get("name", "")).startswith("llmops_")]  # isolate from ui_qa_* (CI/CD stack)
        for rec in recs[:10]:
            item = {"id": rec.get("recommendationId"), "name": rec.get("name"),
                    "status": str(rec.get("status")), "type": rec.get("type"),
                    "createdAt": str(rec.get("createdAt", ""))}
            if item["status"] == "COMPLETED":
                try:
                    d = data.get_recommendation(recommendationId=item["id"])
                    rr = d.get("recommendation", d).get("recommendationResult", {})
                    spr = rr.get("systemPromptRecommendationResult", {})
                    item["recommendedPrompt"] = spr.get("recommendedSystemPrompt", "")
                    item["explanation"] = (spr.get("explanation") or "")[:400]
                except Exception:
                    pass
            out.append(item)
        out.sort(key=lambda x: x.get("createdAt", ""), reverse=True)
        return out
    except Exception as e:
        return {"error": str(e)[:200]}


def start_native_recommendation(harness=None):
    """AWS-native system-prompt recommendation from the last 12h of the target
    harness's traces. Enum SYSTEM_PROMPT_RECOMMENDATION; max ONE evaluator."""
    hname = harness or OPTIMIZE_HARNESS
    hid = _resolve_harness_id(hname)
    h = ctl.get_harness(harnessId=hid)["harness"]
    hname = h.get("harnessName", hname)
    cur = (h.get("systemPrompt") or [{}])[0].get("text", "")
    rt = _runtime_for(hname)
    if not rt:
        return {"error": f"no runtime harness_{hname} found"}
    lg_arn = (f"arn:aws:logs:{REGION}:{ACCOUNT_ID}:log-group:"
              f"/aws/bedrock-agentcore/runtimes/{rt['agentRuntimeId']}-DEFAULT")
    end = datetime.now(timezone.utc)
    r = data.start_recommendation(
        name="llmops_rec_" + secrets.token_hex(3),
        type="SYSTEM_PROMPT_RECOMMENDATION",     # enum, not "SYSTEM_PROMPT"
        recommendationConfig={"systemPromptRecommendationConfig": {
            "systemPrompt": {"text": cur},
            "agentTraces": {"cloudwatchLogs": {
                "logGroupArns": [lg_arn],
                "serviceNames": [f"harness_{hname}.DEFAULT"],
                "startTime": end - timedelta(hours=12), "endTime": end}},
            "evaluationConfig": {"evaluators": [   # max ONE evaluator
                {"evaluatorArn": "arn:aws:bedrock-agentcore:::evaluator/Builtin.GoalSuccessRate"}]}}},
        clientToken=secrets.token_hex(20))
    return {"id": r.get("recommendationId"), "status": str(r.get("status")), "harness": hname}


def apply_native_recommendation(rec_id, harness=None):
    d = data.get_recommendation(recommendationId=rec_id)
    rec = d.get("recommendation", d)
    spr = rec.get("recommendationResult", {}).get("systemPromptRecommendationResult", {})
    prompt = spr.get("recommendedSystemPrompt", "")
    if not prompt:
        return {"error": "recommendation has no completed prompt"}
    hid = _resolve_harness_id(harness or OPTIMIZE_HARNESS)
    ctl.update_harness(harnessId=hid, systemPrompt=[{"text": prompt}],
                       clientToken=secrets.token_hex(20))  # clientToken >= 33 chars
    return {"ok": True, "applied": rec_id, "harnessId": hid}


def list_optimizations():
    if not console_tbl:
        return []
    try:
        items = [i for i in console_tbl.scan(Limit=50).get("Items", [])
                 if str(i.get("id", "")).startswith("opt-")]
        items.sort(key=lambda x: x.get("startedAt", ""), reverse=True)
        return items[:10]
    except Exception:
        return []


def enqueue_optimization(now_iso, harness=None):
    """API Gateway caps integrations at 30s; Bedrock generation takes longer.
    Enqueue a placeholder and self-invoke async (ported pattern)."""
    opt_id = "opt-" + secrets.token_hex(5)
    hid = _resolve_harness_id(harness or OPTIMIZE_HARNESS)
    if console_tbl:
        console_tbl.put_item(Item={"id": opt_id, "startedAt": now_iso,
                                   "status": "generating", "harnessId": hid})
    if SELF_FUNCTION:
        lam.invoke(FunctionName=SELF_FUNCTION, InvocationType="Event",
                   Payload=json.dumps({"mode": "optimize", "opt_id": opt_id,
                                       "harness_id": hid, "now_iso": now_iso}).encode())
    return {"id": opt_id, "status": "generating",
            "note": "generation runs async (~30s); refresh the Optimizations panel"}


def generate_optimization(now_iso, opt_id=None, harness_id=None):
    """Draft an improved system prompt via Bedrock from the harness's current prompt,
    recent run outcomes and eval scores; store for human review; apply via UpdateHarness."""
    hid = harness_id or _resolve_harness_id(OPTIMIZE_HARNESS)
    h = ctl.get_harness(harnessId=hid)["harness"]
    cur = h.get("systemPrompt") or []
    cur_text = cur[0].get("text", "") if cur else ""
    # recent run outcomes: status/current_stage + latest run's gate metrics
    outcomes = [{"run_id": str(i.get("run_id", "")), "status": str(i.get("status", "")),
                 "stage": str(i.get("current_stage", ""))} for i in recent_runs(5)]
    gate_metrics = {}
    if outcomes:
        try:
            latest = run_detail(outcomes[0]["run_id"])
            gate_metrics = {"gates": latest.get("gates", []), "verdict": latest.get("gateVerdict")}
        except Exception:
            pass
    evs = evaluations()["configs"]
    ev_summary = "; ".join(
        f"{e['name']}={e['status']} stats={json.dumps(e.get('scoreStats', {}), default=str)}"
        for e in evs) or "none"
    prompt = (
        "You are optimizing the system prompt of an agent in an LLMOps fine-tuning pipeline "
        "(Bedrock AgentCore harness). The pipeline distills a teacher model into a small student "
        "via QLoRA on SageMaker, gates on eval metrics, and deploys behind a smoke test.\n"
        f"HARNESS: {h.get('harnessName', hid)}\n"
        f"CURRENT SYSTEM PROMPT:\n{cur_text}\n\n"
        f"RECENT RUN OUTCOMES (JSON): {json.dumps(outcomes, default=str)[:2000]}\n"
        f"LATEST GATE METRICS (JSON): {json.dumps(gate_metrics, default=str)[:1500]}\n"
        f"ONLINE EVALUATIONS: {ev_summary[:1500]}\n\n"
        "Propose ONE improved system prompt that keeps current strengths, makes the stage_complete "
        "reporting contract explicit (outputs, metrics, evidence with fixed field names), and "
        "addresses whatever the run outcomes and eval scores show is weakest. Reply as JSON: "
        '{"proposed_prompt": "...", "rationale": "...", "expected_improvements": ["..."]}')
    resp = brt.converse(modelId=JUDGE_MODEL,
                        messages=[{"role": "user", "content": [{"text": prompt}]}],
                        inferenceConfig={"maxTokens": 4000, "temperature": 0.2})
    txt = resp["output"]["message"]["content"][0]["text"].strip()
    # strip markdown code fences the model sometimes wraps around JSON
    if txt.startswith("```"):
        txt = txt.split("\n", 1)[1] if "\n" in txt else txt
        if txt.rstrip().endswith("```"):
            txt = txt.rstrip()[:-3]
    try:
        start = txt.index("{"); end = txt.rindex("}") + 1
        rec = json.loads(txt[start:end])
    except Exception:
        rec = {"proposed_prompt": txt, "rationale": "model returned non-JSON; raw text kept",
               "expected_improvements": []}
    item = {"id": opt_id or ("opt-" + secrets.token_hex(5)), "startedAt": now_iso,
            "status": "proposed", "harnessId": hid, "currentPrompt": cur_text,
            "proposedPrompt": rec.get("proposed_prompt", ""), "rationale": rec.get("rationale", ""),
            "expectedImprovements": rec.get("expected_improvements", []), "model": JUDGE_MODEL}
    if console_tbl:
        console_tbl.put_item(Item=item)
    return item


def apply_optimization(opt_id, now_iso):
    if not console_tbl:
        return {"error": "no console table"}
    item = console_tbl.get_item(Key={"id": opt_id}).get("Item")
    if not item or not item.get("proposedPrompt"):
        return {"error": "recommendation not found"}
    ctl.update_harness(harnessId=item["harnessId"],
                       systemPrompt=[{"text": item["proposedPrompt"]}],
                       clientToken=secrets.token_hex(20))
    console_tbl.update_item(Key={"id": opt_id},
                            UpdateExpression="SET #s = :s, appliedAt = :t",
                            ExpressionAttributeNames={"#s": "status"},
                            ExpressionAttributeValues={":s": "applied", ":t": now_iso})
    return {"ok": True, "applied": opt_id, "harnessId": item["harnessId"]}


# ── start a pipeline run ──────────────────────────────────────────────────────
def start_run(body):
    params = {}
    for k in ("task_count", "sample_count"):
        if body.get(k) not in (None, ""):
            try:
                params[k] = int(body[k])
            except Exception:
                pass
    if body.get("note"):
        params["note"] = str(body["note"])[:200]

    # ── the cost gate ────────────────────────────────────────────────────────
    # An estimate_id is optional: every run before this feature launched without one,
    # and keeping that path legal is what lets the variance report state honestly what
    # fraction of spend was never estimated. But when one IS supplied, it is the gate.
    est = None
    if body.get("estimate_id"):
        est = _get_estimate(body["estimate_id"])
        if not est:
            return {"error": "unknown estimate_id", "status_code": 400}
        status = est.get("status")
        try:
            gate = json.loads(est.get("gate", "{}"))
        except Exception:
            gate = {}
        # Re-derive the gate at launch time. The estimate may have been priced when
        # project-to-date was low; launching it a week later is a different exposure,
        # and a gate that trusts a stale verdict is no gate.
        fresh = gate_decision(_f(est.get("worst_case_usd")))
        needs_approval = bool(gate.get("approval_required")
                              or fresh.get("approval_required"))
        cm = _cost_model()
        if needs_approval:
            # Only reachable in blocking mode; advisory never sets approval_required.
            # Delegated so the console and the pipeline agree on what "may launch"
            # means; a second copy of this rule is a copy that can drift.
            ok = cm.can_launch(est) if cm else {
                "ok": False, "code": 503,
                "error": "cost model unavailable — cannot verify the approval status"}
            if not ok.get("ok"):
                return {"error": (ok.get("error", "not launchable") + ". "
                                  + "; ".join(fresh.get("reasons", []))),
                        "status_code": int(ok.get("code", 409)), "gate": fresh}
        if status in ("rejected", "launched"):
            # NOT a budget check, which is why it survives advisory mode. Both statuses
            # are terminal for reasons that have nothing to do with the amount: a
            # rejection someone recorded must not be relaunched behind their back, and
            # relaunching a launched estimate attaches two runs to one record and
            # double-counts it in the variance report.
            #
            # This was an `elif` on the approval branch until advisory mode. That was
            # NOT exploitable -- checked by reverting it, which changed no test: when
            # needs_approval was true, can_launch already refused everything except
            # status=="approved", so rejected/launched were rejected one branch earlier.
            # It is a plain `if` now because advisory mode never sets approval_required,
            # so the elif would have been dead code and the terminal check would have
            # depended on a budget verdict it has nothing to do with.
            return {"error": f"estimate is {status} and cannot launch",
                    "status_code": 409}
        for k in ("task_count", "sample_count"):
            if k not in params:
                try:
                    params[k] = int(json.loads(est.get("plan", "{}")).get(k))
                except Exception:
                    pass

    # The priced plan travels to start-pipeline as `plan`, which is what makes the run
    # the run that was approved. It used to be scraped for exactly two integers, so the
    # other seven fields the estimator PRICED -- both instance types, teacher_model,
    # harness_model, endpoint_hours, keep_reasoning, teardown -- were dropped, and the
    # pipeline ran on ARC-shaped defaults instead. That is unobservable afterwards: the
    # variance report joins this estimate to that run's actuals and reports the gap as an
    # underspend rather than as two different runs. start-pipeline gives `plan` precedence
    # over `params` and refuses a contradiction, so the two integers above staying in
    # `params` is redundant-but-consistent, not a second source of truth.
    payload = {"trigger_source": "console", "params": params}
    if est is not None:
        try:
            priced = json.loads(est.get("plan", "{}"))
        except Exception:
            priced = {}
        if isinstance(priced, dict) and priced:
            payload["plan"] = priced

    r = lam.invoke(FunctionName=START_FN, InvocationType="RequestResponse",
                   Payload=json.dumps(payload).encode())
    try:
        out = json.loads(r["Payload"].read())
    except Exception:
        out = {"note": "started (no parseable response)"}
    if r.get("FunctionError"):
        return {"error": f"start-pipeline failed: {json.dumps(out, default=str)[:300]}"}

    if est is not None and estimates_tbl:
        # Stamp the run back onto the estimate. Without this link the variance report
        # has an estimate and an actual it cannot join, which is the whole point.
        try:
            estimates_tbl.update_item(
                Key={"id": est["id"]},
                UpdateExpression=("SET #s = :s, run_id = :r, sfn_execution_arn = :a, "
                                  "launched_at = :t"),
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":s": "launched",
                    ":r": str(out.get("run_id") or out.get("runId") or ""),
                    ":a": str(out.get("execution_arn")
                              or out.get("executionArn") or ""),
                    ":t": datetime.now(timezone.utc).isoformat()})
        except Exception as e:
            # The run is already going; failing the response here would tell the caller
            # the launch failed when it did not. Log loudly instead.
            print(f"[finops] could not stamp estimate {est['id']}: {e}")
            out["warning"] = "run started but the estimate record was not updated"
        out["estimate_id"] = est["id"]
        # The re-derived verdict travels with the launch, not just with the refusal.
        # In advisory mode nothing above stopped this run, so this is the ONLY place the
        # caller learns it went over -- and a budget that is neither enforced nor
        # mentioned is not a reference, it is a number nobody will ever act on.
        out["gate"] = fresh
        if fresh.get("over_budget") or fresh.get("budget_unknown"):
            out["budget_notice"] = "; ".join(
                fresh.get("over_budget") or fresh.get("notes") or [])
    return {"ok": True, "result": out}


# ── FinOps: estimates, the dual approval gate, actuals rollup ─────────────────
# Design premise for everything below: an estimate is a guess, but an *actual* must
# never be a guess. Every figure returned carries where it came from and whether it has
# settled, because a confidently-wrong cost number is worse than an admitted unknown —
# someone approves real spend on it.

def _cost_model():
    """Import the canonical estimator lazily.

    The zip may be built without pipeline/ (the console deploys independently of the
    pipeline package). Failing here must degrade the Cost tab, not 500 the whole
    dashboard, so the caller turns None into a visible 'estimator unavailable'.
    """
    try:
        import sys
        for cand in (os.path.join(_HERE, "pipeline", "contracts"),
                     os.path.join(_HERE, "contracts"), _HERE):
            if cand not in sys.path:
                sys.path.insert(0, cand)
        import cost_model  # noqa: PLC0415 — deliberately lazy
        return cost_model
    except Exception as e:
        print(f"[finops] cost_model unavailable: {e}")
        return None


def _rate_card_doc():
    """The rate-card DOCUMENT from S3 as a plain dict, or None.

    Separate from _rate_card() because RateCard keeps only the SKU table: the document's
    generated_at / rate_precedence / health fields do not survive the constructor. A
    caller that needs them (rate_card_for_prompt) cannot get them from a RateCard, and
    reaching for a `.doc` attribute that does not exist fails SILENTLY through getattr —
    which would make the console tell every customer it cannot quote prices.
    """
    try:
        o = s3.get_object(Bucket=data_bucket(),
                          Key="finops/rates/rate_card_latest.json")
        doc = json.loads(o["Body"].read())
        return doc if isinstance(doc, dict) else None
    except Exception as e:
        print(f"[finops] no rate card: {e}")
        return None


def _rate_card():
    """Latest rate card from S3 as a RateCard, or None.

    Never fabricate rates on a read failure — an empty card makes every SKU 'unpriced',
    which is visibly not-an-estimate; a silently-substituted default would produce a
    plausible wrong number instead. Returning None rather than an empty RateCard keeps
    "we could not read the card" distinguishable from "the card is empty".
    """
    cm = _cost_model()
    if cm is None:
        return None
    doc = _rate_card_doc()
    if doc is None:
        return None
    # RateCard unwraps the document itself now. Repeating doc.get("rates", doc)
    # here is how the knowledge stayed in ONE caller while every other caller --
    # including an agent following the orchestrator prompt's instruction to read
    # this exact file -- died on a ValueError from dict('rate_card').
    return cm.RateCard(doc)


def _f(v, default=0.0):
    """DynamoDB gives Decimal; JSON needs float. Decimal survives json.dumps only via
    the default= hook, which would stringify it and break arithmetic in the browser."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def project_to_date_usd(project=None):
    """Actual settled+provisional spend recorded for this project, all periods.

    Reads the ledger the finops agent writes. Audit/finding rows are skipped: they are
    the agent's own notes, and summing them would double-count the very variances they
    describe.
    """
    if not actuals_tbl:
        return 0.0, 0
    total, n = 0.0, 0
    try:
        resp = actuals_tbl.query(
            KeyConditionExpression=Key("project").eq(project or PROJECT))
        for it in resp.get("Items", []):
            sk = str(it.get("sk", ""))
            if "#audit#" in sk or "#finding#" in sk:
                continue
            total += _f(it.get("cost_usd"))
            n += 1
    except Exception as e:
        print(f"[finops] project_to_date failed: {e}")
    return round(total, 2), n


def gate_decision(worst_case_usd, project=None):
    """The dual threshold, delegated to cost_model.approval_decision.

    Deliberately not re-implemented here. The gate arithmetic is already tested in
    tests/test_cost_model.py, and a second copy in the console is a copy that can drift
    — the drifting one being the one that actually guards the launch button.
    """
    cm = _cost_model()
    ptd, _ = project_to_date_usd(project)
    if cm is None:
        # No estimator, so we cannot price the run. In advisory mode that does not
        # block it -- but it must not be reported as "under budget" either, because we
        # did not check. It is an UNKNOWN, which is a third answer, and saying so is
        # the whole reason this branch exists.
        blocking = BUDGET_MODE == "blocking"
        return {"approval_required": blocking, "project_to_date_usd": ptd,
                "gating_usd": _f(worst_case_usd), "budget_mode": BUDGET_MODE,
                "status": "pending_approval" if blocking else "approved",
                "budget_unknown": True, "over_budget": [],
                "reasons": (["cost model unavailable — cannot verify the spend limit, "
                             "so approval is required"] if blocking else []),
                "notes": ["cost model unavailable — this run's cost was NOT checked "
                          "against the budget"]}
    return cm.approval_decision({"worst_case_usd": _f(worst_case_usd)},
                                project_to_date_usd=ptd,
                                single_run_limit_usd=APPROVAL_LIMIT_USD,
                                cumulative_limit_usd=CUMULATIVE_LIMIT_USD,
                                budget_mode=BUDGET_MODE)


def create_estimate(body, username, now_iso):
    """Price a draft plan and record it, with its gate verdict already computed."""
    cm = _cost_model()
    if cm is None:
        return {"error": "estimator unavailable: cost_model.py not in this bundle",
                "status_code": 503}
    # Only keys estimate_run actually reads are forwarded, so a typo in the form does
    # not become a silently-ignored field that the operator believes was priced.
    INT_KEYS = ("sample_count", "task_count", "max_iterations", "n_stages")
    FLOAT_KEYS = ("endpoint_hours", "train_rows", "minutes_per_stage")
    STR_KEYS = ("training_instance", "inference_instance", "teacher_model",
                "harness_model")
    BOOL_KEYS = ("keep_reasoning", "teardown")
    plan = {}
    for k in INT_KEYS:
        if body.get(k) not in (None, ""):
            try:
                plan[k] = int(body[k])
            except Exception:
                return {"error": f"{k} must be an integer", "status_code": 400}
    for k in FLOAT_KEYS:
        if body.get(k) not in (None, ""):
            try:
                plan[k] = float(body[k])
            except Exception:
                return {"error": f"{k} must be a number", "status_code": 400}
    for k in STR_KEYS:
        if body.get(k):
            plan[k] = str(body[k])[:120]
    for k in BOOL_KEYS:
        if k in body:
            plan[k] = bool(body[k])
    if not (plan.get("sample_count") or plan.get("train_rows")):
        return {"error": "sample_count (or train_rows) is required — with neither, the "
                         "training line is $0 and the total is not an estimate",
                "status_code": 400}

    card = _rate_card()
    if card is None:
        # Refuse rather than return a $0-with-warnings total. A number on a screen gets
        # quoted; an explicit refusal does not.
        return {"error": "no rate card available — run pricing_refresh before "
                         "estimating; without rates every line would price at $0",
                "status_code": 503}
    try:
        est = cm.estimate_run(plan, card)
    except Exception as e:
        return {"error": f"estimate failed: {str(e)[:300]}", "status_code": 400}

    worst = _f(est.get("worst_case_usd"))
    gate = gate_decision(worst)
    eid = "est-" + secrets.token_hex(8)
    item = {
        "id": eid, "project": PROJECT, "created_at": now_iso,
        "requested_by": username or "anonymous",
        "plan": json.dumps(plan, default=str),
        # Numbers stored as strings, not Decimal: DynamoDB rejects float, and the whole
        # doc is round-tripped through json anyway.
        "estimate": json.dumps(est, default=str)[:380000],
        "total_usd": str(round(_f(est.get("total_usd")), 4)),
        "worst_case_usd": str(round(worst, 4)),
        "confidence": str(est.get("confidence", "unknown")),
        "n_unpriced": len(est.get("unpriced", []) or []),
        # draft = priced but not submitted. Submitting is a separate, explicit act, so a
        # user exploring numbers never accidentally files an approval request.
        "status": "draft",
        "gate": json.dumps(gate, default=str),
        # The rates live at estimate time, so a variance months later can be re-derived
        # against what was known then. Storing only "latest" makes old misses
        # unexplainable — the estimate looks wrong when the rate card simply moved.
        "rate_card_as_of": str(rate_card_health(plan).get("oldest_as_of") or ""),
    }
    if estimates_tbl:
        estimates_tbl.put_item(Item=item)
    return {"ok": True, "estimate_id": eid, "estimate": est, "gate": gate,
            "rate_card": rate_card_health(plan), "plan": plan}


def _get_estimate(estimate_id):
    if not (estimates_tbl and estimate_id):
        return None
    try:
        return estimates_tbl.get_item(Key={"id": str(estimate_id)}).get("Item")
    except Exception as e:
        print(f"[finops] get estimate failed: {e}")
        return None


def request_approval(body, username, now_iso):
    """Move a draft to pending_approval. Only a draft may be submitted."""
    eid = str(body.get("estimate_id", ""))
    item = _get_estimate(eid)
    if not item:
        return {"error": "unknown estimate_id", "status_code": 404}
    if item.get("status") != "draft":
        # Re-submitting a decided estimate would let a rejection be quietly retried
        # until someone approves it — the same estimate, a different approver, no record
        # that it was already refused.
        return {"error": f"estimate is {item.get('status')}, not draft",
                "status_code": 409}
    estimates_tbl.update_item(
        Key={"id": eid},
        UpdateExpression=("SET #s = :s, requested_by = :u, requested_at = :t, "
                          "justification = :j"),
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":s": "pending_approval", ":u": username or "anonymous", ":t": now_iso,
            ":j": str(body.get("justification", ""))[:1000]})
    return {"ok": True, "estimate_id": eid, "status": "pending_approval"}


def decide_approval(body, user, now_iso):
    """Approve or reject. Enforces separation of duties server-side.

    Three checks, each of which has to be here rather than in the UI:
      1. approver-group membership — a hidden button is not an access control;
      2. approved_by != requested_by — self-approval defeats the whole gate;
      3. status must still be pending_approval — otherwise a decision can be replayed
         over an earlier one.
    """
    username = (user or {}).get("username", "")
    groups = (user or {}).get("groups", []) or []
    eid = str(body.get("estimate_id", ""))
    decision = str(body.get("decision", "")).lower()
    if decision not in ("approve", "reject"):
        return {"error": "decision must be 'approve' or 'reject'", "status_code": 400}
    item = _get_estimate(eid)
    if not item:
        return {"error": "unknown estimate_id", "status_code": 404}

    cm = _cost_model()
    if cm is None:
        # No estimator means the separation-of-duties rules cannot be evaluated. Deny;
        # an approval granted because the checker was missing is worse than no approval.
        return {"error": "cost model unavailable — cannot validate the approval",
                "status_code": 503}
    verdict = cm.check_approval(item, username, groups,
                               required_group=APPROVER_GROUP)
    if not verdict.get("allowed"):
        return {"error": verdict.get("error", "not allowed"),
                "status_code": int(verdict.get("code", 403))}
    reason = str(body.get("reason", ""))[:1000]
    if decision == "reject" and not reason:
        # A rejection with no reason cannot be acted on: the requester learns only that
        # someone said no, and re-submits the same estimate.
        return {"error": "a rejection needs a reason", "status_code": 400}

    new_status = "approved" if decision == "approve" else "rejected"
    estimates_tbl.update_item(
        Key={"id": eid},
        UpdateExpression=("SET #s = :s, approved_by = :u, decided_at = :t, "
                          "decision_reason = :r"),
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": new_status, ":u": username, ":t": now_iso,
                                   ":r": reason})
    out = {"ok": True, "estimate_id": eid, "status": new_status,
           "approved_by": username}

    task_id = str(item.get("task_id") or "")
    if task_id:
        # A Tasks-tab plan behind this estimate. The dispatch must go through the
        # orchestrator's launch_run (supreme directive: through the pipeline), not
        # start_run — and the approver's decision is itself a signed audit record.
        task = _task_get(task_id)
        if task:
            prev = (task.get("approvals") or [None])[-1]
            rec2 = {
                "task_id": task_id, "plan_uri": str(task.get("plan_uri", "")),
                "plan_sha256": str((prev or {}).get("plan_sha256", "")),
                "cost_estimate_usd": str(task.get("cost_estimate_usd", "")),
                "gate": {"approval_required": True,
                         # str(): DynamoDB rejects floats, and the signature
                         # canonicalizes with default=str anyway
                         "single_run_limit_usd": str(APPROVAL_LIMIT_USD),
                         "cumulative_limit_usd": str(CUMULATIVE_LIMIT_USD)},
                "decision": "accepted" if decision == "approve" else "rejected",
                "approved_by": username, "cognito_sub": str((user or {}).get("sub", "")),
                "source_ip": str((user or {}).get("source_ip", "")),
                "approved_at": now_iso,
                "prev_event_sha256": conductor_tools.chain_hash(prev),
            }
            signed2 = conductor_tools.sign_record(kms, rec2, APPROVAL_KEY)
            s3.put_object(Bucket=data_bucket(),
                          Key=f"tasks/{task_id}/approval-{decision}.json",
                          Body=json.dumps(signed2, indent=2).encode(),
                          ContentType="application/json")
            if decision == "approve":
                accept_msg = {"role": "system",
                              "text": f"PLAN ACCEPTED by {username} (record "
                                      f"{signed2['record_sha256'][:12]}) at {now_iso}. "
                                      "Budget approval granted. Dispatch the run now: call "
                                      "launch_run exactly once with the plan_uri you wrote.",
                              "at": now_iso, "by": "system"}
                _append_messages(task_id, [accept_msg],
                                 "#s = :s, approvals = list_append(if_not_exists(approvals, :e), :a)",
                                 {"#s": "status"}, {":s": "accepting", ":a": [signed2], ":e": []})
                _task_event(task_id, "BudgetApproved", username, {"estimate_id": eid})
                _enqueue_task_turn(task_id, accept=True)
                out["task"] = {"id": task_id, "status": "accepting"}
            else:
                # rejection is feedback, not a tombstone: feed the reason back to the
                # orchestrator so it can revise the plan
                fb = {"role": "user",
                      "text": f"[budget approval REJECTED by {username}] {reason}",
                      "at": now_iso, "by": username}
                _append_messages(task_id, [fb],
                                 "#s = :s, approvals = list_append(if_not_exists(approvals, :e), :a)",
                                 {"#s": "status"}, {":s": "thinking", ":a": [signed2], ":e": []})
                _task_event(task_id, "BudgetRejected", username, {"reason": reason[:200]})
                _enqueue_task_turn(task_id)
                out["task"] = {"id": task_id, "status": "thinking"}
        return out

    if decision == "approve" and body.get("launch"):
        launch = start_run({"estimate_id": eid, "note": f"approved by {username}"})
        # The nested status_code is dropped deliberately: the approval itself succeeded
        # and is recorded, so this response is a 200 carrying a launch error, not a
        # failed approval. Returning 4xx here would suggest the approval did not land.
        launch.pop("status_code", None)
        out["launch"] = launch
    return out


def cost_estimates(limit=50):
    """Estimates newest-first, plus the pending-approval queue."""
    items = []
    if estimates_tbl:
        try:
            r = estimates_tbl.query(
                IndexName="project-created_at-index",
                KeyConditionExpression=Key("project").eq(PROJECT),
                ScanIndexForward=False, Limit=int(limit))
            items = r.get("Items", [])
        except Exception as e:
            # A brand-new deployment has the table but an unbackfilled GSI; scanning is
            # correct here rather than showing an empty queue, which would read as
            # "nothing awaiting approval".
            print(f"[finops] GSI query failed, falling back to scan: {e}")
            try:
                items = estimates_tbl.scan(Limit=int(limit)).get("Items", [])
            except Exception as e2:
                print(f"[finops] estimates scan failed: {e2}")
    out = []
    for it in items:
        try:
            est = json.loads(it.get("estimate", "{}"))
        except Exception:
            est = {}
        try:
            gate = json.loads(it.get("gate", "{}"))
        except Exception:
            gate = {}
        out.append({
            "id": it.get("id"), "status": it.get("status"),
            "created_at": it.get("created_at"),
            "requested_by": it.get("requested_by"), "approved_by": it.get("approved_by"),
            "decided_at": it.get("decided_at"),
            "decision_reason": it.get("decision_reason"),
            "justification": it.get("justification"),
            "total_usd": _f(it.get("total_usd")),
            "worst_case_usd": _f(it.get("worst_case_usd")),
            "confidence": it.get("confidence"),
            "n_unpriced": int(_f(it.get("n_unpriced"))),
            "rate_card_as_of": it.get("rate_card_as_of"),
            "run_id": it.get("run_id"), "sfn_execution_arn": it.get("sfn_execution_arn"),
            "line_items": est.get("line_items", []),
            "subtotals": est.get("subtotals", {}),
            "unpriced": est.get("unpriced", []),
            "assumptions": est.get("assumptions", []),
            "gate": gate,
        })
    out.sort(key=lambda x: str(x.get("created_at") or ""), reverse=True)
    return {"estimates": out,
            "pending": [e for e in out if e["status"] == "pending_approval"],
            # budget_mode travels WITH the limits, because a limit without its mode is
            # the more misleading of the two halves. Two numbers labelled "limit" read as
            # enforced; in advisory -- the deployed default -- an over-budget run is named,
            # priced, and then launched anyway. A reader who trusts the number to stop
            # something is wrong about the only thing they wanted to know.
            "limits": {"single_usd": APPROVAL_LIMIT_USD,
                       "cumulative_usd": CUMULATIVE_LIMIT_USD,
                       "approver_group": APPROVER_GROUP,
                       "budget_mode": BUDGET_MODE,
                       "enforced": BUDGET_MODE == "blocking"}}


def cost_overview(period_days=30):
    """Project rollup: actual by service and category, with settlement state.

    Two numbers no dashboard should merge: settled and provisional. Cost Explorer lags
    ~24 h, so the trailing days will move. Showing one blended total invites quoting a
    figure that has not landed.
    """
    rows, audit = [], []
    if actuals_tbl:
        try:
            r = actuals_tbl.query(
                KeyConditionExpression=Key("project").eq(PROJECT),
                ScanIndexForward=False, Limit=2000)
            for it in r.get("Items", []):
                sk = str(it.get("sk", ""))
                if "#audit#" in sk or "#finding#" in sk:
                    audit.append({"sk": sk, "task": it.get("task"),
                                  "tool": it.get("tool"), "status": it.get("status")})
                    continue
                parts = sk.split("#")
                rows.append({
                    "period": parts[0] if parts else "",
                    "run_id": parts[1] if len(parts) > 1 else "",
                    "category": parts[2] if len(parts) > 2 else it.get("category", ""),
                    "service": it.get("service", ""),
                    "resource_id": it.get("resource_id", ""),
                    "cost_usd": _f(it.get("cost_usd")),
                    "quantity": _f(it.get("quantity")),
                    "unit": it.get("unit", ""),
                    "settlement": it.get("settlement", "provisional"),
                    "rate_source": it.get("rate_source", ""),
                })
        except Exception as e:
            print(f"[finops] actuals query failed: {e}")

    by_cat, by_svc, by_run, by_period = {}, {}, {}, {}
    settled = provisional = 0.0
    for r in rows:
        c = r["cost_usd"]
        by_cat[r["category"] or "unknown"] = round(
            by_cat.get(r["category"] or "unknown", 0.0) + c, 4)
        by_svc[r["service"] or "unknown"] = round(
            by_svc.get(r["service"] or "unknown", 0.0) + c, 4)
        if r["run_id"]:
            by_run[r["run_id"]] = round(by_run.get(r["run_id"], 0.0) + c, 4)
        by_period[r["period"]] = round(by_period.get(r["period"], 0.0) + c, 4)
        if r["settlement"] == "settled":
            settled += c
        else:
            provisional += c

    budgets = []
    try:
        b = boto3.client("budgets", region_name=REGION)
        for bd in b.describe_budgets(AccountId=ACCOUNT_ID, MaxResults=20).get("Budgets", []):
            budgets.append({
                "name": bd.get("BudgetName"),
                "limit_usd": _f((bd.get("BudgetLimit") or {}).get("Amount")),
                "actual_usd": _f(((bd.get("CalculatedSpend") or {}).get("ActualSpend")
                                  or {}).get("Amount")),
                "time_unit": bd.get("TimeUnit"),
            })
    except Exception as e:
        # An account guardrail we cannot read is worth saying so about, not hiding.
        print(f"[finops] budgets unavailable: {e}")

    return {
        "project": PROJECT,
        "total_usd": round(settled + provisional, 2),
        "settled_usd": round(settled, 2),
        "provisional_usd": round(provisional, 2),
        "n_line_items": len(rows),
        "by_category": by_cat, "by_service": by_svc,
        "by_run": dict(sorted(by_run.items(), key=lambda kv: -kv[1])[:25]),
        "by_period": dict(sorted(by_period.items(), reverse=True)[:int(period_days)]),
        "line_items": rows[:400],
        "audit_rows": audit[:50],
        "budgets": budgets,
        # The SECOND limits payload, and the reason the first fix was incomplete.
        # `cost_estimates` and `cost_overview` both publish a dict named `limits`, and
        # only one of them was given the mode. The live read-back after deploying that
        # fix is what found it: /api/cost-estimates answered with budget_mode, and
        # /api/cost-overview -- the endpoint whose name says overview -- still answered
        # with two bare numbers. A second surface carrying the same key with less in it
        # is worse than none, because a caller who found the mode once will not check
        # again. Derived identically, from the same predicate the gate uses.
        "limits": {"single_usd": APPROVAL_LIMIT_USD,
                   "cumulative_usd": CUMULATIVE_LIMIT_USD,
                   "budget_mode": BUDGET_MODE,
                   "enforced": BUDGET_MODE == "blocking"},
        "note": ("Attribution is by explicit resource match (training-job/llmops-*, "
                 "manifest endpoints, spans by session id) — never by service, because "
                 "this account also carries unrelated SageMaker spend. Cost Explorer "
                 "lags ~24h, so provisional figures will still move."),
    }


def rate_card_health(plan=None):
    """What the rate card knows and what it admits it does not.

    This panel exists because the Price List API cannot price Fable 5 or Opus 5 on this
    account (verified 2026-07-31: every `provider=Anthropic` entry for us-east-1 is
    Claude 3 or older) — the models the harness fleet itself runs on. Without the panel,
    a stale feed prices the agent fleet at $0 and the estimate is quietly, plausibly
    wrong.

    Health is measured against the SKUs a real plan NEEDS, not against the card's own
    contents — a card with 40 irrelevant rates and no teacher price is not healthy, and
    only `required_skus_for` can tell the difference.
    """
    cm = _cost_model()
    card = _rate_card()
    if cm is None:
        return {"present": False, "warning": "cost model unavailable in this bundle"}
    if card is None:
        return {"present": False, "healthy": False,
                "warning": "no rate card in S3 — run pricing_refresh; until then every "
                           "SKU is unpriced and estimates are not usable"}
    out = cm.rate_card_health(card, cm.required_skus_for(plan or {}))
    out["present"] = True
    return out


def rate_card_for_prompt():
    """The rate card as a compact dict to hand the orchestrator IN its invocation.

    The consult prompt used to say "read s3://<bucket>/finops/rates/rate_card_latest.json
    FIRST". That cost a whole model round-trip -- the agent had to answer with a tool call,
    wait, then be re-invoked with the result -- and X-Ray showed the round-trip is where
    the latency lives: on one measured 60.6s turn, 8.4s went to the call that only decided
    to run a shell, and 44.8s to the call that finally answered. Worse, the agent did not
    reliably obey: traces 1-6a6d85d0-... and 1-6a6d85c5-... show it fetching litellm's
    model_prices_and_context_window.json from raw.githubusercontent.com instead, so it was
    paying for a round-trip AND quoting the customer prices from a third party's file.

    Handing it the rates removes both failures at once. The card is read here, at invoke
    time, from the same S3 object _rate_card() uses -- so this is not a snapshot baked into
    a prompt that goes stale the next time pricing_refresh runs.

    Only unit_price/unit/source per SKU: the full document is 10463 bytes of provenance
    (realized_from bases, usage types, CE windows) the agent does not quote, and every
    injected byte is billed on every turn. 16 SKUs compact to ~1.8 KB.

    Returns None when the card is unreadable -- the caller must then say it cannot quote
    prices, exactly as the prompt already required. An empty dict here would read as "no
    rates exist" and invite an invented number.

    Reads the DOCUMENT, not a RateCard: the constructor keeps only the SKU table, so
    generated_at / rate_precedence do not survive it, and there is no `.doc` attribute to
    reach back through. getattr(card, "doc", None) returns None silently -- which would
    ship a console that tells every customer it cannot quote prices.
    """
    doc = _rate_card_doc() or {}
    rates = doc.get("rates") or {}
    if not rates:
        return None
    return {
        "generated_at": doc.get("generated_at", ""),
        "rate_precedence": doc.get("rate_precedence", []),
        "rates": {k: {"unit_price": v.get("unit_price"), "unit": v.get("unit"),
                      "source": v.get("source")}
                  for k, v in rates.items() if isinstance(v, dict)},
    }


def cost_variance(estimates=None, overview=None):
    """Estimate vs actual per run, naming the driving category rather than one aggregate %.

    A single "we were 40% off" tells nobody what to fix; the next estimate is only better
    if the miss is attributed to a line — so this delegates to cost_model.reconcile,
    which is where that attribution lives and is tested.

    Both inputs are injectable so the caller can reuse reads it already made; recomputing
    them would double the DynamoDB queries behind one page render.
    """
    cm = _cost_model()
    ests = (estimates if estimates is not None
            else cost_estimates(limit=100)["estimates"])
    ov = overview if overview is not None else cost_overview()

    # Per-run, per-category actuals, assembled from the same rows the rollup used.
    by_run_cat = {}
    settle = {}
    for r in ov.get("line_items", []):
        rid = r.get("run_id")
        if not rid:
            continue
        cat = by_run_cat.setdefault(rid, {})
        cat[r.get("category") or "unknown"] = round(
            cat.get(r.get("category") or "unknown", 0.0) + _f(r.get("cost_usd")), 6)
        # A run is only settled if EVERY row for it is: one provisional row means the
        # total can still move, and calling that settled is the error this guards.
        if r.get("settlement") != "settled":
            settle[rid] = "provisional"
        else:
            settle.setdefault(rid, "settled")

    out = []
    for e in ests:
        rid = e.get("run_id")
        if not rid or rid not in by_run_cat:
            continue
        actual = {"subtotals": by_run_cat[rid],
                  "total_usd": round(sum(by_run_cat[rid].values()), 6),
                  "settlement": settle.get(rid, "provisional")}
        est_doc = {"subtotals": e.get("subtotals") or {},
                   "total_usd": e.get("total_usd", 0.0)}
        rec = (cm.reconcile(est_doc, actual) if cm else
               {"estimate_usd": e.get("total_usd"), "actual_usd": actual["total_usd"],
                "verdict": "cost model unavailable — cannot attribute the variance"})
        rec.update({"run_id": rid, "estimate_id": e.get("id"),
                    "confidence": e.get("confidence")})
        out.append(rec)

    matched = {o["run_id"] for o in out}
    unestimated = [r for r in by_run_cat if r not in matched]
    return {"variance": out,
            # Stated explicitly: a variance report silent about unestimated spend
            # implies full coverage it does not have. Runs launched without an estimate
            # are legal — that is how every run worked before this feature.
            "unestimated_runs": sorted(unestimated)[:50],
            "n_unestimated": len(unestimated)}


def finops_run(body):
    """Trigger reconcile / pricing_refresh / report on demand (the schedule is daily)."""
    task = str(body.get("task", "reconcile"))
    if task not in ("reconcile", "pricing_refresh", "report"):
        return {"error": f"unknown task {task}", "status_code": 400}
    payload = {"task": task, "project": PROJECT, "sync": False}
    if body.get("period"):
        payload["period"] = str(body["period"])
    try:
        r = lam.invoke(FunctionName=FINOPS_FN, InvocationType="Event",
                       Payload=json.dumps(payload).encode())
        # Named invoke_status, NOT status_code: _resp_result treats status_code as the
        # HTTP status to return, and Lambda's async 202 would silently become this
        # route's response code.
        return {"ok": True, "task": task, "invoke_status": r.get("StatusCode"),
                "note": "async — watch the rollup; Cost Explorer lags ~24 h"}
    except Exception as e:
        return {"error": f"could not invoke {FINOPS_FN}: {str(e)[:300]}",
                "status_code": 502}


# ── Tasks: goal-driven entry — human ⇄ orchestrator consultation ──────────────
# A task is a consultation with the llmops_orchestrator (pre-sales solution
# architect): the human states a goal, the agent guides requirements (data first),
# proposes a signed-off-able plan, and dispatches through the pipeline only after
# a human's KMS-signed acceptance. The DDB record is the source of truth for the
# conversation; the AgentCore session is a cache of it that may die (900s idle).

TASK_ACTIVE = ("thinking", "accepting")           # one in-flight turn per task
TASK_TERMINAL = ("dispatched", "closed", "error")
#: The state machine closes a task row out with "completed"/"failed" (see
#: orchestration/state_machine.asl.json), which TASK_TERMINAL predates and does not
#: list. Kept as a SEPARATE tuple rather than folded into TASK_TERMINAL: widening that
#: one would also change what post_task_message and close_task refuse, which is a
#: lifecycle decision this change has no business making quietly. New checks that mean
#: "this consultation is over" should test both.
TASK_SETTLED = ("completed", "failed")
STALE_TURN_MIN = 20                               # zombie 'thinking' escape hatch


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _task_get(task_id):
    if not tasks_tbl:
        return None
    return tasks_tbl.get_item(Key={"id": task_id}).get("Item")


def _task_event(task_id, event_name, actor, detail=None):
    """Audit + flow-diagram feed, one write: the same stage-events table the
    pipeline uses, keyed by the task id, drives both the lifecycle SVG and the
    'who did what when' question."""
    if not events_tbl:
        return
    try:
        events_tbl.put_item(Item={
            "run_id": task_id,
            "sk": f"{_now_iso()}#task#{event_name}",
            "stage": "task", "event_name": event_name,
            "detail": json.dumps({"actor": actor, **(detail or {})}, default=str)[:2000]})
    except Exception:
        pass  # an audit-log write must never take the user action down with it


#: The whole conversation lives in ONE DynamoDB item under a 400 KB ceiling, so a long
#: message is capped in the record the UI renders. The S3 copy is the one that must stay
#: whole: it is the audit artifact, and an "audit copy" holding a truncated subset of the
#: thing it audits answers no question worth asking. Live, one message sat at exactly
#: 8000 characters in BOTH copies -- the cap was applied by the callers, before the split.
MSG_TEXT_MAX = 8000
#: A declared ceiling for the audit copy too, so one request cannot write an unbounded
#: object. Generous by two orders of magnitude versus the DynamoDB cap: the point is that
#: a real reply is never truncated, not that there is no limit at all.
TRANSCRIPT_TEXT_MAX = 100_000


def _transcript_append(task_id, entries):
    """Append the FULL text of each message to the S3 audit log.

    Genuinely append-only: ONE OBJECT PER CALL, never a read-modify-write of a single
    transcript.jsonl. The old shape re-read the whole file, concatenated, and put it back,
    which cost two things:

    1. The read's `except Exception` treated *every* failure as "no file yet", so the put
       then REPLACED the entire history with just the newest lines. A transient 503 or a
       throttle silently erased the audit log whose whole job is to survive.
    2. Two writers for one task are reachable -- close_task is permitted while a turn is
       in flight, because "thinking" is not in TASK_TERMINAL -- and under read-modify-write
       the loser's messages vanish from the copy while DynamoDB's list_append keeps both.
       An audit copy missing a message the audited record has is worse than no copy.

    Keys sort lexicographically into chronological order, so `aws s3 cp --recursive` plus
    a sort reassembles the thread. Callers pass UNTRUNCATED messages; only the DynamoDB
    copy is capped (MSG_TEXT_MAX), which is the split this docstring used to claim and
    the code did not implement. The 26 pre-existing `transcript.jsonl` objects stay where
    they are -- they are history, and rewriting them would be the very thing this fix
    stops.
    """
    b = data_bucket()
    key = f"tasks/{task_id}/transcript/{_now_iso()}-{secrets.token_hex(4)}.jsonl"
    lines = b"".join(
        json.dumps({**e, "text": str(e.get("text", ""))[:TRANSCRIPT_TEXT_MAX]},
                   default=str).encode() + b"\n"
        for e in entries)
    s3.put_object(Bucket=b, Key=key, Body=lines, ContentType="application/x-ndjson")


def _safe_transcript_append(task_id, entries):
    """The audit copy is a SECOND channel, so it must never gate the first.

    It used to be called unwrapped at the end of _append_messages, so one S3 failure
    propagated out and skipped everything after that call in the caller. For accept_task
    that is `_task_event(PlanAccepted)` and `_enqueue_task_turn(accept=True)`: a
    KMS-signed acceptance would sit at "accepting" with no worker ever launched, and the
    only escape is the 20-minute STALE_TURN_MIN hatch. Same shape as the SNS publish that
    was gating the other three escalation channels -- write to each channel separately,
    and never let the one with no reader take down the one the customer is waiting on.
    """
    try:
        _transcript_append(task_id, entries)
    except Exception as e:
        print(f"[task-chat] transcript append failed for {task_id} (ignored): {e}")


def _task_session(task):
    return session_id(f"task-{task['id']}", "consult", f"s{int(task.get('session_seq', 0))}")


def _user_may_task(user):
    groups = (user or {}).get("groups", []) or []
    return DS_GROUP in groups or APPROVER_GROUP in groups


#: Path prefix of the consult plane: one customer engagement per thread.
CONSULT_PREFIX = "/api/tasks"


def _is_consult_path(path):
    """True for every route that reads or writes a customer engagement.

    A PREFIX, not a list of paths, and that is the whole fix rather than a tidier
    spelling of it. Four consult READS -- /api/tasks, /api/tasks/{id} and that thread's
    approval and readiness panels -- were served anonymously on a public API Gateway URL
    for the platform's whole life, because the auth chokepoint was keyed on the HTTP
    METHOD: `if method == "POST": user = _authed_user(...)`. So the design property the
    docs boast, "adding a route cannot accidentally add an unauthenticated write", was
    exactly true and exactly insufficient -- it says nothing about adding an
    unauthenticated READ of the customer plane, which is how all four got there. What
    leaked was not operational fact: GET /api/tasks/{id} returns the whole DynamoDB item,
    which is the customer's transcript, and /approval returns approved_by, cognito_sub
    and source_ip -- the identity fields the KMS signature exists to bind.

    Enumerating the four would have fixed today's leak and left the mechanism that
    produced it intact, so the fifth panel added to a thread would arrive anonymous the
    same way. A prefix cannot be outgrown by a route nobody remembered to add to a list.

    The read plane above it stays public deliberately: /api/overview and friends are
    already-reconciled operational fact, and gating them would add friction fifty times
    a day while protecting what is already in the architecture diagrams. Authority is a
    different question from visibility -- but a customer's conversation is neither.
    """
    return path == CONSULT_PREFIX or path.startswith(CONSULT_PREFIX + "/")


def _append_messages(task_id, msgs, extra_update="", extra_names=None, extra_values=None,
                     drop_partial=False):
    """Append to the messages list atomically-enough (single writer per task is
    enforced by the thinking-status lock at the route layer).

    drop_partial removes the streamed draft in the SAME write that commits the real
    message. Two writes would leave a window in which a poll (every 3s during a turn)
    sees both, and the customer watches the reply appear twice.

    Callers pass FULL text. The DynamoDB copy is capped here (one item, 400 KB, and it is
    what the UI renders); the S3 audit copy gets the message whole. Capping at the call
    sites put the same truncated string in both, which made the audit copy audit nothing.
    """
    trimmed = [{**m, "text": str(m.get("text", ""))[:MSG_TEXT_MAX]} for m in msgs]
    names = {"#m": "messages"}
    values = {":new": trimmed, ":empty": [], ":t": _now_iso()}
    expr = "SET #m = list_append(if_not_exists(#m, :empty), :new), updated_at = :t"
    if extra_update:
        expr += ", " + extra_update
        names.update(extra_names or {})
        values.update(extra_values or {})
    if drop_partial:
        expr += " REMOVE partial_reply"
    tasks_tbl.update_item(Key={"id": task_id}, UpdateExpression=expr,
                          ExpressionAttributeNames=names,
                          ExpressionAttributeValues=values)
    # Second channel, wrapped, and AFTER the write it must not gate. msgs, not trimmed:
    # the audit copy is the one that keeps the whole message.
    _safe_transcript_append(task_id, msgs)


def create_task(body, user):
    if not _user_may_task(user):
        return {"error": f"membership in {DS_GROUP} or {APPROVER_GROUP} required",
                "status_code": 403}
    goal = str(body.get("goal", "")).strip()
    if not goal:
        return {"error": "goal is required", "status_code": 400}
    task_id = "task-" + secrets.token_hex(8)
    now = _now_iso()
    first = {"role": "user", "text": goal, "at": now, "by": user["username"]}
    item = {"id": task_id, "status": "thinking", "created_by": user["username"],
            "created_at": now, "updated_at": now, "goal": goal[:500],
            "messages": [{**first, "text": goal[:MSG_TEXT_MAX]}], "plan_uri": "",
            "plan_summary": "", "cost_estimate_usd": "", "run_id": "", "session_seq": 0,
            "error_msg": ""}
    tasks_tbl.put_item(Item=item)
    # Wrapped, and after the put_item: an audit write that fails must not cost the
    # customer the task they just created (nor the _enqueue_task_turn below it).
    _safe_transcript_append(task_id, [first])
    _task_event(task_id, "TaskCreated", user["username"], {"goal": goal[:200]})
    _enqueue_task_turn(task_id)
    return {"ok": True, "task": item}


#: Where a customer's own data lands. Read-only to the pipeline (see
#: deploy/iam/harness_execution_role.json: a pipeline that can rewrite customer data can
#: destroy the held-out set its own gates are judged on), writable only by this console
#: signing a short-lived presigned PUT.
CUSTOMER_DATA_PREFIX = "customer-data"
#: 15 min: long enough to upload a large file over a slow link, short enough that a URL
#: leaked from a browser history or a proxy log is not a standing write grant.
UPLOAD_URL_TTL_S = 900
#: 5 GiB is S3's single-PUT ceiling. Declared rather than implied so the caller gets a
#: 400 with a number instead of an opaque S3 failure at the end of a long upload.
UPLOAD_MAX_BYTES = 5 * 1024 * 1024 * 1024
#: A dataset the pipeline can actually read. Everything else is refused by extension
#: rather than sniffed: this console never opens the bytes, so the extension is the only
#: honest signal, and .html/.svg in a bucket that also serves content is a stored-XSS
#: shape we simply do not accept.
UPLOAD_EXTS = ("jsonl", "json", "csv", "tsv", "txt", "parquet", "zip", "gz")
#: Only for the Content-Type pinned INTO the presigned URL. A dataset is never served as
#: a document, so anything unrecognised becomes a stream rather than guessing.
_UPLOAD_CTYPES = {"jsonl": "application/x-ndjson", "json": "application/json",
                  "csv": "text/csv", "tsv": "text/tab-separated-values",
                  "txt": "text/plain", "parquet": "application/vnd.apache.parquet",
                  "zip": "application/zip", "gz": "application/gzip"}


def _safe_upload_name(filename):
    """A filename reduced to something that cannot escape its prefix.

    The key is built server-side from this, never taken from the client. `basename`
    alone is not enough: "..%2f" style input, backslashes (a Windows client sends
    "C:\\data\\set.jsonl"), and leading dots all survive it. So: split on both
    separators, keep the last segment, then keep only [A-Za-z0-9._-] and collapse the
    rest. Returns "" when nothing usable remains, which the caller turns into a 400 --
    silently inventing a name would store a file the customer cannot recognise later.
    """
    raw = str(filename or "").strip().replace("\\", "/")
    raw = raw.split("/")[-1]
    cleaned = "".join(ch if (ch.isalnum() or ch in "._-") else "-" for ch in raw)
    # Collapse dot runs. Percent-encoded traversal ("..%2f..%2fruns%2fx.json") survives
    # the character filter as "..-2f..-2fruns-2fx.json" -- harmless as an S3 key, since
    # S3 does not resolve "..", but it leaves a name that READS like a traversal in every
    # log and audit event that quotes it. Collapsing means no reviewer ever has to decide
    # whether a ".." in our own bucket listing is the dangerous kind.
    while ".." in cleaned:
        cleaned = cleaned.replace("..", ".")
    # No leading dots: ".." collapses to nothing usable, and a dotfile is not a dataset.
    cleaned = cleaned.lstrip(".")[:120]
    if not cleaned or cleaned in (".", ".."):
        return ""
    ext = cleaned.rsplit(".", 1)[-1].lower() if "." in cleaned else ""
    if ext not in UPLOAD_EXTS:
        return ""
    return cleaned


def data_upload_url(body, user):
    """Mint a short-lived presigned PUT so the customer's browser can upload a dataset
    straight to S3.

    Why presigned rather than posting the file here: API Gateway caps a payload at 6 MB
    and this Lambda has a 900s timeout, so routing an enterprise dataset through it
    would fail on size or cost minutes of Lambda time per upload. The bytes go
    browser -> S3; only the signature comes from here.

    Before this route existed the consult prompt opened every consultation by asking
    "where is your data (an S3 URI under customer-data/)" -- a question the product had
    no way to help answer, because the console's IAM could write only tasks/* and the UI
    had no file input. Someone with AWS credentials had to upload out of band.
    """
    if not _user_may_task(user):
        return {"error": f"membership in {DS_GROUP} or {APPROVER_GROUP} required",
                "status_code": 403}
    task_id = str(body.get("task_id", "")).strip()
    task = _task_get(task_id) if task_id else None
    if not task:
        # The upload is scoped to a consultation, so an unknown task is not a 400 about
        # a field -- there is nothing to attach the data to.
        return {"error": "unknown task_id", "status_code": 404}
    status = str(task.get("status", ""))
    if status in TASK_TERMINAL or status in TASK_SETTLED:
        return {"error": f"task is {status}; data can only be added to an open "
                         "consultation", "status_code": 409}
    name = _safe_upload_name(body.get("filename"))
    if not name:
        return {"error": "filename must be a plain name ending in one of: "
                         + ", ".join(UPLOAD_EXTS), "status_code": 400}
    try:
        size = int(body.get("content_length") or 0)
    except (TypeError, ValueError):
        return {"error": "content_length must be an integer", "status_code": 400}
    if size <= 0:
        return {"error": "content_length is required (an empty upload is not data)",
                "status_code": 400}
    if size > UPLOAD_MAX_BYTES:
        return {"error": f"{size} bytes exceeds the {UPLOAD_MAX_BYTES} byte limit for a "
                         "single upload", "status_code": 413}

    bucket = data_bucket()
    # The key is composed here from the task id and the sanitised name. Nothing the
    # client sent reaches it verbatim, so a crafted filename cannot write into runs/,
    # finops/, or another task's prefix.
    key = f"{CUSTOMER_DATA_PREFIX}/{task_id}/{name}"
    ext = name.rsplit(".", 1)[-1].lower()
    ctype = _UPLOAD_CTYPES.get(ext, "application/octet-stream")
    try:
        url = s3.generate_presigned_url(
            "put_object",
            # ContentType is signed IN, so the browser must send this exact value and
            # cannot store a dataset as text/html. ServerSideEncryption matches the
            # bucket default (AES256) rather than relying on it.
            Params={"Bucket": bucket, "Key": key, "ContentType": ctype,
                    "ServerSideEncryption": "AES256"},
            ExpiresIn=UPLOAD_URL_TTL_S)
    except Exception as e:
        return {"error": f"could not sign an upload URL: {str(e)[:200]}",
                "status_code": 502}
    _task_event(task_id, "DataUploadUrlIssued", user["username"],
                {"key": key, "bytes": str(size)})
    return {"ok": True, "url": url, "key": key, "bucket": bucket,
            "uri": f"s3://{bucket}/{key}", "content_type": ctype,
            "expires_in": UPLOAD_URL_TTL_S}


def _enqueue_task_turn(task_id, accept=False):
    if SELF_FUNCTION:
        lam.invoke(FunctionName=SELF_FUNCTION, InvocationType="Event",
                   Payload=json.dumps({"mode": "task-chat", "task_id": task_id,
                                       "accept": accept}).encode())


def list_tasks(limit=25):
    items = []
    if tasks_tbl:
        items = tasks_tbl.scan(Limit=int(limit)).get("Items", [])
    items.sort(key=lambda i: str(i.get("updated_at", "")), reverse=True)
    return {"tasks": [{k: i.get(k, "") for k in
                       ("id", "status", "goal", "created_by", "cost_estimate_usd",
                        "run_id", "updated_at", "plan_summary")} for i in items]}


def get_task(task_id):
    item = _task_get(task_id)
    if not item:
        return {"error": "unknown task", "status_code": 404}
    out = json.loads(json.dumps(item, default=str))
    # event timeline feeds the lifecycle flow diagram. Directives come back on their
    # own key for the same reason as in run_detail: they sort after every event, so
    # left in the list they render as blank rows and push real events out of view.
    try:
        evs, dirs = _timeline(task_id)
        out["events"] = [{"sk": str(e.get("sk", "")),
                          "event_name": str(e.get("event_name", "")),
                          "detail": str(e.get("detail", ""))[:500]} for e in evs]
        out["directives"] = [_directive_view(d) for d in dirs]
    except Exception:
        out["events"] = []
        out["directives"] = []
    return out


def post_task_message(task_id, body, user):
    if not _user_may_task(user):
        return {"error": f"membership in {DS_GROUP} or {APPROVER_GROUP} required",
                "status_code": 403}
    text = str(body.get("text", "")).strip()
    if not text:
        return {"error": "text is required", "status_code": 400}
    task = _task_get(task_id)
    if not task:
        return {"error": "unknown task", "status_code": 404}
    status = str(task.get("status", ""))
    if status in TASK_TERMINAL:
        return {"error": f"task is {status}; open a new task", "status_code": 409}
    if status in TASK_ACTIVE:
        # zombie escape: a worker that crashed leaves 'thinking' behind forever
        updated = str(task.get("updated_at", ""))
        try:
            age_min = (datetime.now(timezone.utc)
                       - datetime.fromisoformat(updated)).total_seconds() / 60
        except Exception:
            age_min = STALE_TURN_MIN + 1
        if age_min < STALE_TURN_MIN:
            return {"error": "a turn is already in flight; wait for the reply",
                    "status_code": 409}
    # Full text: _append_messages caps the DynamoDB copy, the audit copy keeps it whole.
    msg = {"role": "user", "text": text, "at": _now_iso(), "by": user["username"]}
    _append_messages(task_id, [msg], "#s = :s", {"#s": "status"}, {":s": "thinking"})
    _task_event(task_id, "MessageSent", user["username"])
    _enqueue_task_turn(task_id)
    return {"ok": True, "status": "thinking"}


def accept_task(task_id, user):
    """The human signs. Gate decides direct dispatch vs the Cost approval queue.

    The approval record is the accountability artifact: canonical JSON of what was
    approved, hash-chained to the previous audit event, KMS-signed so it can be
    verified by a third party without trusting this system. See conductor_tools.
    """
    task = _task_get(task_id)
    if not task:
        return {"error": "unknown task", "status_code": 404}
    if str(task.get("status")) != "plan_proposed":
        return {"error": f"task is {task.get('status')}, not plan_proposed — nothing "
                         "to accept (no replay over a decided task)", "status_code": 409}
    if not task.get("plan_uri") or task.get("cost_estimate_usd") in ("", None):
        return {"error": "no priced plan on record; ask the orchestrator to propose one",
                "status_code": 409}

    username = user["username"]
    groups = user.get("groups", []) or []
    fresh = gate_decision(task["cost_estimate_usd"])
    needs_approval = bool(fresh.get("approval_required"))

    # Same membership bar either way (datascience or approver may sign). What blocking
    # mode changes is the CONSEQUENCE of signing: within-budget dispatches now;
    # over-budget only submits to the Cost queue, where decide_approval enforces the
    # approver group and requester != approver. In advisory mode (the default) an
    # over-budget plan dispatches too — but the overage is still written into the
    # signed record below, so the audit trail says what this run was expected to cost
    # relative to the budget even though nothing stopped it.
    if not _user_may_task(user):
        return {"error": "not authorized", "status_code": 403}

    # plan_sha256 binds the signature to THIS plan's exact bytes
    b = data_bucket()
    uri = str(task["plan_uri"])
    try:
        plan_raw = s3.get_object(Bucket=b, Key=uri[5:].partition("/")[2])["Body"].read()
    except Exception as e:
        return {"error": f"plan_uri unreadable: {e}", "status_code": 409}
    plan_sha = hashlib.sha256(plan_raw).hexdigest()

    prev = (task.get("approvals") or [{}])[-1] if task.get("approvals") else None
    record = {
        "task_id": task_id, "plan_uri": uri, "plan_sha256": plan_sha,
        "cost_estimate_usd": str(task["cost_estimate_usd"]),
        "gate": {"approval_required": needs_approval,
                 # str(): DynamoDB rejects floats in the stored approval record
                 "single_run_limit_usd": str(APPROVAL_LIMIT_USD),
                 "cumulative_limit_usd": str(CUMULATIVE_LIMIT_USD),
                 # The budget comparison belongs in the SIGNED record, not only in the
                 # runtime response. Advisory mode means the overage did not stop the
                 # dispatch; it does not mean the approval record should read as though
                 # the plan was within budget.
                 "budget_mode": str(fresh.get("budget_mode", BUDGET_MODE)),
                 "over_budget": [str(r) for r in (fresh.get("over_budget") or [])]},
        "decision": "submitted" if needs_approval else "accepted",
        "approved_by": username, "cognito_sub": str(user.get("sub", "")),
        "source_ip": str(user.get("source_ip", "")), "approved_at": _now_iso(),
        "prev_event_sha256": conductor_tools.chain_hash(prev),
    }
    signed = conductor_tools.sign_record(kms, record, APPROVAL_KEY)
    s3.put_object(Bucket=b, Key=f"tasks/{task_id}/approval.json",
                  Body=json.dumps(signed, indent=2).encode(),
                  ContentType="application/json")

    if needs_approval:
        # into the existing Cost-tab queue; decide_approval routes back here on approve
        eid = "est-task-" + secrets.token_hex(5)
        estimates_tbl.put_item(Item={
            "id": eid, "project": PROJECT, "created_at": _now_iso(),
            "status": "pending_approval", "requested_by": task["created_by"],
            "requested_at": _now_iso(), "worst_case_usd": str(task["cost_estimate_usd"]),
            "task_id": task_id,
            "justification": f"Tasks-tab plan {task_id}: {str(task.get('plan_summary'))[:300]}"})
        tasks_tbl.update_item(
            Key={"id": task_id},
            UpdateExpression="SET #s = :s, approvals = list_append(if_not_exists(approvals, :e), :a), "
                             "estimate_id = :eid, updated_at = :t",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": "pending_approval", ":a": [signed],
                                       ":e": [], ":eid": eid, ":t": _now_iso()})
        _task_event(task_id, "ApprovalRequested", username,
                    {"estimate_id": eid, "cost": str(task["cost_estimate_usd"])})
        return {"ok": True, "status": "pending_approval", "estimate_id": eid,
                "note": "over the spend limit — an approver must decide on the Cost tab"}

    # under the limit: signed acceptance dispatches immediately
    accept_msg = {"role": "system",
                  "text": f"PLAN ACCEPTED by {username} (record {signed['record_sha256'][:12]}) "
                          f"at {record['approved_at']}. Dispatch the run now: call launch_run "
                          f"exactly once with the plan_uri you wrote.",
                  "at": _now_iso(), "by": "system"}
    _append_messages(task_id, [accept_msg],
                     "#s = :s, approvals = list_append(if_not_exists(approvals, :e), :a)",
                     {"#s": "status"},
                     {":s": "accepting", ":a": [signed], ":e": []})
    # shadow estimate so finops variance covers ALL conductor runs
    try:
        estimates_tbl.put_item(Item={
            "id": "est-task-" + secrets.token_hex(5), "project": PROJECT,
            "created_at": _now_iso(), "status": "launched",
            "requested_by": task["created_by"],
            "worst_case_usd": str(task["cost_estimate_usd"]), "task_id": task_id,
            "justification": f"shadow estimate (under-limit Tasks acceptance {task_id})"})
    except Exception:
        pass
    _task_event(task_id, "PlanAccepted", username,
                {"record_sha256": signed["record_sha256"], "cost": record["cost_estimate_usd"]})
    _enqueue_task_turn(task_id, accept=True)
    return {"ok": True, "status": "accepting"}


def close_task(task_id, body, user):
    task = _task_get(task_id)
    if not task:
        return {"error": "unknown task", "status_code": 404}
    reason = str(body.get("reason", "")).strip()
    if not reason:
        return {"error": "a close needs a reason", "status_code": 400}
    username = user["username"]
    groups = user.get("groups", []) or []
    if username != str(task.get("created_by")) and APPROVER_GROUP not in groups:
        return {"error": "only the creator or an approver may close", "status_code": 403}
    if str(task.get("status")) in TASK_TERMINAL:
        return {"error": f"task already {task.get('status')}", "status_code": 409}
    msg = {"role": "system", "text": f"Task closed by {username}: {reason[:500]}",
           "at": _now_iso(), "by": username}
    _append_messages(task_id, [msg], "#s = :s, closed_reason = :r",
                     {"#s": "status"}, {":s": "closed", ":r": reason[:500]})
    _task_event(task_id, "TaskClosed", username, {"reason": reason[:200]})
    return {"ok": True, "status": "closed"}


# The consult protocol's step-2 "data" block, as the orchestrator is told to write it.
# Each entry is (dotted path into the plan's data block, label, why the customer should
# care). The WHY is shipped to the browser rather than written into the frontend,
# because these are the same questions the orchestrator asks at step 0 -- if the
# wording drifts between the agent and the panel the customer sees two different
# checklists for one consultation.
#
# This list must cover EVERY key the orchestrator's prompt names in that block, and it
# did not: `datasheet.provenance` and `readiness_report_uri` were specified for the agent
# to write and absent here, so the panel had 7 rows against a 9-key spec. The second
# omission is the expensive one -- readiness_report_uri is the pointer to the Data
# Readiness Report, which is where the audit's PII scan actually lands. A customer could
# therefore read a complete-looking readiness panel, see "PII disposition: redacted" as a
# claim in the plan, and have no link to the one artifact that examined the data. The
# guard for this now derives the key list from harness.json rather than restating it.
DATA_READINESS_FIELDS = (
    ("source_uri", "Where the data is",
     "an S3 URI under customer-data/ — until this exists there is nothing to audit"),
    ("verification_method", "How outputs are verified",
     "tests, exact answers or rules; without one, quality gates fall back to an "
     "LLM judge and are marked low-confidence"),
    ("datasheet.provenance", "Where the data came from",
     "who collected it and how; a license means little without the origin it applies to"),
    ("datasheet.license", "License / provenance",
     "whether the data may legally be used to train, and where it came from"),
    ("datasheet.pii_disposition", "PII disposition",
     "what personal data is present and what happens to it"),
    ("readiness_report_uri", "Data Readiness Report",
     "the audit that actually examined the data, including its PII scan — without this "
     "link the PII answer above is a claim in the plan with nothing behind it"),
    ("datasheet.consent", "Consent to send to the teacher",
     "distillation sends this data to the teacher model; the customer has to know"),
    ("customer_eval_uri", "Held-out acceptance set",
     "the gates are anchored to this; scored against training data they measure "
     "nothing"),
    ("decontamination", "Decontamination",
     "training on the held-out set inflates every score that follows"),
)


def _dig(obj, dotted):
    for part in dotted.split("."):
        if not isinstance(obj, dict):
            return None
        obj = obj.get(part)
    return obj


def task_readiness(task_id):
    """What the plan actually says about the customer's data, and what it does not.

    Answered from plan.json rather than from the chat, because the plan is the artifact
    the customer signs -- a fact stated in conversation and then missing from the plan
    is exactly the gap worth surfacing.

    Unanswered fields are returned EXPLICITLY as answered=False with the reason they
    matter, never as an empty string. A blank row reads as "fine"; the whole point of
    this panel is showing which of the nine data questions nobody has answered yet.

    Nine, not six: this sentence read "six" while DATA_READINESS_FIELDS held nine, ever
    since the commit that grew the list to match the consult prompt. The count in the
    tuple was guarded and the count in the prose was not, so the prose is the copy that
    drifted -- the same shape ARCHITECTURE.md already records for this panel's guard,
    which once restated seven paths against a prompt specifying nine. The number in this
    docstring is now derived from the tuple by
    test_the_readiness_docstring_states_the_real_number_of_questions.
    """
    task = _task_get(task_id)
    if not task:
        return {"error": "unknown task", "status_code": 404}
    uri = str(task.get("plan_uri") or "")
    plan, note = {}, ""
    if not uri:
        note = "no plan yet — the orchestrator writes plan.json once it has enough to price"
    else:
        try:
            raw = s3.get_object(Bucket=data_bucket(),
                                Key=uri[5:].partition("/")[2])["Body"].read()
            plan = json.loads(raw)
        except Exception as e:
            # A 200 with a note, not a 5xx: "the plan is unreadable" is itself a
            # readiness answer the customer needs, and a failed panel would just
            # disappear from the thread.
            note = f"plan.json could not be read ({str(e)[:120]})"
    # Both guards are load-bearing. plan.json is written by a model, so it is not
    # guaranteed to be an object at all: a top-level list parses fine and then
    # AttributeErrors on .get, which surfaced as a 500 on the whole panel.
    if not isinstance(plan, dict):
        note = note or "plan.json is not a JSON object"
        plan = {}
    data = plan.get("data") if isinstance(plan.get("data"), dict) else {}
    fields = []
    for path, label, why in DATA_READINESS_FIELDS:
        val = _dig(data, path)
        answered = val not in (None, "", [], {})
        fields.append({"field": path, "label": label, "why": why,
                       "answered": bool(answered),
                       "value": str(val)[:300] if answered else ""})
    return {"task_id": task_id, "plan_uri": uri, "note": note,
            "answered": sum(1 for f in fields if f["answered"]),
            "total": len(fields), "fields": fields}


def task_approval(task_id):
    """The auditor's endpoint: records + chain + how to verify independently."""
    task = _task_get(task_id)
    if not task:
        return {"error": "unknown task", "status_code": 404}
    approvals = json.loads(json.dumps(task.get("approvals") or [], default=str))
    key_arn = ""
    try:
        key_arn = kms.describe_key(KeyId=APPROVAL_KEY)["KeyMetadata"]["Arn"]
    except Exception:
        pass
    return {"task_id": task_id, "approvals": approvals, "key": APPROVAL_KEY,
            "key_arn": key_arn,
            "verify": "recompute SHA-256 over the canonical JSON of the signed keys "
                      "(sorted, compact), then kms verify --key-id <key_arn> "
                      "--message-type DIGEST --signing-algorithm ECDSA_SHA_256; or "
                      "export the public key once and verify offline"}


# ── Tasks: the async chat worker ──────────────────────────────────────────────

_TRAILER_KEYS = ("plan_uri", "plan_summary", "cost_estimate_usd")


def _parse_plan_trailer(text):
    """The consult protocol ends a proposal with one fenced json block. Tolerant
    parse: last {...} span inside the last fence, else the last {...} in the text."""
    candidate = text
    if "```" in text:
        parts = text.split("```")
        for part in reversed(parts):
            if "{" in part:
                candidate = part
                break
    try:
        start = candidate.index("{")
        end = candidate.rindex("}") + 1
        obj = json.loads(candidate[start:end])
    except Exception:
        return None
    if not isinstance(obj, dict) or not all(k in obj for k in _TRAILER_KEYS):
        return None
    return {k: obj[k] for k in _TRAILER_KEYS}


def _replay_context(task):
    """A fresh session gets a compact reconstruction: the plan state + recent turns.
    The DDB record is the truth; the session is a cache that died."""
    msgs = json.loads(json.dumps(task.get("messages") or [], default=str))
    recent = msgs[-12:]
    lines = ["[session restarted — conversation summary follows]",
             f"Task goal: {task.get('goal', '')}"]
    if task.get("plan_summary"):
        lines.append(f"Current proposed plan: {task['plan_summary']} "
                     f"(plan_uri {task.get('plan_uri')}, "
                     f"cost ${task.get('cost_estimate_usd')})")
    for m in recent:
        lines.append(f"{m.get('role')}: {str(m.get('text'))[:1500]}")
    return "\n".join(lines)


_DISPATCH_RE_ASK = (
    "Your turn ended without calling launch_run, but the plan is ACCEPTED and signed "
    "— describing the dispatch is not dispatching it. Call the launch_run inline "
    "function NOW, exactly once, with {plan_uri, params, cost_estimate_usd}, using "
    "the plan_uri you wrote. Emit only that tool call.")

# The two stop reasons that mean the platform suppressed the turn rather than the model
# producing it. Bedrock spells a model-side filter `content_filtered` and an attached
# guardrail's intervention `guardrail_intervened`; the harness driver has handled both
# since the EvalGate incident, this worker handled only the first. A guardrail block on
# a consult turn therefore ended the task as `drafting` with an EMPTY reply -- no error,
# nothing for the customer to read, nothing for the operator to search -- and on an
# accept turn it ended as "accepted plan was not dispatched by the agent", which is the
# exact false accusation the branch below exists to prevent, one spelling away.
_BLOCKED_STOP_REASONS = ("content_filtered", "guardrail_intervened")

# How often the in-flight reply is written for the browser to see. The frontend polls
# every 3s while a turn is running (frontend.html: busy ? 3000 : 15000), so a shorter
# flush buys nothing a customer can perceive and costs a DynamoDB write per flush.
_STREAM_FLUSH_S = 2.0


def _stream_sink(task_id):
    """A throttled progress writer for the in-flight reply.

    Writes to a SEPARATE `partial_reply` attribute, never into `messages`. Appending
    real messages as the text grows would put half-sentences into the transcript the
    next turn replays to the agent (_replay_context) and into the record an approval is
    signed against — the reply must enter `messages` exactly once, whole, at the end.
    partial_reply is display-only scaffolding and is cleared when the turn lands.

    Returns None when the flush interval has not elapsed, so the caller need not track
    time itself. The closure holds the last-write time; there is one sink per turn and
    one writer per task (the thinking-status lock at the route layer).
    """
    state = {"at": 0.0, "n": 0}

    def sink(text_so_far):
        now = time.time()
        if now - state["at"] < _STREAM_FLUSH_S:
            return
        state["at"] = now
        state["n"] += 1
        tasks_tbl.update_item(
            Key={"id": task_id},
            UpdateExpression="SET partial_reply = :p, updated_at = :t",
            ExpressionAttributeValues={":p": str(text_so_far)[:MSG_TEXT_MAX],
                                       ":t": _now_iso()})

    sink.flushes = lambda: state["n"]
    return sink


def run_task_turn(task_id, accept=False):
    """One orchestrator turn: send pending user/system messages, service tools,
    record the assistant reply. Mirrors the driver's loop, minus task tokens."""
    task = _task_get(task_id)
    if not task:
        return
    b = data_bucket()
    hid = _resolve_harness_id(OPTIMIZE_HARNESS)
    msgs = json.loads(json.dumps(task.get("messages") or [], default=str))
    # everything after the last assistant message is this turn's input
    last_a = max((i for i, m in enumerate(msgs) if m.get("role") == "assistant"),
                 default=-1)
    pending = msgs[last_a + 1:]
    pending_text = "\n\n".join(str(m.get("text", "")) for m in pending) or "(continue)"

    approvals = json.loads(json.dumps(task.get("approvals") or [], default=str))
    approval_ctx = ({"approval": approvals[-1],
                     "cost_estimate_usd": task.get("cost_estimate_usd")}
                    if accept and approvals else None)

    # The default is where to WRITE a new plan; once a plan exists the signed
    # approval's URI is authoritative. Live, the agent wrote its plan under runs/
    # and then stalled reconciling that against a tasks/ default it was handed at
    # dispatch time — a plan_uri the human did not sign is the wrong plan by
    # definition, so never suggest one.
    plan_uri = (((approval_ctx or {}).get("approval") or {}).get("plan_uri")
                or task.get("plan_uri") or f"s3://{b}/tasks/{task_id}/plan.json")
    params = {"task": "consult", "task_id": task_id,
              "plan_uri": str(plan_uri), "bucket": b, "region": REGION}
    # Hand the agent its prices instead of making it fetch them. The prompt used to say
    # "read s3://<bucket>/finops/rates/rate_card_latest.json FIRST", which cost a whole
    # model round-trip per turn -- and X-Ray shows the round-trip IS the latency (one
    # measured 60.6s turn: 8.4s for the call that only decided to run a shell, 44.8s for
    # the call that finally answered). It also did not work: the agent fetched litellm's
    # price file off raw.githubusercontent.com instead, so it paid for the round-trip AND
    # quoted the customer a third party's prices. Read fresh here every turn, so this is
    # not a snapshot that goes stale when pricing_refresh next runs.
    card = rate_card_for_prompt()
    if card:
        params["rate_card"] = card
    envelope = json.dumps({"run_id": f"task-{task_id}", "stage": "consult",
                           "manifest_uri": "", "params": params}, default=str)
    if approval_ctx:
        # An accept turn must always SEE its acceptance. On a resent turn (the first
        # attempt failed) the acceptance sits before the agent's last reply, so the
        # slice above yields "(continue)" and the agent is asked to dispatch by a
        # message that is no longer in front of it. Restate it from the record —
        # the DDB approval is the truth, not the chat scrollback.
        appr = approval_ctx["approval"]
        pending_text = (
            f"PLAN ACCEPTED by {appr.get('approved_by')} "
            f"(record {str(appr.get('record_sha256', ''))[:12]}) at "
            f"{appr.get('approved_at')}. The approved plan is {plan_uri} "
            f"(plan_sha256 {str(appr.get('plan_sha256', ''))[:12]}, "
            f"${appr.get('cost_estimate_usd')}). Call launch_run exactly once with "
            f"that plan_uri now.\n\n" + pending_text)

    sess = _task_session(task)
    fresh_session = int(task.get("session_seq", 0)) > 0 and last_a >= 0
    body_text = (envelope + "\n\n" + (_replay_context(task) + "\n\n" if fresh_session else "")
                 + pending_text)
    messages = _chat_user_text(body_text)

    collected_text = []
    sink = _stream_sink(task_id)
    tool_rounds = 0
    # What the HARNESS spent, accumulated across every invoke this turn makes. Separate
    # from tool_rounds, which counts only the inline functions WE service: a turn using
    # skills logged rounds=0 while actually making 3 model round-trips, so one counter
    # was being read as if it were the other.
    model_rounds = harness_tool_calls = model_ms = in_tok = out_tok = 0
    re_asks = 0          # only spent on a turn that OWES a tool call (see below)
    stream_retried = False
    arn = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT_ID}:harness/{hid}"
    while True:
        try:
            resp = agentcore_chat.invoke_harness(
                harnessArn=arn, runtimeSessionId=sess, messages=messages)
        except Exception as e:
            err = str(e)
            if "session" in err.lower() and int(task.get("session_seq", 0)) < 50:
                # session idled out (900s) — bump seq, replay, retry once
                tasks_tbl.update_item(Key={"id": task_id},
                                      UpdateExpression="SET session_seq = session_seq + :one",
                                      ExpressionAttributeValues={":one": 1})
                task = _task_get(task_id)
                sess = _task_session(task)
                messages = _chat_user_text(envelope + "\n\n"
                                           + _replay_context(task) + "\n\n"
                                           + pending_text)
                try:
                    resp = agentcore_chat.invoke_harness(
                        harnessArn=arn, runtimeSessionId=sess, messages=messages)
                except Exception as e2:
                    _task_fail(task_id, f"invoke failed after session retry: {e2}")
                    return
            else:
                _task_fail(task_id, f"invoke failed: {e}")
                return

        # Show the reply as it arrives. The prefix carries text from earlier rounds of
        # this same turn, or a tool round would appear to erase what the agent already
        # said in front of the customer.
        prefix = "\n".join(t for t in collected_text if t)
        out = _drain_chat(resp, on_text=(
            lambda s, _p=prefix: sink((_p + "\n" + s) if _p else s)))
        model_rounds += out["rounds"]
        harness_tool_calls += len(out["harness_tools"])
        model_ms += out["model_ms"]
        in_tok += out["in_tok"]
        out_tok += out["out_tok"]
        # One line per turn. The first diagnosis of a stalled dispatch had NOTHING to
        # read here — the task record showed the symptom and the logs showed only
        # billing REPORTs, so the cause had to be inferred twice.
        # flushes= is here because its absence cost a diagnosis: the first live check saw
        # no partial_reply and this line could not say whether the sink never fired or the
        # 3s poll simply missed a short turn. Those need opposite fixes.
        #
        # `serviced=` is the old `rounds=` renamed. The name was the defect: it counted
        # only inline functions WE answer, so a turn that made 3 model round-trips and 2
        # internal shell calls logged `rounds=0`, and that zero was read as "the agent
        # did no work". model_rounds/htools/model_ms come off the stream itself and are
        # what a latency question actually needs -- on the measured turn, model_ms was
        # 21.7s of a 29.6s wall clock, i.e. the round-trips ARE the turn.
        #
        # `tool=` names only a call we OWE, for the same reason. A toolUse block also
        # streams alongside end_turn for a tool the harness already ran itself (see the
        # servicing condition below, which deliberately skips those), and the live line
        # read `stop=end_turn tool=shell` -- indistinguishable from a call we failed to
        # answer. Those calls are in htools=, where they belong.
        owed = (out["tool_use"] or {}).get("name") \
            if out["stop_reason"] == "tool_use" else None
        print(f"[task-chat] {task_id} accept={accept} sess={sess} "
              f"stop={out['stop_reason']} tool={owed} "
              f"text={len(out['text'] or '')}b err={out['error']} "
              f"serviced={tool_rounds} model_rounds={model_rounds} "
              f"htools={out['harness_tools']} model_ms={model_ms} "
              f"tok={in_tok}/{out_tok} re_asks={re_asks} flushes={sink.flushes()}")
        if out["text"]:
            collected_text.append(out["text"])

        if out["error"] and not out["tool_use"] and not stream_retried:
            # Involuntary stream death mid-drain — routine in production, and the
            # driver has salvaged it since Phase 5 with a same-session retry. The
            # chat worker did not, so a connection reset during an accept turn
            # surfaced as "the agent refused to dispatch": a false accusation
            # against the agent for a network event.
            stream_retried = True
            messages = _chat_user_text(
                "The stream was interrupted. Continue from where you left off; "
                "call your pending inline function.")
            continue

        if (out["stop_reason"] in _BLOCKED_STOP_REASONS and not out["text"]
                and not out["tool_use"]):
            # The model was BLOCKED, not disobedient. Re-asking cannot help (nothing
            # can leave that session) and calling it "the agent didn't dispatch"
            # points the operator at the wrong thing entirely. The stop reason is
            # named from the stream, not hardcoded: two spellings reach here and an
            # error_msg that says content_filtered for a guardrail block sends the
            # operator to the model's own filter instead of to the guardrail they
            # attached, which is the one thing they can actually change.
            _task_fail(task_id, f"the model's reply was blocked (stopReason="
                                f"{out['stop_reason']}); no output could be "
                                "produced. Rephrase and resend, or start a fresh "
                                "session.")
            return

        tu = out["tool_use"]

        # Service a tool call only when the harness STOPPED for one. A toolUse block
        # can also stream alongside end_turn — that is a call the harness already ran
        # itself (live: `shell`), and answering it makes the next ConverseStream
        # invalid ("toolResult blocks ... exceeds the number of toolUse blocks of
        # previous turn"), which killed three accept turns in a row. An accept turn
        # that ends this way still owes a dispatch; the re-ask below asks in text.
        if tu and out["stop_reason"] == "tool_use":
            tool_rounds += 1
            if tool_rounds > 8:
                # A run that was already launched is spending money right now.
                # Calling the task `error` would hide it from the operator — the cap
                # stops the conversation, it does not undo the dispatch.
                if _task_get(task_id).get("run_id"):
                    print(f"[task-chat] {task_id} hit the round cap after dispatch; "
                          "keeping status=dispatched")
                    break
                _task_fail(task_id, "tool loop exceeded 8 rounds")
                return
            name, args = tu["name"], tu.get("input") or {}
            if name == "checkpoint":
                messages = _chat_tool_result(tu, {"status": "continue"})
                continue
            if name == "page_human":
                if LLMOPS_SNS_TOPIC:
                    try:
                        sns.publish(TopicArn=LLMOPS_SNS_TOPIC,
                                    Subject=f"[llmops task {task_id}] orchestrator paged a human",
                                    Message=json.dumps(args, default=str)[:1500])
                    except Exception:
                        pass
                _task_event(task_id, "HumanPaged", "orchestrator", args)
                messages = _chat_tool_result(tu, {"status": "paged"})
                continue
            if name == "launch_run":
                # "exactly once" cannot be left to the prompt: a second call would
                # start a second GPU run against one signed approval. But the unit
                # of idempotency is the ACCEPTANCE RECORD, not the task: a
                # consultation thread whose earlier run died gets a fresh signature
                # for its continuation, and that new acceptance has never been
                # honored. The first version of this guard keyed on run_id alone,
                # which made every continuation signed in an existing thread
                # undispatchable forever — found live when continuation #5's
                # acceptance (record 17fd4218…) was refused because the dead
                # continuation #4's run_id still sat on the task row.
                task_now = _task_get(task_id)
                already = str(task_now.get("run_id") or "")
                latest_rec = str(((task_now.get("approvals") or [{}])[-1]
                                  or {}).get("record_sha256", ""))
                honored = str(task_now.get("dispatched_record") or "")
                # Block when this exact acceptance already produced a run — and,
                # conservatively, for pre-fix rows that carry a run_id but never
                # recorded which acceptance it honored (dispatched_record absent):
                # those cannot prove the new signature is new, so an operator
                # clears them deliberately rather than this guard guessing.
                if already and (not honored or honored == latest_rec):
                    messages = _chat_tool_result(tu, {
                        "status": "already_dispatched", "run_id": already,
                        "reason": "this acceptance was already dispatched; one "
                                  "acceptance authorizes exactly one run. Report "
                                  "this run_id."})
                    continue
                if not accept:
                    messages = _chat_tool_result(tu, {
                        "status": "rejected",
                        "reason": "No PLAN ACCEPTED message has been issued. Do not "
                                  "dispatch — propose the plan and wait for the human."})
                    continue
                result = conductor_tools.service_launch_run(
                    lam, s3, kms, {**args, "approval": (approval_ctx or {}).get("approval")},
                    START_FN, expected=approval_ctx)
                if not result["ok"]:
                    # The reason travels inside a toolResult the operator never sees.
                    # Without it in the log, a rejected dispatch and a dispatch that
                    # was never attempted look identical from outside.
                    print(f"[task-chat] {task_id} launch_run REJECTED: "
                          f"{result['reason']}")
                    messages = _chat_tool_result(tu, {
                        "status": "rejected", "reason": result["reason"]})
                    continue
                tasks_tbl.update_item(
                    Key={"id": task_id},
                    UpdateExpression="SET #s = :s, run_id = :r, execution_arn = :x, "
                                     "updated_at = :t, dispatched_record = :rec",
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues={":s": "dispatched", ":r": result["run_id"],
                                               ":x": result.get("execution_arn", ""),
                                               ":t": _now_iso(),
                                               # which acceptance this run honors —
                                               # the idempotency guard's key
                                               ":rec": latest_rec})
                _task_event(task_id, "RunDispatched", "orchestrator",
                            {"run_id": result["run_id"]})
                messages = _chat_tool_result(tu, {
                    "status": "dispatched", "run_id": result["run_id"]})
                continue
            messages = _chat_tool_result(tu, {"status": "unsupported"})
            continue

        # No tool call. For most consult turns that IS the turn: the agent asked the
        # customer a question and it is the human's move. But an accept turn owes a
        # launch_run, and the live smoke run showed the model narrating "Dispatching
        # exactly once with that URI:" and then stopping — the missing-signal failure
        # the driver has re-asked for since Phase 5. Marking that an error strands a
        # signed approval and blames the agent for what one nudge would have fixed.
        owes_dispatch = accept and str(_task_get(task_id).get("status", "")) != "dispatched"
        if owes_dispatch and re_asks < 2:
            re_asks += 1
            messages = _chat_user_text(_DISPATCH_RE_ASK)
            continue

        break  # the turn is genuinely done

    reply = "\n".join(t for t in collected_text if t).strip()
    task = _task_get(task_id)  # re-read: launch_run may have set dispatched
    now_status = str(task.get("status", ""))
    new_msgs = []
    if reply:
        # The agent's own words, uncut, into the audit copy. This is the message that
        # actually hit the 8000 cap live, and it is an assistant reply -- the one an
        # acceptance is signed against.
        new_msgs.append({"role": "assistant", "text": reply, "at": _now_iso(),
                         "by": "orchestrator"})

    trailer = _parse_plan_trailer(reply) if reply else None
    if now_status == "dispatched":
        final_status = "dispatched"
    elif accept:
        # accepted but the agent never dispatched — that is an error, not drafting
        final_status, err = "error", "accepted plan was not dispatched by the agent"
        tasks_tbl.update_item(Key={"id": task_id},
                              UpdateExpression="SET error_msg = :e",
                              ExpressionAttributeValues={":e": err})
    elif trailer:
        final_status = "plan_proposed"
    else:
        final_status = "drafting"

    update = "#s = :s"
    names = {"#s": "status"}
    values = {":s": final_status}
    if trailer:
        update += ", plan_uri = :pu, plan_summary = :ps, cost_estimate_usd = :ce"
        values.update({":pu": str(trailer["plan_uri"])[:500],
                       ":ps": str(trailer["plan_summary"])[:2000],
                       ":ce": str(trailer["cost_estimate_usd"])[:50]})
        _task_event(task_id, "PlanProposed", "orchestrator",
                    {"cost": str(trailer["cost_estimate_usd"])})
    # The streamed draft dies in the same write that commits the real message, so a poll
    # can never catch both and render the reply twice.
    if new_msgs:
        _append_messages(task_id, new_msgs, update, names, values, drop_partial=True)
    else:
        tasks_tbl.update_item(Key={"id": task_id},
                              UpdateExpression=f"SET {update}, updated_at = :t "
                                               f"REMOVE partial_reply",
                              ExpressionAttributeNames=names,
                              ExpressionAttributeValues={**values, ":t": _now_iso()})


def _task_fail(task_id, msg):
    tasks_tbl.update_item(Key={"id": task_id},
                          UpdateExpression="SET #s = :s, error_msg = :e, updated_at = :t "
                                           "REMOVE partial_reply",
                          ExpressionAttributeNames={"#s": "status"},
                          ExpressionAttributeValues={":s": "error", ":e": str(msg)[:300],
                                                     ":t": _now_iso()})
    _task_event(task_id, "TurnFailed", "worker", {"error": str(msg)[:200]})


def _drain_chat(resp, on_text=None):
    """Ported from the driver's _drain — same stream shape, same tolerance.

    on_text(joined_text_so_far) is called as deltas arrive, so the customer can watch
    the reply build instead of staring at "thinking…" for the whole turn. MEASURED on
    the live orchestrator harness: the first text delta landed at 8.4s of a 24.65s turn
    and the 214 deltas spread over 16.04s — 65% of wall clock. The stream was always
    incremental; the buffering was ours, right here, in a list nobody could see.

    The sink is called at most every _STREAM_FLUSH_S seconds (see _stream_sink): one
    DynamoDB write per delta would be 214 writes for that turn, which is both throttling
    risk and a cost nobody asked for.

    A sink that raises must not kill the turn — a failed progress write is cosmetic, but
    losing the reply is not. So it is called defensively.

    Also counts what the HARNESS did on its own, which this function used to throw
    away. One InvokeHarness call is not one model round-trip: the harness runs its own
    agent loop, and each internal tool call (a skill read, `shell`, the browser) costs a
    further model round-trip that never reaches our servicing loop. Measured on the
    orchestrator with a skill-reading prompt, ONE invoke carried:

        messageStart(assistant) -> toolUse shell -> messageStop tool_use -> metadata
        messageStart(user)      -> messageStop tool_result
        messageStart(assistant) -> toolUse shell -> messageStop tool_use -> metadata
        messageStart(user)      -> messageStop tool_result
        messageStart(assistant) -> messageStop end_turn                  -> metadata

    Three round-trips, two internal shell calls, latencyMs 6973 + 7764 + 6950 = 21.7s of
    a 29.6s turn. The old log line reported `rounds=0` for exactly that turn, because it
    only counted rounds WE serviced and this turn ended in end_turn.

    Round-trips are counted from assistant `messageStart`, which is what a round-trip IS.
    `metadata` is counted separately rather than trusted as a proxy: the two agreed 3-to-3
    when measured, and logging both means a future divergence shows up instead of being
    silently absorbed by whichever one this code happened to pick.
    """
    text, tool_use, stop_reason, error = [], None, None, None
    rounds = meta_n = in_tok = out_tok = model_ms = 0
    harness_tools = []
    try:
        for ev_ in resp.get("stream", []):
            if "messageStart" in ev_:
                # user messageStart events are the harness feeding a toolResult back to
                # itself; only an assistant message is the model being asked again.
                if ev_["messageStart"].get("role") == "assistant":
                    rounds += 1
            if on_text is not None and "contentBlockDelta" in ev_:
                d_ = ev_["contentBlockDelta"].get("delta", {})
                if "text" in d_:
                    try:
                        on_text("".join(text) + d_["text"])
                    except Exception as sink_exc:
                        print(f"[task-chat] progress sink failed (ignored): {sink_exc}")
            if "contentBlockDelta" in ev_:
                delta = ev_["contentBlockDelta"].get("delta", {})
                if "text" in delta:
                    text.append(delta["text"])
            if "contentBlockStart" in ev_:
                start = ev_["contentBlockStart"].get("start", {})
                if "toolUse" in start:
                    harness_tools.append(start["toolUse"].get("name"))
                    tool_use = {"toolUseId": start["toolUse"].get("toolUseId"),
                                "name": start["toolUse"].get("name"), "input": ""}
            if tool_use is not None and "contentBlockDelta" in ev_:
                delta = ev_["contentBlockDelta"].get("delta", {})
                if "toolUse" in delta:
                    tool_use["input"] += delta["toolUse"].get("input", "")
            if "messageStop" in ev_:
                stop_reason = ev_["messageStop"].get("stopReason")
            if "metadata" in ev_:
                meta_n += 1
                usage = ev_["metadata"].get("usage") or {}
                in_tok += int(usage.get("inputTokens") or 0)
                out_tok += int(usage.get("outputTokens") or 0)
                model_ms += int((ev_["metadata"].get("metrics") or {})
                                .get("latencyMs") or 0)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    if tool_use is not None and isinstance(tool_use.get("input"), str):
        try:
            tool_use["input"] = json.loads(tool_use["input"] or "{}")
        except json.JSONDecodeError:
            tool_use["input"] = {"_raw": tool_use["input"]}
    return {"text": "".join(text), "tool_use": tool_use,
            "stop_reason": stop_reason, "error": error,
            "rounds": rounds, "meta_n": meta_n, "harness_tools": harness_tools,
            "in_tok": in_tok, "out_tok": out_tok, "model_ms": model_ms}


def _chat_user_text(text):
    """A plain user turn, as the messages list invoke_harness wants."""
    return [{"role": "user", "content": [{"text": text}]}]


def _chat_tool_result(tool_use, payload):
    """Resume a paused inline function: echo the toolUse, THEN answer it.

    The InvokeHarness pause/resume contract is two messages -- an assistant message
    replaying the toolUse block the agent emitted, and a user message carrying the
    matching toolResult. Sending the toolResult by itself gets the whole turn
    rejected with "The number of toolResult blocks at messages.N.content exceeds the
    number of toolUse blocks of previous turn": in the history the runtime is handed,
    nothing ever called the tool being answered. That killed every dispatch attempt
    across four sessions, and it fails identically for a well-formed stopReason=
    tool_use turn, so it is the message shape and nothing about the model.

    The payload rides in a TEXT block, not a `json` block -- the runtime rejects
    those with "content_type=<json_> | unsupported type". JSON-in-text is accepted
    everywhere and stays machine-readable on the model's side."""
    return [
        {"role": "assistant", "content": [{"toolUse": {
            "toolUseId": tool_use["toolUseId"],
            "name": tool_use["name"],
            "input": tool_use.get("input") or {}}}]},
        {"role": "user", "content": [{"toolResult": {
            "toolUseId": tool_use["toolUseId"],
            "content": [{"text": json.dumps(payload, default=str)}],
            "status": "success"}}]},
    ]


# ── HTTP glue (ported: security headers, auth, response helper) ───────────────
# CSP: the dashboard is one self-contained HTML document served by this Lambda.
# 'unsafe-inline' is unavoidable today (inline <script> + onclick handlers); CSP is
# defence-in-depth, escaping at the sink (esc()/jstr() in frontend.html) is the
# primary XSS control.
def _upload_origin():
    """The single S3 origin the browser may PUT a dataset to.

    connect-src must name it explicitly or our own CSP blocks the presigned upload --
    the failure looks like a broken S3 permission but is this header. Scoped to this one
    bucket rather than a wildcard: 'https://*.s3.amazonaws.com' would authorise every
    bucket on earth as a fetch target from this page.

    Falls back to a bare-bucket-less origin only if the bucket cannot be resolved at
    cold start, in which case uploads are broken anyway and a wildcard would be a
    silent security downgrade in exchange for nothing.
    """
    try:
        return f"https://{data_bucket()}.s3.{REGION}.amazonaws.com"
    except Exception:
        return ""


def _csp(upload=True):
    """Built per response, not once at import.

    The upload origin needs data_bucket(), which may resolve through SSM. Freezing this
    into a module constant means a single transient SSM failure at cold start bakes an
    upload-less CSP into that container for its whole life -- and the symptom is a
    browser upload blocked by a header, which reads as an S3 permission problem and
    would cost hours. data_bucket() caches on success, so this stays a dict lookup after
    the first resolve.

    `upload=False` omits that origin -- and with it the SSM lookup. It exists for the
    Introduction routes: that page is the default landing tab and issues no fetch of any
    kind (its audio is same-origin, covered by default-src), so naming an S3 upload
    target in its CSP authorises a request it will never make. Worse, data_bucket()
    swallows a failed resolve and does NOT cache it, so every intro response would retry
    Parameter Store -- making the first thing a first-time visitor sees depend on the
    health of a service it has no reason to touch. Omitting an origin is also strictly
    the tighter header, so this is not a security trade.
    """
    origin = _upload_origin() if upload else ""
    connect = f"connect-src 'self' {origin}".rstrip() if origin else "connect-src 'self'"
    return ("default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            + connect + "; frame-src 'self'; frame-ancestors 'self'; "
            "form-action 'self'; base-uri 'none'; object-src 'none'")

_SEC_HEADERS = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "SAMEORIGIN",
    "referrer-policy": "strict-origin-when-cross-origin",
    "strict-transport-security": "max-age=31536000; includeSubDomains",
}


def _resp(code, body, ctype="application/json", cookies=None, csp_upload=True):
    # csp_upload=False: see _csp(). Only the Introduction routes pass it -- every other
    # response is for a page that does fetch S3, and defaults must serve the many.
    headers = {"content-type": ctype}
    headers.update(_SEC_HEADERS)
    headers["content-security-policy"] = _csp(csp_upload)
    if ctype.startswith("application/json"):
        headers["cache-control"] = "no-store"
    if ALLOWED_ORIGIN:
        headers.update({"access-control-allow-origin": ALLOWED_ORIGIN,
                        "access-control-allow-methods": "GET,POST,OPTIONS",
                        "access-control-allow-headers": "content-type,authorization",
                        "vary": "origin"})
    out = {"statusCode": code, "headers": headers,
           "body": body if isinstance(body, str) else json.dumps(body, default=str)}
    # Payload format 2.0 takes Set-Cookie as a LIST under "cookies", not as a header:
    # a "set-cookie" key in `headers` is silently dropped when there is more than one,
    # and the refresh-cookie feature would look like a browser bug. The key is omitted
    # entirely when there are no cookies so every existing response byte is unchanged.
    if cookies:
        out["cookies"] = list(cookies)
    return out


def _resp_result(result):
    """Let a handler choose its own status code without knowing about HTTP.

    The cost handlers return {"error": ..., "status_code": 400|403|409}. Flattening those
    to 200-with-an-error-body would make a denied approval indistinguishable from a
    granted one to anything but a human reading the JSON — and audit tooling reads
    status codes.

    Only an EXPLICIT status_code is honoured; anything else stays 200. Inferring 400
    from the presence of an "error" key would silently re-code the seven pre-existing
    POST routes, whose frontend handlers all read `j.error` off a 200.
    """
    if isinstance(result, dict) and result.get("status_code"):
        code = int(result.pop("status_code"))
        return _resp(code, result)
    return _resp(200, result)


def cognito_login(username, password):
    """Exchange username/password for a Cognito access token (USER_PASSWORD_AUTH).

    Returns (response_body, refresh_token). The refresh token is returned SEPARATELY,
    never inside the body, because the body is JSON that page script reads: the whole
    point of the refresh cookie is that a 30-day credential is unreachable from script,
    and a single forgotten `del body["refreshToken"]` would undo that silently. A tuple
    cannot be leaked by forgetting to strip a key.
    """
    if not COGNITO_CLIENT_ID:
        return {"error": "Cognito not configured"}, ""
    try:
        r = cognito.initiate_auth(ClientId=COGNITO_CLIENT_ID, AuthFlow="USER_PASSWORD_AUTH",
                                  AuthParameters={"USERNAME": username, "PASSWORD": password})
        a = r["AuthenticationResult"]
        return ({"accessToken": a["AccessToken"], "expiresIn": a["ExpiresIn"]},
                a.get("RefreshToken", ""))
    except cognito.exceptions.NotAuthorizedException:
        return {"error": "invalid username or password"}, ""
    except Exception as e:
        return {"error": str(e)[:200]}, ""


# The refresh cookie is scoped to this path and nothing else. Every other API route
# authenticates with a Bearer access token in a header, so no other route has any use
# for the cookie -- and a cookie the browser never attaches to /api/tasks cannot be
# replayed against /api/tasks. /api/refresh/revoke path-matches this prefix (RFC 6265
# path-match), which is what lets sign-out revoke the token server-side instead of
# merely forgetting it client-side.
REFRESH_COOKIE = "llmops_rt"
REFRESH_COOKIE_PATH = "/api/refresh"
# Cognito's refresh validity (30 days on this pool) is the real authority; this is only
# how long the browser bothers to keep the cookie. If the two ever disagree, the worst
# case is one /api/refresh returning 401 and the user signing in -- so this number is
# allowed to be approximate, and deliberately is not read back from Cognito on the login
# hot path.
REFRESH_COOKIE_MAX_AGE_S = int(os.environ.get("REFRESH_COOKIE_MAX_AGE_S", 30 * 24 * 3600))


def _refresh_cookie(token, max_age=None):
    """Serialise the refresh cookie. HttpOnly+Secure+SameSite=Strict, all three needed.

    HttpOnly is the feature: script cannot read it, so an XSS bug costs the attacker one
    8-hour access token instead of 30 days of re-issue. Secure keeps it off any plaintext
    hop. SameSite=Strict means no other site can make the browser attach it, which is the
    CSRF answer for a route whose whole input is a cookie.
    """
    age = REFRESH_COOKIE_MAX_AGE_S if max_age is None else int(max_age)
    return (f"{REFRESH_COOKIE}={token}; Path={REFRESH_COOKIE_PATH}; Max-Age={age}; "
            "HttpOnly; Secure; SameSite=Strict")


def _clear_refresh_cookie():
    """Expire the cookie. Same name+path, or the browser keeps the original alongside."""
    return _refresh_cookie("", max_age=0)


def _event_cookie(event, name):
    """Read one cookie from a payload-format-2.0 event.

    2.0 hands cookies over as a list of "k=v" strings in event["cookies"], NOT in the
    headers dict. Reading headers["cookie"] here would work in a hand-written test and
    return nothing in production.
    """
    for raw in (event.get("cookies") or []):
        k, _, v = str(raw).partition("=")
        if k.strip() == name:
            return v.strip()
    return ""


def cognito_refresh(refresh_token):
    """Mint a fresh access token from the refresh cookie (REFRESH_TOKEN_AUTH).

    A failure here is NOT an error to surface as 500: an expired or revoked refresh token
    is the normal end of a 30-day session, and the client's correct response is to show
    the sign-in prompt. So this returns {"error": ...} and the route answers 401.
    """
    if not COGNITO_CLIENT_ID:
        return {"error": "Cognito not configured"}
    if not refresh_token:
        return {"error": "no session"}
    try:
        r = cognito.initiate_auth(ClientId=COGNITO_CLIENT_ID, AuthFlow="REFRESH_TOKEN_AUTH",
                                  AuthParameters={"REFRESH_TOKEN": refresh_token})
        a = r["AuthenticationResult"]
        # REFRESH_TOKEN_AUTH returns no new refresh token (rotation is off), so the
        # cookie is left exactly as it is rather than being rewritten with "".
        return {"accessToken": a["AccessToken"], "expiresIn": a["ExpiresIn"]}
    except Exception as e:
        return {"error": str(e)[:200]}


def cognito_revoke(refresh_token):
    """Best-effort RevokeToken on sign-out; the cookie is cleared either way.

    Token revocation is enabled on this pool, so revoking kills the whole token family.
    If the call fails we still clear the cookie: a sign-out that reports failure and
    leaves the browser signed in is worse than one that reports success while an
    unreachable token stays technically valid for its remaining window.
    """
    if not (refresh_token and COGNITO_CLIENT_ID):
        return False
    try:
        cognito.revoke_token(Token=refresh_token, ClientId=COGNITO_CLIENT_ID)
        return True
    except Exception as e:
        print(f"[auth] revoke_token failed: {e}")
        return False


def _authed_user(headers):
    """Return {"username", "groups"} for a valid token, else None.

    GetUser validates the token server-side (signature, expiry, revocation) and returns
    the username — but NOT group membership, and a bearer *access* token carries no
    cognito:groups claim either. So the approver check needs a second call,
    AdminListGroupsForUser. Getting this wrong would leave the separation-of-duties gate
    with nothing to check: it would read as enforced while approving everything.

    A groups lookup that fails yields an EMPTY group list rather than an exception. The
    caller then denies, because "we could not prove you are an approver" must land on
    the deny side; a throttled Cognito call must not become an approval.
    """
    h = {k.lower(): v for k, v in (headers or {}).items()}
    auth = h.get("authorization", "")
    if not (auth.startswith("Bearer ") and COGNITO_POOL_ID):
        return None
    try:
        who = cognito.get_user(AccessToken=auth[7:].strip())
    except Exception:
        return None
    username = who.get("Username", "")
    groups = []
    try:
        g = cognito.admin_list_groups_for_user(UserPoolId=COGNITO_POOL_ID,
                                               Username=username, Limit=60)
        groups = [x.get("GroupName", "") for x in g.get("Groups", [])]
    except Exception as e:
        print(f"[auth] group lookup failed for {username}: {e}")
    # sub: the immutable Cognito identity behind the (renameable) username — the
    # approval records bind signatures to it so accountability survives a rename.
    sub = next((a.get("Value", "") for a in who.get("UserAttributes", [])
                if a.get("Name") == "sub"), "")
    return {"username": username, "groups": groups, "sub": sub}


def _authed(headers):
    """Thin bool wrapper so the pre-existing POST routes keep their exact contract."""
    return _authed_user(headers) is not None


def intro_audio(path):
    """Serve one bundled narration clip: GET /intro/audio/<lang>/<scene>.mp3.

    Two things here are deliberate.

    **The clip is looked up in INTRO_CLIPS before any filesystem path is built.** Both
    path segments come from the request, so joining them onto a directory first and
    validating afterwards is the traversal bug; a membership test against what cold start
    found on disk has no traversal to defend against. `..` is simply not a key.

    **The body is base64 with `isBase64Encoded`.** API Gateway payload format 2.0 sends
    the `body` string as UTF-8 unless this flag is set, and MP3 bytes are not valid
    UTF-8 — without the flag the audio arrives corrupted with a 200 status, which the
    browser reports only as a decode error. `_resp` cannot express this, which is why
    this route builds its own envelope rather than reusing it; the security headers are
    still applied, so the response is not a hole in them.
    """
    parts = path.split("/")            # ['', 'intro', 'audio', lang, '<scene>.mp3']
    if len(parts) != 5 or not parts[4].endswith(".mp3"):
        return _resp(404, {"error": "expected /intro/audio/<lang>/<scene>.mp3"},
                     csp_upload=False)
    lang, scene = parts[3], parts[4][:-4]
    if (lang, scene) not in INTRO_CLIPS:
        # A 404 here is not an error state for the page: it falls back to browser speech
        # for this clip. Say what IS bundled so a missing language is diagnosable from
        # one request instead of a deploy-log hunt.
        return _resp(404, {
            "error": f"no narration clip for {lang}/{scene} in this bundle",
            "languages": sorted({l for l, _ in INTRO_CLIPS}),
            "clips": len(INTRO_CLIPS),
        }, csp_upload=False)
    with open(os.path.join(INTRO_AUDIO_DIR, lang, f"{scene}.mp3"), "rb") as f:
        data = f.read()
    headers = {"content-type": "audio/mpeg"}
    headers.update(_SEC_HEADERS)
    headers["content-security-policy"] = _csp(upload=False)
    # The clips are immutable for the life of a deployment — the filename does not change
    # when the text does, so the cache is keyed on the deployment, not the content. A day
    # is short enough that a redeploy is picked up by the next visitor rather than being
    # served stale for a week, and long enough that replaying a scene is free.
    headers["cache-control"] = "public, max-age=86400"
    return {"statusCode": 200, "headers": headers,
            "body": base64.b64encode(data).decode("ascii"), "isBase64Encoded": True}


def handler(event, context):
    # Async self-invocation path (task-chat worker)
    if isinstance(event, dict) and event.get("mode") == "task-chat":
        try:
            run_task_turn(event.get("task_id", ""), bool(event.get("accept")))
        except Exception as e:
            if event.get("task_id"):
                _task_fail(event["task_id"], f"worker crashed: {e}")
        return {"ok": True}

    # Async self-invocation path (optimization draft worker)
    if isinstance(event, dict) and event.get("mode") == "optimize":
        try:
            generate_optimization(event.get("now_iso", ""), event.get("opt_id"),
                                  event.get("harness_id"))
        except Exception as e:
            if console_tbl and event.get("opt_id"):
                console_tbl.update_item(Key={"id": event["opt_id"]},
                                        UpdateExpression="SET #s = :s, error_msg = :e",
                                        ExpressionAttributeNames={"#s": "status"},
                                        ExpressionAttributeValues={":s": "error", ":e": str(e)[:300]})
        return {"ok": True}

    rc = (event.get("requestContext") or {}).get("http") or {}
    method = rc.get("method", "GET")
    path = rc.get("path", "/")
    headers = event.get("headers") or {}
    qs = event.get("queryStringParameters") or {}

    if method == "OPTIONS":
        return _resp(204, "")

    if method == "GET" and path in ("/", "/index.html"):
        return _resp(200, FRONTEND_HTML, "text/html; charset=utf-8")

    if method == "GET" and path in ARCH_SVGS:
        svg = ARCH_SVGS[path]
        if svg is None:
            return _resp(404, {"error": f"{path} missing from the deployed bundle"})
        return _resp(200, svg, "image/svg+xml; charset=utf-8")

    if method == "GET" and path == "/intro":
        if INTRO_HTML is None:
            # Named, not blank. The Introduction tab loads this in an iframe, and an empty
            # frame is indistinguishable from a broken one — this says which build step
            # did not run.
            return _resp(404, f"<h1>intro.html missing from bundle: {INTRO_ERR}</h1>"
                              "<p>deploy/console/deploy.sh runs "
                              "deploy/console/intro/build_intro.py to produce it.</p>",
                         "text/html; charset=utf-8", csp_upload=False)
        # csp_upload=False: this page fetches nothing. See _csp().
        return _resp(200, INTRO_HTML, "text/html; charset=utf-8", csp_upload=False)

    if method == "GET" and path.startswith("/intro/audio/"):
        return intro_audio(path)

    try:
        if method == "GET" and path == "/api/overview":
            return _resp(200, overview())
        if method == "GET" and path == "/api/pipeline":
            return _resp(200, pipeline_detail(qs.get("execution")))
        if method == "GET" and path == "/api/run":
            return _resp(200, run_detail(qs.get("run_id", "")))
        if method == "GET" and path == "/api/observability":
            return _resp(200, observability(qs.get("hours", 24)))
        if method == "GET" and path == "/api/evaluations":
            return _resp(200, evaluations())
        if method == "GET" and path == "/api/batch-evals":
            return _resp(200, {"batchEvaluations": list_batch_evaluations()})
        if method == "GET" and path == "/api/insights-reports":
            return _resp(200, {"reports": list_insights_reports()})
        if method == "GET" and path == "/api/insights-report":
            return _resp(200, get_insights_report(qs.get("id", "")))
        if method == "GET" and path == "/api/optimizations":
            return _resp(200, {"recommendations": list_optimizations(),
                               "native": list_native_recommendations()})
        if method == "GET" and path == "/api/cost-overview":
            out = cost_overview(qs.get("days", 30))
            out["rate_card"] = rate_card_health()
            return _resp(200, out)
        if method == "GET" and path == "/api/cost-estimates":
            out = cost_estimates(qs.get("limit", 50))
            # Reuses the reads above rather than re-querying both tables — one page
            # render should not cost four DynamoDB queries where two suffice.
            out["variance"] = cost_variance(estimates=out["estimates"],
                                            overview=cost_overview())
            return _resp(200, out)
        # The consult plane's READS are authenticated too, gated by PREFIX rather than by
        # a list of paths. Every route above this line is aggregated operational fact --
        # what ran, what it scored, what it cost -- and is public on purpose. Everything
        # under /api/tasks is a customer engagement.
        if method == "GET" and _is_consult_path(path):
            user = _authed_user(headers)
            if user is None:
                return _resp(401, {"error": "unauthorized"})
            if not _user_may_task(user):
                return _resp(403, {"error": f"membership in {DS_GROUP} or "
                                            f"{APPROVER_GROUP} required"})
            if path == "/api/tasks":
                return _resp(200, list_tasks(qs.get("limit", 25)))
            if path.startswith("/api/tasks/"):
                seg = path[len("/api/tasks/"):].split("/")
                if len(seg) == 1:
                    return _resp_result(get_task(seg[0]))
                if len(seg) == 2 and seg[1] == "approval":
                    return _resp_result(task_approval(seg[0]))
                if len(seg) == 2 and seg[1] == "readiness":
                    return _resp_result(task_readiness(seg[0]))

        raw = event.get("body") or "{}"
        if event.get("isBase64Encoded"):
            raw = base64.b64decode(raw).decode("utf-8", "replace")
        try:
            body = json.loads(raw)
        except Exception:
            body = {}
        if method == "POST" and path == "/api/login":
            out, rt = cognito_login(str(body.get("username", "")), str(body.get("password", "")))
            # The cookie is set only on success. Setting it on a failed login would be a
            # cookie holding "" that a later /api/refresh would treat as a session.
            return _resp(200, out, cookies=[_refresh_cookie(rt)] if rt else None)
        # Restore a session after a page reload. Unauthenticated by design -- the cookie
        # IS the credential, and requiring a Bearer token here would mean needing a live
        # session to recover from having lost one.
        if method == "POST" and path == "/api/refresh":
            out = cognito_refresh(_event_cookie(event, REFRESH_COOKIE))
            if out.get("error"):
                # Clear the cookie on failure: a refresh token Cognito rejects will be
                # rejected on every reload forever, and leaving it makes each page load
                # pay a doomed Cognito round-trip.
                return _resp(401, out, cookies=[_clear_refresh_cookie()])
            return _resp(200, out)
        # Sign-out. Also unauthenticated: the access token may already be expired, and
        # refusing to revoke a session because it is too old to prove is backwards.
        if method == "POST" and path == "/api/refresh/revoke":
            revoked = cognito_revoke(_event_cookie(event, REFRESH_COOKIE))
            return _resp(200, {"revoked": revoked}, cookies=[_clear_refresh_cookie()])
        if method == "POST":
            # One auth call for every POST, resolved to a user rather than a bool: the
            # cost routes need the username (self-approval check) and the groups
            # (approver check), and re-calling Cognito per route would double the
            # latency of the approval click for no benefit.
            user = _authed_user(headers)
            if user is None:
                return _resp(401, {"error": "unauthorized"})
            # source IP rides into approval records: who signed, from where
            user["source_ip"] = rc.get("sourceIp", "")
            now = datetime.now(timezone.utc).isoformat()
            if path == "/api/tasks":
                return _resp_result(create_task(body, user))
            if path.startswith("/api/tasks/"):
                seg = path[len("/api/tasks/"):].split("/")
                if len(seg) == 2:
                    tid, action = seg
                    if action == "message":
                        return _resp_result(post_task_message(tid, body, user))
                    if action == "accept":
                        return _resp_result(accept_task(tid, user))
                    if action == "close":
                        return _resp_result(close_task(tid, body, user))
            if path == "/api/data-upload-url":
                return _resp_result(data_upload_url(body, user))
            if path == "/api/start-run":
                return _resp_result(start_run(body))
            if path == "/api/cost-estimate":
                return _resp_result(create_estimate(body, user["username"], now))
            if path == "/api/cost-approval-request":
                return _resp_result(request_approval(body, user["username"], now))
            if path == "/api/cost-approval":
                return _resp_result(decide_approval(body, user, now))
            if path == "/api/finops-run":
                return _resp_result(finops_run(body))
            if path == "/api/batch-eval":
                return _resp(200, start_batch_eval())
            if path == "/api/insights-report":
                return _resp(200, start_insights_report())
            if path == "/api/native-rec":
                return _resp(200, start_native_recommendation(body.get("harness")))
            if path == "/api/native-rec-apply":
                return _resp(200, apply_native_recommendation(body.get("id", ""), body.get("harness")))
            if path == "/api/optimize":
                return _resp(200, enqueue_optimization(now, body.get("harness")))
            if path == "/api/optimize-apply":
                return _resp(200, apply_optimization(body.get("id", ""), now))
        return _resp(404, {"error": "not found", "path": path})
    except Exception as e:
        return _resp(500, {"error": str(e)})
