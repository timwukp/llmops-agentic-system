#!/usr/bin/env python3
"""07_lambdas.py — package and deploy the 6 spine Lambdas + the state machine.

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

LAMBDAS = {
    "driver": {
        "fn": "llmops-harness-driver",
        "src": REPO / "orchestration" / "harness_driver" / "handler.py",
        "role_param": "/llmops/iam/lambda_driver_arn",
        "timeout": 900, "memory": 512,
        "env_keys": ["RUNS_TABLE", "EVENTS_TABLE", "EVENT_BUS", "LLMOPS_SNS_TOPIC", "DATA_BUCKET",
                     "START_FN"],
        # This function is an EventBridge target, so its deploy is checked against the
        # rules live on this bus (see live_bus_translator_gap). The other five are
        # invoked by Step Functions, the console or a schedule -- never by a bus rule --
        # so they have no envelope to translate.
        "bus_delivered": "llmops-pipeline",
    },
    "start": {
        "fn": "llmops-start-pipeline",
        "src": REPO / "orchestration" / "start_pipeline" / "handler.py",
        "role_param": "/llmops/iam/lambda_start_arn",
        "timeout": 60, "memory": 256,
        "env_keys": ["RUNS_TABLE", "EVENT_BUS", "DATA_BUCKET", "STATE_MACHINE_ARN"],
    },
    "resume": {
        "fn": "llmops-resume-pipeline",
        "src": REPO / "orchestration" / "resume_pipeline" / "handler.py",
        "role_param": "/llmops/iam/lambda_resume_arn",
        "timeout": 60, "memory": 256,
        "env_keys": ["RUNS_TABLE", "EVENT_BUS"],
    },
    "webhook": {
        "fn": "llmops-webhook",
        "src": REPO / "orchestration" / "webhook" / "handler.py",
        "role_param": "/llmops/iam/lambda_webhook_arn",
        "timeout": 30, "memory": 256,
        "env_keys": ["WEBHOOK_SECRET_ID", "START_PIPELINE_FN"],
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
        "env_keys": ["RUNS_TABLE", "DATA_BUCKET", "DRIVER_FN",
                     "ESTIMATES_TABLE", "ACTUALS_TABLE", "PROJECT"],
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
        "env_keys": ["EVENTS_TABLE", "DATA_BUCKET", "DRIVER_FN", "PROJECT"],
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
    return {k: base[k] for k in keys}


def deploy_lambda(lam, ssm, region, account, key, cfg, dry, events=None):
    if dry:
        return {"lambda": cfg["fn"], "would": "create/update", "env_keys": cfg["env_keys"]}
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
    env = env_values(ssm, region, account, cfg["env_keys"], None)
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
    return {"lambda": cfg["fn"], "action": action}


def deploy_state_machine(sfn, ssm, region, account, dry):
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
    return {"state_machine": STATE_MACHINE_NAME, "action": action}


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
