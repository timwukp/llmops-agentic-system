#!/usr/bin/env python3
"""07_lambdas.py — package and deploy the 7 spine Lambdas + the state machine.

Each Lambda bundle = its handler.py + the contracts (events.py, report.py,
manifest.schema.json) vendored flat so the `except ImportError` fallback path
resolves. Roles come from SSM (01_iam.py); env vars from SSM (03_storage.py).
State machine is created/updated from orchestration/state_machine.asl.json with
${HarnessDriverArn} and ${EventBusName} substituted.

--only selects among ALL targets, Lambdas and non-Lambdas alike (state_machine,
resume_rule, triage_rule); a bare run still deploys everything.

Usage:
  python deploy/07_lambdas.py --region us-east-1 --dry-run
  python deploy/07_lambdas.py --region us-east-1
  python deploy/07_lambdas.py --region us-east-1 --only driver
  python deploy/07_lambdas.py --region us-east-1 --only state_machine   # ASL only
  python deploy/07_lambdas.py --region us-east-1 --only triage_rule     # bus rule only
"""
import argparse
import io
import json
import pathlib
import re
import sys
import time
import zipfile

import boto3

REPO = pathlib.Path(__file__).resolve().parent.parent
CONTRACTS = REPO / "pipeline" / "contracts"

# The event vocabulary is imported, not re-spelled: a rule whose `source` or detail-type
# disagrees with the emitter's by one character matches nothing, and a rule that matches
# nothing is indistinguishable from a healthy one in the console. Same reason the driver
# imports these constants instead of writing the strings inline.
sys.path.insert(0, str(REPO))
from pipeline.contracts import events as ev  # noqa: E402 — needs REPO on sys.path

