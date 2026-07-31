"""Unit tests for the orchestration spine — no AWS calls, all clients injected.

Covers: contracts (events, normalize, report), the harness driver's full
inline-function loop (stage_complete verify/reject, job_launched release,
escalate, re-ask, stream salvage), start/resume/webhook Lambdas, and the
state machine document (remediation loop wiring, event vocabulary, token
plumbing).

Run: .venv/bin/python -m pytest tests/test_orchestration.py -q
"""
from __future__ import annotations

import importlib.util
import json
import re
import os
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from pipeline.contracts import events as ev
from pipeline.contracts.report import (build_run_report, normalize_stage_complete,
                                       write_run_report)


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


driver = _load("harness_driver", "orchestration/harness_driver/handler.py")
start_pipeline = _load("start_pipeline", "orchestration/start_pipeline/handler.py")
resume_pipeline = _load("resume_pipeline", "orchestration/resume_pipeline/handler.py")
webhook = _load("webhook", "orchestration/webhook/handler.py")

ENV = {
    "RUNS_TABLE": "llmops-pipeline-runs",
    "EVENTS_TABLE": "llmops-stage-events",
    "EVENT_BUS": "llmops-pipeline",
    "LLMOPS_SNS_TOPIC": "arn:aws:sns:us-east-1:123456789012:llmops-escalations",
    "DATA_BUCKET": "llmops-data-test",
    "STATE_MACHINE_ARN": "arn:aws:states:us-east-1:123456789012:stateMachine:llmops",
    "WEBHOOK_SECRET_ID": "llmops/webhook",
    "START_PIPELINE_FN": "llmops-start-pipeline",
    "HARNESS_ARN_LLMOPS_DATA_PREP": "arn:aws:bedrock-agentcore:us-east-1:123456789012:harness/llmops_data_prep-TESTSUFFIX",
    "HARNESS_ARN_LLMOPS_FINETUNE": "arn:aws:bedrock-agentcore:us-east-1:123456789012:harness/llmops_finetune-TESTSUFFIX",
    "HARNESS_ARN_LLMOPS_EVAL": "arn:aws:bedrock-agentcore:us-east-1:123456789012:harness/llmops_eval-TESTSUFFIX",
}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
def _cond_matches(cond, item) -> bool:
    """Evaluate the subset of boto3 Key conditions this suite builds.

    The stub used to ignore KeyConditionExpression entirely and hand back every row,
    which let a query addressed to one run see another run's items -- precisely the
    mis-delivery the directive tests are here to catch. A fake that answers every
    question the same way cannot fail the test that matters."""
    expr = cond.get_expression()
    op, values = expr["operator"], expr["values"]
    if op == "AND":
        return all(_cond_matches(v, item) for v in values)
    name, target = values[0].name, values[1]
    actual = item.get(name)
    if op == "=":
        return actual == target
    if op == "begins_with":
        return str(actual).startswith(str(target))
    raise AssertionError(f"fake table cannot evaluate operator {op!r}")


class FakeTable:
    def __init__(self):
        self.items, self.updates = [], []
        self.query_result = []

    def put_item(self, Item):
        self.items.append(Item)

    def update_item(self, **kw):
        self.updates.append(kw)
        # Apply simple `SET a = :x, b = :y` so a read-after-write sees the write. The
        # delivered-once guarantee is enforced by a ConditionExpression, so a stub that
        # recorded updates without applying them made an every-checkpoint replay look
        # like correct behaviour.
        target = None
        for item in self.items:
            if all(item.get(k) == v for k, v in (kw.get("Key") or {}).items()):
                target = item
                break
        if target is None:
            return {}
        vals = kw.get("ExpressionAttributeValues") or {}
        cond = kw.get("ConditionExpression")
        if isinstance(cond, str) and "=" in cond:
            attr, _, placeholder = (p.strip() for p in cond.partition("="))
            if target.get(attr) != vals.get(placeholder):
                raise Exception("ConditionalCheckFailedException")
        expr = kw.get("UpdateExpression", "")
        if expr.upper().startswith("SET"):
            for clause in expr[3:].split(","):
                lhs, _, rhs = (p.strip() for p in clause.partition("="))
                if rhs in vals:
                    target[lhs] = vals[rhs]
        return {}

    def query(self, **kw):
        # query_result is the scripted-GSI path (resume_pipeline looks a run up by job
        # name). With nothing scripted, a query reads back what was written, filtered by
        # the key condition -- the directive channel writes and re-reads this table.
        if self.query_result:
            return {"Items": self.query_result}
        cond = kw.get("KeyConditionExpression")
        items = [i for i in self.items if cond is None or _cond_matches(cond, i)]
        return {"Items": items}


class FakeDDB:
    def __init__(self):
        self.tables = {}

    def Table(self, name):
        return self.tables.setdefault(name, FakeTable())


class FakeS3:
    def __init__(self, existing=()):
        self.existing = set(existing)
        self.objects = {}

    def head_object(self, Bucket, Key):
        if f"s3://{Bucket}/{Key}" not in self.existing:
            raise Exception("404")

    def put_object(self, Bucket, Key, Body, **kw):
        self.objects[f"s3://{Bucket}/{Key}"] = Body

    def get_object(self, Bucket, Key):
        body = self.objects[f"s3://{Bucket}/{Key}"]

        class _B:
            def read(self):
                return body if isinstance(body, bytes) else body.encode()
        return {"Body": _B()}


class FakeSfn:
    def __init__(self):
        self.successes, self.failures, self.executions = [], [], []

    def send_task_success(self, **kw):
        self.successes.append(kw)

    def send_task_failure(self, **kw):
        self.failures.append(kw)

    def start_execution(self, **kw):
        self.executions.append(kw)
        return {"executionArn": "arn:aws:states:::execution/test"}


class FakeEvents:
    def __init__(self):
        self.entries = []

    def put_events(self, Entries):
        self.entries.extend(Entries)
        return {"FailedEntryCount": 0}


class FakeSns:
    def __init__(self):
        self.published = []

    def publish(self, **kw):
        self.published.append(kw)


class FakeAgentCore:
    """Scripted invoke_harness: pops the next canned stream per call."""

    def __init__(self, scripts):
        self.scripts = list(scripts)
        self.calls = []

    def invoke_harness(self, **kw):
        self.calls.append(kw)
        if not self.scripts:
            return {"stream": []}
        return {"stream": self.scripts.pop(0)}


def tool_use_stream(name, args):
    return [
        {"contentBlockStart": {"start": {"toolUse": {"toolUseId": "tu-1", "name": name}}}},
        {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(args)}}}},
        {"messageStop": {"stopReason": "tool_use"}},
    ]


def text_stream(text):
    return [
        {"contentBlockDelta": {"delta": {"text": text}}},
        {"messageStop": {"stopReason": "end_turn"}},
    ]


class DyingStream:
    """Iterable that raises mid-stream — a production stream death."""

    def __iter__(self):
        yield {"contentBlockDelta": {"delta": {"text": "partial"}}}
        raise ConnectionError("reset by peer")


def clients(agentcore=None, s3=None):
    return {
        "agentcore": agentcore or FakeAgentCore([]),
        "ddb": FakeDDB(),
        "s3": s3 or FakeS3(),
        "sfn": FakeSfn(),
        "sns": FakeSns(),
        "events": FakeEvents(),
        "lambda": None,
    }


