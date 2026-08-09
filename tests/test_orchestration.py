"""Unit tests for the orchestration spine — no AWS calls, all clients injected.

Covers: contracts (events, normalize, report), the harness driver's full
inline-function loop (stage_complete verify/reject, job_launched release,
escalate, re-ask, stream salvage), start/resume/webhook Lambdas, and the
state machine document (remediation loop wiring, event vocabulary, token
plumbing).

Run: .venv/bin/python -m pytest tests/test_orchestration.py -q
"""
from __future__ import annotations

import datetime
import fnmatch
import importlib.util
import io
import json
import re
import os
import pathlib
import sys
import time

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
    # Added with the monitor stages. Without it _resolve_harness_arn falls through to SSM,
    # and conftest.py refuses the socket -- which is the guard working: a driver test that
    # reached the real control plane would be a test of production.
    "HARNESS_ARN_LLMOPS_MONITOR": "arn:aws:bedrock-agentcore:us-east-1:123456789012:harness/llmops_monitor-TESTSUFFIX",
    # The conductor is a driver target too, since an EscalatedToHuman event routes a
    # triage to it. Without the override _resolve_harness_arn would reach SSM, i.e. the
    # network, which conftest refuses.
    "HARNESS_ARN_LLMOPS_ORCHESTRATOR": "arn:aws:bedrock-agentcore:us-east-1:123456789012:harness/llmops_orchestrator-TESTSUFFIX",
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


def _conditional_check_failed():
    """The real exception shape, not a bare Exception carrying the name as a message.

    Code that discriminates a rejected condition from a throttle reads
    ``exc.response["Error"]["Code"]`` -- botocore's ClientError contract -- because the
    typed exception classes hang off a live client instance and are unavailable under an
    injected double. A fake raising ``Exception("ConditionalCheckFailedException")`` has
    no ``response``, so every such check would read the rejection as an unrelated error
    and reraise, and the test would pass for the wrong reason.
    """
    from botocore.exceptions import ClientError
    return ClientError({"Error": {"Code": "ConditionalCheckFailedException",
                                  "Message": "The conditional request failed"}},
                       "UpdateItem")


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
        vals = kw.get("ExpressionAttributeValues") or {}
        cond = kw.get("ConditionExpression")
        # attribute_exists / attribute_not_exists BEFORE the upsert below, because that
        # is the pair whose whole purpose is to gate row CREATION.
        for guard, want_present in (("attribute_exists(", True),
                                    ("attribute_not_exists(", False)):
            if isinstance(cond, str) and guard in cond:
                attr = cond.split(guard, 1)[1].split(")", 1)[0].strip()
                present = target is not None and attr in target
                if present != want_present:
                    raise _conditional_check_failed()
        if target is None:
            # update_item is an UPSERT: on a key with no row DynamoDB CREATES one from
            # the Key plus whatever SET writes. Returning early here instead -- as this
            # fake did -- made a whole class of defect untestable: the driver's escalate
            # path minted {run_id, status: escalated} rows for invocations that are not
            # runs (live: sweep-2026-08-01 from a scheduled orphan sweep), and no test
            # could see it because in the fake the write simply evaporated. A double
            # that is more forgiving than production hides exactly the bugs production
            # will have.
            target = dict(kw.get("Key") or {})
            self.items.append(target)
        if isinstance(cond, str) and "=" in cond and "attribute_" not in cond:
            attr, _, placeholder = (p.strip() for p in cond.partition("="))
            if target.get(attr) != vals.get(placeholder):
                raise _conditional_check_failed()
        expr = kw.get("UpdateExpression", "")
        if expr.upper().startswith("SET"):
            # ExpressionAttributeNames must be resolved, or a `SET #s = :v` write lands
            # under a literal "#s" key and the row reads back with no `status` at all.
            # Latent for as long as nothing re-read a status this fake had written; the
            # phantom-row work reads one back, which is what surfaced it.
            names = kw.get("ExpressionAttributeNames") or {}
            for clause in expr[3:].split(","):
                lhs, _, rhs = (p.strip() for p in clause.partition("="))
                if rhs in vals:
                    target[names.get(lhs, lhs)] = vals[rhs]
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

    def get_item(self, **kw):
        # Reads back what was written, like query. put_directive consults the run's
        # status through this to decide whether anyone can still hear the verdict; a
        # stub returning a fixed row would make an unreachable run look reachable.
        for item in self.items:
            if all(item.get(k) == v for k, v in (kw.get("Key") or {}).items()):
                return {"Item": item}
        return {}

    def scan(self, **kw):
        # The resurrector's discovery read: everything, one page. Pagination is the
        # caller's problem and tens-of-rows is the documented scale.
        return {"Items": list(self.items)}


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


