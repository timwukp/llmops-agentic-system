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
import re
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
    # The deploy agent's smoke task registers the model in the Model Registry with an
    # "approval status per params" -- and no param of that name existed anywhere: not here,
    # not in the console's field map, not in a test. The prompt named an input that could
    # never arrive, so the status was whatever the agent decided that turn. Same defect
    # class as an emitted event with no rule, and nobody had ever reached the deploy stage
    # to find it (llmops-stage-events has zero deploy/smoke/teardown events, ever).
    #
    # PendingManualApproval rather than Approved because approving a model package is the
    # human decision this platform's whole KMS-signed spine exists to protect, and it must
    # not be a side effect of the pipeline that produced the model -- least of all in
    # deploy_only, where no gate was consulted at all. A run that earns Approved gets it
    # from a human reading the registry, not from an agent that just watched three canary
    # prompts return non-empty strings.
    "model_package_approval_status": "PendingManualApproval",
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


#: What each pipeline_mode a run may declare needs in params before it is legal to start.
#
# The state machine's entry Choice can route on the mode, but it cannot tell whether the
# inputs that mode depends on are present -- so a mode whose prerequisites are missing
# fails INSIDE an agent turn, after the run row, the manifest and the events all exist and
# claim a run is under way. eval_only is the mode where that matters most: it skips
# data-prep and finetune, so neither `distillation/curated.jsonl` (the eval agent's fallback
# prompt set) nor a finetune stage entry (where it normally reads the model artifact) exists
# in its manifest. Missing either one, the eval agent's only correct move is to escalate,
# which costs a run row and a human's attention to learn something knowable here for free.
#
# deploy_only is the same argument one stage further along, and it carries more weight there
# because this mode reaches Deploy without a gate verdict. Its legitimacy rests entirely on
# the dispatch being the approval, so the artifact has to be NAMED here: a mode that let the
# agent find the newest model.tar.gz in the bucket would be a deploy nobody chose the
# subject of, which is the objection EvalOnlyStopChoice raises against the far milder case of
# deploying off a re-judge. Refusing at dispatch is also the only place the refusal is free.
MODE_REQUIRED_PARAMS = {
    "eval_only": (
        ("model_artifact_uri",
         "the model.tar.gz to re-judge; there is no finetune stage in an eval_only run to "
         "produce one, so nothing downstream can infer it"),
        ("customer_eval_uri",
         "the prompt set to judge against; the 10% val split the eval agent falls back to "
         "is written by data-prep's curate task, which this mode skips"),
    ),
    "deploy_only": (
        ("model_artifact_uri",
         "the model.tar.gz to serve; this mode enters at Deploy, so there is no finetune "
         "stage in its manifest to read the artifact from and no gate verdict about it"),
    ),
    "lakehouse_ingest": (
        ("generator_seed",
         "the generator's seed. The same seed and scale give byte-identical source files on "
         "any machine, so the seed is what makes an ingest run reproducible; a default "
         "would make every ingest the same dataset without anyone having chosen that"),
        ("generator_scale",
         "the generator's size (small or demo); it fixes the row counts and the planted "
         "defects the Silver rules are held to"),
    ),
    "lakehouse": (
        ("lakehouse_dataset",
         "the Gold dataset to train on, as a lakehouse_ingest run (or the deploy smoke) "
         "named it. Training reads a pinned extract of that dataset's Gold snapshots, so "
         "there is no newest-dataset default: a model nobody chose the data for is not "
         "reproducible, whatever its extract's sha256 says"),
    ),
    "vision": (
        ("source_uri",
         "the labeled detection dataset (images + COCO annotations); vision has no "
         "teacher-generation task to synthesise a corpus from, so without this the "
         "data-prep agent has nothing to verify and nothing to train on"),
        ("customer_eval_uri",
         "the held-out labeled set the mAP gate is anchored to; scored against training "
         "data a detector's gate measures nothing"),
    ),
}