def driver_event(**over):
    base = {"run_id": "run-test-1", "stage": "data-prep", "task": "generate",
            "harness_id": "llmops_data_prep",
            "manifest_uri": "s3://llmops-data-test/runs/run-test-1/manifest.json",
            "task_token": "tok-123", "iteration": 0}
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# Contracts
# ---------------------------------------------------------------------------
class TestContracts:
    def test_all_stage_event_map_values_are_known_events(self):
        for detail_type in driver.STAGE_EVENT_MAP.values():
            assert detail_type in ev.ALL_EVENTS

    def test_emit_event_rejects_unknown_detail_type(self):
        with pytest.raises(ValueError):
            ev.emit_event("bus", "NotAnEvent", {}, client=FakeEvents())

    def test_normalize_alias_drift(self):
        norm = normalize_stage_complete(
            {"stage": "eval", "task": "gate", "artifacts": "s3://b/one",
             "results": {"win_rate": 0.9}, "summary": "ok"})
        assert norm["outputs"] == ["s3://b/one"]
        assert norm["metrics"] == {"win_rate": 0.9}
        assert norm["evidence"] == "ok"

    def test_normalize_empty_outputs_is_valid(self):
        norm = normalize_stage_complete({"outputs": [], "artifacts": ["s3://b/x"]})
        assert norm["outputs"] == []  # presence wins over later alias

    def test_report_counts_and_findings(self):
        manifest = {"run_id": "r1", "stages": {
            "data-prep": {"status": "completed"},
            "eval": {"status": "failed", "evidence": "gate 0.6 < 0.8"}}}
        report = build_run_report(manifest)
        assert report["pass_counts"] == {"total": 2, "passed": 1, "failed": 1}
        assert report["findings"][0]["stage"] == "eval"

    def test_write_run_report_targets_console_key(self):
        s3 = FakeS3()
        write_run_report(s3, "llmops-data-test", {"run_id": "r1", "stages": {}})
        assert ("s3://llmops-data-test/reports/run-latest/test-report-latest.json"
                in s3.objects)


