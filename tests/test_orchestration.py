"""Unit tests for the orchestration spine — no AWS calls, all clients injected.

Covers: contracts (events, normalize, report), the harness driver's full
inline-function loop (stage_complete verify/reject, job_launched release,
escalate, re-ask, stream salvage), start/resume/webhook Lambdas, and the
state machine document (remediation loop wiring, event vocabulary, token
plumbing).

Run: .venv/bin/python -m pytest tests/test_orchestration.py -q
"""
from __future__ import annotations

import fnmatch
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

    def test_an_explicitly_passed_none_client_is_a_bug_not_a_request_for_a_real_one(self):
        """``client=None`` used to mean "not supplied", so emit_event built a real
        boto3 EventBridge client and put the event on the real bus.

        A caller that writes ``client=None`` has a None where a client should be --
        most often a test fake dict with ``"events": None`` for a client it believed
        was never reached. Silently substituting production is the worst available
        answer: on a laptop with credentials the call SUCCEEDS, so the test passes
        while writing to the production bus, and the mistake surfaces only in CI as
        NoCredentialsError from a stack frame that mentions neither the test nor the
        bus. tests/test_finops.py did exactly this and emitted real PipelineFailed
        events for six commits.

        Omitting the argument still resolves a real client -- that is the Lambda path.
        Passing None explicitly must fail, loudly, naming the caller's mistake.
        """
        with pytest.raises(ValueError, match="client=None"):
            ev.emit_event("bus", ev.PIPELINE_FAILED, {"run_id": "r"}, client=None)

    def test_every_lambda_client_factory_builds_a_real_events_client(self):
        """Why the sentinel above cannot change deployed behaviour.

        Every orchestration call site passes ``client=c["events"]`` explicitly -- so
        the question is not whether the argument is supplied, it is whether it can ever
        be None in a Lambda. It cannot: ``handler`` does ``c = clients or _clients()``,
        ``clients`` is injected only by tests, and each ``_clients()`` constructs a real
        ``boto3.client("events")``. A None therefore only ever arrives from a test fake,
        which is exactly the case the sentinel now rejects.

        That reasoning is what let the fix ship without redeploying the bundles that
        vendor events.py, so it is asserted rather than left in a commit message. If a
        factory ever grows an ``"events": None`` -- plausible for a Lambda that believes
        it emits nothing -- the sentinel stops being a no-op live and this fails first.
        """
        factories = []
        for path in sorted(REPO.glob("orchestration/**/handler.py")):
            src = path.read_text()
            if "emit_event(" not in src:
                continue
            m = re.search(r"def _clients\(\).*?(?=\n(?:def|class|@)|\Z)", src, re.DOTALL)
            assert m, f"{path.relative_to(REPO)} emits events but has no _clients()"
            body = m.group(0)
            assert '"events": None' not in body, (
                f"{path.relative_to(REPO)}'s _clients() hands out a None events client; "
                "emit_event now rejects that, so this Lambda would raise at runtime")
            assert re.search(r'"events":\s*boto3\.client\(\s*["\']events["\']', body), (
                f"{path.relative_to(REPO)}'s _clients() does not build a real events "
                "client -- confirm emit_event can still reach EventBridge from it")
            factories.append(path.name)
        assert factories, "found no event-emitting Lambda to check"

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

    def test_two_parallel_runs_do_not_overwrite_each_others_report(self):
        """The report is the one artifact a run exists to produce; it must survive a
        neighbour.

        Every run wrote reports/run-latest/test-report-latest.json, full stop. Serial
        runs made that look fine for months. The moment two run in parallel -- which is
        what this platform is FOR (finetune one model while distilling another) -- the
        second silently destroyed the first's report, and no one could tell which run the
        survivor described, because the key is the same string either way.
        """
        s3 = FakeS3()
        write_run_report(s3, "b", {"run_id": "run-A", "stages": {"eval": {"status": "completed"}}})
        write_run_report(s3, "b", {"run_id": "run-B", "stages": {"eval": {"status": "failed"}}})

        a = json.loads(s3.objects["s3://b/reports/run-A/test-report.json"])
        b = json.loads(s3.objects["s3://b/reports/run-B/test-report.json"])
        assert a["run_id"] == "run-A" and b["run_id"] == "run-B", \
            "a run's own report must describe that run"
        assert a["pass_counts"]["passed"] == 1 and b["pass_counts"]["failed"] == 1, \
            "run-B's write overwrote run-A's durable report"
        # The alias still exists and names the run it came from, so a reader of the
        # convenience key can tell WHICH run it is looking at rather than guessing.
        alias = json.loads(s3.objects["s3://b/reports/run-latest/test-report-latest.json"])
        assert alias["run_id"] == "run-B"

    def test_a_denied_alias_write_does_not_lose_the_report(self):
        """Order matters: durable copy first, alias best-effort.

        If the alias were written first, or its failure raised, then an IAM gap on ONE
        convenience key would fail a run whose real report had already been written --
        reintroducing the exact failure this came from (the driver dying on AccessDenied
        after the teacher tokens were already paid for).
        """
        class _AliasDenied(FakeS3):
            def put_object(self, **kw):
                if kw["Key"] == "reports/run-latest/test-report-latest.json":
                    raise RuntimeError("AccessDenied: s3:PutObject on the alias")
                return super().put_object(**kw)

        s3 = _AliasDenied()
        rep = write_run_report(s3, "b", {"run_id": "run-A", "stages": {}})
        assert "s3://b/reports/run-A/test-report.json" in s3.objects, \
            "the durable report must be written even when the alias is denied"
        assert "alias_error" in rep and "AccessDenied" in rep["alias_error"], \
            "a swallowed alias failure reads as 'no run has finished', a different claim"
        assert rep["report_key"] == "reports/run-A/test-report.json"

    def test_a_blank_run_id_falls_back_to_the_alias_not_a_blank_key(self):
        """reports//test-report.json is worse than the shared alias: nobody reads it."""
        from pipeline.contracts.report import REPORT_KEY, report_key_for
        assert report_key_for("") == REPORT_KEY
        assert report_key_for(None) == REPORT_KEY


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

    def test_a_failed_report_write_still_settles_the_task_token(self):
        """The root cause of run-20260731T183103Z-8b864805's 7200-second stall.

        data-prep finished teacher generation at its approved cap and called
        stage_complete (twice: 19:23:49, 19:26:20). The canonical-report put_object then
        died on AccessDenied -- the driver had no s3:PutObject until 19:30 -- and that
        exception sat directly in front of send_task_success. The token was never
        settled, so it parked for the whole 7200s TimeoutSeconds, and the console
        reported "Data Prep · Generate failed" for work already verified on S3.

        The IAM grant is fixed, but the ORDER is the real defect: the report is a
        dashboard convenience, the token is the pipeline's only channel for learning
        that a paid-for stage succeeded. So this pins the guarantee rather than the
        AccessDenied: whatever makes the report write fail, the token still gets
        settled, and the failure is reported rather than swallowed.
        """
        uri = "s3://llmops-data-test/runs/run-test-1/raw/data.jsonl"
        ac = FakeAgentCore([
            tool_use_stream("stage_complete",
                            {"stage": "data-prep", "task": "generate",
                             "outputs": [uri], "metrics": {"count": 2000}}),
            text_stream("ack")])
        c = clients(ac, FakeS3(existing=[uri]))
        c["s3"].objects["s3://llmops-data-test/runs/run-test-1/manifest.json"] = json.dumps(
            {"run_id": "run-test-1", "stages": {}})

        def _denied(Bucket, Key, Body, **kw):
            raise Exception("AccessDenied: s3:PutObject on reports/run-latest/...")
        c["s3"].put_object = _denied

        out = driver.handler(driver_event(), clients=c)
        assert out["status"] == "completed", "the stage DID complete; its work is on S3"
        assert c["sfn"].successes, (
            "the report write failed and took the task token with it -- this is the "
            "7200-second stall: finished, paid-for work that the pipeline never heard "
            "about, shown to the operator as a failure")
        assert not c["sfn"].failures, "a cosmetic write must not fail a healthy stage"
        # And it must not be silent: a report the console will serve stale needs to say so.
        payload = json.loads(c["sfn"].successes[0]["output"])
        assert "AccessDenied" in payload.get("report_write_failed", ""), (
            "swallowing the error trades a 2-hour stall for an invisible stale report")
        rows = [i for t in c["ddb"].tables.values() for i in t.items]
        assert any("report_write_failed" in json.dumps(r, default=str) for r in rows), (
            "the failure must also land in stage-events, or the stale report is invisible")

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

        # The live trigger was the report write. That specific step is now isolated --
        # it degrades to a warning precisely so it can never park a token again (see
        # test_a_failed_report_write_still_settles_the_task_token), so it can no longer
        # serve as this test's crash. The GUARANTEE here is not about S3: it is that
        # ANY unexpected exception raised while a token is held gets reported. Pinned
        # to the stage-events write, which is genuinely fatal and runs before it.
        class _DeniedEvents(FakeDDB):
            """The audit write raises -- an AccessDenied on the stage-events table has
            exactly the shape the S3 denial had: real work done, then a crash in the
            bookkeeping that follows it, while a task token is held."""
            def Table(self, name):
                raise RuntimeError(
                    "An error occurred (AccessDenied) when calling the UpdateItem "
                    f"operation: dynamodb:PutItem on {name}")

        ac = FakeAgentCore([tool_use_stream("stage_complete", {"outputs": [uri]}),
                            text_stream("ack")])
        c = clients(ac)
        c["s3"] = FakeS3(existing=[uri])
        c["ddb"] = _DeniedEvents()
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
        assert "AccessDenied" in cause or "PitItem" in cause or "PutItem" in cause, (
            "the failure must carry the real cause; 'the stage failed' with the "
            "reason only in CloudWatch is what made this take a night to find")

    def test_a_crash_with_no_task_token_is_still_raised_to_the_caller(self):
        """The console's dispatch path invokes the driver synchronously with no token.
        Swallowing the exception there would turn a hard failure into a silent success,
        which is worse than the parked token. Re-raise unless the token was settled."""
        class _Boom(FakeDDB):
            def Table(self, name):
                raise RuntimeError("kaboom")

        ac = FakeAgentCore([tool_use_stream("stage_complete", {"outputs": ["s3://b/k"]}),
                            text_stream("ack")])
        c = clients(ac)
        c["s3"] = FakeS3(existing=["s3://b/k"])
        c["ddb"] = _Boom()
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