#: Model roles a mode must have EXPLICITLY named, by plan or by params, before it may start.
#
# Separate from MODE_REQUIRED_PARAMS because it asks a different question. That table asks
# whether a param is present; this one asks whether a role was CHOSEN, and for models those
# are not the same question -- DEFAULT_MODELS always fills a silent role, so `student` is
# never absent and a presence check on it can never fail. _resolve_models exists because a
# default standing in for an approval is this platform's most expensive recurring bug (run
# 68cfa9c8 executed on a teacher no human signed while every artifact agreed).
#
# deploy_only is where that bug would be most immediately fatal. The deploy agent merges the
# artifact's adapters into the base weights of params.student_model_id; dispatched silently
# it would get DEFAULT_MODELS' Qwen3-1.7B, and r6e's artifact -- the 12.2 GiB one this mode
# was built to serve -- is an 8B. Merging adapters into the wrong base does not fail at
# dispatch, it fails forty minutes and one GPU endpoint later, which is exactly the kind of
# lesson this mode exists to stop paying for.
MODE_REQUIRED_ROLES = {
    "lakehouse": (
        ("student",
         "the model the Gold extract trains. The extract is a customer-service corpus, not "
         "ARC, so the default student is a choice nobody made for this data"),
    ),
    "deploy_only": (
        ("student",
         "the base model this artifact's adapters merge into. DEFAULT_MODELS would supply "
         "one silently, and a base that nobody chose is not a base: an artifact merged onto "
         "the wrong weights is only discoverable after the endpoint is paid for"),
    ),
    "vision": (
        ("student",
         "the detector family to fine-tune (an Apache-licensed RT-DETR/D-FINE id). "
         "DEFAULT_MODELS would silently fill this role with Qwen3-1.7B -- an LLM -- and a "
         "detection run training a language model does not fail at dispatch, it fails a "
         "GPU-hour later inside the trainer"),
    ),
}

#: Params a mode must have EXPLICITLY chosen -- by plan or by params -- not inherited.
#
# The third question, after presence (MODE_REQUIRED_PARAMS) and role choice
# (MODE_REQUIRED_ROLES): was this VALUE picked by someone, or did DEFAULT_PARAMS fill it?
# For most modes inheritance is the feature -- the defaults ARE the LLM product. For
# vision they are a trap wearing a passing shape: DEFAULT_PARAMS carries
# `dataset: "arc-agi-2"` and `gates: {relative_solve_rate, format_validity}`, so a vision
# plan that forgets its gates block dispatches cleanly and the eval agent then judges a
# detector against an ARC solve-rate gate. A presence check can never catch this --
# merged params always contain `gates`, because the default put it there. Same argument
# as named_roles: by the time the value reaches the manifest, "chosen" and "defaulted"
# are indistinguishable, so dispatch is the only place the distinction exists for free.
MODE_REQUIRED_CHOSEN = {
    "lakehouse": (
        ("gates",
         "the gate block for this corpus. The inherited default is the ARC solve-rate gate, "
         "and a customer-service copilot judged against it passes or fails for reasons "
         "that have nothing to do with the data it was trained on"),
        ("dataset",
         "the dataset label; the inherited 'arc-agi-2' would stamp a lakehouse run's "
         "manifest and cost rows with a benchmark it never touched"),
    ),
    "vision": (
        ("gates",
         "the mAP gate block (e.g. {\"map50_uplift\": ..., \"format_validity\": ...}); "
         "without an explicit choice the run inherits DEFAULT_PARAMS' ARC solve-rate "
         "gates and the eval agent judges a detector against a language benchmark"),
        ("dataset",
         "the dataset label for this detection corpus; the inherited default is "
         "'arc-agi-2', which would stamp every manifest and cost row of a vision run "
         "with an LLM benchmark's name"),
    ),
}