# `env_keys` here is ADDITIVE ONLY: values a handler reads through a defaulted
# `os.environ.get(...)` that we nonetheless want pinned in this environment. Everything a
# handler REQUIRES (`os.environ[...]`) is derived from its source by required_env_keys(),
# because the hand-maintained list disagreed with the driver for eight days -- it omitted
# ACTUALS_TABLE, and the daily cost audit's terminal tools died on KeyError inside an
# agent turn where nothing was watching. Do not re-add required keys here; the guard test
# fails if the two ever describe different sets.
LAMBDAS = {
    "driver": {
        "fn": "llmops-harness-driver",
        "src": REPO / "orchestration" / "harness_driver" / "handler.py",
        "role_param": "/llmops/iam/lambda_driver_arn",
        "timeout": 900, "memory": 512,
        # Additive: START_FN is a defaulted read (launch_run's dispatch target), pinned
        # here so the target is a deploy-time decision rather than a code default.
        "env_keys": ["START_FN"],
        # This function is an EventBridge target, so its deploy is checked against the
        # rules live on this bus (see live_bus_translator_gap). The other six are
        # invoked by Step Functions, the console or a schedule -- never by a bus rule --
        # so they have no envelope to translate.
        "bus_delivered": "llmops-pipeline",
    },
    "start": {
        "fn": "llmops-start-pipeline",
        "src": REPO / "orchestration" / "start_pipeline" / "handler.py",
        "role_param": "/llmops/iam/lambda_start_arn",
        "timeout": 60, "memory": 256,
        "env_keys": [],
    },
    "resume": {
        "fn": "llmops-resume-pipeline",
        "src": REPO / "orchestration" / "resume_pipeline" / "handler.py",
        "role_param": "/llmops/iam/lambda_resume_arn",
        "timeout": 60, "memory": 256,
        "env_keys": [],
    },
    # The dead-driver wake. The driver's turn handoff is an ASYNC self-invoke; Lambda
    # dropped one on 2026-08-08 (AsyncEventsDropped=1) and run 68cfa9c8 sat dead nine
    # hours with its token parked and nothing whose job it was to notice. The driver now
    # heartbeats the run row every turn; this function re-invokes it from the stamped
    # payload when a running run's beat goes stale — and it is also what makes
    # AgentCore's 8-hour session maxLifetime survivable on 8-12h measurement stages.
    "resurrector": {
        "fn": "llmops-resurrector",
        "src": REPO / "orchestration" / "resurrector" / "handler.py",
        "role_param": "/llmops/iam/lambda_resurrector_arn",
        # 120 s: one table scan over tens of rows + at most a handful of async invokes.
        "timeout": 120, "memory": 256,
        "env_keys": [],
    },
    "webhook": {
        "fn": "llmops-webhook",
        "src": REPO / "orchestration" / "webhook" / "handler.py",
        "role_param": "/llmops/iam/lambda_webhook_arn",
        "timeout": 30, "memory": 256,
        "env_keys": [],
    },
    # The auditor's trigger. 08_triggers.py already schedules llmops-finops-daily
    # against this function name, so omitting it here leaves a live EventBridge
    # schedule pointing at a function that does not exist -- a daily failure that
    # surfaces only in the scheduler's own metrics, never in the dashboard.
    "finops": {
        "fn": "llmops-finops-reconcile",
        "src": REPO / "orchestration" / "finops_reconcile" / "handler.py",
        # Its OWN role, not the driver's: iam/lambda_roles.json scopes
        # finops_reconcile to Query/Scan + PutItem + InvokeFunction + Publish, which
        # is strictly narrower than the driver's. Reusing the driver role works and
        # is what a first pass reaches for -- it also hands the auditor every
        # permission the thing it audits has.
        "role_param": "/llmops/iam/lambda_finops_reconcile_arn",
        # 60 s is enough: it lists runs and hands off asynchronously. The auditor's
        # own multi-minute work happens in the harness, not here.
        "timeout": 60, "memory": 256,
        # Additive: PROJECT is a defaulted read, pinned so the ledger partition key is
        # a deploy-time decision. RUNS_TABLE is NOT here -- the auditor reads estimates
        # and actuals, never the runs table.
        "env_keys": ["PROJECT"],
    },
    # The orphan hunter. Its trigger is created by 08_triggers.py, so the same rule the
    # finops entry above records applies verbatim: omit this and the deploy leaves a live
    # EventBridge schedule pointing at a function that does not exist.
    #
    # In the state machine for `health` and `report`, OUT of it for `sweep`: a sweep looks
    # for endpoints left behind by OTHER runs, including runs that crashed and therefore
    # never reached any state that could have looked. A run-scoped agent cannot answer for
    # other runs -- the same shape argument that put the auditor outside the spine.
    "monitor_sweep": {
        "fn": "llmops-monitor-sweep",
        "src": REPO / "orchestration" / "monitor_sweep" / "handler.py",
        "role_param": "/llmops/iam/lambda_monitor_sweep_arn",
        # 60 s: it builds one payload and hands off asynchronously. The sweep's own
        # multi-minute CloudWatch work happens in the harness, not here.
        "timeout": 60, "memory": 256,
        # Additive: PROJECT is a defaulted read, pinned for the same reason as finops.
        "env_keys": ["PROJECT"],
    },
}

STATE_MACHINE_NAME = "llmops-pipeline"


def bundle(src: pathlib.Path) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(src, "handler.py")
        z.write(CONTRACTS / "events.py", "events.py")
        z.write(CONTRACTS / "report.py", "report.py")
        z.write(CONTRACTS / "manifest.schema.json", "manifest.schema.json")
        # launch_run servicing + approval verification, shared verbatim with the
        # console (its deploy.sh vendors the same file) so the two dispatch paths
        # cannot drift. Vendored into every bundle: only the driver imports it,
        # but a uniform bundle is one less special case in this function.
        z.write(REPO / "orchestration" / "conductor_tools.py", "conductor_tools.py")
    return buf.getvalue()


#: Env vars a handler reads through ``os.environ.get(...)`` WITH a default, so their
#: absence is a documented choice rather than a crash. Everything else a handler reads
#: must be passed at deploy time -- see required_env_keys.
OPTIONAL_ENV = frozenset({
    "AWS_REGION",        # Lambda sets this itself; never ours to pass
    "STALE_MINUTES",     # resurrector tuning, defaulted in code
    "RESURRECTIONS_MAX",
    "IDLE_HOURS",        # monitor_sweep tuning, defaulted in code
})