class TricklingStream:
    """A stream that never ends on its own — a reasoning model trickling chunks.

    boto's read_timeout bounds the gap BETWEEN chunks, so a stream like this can
    outlive the Lambda's 900s wall entirely inside one _drain call. That is what
    killed run 68cfa9c8's resumed generate turn at 03:39:49Z: the runtime killed the
    invocation mid-stream, the async retry replayed a stale continuation, and the
    stage failed MissingStageComplete with 51 tasks left.
    """

    def __iter__(self):
        while True:
            yield {"contentBlockDelta": {"delta": {"text": "…"}}}


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
                # time turn 2 would start there is not enough left for another 840s
                # turn. 500s, not 10s: comfortably above _drain's in-stream deadline
                # margin (45s) so the first turn's stream is NOT deadline-cut, and
                # comfortably below the 850s between-turns bar so turn 2 hands off --
                # this test is about the BETWEEN-turns path, the in-stream cut has
                # its own test.
                self.calls += 1
                return 500_000

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
        written to CloudWatch and to nobody else. The token parks until TimeoutSeconds --
        86400s, a full DAY, on every state that waits on real work. The 2026-08-03 raise
        from 7200 made this test's subject strictly more valuable: a stranded token used
        to cost two hours, and now costs a day.

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
            "nothing -- the token parks until TimeoutSeconds (86400s on every "
            "long-work state since 2026-08-03) while the run record still says 'running'")
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
        parked = next(u for u in c["ddb"].Table(ENV["RUNS_TABLE"]).updates
                      if ":j" in (u.get("ExpressionAttributeValues") or {}))
        assert parked["ExpressionAttributeValues"][":j"] == "llmops-qlora-1"
        assert parked["ExpressionAttributeValues"][":t"] == "tok-123"
        assert any(e["DetailType"] == ev.TRAINING_STARTED for e in c["events"].entries)

    def test_eval_job_launched_parks_token_and_releases(self):
        """Launch-and-release is stage-generic: the eval agent parks its student
        inference job on the same rail as finetune's training job, with
        current_stage=eval so the resume Lambda knows whose completion it is
        settling. Before eval had job_launched, its only way to span a long
        inference job was polling in-turn — where prose turn-ends happen; run
        b56281da died there with a healthy job mid-flight."""
        ac = FakeAgentCore([
            tool_use_stream("job_launched", {"job_name": "llmops-eval-infer-1"}),
            text_stream("released")])
        c = clients(ac)
        out = driver.handler(driver_event(stage="eval", task="evaluate"), clients=c)
        assert out["status"] == "released"
        assert not c["sfn"].successes  # token NOT settled — resume λ owns it
        parked = next(u for u in c["ddb"].Table(ENV["RUNS_TABLE"]).updates
                      if ":j" in (u.get("ExpressionAttributeValues") or {}))
        assert parked["ExpressionAttributeValues"][":j"] == "llmops-eval-infer-1"
        assert parked["ExpressionAttributeValues"][":s"] == "eval"

    def test_escalate_human_notifies_and_fails_token(self):
        ac = FakeAgentCore([
            tool_use_stream("escalate_human", {"reason": "irrecoverable data drift"}),
            text_stream("ack")])
        c = clients(ac)
        out = driver.handler(driver_event(), clients=c)
        assert out["status"] == "escalated"
        assert c["sns"].published and c["sfn"].failures
        assert c["sfn"].failures[0]["error"] == "EscalatedToHuman"

    # ---- escalate must never MINT a run row (#62) ----------------------------
    #
    # update_item is an upsert, so "set status=escalated" on an id with no row CREATES
    # one. Live: sweep-2026-08-01 in llmops-pipeline-runs, holding {run_id, status} and
    # nothing else -- no created_at, no trigger_source, no iteration -- filed by a
    # scheduled orphan-endpoint sweep whose own Lambda goes out of its way not to write
    # there. Every id below reaches handle_escalate through a real dispatch path.

    @staticmethod
    def _escalate(c, event):
        return driver.handle_escalate(c, event, {"reason": "budget exhausted"})

    def test_an_escalation_updates_the_run_row_of_a_real_run(self):
        """The behaviour being preserved. A pipeline stage that escalates must still
        close its own run out at status=escalated -- that value is the driver's alone,
        and MarkRunFailed's ConditionExpression deliberately keeps it rather than
        overwriting it with the blunter 'failed'."""
        c = clients()
        table = c["ddb"].Table(ENV["RUNS_TABLE"])
        table.put_item(Item={"run_id": "run-real-1", "status": "running",
                             "created_at": "2026-08-02T00:00:00Z",
                             "trigger_source": "console", "iteration": 0})
        self._escalate(c, driver_event(run_id="run-real-1"))
        row = table.get_item(Key={"run_id": "run-real-1"})["Item"]
        assert row["status"] == "escalated"
        # The richer attributes survive: this is an update, not a replacement.
        assert row["trigger_source"] == "console" and row["created_at"]

    @pytest.mark.parametrize("run_id,exists", [("run-real-1", True),
                                               ("sweep-2026-08-01", False)])
    def test_an_escalation_is_recorded_in_the_timeline_whichever_path_it_took(
            self, run_id, exists):
        """The trace the row write was silently standing in for.

        handle_escalate wrote no stage event at all: for a real run, runs.status WAS the
        record, so an escalation never appeared in the timeline the console renders from
        llmops-stage-events -- unlike a page, which records its own. Making the row write
        conditional would have turned that into no record anywhere for a non-run, so the
        event is written on both paths and `run_row` reports which one ran."""
        c = clients()
        if exists:
            c["ddb"].Table(ENV["RUNS_TABLE"]).put_item(
                Item={"run_id": run_id, "status": "running"})
        self._escalate(c, driver_event(run_id=run_id, stage="monitor", task_token=""))
        rows = [i for i in c["ddb"].Table(ENV["EVENTS_TABLE"]).items
                if i["run_id"] == run_id and i["sk"].endswith("#escalated")]
        assert len(rows) == 1, f"{run_id} escalated leaving no durable trace"
        detail = json.loads(rows[0]["detail"])
        assert detail["reason"] == "budget exhausted"
        assert detail["run_row"] is exists, (
            "the event has to say whether a run row was closed too, or a reader cannot "
            "tell an escalated run from an escalating non-run")

    # ---- one dead escalation channel must not close the others -----------------
    #
    # The channels are independent by design and the ordering used to say otherwise:
    # SNS was the FIRST statement in handle_escalate and unwrapped, so a failed publish
    # took the stage event, the bus event and the task-token settle with it. And SNS is
    # the channel with a known-zero audience -- llmops-escalations has no subscribers
    # live, which ensure_topic reports rather than papering over, because a deploy
    # cannot invent an address. The one channel that reaches nobody was gating the two
    # that work.

    def test_a_dead_sns_topic_does_not_take_the_whole_escalation_with_it(self):
        """The live shape of this: zero subscribers today, and a publish can fail outright
        (topic deleted, throttle, IAM drift). The verdict still has to reach the conductor
        on the bus, the timeline still has to show it, and the token still has to settle."""
        c = clients()
        c["ddb"].Table(ENV["RUNS_TABLE"]).put_item(
            Item={"run_id": "run-real-3", "status": "running"})

        def boom(**kw):
            raise RuntimeError("Topic does not exist")
        c["sns"].publish = boom
        out = self._escalate(c, driver_event(run_id="run-real-3"))
        assert out == {"escalated": True}
        assert any(e["DetailType"] == ev.ESCALATED_TO_HUMAN for e in c["events"].entries)
        assert [i for i in c["ddb"].Table(ENV["EVENTS_TABLE"]).items
                if i["sk"].endswith("#escalated")]
        assert c["ddb"].Table(ENV["RUNS_TABLE"]).get_item(
            Key={"run_id": "run-real-3"})["Item"]["status"] == "escalated"
        assert c["sfn"].failures, "the task token was never settled"

    def test_a_failed_bus_emit_still_settles_the_task_token(self):
        """The expensive half. The settle is what releases the state machine; skipping it
        because a PutEvents failed parks a live token on a run that has already escalated,
        and the only thing that frees it is the stage's own timeout -- 86400s on every
        long-work state since the 2026-08-03 raise, so a full day rather than the two
        hours it used to be. That is the zombie #52 and MarkRunFailed exist to prevent,
        re-entered through the notification path, and the raise makes the settle path
        more load-bearing rather than less."""
        c = clients()

        def boom(**kw):
            raise RuntimeError("bus unreachable")
        c["events"].put_events = boom
        out = self._escalate(c, driver_event(run_id="run-real-4"))
        assert out == {"escalated": True}
        assert c["sfn"].failures and c["sfn"].failures[0]["error"] == "EscalatedToHuman"

    def test_the_finops_audit_path_survives_a_dead_topic_too(self):
        """The audit path returns before the bus and the settle, so SNS is its ONLY
        channel -- which makes it the one place where an unwrapped publish would turn a
        cost finding into a raised exception out of the auditor rather than a logged
        miss. It still must not raise."""
        c = clients()

        def boom(**kw):
            raise RuntimeError("Topic does not exist")
        c["sns"].publish = boom
        out = self._escalate(c, driver_event(run_id="finops-2026-08-02", stage="finops",
                                            task="audit", task_token=""))
        assert out == {"escalated": True}
        assert c["ddb"].Table(ENV["RUNS_TABLE"]).items == []

    def test_a_failed_timeline_write_never_withholds_the_escalation(self):
        """Bookkeeping must not be able to swallow the alert. The stage event is the
        record; SNS and the bus event are how a human finds out. If the record fails,
        the human must still be told -- an escalation nobody hears is the failure mode
        this whole handler exists to prevent."""
        c = clients()
        events_table = c["ddb"].Table(ENV["EVENTS_TABLE"])

        def boom(**kw):
            raise RuntimeError("events table gone")
        events_table.put_item = boom
        out = self._escalate(c, driver_event(run_id="sweep-2026-08-01", stage="monitor",
                                            task_token=""))
        assert out == {"escalated": True}
        assert c["sns"].published
        assert any(e["DetailType"] == ev.ESCALATED_TO_HUMAN for e in c["events"].entries)

    @pytest.mark.parametrize("run_id,stage,task", [
        # The live offender: EventBridge Scheduler -> llmops-monitor-sweep -> driver.
        ("sweep-2026-08-01", "monitor", "sweep"),
        # Triage: the driver invokes itself under triage-<subject> off the bus rule. If
        # a triage escalates in turn, the id names no run either.
        ("triage-run-abc", "orchestrator", "triage"),
    ])
    def test_an_escalation_by_something_that_is_not_a_run_mints_no_run_row(
            self, run_id, stage, task):
        """The runs table is the authority on what a run is, and only start_pipeline
        writes to it. So a synthetic id has no row, and the escalate path must leave the
        table exactly as it found it -- empty."""
        c = clients()
        out = self._escalate(c, driver_event(run_id=run_id, stage=stage, task=task,
                                            task_token=""))
        assert out == {"escalated": True}
        assert c["ddb"].Table(ENV["RUNS_TABLE"]).items == [], (
            f"escalating {run_id} minted a phantom run row")
        # The escalation itself is NOT suppressed: a sweep that cannot finish still has
        # to reach a human. Only the run-row write is conditional.
        assert c["sns"].published, "the escalation was swallowed along with the row"
        assert any(e["DetailType"] == ev.ESCALATED_TO_HUMAN
                   for e in c["events"].entries)

    def test_the_row_write_is_gated_on_the_row_existing_not_on_a_stage_allowlist(self):
        """Why a condition rather than another _is_finops-style list.

        The first fix for this enumerated the one non-run invoker known at the time
        (stage == 'finops'), which is why the sweep -- added later, under its own
        sweep-<date> id -- walked straight back into it. An allowlist of stages that are
        not runs is a hand-maintained second copy of a fact the runs table already
        holds, and it is wrong the moment someone adds a caller. So assert the mechanism:
        the write carries attribute_exists(run_id)."""
        c = clients()
        self._escalate(c, driver_event(run_id="sweep-2026-08-01", stage="monitor",
                                       task="sweep", task_token=""))
        updates = c["ddb"].Table(ENV["RUNS_TABLE"]).updates
        assert updates, "no conditional write was even attempted"
        assert "attribute_exists(run_id)" in updates[0]["ConditionExpression"]

    def test_a_throttle_on_the_row_write_is_not_read_as_this_was_not_a_run(self):
        """The discriminating half, and the reason the fake raises a real ClientError.

        Absorbing every failure would be worse than the bug it fixes: a run that DID
        escalate would silently keep status=running, becoming the zombie MarkRunDone and
        MarkRunFailed exist to prevent. Only a rejected condition means 'not a run'."""
        c = clients()
        table = c["ddb"].Table(ENV["RUNS_TABLE"])

        def throttled(**kw):
            from botocore.exceptions import ClientError
            raise ClientError({"Error": {"Code": "ProvisionedThroughputExceededException",
                                         "Message": "slow down"}}, "UpdateItem")
        table.update_item = throttled
        with pytest.raises(Exception) as ei:
            self._escalate(c, driver_event(run_id="run-real-2"))
        assert "ProvisionedThroughput" in str(ei.value)

    def test_the_condition_is_matched_by_error_code_not_by_message_text(self):
        """_is_condition_failure reads exc.response['Error']['Code'], because the typed
        exception classes hang off a live client instance and cannot be referenced from a
        module that must import under an injected double. A bare
        Exception('ConditionalCheckFailedException') is NOT the rejection: it has no
        response, and treating its text as one would absorb any error whose message
        happened to contain that word."""
        from botocore.exceptions import ClientError
        assert driver._is_condition_failure(ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")) is True
        assert driver._is_condition_failure(ClientError(
            {"Error": {"Code": "ThrottlingException"}}, "UpdateItem")) is False
        assert driver._is_condition_failure(
            Exception("ConditionalCheckFailedException")) is False

    def test_the_fake_table_upserts_like_dynamodb_does(self):
        """A guard on the test double itself, because the double is what hid this.

        This fake used to return early when update_item named a key with no row, so the
        phantom-row write simply evaporated in every test -- the defect was not merely
        untested, it was untestable. A double more forgiving than production hides
        exactly the bugs production will have."""
        t = FakeTable()
        t.update_item(Key={"run_id": "ghost"}, UpdateExpression="SET #s = :v",
                      ExpressionAttributeNames={"#s": "status"},
                      ExpressionAttributeValues={":v": "escalated"})
        assert t.items == [{"run_id": "ghost", "status": "escalated"}], (
            "the fake dropped an unconditional write to an absent key; real DynamoDB "
            "creates the row")
        # ...and attribute_exists gates that creation, which is the fix under test.
        t2 = FakeTable()
        with pytest.raises(Exception) as ei:
            t2.update_item(Key={"run_id": "ghost"}, UpdateExpression="SET #s = :v",
                           ConditionExpression="attribute_exists(run_id)",
                           ExpressionAttributeNames={"#s": "status"},
                           ExpressionAttributeValues={":v": "escalated"})
        assert ei.value.response["Error"]["Code"] == "ConditionalCheckFailedException"
        assert t2.items == []

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

    @staticmethod
    def _seed_run(c, run_id, status):
        c["ddb"].Table("llmops-pipeline-runs").put_item(
            Item={"run_id": run_id, "status": status})

    @pytest.mark.parametrize("status", ["escalated", "failed", "completed",
                                        "stopped-smoke-verification"])
    def test_a_verdict_for_a_run_that_cannot_hear_it_is_not_reported_resolved(self, status):
        """take_directive has exactly ONE caller: the checkpoint branch of a LIVE driver
        invocation. So a verdict addressed to a run whose execution has ENDED lands in a
        mailbox nobody will open again -- and this branch used to return
        {"status": "resolved"} regardless.

        That is how #16 stayed open for three days: run-20260729T104648Z-41631739 was
        already `escalated`, its token failed and its execution FAILED at 11:19:55Z, so
        triaging it would have reported success and changed NOTHING. Same class as the
        stranded task token (#52): the write is authorized, and unreachable.

        Asserted on the returned status ONLY. The follow-up turn and the audit flag are
        separate properties with their own guards below, and asserting them here made this
        test go red for three unrelated reasons -- which is how a suite ends up with
        guards that look independent and are not."""
        ac = FakeAgentCore([
            tool_use_stream("resolve_escalation",
                            {"run_id": "run-dead-1", "decision": "option_B",
                             "adjusted_params": {"teacher_cap_usd": 39}}),
            tool_use_stream("stage_complete", {"outputs": []}),
            text_stream("ack")])
        c = clients(ac)
        self._seed_run(c, "run-dead-1", status)
        out = driver.handler(driver_event(stage="orchestrator", task="triage",
                                          run_id="run-orch-1"), clients=c)

        assert out["status"] != "resolved", \
            f"a verdict for a {status} run was reported as a resolution"

    def test_an_undeliverable_verdict_is_rejected_back_so_triage_can_still_act(self):
        """The rejection must ride back into the SAME turn, not end it. A conductor told
        "undeliverable" can relaunch the work via launch_run or escalate to page_human;
        returning would leave triage having done nothing, which is the bug being fixed.

        This is the ONLY guard on the follow-up turn and on the rejection's wording, so a
        `return` here fails exactly this test and not the reachability guards above."""
        ac = FakeAgentCore([
            tool_use_stream("resolve_escalation",
                            {"run_id": "run-dead-2", "decision": "raise_cap"}),
            tool_use_stream("page_human", {"reason": "needs owner authority"}),
            text_stream("ack")])
        c = clients(ac)
        self._seed_run(c, "run-dead-2", "escalated")
        driver.handler(driver_event(stage="orchestrator", task="triage",
                                    run_id="run-orch-1"), clients=c)
        assert len(ac.calls) >= 2, \
            "the conductor never got another turn to act on the rejection"
        answer = json.loads(
            ac.calls[1]["messages"][-1]["content"][0]["toolResult"]["content"][0]["text"])
        assert answer["status"] == "undeliverable"
        assert answer["run_status"] == "escalated"
        assert "launch_run" in answer["reason"], \
            "a rejection must name the path that CAN act, or triage just stops"

    def test_a_verdict_for_a_live_run_is_still_delivered_and_reported_resolved(self):
        """The reachability check must not break the case it is guarding. A `running`
        run has a listener, so the verdict is delivered and the turn resolves -- a guard
        that refused every directive would 'fix' the silence by making it total."""
        ac = FakeAgentCore([
            tool_use_stream("resolve_escalation",
                            {"run_id": "run-live-1", "decision": "option_B",
                             "adjusted_params": {"teacher_cap_usd": 39}}),
            text_stream("ack")])
        c = clients(ac)
        self._seed_run(c, "run-live-1", "running")
        out = driver.handler(driver_event(stage="orchestrator", task="triage",
                                          run_id="run-orch-1"), clients=c)
        assert out["status"] == "resolved"
        pending = driver.take_directive(c["ddb"], "run-live-1")
        assert pending and pending["adjusted_params"] == {"teacher_cap_usd": 39}

    def test_an_unknown_run_is_treated_as_reachable_not_silently_dropped(self):
        """A run row that cannot be read must NOT withhold the verdict. The failure being
        fixed is a silent no-op; refusing to deliver on a transient DDB error or an
        unseeded row would invent a second one, in the same direction."""
        ac = FakeAgentCore([
            tool_use_stream("resolve_escalation",
                            {"run_id": "run-unknown-1", "decision": "retry"}),
            text_stream("ack")])
        c = clients(ac)  # no run row seeded at all
        out = driver.handler(driver_event(stage="orchestrator", task="triage",
                                          run_id="run-orch-1"), clients=c)
        assert out["status"] == "resolved"
        assert driver.take_directive(c["ddb"], "run-unknown-1")

    def test_the_audit_record_survives_even_when_the_verdict_cannot_be_delivered(self):
        """Undeliverable is not un-decided. The stage-event and the directive row are both
        still written -- flagged deliverable=False -- because what the conductor decided is
        evidence regardless of whether anyone acted on it."""
        ac = FakeAgentCore([
            tool_use_stream("resolve_escalation",
                            {"run_id": "run-dead-3", "decision": "abort",
                             "rationale": "2-sample scale has no quality signal"}),
            tool_use_stream("stage_complete", {"outputs": []}),
            text_stream("ack")])
        c = clients(ac)
        self._seed_run(c, "run-dead-3", "escalated")
        driver.handler(driver_event(stage="orchestrator", task="triage",
                                    run_id="run-orch-1"), clients=c)
        rows = c["ddb"].Table("llmops-stage-events").items
        parked = [r for r in rows
                  if str(r.get("sk", "")).startswith(driver.DIRECTIVE_SK)]
        assert parked, "the decision was lost entirely, not merely undelivered"
        assert parked[0]["deliverable"] is False
        assert parked[0]["run_status_at_put"] == "escalated"
        assert any("EscalationResolved" in str(r.get("sk", "")) for r in rows)

    def test_the_above_authority_exit_actually_pages_the_owner(self):
        """page_human is the conductor's ONLY exit when a decision is above its authority
        -- and the exit the driver's own undeliverable-verdict rejection names. It was
        declared on the orchestrator harness from Phase 5 and serviced only by the
        CONSOLE chat worker, so on the driver path (every triage invocation) it hit the
        unknown-tool branch and answered {"status": "unsupported"}: no SNS, no event, no
        owner told.

        Live, 2026-08-01 13:45Z: #53's fix correctly rejected the verdict as
        undeliverable and named launch_run/page_human. The conductor re-called
        resolve_escalation, was rejected again, wrote two plan files to S3, and the turn
        ended -- zero runs dispatched, zero pages sent. A rejection naming a path that
        answers "unsupported" is a dead end dressed as a choice."""
        ac = FakeAgentCore([
            tool_use_stream("page_human",
                            {"run_id": "run-stuck-9",
                             "situation": "teacher budget 7.5x under-planned",
                             "options": ["raise the cap", "cut coverage"],
                             "recommendation": "raise the cap, preserve coverage"}),
            text_stream("ack")])
        c = clients(ac)
        out = driver.handler(driver_event(stage="orchestrator", task="triage",
                                         run_id="run-orch-1"), clients=c)
        assert out["status"] == "paged", \
            "page_human fell through to the unknown-tool branch; the owner was never told"
        assert c["sns"].published, "an above-authority decision reached no human at all"
        brief = json.loads(c["sns"].published[0]["Message"])
        assert brief["recommendation"] == "raise the cap, preserve coverage"
        assert brief["options"] == ["raise the cap", "cut coverage"]

    def test_a_page_is_recorded_on_the_stuck_run_not_the_triaging_run(self):
        """Same addressing rule as put_directive, for the same reason: the timeline a
        reader opens is the stuck run's, not the conductor's."""
        ac = FakeAgentCore([
            tool_use_stream("page_human",
                            {"run_id": "run-stuck-9", "situation": "s",
                             "recommendation": "r"}),
            text_stream("ack")])
        c = clients(ac)
        driver.handler(driver_event(stage="orchestrator", task="triage",
                                    run_id="run-orch-1"), clients=c)
        paged = [r for r in c["ddb"].Table("llmops-stage-events").items
                 if "HumanPaged" in str(r.get("sk", ""))]
        assert paged, "the page left no audit record on the run it was about"
        assert paged[0]["run_id"] == "run-stuck-9", \
            f"the page was filed against the conductor's own run: {paged[0]['run_id']}"

    def test_a_page_with_no_recommendation_is_rejected_back_not_sent(self):
        """A page reading "needs a human" with no recommendation hands the owner the
        problem and none of the analysis the conductor already did -- which leaves them
        exactly where they started. Rejected into the same turn so it can add one."""
        ac = FakeAgentCore([
            tool_use_stream("page_human", {"run_id": "run-stuck-9",
                                           "situation": "budget blown"}),
            tool_use_stream("page_human",
                            {"run_id": "run-stuck-9", "situation": "budget blown",
                             "recommendation": "approve $13 cap"}),
            text_stream("ack")])
        c = clients(ac)
        out = driver.handler(driver_event(stage="orchestrator", task="triage",
                                         run_id="run-orch-1"), clients=c)
        first = json.loads(
            ac.calls[1]["messages"][-1]["content"][0]["toolResult"]["content"][0]["text"])
        assert first["status"] == "rejected"
        assert "recommendation" in first["reason"]
        assert out["status"] == "paged", "the corrected page never went out"
        assert len(c["sns"].published) == 1, \
            "the incomplete page was sent anyway, then sent again"

    def test_every_triage_tool_is_serviced_on_the_driver_path(self):
        """The existing drift guard asks whether a declared tool is serviced ANYWHERE,
        and that is what let page_human ship half-wired: the console handled it, the
        driver did not, and only the driver runs a triage.

        So the guard is per-path. The tools the orchestrator's triage protocol names must
        be serviced by the DRIVER, because an EscalatedToHuman event routes to the driver
        and there is no chat session anywhere near it. Derived from the prompt's own
        triage clause, not a hand-kept list."""
        h = json.loads((REPO / "agents/orchestrator/harness.json").read_text())
        prompt = h["systemPrompt"][0]["text"]
        triage_clause = prompt.split('- "triage"')[1].split('- "report"')[0]
        declared = {t["name"] for t in h["tools"] if t["type"] == "inline_function"}
        named_in_triage = {n for n in declared if n in triage_clause}
        assert {"resolve_escalation", "page_human"} <= named_in_triage, \
            "the triage clause stopped naming its own exits; this guard went vacuous"
        driver_src = (REPO / "orchestration/harness_driver/handler.py").read_text()
        unserviced = {n for n in named_in_triage
                      if f'name == "{n}"' not in driver_src}
        assert not unserviced, (
            f"the triage protocol tells the conductor to call {unserviced}, but the "
            "driver -- the only thing that runs a triage -- does not service them; "
            "they return 'unsupported' and the escalation stays stuck")

    def test_running_is_the_only_state_with_a_listener(self):
        """Derived, not hand-listed: every terminal status any writer in the repo can put
        on a run must be treated as unreachable. A status added later (a new terminal
        marker) that this tuple does not know about would silently go back to filing
        verdicts into a dead mailbox."""
        assert "running" not in driver.UNREACHABLE_RUN_STATES
        for status in ("escalated", "failed", "completed"):
            assert status in driver.UNREACHABLE_RUN_STATES

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

    def test_a_serviced_tool_call_resets_the_re_ask_budget(self):
        """The re-ask budget must count CONSECUTIVE prose turns, not lifetime ones.

        Live failure this pins: run b56281da's eval agent ended two early turns in
        prose (burning both re-asks), then worked correctly through checkpoints for
        an hour -- and the moment one more turn ended in prose, the driver failed the
        stage with MissingStageComplete while the agent's third SageMaker relaunch
        was healthy and mid-flight. A lifetime budget punishes an agent for sins it
        already recovered from; the reset makes any serviced tool call re-arm it."""
        uri = "s3://llmops-data-test/runs/run-test-1/raw/data.jsonl"
        ac = FakeAgentCore([
            text_stream("narrating instead of calling"),        # re_ask 1
            text_stream("still narrating"),                     # re_ask 2 (exhausted)
            tool_use_stream("checkpoint", {"next_action": "resume work"}),  # re-arms
            text_stream("prose again after the checkpoint"),    # re_ask 1 (fresh)
            text_stream("and once more"),                       # re_ask 2 (fresh)
            tool_use_stream("stage_complete", {"outputs": [uri]}),
            text_stream("ack")])
        c = clients(ac, FakeS3(existing=[uri]))
        out = driver.handler(driver_event(), clients=c)
        assert out["status"] == "completed", (
            "four prose turns split 2+2 around a healthy checkpoint killed the stage "
            "-- the budget is still lifetime, not consecutive")
        assert not c["sfn"].failures, "MissingStageComplete fired despite the reset"

    def test_re_ask_budget_survives_a_self_reinvoke(self):
        """'Consecutive' is counted across Lambda invocations: two prose turns split
        by a self-reinvoke are still consecutive. The counter rides the continuation
        payload as _re_asks -- if a refactor drops it from the handoff, every
        reinvoke silently refills the budget and MissingStageComplete can never fire
        on long stages, exactly where the protocol violations happen."""
        src = (REPO / "orchestration/harness_driver/handler.py").read_text()
        body = src[src.index("def _run_stage"):]
        assert '"_re_asks": re_asks' in body, \
            "_self_reinvoke stopped carrying the re-ask counter"
        assert 'int(event.get("_re_asks", 0))' in body, \
            "the continuation branch stopped restoring the re-ask counter"

    def test_a_stream_that_outlives_the_lambda_wall_hands_off_instead_of_dying(self):
        """boto's read_timeout (870s) bounds the gap BETWEEN chunks, not the stream's
        life: a reasoning model trickling a chunk every few seconds can stream past
        the Lambda's 900s wall inside one _drain call, where the between-turns
        _out_of_time() check can never look. Live: run 68cfa9c8's resumed generate
        turn hit the wall mid-stream (REPORT ... Duration: 900000.00 ms Status:
        timeout, 03:39:49Z); the async Lambda retry then replayed a continuation
        whose session no longer matched and the stage died MissingStageComplete
        with 51 tasks still to run. _drain now watches the clock between chunks and
        hands the turn to a fresh invocation with the whole 900s available."""
        class _Ctx:
            function_name = "llmops-harness-driver"

            def __init__(self):
                self.reads = 0

            def get_remaining_time_in_millis(self):
                # plenty of wall at turn start; below the drain margin after the
                # stream has trickled for a while
                self.reads += 1
                return 860_000 if self.reads < 10 else 30_000

        class _Lam:
            def __init__(self):
                self.invocations = []

            def invoke(self, **kw):
                self.invocations.append(kw)
                return {"StatusCode": 202}

        ac = FakeAgentCore([TricklingStream()])
        c = clients(ac)
        c["lambda"] = _Lam()
        out = driver.handler(driver_event(), clients=c, context=_Ctx())
        assert out["status"] == "self_reinvoked_between_turns", (
            "a stream that outlived the wall was not handed off — the runtime would "
            "have killed this invocation mid-stream")
        assert not c["sfn"].failures, "the deadline cut was treated as a failure"
        payload = json.loads(c["lambda"].invocations[0]["Payload"])
        assert payload.get("_continuation"), (
            "the handoff lost the salvage continuation — the resumed invocation "
            "would restart the stage from scratch")

    def test_a_deadline_cut_does_not_burn_the_stream_salvage_retry(self):
        """The one same-session salvage retry exists for involuntary stream deaths.
        A deadline cut is voluntary — spending the retry on it leaves a REAL death
        later in the same stage unprotected. The handoff must not set
        _stream_retried."""
        class _Ctx:
            function_name = "llmops-harness-driver"

            def __init__(self):
                self.reads = 0

            def get_remaining_time_in_millis(self):
                self.reads += 1
                return 860_000 if self.reads < 10 else 30_000

        class _Lam:
            def __init__(self):
                self.invocations = []

            def invoke(self, **kw):
                self.invocations.append(kw)
                return {"StatusCode": 202}

        ac = FakeAgentCore([TricklingStream()])
        c = clients(ac)
        c["lambda"] = _Lam()
        driver.handler(driver_event(), clients=c, context=_Ctx())
        payload = json.loads(c["lambda"].invocations[0]["Payload"])
        assert payload.get("_stream_retried") is False, (
            "the deadline handoff spent the stream-salvage retry")

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

    def test_a_completed_eval_inference_job_resumes_without_announcing_a_training(self):
        """Eval-stage inference rides the SageMaker training-job rail (same EventBridge
        rule, same parked token), but it is not a training: MODEL_TRAINED on the bus
        for a batch-scoring job would be a lie on the timeline. The run row's
        current_stage — written by handle_job_launched — is what tells them apart.
        The token still settles identically; EvalScore's stage_complete emits
        MODEL_EVALUATED moments later, so eval jobs get no bus event here at all."""
        c = self._clients({"run_id": "run-1", "task_token": "tok-9",
                           "current_stage": "eval"})
        out = resume_pipeline.handler(
            sm_event("Completed", job="llmops-eval-infer-1",
                     ModelArtifacts={"S3ModelArtifacts": "s3://b/out.tar.gz"}),
            clients=c)
        assert out["outcome"] == "resumed"
        assert c["sfn"].successes, "the eval token was never settled"
        assert not any(e["DetailType"] == ev.MODEL_TRAINED
                       for e in c["events"].entries), (
            "a student-inference completion was announced as MODEL_TRAINED")

    def test_a_zero_billed_stop_relaunches_without_spending_an_iteration(self):
        """Stopped with $0 billed is capacity, not code: the job never ran (Pending
        time is unbilled) and proved nothing. Before this branch existed, a capacity
        race loser fired TrainingJobFailed and spent a remediation iteration on
        weather — with only 3 in the budget, two starved instance types and one real
        bug exhausted a run that did nothing wrong."""
        c = self._clients({"run_id": "run-1", "task_token": "tok-9"})
        out = resume_pipeline.handler(
            sm_event("Stopped", BillingSecondsUsed=0), clients=c)
        assert out["outcome"] == "capacity-relaunch"
        assert c["sfn"].failures[0]["error"] == "CapacityStopped", (
            "the launch state's Catch routes on this exact error name")
        assert any(e["DetailType"] == ev.CAPACITY_STOPPED
                   for e in c["events"].entries)
        assert not any(e["DetailType"] == ev.PIPELINE_FAILED
                       for e in c["events"].entries), (
            "a free relaunch announced itself as a pipeline failure")
        # the retry counter rides the token-clear write: a crash between two separate
        # writes would hand out an uncounted free relaunch
        upd = c["ddb"].Table(ENV["RUNS_TABLE"]).updates[0]
        assert "capacity_retries" in upd["UpdateExpression"]
        assert upd["ExpressionAttributeValues"][":n"] == 1

    def test_a_billed_stop_is_a_real_failure(self):
        """A stop that billed seconds was a judgment call on a RUNNING job — an
        operator or guard stopped real work. That keeps the TrainingJobFailed path
        and its remediation-iteration cost; only never-ran stops are free."""
        c = self._clients({"run_id": "run-1", "task_token": "tok-9"})
        out = resume_pipeline.handler(
            sm_event("Stopped", BillingSecondsUsed=1200), clients=c)
        assert out["outcome"] == "failed"
        assert c["sfn"].failures[0]["error"] == "TrainingJobFailed"

    def test_the_fourth_capacity_stop_stops_being_free(self):
        """The CapacityStopped Catch re-enters the launch state without
        IncrementIteration, so the ASL has no loop guard of its own — this cap is
        it. A permanently starved instance type must eventually become a real
        failure someone gets paged about, not an infinite quiet relaunch loop."""
        c = self._clients({"run_id": "run-1", "task_token": "tok-9",
                           "capacity_retries": 3})
        out = resume_pipeline.handler(
            sm_event("Stopped", BillingSecondsUsed=0), clients=c)
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

    @staticmethod
    def _client_error(code):
        from botocore.exceptions import ClientError
        return ClientError({"Error": {"Code": code, "Message": code}}, "SendTaskSuccess")

    def _sfn_raising(self, c, exc):
        def boom(**kw):
            raise exc
        c["sfn"].send_task_success = boom
        c["sfn"].send_task_failure = boom
        return c

    @pytest.mark.parametrize("code", ["TaskTimedOut", "TaskDoesNotExist"])
    @pytest.mark.parametrize("status", ["Completed", "Failed"])
    def test_a_dead_token_is_still_cleared_from_the_run_row(self, code, status):
        """A token Step Functions has already discarded is stale data, not a pending
        obligation. Live: run-20260729T104648Z-41631739 held a task_token for an
        execution that ended 2026-07-29T11:19:55Z and still held it three days later,
        because the settle raised (AccessDenied on the clear, then TaskTimedOut,
        'Provided task does not exist anymore', over ~5 deliveries) and every retry
        raised before reaching the REMOVE. Both terminal statuses and both
        already-gone error codes, because the clear sat after a two-branch if."""
        c = self._sfn_raising(self._clients({"run_id": "run-1", "task_token": "tok-9"}),
                              self._client_error(code))
        out = resume_pipeline.handler(sm_event(status), clients=c)
        assert out["outcome"] == "token-already-gone"
        assert code in out["settle_error"]
        updates = c["ddb"].Table(ENV["RUNS_TABLE"]).updates
        assert updates and "REMOVE task_token" in updates[0]["UpdateExpression"], \
            "a token whose execution is over stayed parked in the run row"

    @pytest.mark.parametrize("code", ["ThrottlingException", "InvalidToken",
                                      "InternalServerError"])
    def test_a_settle_that_might_still_land_is_retried_and_the_token_kept(self, code):
        """The discriminating half. Clearing on EVERY failure would be worse than the
        bug: the token is the pipeline's only way to learn a paid-for stage finished, so
        a throttle or a 5xx must reraise for EventBridge to retry, and must NOT clear.
        Only 'the task is gone' is safe to absorb."""
        c = self._sfn_raising(self._clients({"run_id": "run-1", "task_token": "tok-9"}),
                              self._client_error(code))
        with pytest.raises(Exception) as ei:
            resume_pipeline.handler(sm_event("Completed"), clients=c)
        assert code in str(ei.value)
        assert not c["ddb"].Table(ENV["RUNS_TABLE"]).updates, \
            "a retryable settle failure cleared the token the retry still needs"

    def test_a_failure_to_clear_the_token_is_raised_not_swallowed(self):
        """The 2026-07-29 clear failed with AccessDenied and that was invisible: the
        traceback that followed was about the settle, not about this write. If the field
        cannot be cleared, the run row is wrong and the caller has to hear about it."""
        c = self._clients({"run_id": "run-1", "task_token": "tok-9"})
        table = c["ddb"].Table(ENV["RUNS_TABLE"])

        def denied(**kw):
            raise self._client_error("AccessDeniedException")
        table.update_item = denied
        with pytest.raises(Exception) as ei:
            resume_pipeline.handler(sm_event("Completed"), clients=c)
        assert "AccessDenied" in str(ei.value)
        # The settle still happened -- the pipeline moved on; only bookkeeping failed.
        assert c["sfn"].successes, "the token was not settled before the clear was tried"


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
        # remediation loops BACK to analysis -> eval, closing the self-iteration loop.
        # Reachability, not a literal Next, for the reason _reaches documents: this
        # asserted FinetuneAnalyze["Next"] == "EvalGate" and broke when EvalGenerate was
        # inserted between them, though the guarantee -- remediation returns to analysis
        # and reaches the gate again -- held throughout.
        assert states["RemediateFinetune"]["Next"] == "FinetuneAnalyze"
        assert _reaches(states, "FinetuneAnalyze", "EvalGate")
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

    def test_a_successful_run_is_closed_out_too_not_only_a_failed_one(self, asl):
        """The failure path was fixed and the success path was not, and the asymmetry
        survived precisely because it could not be seen: runs.status is written 'running'
        by start_pipeline, 'escalated' by the driver, 'failed' by MarkRunFailed -- and
        'completed' by nothing at all. stage_complete updates the manifest and settles the
        task token; it never touches the run's own status.

        So a run that SUCCEEDED sat at status=running forever, the same zombie
        MarkRunFailed exists to prevent, on the other branch. It stayed invisible because
        no execution had ever succeeded before: run-20260801T062313Z-4d3e2e69 was the
        first, and five hours after it finished its task row read 'completed' while its
        run row still read 'running'.

        Asserted as a property of the success path rather than by naming the state, so
        rerouting Complete elsewhere cannot quietly drop the closer.
        """
        states = asl["States"]
        closers = [n for n, st in states.items()
                   if st.get("Resource", "").endswith("dynamodb:updateItem")
                   and st["Parameters"]["TableName"] == "llmops-pipeline-runs"
                   and "completed" in json.dumps(
                       st["Parameters"]["ExpressionAttributeValues"])]
        assert closers, (
            "no state writes status=completed to llmops-pipeline-runs; a successful run "
            "stays at 'running' forever and the console shows it as still in flight")
        for name in closers:
            assert _reaches(states, "Complete", name) or name == "Complete", (
                f"{name} writes the completed status but the success path never reaches "
                "it, so the run record is still never closed")
        # And it must not turn a successful pipeline into a failed execution.
        for name in closers:
            assert _terminals_from(states, name) == {"Succeed"}, (
                f"{name} can end the execution somewhere other than Succeed; closing a "
                "record is bookkeeping and must never change the run's verdict")

    def test_closing_a_successful_run_never_overwrites_a_richer_status(self, asl):
        """Mirrors the MarkRunFailed guard. The driver may already have written
        'escalated' -- more informative than 'completed' -- and a DDB outage must not
        fail an execution whose pipeline genuinely succeeded."""
        states = asl["States"]
        closer = next(n for n, st in states.items()
                      if st.get("Resource", "").endswith("dynamodb:updateItem")
                      and st["Parameters"]["TableName"] == "llmops-pipeline-runs"
                      and "completed" in json.dumps(
                          st["Parameters"]["ExpressionAttributeValues"]))
        params = states[closer]["Parameters"]
        assert "running" in json.dumps(params["ExpressionAttributeValues"]), (
            "the write must be conditional on the run still being 'running', or it "
            "would clobber the driver's richer 'escalated'")
        assert "States.ALL" in states[closer]["Catch"][0]["ErrorEquals"]
        assert _terminals_from(states, states[closer]["Catch"][0]["Next"]) == {"Succeed"}

    def test_both_records_are_closed_on_both_paths(self, asl):
        """The run record and the task record are separate records with separate reasons
        to go stale, and each path has now been caught missing one of them: the failure
        path closed the run but not the task (task-58ecde82adcd73bf sat at 'dispatched'
        for a day), the success path closed the task but not the run. Assert the full
        2x2 rather than the one cell that was most recently broken.
        """
        states = asl["States"]
        for start, table, want in (("Complete", "llmops-pipeline-runs", "completed"),
                                   ("Complete", "llmops-tasks", "completed"),
                                   ("EscalateFail", "llmops-pipeline-runs", "failed"),
                                   ("EscalateFail", "llmops-tasks", "failed")):
            hit = [n for n, st in states.items()
                   if st.get("Resource", "").endswith("dynamodb:updateItem")
                   and st["Parameters"]["TableName"] == table
                   and want in json.dumps(st["Parameters"]["ExpressionAttributeValues"])
                   and _reaches(states, start, n)]
            assert hit, (
                f"nothing reachable from {start} writes {want!r} to {table}; that record "
                "stays open and the console keeps showing finished work as in flight")

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

    def test_a_stage_that_deletes_the_endpoint_keeps_a_short_timeout(self, asl):
        """The asymmetry is deliberate and it is the whole safety argument for 86400.

        TimeoutSeconds is the only real ceiling on a stage: the driver Lambda's 900s
        limit does not bound it (it self-reinvokes via _continuation), so the task token
        surviving for TimeoutSeconds is what keeps a long stage alive. Raising the six
        states that wait on real work to a day is therefore correct -- a 480-teacher-call
        generation run does not fit in 7200s and was cut off mid-work by it.

        Applying the same day to the CLEANUP states would not be. Teardown is what
        deletes the endpoint; MonitorHealth and MonitorReport sit on the only path to it.
        A wedged Teardown at 86400 leaves an ml.g5.2xlarge InService for a full day at
        $1.515/hr -- the exact shape of the 843-day, 0-invocation orphan this project
        already paid for and deleted on 2026-08-02. So the cost-bearing states keep an
        hour, and a raise that reached them would be a cost regression disguised as a
        reliability improvement.

        Asserted as an upper bound rather than an equality: tightening a cleanup timeout
        is always safe and should not need this test edited.
        """
        CLEANUP = {"Teardown", "MonitorHealth", "MonitorReport", "SmokeTest",
                   "Deploy", "DataAudit", "FinetuneAnalyze"}
        LONG_WORK = {"DataPrepGenerate", "DataPrepCurate", "FinetuneLaunch",
                     "EvalGenerate", "EvalScore", "EvalGate", "RemediateFinetune"}
        timed = {n for n, st in asl["States"].items() if "TimeoutSeconds" in st}
        assert timed == CLEANUP | LONG_WORK, (
            f"the timeout policy names {sorted(CLEANUP | LONG_WORK)} but the ASL times "
            f"{sorted(timed)}; a new timed state must be classified, not defaulted -- "
            "an unclassified state is how a cleanup stage inherits a 24-hour ceiling")
        for n in sorted(CLEANUP):
            assert asl["States"][n]["TimeoutSeconds"] <= 7200, (
                f"{n} is on the endpoint-lifecycle path with TimeoutSeconds "
                f"{asl['States'][n]['TimeoutSeconds']}; a wedged cleanup stage holding a "
                "GPU endpoint for that long is the orphan-endpoint cost this project "
                "already paid once")
        for n in sorted(LONG_WORK):
            assert asl["States"][n]["TimeoutSeconds"] == 86400, (
                f"{n} waits on real agent work and has TimeoutSeconds "
                f"{asl['States'][n]['TimeoutSeconds']}, not the 86400 the owner set; "
                "the 7200 it was cut off at is what this change exists to remove")

    def test_a_heartbeat_interval_requires_something_to_send_heartbeats(self, asl):
        """HeartbeatSeconds without a sender is not a liveness signal -- it is a second,
        SHORTER deadline, and one nobody reads as a deadline.

        FinetuneLaunch and RemediateFinetune carried `HeartbeatSeconds: 18000` beside
        `TimeoutSeconds: 21600` from the day they were written. Step Functions resets the
        heartbeat clock on SendTaskHeartbeat and fails the state with States.Timeout if
        the interval elapses without one. Nothing in this platform has ever called it:
        the driver settles a token with SendTaskSuccess/SendTaskFailure and nothing else,
        even though the IAM role grants states:SendTaskHeartbeat. So the first heartbeat
        never arrived and both states really died at 18000s while their ASL said 21600 --
        the console's hover card even rendered a "heartbeat 18000s" row, which reads as
        *we monitor liveness* rather than *this stage has a 5-hour cap you cannot see*.

        It surfaced when the six long-work states were raised to 86400 on the platform
        owner's instruction: those two would have kept dying at 5 hours while every
        surface reported a day. The number in the ASL not being the number that fires is
        precisely the defect class this suite exists for.

        So the field is allowed back ONLY together with a sender. This test is what will
        let it in -- it does not forbid heartbeats, it forbids the half of the pair that
        looks reassuring on its own.
        """
        with_hb = sorted(n for n, st in asl["States"].items()
                         if "HeartbeatSeconds" in st)
        if not with_hb:
            return
        senders = []
        for rel in ("orchestration/harness_driver/handler.py",
                    "orchestration/resume_pipeline/handler.py",
                    "orchestration/start_pipeline/handler.py"):
            p = REPO / rel
            if p.exists() and "send_task_heartbeat" in p.read_text():
                senders.append(rel)
        assert senders, (
            f"{with_hb} declare a heartbeat interval but no Lambda calls "
            "send_task_heartbeat, so the first heartbeat never arrives and the interval "
            "is a shorter timeout wearing a liveness signal's name")
        # And the pair must be consistent: an interval at or above the timeout can never
        # fire, which is a different way of writing a field that does nothing.
        for n in with_hb:
            st = asl["States"][n]
            assert st["HeartbeatSeconds"] < st.get("TimeoutSeconds", float("inf")), (
                f"{n}: HeartbeatSeconds {st['HeartbeatSeconds']} is not below its "
                f"TimeoutSeconds {st.get('TimeoutSeconds')}, so it can never fire")

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
        """Both of SmokeTest's exits must REACH Teardown -- not neighbour it.

        This asserted `Next == "Teardown"` literally, and MonitorHealth landing between
        them broke the test while strengthening the property: health reads the endpoint's
        metrics while it is still alive, and both of its own exits go to Teardown. The
        invariant that matters is "the endpoint is never orphaned", i.e. no path out of
        SmokeTest can miss the delete -- so follow the path instead of naming one hop of
        it, or the next state inserted here fails a test that should pass and, worse, a
        state inserted with a path that ESCAPES teardown passes one that should fail.
        """
        states = asl["States"]

        def reaches_teardown(start, seen=None):
            seen = seen or set()
            if start in seen:
                return False          # a cycle that never reaches Teardown
            if start == "Teardown":
                return True
            seen = seen | {start}
            st = states[start]
            nxt = [st["Next"]] if st.get("Next") else []
            nxt += [c["Next"] for c in st.get("Catch", [])]
            # A terminal state that is not Teardown is a leak, and `not nxt` -> all() is
            # vacuously True, so say so explicitly rather than letting it pass.
            if not nxt:
                return False
            return all(reaches_teardown(n, seen) for n in nxt)

        smoke = states["SmokeTest"]
        for exit_name, target in [("Next", smoke["Next"])] + \
                [("Catch", c["Next"]) for c in smoke["Catch"]]:
            assert reaches_teardown(target), (
                f"SmokeTest's {exit_name} goes to {target}, from which some path never "
                "reaches Teardown -- the endpoint can be orphaned, which is the #1 cost "
                "risk in the platform")

    def test_monitor_health_reads_metrics_while_the_endpoint_still_exists(self, asl):
        """MonitorHealth must sit AFTER SmokeTest and BEFORE Teardown, and gate nothing.

        Placement here is forced by the shape of the work, not taste. Teardown deletes the
        endpoint on every path including SmokeTest's Catch, and after the delete
        `cloudwatch:GetMetricData` returns an empty series for it -- indistinguishable from
        a healthy endpoint sitting idle. So the only window in which the health task can
        answer its question at all is between those two states. And it must not gate: a
        CloudWatch read that fails cannot be allowed to strand the endpoint it was
        watching, because the endpoint bills whether or not we managed to measure it.
        """
        states = asl["States"]
        health = states["MonitorHealth"]
        payload = health["Parameters"]["Payload"]
        assert (payload["stage"], payload["task"]) == ("monitor", "health")
        assert payload["harness_id"] == "llmops_monitor"

        assert states["SmokeTest"]["Next"] == "MonitorHealth", \
            "SmokeTest's success exit must reach health before the endpoint is deleted"
        assert health["Next"] == "Teardown"
        assert [c["Next"] for c in health["Catch"]] == ["Teardown"], \
            "a failed metric read must still delete the endpoint -- observation, not a gate"
        assert health.get("ResultPath", "").startswith("$."), \
            "health must not replace the state: Teardown and the closeout still need $.run_id"

    def test_monitor_report_runs_after_teardown_on_the_finished_manifest(self, asl):
        """The narrative is written last, and cannot fail a run that succeeded.

        `report` consolidates the run's story from the finished manifest, so it has to run
        after the final stage has written to it -- a report composed before Teardown would
        omit the teardown it exists to confirm. And its Catch goes to Complete: a report
        that failed to write must not change a run's terminal state. The narrative is a
        deliverable; the run's outcome is a fact.
        """
        states = asl["States"]
        report = states["MonitorReport"]
        payload = report["Parameters"]["Payload"]
        assert (payload["stage"], payload["task"]) == ("monitor", "report")
        assert states["Teardown"]["Next"] == "MonitorReport"
        assert report["Next"] == "Complete"
        assert [c["Next"] for c in report["Catch"]] == ["Complete"], \
            "a failed report must not fail a run whose pipeline succeeded"
        assert report.get("ResultPath", "").startswith("$."), \
            "report must not replace the state: MarkRunDone still reads $.run_id"

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

    def test_a_capacity_stop_relaunches_the_same_state_without_incrementing_iteration(self, asl):
        """A CapacityStopped Catch must re-enter the state that launched the job,
        with $.iteration untouched: capacity is weather, and the remediation budget
        (iteration < 3) exists for code failures. If the Catch ever routes through
        IncrementIteration — or anywhere other than straight back — a starved
        instance type starts eating the budget again, which is how a run that did
        nothing wrong got executed. Derived over every state that has the Catch, so
        a launch state added later inherits the assertion."""
        found = []
        for name, st in asl["States"].items():
            for cat in st.get("Catch", []):
                if "CapacityStopped" not in cat.get("ErrorEquals", []):
                    continue
                found.append(name)
                assert cat["Next"] == name, (
                    f"{name}'s CapacityStopped catch goes to {cat['Next']}; a free "
                    "relaunch means re-entering the SAME state")
                assert cat.get("ResultPath") == "$.error", (
                    f"{name}'s capacity catch must file the error under $.error and "
                    "keep $.iteration — replacing the state resets the run's context")
        assert set(found) >= {"FinetuneLaunch", "EvalGenerate"}, (
            f"the capacity exemption covers {found}; both launch-and-release states "
            "park tracked jobs and both race capacity")

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


#: Harness tasks a prompt DECLARES that nothing dispatches, each with the reason it is
#: allowed to stay unreachable. Anything absent from this map must have a dispatcher.
#:
#: This is an allowlist, not a threshold, for the reason SESSION_POSTS is one in
#: test_console_routes.py: a count passes when a swap happens. A newly-orphaned task
#: that replaced a wired one keeps the total intact, and the whole failure this guards
#: against is a task the prompt promises and no path can reach.
#:
#: Every entry is a real gap, not a design. Two are tracked work (#58, and the s3 skill
#: switch that mirror_model belongs to); the rest are prompt surface written ahead of
#: its wiring. They are listed so the suite NAMES them on every run instead of letting
#: them stay invisible -- which is exactly how ("eval", "evaluate") survived to the
#: point of gating a real verdict on a file no path produced.
UNDISPATCHED_HARNESS_TASKS = {
    # llmops_monitor's health/sweep/report were listed here as "the whole harness is
    # unreachable" until #58 wired all three (MonitorHealth and MonitorReport in the ASL,
    # sweep on the daily schedule). main still carried the entries because #58 lives on
    # this branch, so the merge produced an allowlist claiming three DISPATCHED tasks are
    # unreachable -- caught by test_the_allowlist_does_not_outlive_its_entries, which
    # exists because a stale note here is read as evidence the wiring is still missing.
    ("llmops_data_prep", "verify"): "superseded by audit; prompt surface kept for now",
    ("llmops_data_prep", "mirror_model"): "needs the s3 mirror switch (task #42)",
    ("llmops_finetune", "prepare"): "folded into launch; never separately dispatched",
    ("llmops_finops", "pricing_refresh"): "operator-invoked, not scheduled",
    ("llmops_finops", "report"): "operator-invoked, not scheduled",
    ("llmops_orchestrator", "plan"): "consult supersedes it for console-driven runs",
    ("llmops_orchestrator", "report"): "operator-invoked, not scheduled",
    # The one entry here that is a live PRODUCTION GAP rather than unused surface.
    # The prompt says triage begins when "an EscalatedToHuman event arrives", and
    # list_rules(EventBusName="llmops-pipeline") returns [] -- the bus every pipeline
    # event is published to has no rules at all, so nothing converts that event into an
    # invocation. Task #54 made triage behave correctly once invoked; this is why it is
    # not yet reachable. Tracked as #59; it stays listed rather than silently tolerated.
    ("llmops_orchestrator", "triage"): "task #59 -- no rule routes EscalatedToHuman",
}

#: Places a harness task can be dispatched from, besides the state machine.
_DISPATCH_SOURCES = ("orchestration", "deploy")


def _declared_tasks() -> dict:
    """(harness_id, task) -> True for every `params.task` bullet in every prompt.

    Read out of the prompts because the prompt is the contract the agent is held to:
    it tells the model "your job depends on params.task", then enumerates the values.
    A value listed there that no caller can send is a promise with no path to it.
    """
    out = {}
    for cfg in sorted((REPO / "agents").glob("*/harness.json")):
        doc = json.loads(cfg.read_text())
        hid = doc.get("name") or ("llmops_" + cfg.parent.name.replace("-", "_"))
        text = "".join(b.get("text", "") for b in (doc.get("systemPrompt") or []))
        tasks = re.findall(r'- \\?"([a-z_]+)\\?":', text)
        assert tasks, f"{cfg}: parsed no params.task bullets -- the parse is broken"
        for t in tasks:
            out[(hid, t)] = str(cfg.relative_to(REPO))
    return out


def _dispatched_tasks(asl: dict) -> dict:
    """(harness_id, task) -> where it is dispatched from."""
    out = {}
    for name, st in asl["States"].items():
        payload = (st.get("Parameters") or {}).get("Payload") or {}
        if "harness_id" in payload:
            out[(payload["harness_id"], payload.get("task"))] = f"ASL:{name}"
    # A task may also be dispatched by Python (the console's consult path, the finops
    # schedule). Those senders name the harness elsewhere, so the task literal is
    # matched on its own and credited to every harness that declares it -- deliberately
    # generous, because a FALSE "dispatched" here only ever weakens this guard, while a
    # false "orphaned" would force a real dispatcher into the allowlist above.
    literals = set()
    for top in _DISPATCH_SOURCES:
        for path in (REPO / top).rglob("*.py"):
            literals.update(re.findall(r'"task"\s*:\s*"([a-z_]+)"', path.read_text()))
    for (hid, task) in _declared_tasks():
        if task in literals and (hid, task) not in out:
            out[(hid, task)] = "python"
    return out


class TestEveryDeclaredTaskIsReachable:
    """A harness task the prompt declares must have some caller able to send it.

    ("eval", "evaluate") was declared, documented as the producer of
    evaluation/report.json, and dispatched from nowhere -- while ("eval", "gate") was
    dispatched and specified to READ that report. The pipeline's only eval task
    consumed an input no path in the repo produced.

    Nothing surfaced it, because the gate's fail-closed rule (correct, and load-bearing
    for a different bug) renders a missing report as gate_passed=False -- identical to a
    student that genuinely failed. Phase 4's FAILED verdict is trustworthy only because
    eval ran DIRECTLY, outside the machine; through the machine the same verdict would
    have been unfalsifiable.
    """

    def test_no_declared_task_is_unreachable(self, asl):
        declared, dispatched = _declared_tasks(), _dispatched_tasks(asl)
        orphaned = {k: v for k, v in declared.items() if k not in dispatched}
        unexpected = {k: v for k, v in orphaned.items()
                      if k not in UNDISPATCHED_HARNESS_TASKS}
        assert not unexpected, (
            "these harness tasks are declared in a prompt but no caller can send them: "
            + "; ".join(f"{h}/{t} ({src})" for (h, t), src in sorted(unexpected.items()))
            + ". The agent is told the task is its job and no path reaches it. Either "
            "dispatch it, or add it to UNDISPATCHED_HARNESS_TASKS with the reason.")

    def test_the_allowlist_does_not_outlive_its_entries(self, asl):
        """An entry that HAS a dispatcher must leave the allowlist.

        Otherwise wiring a task up leaves behind a note saying it is unreachable, and
        the next reader trusts the note over the machine.
        """
        dispatched = _dispatched_tasks(asl)
        stale = sorted(k for k in UNDISPATCHED_HARNESS_TASKS if k in dispatched)
        assert not stale, (
            f"these are dispatched now but still listed as unreachable: {stale}. "
            "Remove them from UNDISPATCHED_HARNESS_TASKS.")

    def test_the_eval_report_is_produced_before_it_is_gated(self, asl):
        """The producer of evaluation/report.json must precede its consumer.

        Asserted as reachability rather than a literal Next: the guarantee is ordering,
        and it survives a state being inserted between them. Since the eval stage
        became a launch/score pair, the report's producer is the SCORE task (evaluate
        may end at job_launched with no report yet); score is also the only entry
        into the gate, so no route arrives at EvalGate without the report written.
        """
        states = asl["States"]
        producer = [n for n, st in states.items()
                    if ((st.get("Parameters") or {}).get("Payload") or {}).get("task")
                    == "score"]
        assert len(producer) == 1, f"expected exactly one score state, found {producer}"
        launcher = [n for n, st in states.items()
                    if ((st.get("Parameters") or {}).get("Payload") or {}).get("task")
                    == "evaluate"]
        assert len(launcher) == 1, f"expected exactly one evaluate state, found {launcher}"
        assert _reaches(states, launcher[0], producer[0]), (
            f"{launcher[0]} launches the inference {producer[0]} scores, but cannot "
            "reach it")
        assert _reaches(states, producer[0], "EvalGate"), (
            f"{producer[0]} produces evaluation/report.json but cannot reach EvalGate, "
            "which reads it")
        # Ordering is asserted on the ENTRY paths into the gate, not by demanding the
        # gate cannot reach the producer: it can and must, because the remediation
        # loop goes back around and re-evaluates (the next test pins that). What must
        # hold is that no route ARRIVES at EvalGate without passing the producer
        # first -- that is the reading a stale report would need.
        entries = [n for n, st in states.items() if "EvalGate" in _exits(st)]
        assert entries, "nothing transitions into EvalGate"
        assert entries == [producer[0]], (
            f"EvalGate is entered from {entries}; only {producer[0]} may lead into it, "
            "or a run can reach the gate without the report it reads having been written")

    def test_the_remediation_loop_regenerates_the_report_it_regates(self, asl):
        """A second gate attempt must re-run evaluation, not re-read the stale report.

        RemediateFinetune loops back to FinetuneAnalyze. If that path rejoined below
        the evaluate state, iteration 2 would apply the gate to iteration 1's report --
        it would score the OLD student and could "pass" a remediation that changed
        nothing.
        """
        states = asl["States"]
        gen = next(n for n, st in states.items()
                   if ((st.get("Parameters") or {}).get("Payload") or {}).get("task")
                   == "evaluate")
        assert _reaches(states, "RemediateFinetune", gen), (
            "the remediation loop rejoins the pipeline without passing through "
            f"{gen}, so a re-gate would read the previous iteration's report")

    def test_a_stage_event_exists_for_the_evaluate_completion(self):
        """ModelEvaluated was in the vocabulary and emitted by nothing.

        The absent event and the absent dispatcher are the same gap seen from two
        sides, so the fix is only half done if the completion stays silent.
        """
        assert driver.STAGE_EVENT_MAP.get(("eval", "evaluate")) == ev.MODEL_EVALUATED
        emitted = set(driver.STAGE_EVENT_MAP.values()) | {
            ev.QUALITY_GATE_PASSED, ev.QUALITY_GATE_FAILED}
        assert ev.MODEL_EVALUATED in emitted


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

    def test_every_harness_prompt_carries_the_turn_end_invariant_naming_its_own_terminal_tools(self):
        """The driver only recognizes tool calls; a stage that finishes in prose is a
        stage that failed (MissingStageComplete). Four runs in one week died this way —
        the same specialist fault each time, and re-stating the rule louder in per-plan
        params fixed exactly the stages whose plans carried it (run b56281da: data-prep,
        curate and finetune closed correctly; eval, mid-polling, did not). So the rule
        lives in every fleet prompt now, and this guard derives it BOTH directions from
        each harness's own tools[] list: every declared inline function must be named in
        the invariant sentence (an unnamed escape hatch is one the model won't use under
        pressure), and no tool may be named that is not declared (a stale name after a
        tool is removed points the model at a function that returns 'unsupported')."""
        for cfg in sorted((REPO / "agents").glob("*/harness.json")):
            h = json.loads(cfg.read_text())
            prompt = h["systemPrompt"][0]["text"]
            declared = {t["name"] for t in h.get("tools", [])
                        if t.get("type") == "inline_function"}
            assert prompt.count("TURN-END INVARIANT") == 1, (
                f"{cfg.parent.name}: the invariant must appear exactly once, "
                f"found {prompt.count('TURN-END INVARIANT')}")
            sentence = prompt.split("TURN-END INVARIANT")[1].split("\n- ")[0]
            missing = {n for n in declared if n not in sentence}
            assert not missing, (
                f"{cfg.parent.name}: declared tools {missing} are not named in the "
                "turn-end invariant — an exit the rule does not mention is an exit "
                "the model will not take")
            fleet_tools = {"stage_complete", "job_launched", "checkpoint",
                           "escalate_human", "launch_run", "resolve_escalation",
                           "page_human", "write_report", "publish_cost_report",
                           "update_rate_card", "flag_variance"}
            stale = {n for n in fleet_tools - declared if n in sentence}
            assert not stale, (
                f"{cfg.parent.name}: invariant names {stale} which this harness does "
                "not declare — calling it returns 'unsupported' and burns the turn")
            assert "BEFORE the call" in sentence, (
                f"{cfg.parent.name}: the write-first clause is gone — prose is not "
                "proof of work, and neither is a tool call claiming artifacts that "
                "were never written")
        # the orchestrator's consult mode legitimately ends turns in prose (a question
        # to the customer IS the turn); its invariant must carve that out or the agent
        # emits spurious checkpoints mid-conversation and breaks the choices/trailer
        # protocol the console parses.
        orch = json.loads((REPO / "agents/orchestrator/harness.json").read_text())
        orch_sentence = orch["systemPrompt"][0]["text"].split(
            "TURN-END INVARIANT")[1].split("\n- ")[0]
        assert "consult" in orch_sentence, (
            "the orchestrator's invariant lost its consult carve-out — consult turns "
            "that properly end in prose would now read as protocol failures")

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

        The path is read from the git path OR the s3 URI, because this guard asks WHICH
        SKILL is mounted and that question is independent of where the bytes come from. Read
        git-only, it went quietly green-on-nothing at the s3 migration: every skill became
        an empty string, and `want in {""}` is simply False for both agents -- so the day
        the mount was actually intact it failed, and a day the mount was deleted it would
        have failed identically. A guard that cannot distinguish those two is not a guard.
        """
        def skill_paths(agent):
            h = json.loads((REPO / f"agents/{agent}/harness.json").read_text())
            out = set()
            for s in h.get("skills") or []:
                if "git" in s:
                    out.add(s["git"].get("path", ""))
                elif isinstance(s.get("s3"), dict):
                    rest = s["s3"].get("uri", "").split("://", 1)[-1]
                    out.add(rest.split("/", 1)[1].rstrip("/") if "/" in rest else "")
            return out

        want = "skills/llmops/llm-data-preparation"
        assert want in skill_paths("orchestrator"), (
            "the orchestrator asks the data-discovery questions with no data-prep skill "
            "behind it")
        assert want in skill_paths("data-prep"), (
            f"{want} was MOVED off data-prep rather than also mounted -- the worker that "
            "actually prepares the data lost its guidance")

    def test_every_mounted_skill_is_named_in_the_prompt_that_must_consult_it(self):
        """A mount makes a skill READABLE; only the prompt makes the agent read it.

        The orchestrator's prompt said "Your mounted skills (llm-agent-orchestration,
        ml-solution-design) are your methodology — consult them before acting" while FOUR
        were mounted. `llm-cost-optimization` and `llm-data-preparation` were mounted by
        later work that never revisited the sentence, and the omitted one is the skill for
        step 0 DATA DISCOVERY -- the protocol's own opening move. The test above asserts
        the MOUNT, which was intact, so nothing failed.

        Verified live against the harness rather than reasoned about: asked which skills it
        had, the deployed agent listed all four -- because the RUNTIME injects a skills
        manifest into the system prompt that `GetHarness` does not return (a deterministic
        1148 extra input tokens; see the pass-through gotcha in AGENTS.md). So the agent can
        SEE all four and is still told, in prose, that two of them are its methodology. Both
        statements are in front of the model at once and the prose is the one that says
        "consult them before acting".

        Derived from the mount list in both directions, per the standing rule that a guard
        carrying its own copy of a checklist cannot detect drift: a skill added to `skills`
        without being named fails here, and a name left behind after a mount is removed
        fails too. Every harness is checked, not just the one that drifted.
        """
        for cfg in sorted((REPO / "agents").glob("*/harness.json")):
            h = json.loads(cfg.read_text())
            mounted = set()
            for s in h.get("skills") or []:
                for kind in ("s3", "git", "path"):
                    if isinstance(s.get(kind), dict):
                        loc = (s[kind].get("uri") or s[kind].get("path") or "")
                        mounted.add(loc.rstrip("/").rsplit("/", 1)[-1])
            prompt = " ".join(b.get("text", "") for b in h.get("systemPrompt") or [])
            # Only skill-shaped tokens, so ordinary prose cannot accidentally satisfy this.
            named = set(re.findall(r"\b(?:llm|ml|mlops)-[a-z0-9-]+\b", prompt))
            agent = h.get("harnessName", cfg.parent.name)
            assert not (mounted - named), (
                f"{agent} mounts {sorted(mounted - named)} but its prompt never names "
                "them -- a mounted skill the prompt does not name is a skill the agent "
                "is not told to consult")
            assert not (named - mounted), (
                f"{agent}'s prompt names {sorted(named - mounted)} but nothing is "
                "mounted -- the agent is told to consult a skill it cannot read")

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

    def test_the_signed_plan_outranks_the_boilerplate_model_defaults(self):
        """Model consent is model-specific: approving a Fable-5 teacher is not approving
        a DeepSeek-R1 one. So the plan a human SIGNED has to beat DEFAULT_MODELS.

        Red before the fix: `models` was `{**DEFAULT_MODELS, **params.models}` with the
        plan never consulted, so run 68cfa9c8's manifest carried
        models.teacher=us.deepseek.r1-v1:0 while its signed plan said
        global.anthropic.claude-fable-5 — two contradictory teacher ids in one manifest.
        The data-prep agent resolved it by judgment, writing "top-level manifest 'models'
        field is stale boilerplate" into the driver it generated. It chose correctly;
        that it had to choose at all is the defect."""
        plan = {"models": {"teacher": "global.anthropic.claude-fable-5"}}
        m = start_pipeline.seed_manifest("run-x", "conductor", {}, plan)
        assert m["models"]["teacher"] == "global.anthropic.claude-fable-5", (
            "boilerplate DEFAULT_MODELS overwrote the teacher a human signed for")
        # roles the plan is silent about still fall back to the defaults
        assert m["models"]["student"] == start_pipeline.DEFAULT_MODELS["student"]

    def test_a_plan_silent_on_models_still_gets_the_defaults(self):
        """Most runs (scheduler, webhook) have no plan at all. The authority rule must
        not turn "the plan didn't say" into "no teacher configured"."""
        for plan in ({}, None, {"models": {}}):
            m = start_pipeline.seed_manifest("run-x", "scheduler", {}, plan)
            assert m["models"] == start_pipeline.DEFAULT_MODELS

    def test_params_may_fill_gaps_the_plan_leaves_but_not_contradict_it(self):
        """`params` is authored by the dispatching agent, so letting it win over the
        plan would reopen the same bypass from the other side. Filling a role the plan
        is silent about is fine; overriding a role the plan named is not."""
        plan = {"models": {"teacher": "global.anthropic.claude-fable-5"}}
        m = start_pipeline.seed_manifest(
            "run-x", "conductor", {"models": {"student": "Qwen/Qwen3-4B"}}, plan)
        assert m["models"]["teacher"] == "global.anthropic.claude-fable-5"
        assert m["models"]["student"] == "Qwen/Qwen3-4B"

    def test_a_dispatch_contradicting_the_signed_plan_is_refused(self):
        """A disagreement here is never routine: the dispatch path and the approval path
        disagree about what was bought. Failing costs one visible error; guessing costs
        an unapproved spend that looks authorized in every artifact afterward."""
        plan = {"models": {"teacher": "global.anthropic.claude-fable-5"}}
        with pytest.raises(ValueError) as e:
            start_pipeline.seed_manifest(
                "run-x", "conductor", {"models": {"teacher": "us.deepseek.r1-v1:0"}}, plan)
        msg = str(e.value)
        assert "teacher" in msg and "fable-5" in msg and "deepseek" in msg, (
            "the refusal must name the role AND both models — an error that says only "
            "'models conflict' sends the operator back to diffing two JSON blobs")

    def test_identical_models_in_plan_and_params_are_not_a_conflict(self):
        """Belt-and-braces dispatches that echo the plan's own models are the common
        case, not an error: the check is on DISAGREEMENT, not on presence."""
        models = {"teacher": "global.anthropic.claude-fable-5"}
        m = start_pipeline.seed_manifest("run-x", "conductor", {"models": dict(models)},
                                        {"models": dict(models)})
        assert m["models"]["teacher"] == "global.anthropic.claude-fable-5"

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
    # Added with the sweep itself. A scheduled Lambda is the worst place for this defect:
    # nobody watches an 08:00 UTC invocation, so an AccessDenied on its PutItem would make
    # the sweep look like a sweep that ran and found nothing.
    ("monitor_sweep", "orchestration/monitor_sweep/handler.py"),
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
    # BOTH kinds. Counting only `git` would make this guard evaporate at exactly the
    # moment it matters: after the migration every source is `s3`, so a git-only sync
    # plan covers 0 mounts, a git-only expectation is also 0, and 0 == 0 passes while the
    # mirror has stopped syncing the only copy any agent ever reads.
    from_cfg = sum(1 for c in configs
                   for s in (json.loads(c.read_text()).get("skills") or [])
                   if "git" in s or "s3" in s)
    assert total == from_cfg, (
        f"the sync plan covers {total} mounts but the configs declare {from_cfg} git+s3 "
        "sources; a mount the sync does not know about is a skill that goes stale in S3 "
        "while every config still reads healthy")
    assert mounts, "no mounts found at all -- the glob or the config shape changed"


def test_the_sync_still_covers_a_skill_once_its_source_is_s3(storage_mod, tmp_path):
    """After the migration S3 is the ONLY copy an agent reads, so that is precisely when
    the mirror must keep syncing it.

    The first version of `mounted_skills` collected `git` only, reasoning that an entry
    already on s3 needs no upload. That inverts at the switch: a git-only plan silently
    covers nothing, and the coverage guard above compares 0 to 0 and passes, so the next
    skill edit would never reach any agent. The path is recovered from the URI because the
    s3 source shape is a single `uri` with no `path` field.
    """
    agents = tmp_path / "agents"
    (agents / "monitor").mkdir(parents=True)
    (agents / "monitor" / "harness.json").write_text(json.dumps({"skills": [
        {"s3": {"uri": "s3://llmops-data/skills/llmops/llm-observability"}},
        {"s3": {"uri": "s3://<DATA_BUCKET>/skills/llmops/llm-cost-optimization"}},
        {"git": {"url": "u", "path": "skills/llmops/llm-agent-orchestration"}},
    ]}))
    mounts = storage_mod.mounted_skills(tmp_path)
    assert set(mounts) == {"skills/llmops/llm-observability",
                           "skills/llmops/llm-cost-optimization",
                           "skills/llmops/llm-agent-orchestration"}, (
        f"an s3-sourced mount was dropped from the mirror plan: {mounts}")
    assert mounts["skills/llmops/llm-cost-optimization"] == ["monitor"], (
        "an UNRESOLVED <DATA_BUCKET> must yield the same repo-relative path as a resolved "
        "bucket, so the plan is identical before and after substitution")


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


def _harness_role_statements():
    doc = json.loads((REPO / "deploy/iam/harness_execution_role.json").read_text())
    return doc["permissionsPolicy"]["Statement"]


def _prefix_is_granted(action, prefix):
    """Does any statement allow `action` on <bucket>/<prefix>...? Returns the Sids."""
    hits = []
    for st in _harness_role_statements():
        if st.get("Effect") != "Allow":
            continue
        actions = st["Action"]
        if action not in ([actions] if isinstance(actions, str) else actions):
            continue
        res = st.get("Resource")
        for r in [res] if isinstance(res, str) else (res or []):
            _, _, tail = r.partition("<DATA_BUCKET>/")
            if tail and prefix.startswith(tail.rstrip("*")):
                hits.append(st.get("Sid"))
    return hits


def test_the_role_that_fetches_a_skill_at_session_start_can_read_the_mirror(storage_mod):
    """The read-back inside ensure_skills proves the object is fetchable BY THE DEPLOYER.

    That is not the claim that matters. An s3 skill source is fetched at session start by
    the harness execution role, and this was measured live: after the mirror uploaded 66
    files and head_object confirmed all 11 SKILL.md keys under my admin credentials,
    `simulate_principal_policy` for llmops-harness-execution on
    skills/llmops/llm-agent-orchestration/SKILL.md returned **implicitDeny**. The role file
    granted `skills-mirror/*` -- a prefix that has never held a single object -- and not
    the `skills/*` prefix the sync actually writes. Switching a source then would have been
    accepted by UpdateHarness, reported READY, and failed every session afterwards.

    Derived from ensure_skills' own key layout rather than a literal, so renaming the
    prefix fails here instead of at session start.
    """
    key = f"{sorted(storage_mod.mounted_skills(REPO))[0]}/SKILL.md"
    assert _prefix_is_granted("s3:GetObject", key), (
        f"no statement lets the harness role GET {key}; an s3 skill source is fetched "
        "under that role at session start, not under the deployer's credentials")


def test_the_skill_mirror_is_listable_because_a_source_fetches_a_tree_not_one_object():
    """ListBucket here is condition-scoped, and this repo has already been bitten by a
    prefix granted for GetObject but absent from that condition: readable object-by-object,
    never enumerable, which surfaced as 'the rate card history is there but the agent
    reports no card exists'. A skill source resolves a whole directory, so the same gap
    would strand it."""
    for st in _harness_role_statements():
        if st.get("Sid") == "S3PipelineList":
            prefixes = st["Condition"]["StringLike"]["s3:prefix"]
            assert "skills/*" in prefixes, (
                f"skills/* is missing from the ListBucket condition {prefixes}; the "
                "objects would be individually readable and the tree still unlistable")
            return
    raise AssertionError("no S3PipelineList statement")


def test_the_agents_cannot_write_the_skill_tree_they_are_judged_against():
    """Read-only on purpose, and NOT folded into S3PipelineObjects, which carries
    PutObject alongside GetObject. An agent that can rewrite its own skill tree can
    rewrite the instructions it is evaluated against on the next session -- the same
    reasoning that keeps customer-data/ read-only for the pipeline."""
    assert not _prefix_is_granted("s3:PutObject", "skills/llmops/x/SKILL.md"), (
        "the harness role can WRITE the skill mirror; the skills grant must be "
        "GetObject-only and separate from the read/write pipeline prefixes")
    assert not _prefix_is_granted("s3:DeleteObject", "skills/llmops/x/SKILL.md")


# ── declared vs dispatched: the third recurrence gets a guard ──────────────────
# page_human (#54), eval `evaluate` (#57) and the entire monitor harness (#58) were all
# the same defect: a capability the prompt, the docs and the IAM described, that no code
# path could reach. Three times is a pattern, and a pattern needs a check rather than a
# fourth fix. pipeline/contracts/tasks.py declares the rule; these tests enforce it.

from pipeline.contracts.tasks import (NON_ASL_DISPATCH_SITES,          # noqa: E402
                                      TASKS_WITHOUT_A_DISPATCH_SITE,
                                      declared_tasks, prompt_text)


def _asl_dispatched():
    """(stage, task) for every state machine state that invokes a harness."""
    asl = json.loads((REPO / "orchestration/state_machine.asl.json").read_text())
    out = {}
    for name, st in asl["States"].items():
        payload = (st.get("Parameters") or {}).get("Payload") or {}
        if payload.get("stage") and payload.get("task"):
            out[(payload["stage"], payload["task"])] = name
    return out


def _declared_everywhere():
    """(harness_dir, task) for every task any agent prompt declares."""
    out = {}
    for cfg_path in sorted((REPO / "agents").glob("*/harness.json")):
        cfg = json.loads(cfg_path.read_text())
        for task in declared_tasks(prompt_text(cfg)):
            out[(cfg_path.parent.name, task)] = cfg_path
    return out


def test_every_task_a_prompt_declares_can_actually_be_dispatched():
    """The guard the last three fixes each needed and none of them left behind.

    A task clause in a system prompt is a promise: the agent is told what `params.task`
    values mean and is judged on handling them. When nothing dispatches one, the promise
    is unfalsifiable from inside the platform -- no error, no metric, no log line, because
    "never dispatched" and "dispatched and did nothing" look identical from outside. That
    is exactly how the monitor harness went the platform's whole life with three declared
    tasks and zero dispatch sites, while ARCHITECTURE.md described it as a stage.

    So each declared task must be dispatched by the state machine, or by a site named in
    NON_ASL_DISPATCH_SITES, or listed in TASKS_WITHOUT_A_DISPATCH_SITE with a reason. The
    allowlist is not a way around the test: writing the reason down is the work, because
    "by design" and "we forgot" are indistinguishable until somebody says which it is.
    """
    dispatched = _asl_dispatched()
    unaccounted = []
    for (agent, task) in sorted(_declared_everywhere()):
        if (agent, task) in dispatched:
            continue
        if (agent, task) in NON_ASL_DISPATCH_SITES:
            continue
        if (agent, task) in TASKS_WITHOUT_A_DISPATCH_SITE:
            continue
        unaccounted.append(f"{agent}:{task}")
    assert not unaccounted, (
        f"these tasks are declared in a prompt and nothing can ever dispatch them: "
        f"{unaccounted}. Wire a dispatch site, or record WHY not in "
        "pipeline/contracts/tasks.py -- an undispatchable task is invisible in production.")


def test_the_dispatch_allowlists_do_not_outlive_the_tasks_they_excuse():
    """An allowlist that keeps entries for tasks nobody declares any more is worse than
    no allowlist: it reads as deliberate coverage of ground the prompt has abandoned, and
    the next person adding a task with a colliding name inherits somebody else's excuse.
    """
    declared = set(_declared_everywhere())
    stale = sorted(k for k in
                   list(NON_ASL_DISPATCH_SITES) + list(TASKS_WITHOUT_A_DISPATCH_SITE)
                   if k not in declared)
    assert not stale, (
        f"pipeline/contracts/tasks.py accounts for tasks no prompt declares: {stale}; "
        "delete the entries or restore the clauses")


def test_each_named_dispatch_site_still_dispatches_what_it_claims():
    """A pointer to a file is only worth as much as the file's contents.

    NON_ASL_DISPATCH_SITES is bookkeeping, and bookkeeping decays: the file gets renamed,
    the task gets dropped from a TASKS tuple, the harness id changes -- and the entry keeps
    asserting a dispatch that no longer happens, which is the precise failure this whole
    module exists to catch, reintroduced by the fix for it. So check the file exists and
    that it names both the harness and the task.
    """
    for (agent, task), rel in sorted(NON_ASL_DISPATCH_SITES.items()):
        path = REPO / rel
        assert path.exists(), f"{agent}:{task} points at {rel}, which does not exist"
        text = path.read_text()
        assert task in text, f"{rel} is named as the dispatch site for {task!r} but never mentions it"
        harness_id = f"llmops_{agent.replace('-', '_')}"
        assert harness_id in text, (
            f"{rel} dispatches {task!r} but never names {harness_id}; it cannot be "
            "reaching that harness")


def test_the_state_machine_only_dispatches_tasks_the_prompts_declare():
    """The reverse direction, which fails a different way: a state dispatching a task no
    prompt declares reaches the agent, and the agent -- told to handle a closed set of
    values -- improvises. That is worse than a state that fails, because it produces
    plausible artifacts nobody asked for and a stage_complete that looks like success.
    """
    declared = set(_declared_everywhere())
    for (stage, task), state in sorted(_asl_dispatched().items()):
        assert (stage, task) in declared, (
            f"{state} dispatches {stage}:{task}, which no agents/{stage}/harness.json "
            "prompt declares; the agent would improvise a task it was never given")


# ── prompt-named AWS APIs vs the role that must make the call ──────────────────
# test_finops.py already checks S3 PREFIXES named in prompts against the role. That is
# only half the surface: the monitor prompt named `aws cloudwatch get-metric-statistics`
# and `aws sagemaker list-tags`, and the harness role granted NEITHER -- an implicitDeny
# confirmed against the live role with simulate_principal_policy. Nobody noticed for the
# same reason as the missing dispatch: no monitor task had ever run. So check ACTIONS too.

#: `aws <service> <sub-command>` as written in a prompt -> the IAM action it performs.
#: Only the commands the prompts actually use; a CLI-wide table would be a second source
#: of truth for the AWS API surface and would rot faster than the prompts do.
_CLI_TO_IAM = {
    ("sts", "get-caller-identity"): None,  # implicitly allowed for any principal
    ("sagemaker", "list-training-jobs"): "sagemaker:ListTrainingJobs",
    ("sagemaker", "list-endpoints"): "sagemaker:ListEndpoints",
    ("sagemaker", "list-tags"): "sagemaker:ListTags",
    ("sagemaker", "describe-endpoint"): "sagemaker:DescribeEndpoint",
    ("sagemaker", "describe-endpoint-config"): "sagemaker:DescribeEndpointConfig",
    ("sagemaker", "delete-endpoint"): "sagemaker:DeleteEndpoint",
    ("sagemaker", "create-training-job"): "sagemaker:CreateTrainingJob",
    ("sagemaker", "describe-training-job"): "sagemaker:DescribeTrainingJob",
    ("sagemaker", "create-model"): "sagemaker:CreateModel",
    ("sagemaker", "create-model-package"): "sagemaker:CreateModelPackage",
    ("sagemaker-runtime", "invoke-endpoint"): "sagemaker:InvokeEndpoint",
    ("bedrock-runtime", "converse"): "bedrock:InvokeModel",
    ("cloudwatch", "get-metric-statistics"): "cloudwatch:GetMetricStatistics",
    ("cloudwatch", "get-metric-data"): "cloudwatch:GetMetricData",
    ("ce", "get-cost-and-usage"): "ce:GetCostAndUsage",
    ("ce", "get-cost-and-usage-with-resources"): "ce:GetCostAndUsageWithResources",
    ("pricing", "get-products"): "pricing:GetProducts",
}


def _harness_allowed_actions():
    allowed = set()
    for st in _harness_role_statements():
        if st.get("Effect") != "Allow":
            continue
        acts = st["Action"]
        allowed.update([acts] if isinstance(acts, str) else acts)
    return allowed


def test_every_aws_api_a_prompt_tells_an_agent_to_call_is_in_the_harness_role():
    """The action-level twin of test_every_s3_prefix_any_agent_prompt_uses_is_one_the_role
    _can_reach, added because the prefix guard could not see this class of gap at all.

    A prompt naming an API is the strongest possible statement that the agent will call it
    -- stronger than a code path, because the agent has no fallback and no branch: it runs
    the command it was told to run, takes AccessDeniedException in a shell, and then has to
    decide what to do about it mid-task. Live, the monitor prompt's very first instruction
    was `aws cloudwatch get-metric-statistics` against a role with no cloudwatch read
    action at all.

    Driven off the prompts, so an API added to any prompt fails here rather than in the
    middle of a paid run.
    """
    allowed = _harness_allowed_actions()

    def granted(action):
        return action in allowed or any(
            p.endswith("*") and action.startswith(p[:-1]) for p in allowed)

    missing = {}
    for cfg_path in sorted((REPO / "agents").glob("*/harness.json")):
        text = cfg_path.read_text()
        for service, sub in set(re.findall(r"aws ([a-z0-9-]+) ([a-z0-9-]+)", text)):
            if (service, sub) not in _CLI_TO_IAM:
                raise AssertionError(
                    f"{cfg_path.parent.name} prompt runs `aws {service} {sub}` and this "
                    "test has no IAM mapping for it -- add it to _CLI_TO_IAM so the grant "
                    "is checked; an unmapped command is an unchecked permission")
            action = _CLI_TO_IAM[(service, sub)]
            if action and not granted(action):
                missing[f"{cfg_path.parent.name}: aws {service} {sub}"] = action
    assert not missing, (
        f"prompts tell agents to call APIs the harness role denies: {missing}. The agent "
        "takes AccessDenied in a shell, mid-task, with no fallback.")


def test_the_sweep_can_read_tags_of_endpoints_nobody_claimed():
    """ListEndpoints/ListTags stay on Resource "*" deliberately, and that is not laziness.

    The single genuine orphan in this account is jumpstart-dft-hf-asr-whisper-large-v2:
    InService since 2024-04-11, carrying no `project` tag at all. A sweep whose ListTags
    were scoped to endpoint/llmops-* could never see the one endpoint it exists to catch --
    an untagged endpoint is unattributable, not foreign, and the unattributable ones are
    exactly what nobody is watching. Enumeration and metadata are account-wide; every
    MUTATION stays scoped, so the sweep can SEE everything and TOUCH only ours. The reads
    that make an orphan costable live in the sibling test below.
    """
    stmts = {st.get("Sid"): st for st in _harness_role_statements()}
    lst = stmts["SageMakerList"]
    assert set(lst["Action"]) >= {"sagemaker:ListEndpoints", "sagemaker:ListTags"}
    assert lst["Resource"] == "*" or lst["Resource"] == ["*"], (
        "scoping the sweep's enumeration to llmops-* hides untagged orphans, which is "
        "the only kind of orphan there is")
    delete = stmts["SageMakerLifecycleScoped"]
    assert "sagemaker:DeleteEndpoint" in delete["Action"]
    assert "*" not in ([delete["Resource"]] if isinstance(delete["Resource"], str)
                       else delete["Resource"]), \
        "DeleteEndpoint must stay scoped: the sweep reports, it does not get to delete "\
        "anything in the account"


def test_the_sweep_can_characterise_an_orphan_it_may_not_touch():
    """Read account-wide, mutate llmops-* only -- and the split runs THROUGH Describe.

    The first live sweep is the evidence. It found the orphan, then filed its own permission
    gap: DescribeEndpoint was scoped to endpoint/llmops-*, so it could not read the instance
    type of the one endpoint it flagged, and its headline cost -- ~$1106/month, ~$30.6k since
    2024-04-11 -- went out as a guess at the JumpStart default. A finding whose number is an
    assumption is a finding an owner can correctly dismiss.

    So Describe is account-wide and read-only while every mutation stays scoped. That pairing
    is the invariant worth pinning, because the tempting fix is to widen
    SageMakerLifecycleScoped instead -- which would hand DeleteEndpoint over the whole
    account to an agent whose prompt forbids deleting anything.

    The grant is also only half the fix, which is why the prompt is asserted here too. The
    live sweep did not fail on AccessDenied for a call it was told to make -- it was never
    told to make the call. A permission nothing instructs the agent to use buys the same
    guessed cost figure it bought before, and reads as fixed.
    """
    cfg = json.loads((REPO / "agents/monitor/harness.json").read_text())
    sweep_clause = [ln for ln in prompt_text(cfg).splitlines() if ln.startswith('- "sweep"')]
    assert len(sweep_clause) == 1, "the sweep clause moved; re-anchor this guard"
    for cmd in ("aws sagemaker describe-endpoint",
                "aws sagemaker describe-endpoint-config"):
        assert cmd in sweep_clause[0], (
            f"the sweep clause never tells the agent to run '{cmd}', so granting it in IAM "
            "changes nothing: the flagged endpoint still gets priced off a guessed instance "
            "type. The live sweep did not take an AccessDenied here -- it never tried.")

    stmts = {st.get("Sid"): st for st in _harness_role_statements()}
    read = stmts["SageMakerDescribeReadOnly"]
    assert read["Resource"] in ("*", ["*"]), (
        "a sweep that can only describe endpoints already named llmops-* cannot cost out "
        "the untagged ones, which are the only orphans there are")
    assert set(read["Action"]) >= {"sagemaker:DescribeEndpoint",
                                   "sagemaker:DescribeEndpointConfig"}, (
        "DescribeEndpoint alone gives the config NAME, not the instance type behind it; "
        "the cost figure needs both calls")
    mutations = {"sagemaker:CreateEndpoint", "sagemaker:UpdateEndpoint",
                 "sagemaker:DeleteEndpoint", "sagemaker:CreateModel", "sagemaker:AddTags",
                 "sagemaker:CreateEndpointConfig", "sagemaker:CreateTrainingJob",
                 "sagemaker:StopTrainingJob"}
    assert not (set(read["Action"]) & mutations), (
        f"{sorted(set(read['Action']) & mutations)} is a mutation on Resource '*'; this "
        "statement is the account-wide one and must stay read-only")
    for sid, st in stmts.items():
        acts = st["Action"] if isinstance(st["Action"], list) else [st["Action"]]
        if not (set(acts) & mutations):
            continue
        res = st["Resource"] if isinstance(st["Resource"], list) else [st["Resource"]]
        assert "*" not in res, (
            f"{sid} mutates SageMaker on Resource '*'. Widening the LIFECYCLE statement is "
            "the wrong fix for a read gap: it grants DeleteEndpoint account-wide to an "
            "agent whose prompt says 'do NOT delete endpoints yourself -- report them'")


# ── the DriftDetected emitter that never existed ───────────────────────────────
# DRIFT_DETECTED was declared in pipeline/contracts/events.py from Phase 1 and emitted by
# NOTHING, while the monitor prompt tells the agent to put its finding in
# metrics.drift_detected "so the orchestrator can emit the event" -- naming an emitter that
# did not exist. Unobservable at the same time as the missing dispatch, and for the same
# reason: no monitor task had ever run.

def _monitor_health_run(metrics):
    """Run a monitor:health stage_complete through the driver; return the clients."""
    uri = "s3://llmops-data-test/runs/run-test-1/monitoring/health.json"
    ac = FakeAgentCore([
        tool_use_stream("stage_complete",
                        {"stage": "monitor", "task": "health",
                         "outputs": [uri], "metrics": metrics}),
        text_stream("ack")])
    c = clients(ac, FakeS3(existing=[uri]))
    c["s3"].objects["s3://llmops-data-test/runs/run-test-1/manifest.json"] = json.dumps(
        {"run_id": "run-test-1", "stages": {}})
    driver.handler(driver_event(stage="monitor", task="health",
                               harness_id="llmops_monitor"), clients=c)
    return c


def test_a_health_task_that_finds_drift_emits_the_event_nothing_used_to_emit():
    c = _monitor_health_run({"drift_detected": True, "p90_ms": 812})
    assert any(e["DetailType"] == ev.DRIFT_DETECTED for e in c["events"].entries), (
        "the prompt promises the orchestrator emits DriftDetected from this metric; for "
        "the platform's whole life nothing did")


@pytest.mark.parametrize("metrics", [
    {"drift_detected": False},
    {"drift_detected": None},
    {},                              # the agent did not answer the question
    {"drift_detected": "unknown"},   # ...or answered it in prose
    {"drift_detected": "false"},     # a non-empty string, which bool() calls True
    {"drift_detected": 1},           # truthy, but not the boolean the contract asks for
])
def test_only_a_literal_true_announces_drift(metrics):
    """Strict `is True`, deliberately matching the eval gate rather than bool().

    The failure mode is asymmetric, so the test is too. A DriftDetected event is an
    accusation about a deployed model: whatever subscribes to it will roll back, retrain or
    page somebody. "unknown" and "false" are both truthy strings, and an agent that could
    not measure drift is far likelier to say one of those than to omit the key -- so bool()
    would have the platform announcing drift nobody observed, sourced from an agent that
    said it did not know. Under-reporting here loses a signal; over-reporting spends money
    and burns trust in the signal itself.
    """
    c = _monitor_health_run(dict(metrics, p90_ms=100))
    assert not [e for e in c["events"].entries if e["DetailType"] == ev.DRIFT_DETECTED], (
        f"metrics={metrics!r} announced drift; only a literal True may")


@pytest.mark.parametrize("echoed, dispatched", [
    ({}, "sweep"),                                   # the field simply omitted -- both live sweeps
    ({"task": ""}, "sweep"),                         # ...or present and empty
    ({"task": "report", "stage": "finops"}, "sweep"),  # ...or confidently wrong
])
def test_the_event_row_records_the_task_that_was_dispatched_not_the_one_echoed(echoed, dispatched):
    """Which task ran is the driver's fact, not the agent's.

    Everything else in a stage_complete payload is the agent's to report -- nobody else
    knows the outputs or the metrics. stage and task are the opposite: the driver was
    handed both in its own invocation event, so the agent's copy adds nothing and can
    subtract. Both live monitor sweeps (2026-08-01 19:59Z and 20:13Z) filed
    ``"task": ""``, so the row said a monitor stage completed without saying which of
    health/sweep/report it was -- the ambiguity #58 exists to remove, reintroduced one
    layer down.

    The console reads this field to decide which (stage, task) pairs a run executed
    (``_session_ids``), and an empty task there matches ANY task of the stage, so a sweep
    could lend its evidence to a health check that never ran. The wrong-echo case is the
    reason the fix overwrites rather than fills-if-blank.
    """
    uri = "s3://llmops-data-test/monitoring/sweeps/sweep-2026-08-01.json"
    ac = FakeAgentCore([
        tool_use_stream("stage_complete",
                        {"outputs": [uri], "metrics": {"endpoints_total": 1}, **echoed}),
        text_stream("ack")])
    c = clients(ac, FakeS3(existing=[uri]))
    driver.handler(driver_event(stage="monitor", task=dispatched,
                               harness_id="llmops_monitor"), clients=c)
    rows = [i for i in c["ddb"].tables["llmops-stage-events"].items
            if "stage_complete" in str(i.get("sk", ""))]
    assert rows, "no stage_complete row was written at all"
    detail = json.loads(rows[-1]["detail"])
    assert detail["task"] == dispatched, (
        f"the row records task={detail['task']!r}; the driver dispatched {dispatched!r}. "
        "The agent's echo is a restatement at best -- the dispatch is the fact.")
    assert detail["stage"] == "monitor", (
        f"the row records stage={detail['stage']!r} for a monitor dispatch")


def test_health_never_reports_a_gate_because_observation_is_not_a_verdict():
    """A health task settles its token with gate_passed True by the non-gate default, and
    that is correct: MonitorHealth has no Choice after it, and a metric read must not be
    able to decide a run's fate. If health could fail the run, a CloudWatch hiccup would
    strand the endpoint it was watching -- the exact cost risk it exists to reduce."""
    c = _monitor_health_run({"drift_detected": True, "error_rate": 0.9})
    payload = json.loads(c["sfn"].successes[0]["output"])
    assert payload["gate_passed"] is True, (
        "health reported a gate verdict; drift is a finding for a human and the "
        "orchestrator, never a pipeline decision made inside the observation step")


# ── the scheduled sweep Lambda ─────────────────────────────────────────────────

sweep = _load("monitor_sweep", "orchestration/monitor_sweep/handler.py")

SWEEP_ENV = {"DATA_BUCKET": "llmops-data-test", "DRIVER_FN": "llmops-harness-driver",
             "EVENTS_TABLE": "llmops-stage-events", "PROJECT": "llmops-agentic-system",
             "AWS_REGION": "us-east-1"}


class _SweepLambda:
    def __init__(self, payload=None, raises=None):
        self.calls = []
        self._payload = payload
        self._raises = raises

    def invoke(self, **kw):
        self.calls.append(kw)
        if self._raises:
            raise self._raises
        out = {"StatusCode": 202 if kw["InvocationType"] == "Event" else 200}
        if self._payload is not None:
            out["Payload"] = io.BytesIO(json.dumps(self._payload).encode())
        return out


def _sweep_clients(lam=None, ddb=None):
    return {"lambda": lam or _SweepLambda(), "ddb": ddb or FakeDDB(), "sns": FakeEvents()}


@pytest.fixture
def sweep_env(monkeypatch):
    for k, v in SWEEP_ENV.items():
        monkeypatch.setenv(k, v)


def test_the_sweep_id_is_derived_from_the_date_so_re_running_a_day_is_idempotent():
    """A sweep has no run, but the driver keys its session id and every stage-event row
    off run_id. Date-derived rather than random so a re-run of the same day lands in the
    same session and the same rows: re-running a sweep is normal operations, and two
    sweeps of one day must not read as two different sets of findings."""
    assert sweep.sweep_id(datetime.date(2026, 8, 2)) == "sweep-2026-08-02"
    assert sweep.sweep_id(datetime.date(2026, 8, 2)) == sweep.sweep_id(datetime.date(2026, 8, 2))
    assert sweep.sweep_id(datetime.date(2026, 8, 3)) != sweep.sweep_id(datetime.date(2026, 8, 2))


def test_the_sweep_payload_carries_the_idle_threshold_instead_of_restating_it():
    """idle_hours travels in the payload rather than being described twice in prose. The
    prompt says "flag any idle >2 hours"; if the schedule believed something else, the two
    would disagree and nothing would say so -- the agent would apply the prompt's number
    and the operator would read the schedule's."""
    p = sweep.build_payload("proj", "buck", "us-east-1", "sweep-2026-08-02", idle_hours=6)
    assert p["params"]["idle_hours"] == 6
    assert (p["stage"], p["task"], p["harness_id"]) == ("monitor", "sweep", "llmops_monitor")
    assert sweep.DEFAULT_IDLE_HOURS == 2, "the default must match the prompt's threshold"


