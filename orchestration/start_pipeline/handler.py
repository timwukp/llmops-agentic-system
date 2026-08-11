"""start-pipeline Lambda — single entry point for every trigger.

All four triggers (EventBridge Scheduler, GitHub Actions, Admin API, webhook)
and the conductor harness converge here. It mints the run_id, seeds the S3
manifest (the single source of truth every stage reads), records the run in
DynamoDB, emits PipelineStarted, and starts the Step Functions execution.

Env: DATA_BUCKET, RUNS_TABLE, EVENT_BUS, STATE_MACHINE_ARN.
"""
from __future__ import annotations

import datetime
import json
import os
import sys
import uuid

import boto3

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))  # repo layout
try:
    from pipeline.contracts import events as ev
except ImportError:  # Lambda bundle layout
    import events as ev  # type: ignore

# Defaults are overridable per-run via the trigger payload's "params".
DEFAULT_MODELS = {
    "teacher": "us.deepseek.r1-v1:0",
    "student": "Qwen/Qwen3-1.7B",
}
DEFAULT_PARAMS = {
    "dataset": "arc-agi-2",
    "sample_count": 2000,
    "keep_reasoning": True,          # reasoning distillation for ARC domain
    "max_iterations": 3,             # remediation loop budget
    "training_instance": "ml.g5.2xlarge",
    "inference_instance": "ml.g5.xlarge",
    "gates": {"relative_solve_rate": 0.80, "format_validity": 0.95},
}

#: Sentinel for "this run was not dispatched from a conductor task".
#
# The state machine closes the conductor's llmops-tasks row when a run reaches a
# terminal state, which means it reads $.task_id -- and a JSONPath that is not present
# raises States.Runtime, which NO Catch can intercept (the run then dies before it can
# self-close, strictly worse than the zombie task being fixed). Most runs have no task:
# schedule and webhook triggers never went through a human plan approval. So the field
# is always set, and the closer's ConditionExpression makes this value a no-op write.
NO_TASK = "none"


def _clients():
    region = os.environ.get("AWS_REGION", "us-east-1")
    return {
        "s3": boto3.client("s3", region_name=region),
        "ddb": boto3.resource("dynamodb", region_name=region),
        "sfn": boto3.client("stepfunctions", region_name=region),
        "events": boto3.client("events", region_name=region),
    }


def new_run_id() -> str:
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"run-{stamp}-{uuid.uuid4().hex[:8]}"


def _as_obj(value, what: str) -> dict:
    """Accept an object or a JSON string; anything else is an error, loudly.

    The conductor's launch_run arguments are authored by a language model, and live it
    passed `params` as a JSON string. `{**DEFAULT_PARAMS, **params}` on a str raises
    "TypeError: 'str' object is not a mapping" -- start-pipeline 500s and the agent is
    told only "did not return a run_id", so an approved plan silently never dispatches.
    Coercing here (rather than tightening the prompt) fixes it for every caller.

    Unparseable input raises: running with defaults would spend GPU money on
    parameters no human approved."""
    if value is None or value == "":
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{what} is a string but not valid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError(f"{what} must be a JSON object, got {type(parsed).__name__}")
        return parsed
    raise ValueError(f"{what} must be an object or a JSON string, "
                     f"got {type(value).__name__}")


#: The roles a manifest assigns a model to. `models` is a role->model-id map and
#: nothing else, which is what makes the conflict check below meaningful.
MODEL_ROLES = ("teacher", "student", "judge", "harness")

#: Per-role aliases a signed plan may use for the SAME fact, newest name first.
#
# One fact must have one name, but it already had four before this map existed, and
# three of them are in signed artifacts on S3 that cannot be rewritten: the console's
# estimate form posts `plan.teacher_model` (its STR_KEYS list, which is also what
# cost_model.py prices), the conductor prompt says "teacher/student models", and
# `plan.models.teacher` is what this function used to read. So the aliases are
# accepted on READ and normalised to one role name, rather than declared illegal --
# a plan a human signed last week must still dispatch as the model they approved.
ROLE_ALIASES = {
    "teacher": ("teacher", "teacher_model", "teacher_model_id"),
    "student": ("student", "student_model", "student_model_id"),
    "judge": ("judge", "judge_model", "judge_model_id"),
    "harness": ("harness", "harness_model", "harness_model_id"),
}