def _check_mode_prerequisites(merged: dict, named_roles=(), chosen_keys=()) -> None:
    """Refuse a mode whose inputs are absent, before a run_id exists to blame it on.

    `named_roles` is the set of model roles a plan or params EXPLICITLY assigned, which is
    the only way to tell a chosen model from one DEFAULT_MODELS filled in -- by the time a
    role reaches the manifest the two are indistinguishable. See MODE_REQUIRED_ROLES.
    `chosen_keys` is the same distinction for params: the keys a plan or params actually
    set, before DEFAULT_PARAMS filled the rest. See MODE_REQUIRED_CHOSEN.
    """
    mode = merged.get("pipeline_mode", "full")
    required = MODE_REQUIRED_PARAMS.get(mode, ())
    if mode == "lakehouse" and lakehouse_builds(merged):
        # A build-then-train run names no existing dataset: it builds one, so it needs what an
        # ingest needs instead. Which of the two it is, and that it is not both, is
        # _check_lakehouse_values' question.
        required = MODE_REQUIRED_PARAMS["lakehouse_ingest"]
    missing = [(f"params.{k}", why) for k, why in required
               if not merged.get(k)]
    missing += [(f"an explicitly named {r} model", why)
                for r, why in MODE_REQUIRED_ROLES.get(mode, ())
                if r not in set(named_roles)]
    missing += [(f"an explicitly chosen params.{k}", why)
                for k, why in MODE_REQUIRED_CHOSEN.get(mode, ())
                if k not in set(chosen_keys)]
    if missing:
        detail = "; ".join(f"{what} ({why})" for what, why in missing)
        raise ValueError(
            f"pipeline_mode={mode!r} requires {detail}. Refusing at dispatch rather than "
            "starting a run that can only escalate: a half-specified mode produces a "
            "manifest, a run row and a PipelineStarted event that all describe work the "
            "pipeline cannot do.")


#: The generator's scales. pipeline/lakehouse/generate.py is not in this Lambda's bundle, so
#: the names are restated here and a test holds them equal to its SCALES.
LAKEHOUSE_SCALES = ("demo", "small")
#: The lakehouse dataset id: it becomes an S3 key segment in every Glue job's arguments, and
#: the console and the export refuse anything else, so dispatch refuses it first.
LAKEHOUSE_DATASET_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")


def lakehouse_builds(merged: dict) -> bool:
    """A lakehouse run that builds its own dataset first: it was given a generator seed or scale.

    One dispatch then goes from raw (generated) customer data to a judged model: Generate →
    Bronze → Silver → Gold → export → review → finetune → eval, in a single execution. The
    alternative, an existing dataset id, keeps today's train-only path.
    """
    return (merged.get("pipeline_mode") == "lakehouse"
            and (merged.get("generator_seed") not in (None, "")
                 or merged.get("generator_scale") not in (None, "")))


def _check_lakehouse_values(merged: dict) -> None:
    """Presence is MODE_REQUIRED_PARAMS' question; this is whether the values are usable.

    Each one is formatted into a Glue job argument by the state machine, where a bad value
    does not fail at dispatch but as a FAILED job a stage later, after a run row, a manifest
    and a PipelineStarted event already describe work that cannot happen.
    """
    mode = merged.get("pipeline_mode", "full")
    if mode == "lakehouse" and lakehouse_builds(merged) and merged.get("lakehouse_dataset"):
        raise ValueError(
            "a lakehouse run takes EITHER params.lakehouse_dataset (train on a dataset an ingest "
            "built) OR params.generator_seed + params.generator_scale (build one, then train), "
            "not both: with both, which data trained the model is not a fact the dispatch fixed")
    if mode == "lakehouse_ingest" or (mode == "lakehouse" and lakehouse_builds(merged)):
        seed = merged.get("generator_seed")
        if isinstance(seed, bool) or not str(seed).isdigit():
            raise ValueError(f"params.generator_seed must be a non-negative integer, got {seed!r}")
        if merged.get("generator_scale") not in LAKEHOUSE_SCALES:
            raise ValueError(f"params.generator_scale must be one of {LAKEHOUSE_SCALES}, "
                             f"got {merged.get('generator_scale')!r}")
    if (mode == "lakehouse" and not lakehouse_builds(merged)
            and not LAKEHOUSE_DATASET_ID.match(str(merged.get("lakehouse_dataset")))):
        raise ValueError("params.lakehouse_dataset must match ^[a-z0-9][a-z0-9-]{0,79}$, got "
                         f"{merged.get('lakehouse_dataset')!r}")


#: What an eval_only run may name as the model to judge: a model.tar.gz a run already produced, or
#: an untrained base model pinned to a commit (a BASELINE: the same prompts and scorers on the model
#: before fine-tuning, which is what makes a fine-tuned score mean anything). The revision is a
#: full 40-hex sha because a branch name ("main") re-resolves on every download, and a baseline
#: that can silently become a different model cannot be compared to anything.
EVAL_ARTIFACT_URI = re.compile(
    r"^(s3://\S+|hf://[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*@[0-9a-f]{40})$")


