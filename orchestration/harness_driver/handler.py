"""harness-driver Lambda — the bridge between Step Functions and a worker harness.

One invocation = one harness task. Streams InvokeHarness, services the
inline-function protocol (toolUse ⇄ toolResult), verifies claimed outputs,
publishes canonical state, and settles the Step Functions task token.

Production patterns baked in (from Tim's live CI agents — real failures, not theory):
  - BotoConfig(read_timeout=870, retries=0): default 60s read timeout kills long
    streams; botocore auto-retry would silently re-run a whole agent turn.
  - safe stream loop: streams die mid-turn (urllib3 timeout, reset, runtimeClientError);
    salvage and retry the SAME session once before failing.
  - missing-signal re-ask: models narrate completion but skip the structured call;
    re-invoke the same session once demanding stage_complete.
  - empty-but-valid: stage_complete with outputs=[] is a legitimate success.
  - trust-but-verify: head_object every claimed S3 output; the driver (not the
    agent) writes the canonical report.

Env: RUNS_TABLE, EVENTS_TABLE, EVENT_BUS, LLMOPS_SNS_TOPIC, DATA_BUCKET.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Optional

import boto3
from botocore.config import Config as BotoConfig
from boto3.dynamodb.conditions import Key

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))  # repo layout
try:
    from pipeline.contracts import events as ev
    from pipeline.contracts.report import normalize_stage_complete, write_run_report
    from pipeline.contracts.task_tokens import is_task_gone
    from orchestration import conductor_tools
except ImportError:  # Lambda bundle layout (contracts vendored alongside)
    import events as ev  # type: ignore
    from report import normalize_stage_complete, write_run_report  # type: ignore
    from task_tokens import is_task_gone  # type: ignore
    import conductor_tools  # type: ignore

# (stage, task) -> EventBridge detail-type. eval/gate resolved dynamically.
STAGE_EVENT_MAP = {
    ("data-prep", "generate"): ev.DATASET_GENERATED,
    ("data-prep", "curate"): ev.DATASET_CURATED,
    ("finetune", "analyze"): ev.MODEL_TRAINED,
    # ModelEvaluated was declared in the event vocabulary and emitted by NOTHING --
    # the same absence as the evaluate task itself, from the other side.
    ("eval", "evaluate"): ev.MODEL_EVALUATED,
    # score is the follow-on state after a launch-and-release inference job; when
    # evaluate finishes synchronously instead, score just verifies the report, and
    # a second ModelEvaluated for the same iteration is harmless bus noise.
    ("eval", "score"): ev.MODEL_EVALUATED,
    ("deploy", "deploy"): ev.MODEL_DEPLOYED,
    ("deploy", "smoke"): ev.SMOKE_TEST_PASSED,
    ("deploy", "teardown"): ev.ENDPOINT_DELETED,
}

_AGENTCORE_CFG = BotoConfig(read_timeout=870, connect_timeout=30,
                            retries={"max_attempts": 0})

#: The conductor's own run_id for a triage. NOT the escalated run's id, which is the
#: obvious choice and is wrong twice over:
#:
#:   * take_directive() is keyed on event["run_id"], and the checkpoint branch is its
#:     only caller. A triage invoked under the subject's id would pop the subject's own
#:     parked verdict -- the one the conductor is about to write -- and hand it to the
#:     conductor as an instruction from a human. The conductor would then be answering
#:     itself.
#:   * handle_escalate() and handle_job_launched() update the runs table keyed on
#:     event["run_id"]. A triage that escalated in turn would overwrite the subject
#:     run's status and job_name with the triage's.
#:
#: The subject run reaches the conductor as params.escalation.run_id, which is what the
#: orchestrator prompt's triage clause already tells it to read.
TRIAGE_STAGE = "orchestrator"
TRIAGE_TASK = "triage"


def _is_triage(event) -> bool:
    return event.get("stage") == TRIAGE_STAGE and event.get("task") == TRIAGE_TASK


def triage_subject(event) -> str:
    """The run a triage is ABOUT, read from the invocation rather than from the agent.

    The comment above TRIAGE_STAGE says the subject "reaches the conductor as
    params.escalation.run_id", and triage_event_from_bus is the only writer of that key.
    Nothing was reading it. Every consumer instead took the subject from the model's own
    tool arguments and fell back to `event["run_id"]` -- which on a triage is
    `triage-<subject>`, the one id that must never be used as the subject. So a
    conductor that omitted run_id, or echoed back the id it was invoked under, addressed
    the record to itself.

    Measured over every HumanPaged row in llmops-stage-events (12 rows, full scan,
    ScannedCount == Count so nothing was paged over): 3 are filed under a `triage-` id --
    86ab8a14, c8b13faa and b56281da, each an ARC-2 lineage run that died with its
    scientific work complete. An owner opening the stuck run's timeline sees no page
    there, because the page is in the conductor's timeline. The alert fired and the
    audit trail points at the wrong run, which is the failure mode #72's backstop was
    built to end and could not see: the backstop only asks WHETHER a page happened.

    Why the fallback existed at all: `run_id` is not in page_human's `required` list
    (agents/orchestrator/harness.json declares only situation + recommendation), so a
    page legitimately arrives without one. The fix is not to require it -- the driver
    already knows the answer and the model's copy is redundant. Prefer the event, and
    treat the agent's value as usable only when the event carries no subject (the
    console chat path, where there is no escalation envelope and `event["run_id"]` IS
    the subject).
    """
    subject = str(((event.get("params") or {}).get("escalation") or {}).get("run_id")
                  or "")
    if subject:
        return subject
    # Not a bus triage: no escalation envelope, so event["run_id"] is the real subject.
    # Deliberately NOT reachable on the triage path -- triage_event_from_bus raises on an
    # escalation with no run_id, so a triage always has one.
    return "" if _is_triage(event) else str(event.get("run_id") or "")


#: A triage return status that means the escalation was actually ANSWERED -- a verdict
#: reached a listening agent, a run was relaunched, the owner was told, or the turn is
#: not over yet. Anything else means the conductor finished and the stuck run is still
#: stuck with nobody informed, which is what _backstop_page exists to prevent.
#:
#: `escalated` counts because handle_escalate publishes to the escalation SNS topic, so a
#: human IS reached. `self_reinvoked_between_turns` counts because the work continues in
#: the next invocation; paging there would page once per Lambda boundary.
TRIAGE_ANSWERED = ("resolved", "dispatched", "paged", "escalated",
                   "self_reinvoked_between_turns")


def dispatch_is_possible(event) -> bool:
    """Can ``launch_run`` actually succeed on THIS invocation?

    service_launch_run refuses without a KMS-verifiable approval record, taken from
    ``args["approval"]`` or from ``expected["approval"]``. On the driver path neither
    exists:

      * ``expected`` comes from ``params.approval_context``, and NOTHING in the repo
        writes that key -- triage_event_from_bus builds params with `escalation` alone.
        It has been a read with no writer since the branch was added.
      * the agent cannot supply it either: ``approval`` is not among the properties
        ``launch_run`` declares in agents/orchestrator/harness.json, so a conductor that
        tried would have the field dropped before the driver ever saw it.

    So launch_run on the driver path always came back "no approval record present" --
    while the undeliverable-verdict rejection was telling the conductor to use it. That
    is a dead end dressed as a choice, the exact phrase this file already uses for the
    previous incarnation of the same defect: a rejection naming a path that cannot work
    leaves triage nowhere to go, and the turn ends in prose.

    Live, 2026-08-05 to 2026-08-08: 4 of the 9 runs whose escalation was triaged got
    ZERO HumanPaged stage-event -- the conductor was sent to launch_run, refused, and
    ran out of moves. The other 5 paged, which is why the channel looked half-working.
    """
    ctx = (event.get("params") or {}).get("approval_context") or {}
    return bool(ctx.get("approval"))


def triage_event_from_bus(record: dict, bucket: str) -> dict:
    """Turn an EventBridge EscalatedToHuman event into a driver invocation.

    Done HERE, in Python, rather than in an EventBridge InputTransformer, for one
    reason: a transformer that references a JSON path the event does not carry drops the
    event silently -- no invocation, no failure, nothing on any metric that names this
    pipeline. That is the exact class of defect this task exists to fix (an emitted
    event with no listener), and an InputTransformer would let it back in through a
    channel with even less visibility, since the ASL's own EscalateFail entry carries a
    different key set from the driver's handle_escalate entry. A dict built in a
    function is testable offline against every real emitter's payload.

    manifest_uri is derived from the SUBJECT run, not the triage: the conductor's job is
    to read the stuck run's manifest, and there is no manifest for a triage.
    """
    detail = record.get("detail") or {}
    subject = str(detail.get("run_id") or "")
    if not subject:
        # An escalation with no run_id names nothing to triage. Better to fail loudly
        # here than to invoke a conductor that will read an empty manifest URI.
        raise ValueError(f"EscalatedToHuman with no run_id in detail: {detail!r}")
    return {
        "run_id": f"triage-{subject}",
        "stage": TRIAGE_STAGE,
        "task": TRIAGE_TASK,
        "harness_id": "llmops_orchestrator",
        "manifest_uri": f"s3://{bucket}/runs/{subject}/manifest.json",
        "params": {"escalation": {
            "run_id": subject,
            "stage": detail.get("stage", ""),
            "reason": detail.get("reason", ""),
            "iteration": detail.get("iteration", 0),
            "escalated_at": detail.get("emitted_at", ""),
        }},
    }


#: AgentCore reclaims a runtime session at `maxLifetime` — 28800s (8h), a HARD cap that
#: cannot be reset by activity (measured; see the account's warm-start study). A stage
#: that runs 8-12h therefore outlives its own session, and letting the platform reclaim
#: one mid-request is the documented anti-pattern: the invoke fails in a way nothing in
#: the driver distinguishes from a real error. Roll to a fresh session BEFORE the cap
#: instead. 7h leaves an hour of margin for a turn already in flight (840s) plus the
#: continuation chain behind it.
SESSION_MAX_LIFETIME_S = 28800
SESSION_ROLLOVER_S = 25200


def session_id(run_id: str, stage: str, task: str, epoch: int = 0) -> str:
    """Deterministic, >=33 chars (AgentCore minimum). Same task -> same session.

    `epoch` rolls the identity forward when the previous session approaches
    AgentCore's 8h maxLifetime. Determinism is what makes both resumption paths work
    — a self-reinvoke and a resurrector wake both recompute the same id from the same
    (run, stage, task, epoch) — so the epoch has to travel in the event, never be
    derived from a clock at call time.
    """
    base = f"{run_id}-{stage}-{task}" + (f"-e{epoch}" if epoch else "")
    if len(base) >= 33:
        return base[:100]
    return (base + "-" + hashlib.sha256(base.encode()).hexdigest())[:64]


def _clients():
    region = os.environ.get("AWS_REGION", "us-east-1")
    return {
        "agentcore": boto3.client("bedrock-agentcore", region_name=region, config=_AGENTCORE_CFG),
        "ddb": boto3.resource("dynamodb", region_name=region),
        "s3": boto3.client("s3", region_name=region),
        "sfn": boto3.client("stepfunctions", region_name=region),
        "sns": boto3.client("sns", region_name=region),
        "events": boto3.client("events", region_name=region),
        "lambda": boto3.client("lambda", region_name=region),
    }


def _kms(c: dict):
    """KMS client for approval-signature verification, created on first use.

    Not in _clients() because only the launch_run path needs it, and the unit-test
    fake client dicts predate it — a lazy accessor means they keep working and a
    test can inject c["kms"] to stub verification.
    """
    if "kms" not in c:
        region = os.environ.get("AWS_REGION", "us-east-1")
        c["kms"] = boto3.client("kms", region_name=region)
    return c["kms"]


_arn_cache: dict = {}


def _resolve_harness_arn(harness_id: str) -> str:
    """InvokeHarness requires the full ARN (live-verified: harnessId is not an
    accepted parameter). The state machine passes logical names (llmops_data_prep);
    the full suffixed id lives in SSM /llmops/harness/<agent> (set by 05_harnesses.py).
    Env override HARNESS_ARN_<NAME> short-circuits SSM (also keeps unit tests offline)."""
    if harness_id.startswith("arn:"):
        return harness_id
    if harness_id in _arn_cache:
        return _arn_cache[harness_id]
    env_key = f"HARNESS_ARN_{harness_id.upper()}"
    if os.environ.get(env_key):
        _arn_cache[harness_id] = os.environ[env_key]
        return _arn_cache[harness_id]
    region = os.environ.get("AWS_REGION", "us-east-1")
    agent = harness_id.removeprefix("llmops_").replace("_", "-")
    ssm = boto3.client("ssm", region_name=region)
    full_id = ssm.get_parameter(Name=f"/llmops/harness/{agent}")["Parameter"]["Value"]
    account = boto3.client("sts", region_name=region).get_caller_identity()["Account"]
    _arn_cache[harness_id] = f"arn:aws:bedrock-agentcore:{region}:{account}:harness/{full_id}"
    return _arn_cache[harness_id]


#: Every tool name _run_stage's dispatch has a branch for -- i.e. every tool THIS
#: DRIVER is the answerer for. Membership here is what licenses servicing a call that
#: arrived with a non-tool_use stop_reason (see the override in the turn loop), so it
#: has to stay exactly the set of branches below: a name in this set with no branch
#: falls through to {"status": "unsupported"}, and a branch missing from this set is
#: silently discarded when it rides with end_turn -- the bug this exists to fix.
#:
#: Kept as a literal rather than read from the harness config, because the question
#: being asked is "can I service this?", which is a fact about this file, not about the
#: control plane. Reading it remotely would answer a slightly different question
#: (what the config DECLARES) over a network call that can fail, and a fallback for
#: that failure is a second copy of this same list. It does not go stale unnoticed:
#: test_the_serviced_tool_set_matches_the_dispatch_branches derives the branch names
#: out of this module's source and asserts set equality, so adding a branch without
#: adding the name -- or renaming either -- turns that guard red.
SERVICED_TOOLS = frozenset({"stage_complete", "checkpoint", "escalate_human",
                            "job_launched", "publish_cost_report", "update_rate_card",
                            "flag_variance", "launch_run", "resolve_escalation",
                            "page_human", "write_report"})


def _invoke(ac, harness_id: str, sess: str, messages: list, qualifier: Optional[str],
            model: Optional[dict] = None):
    """`messages` is the full messages list -- a resume needs two entries (assistant
    toolUse echo + user toolResult), so this can no longer wrap a single content
    block. See _tool_result_content.

    `model` is InvokeHarness's per-invocation model override ("If specified, overrides
    the harness default"), and it is how _maybe_failover_model switches models: the
    override is scoped to THIS CALL, so nothing outside this session can observe it.
    Omitted when None -- the harness serves its declared model, which is what every
    turn outside a vendor 5xx burst wants.
    """
    kwargs = dict(harnessArn=_resolve_harness_arn(harness_id), runtimeSessionId=sess,
                  messages=messages)
    if qualifier:
        kwargs["qualifier"] = qualifier
    if model:
        kwargs["model"] = model
    return ac.invoke_harness(**kwargs)


def _ack_terminal(c, event, sess, tu, payload, effect: str) -> None:
    """Answer a serviced inline function whose EFFECT HAS ALREADY LANDED.

    Every terminal branch below has the same shape: do the irreversible thing (settle
    the task token, park the job, record the escalation, deliver the page, dispatch the
    run), then send the agent a toolResult so its session sees an answer, then return a
    status the state machine reads. The ack is the only one of those three that can
    fail, and it is the only one that does not matter -- nothing downstream reads it,
    and the session is finished with either way.

    Before this helper, any exception here propagated out of _run_stage and turned a
    stage that had genuinely finished -- token already settled, artifacts already in S3
    -- into a failed invocation, leaving the state machine with a settled token AND a
    Lambda error for the same stage. Every _invoke can fail on throttling or a 5xx
    alone, and the override above adds a shape with no production record either way:
    servicing a call that arrived with stopReason=end_turn now sends a resume for a turn
    the runtime has already closed. _tool_result_content echoes the toolUse alongside
    the result, so that resume carries its own matching call and MAY be accepted -- but
    it is untested, and the failure mode if it is not (see the rejection quoted in
    _tool_result_content) would land exactly here, one state after the effect.

    Which is the point: a courtesy message must not be able to un-complete a stage,
    whatever the reason it fails. Nothing downstream reads the ack.
    """
    try:
        _invoke(c["agentcore"], event["harness_id"], sess,
                _tool_result_content(tu, payload), event.get("qualifier"),
                c.get("_model_override"))
    except Exception as exc:  # noqa: BLE001 -- the effect is already irreversible
        print(f"[driver] ack for {tu.get('name')} was rejected "
              f"({type(exc).__name__}: {exc}); {effect} already landed and nothing "
              "downstream reads the ack, so this turn is finished as a success")


#: How much Lambda wall must remain when _drain abandons a stream. Enough for the
#: self-reinvoke call, the heartbeat write, and the runtime's own teardown — measured
#: driver overhead is single-digit seconds; 45 gives margin without wasting a minute
#: of every deadline-cut turn.
#:
#: This margin ALSO has to be reachable, which for three days it was not. It is larger
#: than the gap the boto read_timeout leaves (900 wall - 870 read = 30s remaining, i.e.
#: 15s short of the 45 demanded here), so on the one path that did fire an exception the
#: handoff was already too late. See _drain's docstring for the window that made both
#: escape hatches unreachable, and _stream_watchdog for the alarm that closes it.
DRAIN_DEADLINE_MARGIN_MS = 45_000

#: _drain's error marker for a deadline cut. Checked by name at the call site, so it
#: must stay distinguishable from a real stream death: a real death burns the one
#: stream-salvage retry; a deadline cut must instead hand the turn to the next
#: invocation, where the full 900s is available again.
DEADLINE_CUT = "LambdaDeadlineApproaching"


class _StreamWatchdogFired(Exception):
    """Raised INTO the stream reader by the watchdog alarm. See _stream_watchdog."""


def _stream_watchdog(remaining_ms):
    """Context manager that guarantees _drain gets control back before the wall.

    `out_of_wall` alone could not: it is evaluated once per chunk, inside the `for event
    in resp["stream"]` loop, so it needs a chunk to arrive in order to notice that no
    chunk is arriving. That is the bug (#26), and it is structural rather than a race:

      * the boto read_timeout (870s) restarts on every read, so after a chunk at elapsed
        `t` the next timeout is due at `t + 870` — past the 900s wall for any t > 30;
      * `out_of_wall` fires only at 855s elapsed, and only if a chunk lands after that.

    So a last chunk anywhere in the OPEN interval (30, 855) seconds — 825 of the 900 —
    left BOTH escape hatches unreachable, and the runtime hard-killed the invocation.
    Live: run-20260810T182807Z-e394ada9's curate turn, `REPORT RequestId: 925119d7-...
    Duration: 900000.00 ms ... Status: timeout`, with zero application log lines. The
    agent had already produced curated.jsonl, generated.jsonl and stats.json and had
    already called stage_complete; the call died with the invocation, and the stage failed
    MissingStageComplete with its own outputs sitting in S3. The agent's recorded cause
    was literally true: "is complete and was verified in my previous turn".

    SIGALRM rather than a reader thread with a timeout: the blocking read is inside
    botocore's urllib3 socket, so there is nothing to poll and no future to cancel — only
    a signal can interrupt it. Lambda's Python runtime serves the invocation on the main
    thread, which is where signal handlers must be installed and where they are delivered,
    so this works here for the same reason it would not work off the main thread. Setting
    it up is guarded anyway (ValueError) so an off-main-thread caller degrades to the old
    per-chunk behaviour instead of crashing — a watchdog that cannot arm must not take the
    turn down with it.

    The alarm raises INTO the reader, so `_drain`'s own `except` sees it like any other
    stream death; _drain then relabels it DEADLINE_CUT, which is the distinction the call
    site depends on (a real death burns the one salvage retry; a deadline cut hands the
    turn to the next invocation with the full 900s).
    """
    import contextlib
    import signal

    @contextlib.contextmanager
    def _cm():
        # int() truncates toward zero, so a 1.4s budget arms at 1s, never 0 (= cancel).
        secs = int((remaining_ms() - DRAIN_DEADLINE_MARGIN_MS) / 1000) if remaining_ms \
            else 0
        if secs < 1:
            # Either no context (unit tests) or already inside the margin. Nothing to
            # arm: the loop-top _out_of_time() check owns the too-late case.
            yield
            return

        def _fire(signum, frame):
            raise _StreamWatchdogFired(
                f"no stream progress with {DRAIN_DEADLINE_MARGIN_MS}ms of wall left")

        try:
            prev = signal.signal(signal.SIGALRM, _fire)
        except ValueError:  # not the main thread — degrade, do not fail
            yield
            return
        signal.alarm(secs)
        try:
            yield
        finally:
            # Cancel BEFORE restoring, not after: the reverse order leaves a live alarm
            # armed against whatever handler was there before, which for the default
            # disposition terminates the process.
            signal.alarm(0)
            signal.signal(signal.SIGALRM, prev)

    return _cm()


def _drain(resp, out_of_wall=None, remaining_ms=None) -> dict:
    """Consume the stream; return {text, tool_use, stop_reason, error}.

    `out_of_wall` is the in-turn deadline check. The boto read_timeout (870s) bounds
    the gap BETWEEN chunks, not the stream's total life — a reasoning model that
    trickles a chunk every few seconds can stream for longer than the Lambda's 900s
    wall, and did: run 68cfa9c8's generate turn hit the wall mid-stream at
    03:39:49Z, the runtime killed the invocation with the harness turn unanswered,
    and the async retry replayed a continuation whose session state no longer
    matched — MissingStageComplete three minutes later. The between-turns
    _out_of_time() check cannot see any of this; only the stream reader can.

    `remaining_ms` arms the watchdog that makes the above ACTUALLY reachable when the
    stream goes quiet instead of slow — the case `out_of_wall` structurally cannot see,
    because it is only evaluated when a chunk arrives. See _stream_watchdog.
    """
    text, tool_use, stop_reason, error = [], None, None, None
    try:
        with _stream_watchdog(remaining_ms):
            for event in resp.get("stream", []):
                if out_of_wall is not None and out_of_wall():
                    error = DEADLINE_CUT
                    break
                if "contentBlockDelta" in event:
                    delta = event["contentBlockDelta"].get("delta", {})
                    if "text" in delta:
                        text.append(delta["text"])
                if "contentBlockStart" in event:
                    start = event["contentBlockStart"].get("start", {})
                    if "toolUse" in start:
                        tool_use = {"toolUseId": start["toolUse"].get("toolUseId"),
                                    "name": start["toolUse"].get("name"), "input": ""}
                if tool_use is not None and "contentBlockDelta" in event:
                    delta = event["contentBlockDelta"].get("delta", {})
                    if "toolUse" in delta:
                        tool_use["input"] += delta["toolUse"].get("input", "")
                if "messageStop" in event:
                    stop_reason = event["messageStop"].get("stopReason")
    except _StreamWatchdogFired as exc:
        # A quiet stream at the wall is a DEADLINE CUT, not a stream death: the harness
        # turn is still running server-side and the next invocation gets the full 900s.
        # Labelling it as a death would instead burn the one salvage retry on a turn that
        # never failed, leaving a REAL death later in the same stage unprotected.
        error = DEADLINE_CUT
        print(f"[driver] stream watchdog fired: {exc}")
    except Exception as exc:  # stream death is expected in production
        error = f"{type(exc).__name__}: {exc}"
    if tool_use is not None and isinstance(tool_use.get("input"), str):
        try:
            tool_use["input"] = json.loads(tool_use["input"] or "{}")
        except json.JSONDecodeError:
            tool_use["input"] = {"_raw": tool_use["input"]}
    return {"text": "".join(text), "tool_use": tool_use,
            "stop_reason": stop_reason, "error": error}


def _user_text(text: str) -> list:
    """A plain user turn as a messages list."""
    return [{"role": "user", "content": [{"text": text}]}]


def _tool_result_content(tool_use: dict, payload: dict) -> list:
    """Resume a paused inline function: echo the toolUse, THEN answer it.

    Two messages, per the InvokeHarness pause/resume contract -- an assistant message
    replaying the emitted toolUse, then a user message with the matching toolResult.
    A lone toolResult is rejected ("The number of toolResult blocks at
    messages.N.content exceeds the number of toolUse blocks of previous turn"),
    because the history handed to the runtime contains no call to answer. Found live
    on the console's dispatch path, where it broke four sessions in a row; here the
    same bug hid behind the stream-retry, a rejected result looking like stream death.

    JSON travels in a TEXT block: `json` blocks come back as
    "runtimeClientError ... content_type=<json_> | unsupported type".

    A RECOVERED call (parse_typed_call) has no toolUseId, because the model typed the
    call instead of making one and the runtime therefore never minted an id. There is no
    pending call to answer in that session, so the echo-plus-result pair is not just
    unnecessary, it is a lie the runtime would reject on a null id. Such a call is
    answered as PLAIN TEXT: the agent still learns the outcome, which is the only thing
    the reply is for, and the two shapes stay honest about which one actually happened.
    """
    if not tool_use.get("toolUseId"):
        return _user_text(f"Result of your {tool_use.get('name')} call "
                          f"(recovered from your message text, which wrote the call out "
                          f"instead of invoking it -- invoke it as a tool next time): "
                          f"{json.dumps(payload, default=str)}")
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


#: A call the model TYPED instead of made: `<invoke name="x">` with `<parameter name="k">v`
#: children, closed by `</invoke>`. Non-greedy body, DOTALL, because the parameters sit on
#: their own lines and a turn can carry more than one of these.
_TYPED_CALL_RE = re.compile(r'<invoke\s+name="([A-Za-z_][A-Za-z0-9_]*)"\s*>(.*?)</invoke>',
                            re.DOTALL)
_TYPED_PARAM_RE = re.compile(r'<parameter\s+name="([A-Za-z_][A-Za-z0-9_]*)"\s*>(.*?)'
                             r'</parameter>', re.DOTALL)

#: The SECOND observed spelling of a typed call: Python source, `stage_complete(**{...})`
#: (or without the splat), with a JSON object as the whole argument. Live: the triage of
#: run-20260811T101948Z-f9d34d27 wrote a complete, correct verdict, announced "emitting
#: the completion signal now" -- and printed exactly this shape as prose. The XML regex
#: above cannot see it, so a fix deployed against one spelling died to the next one.
#: The `{` is captured so the brace matcher knows where the object starts.
_TYPED_PYCALL_RE = re.compile(r'\b([A-Za-z_][A-Za-z0-9_]*)\(\s*(?:\*\*)?\s*(\{)')


def _balanced_braces(text: str, start: int) -> Optional[str]:
    """The substring of `text` from the `{` at `start` to its matching `}`, JSON-aware.

    A regex cannot do this (braces nest, and `}` appears inside string values), and
    json.JSONDecoder.raw_decode cannot either once the model indents Python-style.
    String/escape state tracked so a `}` inside a quoted summary does not close early.
    """
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if esc:
            esc = False
        elif in_str:
            if ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def parse_typed_call(text: str, serviced=SERVICED_TOOLS) -> Optional[dict]:
    """Recover an inline function the model WROTE OUT as text instead of calling.

    Returns a toolUse-shaped dict (toolUseId=None, name, input) or None.

    This is not a tolerance for sloppiness, it is the difference between a run that
    finishes and a run that fails having done all of its work. Live: rehearsal
    run-20260811T005043Z-320cc47e prepared the dataset, uploaded the sourcedir, created
    SageMaker job llmops-qlora-run-20260811T005043Z-320cc47e-i0, verified it InProgress,
    updated the manifest -- and then ended two consecutive turns with the literal text
    `<invoke name="job_launched"><parameter name="job_name">...` instead of a toolUse
    block. The driver reads structured blocks only, so both turns were counted as prose,
    the re-ask budget ran out, and the stage died MissingStageComplete while the training
    job it had correctly launched ran to Completed (442 billable seconds). The work was
    done; only the sentence announcing it was in the wrong grammar.

    That this is worth code and not a prompt fix is settled by counting: across 25 days of
    driver logs `tool=job_launched` appears ZERO times and `tool=stage_complete` three.
    job_launched has never once been spoken as a real call in production, while the same
    harness answers a direct request for it with a proper toolUse block -- so the tool is
    live and callable, and the failure is a per-turn behaviour no declaration can prevent.
    The prompts already mandate the call (TURN-END INVARIANT, and RE_ASK names
    job_launched outright); the agent complied in substance twice and was not heard.

    Deliberately narrow, because a parser that reads intent out of prose is how a driver
    starts inventing claims:

    * `serviced` gates the name. A typed `shell` is NOT recovered -- the harness runs
      shell itself, and a driver that "helpfully" services one would answer a call the
      runtime never made. Only functions this driver alone can answer are recoverable,
      which is the same rule the stop_reason override above already follows.
    * Parameters come only from `<parameter>` tags the model actually wrote. Nothing is
      inferred from the surrounding sentence, so a recovered call claims exactly what a
      real one would have claimed -- and stage_complete's outputs still go through
      verify_outputs, which is what keeps a typed claim from being a trusted one.
    * JSON-looking values are parsed so a typed `outputs` list arrives as a list rather
      than as text that starts with `[` -- the exact shape that made verify_outputs
      vacuous in report.normalize_stage_complete. A value that is not valid JSON stays
      the literal string, because guessing is worse than a legible unverified value.
    * The LAST typed call wins, matching _drain's one-slot capture of real toolUse blocks:
      a turn that narrates a plan and then commits to it ends on the commitment.
    """
    if not text:
        return None
    found, found_at = None, -1
    for match in _TYPED_CALL_RE.finditer(text):
        name = match.group(1)
        if name not in serviced:
            continue
        args = {}
        for pm in _TYPED_PARAM_RE.finditer(match.group(2)):
            raw = pm.group(2).strip()
            try:
                args[pm.group(1)] = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                args[pm.group(1)] = raw
        found, found_at = {"toolUseId": None, "name": name, "input": args}, match.start()
    # The Python spelling, held to a stricter bar than the XML one: the whole argument
    # must be ONE valid JSON object (the live example is), because in Python source a
    # non-JSON fragment is indistinguishable from code the agent is merely quoting.
    # A kwargs form (`stage_complete(run_id="x")`) stays prose for the same reason.
    # Last typed call wins ACROSS spellings -- position decides, matching _drain's
    # one-slot capture: a turn that narrates and then commits ends on the commitment.
    for match in _TYPED_PYCALL_RE.finditer(text):
        name = match.group(1)
        if name not in serviced or match.start() <= found_at:
            continue
        body = _balanced_braces(text, match.start(2))
        if body is None:
            continue
        try:
            args = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(args, dict):
            found, found_at = {"toolUseId": None, "name": name, "input": args}, match.start()
    return found


def verify_outputs(s3, outputs: list) -> list:
    """head_object every claimed s3:// URI; return the missing ones."""
    missing = []
    for uri in outputs or []:
        if not uri.startswith("s3://"):
            continue
        bucket, _, key = uri[5:].partition("/")
        try:
            s3.head_object(Bucket=bucket, Key=key)
        except Exception:
            missing.append(uri)
    return missing


#: The canonical pairwise judge prompt, as deploy/03_storage.py mirrors it. Spelled here
#: because this module must be able to name the instrument WITHOUT asking the agent where
#: it is: a digest checked against a path the claimant chose is not a check.
JUDGE_PROMPT_KEY = "code/eval/judge_prompt_pairwise.md"

#: Metrics keys whose presence means "this stage claims to have judged something", so the
#: attestation is owed. Derived from the claim rather than from a (stage, task) name list
#: for the same reason the escalation guard is derived from the URI: the eval prompt lets a
#: small enough prompt set be scored inside the "evaluate" task instead of "score", and a
#: name list would quietly stop checking the run that took that branch.
JUDGE_CLAIM_KEYS = ("judge_prompt_sha256", "judge_score", "judge_wins", "judge_losses",
                    "judge_ties", "judge_n", "judge_win_rate")


def _verify_judge_instrument(s3, bucket: str, metrics: dict) -> dict:
    """Compare the judge digest a stage CLAIMED against the canonical object's own bytes.

    `report.json carries judge_prompt_sha256, and comparing two runs is then a digest
    comparison rather than an argument` (deploy/03_storage.py) -- but nothing compared it to
    anything. The digest was a string the scoring agent wrote next to its own score, which
    makes it an attestation of the same standing as the score: it cannot corroborate it. The
    failure it exists to catch is exactly the one r5 hit -- the eval prompt said "fixed judge
    prompts" and fixed none, so that run authored its own A-or-B-only instrument and reported
    `judge_ties: 0`, read at the time as a property of the student and actually a property of
    a prompt that offered no tie. A self-authored instrument produces a number that LOOKS
    comparable to the previous run's and is not.

    Returns the attestation record, always -- there is no "nothing to say" outcome. The
    driver deliberately does NOT fall back to stamping the canonical digest when the claim
    is missing: that would turn an absent attestation into a false one, asserting the
    canonical instrument for a score that may well have been produced with another.

    `bucket` is the driver's own DATA_BUCKET, not a bucket named in params, for the reason
    in JUDGE_PROMPT_KEY's comment.
    """
    claimed = str(metrics.get("judge_prompt_sha256") or "")
    att = {"kind": "judge_instrument", "key": JUDGE_PROMPT_KEY,
           "claimed_sha256": claimed, "canonical_sha256": "", "verified": False,
           "checked_at": datetime.now(timezone.utc).isoformat()}
    try:
        body = s3.get_object(Bucket=bucket, Key=JUDGE_PROMPT_KEY)["Body"].read()
    except Exception as exc:  # noqa: BLE001 — an unreadable instrument is a finding, not a raise
        # Today's live state, and worth recording as its own outcome rather than as a
        # mismatch: the mirror ships in the same change as the prompt clause that reads it,
        # so until deploy/03_storage.py runs there is nothing to check against. "Could not
        # check" and "checked and wrong" are different claims about a run.
        att["error"] = f"{type(exc).__name__}: {exc}"
        return att
    att["canonical_sha256"] = hashlib.sha256(body).hexdigest()
    att["verified"] = bool(claimed) and claimed == att["canonical_sha256"]
    return att


def _load_manifest(s3, manifest_uri: str) -> dict:
    bucket, _, key = manifest_uri[5:].partition("/")
    try:
        return json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
    except Exception:
        return {}


#: Manifest blocks the driver must never rewrite, because a human's signature is what put
#: them there. `models` is the resolved model consent, `plan` and `approval` are the signed
#: artifacts themselves, and `params` is what those two were merged into by start-pipeline.
#:
#: Bugs #9, #20 and #21 were all one defect: a default standing in for intent that WAS
#: present in a signed artifact. Writing the manifest back from the driver -- on every
#: stage_complete, from data assembled around an agent's tool call -- is exactly the shape
#: that could reintroduce it a fourth time, so the write is narrowed to what a STAGE
#: produced instead of being a whole-document put. A field being absent from this set is
#: not permission to write it: `_save_manifest` writes only `stages`.
IMMUTABLE_MANIFEST_KEYS = frozenset({"models", "plan", "approval", "params",
                                     "run_id", "created_at", "trigger_source"})


def _save_manifest(s3, manifest_uri: str, manifest: Optional[dict],
                   history_record: dict | None = None,
                   escalation: dict | None = None,
                   attestation: dict | None = None) -> None:
    """Persist the run's stage results, re-reading first so a concurrent write survives.

    Read-modify-write rather than a blind put, and only the `stages` block is taken from
    the caller's copy. Two reasons, both measured rather than defensive:

    1. **Every specialist prompt tells the agent to append its own results to this same
       object**, and the harness role really can (`S3PipelineObjects` grants PutObject on
       `runs/*`). So the driver is the SECOND writer, not the only one. A blind put of the
       copy loaded at the top of `handle_stage_complete` would silently erase whatever the
       agent wrote during the turn -- the human-readable stage summary the prompts ask for.
    2. **The signed blocks must not be rewritable by this path at all.** Re-reading and
       replacing only `stages` means a driver bug cannot restate model consent or the plan,
       which is the defect bugs #9/#20/#21 each were.

    S3 has no compare-and-swap, so this narrows the write rather than making it atomic: the
    ASL is fully serial (no Parallel/Map states), so two stages never complete at once, and
    the remaining window is one stage's agent writing between this read and this put. That
    is a real but bounded gap, and it is smaller than the alternative of not persisting
    stage results at all -- which is what the code did before, at 100% loss.

    `history_record` is appended to `stage_history` on the copy JUST RE-READ, never taken
    from the caller's snapshot. That is the difference between the two blocks: `stages` is
    replaced wholesale, so an entry written into it during the window is lost, while an
    append to the fresh list cannot lose a record that landed while the caller was working.
    The list is append-only on purpose -- it is the only place a per-iteration result
    survives, since `stages[<stage>]` keeps exactly one entry per stage name and iteration
    1 overwrites iteration 0 there. `escalations` is the same mechanism for the escalation
    path, and `attestations` the same mechanism for what the DRIVER measured about a
    stage's claim -- kept out of the stage entry precisely because that entry is the
    agent's claim space, and a measurement filed inside a claim stops being independent
    of it.

    `manifest=None` means APPEND ONLY: leave `stages` exactly as S3 has it. That is not a
    convenience, it is the whole safety property of the escalation caller -- an escalation
    has no stage results to persist, so letting it pass a copy of `stages` would give the
    escalation path the power to restate a stage record it did not produce, and to lose a
    stage entry written between its own load and this re-read. The narrower call cannot.
    """
    bucket, _, key = manifest_uri[5:].partition("/")
    current = _load_manifest(s3, manifest_uri)
    if not current:
        # Nothing to merge into. Refusing rather than creating the object: the manifest is
        # seeded by start-pipeline from a signed plan, so an absent one here means the URI
        # is wrong or the run does not exist, and writing a stages-only document would
        # manufacture a manifest with no plan, no approval and no models -- which reads
        # downstream as "this run was never planned".
        raise ValueError(
            f"refusing to write stage results to {manifest_uri}: no manifest is there to "
            "merge into. start-pipeline seeds the manifest from the signed plan; a "
            "stages-only document would look like a run nobody planned.")
    # The signed blocks are taken from `current` (just re-read) and never from the caller's
    # copy, so divergence between the two cannot reach S3 through here. But it is worth
    # NOTICING: the harness role can write this object, so a block changing between
    # start-pipeline's seed and this stage's completion means either an agent rewrote a
    # human's signature or the driver corrupted its own copy. Both are bug #9's class, and
    # both are invisible afterwards because the value that reaches downstream is correct.
    # Recorded rather than raised -- the stage results below are still worth persisting, and
    # withholding them to protest a field we are not writing would trade one loss for two.
    if manifest is not None:
        tampered = sorted(k for k in IMMUTABLE_MANIFEST_KEYS
                          if k in current and k in manifest and current[k] != manifest[k])
        if tampered:
            print(f"[driver] signed manifest blocks changed mid-run for {manifest_uri}: "
                  f"{tampered} — keeping the copy on S3, not the driver's")
        current["stages"] = manifest.get("stages", {})
    for field, record in (("stage_history", history_record), ("escalations", escalation),
                          ("attestations", attestation)):
        if record:
            existing = current.get(field)
            current[field] = ([*existing, record] if isinstance(existing, list)
                              else [record])
    s3.put_object(Bucket=bucket, Key=key,
                  Body=json.dumps(current, indent=2, default=str).encode(),
                  ContentType="application/json")


#: What each stage prompt calls the model it must use, mapped to its manifest role.
#
# The prompts say "model id in params.teacher_model_id" (data-prep "generate", eval
# "score"). NOTHING has ever written that key: start-pipeline resolves the signed
# plan's models into `manifest.models`, and no prompt mentions `manifest.models` at
# all. So the agents read an absent field and fall back to the model named in their
# own persona line -- "teacher DeepSeek-R1 on Bedrock" -- which is boilerplate, not
# consent. Injecting the resolved roles here, under the names the prompts already
# use, means the consent that start-pipeline enforces is the consent the agent obeys.
MODEL_PARAM_FOR_ROLE = {"teacher": "teacher_model_id", "student": "student_model_id",
                        "judge": "judge_model_id"}


def model_params_from_manifest(manifest: dict) -> dict:
    """`{teacher_model_id: ..., ...}` for the roles this run's manifest assigns.

    Roles the manifest is silent about are omitted rather than defaulted: a stage that
    needs a teacher and finds none must fail visibly, not spend on a guess. The whole
    class of bug here is a default standing in for an approval.
    """
    models = manifest.get("models") or {}
    if not isinstance(models, dict):
        return {}
    return {param: str(models[role])
            for role, param in MODEL_PARAM_FOR_ROLE.items()
            if models.get(role)}


#: Facts a stage PRODUCES that a later stage's prompt reads as a param: the param name each
#: prompt uses, mapped to where the producing stage reported it in `stages`.
#
# `MODEL_PARAM_FOR_ROLE` above carries what a human SIGNED into the stages that obey it.
# This carries what the run itself DISCOVERED, and nothing carried it before: an endpoint
# name does not exist until the deploy stage creates one, so no plan can be signed with it
# and no default can stand in for it. `params.student_endpoint` is read by eval ("a live
# endpoint in params.student_endpoint") and by monitor ("name in the manifest or
# params.student_endpoint") and was written by NOTHING -- not the console, not
# start-pipeline, not the driver. The deploy stage reported `endpoint_name` in its
# stage_complete metrics, the driver put it in a local dict, and it was dropped.
#
# So both halves were correct and never connected, for the fifth time in this repo -- but
# the information here is not a signature that went missing, it is a fact the run itself
# produced. That is the difference that matters for autonomy: a pipeline whose stages cannot
# read each other's results cannot reflect on a run or iterate on it, only redo it.
#
# Read from `stages`, never from `models`: `models.student.endpoint_name` exists in
# manifest.schema.json, but `models` is the resolved record of model CONSENT and the driver
# must not write it (see IMMUTABLE_MANIFEST_KEYS). The stage's own report is the honest
# home for a stage's own finding.
STAGE_FACT_PARAMS = {
    "student_endpoint": ("deploy", "endpoint_name"),
}


def signed_plan_params(manifest: dict) -> dict:
    """`manifest["params"]` — the settings start-pipeline merged out of the signed plan.

    THE SAME OMISSION AS #20/#21/#22, one level up: both halves exist and were never
    connected. start-pipeline merges DEFAULT_PARAMS < trigger params < signed plan into
    `manifest.params` (19 keys on the r5 run) and the prompts read 29 distinct
    `params.X` names -- but the payload this driver hands the agent carried SIX of them
    (task, iteration, the three model roles, student_endpoint). The other 23 lived only
    in the manifest, under a key that is also spelled `params`.

    It worked anyway, which is the part worth being precise about. The eval prompt's
    first line is "the S3 manifest is the single source of truth; read it first", so the
    agent read the manifest, found a `params` object there, and used it -- r5's
    gate-i1.json records it in as many words: "invocation params carried no gates key,
    manifest is source of truth". The bar a human signed was applied correctly.

    But the same prompt also says "if params.gates is absent entirely, do NOT invent a
    threshold: escalate_human", and by the payload's literal `params` that condition was
    TRUE. What stood between a correctly signed plan and a spurious escalation was the
    agent noticing that two different dicts share one name. That is a correctness
    property resting on model judgment, and it must not be load-bearing.

    Merged at the LOWEST precedence in `_run_stage`, so nothing that resolves today
    resolves differently: the signed models, the run's own discovered facts and the
    dispatching caller all still win. Purely additive, by construction.
    """
    params = manifest.get("params")
    return dict(params) if isinstance(params, dict) else {}


def stage_fact_params(manifest: dict) -> dict:
    """`{student_endpoint: ...}` for facts an earlier stage of THIS run reported.

    Absent facts are omitted, never defaulted or guessed -- same rule as the models above.
    A stage that needs the endpoint and finds no param must fail visibly, because the
    alternative is an agent inventing a plausible endpoint name and reporting CloudWatch
    metrics for something that is not the model under test. A metric attributed to the
    wrong endpoint is worse than a missing metric: it reads as evidence.
    """
    stages = manifest.get("stages")
    if not isinstance(stages, dict):
        return {}
    out = {}
    for param, (stage, key) in STAGE_FACT_PARAMS.items():
        entry = stages.get(stage)
        if not isinstance(entry, dict):
            continue
        metrics = entry.get("metrics")
        value = metrics.get(key) if isinstance(metrics, dict) else None
        if value:
            out[param] = str(value)
    return out


def settle_token(sfn, token: str, **kw) -> bool:
    """Settle a task token. Returns True if settled, False if the token was already dead.

    Every ``send_task_success`` / ``send_task_failure`` in this file goes through here, so
    "the token is gone" is answered once instead of four times. Pass ``output=`` for a
    success and ``error=``/``cause=`` for a failure; the presence of ``output`` picks the
    call, so a caller cannot accidentally report success on a failure path.

    A dead token is an ANSWER, not an error -- the same reading this file already applies
    to a rejected DynamoDB condition. It means the execution has ended (timed out, aborted,
    or settled by another route), so there is nothing left to do and no retry can change
    that. Raising instead cost four invocations: ``TaskTimedOut: 'Provided task does not
    exist anymore'`` came out of the re-asks-exhausted settle in ``_run_stage``, the
    ``handler()`` wrapper re-raised it, and Lambda retried the whole asynchronous
    invocation twice -- 2026-08-09 at 05:50:48Z, 05:52:03Z and 05:54:28Z, each retry a
    fresh billed AgentCore turn re-running an agent whose stage had already been decided
    against a token none of them could settle. Also seen once on 2026-08-05 at 15:39:51Z.

    Everything else still raises. A throttle or a 5xx means the settle may yet succeed, and
    swallowing it would strand the token for its full ``TimeoutSeconds`` -- 86400s, a day,
    on every long-work state -- which is the zombie ``MarkRunDone``/``MarkRunFailed`` and
    the report/settle isolation above all exist to prevent.
    """
    try:
        if "output" in kw:
            sfn.send_task_success(taskToken=token, output=kw["output"])
        else:
            sfn.send_task_failure(taskToken=token, error=kw.get("error", ""),
                                  cause=kw.get("cause", ""))
    except Exception as exc:  # noqa: BLE001 — re-raised unless the token is provably dead
        if not is_task_gone(exc):
            raise
        print(f"[driver] task token was already gone, nothing to settle: "
              f"{type(exc).__name__}: {exc}")
        return False
    return True


def _record_stage_event(ddb, run_id: str, stage: str, event_name: str, detail: dict):
    table = ddb.Table(os.environ["EVENTS_TABLE"])
    table.put_item(Item={
        "run_id": run_id,
        "sk": f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}#{stage}#{event_name}",
        "detail": json.dumps(detail, default=str),
    })


#: The stage-event name handle_page_human files a decision brief under, and the one the
#: checkpoint branch files when the agent goes back to waiting after paging. Named
#: constants because they are now a CONTRACT with a second reader: the console derives
#: "this run is waiting on a human" from the pair (D12), and a run silently waiting is
#: the state that fix exists to make visible -- so a renamed event must fail a test, not
#: a pill. `sk` is `<iso>#<stage>#<name>`, so a reader matches on the suffix.
PAGE_EVENT = "HumanPaged"
WAIT_EVENT = "WaitingOnHuman"

#: How many waiting turns get their own timeline row. The eval prompt caps waiting at 6
#: checkpoints, but a prompt is a request and not an enforcement, and maxIterations is
#: 100: an agent that ignores its cap would otherwise write a hundred rows into the
#: timeline an operator opens to find out what happened. Twice the prompt's cap, so an
#: agent that overshoots slightly is still fully recorded.
#:
#: Past the cap the driver writes nothing further, so the newest row's `waiting_turn` is
#: a FLOOR, not a total -- the console renders it as one (`12+`) instead of counting rows,
#: because a row count cannot even be a floor once rows stop being written.
WAIT_ROW_CAP = 12


#: A human verdict addressed to a run, parked where the run's own events live. The sk
#: prefix keeps directives in one contiguous query range and out of the timeline the
#: console renders.
DIRECTIVE_SK = "directive#"


#: A run in one of these states has no live driver invocation, so nothing will ever
#: call take_directive() for it again. Derived from the only two writers of a terminal
#: runs.status -- the driver's own handle_escalate ("escalated") and the state machine's
#: MarkRunFailed / MarkRunDone ("failed" / "completed") -- plus the manual-stop marker.
#: `running` is deliberately absent: that is the ONE state with a listener.
UNREACHABLE_RUN_STATES = ("escalated", "failed", "completed", "stopped")


def run_can_hear_a_directive(ddb, run_id: str) -> tuple[bool, str]:
    """Can a directive addressed to `run_id` still reach an agent? (reachable, status).

    take_directive has exactly one caller: the checkpoint branch of a LIVE driver
    invocation. So "delivered" is not a property of the write -- it is a property of
    whether anyone will ever read it. A verdict parked for a run whose execution has
    ended sits in a mailbox nobody will open again.

    Unknown or unreadable status returns reachable=True. A directives lookup that
    fails must not be the reason a verdict is withheld from a run that could act on
    it; the failure mode we are fixing is a SILENT no-op, and refusing to deliver on
    a transient DDB error would invent a second one.
    """
    try:
        row = ddb.Table(os.environ["RUNS_TABLE"]).get_item(
            Key={"run_id": run_id}).get("Item") or {}
    except Exception:  # noqa: BLE001 — see docstring: unknown means "try to deliver"
        return True, ""
    status = str(row.get("status", ""))
    if not row:
        return True, ""
    return not status.startswith(UNREACHABLE_RUN_STATES), status


def put_directive(ddb, run_id: str, decision: str, rationale: str = "",
                  adjusted_params: dict | None = None, actor: str = "conductor") -> dict:
    """Park a verdict where the agent working `run_id` will pick it up.

    Until this existed, a stage agent could ASK a blocking question (checkpoint /
    escalate_human) but nothing could ANSWER it: resolve_escalation wrote an
    EscalationResolved stage-event that no reader consumed, so triage was advice
    delivered into the void. Live, data-prep proved its own approved teacher budget
    infeasible -- 13.5k output tokens per attempt against a plan that assumed 1,800 --
    and then went on spending under that cap, because "continue" was the only answer
    the driver knew how to give.

    Returns {"parked": bool, "reachable": bool, "run_status": str}. The verdict is
    still WRITTEN when unreachable -- it is the audit record of what was decided --
    but `reachable: False` is what stops the caller reporting success. Answering an
    escalation whose run has already ended is not an answer, and a channel that
    returns 200 for it is how #16 stayed open for three days: the tool reported
    "resolved" every time, so nothing looked broken.
    """
    reachable, status = run_can_hear_a_directive(ddb, run_id)
    ddb.Table(os.environ["EVENTS_TABLE"]).put_item(Item={
        "run_id": run_id,
        "sk": f"{DIRECTIVE_SK}{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}",
        "decision": decision,
        "rationale": str(rationale)[:2000],
        "adjusted_params": json.dumps(adjusted_params or {}, default=str),
        "actor": actor,
        "delivered": False,
        "deliverable": reachable,
        **({"run_status_at_put": status} if status else {}),
    })
    return {"parked": True, "reachable": reachable, "run_status": status}


def take_directive(ddb, run_id: str) -> Optional[dict]:
    """Pop the oldest undelivered directive for this run, or None.

    Delivered exactly once, by a conditional update: a verdict redelivered on every
    checkpoint reads as a fresh instruction each time, and an agent told "raise the cap
    to $13" on every breath would raise it repeatedly. The condition also makes two
    concurrent drivers safe -- the loser sees ConditionalCheckFailed and moves on.

    Never raises. A directives lookup that fails must degrade to "no directive", not
    stall a run that merely wanted another turn.
    """
    try:
        table = ddb.Table(os.environ["EVENTS_TABLE"])
        rows = table.query(
            KeyConditionExpression=Key("run_id").eq(run_id)
            & Key("sk").begins_with(DIRECTIVE_SK)).get("Items", [])
        for row in sorted(rows, key=lambda r: r.get("sk", "")):
            if not str(row.get("sk", "")).startswith(DIRECTIVE_SK) or row.get("delivered"):
                continue
            try:
                table.update_item(
                    Key={"run_id": run_id, "sk": row["sk"]},
                    UpdateExpression="SET delivered = :t, delivered_at = :now",
                    ConditionExpression="delivered = :f",
                    ExpressionAttributeValues={
                        ":t": True, ":f": False,
                        ":now": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
            except Exception:  # noqa: BLE001 — another driver claimed it first
                continue
            params = row.get("adjusted_params") or "{}"
            return {"decision": row.get("decision", ""),
                    "rationale": row.get("rationale", ""),
                    "adjusted_params": (json.loads(params)
                                        if isinstance(params, str) else dict(params)),
                    "actor": row.get("actor", "conductor")}
    except Exception:  # noqa: BLE001 — no directive beats a stalled run
        return None
    return None


def _gate_event(metrics: dict) -> str:
    return ev.QUALITY_GATE_PASSED if metrics.get("gate_passed") else ev.QUALITY_GATE_FAILED


def _health_event(metrics: dict) -> Optional[str]:
    """DriftDetected on a monitor health task, or nothing.

    DRIFT_DETECTED has been declared in events.py since Phase 1 and emitted by NOTHING,
    while the monitor prompt tells the agent to put its finding in
    ``metrics.drift_detected`` "so the orchestrator can emit the event" — naming an emitter
    that does not exist. Nobody noticed because no monitor task had ever been dispatched;
    the promise and its absence were unobservable at the same time.

    It belongs here, not in the agent: emitting is the driver's job for every other stage
    (STAGE_EVENT_MAP), the harness role's PutEvents grant is scoped to this bus for exactly
    this reason, and an agent that self-reports an event can emit one for work it did not
    verify.

    Strict truthiness, deliberately matching the eval gate rather than ``bool()``: this
    event is the only signal a human gets that a deployed model has started behaving
    differently. ``is True`` means an absent or null finding stays silent instead of a
    string like "unknown" or "none" — which is truthy — announcing drift that nobody
    observed. The failure directions are asymmetric but both are real, so the rule is that
    the agent must say ``true`` to be heard.
    """
    return ev.DRIFT_DETECTED if metrics.get("drift_detected") is True else None


def _stamp_dispatch(norm: dict, stage: str, task: str) -> dict:
    """Overwrite the agent's echoed stage/task with what was actually DISPATCHED.

    ``normalize_stage_complete`` reads stage and task out of the agent's tool args, which
    is right for everything else in the payload -- outputs, metrics and evidence are the
    agent's findings and nobody else can supply them. Stage and task are the opposite kind
    of fact: the driver was told both in its own invocation event, so the agent's copy is
    at best a restatement and at worst wrong. Both live monitor sweeps recorded
    ``"task": ""`` because the agent simply omitted the field, and the run's row then said
    a monitor stage completed without saying WHICH of health/sweep/report did -- the exact
    ambiguity #58 existed to remove. It is not cosmetic: the console derives which
    (stage, task) pairs a run executed from this field (``_session_ids``), so an empty
    task quietly widens to "any task of that stage".

    The dispatch wins on principle, not just because the echo happened to be empty here:
    an agent that echoes ``task: "report"`` on a sweep invocation would otherwise file its
    sweep findings under a task that never ran.
    """
    return {**norm, "stage": stage or norm.get("stage", ""),
            "task": task or norm.get("task", "")}


def handle_stage_complete(c, event, args) -> dict:
    """Verify → normalize → canonical publish → events → settle token."""
    run_id, stage, task = event["run_id"], event["stage"], event["task"]
    norm = normalize_stage_complete(args)

    missing = verify_outputs(c["s3"], norm["outputs"])
    if missing:
        return {"ok": False, "missing_outputs": missing}

    _record_stage_event(c["ddb"], run_id, stage, "stage_complete",
                        _stamp_dispatch(norm, stage, task))

    if stage == "eval" and task == "gate":
        detail_type = _gate_event(norm.get("metrics", {}))
    elif stage == "monitor" and task == "health":
        detail_type = _health_event(norm.get("metrics", {}))
    else:
        detail_type = STAGE_EVENT_MAP.get((stage, task))
    if detail_type:
        ev.emit_event(os.environ["EVENT_BUS"], detail_type,
                      {"run_id": run_id, "stage": stage, **norm.get("metrics", {})},
                      client=c["events"])

    # Canonical report — the driver writes it; never rely on the agent's upload.
    #
    # ISOLATED from the token settle below, because it used to sit directly in front of
    # it and that cost a run. Live, data-prep finished teacher generation at its cap,
    # called stage_complete (twice, at 19:23:49 and 19:26:20), and this write died on
    # AccessDenied -- the driver had no s3:PutObject until 19:30. The exception left
    # send_task_success unreached, so the token parked for the full 7200s TimeoutSeconds
    # and the console showed "Data Prep · Generate failed" for work that was DONE and
    # verified on S3.
    #
    # The report is a dashboard convenience; the token is the pipeline's only way to
    # learn that a paid-for stage succeeded. Nothing about the former may be allowed to
    # withhold the latter -- so it degrades to a reported warning instead of a 2-hour
    # silence. The IAM gap is fixed too, but the ordering hazard is the general bug: any
    # future failure in this write (throttling, a bucket policy, a KMS denial) would
    # have bought the same outcome.
    # Two independent writes, two independent failure records. They used to share one
    # `try`, with the manifest write first -- so when the driver's role turned out to lack
    # PutObject on runs/*/manifest.json (#25), the AccessDenied aborted the block before
    # write_run_report, and 8 runs since 2026-08-08 produced no per-run report either.
    # The report write was permitted the whole time. One statement's missing grant silently
    # took down a second, unrelated artifact, and the log line named only "canonical report
    # FAILED" -- which is the one of the two that had NOT actually been refused.
    #
    # Sequencing this way round on purpose: the manifest is what the NEXT stage reads, the
    # report is what humans read, and neither is worth withholding to protest the other.
    failures = []
    manifest = _load_manifest(c["s3"], event["manifest_uri"])
    # No `if manifest:` guard. It used to be here, and it made the absent-manifest case
    # SILENT: a wrong manifest_uri skipped the write and returned ok, which is bug #22's
    # own failure mode surviving inside the fix for it. `_save_manifest` raises, the
    # handler below reports it into the run's event stream, and the token still settles.
    entry = {
        "status": "completed", "outputs": norm["outputs"],
        "metrics": norm.get("metrics", {}), "evidence": norm.get("evidence", "")}
    manifest.setdefault("stages", {})[stage] = entry
    # ...and the same result again, immutably, keyed by nothing. `stages` is keyed by STAGE
    # NAME, so the entry above is the only record of a stage and the next task of that stage
    # replaces it -- including across iterations. On r5 (run-20260811T101948Z-f9d34d27,
    # remediation loop, 2 iterations) the surviving manifest holds iteration-1 content for
    # both `finetune` (metrics.iteration == 1) and `eval` (metrics.delta_judge_win_rate_vs_i0
    # present, and no row left to compare against): the driver-verified iteration-0 training
    # losses and judge counts are gone. A remediation loop whose whole purpose is "did the
    # one targeted change help?" was overwriting the before half of its own answer. The i0
    # numbers survived that run only because the eval agent happened to archive report-i0.json
    # by hand.
    #
    # Not fixed by keying on "<stage>.<task>.i<n>" instead: stage_fact_params and the
    # specialist prompts read manifest["stages"][<stage>] by bare stage name, and the agents
    # already write their own thin "<stage>.<task>.i<n>" entries into the same map -- the
    # driver writing that key too would race an agent for it and win or lose per run. A
    # separate append-only list collides with neither reader.
    record = {"stage": stage, "task": task,
              "iteration": event.get("iteration", manifest.get("iteration")),
              "recorded_at": datetime.now(timezone.utc).isoformat(), **entry}
    # Appended in two places, deliberately, and NOT double-counted: this copy is what
    # write_run_report reads below, and _save_manifest ignores the caller's stage_history
    # entirely -- it appends `record` to the list it re-reads from S3, so the durable list
    # gains exactly one entry per completion even though two dicts saw the append.
    manifest.setdefault("stage_history", []).append(record)
    # Trust-but-verify, applied to the INSTRUMENT and not only to the artifacts. Every other
    # claim in this call is checked against reality -- outputs are head_object'd, the gate
    # verdict is re-derived strictly, the signed blocks are re-read from S3 -- and the one
    # claim that decides whether two runs' scores mean the same thing was taken on trust.
    stage_metrics = norm.get("metrics", {})
    attestation = None
    if any(k in stage_metrics for k in JUDGE_CLAIM_KEYS):
        attestation = {**_verify_judge_instrument(
            c["s3"], os.environ.get("DATA_BUCKET", ""), stage_metrics),
            "stage": stage, "task": task,
            "iteration": event.get("iteration", manifest.get("iteration"))}
        manifest.setdefault("attestations", []).append(attestation)
    try:
        # The manifest goes BACK to S3, which it never used to. Every prompt is told
        # "the S3 manifest at manifest_uri is the single source of truth; read it first,
        # append your results to it" -- and this dict was a local variable handed to
        # write_run_report and then dropped. So the report (which humans read) carried
        # every stage's outputs and metrics while the manifest (which AGENTS read)
        # carried none of them: `stages` was still `{}` after a deploy stage reported
        # `endpoint_name`. Measured, not inferred.
        #
        # That is what makes it worse than a stale field. A stage cannot see what the
        # stage before it produced, so eval and monitor read `params.student_endpoint`
        # for an endpoint deploy had just created and named, finetune's "analyze" task
        # is told to diagnose from artifacts the manifest does not list, and an agent
        # asked to reflect on a run has only its own turn to reflect on. A pipeline
        # whose stages cannot read each other's results cannot iterate on a run; it can
        # only redo it.
        _save_manifest(c["s3"], event["manifest_uri"], manifest, history_record=record,
                       attestation=attestation)
    except Exception as exc:  # noqa: BLE001 — never withhold the token for an artifact
        failures.append(f"manifest {type(exc).__name__}: {exc}")
        print(f"[driver] manifest stage-write FAILED for {run_id}/{stage}: "
              f"{failures[-1]}")
    # Guarded on run_id, not on the manifest write's success: an unwritable manifest is no
    # reason to withhold the report, but an EMPTY manifest is. `report_key_for("")` falls
    # back to the alias key, so reporting a manifest that failed to load would publish a
    # document describing nothing to reports/run-latest/ -- destroying the last real run's
    # published report to announce a run whose manifest could not even be read. Splitting
    # the two writes made that reachable for the first time, which is why it is stated here
    # rather than left to the key helper.
    if manifest.get("run_id"):
        try:
            write_run_report(c["s3"], os.environ["DATA_BUCKET"], manifest)
        except Exception as exc:  # noqa: BLE001 — same reason, separately
            failures.append(f"report {type(exc).__name__}: {exc}")
            print(f"[driver] canonical report FAILED for {run_id}/{stage}: {failures[-1]}")
    else:
        failures.append(
            f"report skipped: no manifest at {event['manifest_uri']} to report on")
        print(f"[driver] report SKIPPED for {run_id}/{stage}: {failures[-1]}")

    # One event per stage still, listing whichever writes failed: the row is keyed by
    # (run_id, stage) and a second _record_stage_event with the same name would either
    # overwrite the first or need a suffix nobody queries.
    report_error = "; ".join(failures) or None
    if report_error:
        _record_stage_event(c["ddb"], run_id, stage, "report_write_failed",
                            {"error": report_error})

    if event.get("task_token"):
        metrics = norm.get("metrics", {})
        payload = {"run_id": run_id, "stage": stage, "task": task, **metrics}
        # Gate semantics must be strict for gate tasks: an absent/None gate_passed on
        # an eval gate means NOT passed (fail closed) — a mini-run agent once emitted
        # gate_passed=null + needs_human=true and the old default-True promoted it.
        if stage == "eval" and task == "gate":
            payload["gate_passed"] = metrics.get("gate_passed") is True
        else:
            payload["gate_passed"] = bool(metrics.get("gate_passed", True))
        if report_error:
            payload["report_write_failed"] = report_error
        settle_token(c["sfn"], event["task_token"],
                     output=json.dumps(payload, default=str))
    return {"ok": True, "normalized": norm,
            **({"report_error": report_error} if report_error else {})}


def handle_job_launched(c, event, args) -> dict:
    """Launch-and-release: park the token keyed by job name; resume λ settles it.

    Stage-generic: finetune parks its training job here and eval parks its student
    inference job (which runs as a SageMaker training-type job, so the same
    EventBridge rule wakes the same resume Lambda). TRAINING_STARTED is emitted for
    both deliberately -- the event describes the SageMaker job kind, and the run row's
    current_stage says whose it is."""
    run_id = event["run_id"]
    table = c["ddb"].Table(os.environ["RUNS_TABLE"])
    table.update_item(
        Key={"run_id": run_id},
        UpdateExpression="SET job_name = :j, task_token = :t, current_stage = :s",
        ExpressionAttributeValues={":j": args.get("job_name", ""),
                                   ":t": event.get("task_token", ""),
                                   ":s": event["stage"]})
    ev.emit_event(os.environ["EVENT_BUS"], ev.TRAINING_STARTED,
                  {"run_id": run_id, "job_name": args.get("job_name", "")},
                  client=c["events"])
    return {"released": True}


#: The finops agent is the one harness that is NOT a pipeline stage: it is invoked by
#: the scheduler, not the state machine, so it has no task token, no run in the runs
#: table, and no manifest to append to. Its terminal tools are its own.
FINOPS_STAGE = "finops"
FINOPS_TERMINAL_TOOLS = ("publish_cost_report", "update_rate_card", "flag_variance")


def _is_finops(event) -> bool:
    return event.get("stage") == FINOPS_STAGE


def _mark_run_escalated(ddb, run_id: str) -> bool:
    """Set status=escalated on an EXISTING run row. True if a row was updated.

    `attribute_exists(run_id)` is the whole point. DynamoDB's update_item is an UPSERT:
    on a key with no row it CREATES one carrying the key plus whatever SET writes. So
    this call was never "update the run's status" -- for any invocation with no run, it
    was "mint a run", and the row it minted was the two-attribute shape
    {run_id, status: escalated} with no created_at, trigger_source or iteration.

    That is not hypothetical: `sweep-2026-08-01` sat in llmops-pipeline-runs from a
    scheduled orphan-endpoint sweep that escalated. monitor_sweep/handler.py is careful
    -- it writes its own bookkeeping row to EVENTS_TABLE and its docstring says why a
    sweep must never appear as a run -- and then the driver wrote one on its behalf,
    through a path the sweep does not know exists.

    The condition, rather than another `_is_finops`-style stage allowlist: the previous
    fix enumerated the one non-run invoker known at the time (finops), which left the
    NEXT one -- the sweep, added later, under its own synthetic sweep-<date> id -- to
    rediscover the same defect. Triage (triage-<subject>) and any future scheduled
    dispatch are the same shape. The runs table already knows which ids name runs; a
    list of stages that don't is a second copy of that fact, maintained by hand, that
    is wrong the moment someone adds a caller. Only start_pipeline creates run rows,
    which makes "a row exists" exactly the right question.

    A rejected condition is NOT an error here: it is the answer. Anything else raises,
    because a throttle or an outage must not read as "this was not a run".
    """
    try:
        ddb.Table(os.environ["RUNS_TABLE"]).update_item(
            Key={"run_id": run_id},
            UpdateExpression="SET #s = :v",
            ConditionExpression="attribute_exists(run_id)",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":v": "escalated"})
        return True
    except Exception as exc:  # noqa: BLE001 — only the rejected condition is absorbed
        if _is_condition_failure(exc):
            return False
        raise


def _is_condition_failure(exc) -> bool:
    """ConditionalCheckFailedException, matched by botocore error CODE.

    Same reasoning as TASK_GONE_CODES in pipeline/contracts/task_tokens.py: the exception
    classes hang off a live client instance, so referencing table.meta.client.exceptions
    here would make this module unimportable under an injected test double.
    """
    code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
    return code == "ConditionalCheckFailedException"


def _record_manifest_escalation(c, event, args) -> bool:
    """Land the escalation in the run's own manifest, so its report carries a finding.

    `build_run_report` raises a critical finding for a stage whose status is "escalated" --
    and NOTHING has ever written that status into `manifest["stages"]`. handle_escalate's
    durable record was runs.status plus (since #52) a DDB stage event, neither of which the
    report reads. So r5 (run-20260811T101948Z-f9d34d27) escalated to a human at its
    iteration-1 gate and published `"findings": []`: the one run that needed a person to
    look at it produced the report of a run with nothing to say. A branch no writer can
    reach is not coverage.

    Recorded as an append to `escalations` rather than as `stages[stage]["status"]`, because
    the escalating stage usually HAS a completed entry -- r5's eval stage holds the scoring
    task's judge counts -- and overwriting it would trade a missing finding for a destroyed
    measurement.

    Returns whether a record was written, for the caller's log line. Never raises: the
    guarantee this function sits inside is that no bookkeeping may withhold the alert.
    """
    uri = str(event.get("manifest_uri") or "")
    run_id = str(event.get("run_id") or "")
    if not uri or not run_id or f"/runs/{run_id}/" not in uri:
        # Derived from the URI rather than from _is_triage/_is_finops on purpose. A triage's
        # manifest_uri names the SUBJECT run while its run_id is the conductor's own (see
        # ESCALATION_SUBJECT above), and a scheduled sweep has no manifest at all -- so the
        # condition that actually matters is "this escalation belongs to the run whose
        # manifest this is". Checking that directly means a future non-run caller cannot
        # start writing its escalations into somebody else's run just by not being named
        # here.
        return False
    try:
        _save_manifest(c["s3"], uri, None, escalation={
            "stage": event.get("stage", ""), "task": event.get("task", ""),
            "iteration": event.get("iteration"),
            "reason": str(args.get("reason", ""))[:2000],
            "escalated_at": datetime.now(timezone.utc).isoformat()})
        return True
    except Exception as exc:  # noqa: BLE001 — an alert must not wait on its own filing
        print(f"[driver] could not record the escalation of {run_id} in its manifest: "
              f"{type(exc).__name__}: {exc}")
        return False


def handle_escalate(c, event, args) -> dict:
    """Raise a stage's escalation to a human, on every channel that still works.

    The channels are deliberately independent, and the ordering matters. SNS used to be
    the first statement here and unwrapped, so a failed publish took the stage event, the
    bus event AND the task-token settle down with it -- the run would then sit at
    `running` holding a live token until the state machine's timeout, hours later, over a
    notification. That is the worst possible thing to gate on this particular call:
    `llmops-escalations` had **zero subscribers** when this was written, so SNS was the
    one channel already known to reach nobody, and `ensure_topic` in deploy/03_storage.py
    reports that state as "NO SUBSCRIBERS -- every escalate_human call publishes into the
    void" rather than as health, because the deploy cannot invent an address.

    That has since changed and the ordering still stands. Measured 2026-08-10: one
    confirmed email subscriber, and 2026-07-29..08-08 the topic published 15 and
    delivered 11 with 0 failures -- the 4 undelivered all predate the confirmation on
    2026-08-02. So SNS now reaches someone, which makes it MORE important not to gate on
    it, not less: a working notification channel is still the one that fails on a
    throttle or an IAM change, and it must not be able to silence the two that do not
    leave the account -- the stage event the console renders, and the EscalatedToHuman
    event the conductor triages off the bus.
    """
    run_id = event["run_id"]
    try:
        c["sns"].publish(TopicArn=os.environ["LLMOPS_SNS_TOPIC"],
                         Subject=f"[llmops] escalation: {run_id}/{event['stage']}",
                         Message=json.dumps(args, indent=2, default=str))
    except Exception as exc:  # noqa: BLE001 — one dead channel must not close the rest
        print(f"[driver] SNS publish failed for the escalation of {run_id}: {exc}")
    if _is_finops(event):
        # No runs-table row exists for an audit invocation; updating one would mint a
        # phantom run that the console would then display alongside real pipeline runs.
        # Kept as an early return even though _mark_run_escalated would now decline the
        # write anyway: the audit path also has no task token and no run to emit about,
        # so it exits before the event and the settle below, not just before the write.
        return {"escalated": True}
    run_row = _mark_run_escalated(c["ddb"], run_id)
    # Record the escalation in stage-events on BOTH paths. handle_escalate has never
    # written one -- for a real run the runs.status write was the whole durable record,
    # so an escalation has never appeared in the timeline the console renders from this
    # table, unlike a page (handle_page_human records its own). Declining the row write
    # for a non-run would have made that worse: a scheduled sweep's escalation would be
    # left with nothing on record anywhere but an SNS email. Unconditional, so the trail
    # does not depend on which path ran; `run_row` says which it was, and false is the
    # signal that this id names no pipeline run. Bookkeeping only -- it must never be
    # able to withhold the alert below.
    try:
        _record_stage_event(c["ddb"], run_id, event["stage"], "escalated",
                            {"reason": args.get("reason", ""),
                             "task": event.get("task", ""), "run_row": run_row})
    except Exception as exc:  # noqa: BLE001 — never withhold an escalation for a log
        print(f"[driver] could not record the escalation of {run_id}: {exc}")
    # The second durable record, and the only one the run's own report can read. Same rule
    # as the stage event above: bookkeeping, so it swallows its own failures internally and
    # cannot reach the emit or the settle below.
    _record_manifest_escalation(c, event, args)
    try:
        ev.emit_event(os.environ["EVENT_BUS"], ev.ESCALATED_TO_HUMAN,
                      {"run_id": run_id, "stage": event["stage"],
                       "reason": args.get("reason", "")}, client=c["events"])
    except Exception as exc:  # noqa: BLE001 — see below: the token must still settle
        # The settle is what releases the state machine. Letting a failed PutEvents skip
        # it would park a live task token on a run that has already escalated, and the
        # only thing that ever frees it is the stage's own timeout (86400s on every
        # long-work state since 2026-08-03, so a DAY) -- the zombie that MarkRunDone,
        # MarkRunFailed and #52 all exist to prevent, re-entered through the notification
        # path. The raise makes this except clause more load-bearing, not less.
        print(f"[driver] could not emit {ev.ESCALATED_TO_HUMAN} for {run_id}: {exc}")
    if event.get("task_token"):
        settle_token(c["sfn"], event["task_token"], error="EscalatedToHuman",
                     cause=args.get("reason", "")[:250])
    return {"escalated": True}


def handle_page_human(c, event, args: dict) -> dict:
    """Escalate a triage decision to the human owner: SNS brief + audit event.

    `page_human` is the conductor's ONLY exit when a decision is above its authority,
    and it is the exit the driver names in its own undeliverable-verdict rejection.
    It was declared on the orchestrator harness from Phase 5 on and serviced only by
    the console's chat worker -- so on the DRIVER path (every triage invocation: the
    conductor is not in a chat when an EscalatedToHuman event routes to it) it fell
    through to the unknown-tool branch and came back {"status": "unsupported"}.

    Live proof, 2026-08-01 13:45Z: the fix to resolve_escalation (#53) correctly told
    the conductor its verdict was undeliverable and to use launch_run or page_human.
    The conductor did neither -- it re-called resolve_escalation, was rejected again,
    then wrote plan.json + relaunch-plan.json to S3 and the turn ended. No run was
    dispatched and no human was paged. A rejection that names two paths where one of
    them silently answers "unsupported" is a dead end dressed as a choice.

    The paging itself is NOT trust-but-verify'd against anything, because there is
    nothing to verify: the brief IS the artifact. What is enforced is that a page
    carries a decision brief -- situation + recommendation, matching the harness's own
    required schema. A page reading "needs a human" with no options tells the owner
    only that something is wrong, which is the state they were already in.
    """
    situation = str(args.get("situation") or args.get("reason") or "").strip()
    recommendation = str(args.get("recommendation") or "").strip()
    if not situation or not recommendation:
        return {"ok": False, "reason": (
            "page_human needs both 'situation' and 'recommendation': a page without a "
            "recommendation hands the owner the problem and none of the analysis you "
            "already did. Include 'options' too if there is a real choice to make.")}

    # The event decides, not the agent -- see triage_subject. The agent's own run_id is
    # kept only as the last resort for an invocation that carries no subject at all,
    # which on the triage path cannot happen.
    subject_run = triage_subject(event) or str(args.get("run_id") or "")
    # WHO is asking, read off the invocation the same way the subject is. This was the
    # literal string "orchestrator-triage" and the stage below was the literal
    # "orchestrator" -- correct while the orchestrator was the only harness that declared
    # the tool, and a lie the moment a stage agent does (D12: a borderline gate is a
    # question a human can answer, so eval needs a channel that notifies WITHOUT ending
    # the run). An eval page would have arrived claiming to be a triage of someone
    # else's run, been filed in a timeline the operator has no reason to open, and gone
    # onto the bus as `stage: orchestrator`. The subject was already derived; the AUTHOR
    # was not, and a brief whose author is wrong is a brief addressed to the wrong reader.
    #
    # On the triage path this derives the OLD string byte-for-byte -- TRIAGE_STAGE is
    # "orchestrator" and TRIAGE_TASK is "triage" -- so nothing about a conductor page
    # changes, which is why the value can be derived rather than special-cased.
    stage = str(event.get("stage") or "") or TRIAGE_STAGE
    task = str(event.get("task") or "")
    brief = {"run_id": subject_run,
             "situation": situation,
             "options": args.get("options") or [],
             "recommendation": recommendation,
             "paged_by": f"{stage}-{task}" if task else stage,
             "triaging_run_id": event.get("run_id", "")}
    c["sns"].publish(
        TopicArn=os.environ["LLMOPS_SNS_TOPIC"],
        Subject=f"[llmops] owner decision needed: {subject_run or 'triage'}"[:100],
        Message=json.dumps(brief, indent=2, default=str))
    # Recorded against the run being escalated ABOUT, not the triaging run -- same
    # addressing rule as put_directive, for the same reason: the timeline a reader
    # opens is the stuck run's, not the conductor's. This comment CLAIMED that rule
    # while the `or event.get("run_id")` it sat above did the opposite, and put_directive
    # has no such fallback: 3 of the 12 pages on record went to the conductor's own
    # timeline instead of the stuck run's.
    #
    # A page with no derivable subject still gets a row, because the alternative is a
    # put_item on an empty partition key -- a ValidationException that would turn a
    # successfully-published page into a crashed invocation, and #72 exists to stop a
    # bookkeeping failure from swallowing an alert. It is filed under the triaging run
    # and says so: `run_id: ""` in the brief is what distinguishes this row from the
    # mis-addressed ones, which carried the triage id in BOTH fields. Unreachable from
    # the bus (triage_event_from_bus raises without a subject); reachable only from a
    # hand-built invocation.
    _record_stage_event(c["ddb"], subject_run or str(event.get("run_id") or ""),
                        stage, PAGE_EVENT, brief)
    # OwnerPaged, NOT EscalatedToHuman. A page is what the conductor emits when it has
    # ALREADY triaged and found the decision above its authority; EscalatedToHuman means
    # "a conductor should look at this". Sharing one detail-type made the triage rule
    # feed itself the moment that rule existed -- escalate -> triage -> page -> triage --
    # and every lap is a real harness invocation billed against a decision already made.
    ev.emit_event(os.environ["EVENT_BUS"], ev.OWNER_PAGED,
                  {"run_id": subject_run, "stage": stage,
                   "reason": situation[:500]}, client=c["events"])
    return {"ok": True, "run_id": subject_run, "stage": stage}


def _backstop_page(c, event, outcome: dict) -> dict:
    """Page the owner when a triage ended without answering the escalation.

    The escalation bus has exactly ONE rule (`llmops-escalation-triage`) and exactly one
    target (this Lambda). The state machine's EscalateFail is a bare `events:putEvents`
    with no SNS anywhere on the path -- so on the state-machine escalation path the
    conductor is not the FIRST line to a human, it is the ONLY one. If its turn ends
    without resolving, dispatching or paging, nobody is ever told: the run row reads
    `failed`, the execution reads FAILED, and the only trace is a log stream.

    That is the third layer of this defect and the reason it looked intermittent. Layer 1
    was a rejection naming an exit that cannot work; layer 2 is that launch_run genuinely
    cannot dispatch from a bus triage; layer 3 is that failing both costs the owner
    nothing but silence. Measured 2026-08-05..08: 11 of 11 directives ever parked were
    undeliverable, and 4 of the 9 triaged runs produced no HumanPaged event at all --
    c8b13faa, 86ab8a14 and b56281da among them, each an ARC-2 lineage run that died with
    scientific work complete and nobody notified.

    Best effort by construction: it runs on the way out, after the outcome is already
    decided, and a failure to page must not turn a merely-unanswered triage into a
    crashed invocation. The brief says plainly that it comes from the driver, not from
    the conductor's judgment, so an owner reading it knows the agent did not choose to
    escalate -- it simply stopped.
    """
    if not _is_triage(event) or outcome.get("status") in TRIAGE_ANSWERED:
        return outcome
    # Was the same expression triage_subject now holds, spelled out a second time. This
    # copy was the CORRECT one -- which is why the backstop's own pages are the ones
    # filed properly -- and having it here while handle_page_human trusted the agent is
    # exactly the drift: the driver already knew the subject at the only site that did
    # not need the agent to tell it.
    subject = triage_subject(event)
    try:
        handle_page_human(c, event, {
            "run_id": subject,
            "situation": (
                f"Triage of {subject or 'an escalation'} ended without resolving it "
                f"(driver outcome: {outcome.get('status', 'unknown')}"
                + (f" -- {outcome['reason']}" if outcome.get("reason") else "") + "). "
                "No verdict was delivered, no run was dispatched, and the conductor did "
                "not page you itself. This page is the driver's backstop, not the "
                "conductor's judgment."),
            "options": [
                "Read the run's stage events and decide the corrective action yourself",
                "Relaunch the work from the console's Tasks tab with adjusted params "
                "(a dispatch needs your signature; the conductor cannot sign one)",
                "Close the run as a documented failure",
            ],
            "recommendation": (
                "Open the stuck run in the console and decide directly: an unanswered "
                "triage means the automated path is exhausted for this run."),
        })
    except Exception as exc:  # noqa: BLE001 — a failed page must not mask the outcome
        print(f"[driver] backstop page failed for triage of {subject}: "
              f"{type(exc).__name__}: {exc}")
        return outcome
    return {**outcome, "backstop_paged": True}


def handle_finops_tool(c, event, name: str, args: dict) -> dict:
    """Service one of the finops agent's terminal tools.

    Same trust-but-verify contract as ``stage_complete``: a claimed S3 artifact is
    head_object'd before the call is accepted, because an agent that reports a report
    it never wrote produces a dashboard panel pointing at a 404.

    Two rejections are specific to cost data and are worth stating in code rather than
    only in the prompt:

    - A report for a period Cost Explorer still marks ``Estimated`` must declare
      ``settlement: provisional``. A provisional number published as settled is how a
      figure that will still move gets quoted as final.
    - ``flag_variance`` must name a ``driver`` category. One aggregate percentage says
      the estimate was wrong without saying what to fix, and the whole point of
      reconciliation is that the next estimate is better.

    Rejections come back as a toolResult so the agent can correct and re-call, exactly
    as stage_complete's missing-output path does — the turn is not wasted.
    """
    period = event.get("params", {}).get("period", "")
    project = event.get("params", {}).get("project", "")

    if name == "publish_cost_report":
        missing = verify_outputs(c["s3"], [args.get("report_uri", "")])
        if missing:
            return {"ok": False, "reason": f"report_uri not in S3: {missing}. "
                                          "Write it and call publish_cost_report again."}
        if args.get("settlement") not in ("provisional", "settled"):
            return {"ok": False, "reason": "settlement must be 'provisional' or "
                                          "'settled'; a period Cost Explorer flagged "
                                          "Estimated is provisional."}
    elif name == "flag_variance":
        if not args.get("driver"):
            return {"ok": False, "reason": "flag_variance needs 'driver': the single "
                                          "category driving the delta. A percentage "
                                          "alone does not say what to fix."}
    elif name == "update_rate_card":
        missing = verify_outputs(c["s3"], [args.get("rates_uri", "")])
        if missing:
            return {"ok": False, "reason": f"rates_uri not in S3: {missing}."}

    # Audit trail keyed (project, period) so a missing daily reconcile is visible, and
    # so the console can read a period's findings without scanning.
    c["ddb"].Table(os.environ["ACTUALS_TABLE"]).put_item(Item={
        "project": project or "unknown",
        "sk": f"{period}#finding#{name}#{args.get('run_id', '-')}",
        "task": event.get("task", ""),
        "tool": name,
        "detail": json.dumps(args, default=str)[:8000],
    })

    # A variance the agent flagged is a human-facing finding, not just a log line.
    if name == "flag_variance":
        try:
            c["sns"].publish(
                TopicArn=os.environ["LLMOPS_SNS_TOPIC"],
                Subject=f"[llmops] cost variance: {args.get('run_id', '?')} "
                        f"{args.get('variance_pct', '?')}%",
                Message=json.dumps(args, indent=2, default=str))
        except Exception as exc:  # noqa: BLE001 — a notify failure must not lose the row
            print(f"[finops] variance notify failed: {exc}")

    return {"ok": True, "tool": name, "args": args}


#: Ordered fallback chain per declared model, cheapest deviation first.
#
# This is an APP-LAYER AVAILABILITY-DEGRADATION decision, not a throttling response, and
# the distinction decides where the next operator looks. AWS documents 503
# ServiceUnavailableException as a regional capacity constraint and prescribes switching
# Region / cross-Region inference / provisioned throughput for it; "switch models" appears
# nowhere in that list. 429 ThrottlingException is the one that means your own rate against
# your own quota, and it is the only one a Service Quotas increase answers -- which is why
# _is_model_5xx deliberately does not match it (a shared RETRY path is AWS's own advice for
# both; a shared DIAGNOSIS path sends someone to file a quota ticket for an outage that had
# nothing to do with their quota).
#
# Measured on the r6e storm window (CloudWatch AWS/Bedrock, 2026-08-14 18:00-19:00Z), which
# is what turned that from a preference into a fact:
#
#   EstimatedTPMQuotaUsage peak      44,719 of a 4,000,000 global quota   = 1.1%
#   InvocationThrottles              no data
#   InvocationServerErrors           46 of 59 invocations                 = 78%
#   global.anthropic.claude-opus-5   0 invocations  <- the fallback never once ran
#   us.anthropic.claude-fable-5      0 invocations
#
# So the 503s were not ours to shed: at 1.1% of quota, throttling our own offered load buys
# nothing, and a quota increase buys nothing. The app layer is the only layer that can act.
#
# The chain's FIRST rung changes inference profile without changing model:
# global.* and us.* are distinct SYSTEM_DEFINED profiles (both ACTIVE in us-east-1,
# verified with list-inference-profiles) routing into different Region pools, so the rung
# costs zero prompt change and zero price change -- the same weights at the same rate,
# reached another way. Only when that also fails do we spend a model change, because Fable
# 5 and Opus 5 are different price tiers and a tier change is a spend the plan did not
# budget (hence _record_failover).
#
# The two zeros in the table above are also the reason the ORDER matters more than it looks:
# failover has never executed in production, so nothing here has ever observed the fallback
# being healthy. A rung that keeps the model constant is the one rung whose success we can
# still reason about if it fires.
MODEL_FALLBACKS = {
    "global.anthropic.claude-fable-5": (
        "us.anthropic.claude-fable-5",
        "global.anthropic.claude-opus-5",
    ),
    "us.anthropic.claude-fable-5": (
        "global.anthropic.claude-fable-5",
        "global.anthropic.claude-opus-5",
    ),
}


def _is_model_5xx(error: str) -> bool:
    return any(sig in error for sig in
               ("InternalServerException", "ServiceUnavailableException"))


#: Consecutive dead-stream turns the retry ladder will absorb before it settles the token
#: ModelUnavailable. Env-overridable (like the resurrector's STALE_MINUTES) so a conductor
#: watching a live vendor outage can widen it without a redeploy.
#:
#: Was 4, with a 60s backoff cap: a ~6 minute ceiling, and it was calibrated against
#: nothing. Both storms actually measured ran ~3x longer, and the second one is why this
#: number moved:
#:
#:   r6c  EvalScore        2026-08-12 02:18-02:36Z   ~18 min
#:   r6e  FinetuneAnalyze  2026-08-14 18:21-18:37Z   ~16 min
#:
#: r6e is the proof that the ceiling, not the outage, killed the run. Analyze fought from
#: 18:21:00 to 18:26:57 and gave up. The storm was BURSTY, and the orchestrator's triage
#: got a clean turn at 18:31:59 -- five minutes later, on the SAME model (every
#: agents/*/harness.json declares global.anthropic.claude-fable-5), so capacity had
#: demonstrably returned while the stage that needed it had already been failed. 10 turns
#: against the cap below is ~18 min of backoff plus the attempts themselves (a 5xx turn
#: dies in seconds), i.e. ~21-22 min of patience.
#:
#: Three ceilings bound how far this may go, and all three still have room:
#:   - ASL FinetuneAnalyze TimeoutSeconds=3600 is the outer guard.
#:   - The resurrector wakes a stage whose liveness beat is STALE_MINUTES=20 old, and the
#:     beat is written once per turn -- so the budget must grow by adding TURNS, never by
#:     lengthening one sleep past ~20 min. Hence the 180s cap.
#:   - Lambda's 900s wall does NOT bound this: infra_error_turns rides the
#:     _self_reinvoke() payload, so the counter survives across invocations.
#:
#: Two things this number is NOT, both worth stating because they are easy to assume:
#:   - It is not an AWS-endorsed shape. The Builders' Library argues the other way --
#:     cap retries, fail earlier, and treat a long timeout as wasted resources -- and AWS
#:     documents Bedrock 503 as a "temporary capacity constraint" without ever putting a
#:     duration on it or suggesting the same model will succeed on a later attempt. The
#:     justification here is local and empirical: the two storms above, and a stage that
#:     had nothing to salvage on failure. r6e's loss was giving up too early, not failing
#:     too slowly.
#:   - It is not multiplied by an SDK retry underneath. _AGENTCORE_CFG sets
#:     retries={"max_attempts": 0}, so botocore adds no attempts of its own, and an
#:     in-band event-stream error raises from the stream parser outside the retry chain
#:     regardless. 10 turns here means 10 requests, not 30 or 50.
INFRA_ERROR_BUDGET = int(os.environ.get("INFRA_ERROR_BUDGET", "10"))

#: Ceiling on one backoff sleep. Deliberately below STALE_MINUTES (see above): a sleep
#: long enough to look dead to the resurrector would be answered with a duplicate stage.
INFRA_BACKOFF_CAP_S = 180


def _agentcore_control(c: dict):
    """Control-plane client, created on first use — same shape as _kms above.

    A lazy accessor rather than a client built inside the failover function, because a
    client constructed in the callee cannot be injected, and a failover path no test
    can drive is a failover path no test was driving: the swap-with-no-restore bug
    lived in exactly the region this seam made unreachable.
    """
    if "agentcore_control" not in c:
        region = os.environ.get("AWS_REGION", "us-east-1")
        c["agentcore_control"] = boto3.client("bedrock-agentcore-control",
                                              region_name=region)
    return c["agentcore_control"]


def _record_failover(c, event, harness_full_id: str, from_model: str,
                     to_model: str) -> None:
    """Leave the override on the run row: which model actually served this stage.

    Not a divergence witness any more (there is nothing durable to diverge -- see
    _maybe_failover_model), but still the only record that answers a question the
    artifacts cannot: Fable 5 and Opus 5 are different price tiers, so a run that
    failed over was billed at a rate the plan did not budget, and finops reconciles
    against this row. Best effort -- a bookkeeping write must not break the retry it
    is describing.

    Written on EVERY rung, including the profile-only first rung where the tier does not
    move, because the row answers two questions and only one of them is about money: which
    model served this stage at all. A run whose numbers came off us.* while its plan named
    global.* changed nothing a finops reader cares about and everything a reproducibility
    reader does. Each rung overwrites the last, so the row names the model that served the
    stage rather than the whole path taken to it -- the path is in the log and on the bus.
    """
    try:
        c["ddb"].Table(os.environ["RUNS_TABLE"]).update_item(
            Key={"run_id": event["run_id"]},
            UpdateExpression="SET model_failover = :f",
            ConditionExpression="attribute_exists(run_id)",
            ExpressionAttributeValues={":f": json.dumps({
                "harness_id": harness_full_id, "from_model": from_model,
                "to_model": to_model, "scope": "invocation",
                "at": datetime.now(timezone.utc).isoformat()})})
    except Exception as exc:  # noqa: BLE001
        print(f"[driver] could not record failover state: {type(exc).__name__}: {exc}")


def _maybe_failover_model(c, event) -> None:
    """Advance this session one rung down its model's fallback chain on a vendor 5xx burst.

    Best-effort: any failure here must not break the salvage retry.

    One rung per dead-stream turn, and the chain is walked at most once end to end per
    invocation -- see MODEL_FALLBACKS for why the first rung keeps the model and only
    changes inference profile. Every rung after the first is served from state parked on
    `c`, so a storm turn never re-reads the control plane it is already failing on.

    The walk restarts at the DECLARED model in each fresh invocation, because `c` dies with
    the invocation and _self_reinvoke() hands the retry to a new one. That is deliberate:
    the declared model is the one a human signed, so it earns the first turn of every
    invocation, and if the storm ended during the handoff that first turn is also the
    cheapest possible recovery.

    The switch is InvokeHarness's own per-invocation `model` field, parked on `c` and
    passed by every later _invoke in this invocation. AgentCore documents that field as
    the way to override a harness's declared model for one call ("If specified,
    overrides the harness default"), and being per-call is the entire point: a harness
    is shared by all seven agents and every concurrent run, and a fallback is needed by
    exactly one session for a few minutes.

    UpdateHarness is the mechanism this used to reach for, and it is the wrong size in
    both directions. It is a control-plane WRITE that mints a new immutable harness
    version and repoints the DEFAULT endpoint -- and every stage here invokes through
    DEFAULT (no qualifier appears anywhere in state_machine.asl.json), so a swap during
    a storm moves the model under every OTHER run mid-flight, not just this one. It then
    needed a restore on the way out to undo that blast radius, which cost a second write
    plus ~15s of `wait_ready` per swap, on a control plane that is 5xxing at the time,
    with the fleet left diverged whenever the restore lost. The override needs no write,
    no wait, and no restore: `c` dies with the invocation.

    Unscoped, the old swap repointed the deployed fleet permanently: every later run
    would execute on a model the human never signed (the whole point of the KMS
    approval spine), that the cost model never priced (Fable 5 and Opus 5 are different
    tiers), and that docs/ARCHITECTURE.md §9.3 asserts is NOT what is deployed -- with
    nothing in the tree able to notice, because the divergence lived in the control
    plane while agents/*/harness.json still read the way it always did.
    """
    try:
        arn = _resolve_harness_arn(event["harness_id"])
        harness_full_id = arn.rsplit("/", 1)[-1]
        declared = c.get("_failover_declared")
        if declared is None:
            # First 5xx of this invocation: the one control-plane call left, and a READ --
            # what the harness DECLARES, which is what MODEL_FALLBACKS is keyed on. Not
            # taken from the dispatch event: the fallback has to answer "what is this
            # harness actually serving", and that answer lives in the control plane.
            #
            # The whole config comes back with it, so inference settings the harness
            # declares ride along on the override instead of being dropped by a hand-built
            # one. It is parked and repointed per rung from here on.
            ctl = _agentcore_control(c)
            model = ctl.get_harness(harnessId=harness_full_id)["harness"]["model"]
            declared = model.get("bedrockModelConfig", {}).get("modelId", "")
            if not MODEL_FALLBACKS.get(declared):
                # One of the two ways this declines to act, and it is printed for the same
                # reason as the other: the declared model has no chain, so the retry runs
                # unprotected, and an operator reading the log must be able to tell which
                # of the two silences they are looking at.
                print(f"[driver] no failover for {harness_full_id}: modelId={declared!r} "
                      "has no MODEL_FALLBACKS entry; retrying on the same model")
                return
            c["_failover_declared"] = declared
            c["_failover_config"] = model
            c["_failover_depth"] = 0
        chain = MODEL_FALLBACKS[declared]
        depth = c["_failover_depth"]
        if depth >= len(chain):
            # Reached from the third 5xx of an invocation onward. A storm turn arrives here
            # up to INFRA_ERROR_BUDGET times, so this must neither re-read the control
            # plane nor flap between rungs it has already spent -- but it still has to SAY
            # something, because an early return that printed nothing is exactly the
            # silence that made r6e unreadable.
            print(f"[driver] failover chain exhausted for {harness_full_id} "
                  f"({declared} -> {' -> '.join(chain)}); retrying on {chain[-1]}")
            return
        # Walk the DECLARED model's chain by index rather than re-keying MODEL_FALLBACKS on
        # the current override. Re-keying would cycle: global.* names us.* as its first rung
        # and us.* names global.* as its own, so a lookup on the override would ping-pong
        # between two profiles forever and never reach the model change behind them.
        previous = declared if depth == 0 else chain[depth - 1]
        fallback = chain[depth]
        c["_failover_depth"] = depth + 1
        print(f"[driver] failing over {harness_full_id}: {previous} -> {fallback} "
              f"(vendor 5xx burst, per-invocation override, rung {depth + 1}/{len(chain)})")
        # Deep-copied per rung so the parked config stays the pristine declared one and
        # each override is its own object. Mutating one shared dict in place would make
        # every rung ALIAS the last: the request already sent on rung 1 would read back as
        # rung 2 to anything holding the dict, which is the difference between a test that
        # proves the chain advanced and a test that cannot tell.
        model = copy.deepcopy(c["_failover_config"])
        model["bedrockModelConfig"]["modelId"] = fallback
        c["_model_override"] = model
        _record_failover(c, event, harness_full_id, previous, fallback)
        # ModelFailedOver, NOT EscalatedToHuman. Nothing is being escalated: the
        # failover already fixed it and the retry continues. This carried the word
        # "informational" inside a reason string, which was harmless only while the bus
        # had no rules -- an EventBridge pattern cannot read prose, so the first rule to
        # route EscalatedToHuman to triage would have paged the conductor about a run
        # that had just healed itself.
        ev.emit_event(os.environ["EVENT_BUS"], ev.MODEL_FAILED_OVER, {
            "run_id": event.get("run_id", "?"), "stage": event.get("stage", "?"),
            "from_model": previous, "to_model": fallback,
            "reason": f"ModelFailover: {previous} -> {fallback} (vendor 5xx burst); "
                      "informational, pipeline continuing"}, client=c["events"])
    except Exception as exc:  # noqa: BLE001 — never let failover break the retry path
        # Swallowing is still right: a failed failover must not break the retry it was
        # trying to help. Swallowing SILENTLY was the bug, and it cost r6e.
        #
        # The driver's role shipped with bedrock-agentcore:InvokeHarness and
        # InvokeAgentRuntime but WITHOUT GetHarness, so the get_harness() above raised
        # AccessDeniedException on every 5xx this function was ever called for. Failover
        # has therefore never once run in production, and there was no way to know: no
        # log here, no log on the no-fallback return, no log on success. The run row had
        # no model_failover, the bus had no ModelFailedOver, and the only available
        # reading was "the fallback was tried and did not help". A guard test now pins
        # the policy to the calls this module makes; this line is what would have caught
        # it on day one.
        print(f"[driver] failover attempt failed (continuing to retry on the declared "
              f"model): {type(exc).__name__}: {exc}")


RE_ASK = ("Your turn ended without an inline-function call. If your task is "
          "INCOMPLETE, continue now and finish it — then call the appropriate "
          "inline function (job_launched for launched jobs, stage_complete for "
          "completed stages, checkpoint if you need another turn). If the work "
          "is already done, call stage_complete now (outputs may be an empty "
          "list if nothing was produced).")

#: The last resort before ContentFiltered: a fresh session whose first turn is told to
#: attest WITHOUT prose. Appended to the re-seeded task payload by the filtered branch.
#: Why a fresh session works when re-asking does not: the filter acts on what the model
#: writes, and a model re-asked in the SAME session keeps trying to restate the gagged
#: summary sitting in its context (live, EvalScore of run-9d995076: the eval agent had
#: already written report.json, then had three summary turns suppressed in a row — the
#: stage died with its work complete on S3). A fresh session has no gagged summary to
#: restate, and the instruction forecloses writing a new one: verify your own outputs,
#: claim them through the tool channel — URIs and numbers, which no filter acts on — or
#: escalate. The attestation protocol is intact either way: the agent still claims, the
#: driver still head-checks every claimed URI.
CF_RECOVERY = (
    "\n\nRECOVERY in a fresh runtime session: the platform content filter suppressed "
    "your previous session's replies repeatedly — nothing you wrote in chat was "
    "delivered, but everything you wrote to S3 is intact. Re-read your manifest and "
    "your own S3 outputs to see what is already done. Do NOT redo finished work, and "
    "do NOT summarize, quote or restate ANY data content in this session — no ticket "
    "text, no examples, no personal data; URIs and numbers only. If the task's outputs "
    "are complete, END YOUR FIRST TURN with stage_complete carrying the output URIs "
    "and numeric metrics (one neutral sentence of summary at most). If they are not "
    "complete, call escalate_human and say which output is missing, by URI alone. "
    "Prose will be filtered again; the inline-function call is the only channel that "
    "survives.")


#: The EVENTS_TABLE partition for non-run driver heartbeats (#37). The resurrector
#: duplicates this constant (separate bundles); a test pins the two spellings together.
LIVENESS_PK = "__liveness__"


def _settle_liveness(c, event, result):
    """DELETE a triage's liveness item on every terminal return.

    One choke point instead of a mark at every terminal branch: the statuses that end
    an invocation are exactly the ones that are not the between-turns handoff. A
    delete rather than a done_at mark, by review finding: a marked item is immortal
    (the 15-minute sweep re-reads the whole history forever) and its resurrection
    count leaks into the subject's NEXT incarnation, which would arrive dead at the
    cap it inherited. Deleting a nonexistent item is a no-op in DynamoDB, so no
    condition is needed -- but a REAL failure is printed, not swallowed: a throttled
    delete left silent is a finished triage the sweep will revive to page a second
    human about an answered question. An exception path deliberately never reaches
    this (handler re-raises first): a CRASHED invocation must stay revivable, that
    being the entire point of the beat."""
    if not _is_triage(event):
        return
    if (result or {}).get("status") == "self_reinvoked_between_turns":
        return
    try:
        c["ddb"].Table(os.environ["EVENTS_TABLE"]).delete_item(
            Key={"run_id": LIVENESS_PK,
                 "sk": "beat#" + str(event.get("run_id") or "")})
    except Exception as exc:  # noqa: BLE001
        print(f"[driver] liveness settle delete failed -- the resurrector may revive "
              f"a finished triage: {type(exc).__name__}: {exc}")


def handler(event, context=None, clients=None):
    """Run one stage, and never strand the task token on the way out.

    A SYNCHRONOUS stage invocation that raises is reported to Step Functions by the
    Lambda integration itself. An ASYNCHRONOUS continuation is not: the state machine
    waits on the task token, not on this invocation, so an exception here goes to
    CloudWatch and to nobody else. The token then parks until TimeoutSeconds -- 86400s
    on every long-work state since the 2026-08-03 raise, so a full DAY -- with the run
    record still saying 'running'.

    Live: the driver's missing s3:PutObject grant crashed the final stage_complete of
    run-...-8b864805 twice in the same silence, and the run held its token for 90
    minutes. The AccessDenied was one bug. The 90 minutes was this one: the stage had
    genuinely failed and the only participant who knew was a log stream.

    So an unexpected exception fails the token with the real cause attached, then
    re-raises so the invocation is still recorded as an error (and a synchronous
    caller, like the console's dispatch path, still sees a hard failure rather than a
    silent success).
    """
    c = clients or _clients()
    # An EventBridge delivery is not a state-machine payload. Recognised by its own
    # envelope keys ("detail-type" + "detail") rather than by the absence of a task
    # token, because plenty of legitimate driver invocations have no token.
    if event.get("detail-type") == ev.ESCALATED_TO_HUMAN and "detail" in event:
        event = triage_event_from_bus(event, os.environ["DATA_BUCKET"])
    try:
        # _backstop_page wraps the RETURN, not a branch inside the loop, so it covers
        # every way a triage can end without answering -- prose after re-asks, an
        # unsupported tool, a rejected page, a stage_complete that decided nothing. A
        # check placed at any single one of those would have to be repeated at the rest.
        result = _run_stage(event, context, c)
        _settle_liveness(c, event, result)
        return _backstop_page(c, event, result)
    except Exception as exc:
        token = event.get("task_token")
        if token:
            try:
                settle_token(c["sfn"], token, error="DriverCrashed",
                             cause=f"{type(exc).__name__}: {exc}"[:32000])
            except Exception as report_exc:  # noqa: BLE001
                # Nothing left to do but say so; the timeout is now the only backstop.
                print(f"[driver] could not fail the parked token: {report_exc}")
        # A crashed triage is also an unanswered one, and the crash reaches nobody but
        # CloudWatch: a bus triage has no task token, so there is no send_task_failure
        # above to carry the news and no state machine waiting to hear it.
        _backstop_page(c, event, {"status": "crashed",
                                  "reason": f"{type(exc).__name__}: {exc}"[:500]})
        raise
    # No `finally` restoring a model here any more, and that is the point of the
    # redesign: a failover is now InvokeHarness's per-invocation `model` override parked
    # on `c`, so it expires when this invocation does. It used to be an UpdateHarness
    # write that had to be undone on every exit path -- settled, crashed,
    # self-reinvoked -- by a restore that could itself fail on the same control plane
    # that was 5xxing, and did, leaving the whole fleet on a model no human signed. See
    # _maybe_failover_model.


def _run_stage(event, context=None, c=None):
    c = c if c is not None else _clients()
    payload = {"run_id": event["run_id"], "stage": event["stage"],
               "manifest_uri": event["manifest_uri"],
               "params": {"task": event["task"], **(event.get("params") or {})}}
    if event.get("task_token"):
        payload["params"]["iteration"] = event.get("iteration", 0)

    # The models the SIGNED plan approved, under the param names the prompts read.
    #
    # An extra S3 GET per stage (~10ms) rather than trusting the dispatch event: the
    # manifest is where start-pipeline recorded the resolved consent, and the event is
    # authored by whatever dispatched this stage. A caller-supplied value still wins,
    # because a remediation iteration may legitimately override -- but it can no longer
    # be the DEFAULT, which is how "params.teacher_model_id" came to mean "whatever
    # model the persona line happens to name".
    #
    # The same read also carries forward what EARLIER STAGES OF THIS RUN discovered --
    # `params.student_endpoint` from the deploy stage's reported `endpoint_name`. One GET
    # serves both: two reads of one object would be a second failure surface for the same
    # fact, and could hand one stage a manifest the other did not see.
    #
    # Caller-supplied values win over both, and the run's own facts win over nothing. The
    # precedence is deliberate and only looks arbitrary: `approved` and `facts` cannot
    # collide (a signed model role is not a runtime endpoint), so their order between
    # themselves is unobservable, and putting the dispatch event last preserves the
    # remediation override that `model_params_from_manifest` was written for.
    #
    # `signed` goes FIRST -- lowest precedence -- because it is the only one of the four
    # that a later step may legitimately refine: the plan names a training_instance, the
    # remediation iteration may override it, and the run's own endpoint is not a plan
    # setting at all. Being last would let a static plan value overwrite a fact the run
    # discovered. Being first makes the merge purely additive: every key that resolved
    # before this line existed still resolves to the same value. See signed_plan_params.
    try:
        manifest = _load_manifest(c["s3"], event["manifest_uri"])
        signed = signed_plan_params(manifest)
        approved = model_params_from_manifest(manifest)
        facts = stage_fact_params(manifest)
        payload["params"] = {**signed, **approved, **facts, **payload["params"]}
    except Exception as exc:  # noqa: BLE001
        # Not fatal: stages that need no model (deploy smoke tests, monitor sweeps) must
        # not be blocked by a manifest read. The stage that DOES need one will fail on
        # the absent param, which is the visible failure a silent default replaced.
        print(f"[driver] could not read approved models or prior-stage facts from the "
              f"manifest ({type(exc).__name__}: {exc}) -- continuing without them")

    # Session epoch: which incarnation of this stage's session we are on. Travels in
    # the event so every resumption path (self-reinvoke, resurrector wake) recomputes
    # the SAME session id; a clock read at call time would hand two live drivers two
    # different sessions for one stage.
    epoch = int(event.get("_session_epoch", 0))
    session_started_at = float(event.get("_session_started_at") or time.time())
    sess = session_id(event["run_id"], event["stage"], event["task"], epoch)

    # Continuation across Lambda invocations: one harness turn can run 840s and the
    # Lambda dies at 900s, so only ONE turn fits per invocation. Whenever the loop
    # would start another turn without enough time left, self-reinvoke carrying the
    # pending messages list (session + task token survive; live-verified Sandbox.Timedout
    # killed a run whose agent finished its work but never got to report it).
    if event.get("_continuation"):
        messages = event["_continuation"]
        stream_retried = bool(event.get("_stream_retried"))
        re_asks = int(event.get("_re_asks", 0))
        filtered_turns = int(event.get("_filtered_turns", 0))
        infra_error_turns = int(event.get("_infra_error_turns", 0))
        # A run waiting on a human waits across Lambda boundaries -- a paged agent that
        # checkpoints until an answer arrives will cross several -- so the fact that it
        # is waiting has to ride the continuation like every other consecutive counter.
        # `_resumed` is the cautionary tale here: a continuation key nothing reads is a
        # key that silently means nothing, so both of these are read at the top and used
        # in the checkpoint branch below.
        paged_at = str(event.get("_paged_at") or "")
        wait_turns = int(event.get("_wait_turns", 0))
        cf_recovery = int(event.get("_cf_recovery", 0))
    else:
        messages = _user_text(json.dumps(payload, default=str))
        stream_retried = False
        re_asks = 0  # up to 2 CONSECUTIVE: nudge, final demand; any serviced tool call re-arms it
        filtered_turns = 0  # consecutive platform-suppressed turns; see the filtered branch
        infra_error_turns = 0  # consecutive dead-stream turns; see the outage branch
        cf_recovery = 0  # 1 once the fresh-session filter recovery has been spent
        # Not carried over from a PREVIOUS invocation of this stage on purpose: a fresh
        # start (resurrector wake, state-machine re-entry) re-sends the stage prompt, so
        # the agent's own decision to wait is gone with the transcript. The page itself
        # survives -- it is a row in the run's timeline, which is what the console reads
        # to draw the waiting pill -- and only the per-turn marker restarts.
        paged_at = ""
        wait_turns = 0

    def _out_of_time() -> bool:
        return bool(context) and context.get_remaining_time_in_millis() < 850_000

    def _self_reinvoke():
        c["lambda"].invoke(
            FunctionName=context.function_name, InvocationType="Event",
            Payload=json.dumps({**event, "_continuation": messages,
                                "_stream_retried": stream_retried,
                                "_re_asks": re_asks,
                                "_filtered_turns": filtered_turns,
                                "_infra_error_turns": infra_error_turns,
                                "_cf_recovery": cf_recovery,
                                "_paged_at": paged_at,
                                "_wait_turns": wait_turns,
                                "_session_epoch": epoch,
                                "_session_started_at": session_started_at},
                               default=str))
        return {"status": "self_reinvoked_between_turns"}

    def _roll_session():
        """Start the next session epoch for this stage.

        A fresh session carries NO conversation history, so the pending `messages`
        list cannot travel with it: it is usually a toolResult answering a toolUse the
        new session never issued, which AgentCore rejects outright. Re-seed with the
        original task payload plus a resume instruction — the same shape that made the
        2026-08-08 hand-resurrection lossless, because every stage's real state lives
        in S3, not in the transcript.
        """
        nonlocal epoch, session_started_at, sess, messages, re_asks, stream_retried, \
            filtered_turns, infra_error_turns
        epoch += 1
        filtered_turns = 0
        infra_error_turns = 0
        session_started_at = time.time()
        sess = session_id(event["run_id"], event["stage"], event["task"], epoch)
        re_asks = 0
        stream_retried = False
        messages = _user_text(json.dumps(payload, default=str) + (
            "\n\nRESUMING in a fresh runtime session (the previous one reached the "
            "platform's 8h session lifetime). Nothing you wrote to S3 is lost and "
            "nothing before this point is repeatable from memory: re-read your "
            "manifest and your own S3 outputs to see what is already done, skip that "
            "work, and continue. End this turn with an inline-function call."))
        print(f"[driver] session rollover -> epoch {epoch} ({sess})")
        try:
            # Rolled session ids are NOT derivable from (run, stage, task) alone, and
            # the console's batch-eval scoring reconstructs ids that way — an
            # unrecorded epoch is a session whose spans nobody ever scores. Append it
            # to the run row so the derivation has something to union with.
            c["ddb"].Table(os.environ["RUNS_TABLE"]).update_item(
                Key={"run_id": event["run_id"]},
                UpdateExpression=("SET rolled_session_ids = "
                                  "list_append(if_not_exists(rolled_session_ids, :e), :s)"),
                ConditionExpression="attribute_exists(run_id)",
                ExpressionAttributeValues={":e": [], ":s": [sess]})
        except Exception as exc:  # noqa: BLE001 — bookkeeping must not kill the roll
            print(f"[driver] could not record rolled session id (continuing): "
                  f"{type(exc).__name__}: {exc}")

    def _heartbeat():
        """Stamp the run row before every turn: driver_beat_at + the exact payload a
        resurrector needs to re-invoke this stage. The async self-reinvoke is
        fire-and-forget — Lambda dropped one on 2026-08-08 (AsyncEventsDropped=1) and
        run 68cfa9c8 sat dead for 9 hours at 4/55 with its token parked and nobody
        left alive to be re-invoked; an operator resurrected it by hand from the
        Step Functions history. A beat that stops while the stage is unfinished IS
        the dead-driver signal, and the stamped payload is the resurrection. Best
        effort: a beat that cannot be written must not kill the turn it announces.

        `attribute_exists(run_id)` for the reason _mark_run_escalated spells out at
        length: update_item is an UPSERT, so on a key with no row this call would MINT
        one -- and here the minted shape is worse than the {run_id, status} phantom that
        left `sweep-2026-08-01` in the table, because a row carrying driver_beat_at and
        driver_beat_payload is exactly what the resurrector sweeps for. The driver would
        be manufacturing resurrectable ghost runs for every non-run invocation.

        But a REJECTED condition is the answer, not a failure. A triage runs under
        `triage-<subject>` (see TRIAGE_STAGE) and only start_pipeline creates run rows, so
        a triage has no row and CANNOT have one: every triage heartbeat was guaranteed to
        be refused, and each one printed "heartbeat write failed (continuing)". Measured
        on run-20260810T182807Z-e394ada9's triage: 11 such lines in 2 minutes, all of them
        describing correct behaviour. The same distinction was already reasoned out at
        _mark_run_escalated and simply never applied here.

        The refusal is also the HANDOFF (#37): a non-run invocation still needs a
        heartbeat somewhere, or nothing can resurrect a dead triage. The runs table is
        the wrong somewhere -- a minted row carrying driver_beat_at is a resurrectable
        ghost run -- so the rejected condition routes the same beat into EVENTS_TABLE
        under the dedicated `__liveness__` partition, which the resurrector reads with
        a Query. done_at is written by _settle_liveness on every terminal return, so a
        finished triage's silence is an ending, not a death."""
        try:
            c["ddb"].Table(os.environ["RUNS_TABLE"]).update_item(
                Key={"run_id": event["run_id"]},
                UpdateExpression="SET driver_beat_at = :t, driver_beat_payload = :p",
                ConditionExpression="attribute_exists(run_id)",
                ExpressionAttributeValues={
                    ":t": datetime.now(timezone.utc).isoformat(),
                    ":p": json.dumps({**{k: event[k] for k in
                                         ("run_id", "stage", "task", "harness_id",
                                          "manifest_uri", "task_token", "iteration")
                                         if k in event},
                                      "_session_epoch": epoch,
                                      "_session_started_at": session_started_at},
                                     default=str)})
        except Exception as exc:  # noqa: BLE001 — the beat is telemetry, not the work
            if _is_condition_failure(exc):
                _beat_liveness()  # not a run row: the beat belongs in __liveness__
                return
            print(f"[driver] heartbeat write failed (continuing): "
                  f"{type(exc).__name__}: {exc}")

    def _beat_liveness():
        """The non-run half of the heartbeat — for TRIAGES ONLY, by review finding:
        a crashed scheduled job (sweep-<date>, finops-<period>) must wait for its next
        schedule, not get five async re-runs of non-idempotent work; a triage is the
        one non-run whose loss is unrecoverable, because its escalation exists nowhere
        but in the invocation. Which is also why `params` rides the payload -- a run
        re-resolves its work order from the manifest, a triage's work order IS
        params.escalation, and a revival without it triages blind and files its pages
        under an empty subject. One item per subject; _settle_liveness DELETES it on
        terminal return, so a recurring subject starts a fresh item with a fresh
        resurrection count."""
        if not _is_triage(event):
            return  # a crashed scheduled job waits for its next schedule
        try:
            c["ddb"].Table(os.environ["EVENTS_TABLE"]).update_item(
                Key={"run_id": LIVENESS_PK,
                     "sk": "beat#" + str(event.get("run_id") or "")},
                UpdateExpression="SET beat_at = :t, payload = :p",
                ExpressionAttributeValues={
                    ":t": datetime.now(timezone.utc).isoformat(),
                    ":p": json.dumps({**{k: event[k] for k in
                                         ("run_id", "stage", "task", "harness_id",
                                          "manifest_uri", "task_token", "iteration",
                                          "params")
                                         if k in event},
                                      "_session_epoch": epoch,
                                      "_session_started_at": session_started_at},
                                     default=str)})
        except Exception as exc:  # noqa: BLE001 — still telemetry
            print(f"[driver] liveness beat write failed (continuing): "
                  f"{type(exc).__name__}: {exc}")

    first_turn = True

    while True:
        # Between turns is the ONLY safe place to roll: mid-turn the session holds an
        # unanswered toolUse. Checked before the beat so the stamped payload names the
        # epoch a resurrector should wake into.
        if time.time() - session_started_at >= SESSION_ROLLOVER_S:
            _roll_session()
        _heartbeat()
        if not first_turn and _out_of_time():
            # Said out loud for the same reason as the per-turn line below (#24: "a stage
            # that can fail must say how"), which covered only turns that COMPLETED. The
            # paths that end an invocation without completing a turn stayed mute, and two
            # of them in a row cost 956s of wall with ZERO application log lines --
            # invocations fe22e1c6 (55.9s, silent) and 925119d7 (900s, Status: timeout) of
            # run-20260810T182807Z-e394ada9. From CloudWatch alone there was no way to
            # tell a healthy handoff from a driver dying in a loop.
            print(f"[driver] handing off stage={event.get('stage')} "
                  f"task={event.get('task')} between turns: "
                  f"{context.get_remaining_time_in_millis()}ms wall left, "
                  f"re_asks={re_asks}, epoch={epoch}")
            return _self_reinvoke()
        first_turn = False
        resp = _invoke(c["agentcore"], event["harness_id"], sess, messages,
                       event.get("qualifier"), c.get("_model_override"))
        out = _drain(resp, out_of_wall=lambda: bool(context) and
                     context.get_remaining_time_in_millis() < DRAIN_DEADLINE_MARGIN_MS,
                     remaining_ms=(context.get_remaining_time_in_millis
                                   if context else None))

        if out["error"] == DEADLINE_CUT:
            # The Lambda wall arrived mid-stream. This is not a stream death — the
            # harness turn is still running server-side (840s cap) and will finish
            # without us. Hand the session to the next invocation with the salvage
            # prompt: on resume the turn is over, and the agent is asked to restate
            # its pending call. Burning the stream-salvage retry here instead would
            # leave a REAL death later in the same invocation unprotected.
            messages = _user_text("The stream was interrupted. Continue from where "
                                  "you left off; call your pending inline function.")
            return _self_reinvoke()

        if out["error"] and not stream_retried:
            # involuntary stream death — same-session salvage retry, once
            #
            # Printed, because this branch `continue`s without reaching the per-turn line
            # below: a stream that died at t=56s took its cause with it, and the very next
            # loop-top _out_of_time() check handed the invocation off (also silently until
            # now), so BOTH exits from a 55.9s invocation left no record. The cause string
            # is the whole diagnosis here -- a 5xx failover, a throttle and a socket reset
            # are three different problems with one symptom.
            print(f"[driver] stream died for stage={event.get('stage')} "
                  f"task={event.get('task')}, salvage retry in the same session: "
                  f"{out['error']}")
            stream_retried = True
            if _is_model_5xx(out["error"]):
                _maybe_failover_model(c, event)  # vendor-quota burst: override the model
            messages = _user_text("The stream was interrupted. Continue from where "
                                  "you left off; call your pending inline function.")
            continue

        # A platform outage is not the agent's answer. Live: r6c's EvalScore met a
        # Bedrock ServiceUnavailable storm (02:18-02:36Z, 2026-08-12) AFTER its one
        # salvage retry was spent; every subsequent dead-stream turn fell through to
        # the prose branch, was billed to the re-ask budget (re_asks 0->1->2 with
        # text_chars=0 and error= on every line), and the stage died
        # MissingStageComplete -- the agent blamed for silence while the MODEL API was
        # down. Its triage then suffocated in the same storm. Same disease as
        # stop_reason=content_filtered, same cure: an own bounded budget that rides
        # the continuation payload, a backoff (storms last minutes, not seconds), and
        # an exhaustion that settles under its real name -- ModelUnavailable tells the
        # operator to check the vendor, MissingStageComplete sends them hunting a
        # transcript that never existed.
        # r6e (run-20260814T161302Z-7ff2a30a, FinetuneAnalyze) is why the budget below is
        # INFRA_ERROR_BUDGET rather than a literal 4: the ladder worked exactly as written
        # and still lost a run whose model had recovered. See that constant for the
        # measurements.
        if out["error"]:
            if infra_error_turns < INFRA_ERROR_BUDGET:
                infra_error_turns += 1
                if _is_model_5xx(out["error"]):
                    _maybe_failover_model(c, event)
                wait_s = min(20 * infra_error_turns, INFRA_BACKOFF_CAP_S)
                messages = _user_text("The stream was interrupted. Continue from "
                                      "where you left off; call your pending inline "
                                      "function.")
                # A real stream death can arrive with as little as ~45s of wall left
                # (DRAIN_DEADLINE_MARGIN_MS) -- sleeping 60s there gets the invocation
                # hard-killed mid-sleep: no reinvoke, no settle, the counter's progress
                # lost. If the backoff does not fit, hand the retry to a fresh
                # invocation instead; its cold start IS the backoff.
                #
                # With the cap at INFRA_BACKOFF_CAP_S this branch is taken far more often
                # than it was at 60s, and that is the intended shape: the deep end of the
                # ladder wants long waits, and a long wait belongs in a fresh invocation's
                # wall rather than gambling against this one's.
                if context and context.get_remaining_time_in_millis() < (
                        wait_s * 1000 + 120_000):
                    print(f"[driver] stream died again "
                          f"({infra_error_turns}/{INFRA_ERROR_BUDGET}) with "
                          f"too little wall left to back off in-place -- handing off: "
                          f"{out['error']}")
                    return _self_reinvoke()
                print(f"[driver] stream died again "
                      f"({infra_error_turns}/{INFRA_ERROR_BUDGET}), backing "
                      f"off {wait_s}s before the retry (re_asks untouched): "
                      f"{out['error']}")
                time.sleep(wait_s)
                continue
            if event.get("task_token"):
                # +2: the first death is absorbed by the salvage retry before this
                # counter starts, and the exhausting death is the one in hand -- an
                # operator correlating against the vendor status page needs the true
                # window, not one turn short.
                settle_token(c["sfn"], event["task_token"],
                             error="ModelUnavailable",
                             cause=f"{infra_error_turns + 2} consecutive turns died "
                                   f"on stream errors; last: {str(out['error'])[:180]}")
            ev.emit_event(os.environ["EVENT_BUS"], ev.PIPELINE_FAILED,
                          {"run_id": event["run_id"], "stage": event["stage"],
                           "reason": "model unavailable"}, client=c["events"])
            return {"status": "failed", "reason": "model_unavailable"}
        infra_error_turns = 0

        tu = out["tool_use"]
        # One line per turn, because without it a failed stage is undiagnosable. Live:
        # run-20260810T174626Z-3f08b4c6's generate stage burned three turns and died
        # MissingStageComplete with `distillation/generated.jsonl` sitting in S3, and
        # CloudWatch held only three REPORT lines. There was no way to tell "the agent
        # narrated instead of calling" from "the agent called and the branch below
        # dropped it" -- two different bugs with one symptom, and the evidence needed to
        # separate them was never recorded. A stage that can fail must say how.
        # `error` included because a SECOND stream death in one invocation falls through
        # to here (stream_retried is already True), and a partial turn read as a prose
        # turn-end is exactly the "two bugs, one symptom" this line exists to separate.
        print(f"[driver] turn stage={event.get('stage')} task={event.get('task')} "
              f"stop_reason={out['stop_reason']} tool={(tu or {}).get('name')} "
              f"re_asks={re_asks} text_chars={len(out['text'])} "
              f"error={out['error']}")
        # Only a stopReason of "tool_use" means the harness is WAITING for a result.
        # A toolUse block riding along with end_turn was already serviced inside the
        # harness; replying to it makes the next ConverseStream invalid with
        # "toolResult blocks ... exceeds the number of toolUse blocks of previous
        # turn" (found live on the console's dispatch path, same shape here).
        #
        # That reasoning holds only for tools the HARNESS can service -- code_interpreter
        # and shell, which run inside it. It does NOT hold for the inline functions this
        # driver owns: the runtime emits those blocks and has no way to answer them, so
        # one arriving with end_turn is not "already serviced", it is a call nobody will
        # ever answer. Discarding it counted a fully compliant turn as prose, and after
        # three of those the stage failed MissingStageComplete with its outputs already
        # in S3 -- a stage_complete that was CALLED, reported as never called.
        #
        # So the stop_reason decides only for tools this driver cannot service; for the
        # ones it can, the call is serviced whatever the stop_reason says. The set is
        # the dispatch table itself (SERVICED_TOOLS), which is the same question the
        # branches below answer.
        if tu and out["stop_reason"] != "tool_use" and tu.get("name") in SERVICED_TOOLS:
            print(f"[driver] {tu['name']} arrived with stop_reason="
                  f"{out['stop_reason']}; servicing it anyway -- the harness cannot "
                  "have serviced a call only this driver can answer")
            out = {**out, "stop_reason": "tool_use"}
        # The turn may instead have WRITTEN the call out: `<invoke name="job_launched">`
        # as literal text, which no structured reader can see. Recovered only when no
        # real toolUse block arrived, so a genuine call always wins over a transcribed
        # one, and only for SERVICED_TOOLS -- see parse_typed_call for why this is a
        # dispatch path and not a prompt problem (job_launched: 0 real calls in 25 days).
        if not tu:
            typed = parse_typed_call(out["text"])
            if typed:
                print(f"[driver] {typed['name']} was TYPED as text, not called; "
                      f"dispatching it -- params={sorted(typed['input'])}")
                tu = typed
                out = {**out, "stop_reason": "tool_use"}
        if tu and out["stop_reason"] == "tool_use":
            # A structured call proves the agent still speaks protocol, so the
            # consecutive-prose budget re-arms — even for a rejected stage_complete
            # or an unknown tool name; what the budget counts is PROSE turn-ends.
            # Before this reset the counter was a lifetime one: run b56281da died
            # with MissingStageComplete while its (healthy) third SageMaker relaunch
            # was mid-flight, because two prose turns much earlier had used up the
            # allowance and nothing ever gave it back.
            re_asks = 0
            filtered_turns = 0  # same consecutiveness rule as re_asks
            name, args = tu["name"], tu.get("input") or {}
            if name == "stage_complete":
                result = handle_stage_complete(c, event, args)
                if not result["ok"]:
                    messages = _tool_result_content(tu, {
                        "status": "rejected",
                        "reason": f"claimed outputs missing from S3: {result['missing_outputs']}. "
                                  "Write them and call stage_complete again."})
                    continue
                _ack_terminal(c, event, sess, tu, {"status": "acknowledged"},
                              "the task token was settled and the outputs verified")
                return {"status": "completed", **result["normalized"]}
            if name == "job_launched":
                # Live (r6a, 2026-08-12): a remediating agent called job_launched with
                # an EMPTY job_name; job_name is a GSI key on the runs table, DynamoDB
                # rejects "" for index keys, and the unguarded write crashed the WHOLE
                # invocation (DriverCrashed) -- a protocol slip by the agent became an
                # infra failure blamed on the driver. Rejected into the same turn
                # instead, like a stage_complete with missing outputs.
                if not str(args.get("job_name") or "").strip():
                    messages = _tool_result_content(tu, {
                        "status": "rejected",
                        "reason": "job_launched carried no job_name, so nothing can "
                                  "ever wake this run. Name the job you actually "
                                  "created (DescribeTrainingJob to confirm it), or "
                                  "fix the launch and call again."})
                    continue
                handle_job_launched(c, event, args)
                _ack_terminal(c, event, sess, tu, {"status": "released"},
                              "the task token was parked for EventBridge to settle")
                return {"status": "released", "job_name": args.get("job_name")}
            if name == "checkpoint":
                # Just answer it and let the loop-top _out_of_time() check own the
                # reinvoke. This branch used to reinvoke itself with {"_resumed": True}
                # -- a key nothing reads -- so the resumed invocation fell through to
                # the fresh-start branch, re-sent the original stage prompt, and
                # silently dropped both the pending toolResult and the work already
                # paid for (live: a budget escalation raised after a pilot found the
                # plan's token estimate 6.5x low). Its own <60s guard was also dead
                # code, since _out_of_time() fires at 850s remaining, far earlier.
                #
                # A checkpoint is also the DELIVERY point for a human verdict. It is
                # the one moment a working agent is guaranteed to be listening, so a
                # directive parked by resolve_escalation (or by an operator) rides back
                # in the toolResult here. Without this the answer channel was
                # write-only: the agent could ask and nothing could reply.
                directive = take_directive(c["ddb"], event["run_id"])
                # A checkpoint after a page is the ONLY moment this system can tell a
                # waiting run from a working one: checkpoint writes nothing anywhere, so
                # before D12 the two were byte-identical to every reader, and "waiting on
                # a human" was a state the operator could not see -- which is the same
                # thing as a state the system does not have (D7, D9, twice before).
                # A row per waiting turn, because what an operator needs is not only THAT
                # it is waiting but how long it has been paying model tokens to wait.
                if paged_at and not directive:
                    wait_turns += 1
                    if wait_turns <= WAIT_ROW_CAP:
                        try:
                            _record_stage_event(
                                c["ddb"], event["run_id"], event["stage"], WAIT_EVENT,
                                {"paged_at": paged_at, "waiting_turn": wait_turns,
                                 "task": event.get("task", "")})
                        except Exception as exc:  # noqa: BLE001 -- telemetry, not the wait
                            print("[driver] waiting marker write failed (continuing): "
                                  f"{type(exc).__name__}: {exc}")
                elif paged_at and directive:
                    # The answer arrived. Stop marking, so the next page starts a new
                    # wait instead of inheriting this one's turn count.
                    paged_at, wait_turns = "", 0
                messages = _tool_result_content(tu, {
                    "status": "directive", "directive": directive} if directive
                    else {"status": "continue"})
                continue
            if name == "escalate_human":
                handle_escalate(c, event, args)
                _ack_terminal(c, event, sess, tu, {"status": "escalated"},
                              "the escalation was recorded and the run paused")
                return {"status": "escalated"}
            if name == "resolve_escalation":
                # Conductor triage verdict: record it where the escalation lives
                # (stage-events) and emit the resolution event. Discovered unserviced
                # by the same drift guard that found launch_run — a triage that ends
                # in "unsupported" leaves the pipeline paused forever.
                #
                # The audit record alone was still a verdict nobody heard: the waiting
                # agent reads directives, not the timeline. So the decision is ALSO
                # parked for delivery — addressed to the run being triaged, never to
                # the conductor's own run, or it lands in the triager's mailbox and the
                # stuck run waits forever.
                #
                # And parking it is still not answering it. take_directive has exactly
                # ONE caller -- the checkpoint branch of a LIVE driver invocation -- so
                # a verdict addressed to a run whose execution has ENDED goes into a
                # mailbox nobody will open again. This branch used to return
                # {"status": "resolved"} either way, which is how #16 stayed open for
                # three days: run-20260729T104648Z-41631739 was already `escalated`
                # with its token failed and its execution FAILED at 11:19:55Z, so
                # triaging it would have reported success and changed nothing. Same
                # class as the stranded task token: the write is authorized, and
                # unreachable. A tool that cannot act must say so, not return 200.
                #
                # The subject comes from the INVOCATION, not from the verdict. Taking it
                # from `args` made two silent failures possible at once, because
                # `run_id` is declared required on resolve_escalation but a model can
                # still omit it: the audit row fell back to the triage's own id (see
                # triage_subject), and `if subject:` then skipped put_directive AND the
                # reachability check while this branch still returned
                # {"status": "resolved"} -- a status inside TRIAGE_ANSWERED, so #72's
                # backstop stayed quiet too. An unanswered escalation reported as
                # answered, with the only record filed under the conductor.
                subject = triage_subject(event) or str(args.get("run_id") or "")
                _record_stage_event(c["ddb"], subject or str(event.get("run_id") or ""),
                                    "orchestrator", "EscalationResolved",
                                    {"decision": args.get("decision"),
                                     "rationale": str(args.get("rationale", ""))[:500],
                                     "adjusted_params": args.get("adjusted_params") or {}})
                if not subject:
                    # No subject means there is no mailbox to park a verdict in, so this
                    # call cannot resolve anything. Rejected into the same turn rather
                    # than skipped: the old `if subject:` fell through to
                    # {"status": "resolved"}, reporting a resolution that never happened
                    # and satisfying the backstop on the way out.
                    messages = _tool_result_content(tu, {
                        "status": "rejected",
                        "reason": ("this invocation names no run to resolve, so the "
                                   "verdict has no mailbox and would change nothing. "
                                   "Call page_human with your decision brief instead.")})
                    continue
                parked = put_directive(
                    c["ddb"], subject,
                    decision=str(args.get("decision", "")),
                    rationale=str(args.get("rationale", "")),
                    adjusted_params=args.get("adjusted_params") or {},
                    actor="conductor")
                if not parked["reachable"]:
                    # Rejected back to the conductor, not returned as a verdict:
                    # a rejection it can still act on in the SAME turn. Returning
                    # here would end the triage having done nothing, which is the bug.
                    #
                    # The rejection names only exits that can actually SUCCEED on
                    # this invocation. It used to name launch_run unconditionally --
                    # but on the bus-triage path launch_run has no approval record to
                    # verify against and always refuses (see dispatch_is_possible),
                    # so the conductor was handed two doors of which one was painted
                    # on. Live: 4 of 9 triaged escalations produced no page at all.
                    can_dispatch = dispatch_is_possible(event)
                    messages = _tool_result_content(tu, {
                        "status": "undeliverable",
                        "run_status": parked["run_status"],
                        "can_dispatch": can_dispatch,
                        "reason": (
                            f"run {subject} is {parked['run_status']}: its execution "
                            "has ended, so no agent will ever read this directive. "
                            "The decision is recorded for audit but CHANGES NOTHING. "
                            + ("To act on it, relaunch the work with launch_run "
                               "(carrying your adjusted_params), or call page_human "
                               "if that is above your authority."
                               if can_dispatch else
                               "This invocation carries no signed approval, so "
                               "launch_run CANNOT dispatch a replacement run from "
                               "here -- only a human can authorize one. Call "
                               "page_human now with your decision as the "
                               "recommendation: situation, options, and the "
                               "adjusted_params you would have applied. That is the "
                               "only exit that changes anything from here."))})
                    continue
                _ack_terminal(c, event, sess, tu, {"status": "recorded"},
                              "the verdict was recorded and parked for the subject run")
                # `subject`, not args["run_id"]: this dict is the driver's own return
                # value and the console renders it, so echoing the agent's copy would
                # report a resolution against whichever id the model happened to send.
                return {"status": "resolved", "decision": args.get("decision"),
                        "run_id": subject}
            if name == "page_human":
                # The conductor's above-authority exit, and one of the two paths the
                # undeliverable-verdict rejection above tells it to take. Unserviced on
                # this path until now: declared on the harness since Phase 5, handled
                # only by the console chat worker, so every triage page answered
                # "unsupported" and the owner was never told. Terminal for the turn --
                # a page is a handoff, so continuing to prompt the agent would have it
                # keep deciding after it just said it could not.
                result = handle_page_human(c, event, args)
                if not result["ok"]:
                    messages = _tool_result_content(tu, {
                        "status": "rejected", "reason": result["reason"]})
                    continue
                if event.get("task_token"):
                    # D12: terminal-for-the-turn is right for a TRIAGE, whose whole job
                    # was to decide whether to page, and catastrophic for a stage. This
                    # invocation is holding a state-machine task token; _ack_terminal
                    # does NOT settle it, so returning here would leave EvalGate waiting
                    # on a token no live driver will ever settle -- a run that hangs for
                    # TimeoutSeconds (86400) after successfully asking its question. The
                    # page is a notification, not an exit: keep the turn, and let the
                    # agent choose checkpoint (wait, answerable) or escalate_human (give
                    # up, terminal) with the consequence spelled out.
                    paged_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                    wait_turns = 0
                    messages = _tool_result_content(tu, {
                        "status": "paged",
                        "delivered": "SNS to the run owner, plus a HumanPaged row in "
                                     "this run's own timeline",
                        "next": "this page did NOT pause anything: your invocation is "
                                "still holding the state machine's task token, and the "
                                "stage stays in progress. To WAIT for the answer, call "
                                "checkpoint -- a verdict arrives as the `directive` in a "
                                "checkpoint result, and only a checkpoint can receive "
                                "one. Do not call escalate_human to wait: that ends the "
                                "run and makes the answer undeliverable."})
                    continue
                _ack_terminal(c, event, sess, tu, {"status": "paged"},
                              "the page was delivered to the run owner")
                return {"status": "paged", "run_id": result["run_id"]}
            if name == "write_report":
                # trust-but-verify, same as every artifact claim
                missing = verify_outputs(c["s3"], [args.get("report_uri", "")])
                if missing:
                    messages = _tool_result_content(tu, {
                        "status": "rejected",
                        "reason": f"report_uri not in S3: {missing}. Write it first."})
                    continue
                _ack_terminal(c, event, sess, tu, {"status": "recorded"},
                              "the report was verified in S3")
                return {"status": "completed", "report_uri": args.get("report_uri"),
                        "headline": str(args.get("headline", ""))[:300]}
            if name == "launch_run":
                # The conductor's dispatch tool, serviced through the same shared
                # module the console's Tasks tab uses — one implementation, no drift.
                # From Phase 5 until this branch existed, launch_run fell through to
                # the unknown-tool fallthrough below and every conductor plan died
                # with {"status": "unsupported"}.
                result = conductor_tools.service_launch_run(
                    c["lambda"], c["s3"], _kms(c), args,
                    os.environ.get("START_FN", "llmops-start-pipeline"),
                    expected=(event.get("params") or {}).get("approval_context"))
                if not result["ok"]:
                    messages = _tool_result_content(tu, {
                        "status": "rejected", "reason": result["reason"]})
                    continue
                _ack_terminal(c, event, sess, tu,
                              {"status": "dispatched", "run_id": result["run_id"]},
                              f"run {result['run_id']} was already dispatched")
                return {"status": "dispatched", "run_id": result["run_id"],
                        "manifest_uri": result["manifest_uri"],
                        "execution_arn": result["execution_arn"]}
            if name in FINOPS_TERMINAL_TOOLS and _is_finops(event):
                result = handle_finops_tool(c, event, name, args)
                if not result["ok"]:
                    messages = _tool_result_content(tu, {
                        "status": "rejected", "reason": result["reason"]})
                    continue
                # flag_variance is a FINDING, not the end of the task: an audit can
                # flag several runs in one turn, so acknowledge and let it continue.
                # The report/rate-card calls are terminal.
                if name == "flag_variance":
                    messages = _tool_result_content(tu, {"status": "recorded"})
                    continue
                _ack_terminal(c, event, sess, tu, {"status": "recorded"},
                              f"{name} was serviced and its artifact verified")
                return {"status": "completed", "tool": name, **result["args"]}
            # unknown tool — acknowledge and continue rather than dying
            messages = _tool_result_content(tu, {"status": "unsupported"})
            continue

        # A platform-suppressed turn is not the agent's answer. Live: EvalGate of
        # run-20260811T101948Z-f9d34d27 resumed after a between-turns handoff and the
        # very next turn came back stop_reason=content_filtered with ZERO text and no
        # tool -- the model was gagged, not silent. re_asks was already at its cap
        # from two real prose turns, so the empty turn fell through to the exhaustion
        # branch below and the stage died MissingStageComplete, blaming the agent for
        # a sentence the platform swallowed. The two failures need different names:
        # prose exhaustion is the agent declining protocol; this is the protocol being
        # unavailable. So a filtered turn gets its own bounded budget (it does not
        # consume re_asks -- the agent said nothing to be re-asked about), and when
        # THAT runs out the token settles as ContentFiltered, a cause an operator can
        # act on, instead of MissingStageComplete, which sends them hunting through a
        # transcript for prose that never existed.
        if not out["text"] and out["stop_reason"] in ("content_filtered",
                                                      "guardrail_intervened"):
            if filtered_turns < 2:
                filtered_turns += 1
                print(f"[driver] turn was suppressed by the platform "
                      f"({out['stop_reason']}); asking again "
                      f"({filtered_turns}/2, re_asks untouched)")
                messages = _user_text(
                    "Your previous reply was suppressed by the platform content "
                    "filter and was NEVER delivered -- nothing you just wrote was "
                    "seen or recorded. Do not repeat it verbatim. State your "
                    "conclusion plainly, without quoting raw data, and end this "
                    "turn with the required inline-function call.")
                continue
            # Re-asking in THIS session has failed twice: the model keeps trying to
            # restate the gagged text sitting in its own context, and the filter
            # keeps acting on it. Live (EvalScore, run-9d995076): the agent had
            # already WRITTEN report.json and every artifact — three suppressed
            # summary turns killed a stage whose work was complete on S3. So before
            # settling, spend one fresh session (no gagged context to restate) whose
            # first turn must attest through the tool channel — URIs and numbers —
            # or escalate. See CF_RECOVERY for why this works and why it does not
            # weaken attestation: the claims it produces still go through
            # handle_stage_complete's head-check like any other.
            if not cf_recovery:
                cf_recovery = 1
                print(f"[driver] {filtered_turns + 1} consecutive suppressed turns "
                      "-- rolling to a fresh session for a tool-channel-only "
                      "recovery attempt")
                _roll_session()
                messages = _user_text(json.dumps(payload, default=str) + CF_RECOVERY)
                continue
            if event.get("task_token"):
                settle_token(c["sfn"], event["task_token"],
                             error="ContentFiltered",
                             cause=f"{filtered_turns + 1} consecutive turns were "
                                   f"suppressed by the platform "
                                   f"({out['stop_reason']}), and a fresh-session "
                                   "recovery attempt was also suppressed; no agent "
                                   "output was delivered")
            ev.emit_event(os.environ["EVENT_BUS"], ev.PIPELINE_FAILED,
                          {"run_id": event["run_id"], "stage": event["stage"],
                           "reason": "content filtered"}, client=c["events"])
            return {"status": "failed", "reason": "content_filtered"}
        filtered_turns = 0

        # Stream ended without a tool call.
        if re_asks < 2:
            re_asks += 1
            messages = _user_text(RE_ASK)
            continue

        # Re-asks exhausted → treat as stage failure.
        if event.get("task_token"):
            settle_token(c["sfn"], event["task_token"],
                         error="MissingStageComplete", cause=out["text"][:250])
        ev.emit_event(os.environ["EVENT_BUS"], ev.PIPELINE_FAILED,
                      {"run_id": event["run_id"], "stage": event["stage"],
                       "reason": "missing stage_complete"}, client=c["events"])
        return {"status": "failed", "reason": "missing stage_complete",
                "text_tail": out["text"][-500:]}