#: `plan.models` keys that describe WHERE an open-weight model came from, not WHICH
#: model fills a role. The conductor prompt tells the orchestrator to write exactly
#: this block for any open-weight model, so it arrives in real signed plans.
SUPPLY_CHAIN_KEYS = ("hf_repo", "revision", "files_sha256", "license", "mirror_uri",
                     "mirrored_at")


def _role_assignments(source: dict, where: str) -> dict:
    """Pull role -> model-id out of one plan/params dict, under any accepted alias.

    Reads both the flat form (`teacher_model`) and the nested `models` block, because
    both appear in artifacts that are already signed. A role named twice in the same
    source with two different ids is refused rather than resolved by precedence: one
    document contradicting itself is not a case where any reading is defensible.
    """
    nested = _as_obj(source.get("models"), f"{where}.models")
    found = {}
    for role, aliases in ROLE_ALIASES.items():
        seen = {}
        for alias in aliases:
            for scope, doc in ((where, source), (f"{where}.models", nested)):
                val = doc.get(alias)
                if val in (None, ""):
                    continue
                seen[f"{scope}.{alias}"] = str(val)
        distinct = sorted(set(seen.values()))
        if len(distinct) > 1:
            detail = ", ".join(f"{k}={v!r}" for k, v in sorted(seen.items()))
            raise ValueError(
                f"{where} names the {role} model more than once, with different ids: "
                f"{detail}. These are aliases for ONE fact, so this document "
                "contradicts itself -- fix it to name the model once.")
        if distinct:
            found[role] = distinct[0]
    unknown = sorted(set(nested) - set(sum(ROLE_ALIASES.values(), ()))
                     - set(SUPPLY_CHAIN_KEYS))
    if unknown:
        raise ValueError(
            f"{where}.models has keys that are neither a model role nor supply-chain "
            f"provenance: {unknown}. Roles are {list(MODEL_ROLES)}; provenance keys are "
            f"{list(SUPPLY_CHAIN_KEYS)}. Refusing rather than guessing, because a "
            "misspelled role silently becomes 'the plan is silent about the teacher', "
            "and the run then spends on a default no human approved.")
    # A mirrored model must fill a role. The conductor prompt has the orchestrator
    # write {hf_repo, revision, license, mirror_uri} for any open-weight model, and
    # that block is where the LICENCE was checked and the bytes were pinned. If it
    # names a repo no role uses, the run trains on a model whose licence nobody
    # cleared while the cleared one sits unused in the mirror -- and it does so
    # silently, because "the plan is silent about the student" is indistinguishable
    # from "there is no plan". Measured: a plan mirroring meta-llama/Llama-3.2-1B
    # produced manifest.student = Qwen/Qwen3-1.7B.
    repo = str(nested.get("hf_repo") or "")
    if repo and repo not in set(found.values()):
        raise ValueError(
            f"{where}.models mirrors {repo!r} but assigns it to no role. The "
            f"supply-chain block is where the licence was checked and the revision "
            f"pinned, so a repo that fills no role means the run would train on a "
            f"DIFFERENT model than the one that was cleared. Name the role explicitly "
            f"(e.g. \"student\": \"{repo}\") alongside the provenance keys.")
    return found


