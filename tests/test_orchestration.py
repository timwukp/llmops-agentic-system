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
}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class FakeTable:
    def __init__(self):
        self.items, self.updates = [], []
        self.query_result = []

    def put_item(self, Item):
        self.items.append(Item)

    def update_item(self, **kw):
        self.updates.append(kw)

    def query(self, **kw):
        return {"Items": self.query_result}


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
        rejection = ac.calls[1]["messages"][0]["content"][0]["toolResult"]
        assert rejection["content"][0]["json"]["status"] == "rejected"

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

    def test_missing_stage_complete_reasks_then_fails(self):
        ac = FakeAgentCore([text_stream("done, I think"), text_stream("still no call")])
        c = clients(ac)
        out = driver.handler(driver_event(), clients=c)
        assert out["status"] == "failed"
        assert len(ac.calls) == 2  # original + one re-ask
        assert "stage_complete" in ac.calls[1]["messages"][0]["content"][0]["text"]
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
        # budget exhausted -> escalate, never silent fail
        assert states["RemediationChoice"]["Default"] == "EscalateFail"
        assert states["EscalateFail"]["Next"] == "Fail"

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

    def test_teardown_always_follows_smoke_even_on_failure(self, asl):
        smoke = asl["States"]["SmokeTest"]
        assert smoke["Next"] == "Teardown"
        assert smoke["Catch"][0]["Next"] == "Teardown"  # endpoint never orphaned