#: os.environ["KEY"] -- a read with NO default, i.e. a hard requirement.
_REQUIRED_ENV_RE = re.compile(r'os\.environ\[\s*"([A-Z][A-Z0-9_]*)"\s*\]')


def required_env_keys(src: pathlib.Path) -> set[str]:
    """Every env var `src` reads WITHOUT a default — derived, never hand-listed.

    The bug this replaces: the driver's env_keys named six variables while
    handler.py read seven. `handle_finops_tool` reads ACTUALS_TABLE with no default,
    the driver role has granted PutItem on that table since the statement was written
    FOR this call (`CostActualsWrite` in iam/lambda_roles.json), and the deploy simply
    never passed the name. So the daily cost audit's terminal tools raised
    `KeyError: 'ACTUALS_TABLE'` -- measured live on 2026-08-01 (3x) and again on
    2026-08-09 (3x, once per Lambda async retry), each retry burning a fresh AgentCore
    turn re-deciding the same period. `llmops-cost-actuals` holds ZERO `#finding#` rows
    for the whole life of the system: not one variance the auditor found was ever
    recorded.

    Why derived rather than a longer hand-maintained list: a list is a second copy of a
    fact the handler already states, and it was wrong for eight days without anything
    noticing. It cannot be checked by eye either -- the crash needs a finops turn to
    reach a terminal tool, which happens once a day inside an agent, so the failure
    surfaces as a missing dashboard row and nothing else. Parsing the source makes the
    handler the single source of truth, and the guard test fails the build the moment a
    new `os.environ[...]` lands without a deploy-time value.

    Deliberately a regex over the source, not an import: importing these handlers pulls
    in boto3 clients at module scope, and a deploy script must not need the runtime's
    dependencies to know what the runtime needs.
    """
    return set(_REQUIRED_ENV_RE.findall(src.read_text())) - OPTIONAL_ENV


def env_values(ssm, region, account, keys, extra):
    bucket = ssm.get_parameter(Name="/llmops/storage/bucket")["Parameter"]["Value"]
    base = {
        "RUNS_TABLE": "llmops-pipeline-runs",
        "EVENTS_TABLE": "llmops-stage-events",
        "EVENT_BUS": "llmops-pipeline",
        "DATA_BUCKET": bucket,
        "LLMOPS_SNS_TOPIC": f"arn:aws:sns:{region}:{account}:llmops-escalations",
        "STATE_MACHINE_ARN": f"arn:aws:states:{region}:{account}:stateMachine:{STATE_MACHINE_NAME}",
        "WEBHOOK_SECRET_ID": "llmops/webhook",
        "START_PIPELINE_FN": "llmops-start-pipeline",
        "START_FN": "llmops-start-pipeline",   # driver's launch_run dispatch target
        "DRIVER_FN": "llmops-harness-driver",
        "ESTIMATES_TABLE": "llmops-cost-estimates",
        "ACTUALS_TABLE": "llmops-cost-actuals",
        "PROJECT": "llmops-agentic-system",
    }
    base.update(extra or {})
    unknown = sorted(set(keys) - set(base))
    if unknown:
        # A key the handler requires that this function has no VALUE for. Refusing
        # beats passing the rest: with the variable absent the handler crashes at the
        # line that reads it, which for the finops tools is inside an agent turn a day
        # after the deploy reported success.
        raise KeyError(
            f"{unknown} is read by a handler with os.environ[...] but env_values has no "
            "value for it. Add it to `base` (and to the role in iam/lambda_roles.json "
            "if it names a resource) -- do not drop it from the handler's requirements.")
    return {k: base[k] for k in keys}


def env_keys_for(cfg) -> list[str]:
    """The env this Lambda gets: what its handler requires, plus declared extras.

    `env_keys` in LAMBDAS is now additive-only -- for values a handler reads through a
    defaulted `.get()` but that we still want set explicitly in this environment. The
    REQUIREMENTS come from the handler source, so the two can no longer disagree.
    """
    return sorted(required_env_keys(cfg["src"]) | set(cfg.get("env_keys") or []))