# ---------------------------------------------------------------------------
# Harness driver
# ---------------------------------------------------------------------------
class TestDriver:
    def test_session_id_deterministic_and_min_length(self):
        a = driver.session_id("r", "s", "t")
        assert a == driver.session_id("r", "s", "t")
        assert len(a) >= 33

    def test_stage_complete_happy_path(self):
        uri = "s3://llmops-data-test/runs/run-test-1/raw/data.jsonl"
        ac = FakeAgentCore([
            tool_use_stream("stage_complete",
                            {"stage": "data-prep", "task": "generate",
                             "outputs": [uri], "metrics": {"count": 2000}}),
            text_stream("ack")])
        c = clients(ac, FakeS3(existing=[uri]))
        # seed the manifest the driver loads for the canonical report
        c["s3"].objects["s3://llmops-data-test/runs/run-test-1/manifest.json"] = json.dumps(
            {"run_id": "run-test-1", "stages": {}})
        out = driver.handler(driver_event(), clients=c)
        assert out["status"] == "completed"
        assert c["sfn"].successes and not c["sfn"].failures
        payload = json.loads(c["sfn"].successes[0]["output"])
        assert payload["gate_passed"] is True  # non-gate stages default True
        assert any(e["DetailType"] == ev.DATASET_GENERATED for e in c["events"].entries)
        # canonical report written by the driver, not the agent
        assert ("s3://llmops-data-test/reports/run-latest/test-report-latest.json"
                in c["s3"].objects)

    def test_stage_complete_rejected_when_outputs_missing(self):
        uri = "s3://llmops-data-test/runs/run-test-1/raw/missing.jsonl"
        ac = FakeAgentCore([
            tool_use_stream("stage_complete", {"outputs": [uri]}),
            tool_use_stream("stage_complete", {"outputs": []}),
            text_stream("ack")])
        c = clients(ac, FakeS3())  # nothing exists in S3
        out = driver.handler(driver_event(), clients=c)
        # first claim rejected, second (empty-but-valid) accepted
        assert out["status"] == "completed"
        # messages == [assistant toolUse echo, user toolResult] -- the resume
        # contract; the result is in the LAST message, not the first.
        rejection = ac.calls[1]["messages"][-1]["content"][0]["toolResult"]
        # text block, not a json block: the harness runtime rejects content_type json
        body = json.loads(rejection["content"][0]["text"])
        assert body["status"] == "rejected"

    def test_tool_result_resume_echoes_the_tooluse_first(self):
        """InvokeHarness resumes a paused inline function only when handed BOTH an
        assistant message echoing the toolUse and a user message with the matching
        toolResult. A lone toolResult is rejected with "The number of toolResult
        blocks at messages.N.content exceeds the number of toolUse blocks of previous
        turn" -- the runtime's history has no call to answer. Live, that broke four
        consecutive dispatch sessions on the console's copy of this loop; here it hid
        behind the stream-retry, a rejected result looking like stream death."""
        uri = "s3://llmops-data-test/runs/run-test-1/raw/data.jsonl"
        ac = FakeAgentCore([
            tool_use_stream("stage_complete", {"outputs": [uri]}),
            text_stream("ack")])
        s3 = FakeS3()
        s3.objects[uri] = b"{}"
        c = clients(ac, s3)
        driver.handler(driver_event(), clients=c)

        resume = ac.calls[1]["messages"]
        assert [m["role"] for m in resume] == ["assistant", "user"]
        echo = resume[0]["content"][0]["toolUse"]
        result = resume[1]["content"][0]["toolResult"]
        assert echo["name"] == "stage_complete"
        assert echo["toolUseId"] == result["toolUseId"], \
            "a mismatched toolUseId makes the runtime reject the resume"
        assert echo["input"] == {"outputs": [uri]}

    def test_self_reinvoke_between_turns_carries_the_pending_messages(self):
        """A harness turn can run 840s and the Lambda dies at 900s, so only one turn
        fits per invocation: when time runs out the driver re-invokes itself carrying
        the pending messages. That path had NO test, and a rename of the loop variable
        (content -> messages, for the two-message resume contract) left the closure
        referencing the old name -- "NameError: cannot access free variable 'content'"
        killed a live run at DataPrepGenerate AFTER a correct dispatch. Anything the
        state machine can reach needs coverage, especially the timeout paths that only
        fire in production."""
        class _Ctx:
            function_name = "llmops-harness-driver"

            def __init__(self):
                self.calls = 0

            def get_remaining_time_in_millis(self):
                # turn 1 always runs (the loop only checks before LATER turns); by the
                # time turn 2 would start there is not enough left for another 840s turn
                self.calls += 1
                return 10_000

        class _Lam:
            def __init__(self):
                self.invocations = []

            def invoke(self, **kw):
                self.invocations.append(kw)
                return {"StatusCode": 202}

        # a checkpoint keeps the loop going, so turn 2 is attempted and hits the wall
        ac = FakeAgentCore([tool_use_stream("checkpoint", {"next_action": "keep going"}),
                            text_stream("never reached")])
        c = clients(ac)
        c["lambda"] = _Lam()
        out = driver.handler(driver_event(), clients=c, context=_Ctx())

        assert out["status"] == "self_reinvoked_between_turns"
        payload = json.loads(c["lambda"].invocations[0]["Payload"])
        cont = payload["_continuation"]
        # the continuation is a full messages list, and it is the pending toolResult
        # resume -- not a bare content block, or the resumed invocation re-sends the
        # wrong shape and the harness rejects it
        assert isinstance(cont, list) and cont, "continuation must carry the messages"
        assert [m["role"] for m in cont] == ["assistant", "user"]
        assert "toolUse" in cont[0]["content"][0]
        assert "toolResult" in cont[1]["content"][0]

    def test_the_driver_has_exactly_one_way_to_hand_a_turn_to_the_next_invocation(self):
        """The checkpoint branch had its OWN reinvoke that sent {"_resumed": True} -- a
        key nothing in the handler reads. The resumed invocation therefore fell through
        to the fresh-start branch, re-sent the original stage prompt, and silently
        dropped both the pending toolResult and work already paid for (live: a budget
        escalation raised after a pilot found the plan's token estimate 6.5x low).

        Two reinvoke sites meant two chances to forget the payload contract, and the
        second one forgot. Now there is one, so this asserts the shape rather than the
        behaviour: every self-invoke goes through _self_reinvoke, and _continuation is
        the only handoff key -- if a future branch invents another, it must teach the
        resume branch to read it."""
        src = (REPO / "orchestration/harness_driver/handler.py").read_text()
        body = src[src.index("def _run_stage") if "def _run_stage" in src else 0:]
        # the resume branch reads exactly one key; nothing may hand off via another
        assert body.count('event.get("_continuation")') == 1
        assert "_resumed" not in body.split("# reinvoke. This branch used to")[0], \
            "a reinvoke is carrying a key the resume branch does not read"
        # every self-invoke of this same function goes through the one closure
        self_invokes = re.findall(r"FunctionName=context\.function_name", body)
        assert len(self_invokes) == 1, (
            f"{len(self_invokes)} self-invoke sites -- there must be exactly one, "
            "_self_reinvoke, so the handoff payload is defined in one place")

    def test_a_crash_while_holding_the_task_token_fails_the_token_instead_of_parking_it(self):
        """A synchronous stage invocation that raises is reported to Step Functions by
        the Lambda integration itself, which is why the NameError above surfaced in
        seconds. An ASYNCHRONOUS continuation has no such reporter: the state machine
        is waiting on the task token, not on this invocation, so an exception here is
        written to CloudWatch and to nobody else. The token parks until TimeoutSeconds
        -- 7200s for DataPrepGenerate, six hours for FinetuneLaunch.

        Live: the driver's missing s3:PutObject grant crashed the final
        stage_complete of run-...-8b864805. The work was done and the report written
        nowhere; the run then sat 'running' for 90 minutes holding a token, and the
        two Lambda retries crashed the same way in the same silence. The AccessDenied
        was one bug; the 90 minutes was this one.

        So: whenever the driver holds a task token, an unexpected exception must fail
        the token on the way out. The stage still fails -- it just fails in seconds,
        with the real cause attached, and the state machine's own failure path (which
        closes the run record out) runs immediately.
        """
        class _Ctx:
            function_name = "llmops-harness-driver"
            def get_remaining_time_in_millis(self):
                return 900_000

        uri = "s3://llmops-test/runs/r/out.json"

        class _DeniedWrite(FakeS3):
            """Reads fine, denies the report write -- the live shape exactly: the
            agent's outputs verified, then put_object raised on the ONE key the
            driver's role had no grant for."""
            def put_object(self, Bucket, Key, Body, **kw):
                raise RuntimeError(
                    "An error occurred (AccessDenied) when calling the PutObject "
                    f"operation: s3:PutObject on {Bucket}/{Key}")

        ac = FakeAgentCore([tool_use_stream("stage_complete", {"outputs": [uri]}),
                            text_stream("ack")])
        c = clients(ac)
        c["s3"] = _DeniedWrite(existing=[uri])
        # the manifest must load, or the driver never reaches the report write --
        # which is where the live grant was missing
        c["s3"].objects["s3://llmops-data-test/runs/run-test-1/manifest.json"] = json.dumps(
            {"run_id": "run-test-1", "stages": {}})
        event = driver_event()
        event["task_token"] = "tok-parked"

        with pytest.raises(Exception):
            driver.handler(event, clients=c, context=_Ctx())

        failures = c["sfn"].failures
        assert failures, (
            "the driver crashed while holding a task token and told Step Functions "
            "nothing -- the token parks until TimeoutSeconds (7200s for data-prep, "
            "21600s for finetune) while the run record still says 'running'")
        assert failures[0]["taskToken"] == "tok-parked"
        cause = json.dumps(failures[0])
        assert "AccessDenied" in cause or "PutObject" in cause, (
            "the failure must carry the real cause; 'the stage failed' with the "
            "reason only in CloudWatch is what made this take a night to find")

    def test_a_crash_with_no_task_token_is_still_raised_to_the_caller(self):
        """The console's dispatch path invokes the driver synchronously with no token.
        Swallowing the exception there would turn a hard failure into a silent success,
        which is worse than the parked token. Re-raise unless the token was settled."""
        class _Boom(FakeS3):
            def put_object(self, Bucket, Key, Body, **kw):
                raise RuntimeError("kaboom")

        ac = FakeAgentCore([tool_use_stream("stage_complete", {"outputs": ["s3://b/k"]}),
                            text_stream("ack")])
        c = clients(ac)
        c["s3"] = _Boom(existing=["s3://b/k"])
        c["s3"].objects["s3://llmops-data-test/runs/run-test-1/manifest.json"] = json.dumps(
            {"run_id": "run-test-1", "stages": {}})
        event = driver_event()
        event.pop("task_token", None)
        with pytest.raises(Exception, match="kaboom"):
            driver.handler(event, clients=c, context=None)

    def test_gate_null_gate_passed_fails_closed(self):
        """A gate stage whose agent omits/nulls gate_passed must NOT promote (fail closed)."""
        ac = FakeAgentCore([
            tool_use_stream("stage_complete",
                            {"outputs": [], "metrics": {"gate_passed": None, "needs_human": True}}),
            text_stream("ack")])
        c = clients(ac)
        out = driver.handler(driver_event(stage="eval", task="gate"), clients=c)
        assert out["status"] == "completed"
        payload = json.loads(c["sfn"].successes[0]["output"])
        assert payload["gate_passed"] is False  # null != pass

    def test_gate_fail_emits_quality_gate_failed_and_flags_token(self):
        ac = FakeAgentCore([
            tool_use_stream("stage_complete",
                            {"outputs": [], "metrics": {"gate_passed": False}}),
            text_stream("ack")])
        c = clients(ac)
        out = driver.handler(driver_event(stage="eval", task="gate"), clients=c)
        assert out["status"] == "completed"
        assert any(e["DetailType"] == ev.QUALITY_GATE_FAILED for e in c["events"].entries)
        payload = json.loads(c["sfn"].successes[0]["output"])
        assert payload["gate_passed"] is False  # drives QualityGateChoice -> remediation

    def test_job_launched_parks_token_and_releases(self):
        ac = FakeAgentCore([
            tool_use_stream("job_launched", {"job_name": "llmops-qlora-1"}),
            text_stream("released")])
        c = clients(ac)
        out = driver.handler(driver_event(stage="finetune", task="launch"), clients=c)
        assert out["status"] == "released"
        assert not c["sfn"].successes  # token NOT settled — resume λ owns it
        parked = c["ddb"].Table(ENV["RUNS_TABLE"]).updates[0]
        assert parked["ExpressionAttributeValues"][":j"] == "llmops-qlora-1"
        assert parked["ExpressionAttributeValues"][":t"] == "tok-123"
        assert any(e["DetailType"] == ev.TRAINING_STARTED for e in c["events"].entries)

    def test_escalate_human_notifies_and_fails_token(self):
        ac = FakeAgentCore([
            tool_use_stream("escalate_human", {"reason": "irrecoverable data drift"}),
            text_stream("ack")])
        c = clients(ac)
        out = driver.handler(driver_event(), clients=c)
        assert out["status"] == "escalated"
        assert c["sns"].published and c["sfn"].failures
        assert c["sfn"].failures[0]["error"] == "EscalatedToHuman"

    def test_a_checkpoint_delivers_any_human_directive_waiting_for_this_run(self):
        """A stage agent's only way to ask a blocking question mid-run is checkpoint,
        and the driver answered it {"status": "continue"} unconditionally -- so a human
        answer had NO path back to the agent that asked.

        Live consequence: data-prep piloted teacher generation, measured 13.5k output
        tokens/attempt against the approved plan's assumed 1,800, wrote a four-option
        budget escalation, and then kept spending under the cap it had just proven
        infeasible -- because "continue" was the only word the driver could say. The
        conductor's resolve_escalation wrote an EscalationResolved stage-event that
        nothing read: a verdict into the void, the same shape as the escalation SNS
        topic with no subscribers.

        A checkpoint is now the delivery point: pending directives for this run ride
        back in the toolResult, so the agent learns the verdict on its next breath."""
        ac = FakeAgentCore([tool_use_stream("checkpoint", {"progress_uri": "s3://b/p.json"}),
                            tool_use_stream("stage_complete", {"outputs": []}),
                            text_stream("ack")])
        c = clients(ac)
        driver.put_directive(c["ddb"], "run-test-1", decision="option_A",
                             rationale="teacher line item raised to $13; lower the "
                                       "coverage gate to 25% and say so in the report",
                             adjusted_params={"teacher_cap_usd": 13},
                             actor="tmwu")
        driver.handler(driver_event(), clients=c)

        answer = json.loads(
            ac.calls[1]["messages"][-1]["content"][0]["toolResult"]["content"][0]["text"])
        assert answer["status"] == "directive", \
            "a checkpoint with a directive waiting must not answer a bare 'continue'"
        d = answer["directive"]
        assert d["decision"] == "option_A"
        assert "coverage gate" in d["rationale"]
        assert d["adjusted_params"] == {"teacher_cap_usd": 13}
        assert d["actor"] == "tmwu"

    def test_a_directive_is_delivered_once_so_it_cannot_be_replayed_forever(self):
        """Two checkpoints in one turn must not see the same verdict twice: a directive
        redelivered every checkpoint reads as a fresh instruction each time, and an
        agent told "raise the cap to $13" on every breath will raise it repeatedly."""
        ac = FakeAgentCore([tool_use_stream("checkpoint", {"progress_uri": "s3://b/1.json"}),
                            tool_use_stream("checkpoint", {"progress_uri": "s3://b/2.json"}),
                            tool_use_stream("stage_complete", {"outputs": []}),
                            text_stream("ack")])
        c = clients(ac)
        driver.put_directive(c["ddb"], "run-test-1", decision="option_A", rationale="go")
        driver.handler(driver_event(), clients=c)

        first = json.loads(
            ac.calls[1]["messages"][-1]["content"][0]["toolResult"]["content"][0]["text"])
        second = json.loads(
            ac.calls[2]["messages"][-1]["content"][0]["toolResult"]["content"][0]["text"])
        assert first["status"] == "directive"
        assert second["status"] == "continue", \
            "the second checkpoint replayed an already-delivered directive"

    def test_a_checkpoint_with_no_directive_still_just_continues(self):
        """The common case must stay free of ceremony -- and must not break when the
        directives table is unreachable, or a DDB hiccup would stall every run that
        merely wanted another turn."""
        ac = FakeAgentCore([tool_use_stream("checkpoint", {"progress_uri": "s3://b/p.json"}),
                            tool_use_stream("stage_complete", {"outputs": []}),
                            text_stream("ack")])
        c = clients(ac)
        out = driver.handler(driver_event(), clients=c)
        answer = json.loads(
            ac.calls[1]["messages"][-1]["content"][0]["toolResult"]["content"][0]["text"])
        assert answer == {"status": "continue"}
        assert out["status"] == "completed"

    def test_resolve_escalation_writes_a_directive_the_waiting_agent_can_read(self):
        """resolve_escalation is the conductor's verdict tool. It recorded a stage-event
        and stopped there -- audit trail, no delivery. It must now also put the verdict
        on the channel a paused agent actually reads, or triage remains advice nobody
        hears."""
        ac = FakeAgentCore([
            tool_use_stream("resolve_escalation",
                            {"run_id": "run-stuck-9", "decision": "option_B",
                             "rationale": "3 attempts/task approved at ~$39",
                             "adjusted_params": {"teacher_cap_usd": 39}}),
            text_stream("ack")])
        c = clients(ac)
        out = driver.handler(driver_event(stage="orchestrator", task="triage",
                                          run_id="run-orch-1"), clients=c)
        assert out["status"] == "resolved"
        pending = driver.take_directive(c["ddb"], "run-stuck-9")
        assert pending, "the verdict never reached the run it was about"
        assert pending["decision"] == "option_B"
        assert pending["adjusted_params"] == {"teacher_cap_usd": 39}

    def test_a_verdict_is_addressed_to_the_run_it_is_about_not_the_triaging_run(self):
        """The orchestrator triages OTHER runs: its own run_id must never be the
        delivery address, or the verdict lands in the conductor's own mailbox and the
        stuck run waits forever."""
        ac = FakeAgentCore([
            tool_use_stream("resolve_escalation",
                            {"run_id": "run-stuck-9", "decision": "abort"}),
            text_stream("ack")])
        c = clients(ac)
        driver.handler(driver_event(stage="orchestrator", task="triage",
                                    run_id="run-orch-1"), clients=c)
        assert driver.take_directive(c["ddb"], "run-orch-1") is None

    def test_missing_stage_complete_reasks_then_fails(self):
        ac = FakeAgentCore([text_stream("done, I think"), text_stream("still no call"),
                            text_stream("third strike")])
        c = clients(ac)
        out = driver.handler(driver_event(), clients=c)
        assert out["status"] == "failed"
        assert len(ac.calls) == 3  # original + two re-asks (continue-and-finish nudge, then final demand)
        assert "stage_complete" in ac.calls[1]["messages"][-1]["content"][0]["text"]
        assert c["sfn"].failures[0]["error"] == "MissingStageComplete"
        assert any(e["DetailType"] == ev.PIPELINE_FAILED for e in c["events"].entries)

    def test_stream_death_salvaged_same_session(self):
        uri = "s3://llmops-data-test/runs/run-test-1/raw/data.jsonl"
        ac = FakeAgentCore([
            DyingStream(),
            tool_use_stream("stage_complete", {"outputs": [uri]}),
            text_stream("ack")])
        c = clients(ac, FakeS3(existing=[uri]))
        out = driver.handler(driver_event(), clients=c)
        assert out["status"] == "completed"
        # salvage re-ask went to the SAME session
        assert ac.calls[0]["runtimeSessionId"] == ac.calls[1]["runtimeSessionId"]