def _resolve_models(params, plan) -> dict:
    """Which models this run may use — with the SIGNED plan as the authority.

    Model consent is model-specific: a human approving a Fable-5 teacher at $0.05/1k
    output has not approved a DeepSeek-R1 one, and vice versa. So the plan a human
    signed outranks both the boilerplate defaults and anything the dispatching agent
    passes in `params`. `params` may only fill in roles the plan is silent about;
    where the two name the same role differently, the manifest is refused.

    Live failure this comes from: run 68cfa9c8's manifest carried
    ``models.teacher = us.deepseek.r1-v1:0`` (DEFAULT_MODELS boilerplate) while its
    signed plan said ``global.anthropic.claude-fable-5``. Both ids sat in one manifest,
    and the data-prep agent had to notice the contradiction and pick the signed one BY
    JUDGMENT — writing "top-level manifest 'models' field is stale boilerplate" into its
    own generated driver. It happened to choose correctly. That is the problem: the
    decision a signature exists to settle was delegated back to the model, and an agent
    resolving it the other way would have spent real money on a teacher no human
    approved, with a manifest that agreed with it.

    Refusing (rather than silently preferring the plan) because a disagreement here is
    never routine: it means the dispatch path and the approval path disagree about what
    was bought. Failing the dispatch costs one visible error; guessing costs an
    unapproved spend that looks authorized in every artifact afterward.

    THE ABOVE WAS ONLY HALF THE FIX, and the other half is why this reads aliases now.
    It matched `plan.models.teacher` exactly, while the console's estimate form posts
    `plan.teacher_model` (deploy/console/lambda_function.py STR_KEYS) and prices the run
    from it. So a plan signed through the console UI -- the only path a customer has --
    landed here with `models` absent, fell through to DEFAULT_MODELS, and produced a run
    PRICED as Fable-5 and EXECUTED on DeepSeek-R1, with every artifact agreeing. The
    same class of defect as the one above, reintroduced through a name rather than a
    precedence rule: a consent check that reads a different field name than the consent
    is written under is not a check.
    """
    plan_roles = _role_assignments(_as_obj(plan, "plan"), "plan")
    param_roles = _role_assignments(_as_obj(params, "params"), "params")
    conflicts = sorted(r for r in set(plan_roles) & set(param_roles)
                       if plan_roles[r] != param_roles[r])
    if conflicts:
        detail = ", ".join(f"{r}: plan={plan_roles[r]!r} vs params={param_roles[r]!r}"
                           for r in conflicts)
        raise ValueError(
            f"params.models contradicts the signed plan for {detail}. Model consent is "
            "model-specific, so the dispatched model must be the approved one: fix the "
            "dispatch to omit these roles, or seek a fresh acceptance for the plan you "
            "actually want to run.")
    # No precedence between the two beyond this point: every shared role has just been
    # proven to agree, so the merge order is unobservable. Both still outrank
    # DEFAULT_MODELS, which is boilerplate no human ever looked at.
    return {**DEFAULT_MODELS, **param_roles, **plan_roles}


#: Keys of a signed plan that are ABOUT the plan rather than settings for a stage.
#
# Everything else in a plan is a stage setting and reaches `params`. A denylist, not an
# allowlist, and that direction is the whole point: an allowlist omits the field nobody
# thought of, and the omission is invisible because a default silently takes its place --
# exactly how `pipeline_mode`, `training_instance` and `gates` came to be dropped. A new
# plan field a future orchestrator writes now arrives by default and has to be named here
# to be excluded, so the failure mode is a stage seeing a field it ignores, not a run
# quietly executing settings no human chose.
#
# `models` is excluded because _resolve_models already normalises it into `manifest.models`
# under one role name per model; carrying the raw block into params too would put a second,
# un-normalised copy of model consent in the manifest, which is the four-names defect.
PLAN_META_KEYS = frozenset({
    "models",              # resolved separately, into manifest.models
    "assumptions",         # prose for the human who signed
    "plan_summary",
    "cost_estimate_usd",   # what it was priced at, not an instruction to a stage
    "rate_card_as_of",
    "budget_usd",
    "created_at",
    "created_by",
})


def _plan_params(plan: dict) -> dict:
    """The stage settings a signed plan carries — flattened one level out of `data`.

    A plan's `data` block is nested ({source_uri, datasheet, customer_eval_uri,
    decontamination, ...}) while the prompts that consume it read flat params:
    data-prep's "audit" task reads `params.source_uri` and `params.customer_eval_uri`.
    Both halves were correct and never connected, so an audit run dispatched from a
    signed plan arrived with no data URI at all and the agent had to refuse or guess.

    Nested keys do NOT overwrite a same-named top-level key: the top level is the more
    specific statement, and a silent overwrite here would be the same defect one layer in.
    """
    out = {k: v for k, v in plan.items() if k not in PLAN_META_KEYS}
    data = plan.get("data")
    if isinstance(data, dict):
        for k, v in data.items():
            out.setdefault(k, v)
    return out