def deploy_lambda(lam, ssm, region, account, key, cfg, dry, events=None):
    keys = env_keys_for(cfg)
    if dry:
        return {"lambda": cfg["fn"], "would": "create/update", "env_keys": keys}
    # FIRST, before the role lookup and long before update_function_code: a driver that
    # cannot read a live rule's envelope is broken from the instant the code lands, and
    # the failure is invisible from here -- PutEvents succeeds, the rule matches, and the
    # invocation raises KeyError inside the Lambda. Refuse rather than warn, for the same
    # reason config_subst refuses an unresolved token: the deploy reports success either
    # way, so a warning is read by nobody.
    if events is not None and cfg.get("bus_delivered"):
        gaps = live_bus_translator_gap(events, cfg["src"].read_text(), cfg["fn"],
                                       cfg["bus_delivered"])
        blocking = [g for g in gaps if "unchecked" not in g]
        if blocking:
            raise SystemExit(
                f"refusing to deploy {cfg['fn']}: live ENABLED rules on the "
                f"{cfg['bus_delivered']} bus deliver events this handler cannot read — "
                f"{json.dumps(blocking, indent=2)}\n"
                "Each such event reaches the function as a raw EventBridge envelope and "
                "dies on KeyError before any handler branch runs. Restore the translator "
                "(or give the rule's target an InputTransformer) and redeploy.")
        if gaps:
            print(json.dumps({"warning": "bus/translator agreement NOT verified",
                              "detail": gaps}), file=sys.stderr)
    role_arn = ssm.get_parameter(Name=cfg["role_param"])["Parameter"]["Value"]
    env = env_values(ssm, region, account, keys, None)
    code = bundle(cfg["src"])
    try:
        lam.get_function(FunctionName=cfg["fn"])
        lam.update_function_code(FunctionName=cfg["fn"], ZipFile=code)
        waiter = lam.get_waiter("function_updated_v2")
        waiter.wait(FunctionName=cfg["fn"])
        # Role goes in the UPDATE too, not just the create. Without it a role change
        # in LAMBDAS above applies only to functions that do not exist yet: every
        # re-run reports "updated" while the live function keeps whatever role it was
        # born with. That is silent in both directions -- a tightened role never takes
        # effect, and nothing ever says so.
        lam.update_function_configuration(
            FunctionName=cfg["fn"], Role=role_arn,
            Timeout=cfg["timeout"], MemorySize=cfg["memory"],
            Environment={"Variables": env})
        action = "updated"
    except lam.exceptions.ResourceNotFoundException:
        lam.create_function(
            FunctionName=cfg["fn"], Runtime="python3.12", Role=role_arn,
            Handler="handler.handler", Code={"ZipFile": code},
            Timeout=cfg["timeout"], MemorySize=cfg["memory"],
            Environment={"Variables": env},
            Tags={"project": "llmops-agentic-system"})
        action = "created"
    # The driver's turn handoff is an async self-invoke, so its async delivery policy
    # is part of the pipeline's correctness, not tuning. Defaults are 2 retries over up
    # to 6 HOURS of event age -- a continuation redelivered hours later would resume a
    # turn whose session and context are long gone, next to whatever the resurrector
    # already restarted. Pin retries at 2 but bound the age at 5 minutes: past that,
    # the heartbeat is stale and the resurrector (PR #67) owns the wake. Explicit for
    # every function, so the policy is in code review rather than in whatever the
    # account default happens to be.
    lam.put_function_event_invoke_config(
        FunctionName=cfg["fn"], MaximumRetryAttempts=2, MaximumEventAgeInSeconds=300)
    return {"lambda": cfg["fn"], "action": action}