# ---------------------------------------------------------------------------
# start_pipeline
# ---------------------------------------------------------------------------
class TestStartPipeline:
    def test_seeds_manifest_and_starts_execution(self):
        c = {"s3": FakeS3(), "ddb": FakeDDB(), "sfn": FakeSfn(), "events": FakeEvents()}
        out = start_pipeline.handler({"trigger_source": "scheduler"}, clients=c)
        assert out["run_id"].startswith("run-")
        manifest = json.loads(next(iter(c["s3"].objects.values())))
        assert manifest["iteration"] == 0
        assert manifest["params"]["max_iterations"] == 3
        assert manifest["models"]["teacher"].startswith("us.deepseek")
        sfn_input = json.loads(c["sfn"].executions[0]["input"])
        assert sfn_input["iteration"] == 0
        assert any(e["DetailType"] == ev.PIPELINE_STARTED for e in c["events"].entries)

    def test_params_and_plan_arriving_as_json_strings_are_parsed(self):
        """The orchestrator is a language model filling in an inline function's
        arguments, and live it passed `params` as a JSON *string* rather than an
        object. seed_manifest did `{**DEFAULT_PARAMS, **params}`, which on a str
        raises "TypeError: 'str' object is not a mapping" -- start-pipeline 500s, the
        toolResult says only "did not return a run_id", and a signed, approved plan
        never dispatches. Coerce at the boundary: this is the one place that knows
        both shapes are possible."""
        c = {"s3": FakeS3(), "ddb": FakeDDB(), "sfn": FakeSfn(), "events": FakeEvents()}
        out = start_pipeline.handler(
            {"trigger_source": "conductor",
             "params": json.dumps({"sample_count": 500, "pipeline_mode": "data_audit"}),
             "plan": json.dumps({"goal": "distill ARC solver"})}, clients=c)
        assert out["run_id"].startswith("run-")
        manifest = json.loads(next(iter(c["s3"].objects.values())))
        assert manifest["params"]["sample_count"] == 500
        assert manifest["params"]["keep_reasoning"] is True   # defaults still merge
        assert manifest["plan"]["goal"] == "distill ARC solver"
        # and the mode still reaches the Choice state, which reads the execution input
        assert json.loads(c["sfn"].executions[0]["input"])["pipeline_mode"] == "data_audit"

    def test_unparseable_params_string_is_not_silently_dropped(self):
        """A string that is not JSON must fail loudly, not vanish into defaults --
        silently running with default params would spend GPU money on a plan nobody
        approved."""
        c = {"s3": FakeS3(), "ddb": FakeDDB(), "sfn": FakeSfn(), "events": FakeEvents()}
        with pytest.raises(ValueError, match="params"):
            start_pipeline.handler(
                {"trigger_source": "conductor", "params": "not json at all"},
                clients=c)

    def test_conductor_plan_and_param_overrides_flow_into_manifest(self):
        c = {"s3": FakeS3(), "ddb": FakeDDB(), "sfn": FakeSfn(), "events": FakeEvents()}
        start_pipeline.handler(
            {"trigger_source": "conductor",
             "plan": {"goal": "distill ARC solver"},
             "params": {"sample_count": 500}}, clients=c)
        manifest = json.loads(next(iter(c["s3"].objects.values())))
        assert manifest["plan"]["goal"] == "distill ARC solver"
        assert manifest["params"]["sample_count"] == 500
        assert manifest["params"]["keep_reasoning"] is True  # default preserved