def test_the_sweep_writes_outside_any_runs_prefix():
    """A sweep's findings are about endpoints that OUTLIVED their runs, so filing them
    under runs/<run_id>/ would bury the account-level answer inside whichever run happened
    to look -- and the sweep has no run to file under in the first place."""
    p = sweep.build_payload("proj", "buck", "us-east-1", "sweep-2026-08-02")
    for uri in (p["manifest_uri"], p["params"]["sweep_uri"]):
        assert uri.startswith("s3://buck/monitoring/"), uri
        assert "/runs/" not in uri


def test_the_sweep_lambda_refuses_the_run_scoped_monitor_tasks(sweep_env):
    """health and report are run-scoped and live in the state machine. Dispatching either
    from here would invent a run_id for a run that does not exist, and then write into
    another run's prefix under it."""
    for task in ("health", "report", "reconcile", ""):
        c = _sweep_clients()
        out = sweep.handler({"task": task}, clients=c)
        assert "error" in out, f"task={task!r} was accepted"
        assert not c["lambda"].calls, f"task={task!r} reached the driver"


def test_the_scheduler_invocation_is_async_and_a_sync_one_reads_the_driver_back(sweep_env):
    """The schedule fires and forgets: a sweep's own work takes minutes in the harness, and
    a 60s Lambda waiting on it would time out having done everything right. An operator
    calling it by hand with sync=True wants the verdict, so that path reads the payload."""
    c = _sweep_clients()
    sweep.handler({"task": "sweep"}, clients=c)
    assert c["lambda"].calls[0]["InvocationType"] == "Event"

    c = _sweep_clients(_SweepLambda(payload={"status": "completed"}))
    out = sweep.handler({"task": "sweep", "sync": True}, clients=c)
    assert c["lambda"].calls[0]["InvocationType"] == "RequestResponse"
    assert out["result"]["status"] == "completed"