def deploy_state_machine(sfn, ssm, region, account, dry, sleep=time.sleep):
    asl = (REPO / "orchestration" / "state_machine.asl.json").read_text()
    driver_arn = f"arn:aws:lambda:{region}:{account}:function:llmops-harness-driver"
    asl = asl.replace("${HarnessDriverArn}", driver_arn)
    asl = asl.replace("${EventBusName}", "llmops-pipeline")
    if dry:
        # json.loads only proves it is JSON. ASL rejects plenty of valid JSON -- an
        # unsupported field, a bad JSONPath, an unknown SDK integration -- and does so
        # at UpdateStateMachine time, i.e. in the middle of a real deploy. The
        # ValidateStateMachineDefinition API is read-only and creates nothing, so the
        # dry run can make the claim it was already printing.
        json.loads(asl)
        try:
            checked = sfn.validate_state_machine_definition(definition=asl,
                                                            type="STANDARD")
        except Exception as exc:  # no credentials / no network: say so, do not claim
            return {"state_machine": STATE_MACHINE_NAME, "would": "create/update",
                    "asl": "json-parses; ASL NOT validated",
                    "validator_unreachable": f"{type(exc).__name__}: {exc}"}
        diags = [f"[{d['severity']}] {d['code']} {d.get('location', '')}: {d['message']}"
                 for d in checked.get("diagnostics", [])]
        return {"state_machine": STATE_MACHINE_NAME, "would": "create/update",
                "asl": checked["result"], "diagnostics": diags}
    # The state machine's own role, published by 01_iam.py from iam/sfn_execution_role.json.
    # The legacy /llmops/iam/sfn_arn name is still read as a fallback for accounts
    # deployed before the role was declared in-repo; the start role is the last resort
    # and is wrong (it cannot write the runs table), so it fails loudly at MarkRunFailed
    # rather than silently granting the state machine the wrong identity.
    for param in ("/llmops/iam/sfn_execution_arn", "/llmops/iam/sfn_arn",
                  "/llmops/iam/lambda_start_arn"):
        try:
            role_arn = ssm.get_parameter(Name=param)["Parameter"]["Value"]
            break
        except ssm.exceptions.ParameterNotFound:
            continue
    else:
        raise RuntimeError("no state machine role in SSM — run deploy/01_iam.py first")
    sm_arn = f"arn:aws:states:{region}:{account}:stateMachine:{STATE_MACHINE_NAME}"
    try:
        sfn.describe_state_machine(stateMachineArn=sm_arn)
        sfn.update_state_machine(stateMachineArn=sm_arn, definition=asl, roleArn=role_arn)
        action = "updated"
    except sfn.exceptions.StateMachineDoesNotExist:
        sfn.create_state_machine(name=STATE_MACHINE_NAME, definition=asl,
                                 roleArn=role_arn, type="STANDARD",
                                 tags=[{"key": "project", "value": "llmops-agentic-system"}])
        action = "created"
    landed = confirm_state_machine_landed(sfn, sm_arn, asl, sleep=sleep)
    return {"state_machine": STATE_MACHINE_NAME, "action": action, **landed}


def state_machine_drift(sent: str, live: str) -> list:
    """How the LIVE definition differs from the one just sent, state by state.

    Semantic, not byte-wise: Step Functions happens to return the definition verbatim
    today (measured), but a formatting-only difference is not a deploy failure, and a
    check that calls it one gets switched off by the third person it wakes. What matters
    is whether the machine will DO what the ASL says -- so the comparison is on parsed
    JSON, and it names the states rather than dumping a 26 KB diff, because the useful
    sentence is "EvalGenerate is absent live", not "definitions differ".
    """
    try:
        want, got = json.loads(sent), json.loads(live)
    except json.JSONDecodeError as exc:
        return [{"problem": f"live definition is not JSON: {exc}"}]
    if want == got:
        return []
    drift = []
    ws, gs = want.get("States", {}), got.get("States", {})
    for name in sorted(set(ws) - set(gs)):
        drift.append({"state": name, "problem": "in the ASL being deployed, ABSENT live"})
    for name in sorted(set(gs) - set(ws)):
        drift.append({"state": name, "problem": "live, but absent from the ASL deployed"})
    for name in sorted(set(ws) & set(gs)):
        if ws[name] == gs[name]:
            continue
        fields = sorted(k for k in set(ws[name]) | set(gs[name])
                        if ws[name].get(k) != gs[name].get(k))
        drift.append({"state": name, "problem": f"differs on {fields}"})
    for key in sorted((set(want) | set(got)) - {"States"}):
        if want.get(key) != got.get(key):
            drift.append({"top_level": key, "problem": "differs"})
    if not drift:
        # Equality failed and nothing above localised it. Reporting clean here would turn
        # an unexplained difference into a pass, which is the failure this whole function
        # exists to prevent -- so say that instead.
        drift.append({"problem": "definitions differ in a way this check cannot localise"})
    return drift