# ---------------------------------------------------------------------------
# resume_pipeline
# ---------------------------------------------------------------------------
def sm_event(status, job="llmops-qlora-1", **detail):
    return {"detail": {"TrainingJobName": job, "TrainingJobStatus": status, **detail}}


class TestResumePipeline:
    def _clients(self, run=None):
        c = {"ddb": FakeDDB(), "sfn": FakeSfn(), "events": FakeEvents()}
        table = c["ddb"].Table(ENV["RUNS_TABLE"])
        if run:
            table.query_result = [run]
        return c

    def test_completed_job_resumes_execution(self):
        c = self._clients({"run_id": "run-1", "task_token": "tok-9"})
        out = resume_pipeline.handler(
            sm_event("Completed",
                     ModelArtifacts={"S3ModelArtifacts": "s3://b/model.tar.gz"}),
            clients=c)
        assert out["outcome"] == "resumed"
        assert json.loads(c["sfn"].successes[0]["output"])["model_artifacts"].endswith(
            "model.tar.gz")
        assert any(e["DetailType"] == ev.MODEL_TRAINED for e in c["events"].entries)
        # token cleared to prevent double-settle on duplicate delivery
        assert "REMOVE task_token" in c["ddb"].Table(
            ENV["RUNS_TABLE"]).updates[0]["UpdateExpression"]

    def test_failed_job_fails_execution(self):
        c = self._clients({"run_id": "run-1", "task_token": "tok-9"})
        out = resume_pipeline.handler(
            sm_event("Failed", FailureReason="OOM on ml.g5.2xlarge"), clients=c)
        assert out["outcome"] == "failed"
        assert c["sfn"].failures[0]["error"] == "TrainingJobFailed"

    def test_unknown_job_is_skipped(self):
        c = self._clients(run=None)
        out = resume_pipeline.handler(sm_event("Completed", job="someone-elses-job"),
                                      clients=c)
        assert out["skipped"] is True
        assert not c["sfn"].successes and not c["sfn"].failures

    def test_non_terminal_status_is_skipped(self):
        c = self._clients({"run_id": "run-1", "task_token": "tok-9"})
        out = resume_pipeline.handler(sm_event("InProgress"), clients=c)
        assert out["skipped"] is True


# ---------------------------------------------------------------------------
# webhook
# ---------------------------------------------------------------------------
class TestWebhook:
    SECRET = "test-secret-not-real"

    def _clients(self):
        class FakeSM:
            def get_secret_value(inner, SecretId):
                return {"SecretString": self.SECRET}

        class FakeLambda:
            def __init__(inner):
                inner.invocations = []

            def invoke(inner, **kw):
                inner.invocations.append(kw)

                class _P:
                    def read(self_p):
                        return json.dumps({"run_id": "run-wh-1",
                                           "manifest_uri": "s3://b/m.json"}).encode()
                return {"Payload": _P()}
        webhook._secret_cache.clear()
        return {"sm": FakeSM(), "lambda": FakeLambda()}

    def _sign(self, body):
        import hashlib as h
        import hmac as hm
        return "sha256=" + hm.new(self.SECRET.encode(), body.encode(), h.sha256).hexdigest()

    def test_valid_signature_starts_pipeline(self):
        body = json.dumps({"params": {"sample_count": 100}})
        c = self._clients()
        resp = webhook.handler(
            {"body": body, "headers": {"X-Signature-256": self._sign(body)}}, clients=c)
        assert resp["statusCode"] == 202
        sent = json.loads(c["lambda"].invocations[0]["Payload"])
        assert sent["trigger_source"] == "webhook"
        assert sent["params"]["sample_count"] == 100

    def test_bad_signature_rejected(self):
        c = self._clients()
        resp = webhook.handler(
            {"body": "{}", "headers": {"X-Signature-256": "sha256=deadbeef"}}, clients=c)
        assert resp["statusCode"] == 403
        assert not c["lambda"].invocations

    def test_missing_signature_rejected(self):
        c = self._clients()
        resp = webhook.handler({"body": "{}", "headers": {}}, clients=c)
        assert resp["statusCode"] == 403


# ---------------------------------------------------------------------------
# State machine document
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def asl():
    return json.loads((REPO / "orchestration/state_machine.asl.json").read_text())