def _exits(st: dict) -> list:
    """Every state this one can transition to: Next, Catch, Choices, Default."""
    return (([st["Next"]] if "Next" in st else [])
            + [c["Next"] for c in st.get("Catch", [])]
            + [c["Next"] for c in st.get("Choices", [])]
            + ([st["Default"]] if "Default" in st else []))


def _reaches(states: dict, start: str, target: str, seen=frozenset()) -> bool:
    """Can `start` transition to `target`, following any number of hops?

    Asserting on a literal `Next` pins the shape of the graph, not the guarantee.
    The guarantees here are about outcomes -- "an exhausted budget ends the
    execution FAILED", "no stage catches to Fail before the run record is
    marked" -- and those survive an extra hop being inserted on the path. Three
    tests broke on exactly that when the task closers landed between
    MarkRunFailed and Fail, though every guarantee still held.
    """
    if start == target:
        return True
    if start in seen:
        return False
    return any(_reaches(states, n, target, seen | {start})
               for n in _exits(states.get(start, {})))


def _terminals_from(states: dict, start: str) -> set:
    """Every terminal state (Succeed/Fail) reachable from `start`."""
    return {n for n in states
            if states[n].get("Type") in ("Succeed", "Fail")
            and _reaches(states, start, n)}


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
        # budget exhausted -> escalate, never silent fail; escalation closes the run
        # record (MarkRunFailed) and the conductor's task (MarkTaskFailed) on the way
        # out. Asserted as reachability, not a literal Next: the guarantee is that an
        # exhausted budget ends the execution FAILED after the marker runs, and that
        # holds however many closers get chained in front of Fail.
        assert states["RemediationChoice"]["Default"] == "EscalateFail"
        assert states["EscalateFail"]["Next"] == "MarkRunFailed"
        assert _terminals_from(states, "MarkRunFailed") == {"Fail"}, (
            "the marker must lead to Fail and ONLY Fail -- a path from here to a "
            "Succeed state would report an exhausted budget as a successful run")

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
        # Exempt exactly the states at or after the run marker: once MarkRunFailed has
        # run (or itself errored), the record is already written or unwritable, so Fail
        # is the only place left to go. Derived by reachability rather than named, so a
        # future state added BEFORE the marker is still held to the rule.
        for name, st in states.items():
            if name == "MarkRunFailed" or _reaches(states, "MarkRunFailed", name):
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
        # Marking the run must not swallow the failure: whatever the marker chains
        # into afterwards, the execution still ends FAILED.
        assert _terminals_from(states, "MarkRunFailed") == {"Fail"}

    def test_marking_a_run_failed_never_overwrites_a_richer_terminal_status(self, asl):
        """When the AGENT escalated, the driver already wrote status=escalated -- more
        informative than 'failed'. The condition keeps it, and the Catch means a
        rejected condition still reaches Fail instead of hanging the execution."""
        states = asl["States"]
        mark = states["MarkRunFailed"]
        assert ":running" in mark["Parameters"]["ConditionExpression"] or \
               "running" in json.dumps(mark["Parameters"]["ExpressionAttributeValues"])
        assert "States.ALL" in mark["Catch"][0]["ErrorEquals"]
        # A rejected condition must still terminate the execution as FAILED. It may
        # pass through the task closer first (a richer run status says nothing about
        # the conductor's task, which is still stuck at 'dispatched'), but it must
        # never hang and never end in Succeed.
        assert _terminals_from(states, mark["Catch"][0]["Next"]) == {"Fail"}, (
            "MarkRunFailed's Catch must lead to Fail: a rejected condition is not a "
            "success, and a dead end would hang the execution instead of failing it")

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

    def test_the_role_covers_every_table_the_state_machine_writes(self, asl):
        """The action alone is not the grant -- the RESOURCE is.

        Checking only for "dynamodb:UpdateItem" passed while the policy was scoped to
        the runs table alone, so the task closers would have taken AccessDenied. Their
        Catch (which exists so a run with no task does not fail) would have swallowed
        it, making a missing grant look exactly like a healthy no-op: the execution
        succeeds, the task stays 'dispatched', and nothing anywhere says why. Every
        table the ASL names must appear in the policy's resources.
        """
        sfn = json.loads((REPO / "deploy/iam/sfn_execution_role.json").read_text())
        granted = set()
        for st in sfn["permissionsPolicy"]["Statement"]:
            acts = st["Action"]
            if "dynamodb:UpdateItem" not in ([acts] if isinstance(acts, str) else acts):
                continue
            res = st["Resource"]
            for r in ([res] if isinstance(res, str) else res):
                granted.add(r.rsplit("table/", 1)[-1])

        written = {st["Parameters"]["TableName"] for st in asl["States"].values()
                   if st.get("Resource", "").endswith("dynamodb:updateItem")}
        assert written, "no updateItem states found -- did the resource ARN change?"
        missing = written - granted
        assert not missing, (
            f"the state machine writes {sorted(missing)} but its role grants "
            f"UpdateItem only on {sorted(granted)}; the closer would take "
            "AccessDenied and its Catch would hide it")

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

    def test_a_conductor_dispatched_task_is_closed_out_when_its_run_ends(self, asl):
        """The zombie-run bug, one level up: the TASK stays 'dispatched' forever.

        MarkRunFailed closes the run record because the state machine is the only
        participant still alive when a stage dies. A conductor-dispatched run has a
        second record with a lifecycle -- the llmops-tasks row whose id is carried in
        the manifest's approval block -- and nothing closed it. Observed live:
        task-58ecde82adcd73bf read status=dispatched while its run
        run-20260731T183103Z-8b864805 had read failed since the day before.

        Nothing else can do this job. llmops-tasks is written only by the console
        Lambda, which is not in the execution path; the driver dies with the stage; and
        the event bus has no rules, so PipelineFailed reaches no subscriber. The
        console's Tasks tab therefore renders a dead task as mid-flight, and the
        lifecycle flow -- the audit view of who ordered what and how it ended -- stops
        at 'dispatched' for every run that fails.

        Both terminal paths must close it: failure via the marker chain, success from
        Complete.
        """
        states = asl["States"]
        closers = {n: st for n, st in states.items()
                   if st.get("Resource", "").endswith("dynamodb:updateItem")
                   and st.get("Parameters", {}).get("TableName") == "llmops-tasks"}
        assert closers, (
            "no state writes llmops-tasks, so a conductor task stays 'dispatched' "
            "after its run reaches a terminal state -- the console shows a dead task "
            "as live and the audit trail never records how the order ended")

        # Reachable from BOTH terminal paths, or half the outcomes still zombie.
        for terminal in ("MarkRunFailed", "Complete"):
            assert any(_reaches(states, terminal, name) for name in closers), (
                f"{terminal} does not reach any llmops-tasks closer, so runs ending "
                f"via {terminal} leave their task at 'dispatched'")

        for name, st in closers.items():
            # A run with no task_id (schedule/webhook trigger) must not fail the
            # execution -- most runs are not conductor-dispatched.
            assert st.get("Catch"), (
                f"{name} has no Catch: a non-conductor run has no task to close, and "
                "the resulting DDB error would fail an otherwise healthy execution")

    def test_a_non_conductor_run_has_a_task_id_the_closer_can_read_harmlessly(self, asl):
        """The closer reads `$.task_id`, and reading a path that is not there raises
        the uncatchable States.Runtime -- no Catch, straight to ExecutionFailed, run
        left at status=running. That is the exact failure the starter-contract guard
        below exists for, and it would be reintroduced by a closer that assumes every
        run is conductor-dispatched. Most are not: schedule and webhook triggers have
        no task.

        So start_pipeline must always set task_id (sentinel when there is no task), and
        the closer must be conditioned so the sentinel writes nothing.
        """
        closers = {n: st for n, st in asl["States"].items()
                   if st.get("Resource", "").endswith("dynamodb:updateItem")
                   and st.get("Parameters", {}).get("TableName") == "llmops-tasks"}
        assert closers, "no llmops-tasks closer to check (see the test above)"
        src = (REPO / "orchestration/start_pipeline/handler.py").read_text()
        start_input = src[src.index("input=json.dumps("):]
        start_input = start_input[:start_input.index("\n\n")]
        assert '"task_id"' in start_input, (
            "the closer reads $.task_id but start_pipeline does not set it; a run "
            "from any non-conductor trigger would die on States.Runtime before it "
            "could self-close -- strictly worse than the zombie task it fixes")

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

    def test_the_agent_that_asks_about_data_has_the_skill_that_knows_how(self):
        """DATA DISCOVERY is step 0 of the orchestrator's consult protocol: it opens every
        consultation by asking about the dataset, its provenance, its PII disposition, and
        how a held-out set stays honest. The skill that knows how to ask those questions
        was mounted only on data-prep -- which does not run until AFTER a plan is priced
        and signed. So the agent doing the asking had no guidance, and the agent holding
        the guidance had nothing left to ask.

        Asserted on both harnesses: it is a MOUNT, not a move. data-prep still needs it to
        do the work the answers describe.
        """
        def skill_paths(agent):
            h = json.loads((REPO / f"agents/{agent}/harness.json").read_text())
            return {s.get("git", {}).get("path", "") for s in (h.get("skills") or [])}

        want = "skills/llmops/llm-data-preparation"
        assert want in skill_paths("orchestrator"), (
            "the orchestrator asks the data-discovery questions with no data-prep skill "
            "behind it")
        assert want in skill_paths("data-prep"), (
            f"{want} was MOVED off data-prep rather than also mounted -- the worker that "
            "actually prepares the data lost its guidance")

    def test_deploying_a_harness_warms_it_so_a_customer_does_not_pay_cold_start(self):
        """READY is not warm, and the deploy script used to believe it was.

        MEASURED on a scratch harness. Eight consecutive turns, fresh session id each,
        no config change between them -- time to first token:

            44.83  3.86  3.89  4.27  4.10  4.27  3.97  4.60

        Then a NO-OP UpdateHarness (identical model, only a new version) and the
        slowness returned immediately: 38.94, 34.89, then 4.39, 4.19. Reusing a single
        session id changed nothing (4.09/4.31/3.48/3.98), so it is not per-session
        state -- publishing a version is what costs ~35s.

        So every `05_harnesses.py` run handed the next speaker a ~35s turn, and the next
        speaker is a customer in the Tasks thread. The script must spend that turn itself.

        Asserted on the source rather than by executing a deploy: this suite refuses
        non-loopback sockets by construction (tests/conftest.py), and a warm-up is
        exactly a network call.
        """
        src = (REPO / "deploy/05_harnesses.py").read_text()
        assert "def warm(" in src, (
            "05_harnesses.py does not warm a harness after publishing a version, so the "
            "first real turn after every deploy pays ~35s")
        # The warm-up needs the DATA plane; the control plane cannot invoke.
        assert '"bedrock-agentcore"' in src, (
            "no data-plane client, so nothing can actually invoke the harness")
        # It must run AFTER wait_ready -- invoking a CREATING harness fails, and warming
        # before the new version is live warms the old one.
        assert src.index("def wait_ready") < src.index("def warm"), \
            "warm() must be defined and used after readiness is established"
        warm_fn = src[src.index("def warm("):]
        warm_fn = warm_fn[:warm_fn.index("\ndef ")]
        # The docstring records the measurements, so it NAMES the very things this test
        # forbids ("concurrent", "ThreadPool"). Matching against prose instead of code is
        # how a guard passes while the code is wrong -- the frontend partial_reply guard
        # failed exactly this way. Strip it and assert on the body.
        body = warm_fn.split('"""')[2] if warm_fn.count('"""') >= 2 else warm_fn
        assert "invoke_harness" in body, "warm() does not invoke anything"
        # ONE turn is not enough, which cost a wrong fix: a single throwaway turn paid
        # 37.59s and the next real turn still took 44.96s. Two are needed (v19: 44.03,
        # 45.62, 5.22, 5.55; v20: 45.27, 43.36, 5.89, 4.38), and they must be SEQUENTIAL
        # -- firing two concurrently left the following turn cold as well (46.1/38.06
        # concurrent, then 45.99, then 5.9), i.e. 3 cold turns instead of 2.
        assert "for " in body and "WARM_FAST_S" in body, (
            "warm() sends a fixed single turn; measurement says the first turn after a "
            "new version is ALWAYS cold and the second usually is too")
        assert "WARM_MAX_TURNS" in body, (
            "an unbounded warm loop turns a broken harness into an unbounded deploy")
        # ONE fast turn is not enough either, and shipping that was the SECOND wrong
        # version of this fix. Paired trials refuted it 5/5: warm_turns [4.45] -> next
        # real turn 36.98s, and the same at [3.92]->37.02, [3.77]->38.29, [3.83]->39.0,
        # [4.13]->46.51. A lone fast turn is the previous version still answering while
        # routing moves to the new one; two consecutive ones tell those apart.
        # Asserted on the STOPPING CONDITION, not merely on the name appearing somewhere:
        # a control that swapped `consec >= WARM_CONSEC_FAST` for `consec >= 1` restored
        # the refuted behaviour and still passed a name-only check, because the constant
        # was still referenced in the `warmed` field below.
        assert re.search(r"if consec >= WARM_CONSEC_FAST:", body), (
            "warm() stops at the first fast turn, which measured warmed=True and then "
            "handed the customer a ~40s turn five times out of five")
        assert re.search(r"consec\s*=\s*consec\s*\+\s*1", body), (
            "the fast-turn streak is not accumulated, so a single fast turn ends the "
            "warm-up no matter what the threshold says")
        for name, lo, hi in (("WARM_FAST_S", 6.0, 35.0), ("WARM_MAX_TURNS", 4, 10),
                             ("WARM_CONSEC_FAST", 2, 4)):
            m = re.search(rf"^{name} = ([\d.]+)$", src, re.M)
            assert m, f"{name} is not a module-level constant"
            assert lo <= float(m.group(1)) <= hi, (
                f"{name}={m.group(1)} is outside the measured range: warm turns were "
                f"3.5-8s and cold ones 37-46s, a cold harness needed 2 cold turns before "
                f"the fast ones, and 2 consecutive fast turns were needed to be sure")
        # The cap has to allow the measured shape: 2 cold turns, then WARM_CONSEC_FAST
        # fast ones. A cap below that makes the warm-up structurally unable to succeed.
        cap = int(re.search(r"^WARM_MAX_TURNS = (\d+)$", src, re.M).group(1))
        need = int(re.search(r"^WARM_CONSEC_FAST = (\d+)$", src, re.M).group(1))
        assert cap >= need + 2, (
            f"WARM_MAX_TURNS={cap} cannot fit the measured shape: 2 cold turns then "
            f"{need} consecutive fast ones")
        # A fast turn is only warm if something came back. One cold turn in five streamed
        # nothing at all, with no error -- and an empty stream returns instantly, so a
        # purely time-based check would score it warm and stop the loop having warmed
        # nothing. Elapsed seconds cannot distinguish a warm reply from no reply.
        assert re.search(r"contentBlockDelta[\s\S]{0,300}?text", body), (
            "warm() times turns without checking that any text arrived, so an empty "
            "stream returns instantly and is mistaken for a warm harness")
        assert "chars" in body and re.search(r"if chars == 0", body), \
            "a turn that streamed no text must not end the warm-up loop"
        # Concurrency here is a trap that looks like an optimization, so keep it out.
        assert "ThreadPool" not in body and "concurrent" not in body, (
            "the warm turns must be sequential -- concurrent ones raced the same "
            "uninitialized slot and cost an EXTRA cold turn")
        # A failed warm-up must not fail a deploy that already succeeded: the harness is
        # live either way, and reporting success as failure would send someone hunting a
        # deploy bug that does not exist.
        assert "except Exception" in body, (
            "a throwaway warm-up turn that raises would fail an otherwise-good deploy")
        # And it must be reported, or a silently-skipped warm-up looks identical to a
        # successful one.
        assert '"warmed"' in body, \
            "the deploy output must say whether the warm-up actually happened"
        # The docstring strip only helps if there IS a docstring to strip; if warm() ever
        # loses it, `body` silently becomes the whole function and every assertion above
        # goes back to matching prose. Fail loudly instead.
        assert body != warm_fn, (
            "warm() has no docstring, so this test is matching prose again -- the "
            "measurements that justify the loop must stay next to the loop")
        # Everything above tests a function that could be dead code. The deploy path has
        # to actually call it, and only after the harness is READY.
        cou = src[src.index("def create_or_update("):src.index("\ndef main(")]
        assert re.search(r"warm\(dat\b", cou), \
            "warm() is defined but the create/update path never calls it"
        assert cou.index("h = wait_ready(ctl, harness_id)") < cou.index("warm(dat"), \
            "the warm-up must run after the harness reports READY, not before"
        # --dry-run must not invoke anything: a dry run that talks to the data plane is
        # not a dry run.
        assert re.search(r"dat = None if args\.dry_run", src), \
            "--dry-run must leave the data-plane client unbuilt so nothing is invoked"

    def test_harness_comments_never_reach_the_agentcore_api(self):
        """The harness JSONs carry _comment keys explaining why each block is shaped the
        way it is, which belongs next to the block rather than in a doc nobody opens. But
        an unknown key fails the whole call -- the same class of failure that cost a
        debugging round on the console IAM policy. Both apply paths must strip them
        RECURSIVELY, since the comments sit nested inside skills[] and tools[] too.
        """
        for mod in ("05_harnesses.py", "update_harness.py"):
            src = (REPO / "deploy" / mod).read_text()
            assert "startswith(\"_\")" in src, f"{mod} does not strip _-prefixed keys"
            # recursive: the stripper must call itself on nested values
            fn = src[src.index("def strip_comments" if "def strip_comments" in src
                               else "def _strip_comments"):]
            fn = fn[:fn.index("\n\n\n")] if "\n\n\n" in fn else fn
            assert fn.count("strip_comments(") >= 3, (
                f"{mod}'s stripper does not recurse, so a _comment nested in skills[] or "
                "tools[] still reaches the API and fails the whole call")

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

    def test_the_execution_input_carries_the_task_id_from_the_approval_block(self):
        """The state machine closes the conductor's task record at the end of a run, so
        it needs the task id in the EXECUTION input -- it cannot read the manifest from
        S3, the same constraint that put pipeline_mode there.

        The id already exists on the approval block (`approval.task_id`, written when
        the human accepted the plan), so this is a plumbing gap rather than missing
        data: run-20260731T183103Z-8b864805's manifest names task-58ecde82adcd73bf, and
        that task still read 'dispatched' a day after the run failed.

        A run with no approval must still get the field, as an explicit sentinel: the
        closer reads $.task_id, and a missing path raises the uncatchable
        States.Runtime.
        """
        for approval, expected in (
                ({"task_id": "task-abc", "approved_by": "alice"}, "task-abc"),
                (None, start_pipeline.NO_TASK),          # scheduler/webhook run
                ({"approved_by": "alice"}, start_pipeline.NO_TASK)):  # signed, no task
            c = {"s3": FakeS3(), "ddb": FakeDDB(), "sfn": FakeSfn(),
                 "events": FakeEvents()}
            start_pipeline.handler({"trigger_source": "conductor", "params": {},
                                    "approval": approval}, clients=c)
            sent = json.loads(c["sfn"].executions[0]["input"])
            assert sent["task_id"] == expected, (
                f"approval={approval!r} produced task_id={sent.get('task_id')!r}; the "
                "closer needs a real id or the sentinel, never a missing path")

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

    The write is scoped to the reports prefix, not the bucket. This test used to pin the
    single literal key REPORT_KEY, which was right when the driver published exactly one
    object and wrong once it published one per run -- so it is written against the
    GUARANTEE (the driver may write reports and nothing else) rather than against the
    string, or the next legitimate key change breaks it again."""
    allowed = _allowed_actions("driver")
    assert "s3:PutObject" in allowed, (
        "handle_stage_complete writes a run report on every stage_complete; without "
        "PutObject every stage dies after doing its work")
    doc = json.loads((REPO / "deploy/iam/lambda_roles.json").read_text())
    writes = [st for st in doc["roles"]["driver"]["permissionsPolicy"]["Statement"]
              if "s3:PutObject" in ([st.get("Action")] if isinstance(st.get("Action"), str)
                                    else st.get("Action", []))]
    assert len(writes) == 1, "one statement owns the driver's report write"
    resources = writes[0]["Resource"]
    resources = [resources] if isinstance(resources, str) else resources

    from pipeline.contracts.report import REPORT_KEY, report_key_for

    # Object-key patterns this statement permits, i.e. the part after "<bucket>/".
    # fnmatch, not hand-rolled prefix arithmetic: an IAM resource is a glob, and a helper
    # clever enough to parse one is clever enough to crash on the very input it is meant
    # to reject (this assertion's first draft raised IndexError on a bucket-wide grant --
    # a test that fails for the wrong reason is a test that will pass for the wrong one).
    patterns = [r.split(":::", 1)[1].split("/", 1)[1] if "/" in r.split(":::", 1)[1] else ""
                for r in resources]

    def _permitted(key):
        return any(p and fnmatch.fnmatch(key, p) for p in patterns)

    # Every key the writer can actually produce must be permitted -- the per-run object
    # and the alias. Derived from report_key_for() rather than restating the shape, so a
    # change to the key fails HERE, at the grant, instead of live on AccessDenied after
    # the stage has already been paid for.
    for key in (report_key_for("run-20260731T183103Z-8b864805"), REPORT_KEY):
        assert _permitted(key), f"the driver writes {key} but no statement allows it"

    # The property the narrow scope exists for, unchanged by widening to a prefix: a
    # pipeline that can rewrite the customer's data can destroy the held-out set its own
    # quality gates are judged against. These are the bucket's real top-level prefixes.
    for forbidden in ("customer-data/held-out.jsonl", "runs/r/manifest.json",
                      "contracts/x.json", "plans/p.json", "code/train.py",
                      "tasks/t.json", "finops/rates.json"):
        assert not _permitted(forbidden), (
            f"the driver must not be able to write {forbidden}; the grant has widened "
            "past the reports prefix")


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


# ── the S3 skill mirror: a prerequisite, not an optimization ───────────────────
# All 19 skill sources across the 7 harnesses are `git` today. Moving them to `s3` is
# worth doing for two reasons -- a git source has NO branch field, so it always reads the
# skills repo's DEFAULT branch and main-branch drift silently changes production agent
# behaviour; and a VPC-mode harness cannot reach GitHub at all -- but the order cannot be
# reversed. Switch the sources first and UpdateHarness ACCEPTS it, mints a version, and
# reports READY; the failure lands at SESSION START, on every invocation. These guards
# hold the sync step to being a real gate rather than a copy loop.

@pytest.fixture(scope="module")
def storage_mod():
    """deploy/03_storage.py as a module (its name starts with a digit, so not importable).
    Nothing at import time calls AWS."""
    return _load("llmops_03_storage_orch", "deploy/03_storage.py")


class _SkillS3:
    """Records uploads and answers head_object only for keys that were uploaded."""

    def __init__(self):
        self.uploads = []
        self.heads = []

    def upload_file(self, local, bucket, key):
        self.uploads.append((local, bucket, key))

    def head_object(self, Bucket, Key):
        self.heads.append((Bucket, Key))
        if Key not in [u[2] for u in self.uploads]:
            raise AssertionError(f"head_object on a key never uploaded: {Key}")
        return {"ContentLength": 1}


def _skill_tree(root, names, frontmatter=True):
    """Write a minimal but valid skills checkout under `root`."""
    for name in names:
        d = root / name
        d.mkdir(parents=True, exist_ok=True)
        head = ("---\n"
                f"name: {name.rsplit('/', 1)[-1]}\n"
                "description: one line\n"
                "---\n\n") if frontmatter else ""
        (d / "SKILL.md").write_text(head + "# body\n", encoding="utf-8")
    return root


def test_the_sync_covers_every_skill_the_configs_mount(storage_mod):
    """The list of what to mirror is DERIVED from agents/*/harness.json, never hand-kept.

    A hand-kept list is the exact failure this whole step guards against: a mount added
    later would be absent from S3, and a missing skill source does not fail at
    UpdateHarness -- it fails at session start, on every invocation, with the config
    still reading healthy.
    """
    mounts = storage_mod.mounted_skills(REPO)
    total = sum(len(v) for v in mounts.values())
    configs = sorted((REPO / "agents").glob("*/harness.json"))
    from_cfg = sum(1 for c in configs
                   for s in (json.loads(c.read_text()).get("skills") or [])
                   if "git" in s)
    assert total == from_cfg, (
        f"the sync plan covers {total} mounts but the configs declare {from_cfg} git "
        "sources; a mount the sync does not know about is a skill that vanishes when "
        "its source is switched to s3")
    assert mounts, "no mounts found at all -- the glob or the config shape changed"