def confirm_state_machine_landed(sfn, sm_arn: str, asl: str, attempts: int = 5,
                                 sleep=time.sleep) -> dict:
    """Read the definition back, and refuse to call the deploy done until it matches.

    `update_state_machine` returning 200 is not evidence that the machine changed, and
    this project has already paid for believing otherwise: `update_function_configuration`
    was called without `Role`, so every run reported "updated" while the live function
    kept the role it was born with -- silent in both directions. The state machine had the
    same gap. On 2026-08-03 the live definition turned out to be missing `EvalGenerate`
    ENTIRELY: merged 2026-08-02 as the whole point of #57 (the quality gate read
    `evaluation/report.json` and nothing wrote it), suite green, ASL simply never
    deployed. What caught it was a human reading the live definition by hand, a day later
    and only because an unrelated timeout change sent them looking.

    This is the argument `live_bus_translator_gap` already makes for rules -- a tree
    cannot know what is live, only the live resource knows -- applied to the definition.

    Polled rather than read once, because UpdateStateMachine is eventually consistent:
    AWS documents that executions started immediately afterwards may still use the
    PREVIOUS definition. A single read would fail on deploys that were in fact fine, and
    a check that cries wolf is a check that gets deleted -- the same eventual consistency
    that bit the push tool's ref read. It converges on the first attempt in practice; the
    retries are what make a drift report trustworthy when one does come out.
    """
    last = [{"problem": "never read"}]
    for i in range(attempts):
        try:
            live = sfn.describe_state_machine(stateMachineArn=sm_arn)
        except Exception as exc:  # noqa: BLE001 — cannot confirm; must not claim confirmed
            return {"definition_confirmed": False,
                    "read_back_unreachable": f"{type(exc).__name__}: {exc}"}
        last = state_machine_drift(asl, live["definition"])
        if not last:
            return {"definition_confirmed": True,
                    "revision_id": live.get("revisionId"),
                    "states_live": len(json.loads(live["definition"]).get("States", {}))}
        if i < attempts - 1:
            sleep(2 ** i)  # 1,2,4,8s — for eventual consistency, not a cure for drift
    raise SystemExit(
        f"{STATE_MACHINE_NAME}: the deploy call succeeded but the LIVE definition still "
        f"disagrees with this tree's ASL after {attempts} reads — "
        f"{json.dumps(last, indent=2)}\n"
        "Every execution will run the live definition, not the one in this tree. Do not "
        "record this deploy as done: nothing else in this repo compares the two, so this "
        "message is the only place the disagreement is visible.")