def _merge_params(params: dict, plan: dict) -> dict:
    """DEFAULT_PARAMS < params < signed plan, with plan/params conflicts refused.

    The precedence and the refusal are both taken from _resolve_models, because this is
    the same question about every other field it asks about models. Bug #9 fixed model
    consent, bug #20 fixed the NAME model consent is written under -- and both left the
    rest of the plan behind. Measured on a signed industrial-defect plan: a run priced on
    ml.p4d.24xlarge with 40000 samples and a {"map50": 0.75} gate executed on
    ml.g5.2xlarge with 2000 samples and ARC's relative_solve_rate gate, because
    seed_manifest read `plan` for models and nothing else. Every artifact agreed, and the
    variance report joined the estimate to the actual and read the underspend as success.

    DEFAULT_PARAMS keeps losing to both, which is also what makes the platform generic:
    `dataset: "arc-agi-2"` and the relative_solve_rate/format_validity gates are only
    harmful because a plan naming a YOLO dataset and a map50 gate could not displace them.
    """
    plan_params = _plan_params(plan)
    conflicts = sorted(k for k in set(plan_params) & set(params)
                       if plan_params[k] != params[k])
    if conflicts:
        detail = ", ".join(f"{k}: plan={plan_params[k]!r} vs params={params[k]!r}"
                           for k in conflicts)
        raise ValueError(
            f"params contradicts the signed plan for {detail}. What a human signed and "
            "what is being dispatched must be the same run: fix the dispatch to omit "
            "these keys, or seek a fresh acceptance for the plan you actually want. "
            "Refusing rather than picking a side, because a disagreement here means the "
            "approval path and the dispatch path describe different spends.")
    return {**DEFAULT_PARAMS, **params, **plan_params}


def seed_manifest(run_id: str, trigger_source: str, params, plan,
                  approval=None) -> dict:
    params = _as_obj(params, "params")
    plan = _as_obj(plan, "plan")
    approval = _as_obj(approval, "approval")
    merged = _merge_params(params or {}, plan or {})
    return {
        "run_id": run_id,
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "trigger_source": trigger_source,
        "iteration": 0,
        "models": _resolve_models(params, plan),
        "params": merged,
        "plan": plan or {},             # conductor-authored run plan, when present
        # The signed human-acceptance record, stored verbatim like the plan: a run
        # must carry its own proof of who approved it (and the budget_usd ceiling
        # the monitor stage will one day enforce). Absent for non-conductor runs.
        "approval": approval or {},
        "stages": {},
    }


def handler(event, context=None, clients=None):
    """event: {trigger_source, params?, plan?, approval?} — from any trigger or the conductor."""
    c = clients or _clients()
    bucket = os.environ["DATA_BUCKET"]

    run_id = new_run_id()
    trigger_source = str(event.get("trigger_source", "unknown"))
    manifest = seed_manifest(run_id, trigger_source, event.get("params"), event.get("plan"),
                             event.get("approval"))
    manifest_uri = f"s3://{bucket}/runs/{run_id}/manifest.json"

    c["s3"].put_object(
        Bucket=bucket, Key=f"runs/{run_id}/manifest.json",
        Body=json.dumps(manifest, indent=2, default=str).encode(),
        ContentType="application/json")

    c["ddb"].Table(os.environ["RUNS_TABLE"]).put_item(Item={
        "run_id": run_id,
        "status": "running",
        "created_at": manifest["created_at"],
        "trigger_source": trigger_source,
        "iteration": 0,
    })

    ev.emit_event(os.environ["EVENT_BUS"], ev.PIPELINE_STARTED,
                  {"run_id": run_id, "trigger_source": trigger_source},
                  client=c["events"])

    execution = c["sfn"].start_execution(
        stateMachineArn=os.environ["STATE_MACHINE_ARN"],
        name=run_id,
        # pipeline_mode rides in the execution input because the Choice state at
        # the top of the machine cannot read the manifest from S3 — "full" runs
        # every stage; "data_audit" is the conductor's cheap starter (audit the
        # customer's data, report, stop before any GPU is provisioned).
        input=json.dumps({"run_id": run_id, "manifest_uri": manifest_uri,
                          "iteration": 0,
                          "pipeline_mode": manifest["params"].get("pipeline_mode", "full"),
                          # The conductor task this run answers to, so the machine can
                          # close that task out when the run ends -- it cannot read the
                          # manifest from S3, same constraint as pipeline_mode above.
                          "task_id": manifest["approval"].get("task_id") or NO_TASK}))

    return {"run_id": run_id, "manifest_uri": manifest_uri,
            "execution_arn": execution["executionArn"]}