def test_a_skill_entry_with_sibling_keys_is_still_collected(storage_mod, tmp_path):
    """One live entry carries a `_comment` beside its `git` key.

    Membership must be tested by key, not by taking the entry's first key -- a counter
    written the latter way reported 18 git sources plus one '_comment', which would have
    silently excluded the orchestrator's llm-data-preparation mount from the mirror.
    """
    agents = tmp_path / "agents"
    (agents / "a").mkdir(parents=True)
    (agents / "a" / "harness.json").write_text(json.dumps({"skills": [
        {"_comment": "why this is mounted here too", "git": {
            "url": "https://github.com/x/y", "path": "skills/llmops/llm-data-preparation"}},
    ]}))
    mounts = storage_mod.mounted_skills(tmp_path)
    assert "skills/llmops/llm-data-preparation" in mounts, (
        f"an entry with a sibling key was dropped: {mounts}")


def test_a_skill_md_without_frontmatter_is_rejected_before_upload(storage_mod, tmp_path):
    """The #1 skills failure, and undocumented: no YAML frontmatter kills every session
    at start. Catching it at upload time is the only cheap place to catch it."""
    d = _skill_tree(tmp_path, ["skills/llmops/llm-evaluation"], frontmatter=False)
    with pytest.raises(ValueError, match="does not start with"):
        storage_mod.skill_frontmatter(d / "skills/llmops/llm-evaluation/SKILL.md")