def live_bus_translator_gap(events, src: str, fn: str, bus: str) -> list:
    """Detail-types a LIVE rule delivers to `fn` that the source about to ship can't read.

    This exists because the driver was deployed WITHOUT the EscalatedToHuman translator
    while `llmops-escalation-triage` was ENABLED and pointed at it. Every escalation
    then reached the driver as a raw EventBridge envelope and died on
    `KeyError: 'run_id'` -- the same channel #59 built, broken from the other end.

    The offline guards could not catch it, and the reason is the point of this function.
    They compare EVENTS_NEEDING_A_RULE against the rules THIS TREE's deployer builds, so
    a branch carrying neither the declaration, nor the rule, nor the translator is
    perfectly self-consistent and green -- which is exactly what the branch that
    overwrote the driver was. A tree cannot know which rules are live on the bus; only
    the bus knows. So the comparison has to be live-rules vs the bytes about to ship,
    made at deploy time, before update_function_code.

    A rule whose target has an InputTransformer needs no Python translator: EventBridge
    reshapes the event before the driver sees it. That is read from the live target
    rather than assumed, because the two are alternatives and either one alone suffices.
    """
    gaps = []
    try:
        rules = events.list_rules(EventBusName=bus).get("Rules", [])
    except Exception as exc:  # noqa: BLE001 — no creds/no bus: report, never claim clean
        return [{"unchecked": f"{type(exc).__name__}: {exc}"}]
    for rule in rules:
        if rule.get("State") != "ENABLED":
            continue
        targets = events.list_targets_by_rule(
            Rule=rule["Name"], EventBusName=bus).get("Targets", [])
        mine = [t for t in targets if t.get("Arn", "").endswith(f":function:{fn}")]
        if not mine:
            continue
        pattern = json.loads(rule.get("EventPattern") or "{}")
        for detail_type in pattern.get("detail-type") or []:
            needed = ev.BUS_DELIVERY_TRANSLATORS.get(detail_type)
            if not needed:
                # A live rule delivering a detail-type nothing declares a translator for
                # is itself the defect: the driver will receive an envelope it has no
                # branch for. Naming it is the whole job of this check.
                gaps.append({"rule": rule["Name"], "detail_type": detail_type,
                             "problem": "no translator declared in BUS_DELIVERY_TRANSLATORS"})
                continue
            if any(t.get("InputTransformer") or t.get("Input") for t in mine):
                continue  # EventBridge reshapes it; the Python translator is not needed
            # `def <name>(`, not a bare substring: a negative control that renamed only the
            # DEFINITION left the call site behind, and the bare-substring form passed --
            # on a source that would raise NameError on the first escalation. A call to a
            # function nobody defines is worse than no call at all, so the check has to
            # look for the definition.
            if f"def {needed}(" not in src:
                gaps.append({"rule": rule["Name"], "detail_type": detail_type,
                             "problem": f"{needed}() is absent from the handler being "
                                        "deployed, and the rule has no InputTransformer"})
    return gaps


def ensure_resume_rule(events, lam, region, account, dry):
    """EventBridge rule: SageMaker Training Job State Change -> resume lambda.
    Default bus (SageMaker service events land there, not on custom buses)."""
    rule = "llmops-sagemaker-job-state"
    pattern = {
        "source": ["aws.sagemaker"],
        "detail-type": ["SageMaker Training Job State Change"],
        "detail": {"TrainingJobStatus": ["Completed", "Failed", "Stopped"]},
    }
    if dry:
        return {"rule": rule, "would": "put_rule + target + permission"}
    events.put_rule(Name=rule, EventPattern=json.dumps(pattern), State="ENABLED",
                    Description="Resume llmops pipeline when a training job finishes")
    fn_arn = f"arn:aws:lambda:{region}:{account}:function:llmops-resume-pipeline"
    events.put_targets(Rule=rule, Targets=[{"Id": "resume", "Arn": fn_arn}])
    try:
        lam.add_permission(FunctionName="llmops-resume-pipeline",
                           StatementId="eventbridge-sagemaker-state",
                           Action="lambda:InvokeFunction",
                           Principal="events.amazonaws.com",
                           SourceArn=f"arn:aws:events:{region}:{account}:rule/{rule}")
    except lam.exceptions.ResourceConflictException:
        pass  # permission already exists
    return {"rule": rule, "action": "ensured"}