def test_every_invocation_leaves_a_row_so_a_MISSED_sweep_is_visible(sweep_env):
    """The same argument as finops's reserved #audit# key. The failure mode worth
    engineering against is not a sweep that reports badly -- it is a sweep that silently
    stopped happening, at 08:00 UTC, where nobody is looking. A cost control nobody can
    tell has stopped is not a control."""
    c = _sweep_clients()
    sweep.handler({"task": "sweep"}, clients=c)
    rows = [i for t in c["ddb"].tables.values() for i in t.items]
    assert len(rows) == 1, f"expected exactly one sweep row, got {rows}"
    assert rows[0]["sk"].startswith("sweep#") and rows[0]["stage"] == "monitor"
    assert rows[0]["run_id"].startswith("sweep-")


def test_a_sweep_row_never_lands_in_the_runs_table(sweep_env):
    """EVENTS_TABLE, not RUNS_TABLE. A synthetic sweep-<date> row in the runs table would
    be listed by the console as a run, reconciled for cost by the auditor, and counted in
    the run totals every doc quotes -- one phantom run per day, forever."""
    c = _sweep_clients()
    sweep.handler({"task": "sweep"}, clients=c)
    assert list(c["ddb"].tables) == ["llmops-stage-events"], (
        f"the sweep wrote to {list(c['ddb'].tables)}; a sweep is not a run")