class TestStateMachine:

    def test_all_next_targets_exist(self, asl):
        states = asl["States"]
        targets = set()
        for st in states.values():
            if "Next" in st:
                targets.add(st["Next"])
            for c in st.get("Choices", []):
                targets.add(c["Next"])
            if "Default" in st:
                targets.add(st["Default"])
            for cat in st.get("Catch", []):
                targets.add(cat["Next"])
        assert asl["StartAt"] in states
        missing = targets - set(states)
        assert not missing, f"dangling Next targets: {missing}"

    def test_remediation_loop_wiring(self, asl):
        states = asl["States"]
        # gate fail -> RemediationChoice -> (iteration < 3) -> increment -> remediate
        assert states["QualityGateChoice"]["Default"] == "RemediationChoice"
        choice = states["RemediationChoice"]["Choices"][0]
        assert choice["NumericLessThan"] == 3
        assert choice["Next"] == "IncrementIteration"
        assert states["IncrementIteration"]["Next"] == "RemediateFinetune"
        # remediation loops BACK to analysis -> eval, closing the self-iteration loop
        assert states["RemediateFinetune"]["Next"] == "FinetuneAnalyze"
        assert states["FinetuneAnalyze"]["Next"] == "EvalGate"
        # budget exhausted -> escalate, never silent fail; escalation now closes the
        # run record out before failing (see MarkRunFailed)
        assert states["RemediationChoice"]["Default"] == "EscalateFail"
        assert states["EscalateFail"]["Next"] == "MarkRunFailed"
        assert states["MarkRunFailed"]["Next"] == "Fail"

    def test_every_harness_task_uses_task_token(self, asl):
        for name, st in asl["States"].items():
            if st.get("Resource", "").endswith("lambda:invoke.waitForTaskToken"):
                payload = st["Parameters"]["Payload"]
                assert payload["task_token.$"] == "$$.Task.Token", name
                assert payload["iteration.$"] == "$.iteration", name

    def test_remediate_task_reaches_finetune_harness(self, asl):
        payload = asl["States"]["RemediateFinetune"]["Parameters"]["Payload"]
        assert payload["stage"] == "finetune"
        assert payload["task"] == "remediate"
        assert payload["harness_id"] == "llmops_finetune"

    def test_terminal_events_are_known_vocabulary(self, asl):
        for state in ("Complete", "EscalateFail"):
            detail_type = asl["States"][state]["Parameters"]["Entries"][0]["DetailType"]
            assert detail_type in ev.ALL_EVENTS

    def test_a_crashed_stage_marks_the_run_failed_before_the_fail_state(self, asl):
        """A stage Lambda that CRASHES never reaches the driver, so nothing in Python
        writes a terminal status -- the run sits at status=running forever while its
        execution is FAILED. Nine live runs were zombies this way. The state machine
        itself has to close the record, because it is the only participant guaranteed
        to still be alive when a stage dies.
        """
        states = asl["States"]
        # Every stage Catch that gives up must pass through the marker rather than
        # jumping to Fail. MarkRunFailed's own Catch is the one exception: it is the
        # marker, and its fallback has nowhere left to go.
        for name, st in states.items():
            if name == "MarkRunFailed":
                continue
            for cat in st.get("Catch", []):
                assert cat["Next"] != "Fail", (
                    f"{name} catches straight to Fail -- the runs table would keep "
                    "saying 'running' after the execution is FAILED")
        assert states["EscalateFail"]["Next"] == "MarkRunFailed"
        mark = states["MarkRunFailed"]
        assert mark["Resource"] == "arn:aws:states:::aws-sdk:dynamodb:updateItem", (
            "mark the run through the AWS SDK integration, not another Lambda: a "
            "Lambda is the thing that just crashed")
        assert mark["Parameters"]["Key"]["run_id"]["S.$"] == "$.run_id"
        assert mark["Next"] == "Fail"

    def test_marking_a_run_failed_never_overwrites_a_richer_terminal_status(self, asl):
        """When the AGENT escalated, the driver already wrote status=escalated -- more
        informative than 'failed'. The condition keeps it, and the Catch means a
        rejected condition still reaches Fail instead of hanging the execution."""
        mark = asl["States"]["MarkRunFailed"]
        assert ":running" in mark["Parameters"]["ConditionExpression"] or \
               "running" in json.dumps(mark["Parameters"]["ExpressionAttributeValues"])
        assert mark["Catch"][0]["Next"] == "Fail"
        assert "States.ALL" in mark["Catch"][0]["ErrorEquals"]

    def test_the_asl_carries_no_fields_amazon_states_language_rejects(self, asl):
        """`_comment` is this repo's convention for explaining a policy document, and it
        is fine in the IAM JSONs -- ASL rejects it outright ("Field '_comment' is not
        supported"), and only at UpdateStateMachine time. Offline is where that belongs."""
        def walk(node, path):
            if isinstance(node, dict):
                for k, v in node.items():
                    assert k != "_comment", f"ASL rejects _comment at {path}"
                    walk(v, f"{path}/{k}")
            elif isinstance(node, list):
                for i, v in enumerate(node):
                    walk(v, f"{path}[{i}]")
        walk(asl, "")

    def test_the_state_machine_role_can_write_the_status_it_is_now_asked_to_write(self):
        """The grant and the ASL change are one fix: shipping the state without the
        permission turns a zombie 'running' into a FAILED execution that fails one
        state later, which is not an improvement."""
        sfn = json.loads((REPO / "deploy/iam/sfn_execution_role.json").read_text())
        acts = [a for st in sfn["permissionsPolicy"]["Statement"]
                for a in ([st["Action"]] if isinstance(st["Action"], str) else st["Action"])]
        assert "dynamodb:UpdateItem" in acts, (
            "MarkRunFailed calls dynamodb:UpdateItem with this role")

    def test_teardown_always_follows_smoke_even_on_failure(self, asl):
        smoke = asl["States"]["SmokeTest"]
        assert smoke["Next"] == "Teardown"
        assert smoke["Catch"][0]["Next"] == "Teardown"  # endpoint never orphaned

    def test_every_state_on_the_failure_path_still_has_the_run_id_to_close_out(self, asl):
        """MarkRunFailed reads `$.run_id`, so every state between the crash and it has
        to leave `$.run_id` in the state. A Task with no ResultPath REPLACES its input
        with the API response -- EscalateFail's response is a PutEvents result, which
        has no run_id at all, so the marker would fail on a missing path and the run
        would stay 'running' exactly as before the fix.

        This is what the earlier zombie-closeout test missed: it checked that the
        marker asks for `$.run_id`, never that `$.run_id` is still there to be asked
        for. That distinction is invisible until a real stage crashes.
        """
        states = asl["States"]
        # Walk backwards from the marker: everything that can reach it must preserve
        # the state, not overwrite it.
        feeders = [n for n, st in states.items()
                   if st.get("Next") == "MarkRunFailed"
                   or any(c["Next"] == "MarkRunFailed" for c in st.get("Catch", []))]
        assert feeders, "nothing routes to MarkRunFailed"
        for name in feeders:
            st = states[name]
            if st["Type"] != "Task":
                continue
            rp = st.get("ResultPath", "__absent__")
            assert rp != "__absent__", (
                f"{name} has no ResultPath, so its API response replaces the state and "
                "$.run_id is gone by the time MarkRunFailed reads it")
            assert rp is None or rp.startswith("$."), (
                f"{name} writes its result to {rp!r}, which does not keep $.run_id")

    def test_a_stage_catch_keeps_the_run_id_it_was_given(self, asl):
        """The Catch side of the same rule: `ResultPath: "$.error"` files the error
        under a key and leaves the rest of the state alone. Omitting it (or using
        "$") swaps the whole state for {Error, Cause}."""
        for name, st in asl["States"].items():
            for cat in st.get("Catch", []):
                if cat["Next"] == "Fail":
                    continue  # Fail needs nothing from the state
                rp = cat.get("ResultPath", "__absent__")
                assert rp not in ("__absent__", "$"), (
                    f"{name}'s catch to {cat['Next']} replaces the state with the error "
                    "object, discarding $.run_id and $.iteration")

    def test_the_starter_supplies_every_top_level_path_the_machine_reads(self, asl):
        """A missing input field raises States.Runtime, which no Catch can intercept.

        Live-verified while proving the closeout fix: an execution started without
        manifest_uri died with "The JSONPath '$.manifest_uri' ... could not be found"
        straight to ExecutionFailed -- no EscalateFail, no MarkRunFailed, run left at
        status=running. Every other failure mode in this machine routes through the
        closeout; this one cannot, so the only defense is that the sole writer of the
        execution input always supplies these fields.
        """
        top_level = set()
        def walk(node):
            if isinstance(node, dict):
                for k, v in node.items():
                    if k.endswith(".$") and isinstance(v, str) and v.startswith("$."):
                        head = v[2:].split(".")[0].split("[")[0]
                        if head and not head.startswith("$"):
                            top_level.add(head)
                    elif k == "Variable" and isinstance(v, str) and v.startswith("$."):
                        continue  # Choice states guard with IsPresent; see PipelineModeChoice
                    else:
                        walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)
        walk(asl["States"])
        # Paths the stages write themselves (ResultPath keys) are not the starter's job.
        produced = {st["ResultPath"][2:].split(".")[0]
                    for st in asl["States"].values()
                    if isinstance(st.get("ResultPath"), str) and st["ResultPath"] != "$"}
        produced.add("error")
        required = top_level - produced
        src = (REPO / "orchestration/start_pipeline/handler.py").read_text()
        start_input = src[src.index("input=json.dumps("):]
        start_input = start_input[:start_input.index("\n\n")]
        for field in sorted(required):
            assert f'"{field}"' in start_input, (
                f"the machine reads $.{field} but start_pipeline's execution input does "
                f"not set it; a missing path raises the uncatchable States.Runtime and "
                "the run can never self-close")