def test_frontmatter_missing_name_or_description_is_rejected(storage_mod, tmp_path):
    """`name` is how the agent addresses the skill, so an absent one is unreachable even
    though the file opens with valid-looking frontmatter."""
    p = tmp_path / "SKILL.md"
    p.write_text("---\ndescription: only a description\n---\n\n# body\n")
    with pytest.raises(ValueError, match="missing.*name"):
        storage_mod.skill_frontmatter(p)
    p.write_text("---\nname: llm-evaluation\n---\n\n# body\n")
    with pytest.raises(ValueError, match="missing.*description"):
        storage_mod.skill_frontmatter(p)
    p.write_text("---\nname: llm-evaluation\ndescription: real\n---\n\n# body\n")
    assert storage_mod.skill_frontmatter(p) == ("llm-evaluation", "real")


def test_unclosed_frontmatter_is_rejected(storage_mod, tmp_path):
    """A file that opens `---` and never closes it has no frontmatter block at all; the
    naive parser would read the whole document as keys and find name/description in prose."""
    p = tmp_path / "SKILL.md"
    p.write_text("---\nname: x\ndescription: y\n\n# body with no closing marker\n")
    with pytest.raises(ValueError, match="never closes"):
        storage_mod.skill_frontmatter(p)


def test_a_mounted_skill_absent_from_the_source_tree_is_fatal(storage_mod, tmp_path):
    """Uploading 10 of 11 and reporting success is how a source switch loses a skill.

    The real checkout is the sync source, so a partial or wrong --skills-src must stop
    the deploy rather than mirror what it happens to have.
    """
    agents = tmp_path / "repo" / "agents" / "a"
    agents.mkdir(parents=True)
    (agents / "harness.json").write_text(json.dumps({"skills": [
        {"git": {"url": "u", "path": "skills/llmops/llm-evaluation"}},
        {"git": {"url": "u", "path": "skills/llmops/llm-nonexistent"}},
    ]}))
    src = _skill_tree(tmp_path / "src", ["skills/llmops/llm-evaluation"])
    s3 = _SkillS3()
    with pytest.raises(SystemExit, match="llm-nonexistent"):
        storage_mod.ensure_skills(s3, "b", dry=False, src=src, repo=tmp_path / "repo")
    assert not s3.uploads, (
        f"a missing skill must be caught BEFORE any upload; these went up: {s3.uploads}")