def ensure_triage_rule(events, lam, region, account, dry):
    """EventBridge rule: EscalatedToHuman -> harness driver, as a conductor triage.

    The llmops-pipeline bus carried ZERO rules from Phase 1 to Phase 5 while
    EscalatedToHuman was emitted from three places, documented as routing to the
    conductor, and serviced by a driver branch (#54's page_human fix) that nothing could
    ever reach. Both halves of the channel existed; the wire between them did not.

    The CUSTOM bus, not the default one -- unlike ensure_resume_rule, whose SageMaker
    service events land on the default bus and cannot be moved. Omitting EventBusName
    here would create a rule that is live, healthy, and matches nothing forever.

    The pattern excludes stage="orchestrator" so a triage cannot trigger a triage. That
    is not hypothetical: handle_page_human emitted EscalatedToHuman until this change,
    so escalate -> triage -> page -> triage would have looped, each lap paying for a
    real harness turn. page_human now emits OwnerPaged, and this exclusion is the second
    line of defence for the next tool that reaches for the escalation vocabulary. Note
    the coupling it creates: `anything-but` does not match an event with no `stage` key
    at all, so an emitter that omits stage would be dropped silently -- a test asserts
    every emitter in the repo carries one.
    """
    rule = "llmops-escalation-triage"
    pattern = {
        "source": [ev.EVENT_SOURCE],
        "detail-type": [ev.ESCALATED_TO_HUMAN],
        "detail": {"stage": [{"anything-but": ["orchestrator"]}]},
    }
    if dry:
        return {"rule": rule, "would": "put_rule + target + permission",
                "bus": "llmops-pipeline", "pattern": pattern}
    events.put_rule(Name=rule, EventPattern=json.dumps(pattern), State="ENABLED",
                    EventBusName="llmops-pipeline",
                    Description="Route escalations to the conductor for first-line triage")
    fn_arn = f"arn:aws:lambda:{region}:{account}:function:llmops-harness-driver"
    # No InputTransformer: the driver translates the envelope in Python
    # (triage_event_from_bus). A transformer referencing a path an event lacks drops it
    # silently, and the two emitters of this detail-type carry different key sets.
    events.put_targets(Rule=rule, EventBusName="llmops-pipeline",
                       Targets=[{"Id": "triage", "Arn": fn_arn}])
    try:
        lam.add_permission(FunctionName="llmops-harness-driver",
                           StatementId="eventbridge-escalation-triage",
                           Action="lambda:InvokeFunction",
                           Principal="events.amazonaws.com",
                           SourceArn=f"arn:aws:events:{region}:{account}:rule/"
                                     f"llmops-pipeline/{rule}")
    except lam.exceptions.ResourceConflictException:
        pass  # permission already exists
    return {"rule": rule, "action": "ensured", "bus": "llmops-pipeline"}


# --only selects among ALL of this script's targets, not just the Lambdas. The state
# machine and the resume rule used to deploy unconditionally on every run, which made
# --only the opposite of what it says: `--only driver` shipped the driver AND the ASL,
# and there was no way to ship the ASL alone. Both directions bit. The ASL change that
# added MarkRunDone could not be deployed without also shipping a driver whose redeploy
# is deliberately held back pending an IAM widen; and a driver-only redeploy silently
# published whatever the working tree's ASL happened to say. A targeted deploy has to
# mean what it claims, because the whole reason to reach for --only is blast radius.
NON_LAMBDA_TARGETS = ("state_machine", "resume_rule", "triage_rule")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", required=True)
    ap.add_argument("--only", action="append",
                    choices=list(LAMBDAS) + list(NON_LAMBDA_TARGETS))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    lam = boto3.client("lambda", region_name=args.region)
    ssm = boto3.client("ssm", region_name=args.region)
    sfn = boto3.client("stepfunctions", region_name=args.region)
    events = boto3.client("events", region_name=args.region)
    account = "" if args.dry_run else boto3.client("sts", region_name=args.region) \
        .get_caller_identity()["Account"]

    targets = args.only or list(LAMBDAS) + list(NON_LAMBDA_TARGETS)
    results = [deploy_lambda(lam, ssm, args.region, account, k, LAMBDAS[k], args.dry_run,
                             events)
               for k in targets if k in LAMBDAS]
    if "state_machine" in targets:
        results.append(deploy_state_machine(sfn, ssm, args.region, account, args.dry_run))
    if "resume_rule" in targets:
        results.append(ensure_resume_rule(events, lam, args.region, account, args.dry_run))
    if "triage_rule" in targets:
        results.append(ensure_triage_rule(events, lam, args.region, account, args.dry_run))
    print(json.dumps({"results": results, "targets": targets,
                      "dry_run": args.dry_run}, indent=2, default=str))


if __name__ == "__main__":
    sys.exit(main())