class TestConductorDispatch:
    """launch_run — declared in the orchestrator's harness.json since Phase 5,
    serviced nowhere until the Tasks-tab work. These tests pin the fix and the
    drift guards that would have caught the original gap."""

    def test_driver_knows_launch_run(self):
        src = (REPO / "orchestration/harness_driver/handler.py").read_text()
        assert 'name == "launch_run"' in src, (
            "the driver must service launch_run — for five phases it fell through "
            "to the unknown-tool 'unsupported' branch and every conductor plan died")

    def test_orchestrator_harness_tools_are_all_serviced_somewhere(self):
        """Drift guard both directions: every inline function the orchestrator
        declares must be handled by the console chat worker or the driver."""
        h = json.loads((REPO / "agents/orchestrator/harness.json").read_text())
        declared = {t["name"] for t in h["tools"] if t["type"] == "inline_function"}
        driver_src = (REPO / "orchestration/harness_driver/handler.py").read_text()
        console_src = (REPO / "deploy/console/lambda_function.py").read_text()
        serviced = {name for name in declared
                    if f'"{name}"' in driver_src or f'"{name}"' in console_src}
        missing = declared - serviced
        assert not missing, (f"harness declares tools nobody services: {missing} — "
                             "they would all return 'unsupported' at runtime")

    def test_orchestrator_prompt_carries_the_consult_contract(self):
        h = json.loads((REPO / "agents/orchestrator/harness.json").read_text())
        prompt = h["systemPrompt"][0]["text"]
        for marker in ("consult", "PLAN ACCEPTED", "rate_card", "DATA DISCOVERY"):
            assert marker in prompt, f"consult-mode contract lost its {marker!r} clause"

    def test_orchestrator_model_is_pinned_to_fable(self):
        """The conductor is the pre-sales brain; a quiet downgrade to a smaller
        model is a product change, not a config tweak — pin it."""
        h = json.loads((REPO / "agents/orchestrator/harness.json").read_text())
        assert h["model"]["bedrockModelConfig"]["modelId"] == "global.anthropic.claude-fable-5"

    def test_data_prep_prompt_carries_audit_and_mirror_tasks(self):
        h = json.loads((REPO / "agents/data-prep/harness.json").read_text())
        prompt = h["systemPrompt"][0]["text"]
        assert '"audit"' in prompt and '"mirror_model"' in prompt
        # supply-chain non-negotiables spelled out where the agent reads them
        assert "safetensors" in prompt and "Hugging Face" in prompt

    def test_every_agent_that_can_checkpoint_is_told_a_directive_can_arrive(self):
        """The directive channel is only useful if the agent recognizes the answer.

        An agent whose tool description promises only {"status":"continue"} has no
        reason to read a directive payload, and the verdict is delivered to a reader
        that ignores it — the same write-only failure one layer up. So every harness
        declaring checkpoint must document the directive shape, and the shape it
        documents must be the one the driver actually sends."""
        driver_src = (REPO / "orchestration/harness_driver/handler.py").read_text()
        assert '"status": "directive", "directive": directive' in driver_src

        for path in sorted((REPO / "agents").glob("*/harness.json")):
            h = json.loads(path.read_text())
            for t in h.get("tools", []):
                if t.get("name") != "checkpoint":
                    continue
                desc = t["config"]["inlineFunction"]["description"]
                for key in ("directive", "decision", "adjusted_params"):
                    assert key in desc, (
                        f"{path.parent.name}'s checkpoint never mentions {key!r} — the "
                        "agent cannot act on an answer it was never told to expect")

    def test_state_machine_data_audit_short_path(self):
        asl = json.loads((REPO / "orchestration/state_machine.asl.json").read_text())
        assert asl["StartAt"] == "PipelineModeChoice"
        choice = asl["States"]["PipelineModeChoice"]
        # IsPresent guard: executions started without the field must not crash
        assert choice["Choices"][0]["And"][0]["IsPresent"] is True
        assert choice["Default"] == "DataPrepGenerate"
        audit = asl["States"]["DataAudit"]
        assert audit["Parameters"]["Payload"]["task"] == "audit"
        assert audit["Parameters"]["Payload"]["harness_id"] == "llmops_data_prep"
        # the audit run must complete WITHOUT reaching any GPU stage
        assert audit["Next"] == "Complete"

    def test_manifest_stores_the_approval_block_verbatim(self):
        m = start_pipeline.seed_manifest("run-x", "conductor", {}, {"p": 1},
                                         {"approved_by": "alice", "budget_usd": 50})
        assert m["approval"] == {"approved_by": "alice", "budget_usd": 50}
        # and absent approval stays an empty dict, not a KeyError for readers
        m2 = start_pipeline.seed_manifest("run-y", "scheduler", {}, None)
        assert m2["approval"] == {}

def _deploy_src(name):
    return (REPO / "deploy" / name).read_text()