def test_a_bookkeeping_failure_does_not_lose_the_sweep(sweep_env):
    """The row exists to make a missed sweep visible; it must not be able to CAUSE one.
    If PutItem fails after the driver was already invoked, the sweep is running -- raising
    here would make the scheduler retry it and start a second one."""
    class _Broken:
        def Table(self, name):
            raise Exception("ProvisionedThroughputExceeded")

    c = _sweep_clients(ddb=_Broken())
    out = sweep.handler({"task": "sweep"}, clients=c)
    assert c["lambda"].calls, "the driver was never invoked"
    assert out["result"]["status"] == "invoked"


def test_the_sweep_schedule_cannot_drift_into_the_wrong_day(sweep_env):
    """FlexibleTimeWindow OFF, deliberately, same as the finops reconcile. sweep_id() reads
    the CURRENT date, so a job allowed to drift past midnight UTC would file its findings
    under a day it did not sweep -- and the row for the day it was scheduled for would be
    missing, which is exactly the "missed sweep" signal above, fired falsely."""
    triggers = _deploy_src("08_triggers.py")
    assert 'SWEEP_SCHEDULE_NAME = "llmops-monitor-sweep-daily"' in triggers
    body = triggers.split("def ensure_sweep_schedule")[1].split("\ndef ")[0]
    assert '"Mode": "OFF"' in body, "a flexible window files findings under the wrong date"
    assert 'cron(0 8 * * ? *)' in body
    assert 'json.dumps({"task": "sweep"})' in body