def test_the_s3_key_mirrors_the_path_the_git_source_already_names(storage_mod, tmp_path):
    """The URI a harness config needs must be mechanical -- s3://<bucket>/<the same path
    the git source names> -- not a second naming scheme to keep in sync by hand."""
    agents = tmp_path / "repo" / "agents" / "a"
    agents.mkdir(parents=True)
    (agents / "harness.json").write_text(json.dumps({"skills": [
        {"git": {"url": "u", "path": "skills/llmops/llm-evaluation"}}]}))
    src = _skill_tree(tmp_path / "src", ["skills/llmops/llm-evaluation"])
    s3 = _SkillS3()
    out = storage_mod.ensure_skills(s3, "bkt", dry=False, src=src,
                                    repo=tmp_path / "repo")
    entry = out["skills"]["skills/llmops/llm-evaluation"]
    assert entry["uri"] == "s3://bkt/skills/llmops/llm-evaluation"
    assert ("bkt", "skills/llmops/llm-evaluation/SKILL.md") in \
        [(u[1], u[2]) for u in s3.uploads]


def test_each_uploaded_skill_is_read_back_at_the_key_a_harness_will_ask_for(
        storage_mod, tmp_path):
    """upload_file returning without an exception is not the same claim as the object
    being fetchable at that key, and the gap between those two claims is a session-start
    failure. So the entry point of every skill is head_object'd after upload."""
    agents = tmp_path / "repo" / "agents" / "a"
    agents.mkdir(parents=True)
    (agents / "harness.json").write_text(json.dumps({"skills": [
        {"git": {"url": "u", "path": "skills/llmops/llm-evaluation"}},
        {"git": {"url": "u", "path": "skills/mlops/ml-solution-design"}}]}))
    src = _skill_tree(tmp_path / "src", ["skills/llmops/llm-evaluation",
                                        "skills/mlops/ml-solution-design"])
    s3 = _SkillS3()
    storage_mod.ensure_skills(s3, "bkt", dry=False, src=src, repo=tmp_path / "repo")
    assert sorted(k for _, k in s3.heads) == [
        "skills/llmops/llm-evaluation/SKILL.md",
        "skills/mlops/ml-solution-design/SKILL.md"], (
        f"every skill's SKILL.md must be read back, got {s3.heads}")