def _check_eval_artifact(merged: dict) -> None:
    if merged.get("pipeline_mode") != "eval_only":
        return
    uri = str(merged.get("model_artifact_uri") or "")
    if not EVAL_ARTIFACT_URI.match(uri):
        raise ValueError(
            "params.model_artifact_uri must be an s3:// model artifact or an untrained base model "
            "pinned as hf://<org>/<repo>@<40-hex commit sha>, got " + repr(uri) + ". A branch or "
            "tag re-resolves on every download, so a baseline named by one is not reproducible")


def lakehouse_input(params: dict, run_id: str, bucket: str):
    """The execution input's `lakehouse` object, or None outside the lakehouse modes.

    It rides in the input for the same reason pipeline_mode does: the state machine builds
    the Glue jobs' arguments with States.Format and cannot read the manifest from S3. An
    ingest run names its dataset after itself (the run id, lowercased: Glue, the console and
    the export all take lowercase ids), so two ingests can never write one landing prefix.
    """
    mode = params.get("pipeline_mode", "full")
    if mode == "lakehouse_ingest" or lakehouse_builds(params):
        return {"dataset": run_id.lower(), "data_bucket": bucket,
                "seed": str(int(params["generator_seed"])), "scale": params["generator_scale"]}
    if mode == "lakehouse":
        return {"dataset": params["lakehouse_dataset"], "data_bucket": bucket}
    return None


def seed_manifest(run_id: str, trigger_source: str, params, plan,
                  approval=None) -> dict:
    params = _as_obj(params, "params")
    plan = _as_obj(plan, "plan")
    approval = _as_obj(approval, "approval")
    merged = _merge_params(params or {}, plan or {})
    _check_mode_prerequisites(
        merged,
        set(_role_assignments(plan or {}, "plan"))
        | set(_role_assignments(params or {}, "params")),
        set(params or {}) | set(_plan_params(plan or {})))
    _check_lakehouse_values(merged)
    _check_eval_artifact(merged)
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
    # In the manifest as well as the execution input: the Glue states write nothing to the
    # manifest, so without this an ingest run's only record of the dataset it built is the
    # convention that names the dataset after the run.
    manifest["lakehouse"] = lakehouse_input(manifest["params"], run_id, bucket)
    if lakehouse_builds(manifest["params"]):
        # A build-then-train run's agents read params.lakehouse_dataset exactly as a train-only
        # run's do (data-prep's lakehouse_review, the console's Data tab), so the dataset it is
        # about to build is named there too: one field, one meaning, in both shapes of the mode.
        manifest["params"]["lakehouse_dataset"] = manifest["lakehouse"]["dataset"]

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
        # On the ROW, not only in the manifest, because the run table is what every reader
        # who is not already holding an S3 client sees: the console's run list, finops
        # reconciliation, and anyone asking the question this project could not answer for
        # itself -- of 38 runs, which ones actually served a model? A deploy_only rehearsal
        # ends in exactly the same terminal state as a full run that passed the gate and
        # deployed, and without this field the two are indistinguishable from the row. That
        # is not a reporting nicety: a rehearsal misread as a gated production deploy is a
        # claim that the quality gate was met, which is the one claim this platform has
        # never yet been able to make.
        "pipeline_mode": manifest["params"].get("pipeline_mode", "full"),
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
        # customer's data, report, stop before any GPU is provisioned);
        # "eval_only" re-judges an artifact an earlier run already paid for
        # (params.model_artifact_uri) and stops at the gate verdict without
        # deploying; "deploy_only" enters at Deploy against a named artifact to
        # rehearse the serving path this machine has never once executed — see
        # MODE_REQUIRED_PARAMS and MODE_REQUIRED_ROLES for what each must be given.
        input=json.dumps({"run_id": run_id, "manifest_uri": manifest_uri,
                          "iteration": 0,
                          "pipeline_mode": manifest["params"].get("pipeline_mode", "full"),
                          # The conductor task this run answers to, so the machine can
                          # close that task out when the run ends -- it cannot read the
                          # manifest from S3, same constraint as pipeline_mode above.
                          "task_id": manifest["approval"].get("task_id") or NO_TASK,
                          # The Glue states' per-run arguments (dataset, bucket, generator
                          # seed/scale); null outside the two lakehouse modes.
                          "lakehouse": manifest["lakehouse"]}))

    return {"run_id": run_id, "manifest_uri": manifest_uri,
            "execution_arn": execution["executionArn"]}