def test_the_scheduler_role_may_invoke_every_function_this_deploy_schedules():
    """A schedule pointing at a function the role may not invoke fails in the scheduler's
    own metrics and nowhere else -- indistinguishable from a schedule that ran and found
    nothing. So the Resource list has to be exhaustive, checked against the schedules the
    file actually creates rather than against a list somebody remembered to update."""
    triggers = _deploy_src("08_triggers.py")
    role_body = triggers.split("def ensure_scheduler_role")[1].split("\ndef ")[0]
    targets = set(re.findall(r'function:(llmops-[a-z-]+)"', role_body))
    scheduled = set(re.findall(r'function:(llmops-[a-z-]+)"', triggers)) - targets
    scheduled |= {m for m in re.findall(r'f"arn:aws:lambda:\{region\}:\{account\}:'
                                       r'function:(llmops-[a-z-]+)"', triggers)}
    missing = scheduled - targets
    assert not missing, (
        f"08_triggers.py schedules {sorted(missing)} but llmops-scheduler-invoke may not "
        "invoke them; the schedule would be ENABLED, healthy in the console, and dead")


def test_the_sweep_function_is_deployed_by_the_deployer_that_schedules_it():
    """The rule the finops entry in 07_lambdas.py already records: 08_triggers.py creates a
    live ENABLED schedule against this function name, so omitting it from LAMBDAS leaves a
    daily invocation of a function that does not exist."""
    lambdas = _deploy_src("07_lambdas.py")
    assert '"fn": "llmops-monitor-sweep"' in lambdas
    assert '/llmops/iam/lambda_monitor_sweep_arn' in lambdas, (
        "the sweep must get its OWN role, not the driver's -- a cost-control probe with "
        "every permission the pipeline it probes has is not a control")
    mod = _lambdas_mod()
    passed = set(mod.env_keys_for(mod.LAMBDAS["monitor_sweep"]))
    for key in ("EVENTS_TABLE", "DATA_BUCKET", "DRIVER_FN", "PROJECT"):
        assert key in passed, f"{key} is read by the handler and not passed at deploy time"


# --- #71: the env a handler requires is derived from the handler ----------------------
# The list used to be hand-copied into LAMBDAS[*]["env_keys"], and for the driver it was
# WRONG: handler.py reads ACTUALS_TABLE (handle_finops_tool, for the #finding# rows the
# cost audit writes) and the list named six other variables. The driver role has granted
# PutItem on that table since the statement was added FOR this call. So the permission was
# there, the code was there, and the variable was not -- `KeyError: 'ACTUALS_TABLE'`,
# measured live on 2026-08-01 (3x) and 2026-08-09 (3x, one per Lambda async retry, each
# retry a fresh billed AgentCore turn). llmops-cost-actuals holds ZERO #finding# rows for
# the entire life of the system.

def test_every_required_env_var_a_handler_reads_is_passed_at_deploy_time():
    """The guard that would have caught the eight-day ACTUALS_TABLE gap on day one.

    Derived on both sides: what each handler requires comes from its own source, and what
    it gets comes from the deployer. A hand-written expectation here would just be a third
    copy of the same fact, wrong in its own way.
    """
    mod = _lambdas_mod()
    gaps = {}
    for key, cfg in mod.LAMBDAS.items():
        required = mod.required_env_keys(cfg["src"])
        missing = required - set(mod.env_keys_for(cfg))
        if missing:
            gaps[key] = sorted(missing)
    assert not gaps, (
        f"these handlers read env vars the deploy never passes: {gaps}. The Lambda "
        "raises KeyError at the line that reads it -- for the finops tools that is "
        "inside an agent turn a day after the deploy reported success.")


def test_the_driver_gets_the_actuals_table_it_writes_findings_to():
    """The specific regression, pinned by name.

    Not covered by the derived guard above on its own: that one passes if someone deletes
    the read instead of adding the variable, which would silently drop the audit's
    findings rather than crash on them.
    """
    mod = _lambdas_mod()
    driver_src = (REPO / "orchestration" / "harness_driver" / "handler.py").read_text()
    assert 'os.environ["ACTUALS_TABLE"]' in driver_src, (
        "the driver must still record the finops agent's findings; if this read is gone, "
        "check that the findings are written somewhere else before relaxing the test")
    assert "ACTUALS_TABLE" in mod.env_keys_for(mod.LAMBDAS["driver"])


def test_every_value_the_deployer_passes_is_a_value_it_knows():
    """A required key with no value in env_values is a deploy that must not proceed.

    Silently passing the others is how the original bug behaved: five of six variables
    arrived, the function came up healthy, and the sixth surfaced as a KeyError a day
    later. Refusing names the missing key while a human is still watching the deploy.
    """
    mod = _lambdas_mod()

    class _SSM:
        def get_parameter(self, Name):
            return {"Parameter": {"Value": "test-bucket"}}

    for key, cfg in mod.LAMBDAS.items():
        env = mod.env_values(_SSM(), "us-east-1", "123456789012",
                             mod.env_keys_for(cfg), None)
        assert set(env) == set(mod.env_keys_for(cfg)), key
        assert all(v for v in env.values()), f"{key} got an empty value: {env}"

    with pytest.raises(KeyError, match="NOT_A_REAL_SETTING"):
        mod.env_values(_SSM(), "us-east-1", "123456789012",
                       ["RUNS_TABLE", "NOT_A_REAL_SETTING"], None)


def test_optional_env_vars_are_the_ones_the_handlers_actually_default():
    """OPTIONAL_ENV is an exemption list, so it has to name real defaulted reads.

    An entry that no handler reads with a default is either a typo or -- worse -- a
    required variable somebody exempted to make this file's guards go quiet.
    """
    mod = _lambdas_mod()
    sources = "\n".join(cfg["src"].read_text() for cfg in mod.LAMBDAS.values())
    for name in mod.OPTIONAL_ENV:
        if name == "AWS_REGION":
            continue          # set by the Lambda runtime itself, never by us
        assert f'os.environ.get("{name}"' in sources, (
            f"{name} is exempted from the required-env guard but no handler reads it "
            "with a default; if it is required, pass it instead of exempting it")
        assert f'os.environ["{name}"]' not in sources, (
            f"{name} is exempted AND read without a default somewhere -- that is the "
            "exact shape of the ACTUALS_TABLE bug, hidden behind the exemption list")


# --- #42: placeholder substitution for harness configs --------------------------------
# The s3 skill-source shape is a single URI that embeds the bucket name, and this
# account's bucket name embeds the account id -- which may not appear in a file of this
# public repo (hooks/pre-commit + .github/workflows/redaction-check.yml). So the configs
# carry <DATA_BUCKET> and the deploy resolves it. deploy/01_iam.py has resolved exactly
# these tokens in its policy documents since Phase 1; this is that mechanism applied to
# agents/*/harness.json, which had NO substitution step at all before this change.

@pytest.fixture(scope="module")
def subst():
    return _load("llmops_config_subst", "deploy/config_subst.py")


@pytest.fixture(scope="module")
def harnesses_mod():
    """deploy/05_harnesses.py as a module (name starts with a digit). Import-time safe."""
    return _load("llmops_05_harnesses", "deploy/05_harnesses.py")


def test_no_harness_config_contains_a_literal_account_id():
    """The redaction scan enforces this repo-wide; this says WHY for these files.

    A CI failure reading "possible account ID found" does not tell the next person that a
    skill URI is the reason a bucket name wanted to be literal here, nor that a
    placeholder is the supported way to write one.
    """
    import re as _re
    bad = []
    for cfg in sorted((REPO / "agents").glob("*/harness*.json")):
        for m in _re.finditer(r"(?<![0-9.])[0-9]{12}(?![0-9.])", cfg.read_text()):
            bad.append(f"{cfg.relative_to(REPO)}: {m.group(0)}")
    assert not bad, (
        f"literal 12-digit account id(s) in harness configs: {bad}. Write <ACCOUNT_ID> / "
        "<DATA_BUCKET> instead -- deploy/config_subst.py resolves them at deploy time, "
        "and the CI redaction scan fails the build on a literal one.")


def test_every_s3_skill_uri_uses_the_bucket_placeholder():
    """An s3 source must name <DATA_BUCKET>, not a bucket spelled out.

    Enforced separately from the account-id guard because a hardcoded bucket without an
    account id in the name (`my-skills-bucket`) passes redaction and still pins every
    harness to one account's storage.
    """
    wrong = []
    for cfg in sorted((REPO / "agents").glob("*/harness*.json")):
        for i, s in enumerate(json.loads(cfg.read_text()).get("skills") or []):
            uri = (s.get("s3") or {}).get("uri")
            if uri and not uri.startswith("s3://<DATA_BUCKET>/"):
                wrong.append(f"{cfg.relative_to(REPO)} skills[{i}]: {uri}")
    assert not wrong, (
        "s3 skill sources must be written s3://<DATA_BUCKET>/<path>: " + "; ".join(wrong))


def test_every_placeholder_a_config_uses_has_a_value(subst):
    """A token nobody can resolve is the failure this whole mechanism exists to prevent.

    `<DATABUCKET>` would sail past the linter, past validate_config, and past
    UpdateHarness -- which mints a version and reports READY -- and then kill every
    session at START. So the set of tokens the configs use must be a subset of the set the
    mapping knows.
    """
    known = set(subst.mapping_for("123456789012", "us-east-1"))
    for cfg in sorted((REPO / "agents").glob("*/harness*.json")):
        used = set(subst.unresolved(json.loads(cfg.read_text())))
        unknown = used - known
        assert not unknown, (
            f"{cfg.relative_to(REPO)} uses {sorted(unknown)}, which nothing resolves. "
            f"Known tokens: {sorted(known)}. AgentCore accepts an unresolved URI and the "
            "harness fails at session start, not at deploy.")


def test_resolve_refuses_a_config_with_a_token_left_in_it(subst):
    """The load-bearing half. Substituting is easy; refusing to ship the leftover is the
    part that turns a session-start failure into a deploy-time error."""
    cfg = {"skills": [{"s3": {"uri": "s3://<DATA_BUCKET>/skills/x"}},
                      {"s3": {"uri": "s3://<DATABUCKET>/skills/y"}}]}
    mapping = subst.mapping_for("123456789012", "us-east-1")
    with pytest.raises(SystemExit, match="DATABUCKET"):
        subst.resolve(cfg, mapping, where="agents/x/harness.json")


def test_resolve_reports_every_unresolved_token_not_just_the_first(subst):
    """One deploy names every token you must supply, instead of one per re-run."""
    cfg = {"a": "<ONE>", "b": ["<TWO>", {"c": "<THREE>"}]}
    with pytest.raises(SystemExit) as e:
        subst.resolve(cfg, {}, where="x")
    for tok in ("<ONE>", "<TWO>", "<THREE>"):
        assert tok in str(e.value), f"{tok} was not reported: {e.value}"


def test_substitution_reaches_a_skill_uri_nested_in_a_list(subst):
    """skills is a LIST of dicts, so a substituter that only walked dict VALUES would
    leave every skill URI untouched while resolving the flat fields and reporting success."""
    cfg = {"harnessName": "llmops_monitor",
           "skills": [{"s3": {"uri": "s3://<DATA_BUCKET>/skills/llmops/llm-observability"}}]}
    out = subst.resolve(cfg, subst.mapping_for("123456789012", "us-east-1",
                                               bucket="llmops-agentic-x"))
    assert out["skills"][0]["s3"]["uri"] == \
        "s3://llmops-agentic-x/skills/llmops/llm-observability"


def test_an_explicit_bucket_beats_the_derived_one(subst):
    """03_storage.py PUBLISHES the bucket to SSM; a name derived from the account id would
    disagree with it after any deploy that passed --bucket, and a skill URI pointing at a
    bucket that does not exist fails at session start."""
    m = subst.mapping_for("123456789012", "us-east-1", bucket="chosen-bucket")
    assert m["<DATA_BUCKET>"] == "chosen-bucket"
    assert subst.mapping_for("123456789012", "us-east-1")["<DATA_BUCKET>"] == \
        "llmops-agentic-123456789012-us-east-1"


def test_the_deployer_resolves_placeholders_before_sending_a_config(harnesses_mod):
    """05_harnesses.load_config must return a resolved config, and must REFUSE one that
    still carries a token. It did neither before #42: the script's only transforms were
    strip_comments and ensure_env, so a placeholder URI had nothing to resolve it."""
    mapping = {"<ACCOUNT_ID>": "123456789012", "<REGION>": "us-east-1",
               "<DATA_BUCKET>": "bkt"}
    cfg = harnesses_mod.load_config("monitor", prod=False, mapping=mapping)
    left = [s for s in json.dumps(cfg).split() if "<DATA_BUCKET>" in s]
    assert not left, f"load_config returned unresolved tokens: {left}"
    for skill in cfg.get("skills") or []:
        uri = (skill.get("s3") or {}).get("uri")
        if uri:
            assert uri.startswith("s3://bkt/"), f"{uri} was not substituted"


# ---------------------------------------------------------------------------
# Escalation routing: the bus had ZERO rules for five phases (#59)
# ---------------------------------------------------------------------------
def _deploy_src_orch(name):
    return (REPO / "deploy" / name).read_text()


def _lambdas_mod():
    return _load("deploy_lambdas_triage", "deploy/07_lambdas.py")


def _triage_rule_pattern():
    """The rule pattern as the deployer would actually PUT it, via --dry-run."""
    mod = _lambdas_mod()
    return mod.ensure_triage_rule(None, None, "us-east-1", "123456789012",
                                  True)["pattern"]