def test_a_dry_run_validates_frontmatter_but_writes_nothing(storage_mod, tmp_path):
    """The validation is the valuable half, so --dry-run must still do it. A dry run that
    only prints a file count cannot tell you the switch is safe."""
    agents = tmp_path / "repo" / "agents" / "a"
    agents.mkdir(parents=True)
    (agents / "harness.json").write_text(json.dumps({"skills": [
        {"git": {"url": "u", "path": "skills/llmops/llm-evaluation"}}]}))
    src = _skill_tree(tmp_path / "src", ["skills/llmops/llm-evaluation"],
                      frontmatter=False)
    s3 = _SkillS3()
    with pytest.raises(ValueError, match="does not start with"):
        storage_mod.ensure_skills(s3, "b", dry=True, src=src, repo=tmp_path / "repo")
    assert not s3.uploads


def test_no_skills_src_skips_loudly_rather_than_uploading_nothing_quietly(
        storage_mod, tmp_path):
    """Uploading nothing is safe. Reporting a mirror that did not happen is not: the next
    step reads that as permission to switch a source."""
    s3 = _SkillS3()
    out = storage_mod.ensure_skills(s3, "b", dry=True, src=None, repo=REPO)
    assert "skipped" in out["status"] and "skills-src" in out["status"]
    assert not s3.uploads