def test_escalations_reach_a_human_or_the_deploy_says_they_do_not():
    """escalate_human publishes to llmops-escalations, and the topic had ZERO
    subscribers -- every escalation the pipeline has ever raised went into the void.
    An escalation nobody receives is worse than no escalation: the agent stops, the
    run waits, and the design says a human was asked.

    The deploy cannot invent an email address, so the contract is: accept one when
    offered (--escalation-email, idempotent), and when the topic has no subscriber at
    all, say so loudly in the output instead of reporting the topic as simply "exists".
    """
    storage = _deploy_src("03_storage.py")
    assert "escalation-email" in storage, (
        "no way to subscribe anyone to the escalation topic at deploy time")
    assert "subscribe" in storage, "the flag must actually call sns.subscribe"
    assert "NO SUBSCRIBERS" in storage, (
        "a topic with no subscribers must be reported as a warning, not as 'exists' -- "
        "silence is what let this go unnoticed across every phase")


def test_start_pipeline_role_can_emit_the_event_its_handler_always_emits():
    """start_pipeline calls ev.emit_event(PIPELINE_STARTED) on every single run, but
    its role shipped without events:PutEvents -- so the function got as far as writing
    the manifest and the DDB row, then died with AccessDeniedException. Live, that
    surfaced as "start-pipeline did not return a run_id" and left orphan manifests for
    runs that never started. Every action a handler unconditionally performs must be
    in its role."""
    doc = json.loads((REPO / "deploy/iam/lambda_roles.json").read_text())
    stmts = doc["roles"]["start"]["permissionsPolicy"]["Statement"]
    allowed = set()
    for st in stmts:
        if st.get("Effect") != "Allow":
            continue
        acts = st.get("Action")
        allowed.update([acts] if isinstance(acts, str) else acts)
    assert "events:PutEvents" in allowed, \
        "start_pipeline emits PIPELINE_STARTED on every run"
    bus = [st for st in stmts
           if "events:PutEvents" in ([st.get("Action")] if isinstance(st.get("Action"), str)
                                     else st.get("Action", []))][0]
    assert "llmops-pipeline" in json.dumps(bus["Resource"]), \
        "scope PutEvents to the project bus, not *"


def _allowed_actions(role: str) -> set:
    doc = json.loads((REPO / "deploy/iam/lambda_roles.json").read_text())
    allowed = set()
    for st in doc["roles"][role]["permissionsPolicy"]["Statement"]:
        if st.get("Effect") != "Allow":
            continue
        acts = st.get("Action")
        allowed.update([acts] if isinstance(acts, str) else acts)
    return allowed


def test_the_driver_role_can_write_the_report_the_driver_always_writes():
    """Same defect as start_pipeline's missing PutEvents, one function over, and the
    role even said so out loud: its S3 statement was GetObject-only and commented
    "the driver verifies artifacts, it does not write them" -- while
    handle_stage_complete calls write_run_report on EVERY successful stage. The
    comment described an intention the code had already outgrown.

    Live cost: data-prep finished teacher generation, called stage_complete, and the
    driver died on AccessDenied AFTER the work was paid for -- twice, since the
    invocation retried. The agent's report was the one thing the run existed to
    produce.

    The write is scoped to the exact key the report lives at, not the bucket: the
    driver publishes one canonical document, and a wildcard would also let it rewrite
    the customer data and held-out sets it is supposed to only read."""
    allowed = _allowed_actions("driver")
    assert "s3:PutObject" in allowed, (
        "handle_stage_complete writes reports/run-latest/test-report-latest.json on "
        "every stage_complete; without PutObject every stage dies after doing its work")
    doc = json.loads((REPO / "deploy/iam/lambda_roles.json").read_text())
    writes = [st for st in doc["roles"]["driver"]["permissionsPolicy"]["Statement"]
              if "s3:PutObject" in ([st.get("Action")] if isinstance(st.get("Action"), str)
                                    else st.get("Action", []))]
    assert len(writes) == 1, "one statement owns the driver's single write"
    resource = json.dumps(writes[0]["Resource"])
    from pipeline.contracts.report import REPORT_KEY
    assert REPORT_KEY in resource, (
        f"scope the write to {REPORT_KEY}, the only object the driver publishes")
    assert "customer-data" not in resource and not resource.rstrip('"]').endswith("/*"), \
        "a bucket-wide write would let the driver rewrite the data it must only read"


#: Handler-local boto3 calls (c["s3"].put_object(...)) mapped to the IAM action they
#: need. Deliberately small: the point is not to model IAM, it is to catch a handler
#: doing something its role never allowed.
_IAM_FOR = {
    ("s3", "put_object"): "s3:PutObject",
    ("s3", "get_object"): "s3:GetObject",
    ("s3", "head_object"): "s3:GetObject",
    ("ddb", "put_item"): "dynamodb:PutItem",
    ("ddb", "update_item"): "dynamodb:UpdateItem",
    ("ddb", "get_item"): "dynamodb:GetItem",
    ("ddb", "query"): "dynamodb:Query",
    ("sns", "publish"): "sns:Publish",
    ("events", "put_events"): "events:PutEvents",
    ("lambda", "invoke"): "lambda:InvokeFunction",
    ("sfn", "send_task_success"): "states:SendTaskSuccess",
    ("sfn", "send_task_failure"): "states:SendTaskFailure",
    ("sfn", "start_execution"): "states:StartExecution",
    ("kms", "verify"): "kms:Verify",
    ("kms", "sign"): "kms:Sign",
}

#: A handler that hands its injected client to a shared helper makes the call from
#: another file, so a per-file scan of the handler sees nothing. That is exactly where
#: the missing s3:PutObject hid: handle_stage_complete's only write is
#: write_run_report(c["s3"], ...), one module over. Each entry names the client a
#: handler passes out and the action the callee performs with it.
_CLIENT_HANDOFFS = {
    "orchestration/harness_driver/handler.py": [
        ('write_run_report(c["s3"]', "s3:PutObject"),
        ('conductor_tools.service_launch_run(\n', "kms:Verify"),
    ],
}


@pytest.mark.parametrize("role,src", [
    ("driver", "orchestration/harness_driver/handler.py"),
    ("start", "orchestration/start_pipeline/handler.py"),
    ("resume", "orchestration/resume_pipeline/handler.py"),
    ("webhook", "orchestration/webhook/handler.py"),
])
def test_every_aws_call_a_handler_makes_is_in_its_role(role, src):
    """Generalizes two separately-shipped defects into one guard.

    start_pipeline shipped without events:PutEvents; the driver shipped without
    s3:PutObject. Both were actions a handler performed unconditionally, both got as
    far as doing real work before dying, and both were found by a live run rather than
    by review. A grep for the call and a grep for the grant would have caught either in
    a second, so do that on every handler, every time.

    Includes calls made through injected clients (write_run_report(c["s3"], ...)) --
    the driver's missing grant hid precisely there, invisible to a scan of the handler
    file alone."""
    text = (REPO / src).read_text()
    needed = {}
    for (client, method), action in _IAM_FOR.items():
        for pattern in (f'c["{client}"].{method}(',
                        f'c["{client}"].Table(os.environ[' ):
            if pattern in text and client != "ddb":
                needed[action] = f'c["{client}"].{method}(...)'
        if client == "ddb" and f".{method}(" in text and 'c["ddb"]' in text:
            needed[action] = f'a DynamoDB {method}'
    for marker, action in _CLIENT_HANDOFFS.get(src, []):
        if marker in text:
            needed[action] = f"{marker.strip()}...) hands the client to a helper"

    allowed = _allowed_actions(role)
    def granted(action):
        return action in allowed or any(
            p.endswith("*") and action.startswith(p[:-1]) for p in allowed)

    missing = {a: why for a, why in needed.items() if not granted(a)}
    assert not missing, (
        f"{src} performs actions the {role!r} role does not allow: {missing}. "
        "The handler will do its work and then die on AccessDenied.")