def test_every_event_that_needs_a_listener_has_a_rule():
    """The allowlist guard, and the reason this task existed.

    The llmops-pipeline bus carried ZERO rules while EscalatedToHuman was emitted from
    three places and documented as routing to the conductor. On a live bus "no rule" and
    "rule missing" look identical -- there is no failure, no metric, no log line; the
    event simply lands and nothing happens. So the decision about which detail-types
    need a listener is DECLARED in the contracts (EVENTS_NEEDING_A_RULE) and checked
    against the rules the deployer builds.

    Not a count: a count passes when a rule is added for the wrong detail-type.
    """
    src = _deploy_src_orch("07_lambdas.py")
    # Every detail-type any rule in the deployer matches on, read from the source so a
    # rule added for the wrong event cannot satisfy this by arithmetic.
    matched = {getattr(ev, n) for n in re.findall(r'"detail-type": \[ev\.(\w+)\]', src)}
    assert matched, "guard reads the wrong thing: found no rule detail-types at all"
    declared = {v for k, v in vars(ev).items()
                if k.isupper() and isinstance(v, str)}
    needed = set(ev.EVENTS_NEEDING_A_RULE)
    missing = needed - matched
    assert not missing, (
        f"{missing} is in EVENTS_NEEDING_A_RULE but no rule in 07_lambdas.py matches "
        "that detail-type; the event would land on the bus and nothing would happen")
    assert needed <= declared, "EVENTS_NEEDING_A_RULE names an event not in events.py"


def test_every_emitted_detail_type_is_either_ruled_or_declared_fire_and_forget():
    """The other direction: an event emitted somewhere in the repo with no rule must be
    a DECISION, not an oversight. Fire-and-forget is fine -- most of the vocabulary is
    audit trail and console timeline -- but it has to be visible as a choice, which
    EVENTS_NEEDING_A_RULE's absence records. This fails when someone adds an emitter for
    a detail-type that plainly wants a listener and nobody notices."""
    emitted = set()
    for rel in ("orchestration/harness_driver/handler.py",
                "orchestration/start_pipeline/handler.py",
                "orchestration/resume_pipeline/handler.py"):
        src = (REPO / rel).read_text()
        emitted |= {getattr(ev, n) for n in re.findall(r"ev\.([A-Z_]+)", src)
                    if isinstance(getattr(ev, n, None), str) and getattr(ev, n) in ev.ALL_EVENTS}
    asl = (REPO / "orchestration/state_machine.asl.json").read_text()
    emitted |= set(re.findall(r'"DetailType": "(\w+)"', asl))
    unknown = emitted - set(ev.ALL_EVENTS)
    assert not unknown, f"emitted detail-types not in ALL_EVENTS: {unknown}"
    # Nothing to assert about the fire-and-forget ones beyond that they are known --
    # the point of the pair is that EVENTS_NEEDING_A_RULE is the only place the
    # distinction lives, so the previous test is what has teeth.
    assert ev.ESCALATED_TO_HUMAN in emitted


def test_the_triage_rule_is_on_the_custom_bus_not_the_default_one():
    """The SageMaker rule uses the DEFAULT bus because service events land there and
    cannot be moved. Copying that shape for a pipeline event gives a rule that is live,
    healthy in the console, and matches nothing forever: llmops.pipeline events are put
    on llmops-pipeline."""
    mod = _lambdas_mod()

    class _Events:
        def __init__(self):
            self.rules, self.targets = [], []

        def put_rule(self, **kw):
            self.rules.append(kw)

        def put_targets(self, **kw):
            self.targets.append(kw)

    class _Lam:
        exceptions = type("E", (), {"ResourceConflictException": Exception})

        def add_permission(self, **kw):
            self.perm = kw

    events, lam = _Events(), _Lam()
    mod.ensure_triage_rule(events, lam, "us-east-1", "123456789012", False)
    assert events.rules[0]["EventBusName"] == "llmops-pipeline", \
        "the triage rule was created on the default bus; it would match nothing"
    assert events.targets[0]["EventBusName"] == "llmops-pipeline", \
        "put_targets without EventBusName targets a same-named rule on the DEFAULT bus"
    assert events.targets[0]["Targets"][0]["Arn"].endswith("llmops-harness-driver")
    # The permission's SourceArn must name the bus too: rule/<bus>/<name>, not rule/<name>.
    assert "rule/llmops-pipeline/llmops-escalation-triage" in lam.perm["SourceArn"]


def test_the_triage_rule_source_and_detail_type_come_from_the_contracts():
    """A rule whose source disagrees with the emitter by one character matches nothing
    and looks healthy. Both sides must read the same constant."""
    pattern = _triage_rule_pattern()
    assert pattern["source"] == [ev.EVENT_SOURCE]
    assert pattern["detail-type"] == [ev.ESCALATED_TO_HUMAN]


def test_a_triage_cannot_trigger_another_triage():
    """handle_page_human emitted EscalatedToHuman until this change, so the first rule to
    route that detail-type to triage would have looped: escalate -> triage -> page ->
    triage, each lap paying for a real harness turn. Two independent defences, because
    the loop is expensive and silent: page_human now emits OwnerPaged, and the rule
    excludes the conductor's own stage."""
    pattern = _triage_rule_pattern()
    assert pattern["detail"]["stage"] == [{"anything-but": ["orchestrator"]}], \
        "the rule does not exclude orchestrator-stage escalations"
    handler_src = (REPO / "orchestration/harness_driver/handler.py").read_text()
    page = handler_src.split("def handle_page_human", 1)[1].split("\ndef ", 1)[0]
    assert "ev.OWNER_PAGED" in page, "page_human still emits the triage trigger"
    assert "ev.ESCALATED_TO_HUMAN" not in page, \
        "page_human emits EscalatedToHuman; the triage rule would feed itself"


def test_the_informational_model_failover_is_not_an_escalation():
    """_maybe_failover_model hot-swaps the model and the retry CONTINUES -- nobody needs
    to decide anything. It said so in a reason string ("informational, pipeline
    continuing"), which was harmless only while the bus had no rules: an EventBridge
    pattern cannot read prose, so a rule on EscalatedToHuman would have paged the
    conductor about a run that had just healed itself. The discrimination has to live in
    the detail-type, where a rule can see it."""
    src = (REPO / "orchestration/harness_driver/handler.py").read_text()
    fn = src.split("def _maybe_failover_model", 1)[1].split("\ndef ", 1)[0]
    assert "ev.MODEL_FAILED_OVER" in fn
    assert "ev.ESCALATED_TO_HUMAN" not in fn, \
        "a self-healed failover still emits the triage trigger"


def test_every_escalation_emitter_carries_the_stage_the_rule_filters_on():
    """The rule uses `anything-but`, which does NOT match an event lacking the key at
    all. So an emitter that omits `stage` is dropped silently -- the same invisible
    no-match this whole task is about, reintroduced from the emitter side. Both live
    emitters are checked: the driver's handle_escalate and the ASL's EscalateFail, which
    carried only run_id and iteration until this change."""
    pattern = _triage_rule_pattern()
    assert "anything-but" in json.dumps(pattern), "guard reads the wrong pattern shape"
    src = (REPO / "orchestration/harness_driver/handler.py").read_text()
    esc = src.split("def handle_escalate", 1)[1].split("\ndef ", 1)[0]
    assert '"stage"' in esc, "handle_escalate's event detail has no stage key"
    asl = json.loads((REPO / "orchestration/state_machine.asl.json").read_text())
    detail = asl["States"]["EscalateFail"]["Parameters"]["Entries"][0]["Detail"]
    assert "stage" in detail, (
        "EscalateFail emits no stage, so anything-but cannot match it: every terminal "
        "pipeline failure -- the escalations that most need a triager -- would be dropped")


def test_the_bus_event_becomes_a_driver_invocation_the_driver_can_run():
    """The translation, asserted against the fields _run_stage actually dereferences.
    An invocation missing any of these raises KeyError inside the driver, i.e. after the
    event is already consumed."""
    record = {"detail-type": "EscalatedToHuman", "source": "llmops.pipeline",
              "detail": {"run_id": "run-abc", "stage": "data-prep",
                         "reason": "teacher budget infeasible", "iteration": 2}}
    inv = driver.triage_event_from_bus(record, "llmops-data-test")
    for key in ("run_id", "stage", "task", "manifest_uri", "harness_id"):
        assert inv[key], f"{key} missing from the translated invocation"
    assert inv["task"] == "triage"
    assert inv["harness_id"] == "llmops_orchestrator"
    assert inv["params"]["escalation"]["run_id"] == "run-abc"
    assert inv["params"]["escalation"]["reason"] == "teacher budget infeasible"
    # No task token: an EventBridge delivery has nothing to strand.
    assert "task_token" not in inv


def test_the_triage_runs_under_its_own_run_id_not_the_escalated_runs():
    """The subtle one, and the reason a synthetic id is worth the confusion it costs.

    take_directive() is keyed on event["run_id"] and its ONLY caller is the checkpoint
    branch. A triage invoked under the subject's id would pop the subject's own parked
    verdict -- the one the conductor is in the middle of writing -- and receive it as a
    directive from an accountable human. The conductor would be answering itself.

    handle_escalate and handle_job_launched also write the runs table keyed on that id,
    so a triage that escalated in turn would overwrite the SUBJECT run's status."""
    inv = driver.triage_event_from_bus(
        {"detail-type": "EscalatedToHuman", "detail": {"run_id": "run-abc"}},
        "llmops-data-test")
    assert inv["run_id"] != "run-abc", (
        "the triage runs under the escalated run's id: its first checkpoint would eat "
        "the verdict it just parked for that run")
    assert "run-abc" in inv["run_id"], "the triage id should still name its subject"
    # The manifest is the SUBJECT's -- a triage has none of its own, and reading the
    # stuck run's manifest is the first thing the prompt's triage clause asks for.
    assert inv["manifest_uri"] == "s3://llmops-data-test/runs/run-abc/manifest.json"


def test_an_escalation_with_no_run_id_is_rejected_not_triaged():
    """Nothing to triage, and a manifest URI built from an empty id would point at
    s3://bucket/runs//manifest.json -- a 404 the conductor would report as "no manifest"
    rather than as a malformed event."""
    with pytest.raises(ValueError):
        driver.triage_event_from_bus({"detail-type": "EscalatedToHuman",
                                      "detail": {"stage": "eval"}}, "b")


def test_the_driver_recognises_a_bus_delivery_at_its_entry_point():
    """handler() must translate before anything dereferences event["stage"], and must
    recognise the envelope by its OWN keys rather than by a missing task token: plenty of
    legitimate driver invocations (finops, console dispatch) carry no token."""
    seen = {}

    def _fake_run_stage(event, context=None, c=None):
        seen.update(event)
        return {"status": "ok"}

    real = driver._run_stage
    try:
        driver._run_stage = _fake_run_stage
        out = driver.handler({"detail-type": "EscalatedToHuman", "source": "llmops.pipeline",
                              "detail": {"run_id": "run-xyz", "stage": "finetune"}},
                             None, clients())
    finally:
        driver._run_stage = real
    assert out == {"status": "ok"}
    assert seen["task"] == "triage", "the bus envelope reached _run_stage untranslated"
    assert seen["params"]["escalation"]["run_id"] == "run-xyz"


def test_a_state_machine_payload_is_not_mistaken_for_a_bus_delivery():
    """The negative half: a normal stage invocation must pass through untouched."""
    seen = {}

    def _fake_run_stage(event, context=None, c=None):
        seen.update(event)
        return {"status": "ok"}

    real = driver._run_stage
    try:
        driver._run_stage = _fake_run_stage
        driver.handler(driver_event(), None, clients())
    finally:
        driver._run_stage = real
    assert seen["task"] == "generate" and seen["stage"] == "data-prep"


def test_the_triage_rule_is_deployable_on_its_own_and_in_a_bare_run(monkeypatch, capsys):
    """Same --only contract as every other target (#51): the rule must be shippable
    without redeploying the driver, and a bare run must not skip it."""
    mod = _lambdas_mod()
    monkeypatch.setattr(mod.boto3, "client", lambda svc, **kw: object())
    monkeypatch.setattr(sys, "argv", ["07_lambdas.py", "--region", "us-east-1",
                                      "--dry-run", "--only", "triage_rule"])
    mod.main()
    report = json.loads(capsys.readouterr().out)
    assert report["targets"] == ["triage_rule"]
    assert [r.get("rule") for r in report["results"]] == ["llmops-escalation-triage"]
    assert not [r for r in report["results"] if "lambda" in r or "state_machine" in r]
    assert "triage_rule" in mod.NON_LAMBDA_TARGETS, "a bare run would skip the rule"


# ---------------------------------------------------------------------------
# The live bus vs the bytes about to ship (#61)
# ---------------------------------------------------------------------------
# #59 built the escalation channel; a later deploy from a DIFFERENT branch overwrote the
# driver with a build that had no triage_event_from_bus, while llmops-escalation-triage
# stayed ENABLED and pointed at it. Every escalation then reached the driver as a raw
# EventBridge envelope and died on KeyError: 'run_id' before any handler branch ran.
#
# Every guard above stayed green, and that is the part worth fixing rather than the
# missing function: they compare EVENTS_NEEDING_A_RULE against the rules THIS TREE's
# deployer builds, so a branch carrying neither the declaration, nor the rule, nor the
# translator is perfectly self-consistent. A tree cannot know what is live on the bus.
# So the check moved to deploy time, where both facts are available at once.

class _FakeEvents:
    """Minimal EventBridge stand-in: rules and targets, per bus."""

    def __init__(self, rules):
        self._rules = rules  # {bus: [ {Name, State, EventPattern, Targets} ]}

    def list_rules(self, EventBusName):
        return {"Rules": [{k: v for k, v in r.items() if k != "Targets"}
                          for r in self._rules.get(EventBusName, [])]}

    def list_targets_by_rule(self, Rule, EventBusName):
        for r in self._rules.get(EventBusName, []):
            if r["Name"] == Rule:
                return {"Targets": r.get("Targets", [])}
        return {"Targets": []}


DRIVER_FN = "llmops-harness-driver"
_TRIAGE_ARN = f"arn:aws:lambda:us-east-1:123456789012:function:{DRIVER_FN}"


def _triage_rule_live(state="ENABLED", targets=None, detail_types=None):
    return {"llmops-pipeline": [{
        "Name": "llmops-escalation-triage",
        "State": state,
        "EventPattern": json.dumps({
            "source": [ev.EVENT_SOURCE],
            "detail-type": detail_types or [ev.ESCALATED_TO_HUMAN],
            "detail": {"stage": [{"anything-but": ["orchestrator"]}]}}),
        "Targets": targets if targets is not None else [{"Id": "triage",
                                                        "Arn": _TRIAGE_ARN}],
    }]}


def test_the_real_driver_source_can_read_the_real_live_rule():
    """The regression itself: today's handler.py against today's rule shape.

    Reads the actual file rather than a fixture, so deleting the translator fails here.
    """
    mod = _lambdas_mod()
    src = (REPO / "orchestration/harness_driver/handler.py").read_text()
    gaps = mod.live_bus_translator_gap(_FakeEvents(_triage_rule_live()), src,
                                       DRIVER_FN, "llmops-pipeline")
    assert gaps == [], f"the driver cannot read a live rule's envelope: {gaps}"


def test_a_driver_without_the_translator_is_a_gap_the_deploy_can_see():
    """The branch that caused this: the rule is live, the handler has no translator.

    Simulated by stripping the function name from the source, which is precisely the
    state the deployed zip was in -- verified live by downloading it: the translator
    string was absent while _stamp_dispatch was present.
    """
    mod = _lambdas_mod()
    src = (REPO / "orchestration/harness_driver/handler.py").read_text()
    without = src.replace("triage_event_from_bus", "some_other_function")
    assert "triage_event_from_bus" not in without
    gaps = mod.live_bus_translator_gap(_FakeEvents(_triage_rule_live()), without,
                                       DRIVER_FN, "llmops-pipeline")
    assert len(gaps) == 1, gaps
    assert gaps[0]["rule"] == "llmops-escalation-triage"
    assert gaps[0]["detail_type"] == ev.ESCALATED_TO_HUMAN
    assert "triage_event_from_bus" in gaps[0]["problem"]


def test_the_deploy_refuses_rather_than_warns_when_the_translator_is_missing():
    """A warning is useless here: the deploy reports success and the channel is dead.

    Same rule as config_subst.resolve() -- an unresolved token and an unreadable envelope
    are both accepted by the API and both fail later, out of sight of the person
    deploying.
    """
    mod = _lambdas_mod()
    tmp = REPO / "tests" / "_tmp_driver_no_translator.py"
    tmp.write_text((REPO / "orchestration/harness_driver/handler.py").read_text()
                   .replace("triage_event_from_bus", "gone"))
    try:
        cfg = dict(mod.LAMBDAS["driver"], src=tmp)
        with pytest.raises(SystemExit) as exc:
            mod.deploy_lambda(None, None, "us-east-1", "123456789012", "driver", cfg,
                              False, _FakeEvents(_triage_rule_live()))
        # The message has to name the function AND the rule: "deploy failed" sends
        # someone reading Lambda logs, and the fault is on the bus.
        assert "llmops-harness-driver" in str(exc.value)
        assert "llmops-escalation-triage" in str(exc.value)
    finally:
        tmp.unlink()


def test_a_dry_run_never_reaches_the_bus_check():
    """--dry-run must stay offline; conftest refuses the socket, so this also proves the
    check is not on the path that reports what WOULD happen."""
    mod = _lambdas_mod()

    class _Boom:
        def list_rules(self, **kw):
            raise AssertionError("a dry run called EventBridge")

    out = mod.deploy_lambda(None, None, "us-east-1", "", "driver",
                            mod.LAMBDAS["driver"], True, _Boom())
    assert out["would"] == "create/update"


def test_an_input_transformer_on_the_rule_removes_the_need_for_a_translator():
    """The two are alternatives, and the check must know it.

    #59 chose Python over an InputTransformer deliberately (a transformer referencing a
    path an event lacks drops it silently). But if a rule DOES carry one, EventBridge has
    already reshaped the event and demanding the Python function would block a correct
    deploy -- a guard that cries wolf gets bypassed, which costs more than it saves.
    """
    mod = _lambdas_mod()
    src = "no translator here at all"
    transformed = [{"Id": "triage", "Arn": _TRIAGE_ARN,
                    "InputTransformer": {"InputPathsMap": {"r": "$.detail.run_id"},
                                         "InputTemplate": '{"run_id": <r>}'}}]
    assert mod.live_bus_translator_gap(
        _FakeEvents(_triage_rule_live(targets=transformed)), src, DRIVER_FN,
        "llmops-pipeline") == []


def test_a_disabled_rule_delivers_nothing_and_blocks_nothing():
    """A DISABLED rule cannot invoke anything, so it must not hold up a deploy."""
    mod = _lambdas_mod()
    assert mod.live_bus_translator_gap(
        _FakeEvents(_triage_rule_live(state="DISABLED")), "no translator", DRIVER_FN,
        "llmops-pipeline") == []


def test_a_rule_targeting_a_different_function_is_not_this_deploys_problem():
    """Scoped to the function being deployed. Otherwise every Lambda deploy is gated on
    every rule on the bus, and the check becomes something people pass with --force."""
    mod = _lambdas_mod()
    other = [{"Id": "x", "Arn": "arn:aws:lambda:us-east-1:123456789012:function:other"}]
    assert mod.live_bus_translator_gap(
        _FakeEvents(_triage_rule_live(targets=other)), "no translator", DRIVER_FN,
        "llmops-pipeline") == []


def test_a_live_rule_for_an_undeclared_detail_type_is_itself_reported():
    """The other direction: a rule delivering something no translator is declared for.

    This is how the NEXT version of this defect arrives -- someone adds a rule pointing
    at the driver for a detail-type nobody wrote a branch for. The handler receives an
    envelope it has no branch for, which is the same crash by a different route, so the
    check names it instead of silently passing an unknown.
    """
    mod = _lambdas_mod()
    gaps = mod.live_bus_translator_gap(
        _FakeEvents(_triage_rule_live(detail_types=["SomeNewEvent"])),
        (REPO / "orchestration/harness_driver/handler.py").read_text(),
        DRIVER_FN, "llmops-pipeline")
    assert [g["detail_type"] for g in gaps] == ["SomeNewEvent"]
    assert "BUS_DELIVERY_TRANSLATORS" in gaps[0]["problem"]


def test_an_unreachable_bus_is_reported_as_unchecked_not_as_clean():
    """No credentials, no bus, an API error: say the check did not happen.

    Returning [] would make an unreachable bus indistinguishable from an agreeing one --
    the exact "no rule and rule missing look identical" confusion #59 was about, moved
    into the guard built to prevent it. The deploy proceeds (a network blip must not
    block shipping) but says so on stderr.
    """
    mod = _lambdas_mod()

    class _Down:
        def list_rules(self, **kw):
            raise RuntimeError("bus unreachable")

    gaps = mod.live_bus_translator_gap(_Down(), "src", DRIVER_FN, "llmops-pipeline")
    assert len(gaps) == 1 and "unchecked" in gaps[0]
    assert "bus unreachable" in gaps[0]["unchecked"]