def test_the_deploy_runs_the_skill_sync_and_reports_it_on_its_own_line(storage_mod):
    """Folded into another string it is one word in output nobody reads -- the same
    mistake CORS made, whose only symptom was a customer upload failing much later."""
    src = (REPO / "deploy/03_storage.py").read_text()
    main = src[src.index("def main("):]
    assert '"skills"] = ensure_skills(' in main, \
        "main() never calls ensure_skills, so the step cannot run at deploy time"
    assert "--skills-src" in src


def test_the_skill_sync_lands_before_any_source_is_switched(storage_mod):
    """The ordering constraint, asserted so it cannot be quietly inverted later.

    While every source is still `git`, ensure_skills must exist. Once sources move to
    `s3`, the mirror they read must be the one this step writes -- so if any config names
    an s3 skill URI, that URI has to be a key ensure_skills would have uploaded.
    """
    assert hasattr(storage_mod, "ensure_skills"), (
        "the sync step must exist BEFORE any source moves to s3: a bad source is "
        "accepted by UpdateHarness and then fails at session start on every invocation")
    for cfg in sorted((REPO / "agents").glob("*/harness.json")):
        for skill in json.loads(cfg.read_text()).get("skills") or []:
            uri = (skill.get("s3") or {}).get("uri", "")
            if uri:
                path = uri.split("/", 3)[-1] if uri.startswith("s3://") else ""
                assert path.startswith("skills/"), (
                    f"{cfg.parent.name} mounts {uri}, which is not under the skills/ "
                    "prefix ensure_skills mirrors, so nothing keeps it in sync")