def test_only_the_bus_delivered_lambda_is_checked_against_the_bus():
    """Exactly one of the six spine Lambdas is an EventBridge target; the rest are
    invoked by Step Functions, the console or a schedule. Marking them all would gate
    unrelated deploys on the bus and dilute the signal."""
    mod = _lambdas_mod()
    marked = {k for k, v in mod.LAMBDAS.items() if v.get("bus_delivered")}
    assert marked == {"driver"}, marked
    assert mod.LAMBDAS["driver"]["bus_delivered"] == "llmops-pipeline"


def test_every_declared_translator_exists_in_the_handler_it_names():
    """BUS_DELIVERY_TRANSLATORS is a promise about code; check it against the code.

    Declaring a translator that does not exist would make the deploy-time check pass on a
    name nobody implemented -- the guard would then be asserting its own spelling.
    """
    src = (REPO / "orchestration/harness_driver/handler.py").read_text()
    for detail_type, fname in ev.BUS_DELIVERY_TRANSLATORS.items():
        assert f"def {fname}(" in src, (
            f"{detail_type} declares translator {fname}(), absent from the driver")
        assert hasattr(driver, fname), f"{fname} is not importable from the driver"


def test_the_translator_declaration_covers_every_event_that_needs_a_rule():
    """The two contracts have to agree: an event that needs a rule to the driver needs a
    way for the driver to read it. Declaring the rule without the translator is exactly
    the half-built channel #59 found, and #61 was its mirror image."""
    for detail_type in ev.EVENTS_NEEDING_A_RULE:
        assert detail_type in ev.BUS_DELIVERY_TRANSLATORS, (
            f"{detail_type} is routed to a Lambda by a rule but no translator is "
            "declared for it; the delivery would arrive as a raw envelope")


def test_a_call_site_without_a_definition_does_not_satisfy_the_check():
    """The check looks for `def <name>(`, not the bare name, and a negative control is why.

    Renaming only the DEFINITION leaves the call site behind, and a bare-substring check
    passed on that source -- a handler that would raise NameError on the first escalation.
    A call to a function nobody defines is worse than no call: it fails at the same place
    for a reason one step further from the fix.
    """
    mod = _lambdas_mod()
    src = (REPO / "orchestration/harness_driver/handler.py").read_text().replace(
        "def triage_event_from_bus(", "def _renamed_translator(", 1)
    assert "triage_event_from_bus" in src, "the call site should still be there"
    gaps = mod.live_bus_translator_gap(_FakeEvents(_triage_rule_live()), src,
                                       DRIVER_FN, "llmops-pipeline")
    assert len(gaps) == 1 and "triage_event_from_bus" in gaps[0]["problem"], gaps


# ── the dead-driver resurrector ────────────────────────────────────────────────

resurrector = _load("resurrector", "orchestration/resurrector/handler.py")

RES_ENV = {"RUNS_TABLE": "llmops-pipeline-runs", "EVENT_BUS": "llmops-pipeline",
           "DRIVER_FN": "llmops-harness-driver", "AWS_REGION": "us-east-1"}


class _ResLambda:
    def __init__(self):
        self.calls = []

    def invoke(self, **kw):
        self.calls.append(kw)
        return {"StatusCode": 202}


def _res_clients(rows):
    ddb = FakeDDB()
    ddb.Table(RES_ENV["RUNS_TABLE"]).items.extend(rows)
    return {"ddb": ddb, "lambda": _ResLambda(), "events": FakeEvents()}


def _beat_row(run_id="run-res-1", minutes_old=45, **over):
    at = (datetime.datetime.now(datetime.timezone.utc)
          - datetime.timedelta(minutes=minutes_old)).isoformat()
    row = {"run_id": run_id, "status": "running",
           "driver_beat_at": at,
           "driver_beat_payload": json.dumps({
               "run_id": run_id, "stage": "data-prep", "task": "generate",
               "harness_id": "llmops_data_prep", "manifest_uri": "s3://b/m.json",
               "task_token": "tok-parked-in-history", "iteration": 0})}
    row.update(over)
    return row


class TestResurrector:
    def _run(self, rows, env_over=None):
        env = {**RES_ENV, **(env_over or {})}
        for k, v in env.items():
            os.environ[k] = v
        c = _res_clients(rows)
        out = resurrector.handler({}, clients=c)
        return out, c

    def test_a_stale_beat_on_a_running_run_is_resurrected_with_its_own_payload(self):
        """The incident this whole Lambda exists for: run 68cfa9c8 sat dead nine hours
        at 4/55 with Step Functions RUNNING and its token parked, because the async
        self-reinvoke was dropped and nothing anywhere had the job of noticing. The
        driver now stamps its re-invoke payload on every turn; a beat that stops while
        the run is unfinished IS the signal, and the stamp is the resurrection."""
        out, c = self._run([_beat_row()])
        assert out["acted"] and out["acted"][0]["action"] == "resurrected"
        assert len(c["lambda"].calls) == 1
        sent = json.loads(c["lambda"].calls[0]["Payload"])
        assert sent["run_id"] == "run-res-1" and sent["stage"] == "data-prep"
        assert c["lambda"].calls[0]["InvocationType"] == "Event"
        assert any(e["DetailType"] == ev.DRIVER_RESURRECTED
                   for e in c["events"].entries)

    def test_a_fresh_beat_is_left_alone(self):
        out, c = self._run([_beat_row(minutes_old=5)])
        assert not out["acted"] and not c["lambda"].calls

    def test_a_parked_token_run_is_waiting_not_dead(self):
        """A run row holding task_token is parked on a SageMaker job BY DESIGN —
        resume_pipeline owns that wake. Resurrecting it would start a second agent
        session next to a healthy launch-and-release wait; the silence the beat
        measures is the driver's, and a released driver is silent on purpose."""
        out, c = self._run([_beat_row(task_token="tok-parked-live")])
        assert not out["acted"] and not c["lambda"].calls

    def test_a_terminal_run_is_not_resurrected(self):
        out, c = self._run([_beat_row(status="failed"),
                            _beat_row(run_id="run-res-2", status="escalated")])
        assert not out["acted"] and not c["lambda"].calls

    def test_the_claim_is_conditional_on_the_beat_it_read(self):
        """Two overlapping sweeps must not double-resurrect one silence. The claim
        update is conditional on driver_beat_at still being the value this sweep
        read; the loser of that race walks away without invoking anything."""
        row = _beat_row()
        seen_beat = row["driver_beat_at"]  # the fake mutates the row in place
        out, c = self._run([row])
        claim = c["ddb"].Table(RES_ENV["RUNS_TABLE"]).updates[0]
        assert "driver_beat_at = :seen" in claim["ConditionExpression"]
        assert claim["ExpressionAttributeValues"][":seen"] == seen_beat

    def test_past_the_cap_it_escalates_instead_of_reviving_the_defect(self):
        """A driver that dies every turn has a real defect; revival only re-runs it.
        Past the cap the resurrector emits ESCALATED_TO_HUMAN — which routes to the
        conductor's triage — and stops touching the run."""
        out, c = self._run([_beat_row(resurrections=5)])
        assert out["acted"][0]["action"] == "escalated"
        assert not c["lambda"].calls
        assert any(e["DetailType"] == ev.ESCALATED_TO_HUMAN
                   for e in c["events"].entries)

    def test_a_pre_heartbeat_run_is_skipped_not_guessed_at(self):
        """A running row with no beat predates the heartbeat (or was not started by
        the driver at all). There is no payload to resurrect it with; inventing one
        would dispatch a stage the state machine never asked for."""
        out, c = self._run([{"run_id": "run-old", "status": "running"}])
        assert not out["acted"] and not c["lambda"].calls


class TestDriverHeartbeat:
    def test_every_turn_stamps_the_beat_with_the_reinvoke_payload(self):
        """The heartbeat is the resurrector's entire input: no stamp, no wake. It must
        carry the exact payload a re-invoke needs — task token included, because the
        parked token in Step Functions history is what the manual resurrection of
        68cfa9c8 was rebuilt from, by hand, at 2 a.m."""
        uri = "s3://llmops-data-test/runs/run-test-1/raw/data.jsonl"
        ac = FakeAgentCore([tool_use_stream("stage_complete", {"outputs": [uri]}),
                            text_stream("ack")])
        c = clients(ac, FakeS3(existing=[uri]))
        # heartbeat's ConditionExpression(attribute_exists) needs the row to exist,
        # exactly like production where start_pipeline creates it at dispatch
        c["ddb"].Table(ENV["RUNS_TABLE"]).items.append({"run_id": "run-test-1"})
        driver.handler(driver_event(), clients=c)
        beats = [u for u in c["ddb"].Table(ENV["RUNS_TABLE"]).updates
                 if "driver_beat_at" in u.get("UpdateExpression", "")]
        assert beats, "no turn stamped a heartbeat — the resurrector is blind"
        payload = json.loads(beats[0]["ExpressionAttributeValues"][":p"])
        assert payload["run_id"] == "run-test-1"
        assert payload["task_token"] == "tok-123", (
            "the stamped payload lost the task token — a resurrection from it would "
            "run the stage with no way to settle the machine")

    def test_a_heartbeat_write_failure_does_not_kill_the_turn(self):
        """The beat is telemetry about the work, not the work. A DynamoDB throttle on
        the stamp must not fail a turn that was about to complete the stage."""
        uri = "s3://llmops-data-test/runs/run-test-1/raw/data.jsonl"
        ac = FakeAgentCore([tool_use_stream("stage_complete", {"outputs": [uri]}),
                            text_stream("ack")])
        c = clients(ac, FakeS3(existing=[uri]))

        class _ThrottlingDDB(FakeDDB):
            def Table(self, name):
                t = super().Table(name)
                if name == ENV["RUNS_TABLE"] and not getattr(t, "_wrapped", False):
                    orig = t.update_item

                    def flaky(**kw):
                        if "driver_beat_at" in kw.get("UpdateExpression", ""):
                            raise Exception("ProvisionedThroughputExceededException")
                        return orig(**kw)
                    t.update_item = flaky
                    t._wrapped = True
                return t

        c["ddb"] = _ThrottlingDDB()
        out = driver.handler(driver_event(), clients=c)
        assert out["status"] == "completed", (
            "a throttled heartbeat killed a turn that was completing the stage")


# a production-shaped run id: real ones are >=33 chars, so session_id returns the plain
# base and the `-e<N>` suffix is the whole difference between epochs
ROLL_RUN_ID = "run-20260808T154900Z-68cfa9c8"
ROLL_MANIFEST_URI = f"s3://llmops-data-test/runs/{ROLL_RUN_ID}/manifest.json"
ROLL_OUTPUT_URI = f"s3://llmops-data-test/runs/{ROLL_RUN_ID}/raw/data.jsonl"


class TestSessionRollover:
    """AgentCore reclaims a runtime session at maxLifetime = 28800s (8h). That cap is
    absolute: activity does not reset it, and no configuration raises it. The ARC-2
    distillation stage runs 8-12h in ONE session (deterministic id, 55+ tasks, turn
    after turn through self-reinvokes), so it outlives its own session and the invoke
    that crosses the line fails in a way nothing here distinguishes from a real error.
    Rolling to a fresh session BEFORE the cap is the documented pattern."""

    def _ctx(self):
        class _Ctx:
            function_name = "llmops-harness-driver"

            def get_remaining_time_in_millis(self):
                return 860_000
        return _Ctx()

    class _Lam:
        def __init__(self):
            self.invocations = []

        def invoke(self, **kw):
            self.invocations.append(kw)
            return {"StatusCode": 202}

    def test_a_session_short_of_the_cap_is_never_rolled(self):
        """The roll costs a whole turn (fresh session, re-read S3, re-orient). Paying
        it on a stage that finishes in minutes would be pure waste, and rolling a
        session whose transcript still holds the task would drop context for nothing."""
        uri = ROLL_OUTPUT_URI
        ac = FakeAgentCore([tool_use_stream("stage_complete", {"outputs": [uri]}),
                            text_stream("ack")])
        c = clients(ac, FakeS3(existing=[uri]))
        out = driver.handler(driver_event(run_id=ROLL_RUN_ID, manifest_uri=ROLL_MANIFEST_URI), clients=c)
        assert out["status"] == "completed"
        assert ac.calls[0]["runtimeSessionId"] == f"{ROLL_RUN_ID}-data-prep-generate", (
            "a fresh stage got a rolled session id — epoch 0 must stay the plain "
            "deterministic id or every past run's session ids stop matching")

    def test_a_session_past_the_rollover_age_moves_to_a_fresh_one(self):
        """Red before the fix: the driver reused one session id forever, so hour 8
        arrived with the platform, not the driver, deciding what happened next."""
        uri = ROLL_OUTPUT_URI
        ac = FakeAgentCore([tool_use_stream("stage_complete", {"outputs": [uri]}),
                            text_stream("ack")])
        c = clients(ac, FakeS3(existing=[uri]))
        c["ddb"].Table(ENV["RUNS_TABLE"]).items.append({"run_id": ROLL_RUN_ID})
        # a continuation whose session was opened 7h+ ago — exactly the shape a
        # long distillation stage reaches after ~30 self-reinvokes
        event = driver_event(run_id=ROLL_RUN_ID, manifest_uri=ROLL_MANIFEST_URI,
            _continuation=[{"role": "user", "content": [{"text": "carry on"}]}],
            _session_started_at=time.time() - driver.SESSION_ROLLOVER_S - 60)
        out = driver.handler(event, clients=c)
        assert out["status"] == "completed"
        assert ac.calls[0]["runtimeSessionId"] == f"{ROLL_RUN_ID}-data-prep-generate-e1", (
            "the aged session was reused — the next invoke would have crossed "
            "AgentCore's 8h maxLifetime and died with an unattributable error")

    def test_the_rollover_threshold_leaves_margin_under_the_platform_cap(self):
        """A threshold at (or above) the cap is not a rollover, it is a race the
        platform wins. One in-flight harness turn is 840s, so the margin has to
        exceed that with room for the continuation behind it."""
        assert driver.SESSION_MAX_LIFETIME_S == 28800, (
            "maxLifetime is a platform constant (8h); if AWS changed it, change the "
            "citation in the code comment too")
        margin = driver.SESSION_MAX_LIFETIME_S - driver.SESSION_ROLLOVER_S
        assert margin >= 1800, (
            f"only {margin}s of margin: an 840s turn plus its handoff can still be "
            "caught by the cap")

    def test_a_rolled_session_re_seeds_the_task_instead_of_replaying_a_toolresult(self):
        """The pending message at roll time is usually a toolResult answering a
        toolUse the OLD session issued. A fresh session never issued it, so replaying
        it makes the first invoke invalid ('toolResult blocks ... exceeds the number
        of toolUse blocks of previous turn' — observed live on the console's dispatch
        path). The new session must be seeded with the task payload plus a resume
        instruction that points the agent at S3, which is where the state actually is."""
        uri = ROLL_OUTPUT_URI
        ac = FakeAgentCore([tool_use_stream("stage_complete", {"outputs": [uri]}),
                            text_stream("ack")])
        c = clients(ac, FakeS3(existing=[uri]))
        c["ddb"].Table(ENV["RUNS_TABLE"]).items.append({"run_id": ROLL_RUN_ID})
        event = driver_event(run_id=ROLL_RUN_ID, manifest_uri=ROLL_MANIFEST_URI,
            _continuation=[{"role": "user", "content": [
                {"toolResult": {"toolUseId": "tu-1", "content": [{"text": "{}"}],
                                "status": "success"}}]}],
            _session_started_at=time.time() - driver.SESSION_ROLLOVER_S - 60)
        driver.handler(event, clients=c)
        sent = json.dumps(ac.calls[0]["messages"], default=str) \
            if not isinstance(ac.calls[0].get("messages"), (bytes, str)) \
            else str(ac.calls[0]["messages"])
        assert "toolResult" not in sent, (
            "the rolled session was handed a toolResult it never asked for — the "
            "first invoke of every rolled session would fail")
        assert "manifest" in sent.lower(), (
            "the rolled session was not re-seeded with the task payload")

    def test_the_epoch_rides_the_continuation_so_both_resume_paths_agree(self):
        """Two things resume a stage: the driver's own self-reinvoke and the
        resurrector. Both rebuild the session id from the event. If the epoch is
        derived from a clock instead of carried, a resurrection lands in a DIFFERENT
        session than the driver it replaced — two live sessions, one task token."""
        src = (REPO / "orchestration/harness_driver/handler.py").read_text()
        body = src[src.index("def _run_stage"):]
        assert '"_session_epoch": epoch' in body, \
            "_self_reinvoke stopped carrying the session epoch"
        assert 'int(event.get("_session_epoch", 0))' in body, \
            "the continuation branch stopped restoring the session epoch"
        assert '"_session_epoch": epoch' in body.split("def _heartbeat")[1], \
            ("the stamped heartbeat payload lost the epoch — a resurrector waking "
             "from it would open a session the driver had already aged out of")

    def test_a_rolled_session_id_is_recorded_for_span_scoring(self):
        """The console reconstructs session ids from (run, stage, task) to point batch
        evaluation at the right spans. A rolled id is not reconstructible from that
        tuple, so an unrecorded epoch is a session whose spans nobody ever scores —
        and rolled sessions are precisely the long, expensive, interesting ones."""
        uri = ROLL_OUTPUT_URI
        ac = FakeAgentCore([tool_use_stream("stage_complete", {"outputs": [uri]}),
                            text_stream("ack")])
        c = clients(ac, FakeS3(existing=[uri]))
        c["ddb"].Table(ENV["RUNS_TABLE"]).items.append({"run_id": ROLL_RUN_ID})
        driver.handler(driver_event(run_id=ROLL_RUN_ID, manifest_uri=ROLL_MANIFEST_URI,
            _continuation=[{"role": "user", "content": [{"text": "carry on"}]}],
            _session_started_at=time.time() - driver.SESSION_ROLLOVER_S - 60),
            clients=c)
        rolls = [u for u in c["ddb"].Table(ENV["RUNS_TABLE"]).updates
                 if "rolled_session_ids" in u.get("UpdateExpression", "")]
        assert rolls, "the rolled session id was never recorded on the run row"
        assert rolls[0]["ExpressionAttributeValues"][":s"] == \
            [f"{ROLL_RUN_ID}-data-prep-generate-e1"]

    def test_recording_failure_does_not_block_the_roll(self):
        """Bookkeeping for a scoring convenience must never outrank keeping the stage
        alive: if the append throttles, the session still has to roll."""
        uri = ROLL_OUTPUT_URI
        ac = FakeAgentCore([tool_use_stream("stage_complete", {"outputs": [uri]}),
                            text_stream("ack")])
        c = clients(ac, FakeS3(existing=[uri]))

        class _ThrottlingDDB(FakeDDB):
            def Table(self, name):
                t = super().Table(name)
                if name == ENV["RUNS_TABLE"] and not getattr(t, "_wrapped", False):
                    orig = t.update_item

                    def flaky(**kw):
                        if "rolled_session_ids" in kw.get("UpdateExpression", ""):
                            raise Exception("ProvisionedThroughputExceededException")
                        return orig(**kw)
                    t.update_item = flaky
                    t._wrapped = True
                return t

        c["ddb"] = _ThrottlingDDB()
        out = driver.handler(driver_event(run_id=ROLL_RUN_ID, manifest_uri=ROLL_MANIFEST_URI,
            _continuation=[{"role": "user", "content": [{"text": "carry on"}]}],
            _session_started_at=time.time() - driver.SESSION_ROLLOVER_S - 60),
            clients=c)
        assert out["status"] == "completed", (
            "a throttled bookkeeping write killed the rolled stage")
        assert ac.calls[0]["runtimeSessionId"].endswith("-e1"), \
            "the roll itself was skipped when its bookkeeping failed"

    def test_the_console_scores_rolled_sessions_it_cannot_derive(self):
        """Derived guard: the driver records rolled ids only so the console can use
        them. A recorder with no reader is dead code, and the failure is silent —
        scoring quietly covers less than it claims."""
        src = (REPO / "deploy/console/lambda_function.py").read_text()
        fn = src[src.index("def _recent_session_ids"):src.index("def _pipeline_runtimes")]
        assert "rolled_session_ids" in fn, (
            "_recent_session_ids ignores rolled session ids — every session past "
            "hour 7 of a long stage is invisible to batch evaluation")


class TestSessionRolloverDocs:
    def test_the_rollover_is_documented_in_both_languages(self):
        for name in ("docs/ARCHITECTURE.md", "docs/ARCHITECTURE.zh-TW.md"):
            text = (REPO / name).read_text()
            assert "28800" in text or "8h" in text or "8 小時" in text, \
                f"{name} does not mention AgentCore's session lifetime cap"
            assert "epoch" in text.lower(), \
                f"{name} does not document the session-epoch rollover"
