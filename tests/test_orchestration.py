"""Unit tests for the orchestration spine — no AWS calls, all clients injected.

Covers: contracts (events, normalize, report), the harness driver's full
inline-function loop (stage_complete verify/reject, job_launched release,
escalate, re-ask, stream salvage), start/resume/webhook Lambdas, and the
state machine document (remediation loop wiring, event vocabulary, token
plumbing).

Run: .venv/bin/python -m pytest tests/test_orchestration.py -q
"""
from __future__ import annotations

import ast
import datetime
import fnmatch
import hashlib
import importlib.util
import inspect
import io
import json
import math
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

    def delete_item(self, **kw):
        # Deleting a nonexistent item is a no-op in DynamoDB, and the same here --
        # _settle_liveness leans on exactly that property.
        key = kw.get("Key") or {}
        self.items[:] = [i for i in self.items
                         if not all(i.get(k) == v for k, v in key.items())]
        return {}


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


def tool_use_stream_ending_in_prose(name, args, text="and that completes the task."):
    """A real shape from production: the agent calls an inline function AND narrates,
    and the runtime stops the message with `end_turn` rather than `tool_use`.

    Observed on run-20260810T174626Z-3f08b4c6, whose data-prep agent wrote 300 rows
    to S3 and called stage_complete in a turn shaped exactly like this. The driver read
    only the stop_reason, decided the block had already been serviced inside the
    harness, and counted the turn as prose -- three times, then failed the stage
    MissingStageComplete with the outputs sitting in the bucket.
    """
    return [
        {"contentBlockStart": {"start": {"toolUse": {"toolUseId": "tu-1", "name": name}}}},
        {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(args)}}}},
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


class FakeAgentCoreControl:
    """AgentCore control plane, with the property that matters: state PERSISTS.

    The failover bug was a write to shared, durable control-plane state, so a double
    that forgot each UpdateHarness could not express it. `model_of` reads back what was
    written, exactly as a later run's GetHarness would.
    """

    def __init__(self, model_id="global.anthropic.claude-fable-5", status="READY",
                 fail_update_on=()):
        self.models = {}
        self.default_model = model_id
        self.status = status
        self.updates = []
        #: model ids whose UpdateHarness raises — the restore-failed path.
        self.fail_update_on = set(fail_update_on)

    def model_of(self, harness_id):
        return self.models.get(harness_id, self.default_model)

    def get_harness(self, harnessId, **kw):
        return {"harness": {
            "status": self.status,
            "model": {"bedrockModelConfig": {"modelId": self.model_of(harnessId)}}}}

    def update_harness(self, harnessId, model, **kw):
        new = model["bedrockModelConfig"]["modelId"]
        if new in self.fail_update_on:
            raise RuntimeError(f"ValidationException: cannot set {new}")
        self.updates.append((harnessId, new))
        self.models[harnessId] = new
        return {"harness": {"status": self.status}}


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

    # --- #27: outputs spelled as a JSON string skipped verification entirely ---

    def test_outputs_sent_as_a_json_string_are_still_verified(self):
        """The whole trust-but-verify mechanism, defeated by a type.

        `verify_outputs` head_objects every element that `startswith("s3://")`. A
        one-element list holding the TEXT '["s3://.../a", "s3://.../b"]' starts with '[',
        so every URI inside it was skipped and the stage passed verification having proved
        nothing -- an agent could claim outputs that do not exist and be believed, which is
        the one thing the head_object exists to stop.

        `metrics` had had the JSON-string parse since the contract was written; `outputs`
        never did, and outputs is the field with a security consequence. Measured live on
        rehearsal run-20260811T005043Z-320cc47e, whose data-prep entry recorded
        outputs=["[\\"s3://...generated.jsonl\\", \\"s3://...manifest.json\\"]"]. Those two
        objects did exist, so the run was honest -- which is exactly why this was invisible
        for as long as the agents were.

        Asserted through verify_outputs rather than on the normalized list alone: the shape
        is only wrong because of what the CALLER then fails to check, and a test that stops
        at the list would keep passing if verify_outputs' skip rule changed underneath it.
        """
        uris = ["s3://b/runs/r/a.jsonl", "s3://b/runs/r/b.json"]
        norm = normalize_stage_complete({"outputs": json.dumps(uris)})
        assert norm["outputs"] == uris

        class NothingExists:
            def head_object(self, Bucket, Key):
                raise RuntimeError("404 NoSuchKey")

        assert driver.verify_outputs(NothingExists(), norm["outputs"]) == uris

    def test_a_json_quoted_single_uri_is_unwrapped_before_verification(self):
        """'"s3://b/x"' must not keep the quote in front of the scheme.

        The identical vacuous check one layer down: a leading '"' also fails
        startswith("s3://"), so unwrapping only the list case would leave the scalar case
        unverifiable. A bare unquoted URI is not valid JSON and must survive untouched.
        """
        assert normalize_stage_complete({"outputs": '"s3://b/x"'})["outputs"] == ["s3://b/x"]
        assert normalize_stage_complete({"outputs": "s3://b/x"})["outputs"] == ["s3://b/x"]

    def test_a_non_json_string_output_is_kept_verbatim(self):
        """Parsing must not eat a claim it cannot understand.

        An agent naming a local path or writing prose into `outputs` is making a claim that
        belongs in the report where a human can see it is not an s3:// URI. Turning it into
        [] would delete the evidence that the stage misreported.
        """
        assert normalize_stage_complete({"outputs": "not json"})["outputs"] == ["not json"]
        assert normalize_stage_complete({"outputs": "[]"})["outputs"] == []

    def test_a_dict_output_is_recorded_as_text_not_mined_for_uris(self):
        """The boundary of the fix, asserted so it is a decision rather than an oversight.

        A dict stays unverified -- but that is verify_outputs' documented rule for any
        non-s3:// element, not this bug. Guessing which of its values are artifacts would
        invent a claim the agent never made, and an invented claim that then PASSES
        head_object is worse than a legible one that is never checked.
        """
        norm = normalize_stage_complete({"outputs": {"uri": "s3://b/x"}})
        assert norm["outputs"] == ["{'uri': 's3://b/x'}"]

    def test_report_counts_and_findings(self):
        manifest = {"run_id": "r1", "stages": {
            "data-prep": {"status": "completed"},
            "eval": {"status": "failed", "evidence": "gate 0.6 < 0.8"}}}
        report = build_run_report(manifest)
        assert report["pass_counts"] == {"total": 2, "passed": 1, "failed": 1,
                                        "in_flight": 0, "unrecognized": 0}
        assert report["findings"][0]["stage"] == "eval"

    def test_a_report_counts_the_stages_the_agents_wrote_not_only_the_drivers(self):
        """r5's report said 3 of 14 passed for a run in which every stage succeeded.

        `manifest["stages"]` has TWO writers. The driver writes `status: "completed"` under
        the bare stage name; the specialist agents are told to append their own results to
        the same manifest and they write `"complete"` / `"launched"` under
        `"<stage>.<task>[.i<n>]"`. Only the driver's spelling was counted, so on
        run-20260811T101948Z-f9d34d27 the published pass_counts were
        {"total": 14, "passed": 3, "failed": 0} against an actual mix of 3 "completed",
        7 "complete" and 4 "launched" -- 11 stages counted as nothing at all, and the
        console renders that as a run that mostly did not pass.

        The exact r5 mix is reproduced here rather than a synthetic one, so this test fails
        against the code that published that number.
        """
        stages = {}
        for i in range(3):
            stages[f"driver-{i}"] = {"status": "completed"}
        for i in range(7):
            stages[f"agent.task.i{i}"] = {"status": "complete"}
        for i in range(4):
            stages[f"agent.launch.i{i}"] = {"status": "launched"}
        counts = build_run_report({"run_id": "r5", "stages": stages})["pass_counts"]
        assert counts["total"] == 14
        assert counts["passed"] == 10, (
            f"3 driver + 7 agent completions is 10 finished stages, report says "
            f"{counts['passed']}")
        assert counts["in_flight"] == 4, (
            "a launched training job has not passed and has not failed; counting it either "
            "way states an outcome that does not exist yet")
        assert counts["failed"] == 0

    def test_a_status_neither_writer_uses_is_reported_not_absorbed(self):
        """The gap between `total` and the sub-counts is what hid the bug.

        With only `passed` and `failed` published, 11 unreadable statuses were
        indistinguishable from 11 genuinely-not-passing stages. The four sub-counts must
        reconcile to `total`, so the next vocabulary drift shows up as a number instead of
        as a silently pessimistic pass rate.
        """
        counts = build_run_report({"run_id": "r", "stages": {
            "a": {"status": "completed"}, "b": {"status": "finished-ish"},
            "c": {}}})["pass_counts"]
        assert counts["unrecognized"] == 2, (
            f"'finished-ish' and a status-less entry are both unreadable: {counts!r}")
        assert (counts["passed"] + counts["failed"] + counts["in_flight"]
                + counts["unrecognized"] == counts["total"]), counts

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
        # DERIVED from the payload _self_reinvoke actually sends, rather than blacklisting
        # the name of the one key that got this wrong. The blacklist version searched the
        # whole body for "_resumed" after slicing it at a COMMENT string, so it reddened on
        # the day a comment explained the bug (D12 added two handoff keys and said why the
        # old one meant nothing) and would never have caught a THIRD key by another name.
        # What the guard is for is the contract: every key a reinvoke sends is a key the
        # resume path reads, or the next invocation silently drops it.
        rein = body[body.index("def _self_reinvoke"):]
        rein = rein[:rein.index("\n\n")]
        sent = set(re.findall(r'"(_[a-z_]+)":', rein))
        assert len(sent) >= 3, f"the handoff payload shape moved: {sent}"
        for key in sorted(sent):
            assert f'event.get("{key}"' in body, (
                f"_self_reinvoke sends {key!r} and nothing reads it back off the event, "
                "so the resumed invocation drops it -- the _resumed bug, again")
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

    # ---- the escalation must reach the run's own report (#D3c) ------------------
    #
    # build_run_report raises a critical finding for a stage whose status is "escalated",
    # and NOTHING wrote that status into manifest["stages"]. handle_escalate's durable
    # record was runs.status plus a DDB stage event, neither of which the report reads. So
    # r5 (run-20260811T101948Z-f9d34d27) escalated to a human at its iteration-1 gate and
    # published "findings": [] -- the one run that needed a person to look at it produced
    # the report of a run with nothing to say.

    URI = "s3://llmops-data-test/runs/run-real-1/manifest.json"

    @classmethod
    def _seeded(cls, manifest):
        s3 = FakeS3()
        s3.objects[cls.URI] = json.dumps(manifest).encode()
        return s3

    def test_an_escalation_becomes_a_finding_in_the_runs_own_report(self):
        """The report is what a human opens; the escalation has to be in it."""
        s3 = self._seeded({"run_id": "run-real-1", "stages": {
            "eval": {"status": "completed", "metrics": {"judge_win_rate": 0.0}}}})
        c = clients(s3=s3)
        out = driver.handle_escalate(
            c, driver_event(run_id="run-real-1", stage="eval", task="gate", iteration=1,
                            manifest_uri=self.URI),
            {"reason": "judge_win_rate 0.0 is below the 0.55 bar and iteration 1 did not "
                       "move it"})
        assert out == {"escalated": True}
        saved = json.loads(s3.objects[self.URI])
        assert saved.get("escalations"), (
            "the escalation left no record the report can read: the DDB stage event and "
            "runs.status are both invisible to build_run_report")
        report = build_run_report(saved)
        crit = [f for f in report["findings"] if f["severity"] == "critical"]
        assert len(crit) == 1, f"escalated run, findings={report['findings']!r}"
        assert crit[0]["stage"] == "eval"
        assert "0.55 bar" in crit[0]["detail"], (
            "a finding that does not carry the reason sends the reader back to their email")
        assert report["escalations"][0]["iteration"] == 1, (
            "which iteration called the human is the whole question in a remediation loop")

    def test_an_escalation_does_not_overwrite_the_stage_it_escalated_from(self):
        """Recorded as an append, not as stages[stage]["status"] = "escalated".

        The escalating stage usually HAS a completed entry -- r5's `eval` holds the scoring
        task's judge counts -- so writing the status onto it would trade a missing finding
        for a destroyed measurement. This is also why the escalation path passes
        `manifest=None`: it has no stage results, so it must not be able to restate any.
        """
        s3 = self._seeded({"run_id": "run-real-1", "stages": {
            "eval": {"status": "completed", "metrics": {"judge_win_rate": 0.0},
                     "evidence": "40-row acceptance set"}}})
        driver.handle_escalate(clients(s3=s3),
                              driver_event(run_id="run-real-1", stage="eval", task="gate",
                                           manifest_uri=self.URI),
                              {"reason": "below bar"})
        saved = json.loads(s3.objects[self.URI])
        assert saved["stages"]["eval"] == {
            "status": "completed", "metrics": {"judge_win_rate": 0.0},
            "evidence": "40-row acceptance set"}, (
            f"the escalation rewrote the stage's measured result: {saved['stages']['eval']!r}")

    def test_a_triage_does_not_file_its_escalation_in_the_subject_run(self):
        """A triage's manifest_uri names the SUBJECT run while its run_id is the
        conductor's own, so "write into event['manifest_uri']" would file the conductor's
        escalation inside somebody else's run record, under a run_id that names neither.

        The guard is derived from the URI rather than from _is_triage, so a future non-run
        caller cannot start doing this just by not being named in a list.
        """
        s3 = self._seeded({"run_id": "run-real-1", "stages": {}})
        driver.handle_escalate(
            clients(s3=s3),
            driver_event(run_id="triage-abc", stage="conductor", task="triage",
                         task_token="", manifest_uri=self.URI),
            {"reason": "cannot resolve run-real-1"})
        saved = json.loads(s3.objects[self.URI])
        assert "escalations" not in saved, (
            f"a triage wrote into the subject run's manifest: {saved.get('escalations')!r}")

    def test_an_unwritable_manifest_does_not_withhold_the_escalation(self):
        """Same law as every other channel here: bookkeeping cannot silence the alert.

        The manifest write is the newest of the four records and the only one that touches
        S3, so it is the one most likely to fail on an IAM change -- exactly how #25 turned
        one refused PutObject into a second, permitted artifact's disappearance.
        """
        class _Refuses(FakeS3):
            def get_object(self, Bucket, Key):
                raise RuntimeError("AccessDenied")

        c = clients(s3=_Refuses())
        out = driver.handle_escalate(
            c, driver_event(run_id="run-real-1", manifest_uri=self.URI),
            {"reason": "budget exhausted"})
        assert out == {"escalated": True}
        assert c["sns"].published, "the alert itself was withheld"
        assert c["sfn"].failures[0]["error"] == "EscalatedToHuman", \
            "the task token was left parked, which is the zombie #52 exists to prevent"
        assert any(i["sk"].endswith("#escalated")
                   for i in c["ddb"].Table(ENV["EVENTS_TABLE"]).items), \
            "the DDB trace went down with the S3 write"

    # ---- one dead escalation channel must not close the others -----------------
    #
    # The channels are independent by design and the ordering used to say otherwise:
    # SNS was the FIRST statement in handle_escalate and unwrapped, so a failed publish
    # took the stage event, the bus event and the task-token settle with it. When that was
    # found, SNS was also the channel with a known-zero audience -- llmops-escalations had
    # no subscribers, which ensure_topic reports rather than papering over, because a
    # deploy cannot invent an address. The one channel that reached nobody was gating the
    # two that worked.
    #
    # llmops-escalations now HAS a confirmed subscriber (measured 2026-08-10: 1 email
    # subscription; 15 published / 11 delivered / 0 failed over 2026-07-29..08-08, the 4
    # undelivered all predating the 08-02 confirmation). This test does not weaken: the
    # reason to wrap the publish was never "nobody is listening", it was that a
    # notification must not be able to withhold a state transition. A live channel still
    # fails on a throttle or an IAM change, and this is what pins that behaviour.

    def test_a_dead_sns_topic_does_not_take_the_whole_escalation_with_it(self):
        """A publish can fail outright (topic deleted, throttle, IAM drift) whether or not
        anyone is subscribed. The verdict still has to reach the conductor on the bus, the
        timeline still has to show it, and the token still has to settle."""
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
        nothing read: a verdict into the void, the same shape the escalation SNS topic
        had while it was still unsubscribed.

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

    def test_a_verdict_is_delivered_to_the_escalated_run_the_agent_did_not_name(self):
        """resolve_escalation is addressed by the invocation, exactly like page_human.

        `run_id` IS in resolve_escalation's required list, which is why this looked safe
        and is not: required is a request, not an enforcement, and a model that omits it
        used to take the branch through `if subject:` -- skipping put_directive AND the
        reachability check -- and still return {"status": "resolved"}. `resolved` is in
        TRIAGE_ANSWERED, so #72's backstop stayed quiet as well: an escalation that was
        never answered, reported as answered, with the only record filed under the
        conductor."""
        subject = "run-live-7"
        ac = FakeAgentCore([
            tool_use_stream("resolve_escalation",   # no run_id at all
                            {"decision": "option_B",
                             "rationale": "cap raised",
                             "adjusted_params": {"teacher_cap_usd": 39}}),
            text_stream("ack")])
        c = clients(ac)
        self._seed_run(c, subject, "running")
        out = driver.handler(driver.triage_event_from_bus(
            {"detail": {"run_id": subject, "stage": "eval", "reason": "gate failed"}},
            "bkt"), clients=c)
        assert out["status"] == "resolved"
        assert out["run_id"] == subject, \
            f"the driver reported a resolution against {out['run_id']}"
        pending = driver.take_directive(c["ddb"], subject)
        assert pending and pending["adjusted_params"] == {"teacher_cap_usd": 39}, \
            "the verdict never reached the escalated run's mailbox"
        assert any("EscalationResolved" in str(r.get("sk", "")) and r["run_id"] == subject
                   for r in c["ddb"].Table("llmops-stage-events").items), \
            "the audit row was filed against the conductor, not the stuck run"

    def test_a_resolve_naming_no_run_is_rejected_not_reported_resolved(self):
        """With no subject there is no mailbox, so the call cannot resolve anything.

        Rejected into the same turn (so the conductor can still page) rather than skipped
        with a success status -- the shape that let a triage end having done nothing while
        every layer of the guard reported health."""
        ac = FakeAgentCore([
            tool_use_stream("resolve_escalation",
                            {"decision": "retry", "rationale": "transient"}),
            tool_use_stream("page_human", {"situation": "s", "recommendation": "r"}),
            text_stream("ack")])
        c = clients(ac)
        out = driver.handler(driver_event(stage="orchestrator", task="triage",
                                          run_id="triage-orphan-2",
                                          harness_id="llmops_orchestrator",
                                          task_token=None), clients=c)
        first = json.loads(
            ac.calls[1]["messages"][-1]["content"][0]["toolResult"]["content"][0]["text"])
        assert first["status"] == "rejected", \
            f"a subject-less resolve was accepted: {first}"
        assert "page_human" in first["reason"], \
            "the rejection must name an exit that can still work"
        assert out["status"] == "paged", "the conductor's follow-up page never went out"
        assert not [r for r in c["ddb"].Table("llmops-stage-events").items
                    if str(r.get("sk", "")).startswith(driver.DIRECTIVE_SK)], \
            "a directive was parked with no run to deliver it to"

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
        # task_token=None because a triage HAS none -- triage_event_from_bus builds the
        # only real triage invocation and omits the key entirely (asserted below in
        # test_a_triage_invocation_carries_no_task_token). It matters to this test: a page
        # from an invocation that IS holding a token must not end the turn, since nothing
        # would settle it, so the default fixture token would send this down the stage path.
        out = driver.handler(driver_event(stage="orchestrator", task="triage",
                                         run_id="run-orch-1", task_token=None), clients=c)
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

    #: Every way a conductor can name (or fail to name) the subject in its page_human
    #: args. The subject is a property of the INVOCATION, so all four must land on the
    #: same run -- the test above only ever exercised the first, which is why the
    #: fallback survived: it passes on main too.
    PAGE_ARG_SHAPES = [
        ("names the subject", {"run_id": "run-subject-3"}),
        # run_id is NOT in page_human's required list (agents/orchestrator/harness.json
        # requires situation + recommendation only), so this is schema-legal.
        ("omits run_id", {}),
        # What a conductor invoked as `triage-run-subject-3` naturally echoes back.
        ("echoes the triage id", {"run_id": "triage-run-subject-3"}),
        ("names an unrelated run", {"run_id": "run-someone-else"}),
    ]

    @pytest.mark.parametrize("label,extra", PAGE_ARG_SHAPES,
                             ids=[s[0] for s in PAGE_ARG_SHAPES])
    def test_a_bus_triage_page_is_addressed_by_the_event_not_the_agent(self, label, extra):
        """The page goes to the run the ESCALATION named, whatever the agent says.

        Live, measured over every HumanPaged row in llmops-stage-events (12 rows, full
        scan): 3 were filed under a `triage-` id -- 86ab8a14, c8b13faa and b56281da, all
        ARC-2 lineage runs that died with their scientific work complete. The owner got
        the email and then found nothing on the stuck run's timeline, because the row was
        in the conductor's. `subject_run = args.get("run_id") or event.get("run_id")`
        resolved to `triage-<subject>` whenever the model omitted the field or echoed the
        id it was invoked under, while the comment directly above it claimed to share
        put_directive's addressing rule -- which has no such fallback.
        """
        subject = "run-subject-3"
        ac = FakeAgentCore([
            tool_use_stream("page_human",
                            dict(extra, situation="s", recommendation="r")),
            text_stream("ack")])
        c = clients(ac)
        out = driver.handler(driver.triage_event_from_bus(
            {"detail": {"run_id": subject, "stage": "eval", "reason": "gate failed"}},
            "bkt"), clients=c)

        assert out["status"] == "paged"
        assert out["run_id"] == subject
        paged = [r for r in c["ddb"].Table("llmops-stage-events").items
                 if "HumanPaged" in str(r.get("sk", ""))]
        assert len(paged) == 1
        assert paged[0]["run_id"] == subject, \
            f"the page ({label}) was filed under {paged[0]['run_id']}, not the stuck run"
        brief = json.loads(c["sns"].published[0]["Message"])
        assert brief["run_id"] == subject, "the owner's brief names the wrong run"
        # The triage's own id is still recorded -- it is how a reader gets from the page
        # to the conductor's reasoning. It is just not the ADDRESS.
        assert brief["triaging_run_id"] == f"triage-{subject}"
        emitted = [e for e in c["events"].entries
                   if e["DetailType"] == ev.OWNER_PAGED]
        assert emitted and json.loads(emitted[0]["Detail"])["run_id"] == subject, \
            "OwnerPaged carries the triage id, so a bus consumer sees the wrong run"

    def test_a_console_page_still_uses_the_invoking_run(self):
        """The event wins, but only when the event HAS a subject.

        The console's chat path invokes the conductor with no escalation envelope, so
        there `event["run_id"]` IS the run being discussed. A fix that keyed only on
        params.escalation.run_id would file every console page under an empty key --
        a DynamoDB ValidationException on the partition key, i.e. a crash on the
        notification path, which is the class of failure #72 closed."""
        ac = FakeAgentCore([
            tool_use_stream("page_human", {"situation": "s", "recommendation": "r"}),
            text_stream("ack")])
        c = clients(ac)
        out = driver.handler(driver_event(stage="orchestrator", task="plan",
                                          run_id="run-console-4",
                                          harness_id="llmops_orchestrator",
                                          task_token=None), clients=c)
        paged = [r for r in c["ddb"].Table("llmops-stage-events").items
                 if "HumanPaged" in str(r.get("sk", ""))]
        assert paged and paged[0]["run_id"] == "run-console-4", \
            f"a page with no escalation envelope was misfiled: {paged}"
        assert out.get("run_id") == "run-console-4"

    def test_a_page_with_no_derivable_subject_is_still_recorded(self):
        """A hand-built triage carrying neither an escalation nor a usable arg.

        Unreachable from the bus -- triage_event_from_bus raises on an escalation with no
        run_id -- but the row must not be written with an empty partition key, because a
        ValidationException here would turn a page that was ALREADY published into a
        crashed invocation, and #72 exists to stop bookkeeping from swallowing an alert.
        Filed under the triaging run and marked as subject-less in the brief, which is
        what distinguishes it from the mis-addressed rows: those carried the triage id in
        both fields."""
        ac = FakeAgentCore([
            tool_use_stream("page_human", {"situation": "s", "recommendation": "r"}),
            text_stream("ack")])
        c = clients(ac)
        driver.handler(driver_event(stage="orchestrator", task="triage",
                                    run_id="triage-orphan", harness_id="llmops_orchestrator",
                                    task_token=None), clients=c)
        paged = [r for r in c["ddb"].Table("llmops-stage-events").items
                 if "HumanPaged" in str(r.get("sk", ""))]
        assert paged, "the page was published but left no record at all"
        assert paged[0]["run_id"] == "triage-orphan"
        assert json.loads(c["sns"].published[0]["Message"])["run_id"] == "", \
            "an unaddressed page must not claim a subject it does not have"

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
                                         run_id="run-orch-1", task_token=None), clients=c)
        first = json.loads(
            ac.calls[1]["messages"][-1]["content"][0]["toolResult"]["content"][0]["text"])
        assert first["status"] == "rejected"
        assert "recommendation" in first["reason"]
        assert out["status"] == "paged", "the corrected page never went out"
        assert len(c["sns"].published) == 1, \
            "the incomplete page was sent anyway, then sent again"

    # --- D12: a blocked run could be answered, or noticed, never both ----------
    #
    # escalate_human notified (SNS + bus + stage event) and made the run UNANSWERABLE:
    # `escalated` is in UNREACHABLE_RUN_STATES, the token settles with
    # error="EscalatedToHuman", and EvalGate's Catch drives the execution to FAILED.
    # checkpoint kept the run answerable (take_directive) and notified NOBODY. So the eval
    # agent's borderline verdict -- the ONE gate outcome a human answer can actually
    # unblock -- had no channel: the prompt sent it to escalate_human, which destroys the
    # run the answer was for. page_human is notify-without-ending, and these pin the half
    # that is easy to get wrong: a page must NOT end a turn that is holding a task token.

    def test_a_triage_invocation_carries_no_task_token(self):
        """The discriminator the page branch keys on, pinned to the only builder of a real
        triage invocation. An EscalatedToHuman event has no token to forward -- the state
        machine already failed the one it had -- so `event.get("task_token")` separates
        "the conductor decided to page and is done" from "a stage asked a question and is
        still holding the state machine open". Two page tests above pass task_token=None
        for this reason and forward-reference this assertion."""
        built = driver.triage_event_from_bus(
            {"detail-type": ev.ESCALATED_TO_HUMAN,
             "detail": {"run_id": "run-stuck-9", "stage": "eval", "reason": "gate failed"}},
            "llmops-data-test")
        assert "task_token" not in built, (
            f"a bus triage now carries a task_token ({sorted(built)}): every triage page "
            "just stopped being terminal-for-the-turn")

    def test_a_stage_page_does_not_end_a_turn_that_is_holding_a_task_token(self):
        """The second-order defect, and the expensive one.

        _ack_terminal does NOT settle the task token -- it acks the tool call and closes
        the session -- so returning after a page would leave EvalGate waiting on a token
        no live driver will ever settle. TimeoutSeconds is 86400, so an eval agent that
        successfully asked its question would hang the run for 24h and then fail it: the
        worst possible outcome of doing exactly what the new prompt asks for.
        """
        ac = FakeAgentCore([
            tool_use_stream("page_human",
                            {"situation": "judge_score 0.46, CI [0.40, 0.53], n=97, bar 0.45",
                             "options": ["accept", "collect more items", "re-train"],
                             "recommendation": "collect more items"}),
            tool_use_stream("checkpoint", {"next_action": "wait for the verdict"}),
            tool_use_stream("stage_complete", {"outputs": []}),
            text_stream("ack")])
        c = clients(ac)
        out = driver.handler(driver_event(stage="eval", task="gate",
                                         harness_id="llmops_eval"), clients=c)
        assert c["sns"].published, "the eval agent's question reached no human"
        assert out["status"] == "completed", (
            f"a page ended the turn while holding a task token ({out!r}); EvalGate would "
            "wait 86400s for a token nothing is left alive to settle")
        assert len(c["sfn"].successes) == 1 and not c["sfn"].failures, (
            f"the task token was not settled exactly once: {c['sfn'].successes!r} "
            f"{c['sfn'].failures!r}")
        answer = json.loads(
            ac.calls[1]["messages"][-1]["content"][0]["toolResult"]["content"][0]["text"])
        assert answer["status"] == "paged", f"{answer!r}"
        # The agent has to be TOLD the page did not pause anything, or it reasonably
        # assumes it is now waiting and ends its turn -- which is the same hang.
        assert "checkpoint" in answer["next"], (
            f"the page result never names the call that receives the answer: {answer!r}")
        assert "escalate_human" in answer["next"], (
            "the result does not warn that escalating to wait makes the answer "
            f"undeliverable: {answer!r}")

    def test_a_stage_page_is_filed_on_its_own_run_under_the_stage_that_asked(self):
        """A page is only visible if it lands where the console looks. For a stage there is
        no escalation envelope and no separate subject: the run it is about is the run it
        was invoked for, which is why page_human is declared on the eval harness WITHOUT a
        run_id field."""
        ac = FakeAgentCore([
            tool_use_stream("page_human", {"situation": "borderline", "recommendation": "wait"}),
            tool_use_stream("stage_complete", {"outputs": []}),
            text_stream("ack")])
        c = clients(ac)
        driver.handler(driver_event(stage="eval", task="gate", harness_id="llmops_eval"),
                       clients=c)
        paged = [r for r in c["ddb"].Table("llmops-stage-events").items
                 if str(r.get("sk", "")).endswith("#" + driver.PAGE_EVENT)]
        assert len(paged) == 1, f"{paged!r}"
        assert paged[0]["run_id"] == "run-test-1", f"filed on {paged[0]['run_id']!r}"
        assert "#eval#" in str(paged[0]["sk"]), (
            f"the row does not say which stage asked: {paged[0]['sk']!r}")
        brief = json.loads(paged[0]["detail"])
        assert brief["paged_by"] == "eval-gate", (
            f"the brief credits the page to {brief['paged_by']!r}; the console prints this")
        emitted = [e for e in c["events"].entries if e["DetailType"] == ev.OWNER_PAGED]
        assert emitted and json.loads(emitted[0]["Detail"])["stage"] == "eval", (
            f"OwnerPaged does not carry the paging stage: {emitted!r}")

    def test_the_triage_label_is_unchanged_by_being_derived(self):
        """`paged_by` was the literal "orchestrator-triage". Deriving it from the invocation
        has to REPRODUCE that on the triage path, or every stored page's label and the new
        ones stop being comparable -- and the 12 live HumanPaged rows are the evidence base
        for the addressing fix above."""
        ac = FakeAgentCore([
            tool_use_stream("page_human", {"run_id": "run-stuck-9", "situation": "s",
                                           "recommendation": "r"}),
            text_stream("ack")])
        c = clients(ac)
        driver.handler(driver_event(stage="orchestrator", task="triage",
                                    run_id="run-orch-1", task_token=None), clients=c)
        paged = [r for r in c["ddb"].Table("llmops-stage-events").items
                 if str(r.get("sk", "")).endswith("#" + driver.PAGE_EVENT)]
        assert json.loads(paged[0]["detail"])["paged_by"] == "orchestrator-triage", (
            f"the derivation changed the label the old code hardcoded: {paged[0]['detail']}")

    def test_a_checkpoint_after_a_page_records_that_the_run_is_waiting(self):
        """The row that did not exist. checkpoint wrote nothing anywhere, so a stage waiting
        for a human and a stage doing its work were byte-identical to every reader -- and
        the state whose whole purpose is to get attention was the one the console could not
        show. One row per waiting turn, because an operator needs not only THAT it is
        waiting but how long it has been paying model tokens to wait."""
        ac = FakeAgentCore([
            tool_use_stream("page_human", {"situation": "borderline", "recommendation": "wait"}),
            tool_use_stream("checkpoint", {"next_action": "waiting"}),
            tool_use_stream("checkpoint", {"next_action": "still waiting"}),
            tool_use_stream("checkpoint", {"next_action": "still waiting"}),
            tool_use_stream("stage_complete", {"outputs": []}),
            text_stream("ack")])
        c = clients(ac)
        driver.handler(driver_event(stage="eval", task="gate", harness_id="llmops_eval"),
                       clients=c)
        waits = [r for r in c["ddb"].Table("llmops-stage-events").items
                 if str(r.get("sk", "")).endswith("#" + driver.WAIT_EVENT)]
        assert len(waits) == 3, f"three waiting checkpoints left {len(waits)} rows"
        turns = sorted(json.loads(r["detail"])["waiting_turn"] for r in waits)
        assert turns == [1, 2, 3], f"the turn counter does not advance: {turns}"
        detail = json.loads(waits[0]["detail"])
        assert detail["paged_at"], "a waiting row that does not say which page it waits on"
        assert detail["task"] == "gate", f"{detail!r}"
        assert "#eval#" in str(waits[0]["sk"]), f"{waits[0]['sk']!r}"

    def test_a_checkpoint_with_no_page_behind_it_records_nothing(self):
        """The common case stays free of ceremony: a working agent checkpoints for another
        turn, and a row per checkpoint would drown the timeline an operator opens to find
        out what happened. Waiting is a property of having ASKED."""
        ac = FakeAgentCore([
            tool_use_stream("checkpoint", {"next_action": "keep going"}),
            tool_use_stream("checkpoint", {"next_action": "keep going"}),
            tool_use_stream("stage_complete", {"outputs": []}),
            text_stream("ack")])
        c = clients(ac)
        driver.handler(driver_event(stage="eval", task="gate", harness_id="llmops_eval"),
                       clients=c)
        assert not [r for r in c["ddb"].Table("llmops-stage-events").items
                    if str(r.get("sk", "")).endswith("#" + driver.WAIT_EVENT)], \
            "a run that never asked anybody anything is recorded as waiting on them"

    def test_the_answer_arriving_ends_the_wait(self):
        """A directive delivered in a checkpoint result IS the answer, so the marking stops
        -- otherwise a run reads as waiting forever after being unblocked, and the next
        page inherits this one's turn count instead of starting its own."""
        ac = FakeAgentCore([
            tool_use_stream("page_human", {"situation": "borderline", "recommendation": "wait"}),
            tool_use_stream("checkpoint", {"next_action": "waiting"}),
            tool_use_stream("checkpoint", {"next_action": "acting on the verdict"}),
            tool_use_stream("stage_complete", {"outputs": []}),
            text_stream("ack")])
        c = clients(ac)
        driver.put_directive(c["ddb"], "run-test-1", decision="accept_at_this_score",
                             rationale="0.46 with n=97 is good enough for a pilot",
                             actor="tmwu")
        driver.handler(driver_event(stage="eval", task="gate", harness_id="llmops_eval"),
                       clients=c)
        answer = json.loads(
            ac.calls[2]["messages"][-1]["content"][0]["toolResult"]["content"][0]["text"])
        assert answer["status"] == "directive", (
            f"the paged agent's checkpoint did not deliver the parked verdict: {answer!r}")
        assert not [r for r in c["ddb"].Table("llmops-stage-events").items
                    if str(r.get("sk", "")).endswith("#" + driver.WAIT_EVENT)], \
            "the run is still being recorded as waiting after its answer was delivered"

    def test_the_wait_survives_the_lambda_boundary_it_will_certainly_cross(self):
        """A human answer takes minutes at best; one invocation is 900s. So a waiting run
        crosses Lambda boundaries by construction, and if the marker restarted at each one
        the turn count would reset to 1 forever -- exactly the number that makes a long
        wait look like a fresh one. Behavioural, through the continuation the reinvoke
        sends, rather than a source string: `_resumed` is the cautionary tale for keys that
        are written and never read."""
        ac = FakeAgentCore([
            tool_use_stream("checkpoint", {"next_action": "still waiting"}),
            tool_use_stream("stage_complete", {"outputs": []}),
            text_stream("ack")])
        c = clients(ac)
        driver.handler(driver_event(
            stage="eval", task="gate", harness_id="llmops_eval",
            _continuation=[{"role": "user", "content": [{"text": "carry on"}]}],
            _paged_at="2026-08-09T10:00:00Z", _wait_turns=4), clients=c)
        waits = [r for r in c["ddb"].Table("llmops-stage-events").items
                 if str(r.get("sk", "")).endswith("#" + driver.WAIT_EVENT)]
        assert len(waits) == 1, f"{waits!r}"
        detail = json.loads(waits[0]["detail"])
        assert detail["waiting_turn"] == 5, (
            f"the wait restarted across the boundary: turn {detail['waiting_turn']}, not 5")
        assert detail["paged_at"] == "2026-08-09T10:00:00Z", (
            f"the resumed invocation lost which page it is waiting on: {detail!r}")

    def test_a_fresh_start_does_not_inherit_a_wait_it_cannot_remember(self):
        """A resurrector wake or a state-machine re-entry re-sends the stage prompt, so the
        agent's own decision to wait is gone with the transcript: continuing to mark
        waiting turns would attribute them to an agent that no longer knows it asked
        anything. The page itself survives as a row in the run's timeline, which is what
        the console reads -- only the per-turn marker restarts."""
        ac = FakeAgentCore([
            tool_use_stream("checkpoint", {"next_action": "starting over"}),
            tool_use_stream("stage_complete", {"outputs": []}),
            text_stream("ack")])
        c = clients(ac)
        driver.handler(driver_event(stage="eval", task="gate", harness_id="llmops_eval",
                                    _paged_at="2026-08-09T10:00:00Z", _wait_turns=4),
                       clients=c)
        assert not [r for r in c["ddb"].Table("llmops-stage-events").items
                    if str(r.get("sk", "")).endswith("#" + driver.WAIT_EVENT)], \
            "a fresh invocation resumed a wait whose transcript it does not have"

    def test_the_waiting_rows_are_bounded_and_the_last_one_is_a_floor(self):
        """maxIterations is 100 and the eval prompt's 6-checkpoint cap is a request, not an
        enforcement: an agent that ignores it would write a row per turn into the timeline
        an operator opens to find out what happened. The rows stop at WAIT_ROW_CAP while
        the WAIT does not, which is why the console reads each row's own `waiting_turn`
        field (a floor) instead of counting rows (not even a floor)."""
        cap = driver.WAIT_ROW_CAP
        assert cap >= 12, f"the cap is below twice the prompt's 6 checkpoints: {cap}"
        ac = FakeAgentCore(
            [tool_use_stream("page_human", {"situation": "b", "recommendation": "wait"})]
            + [tool_use_stream("checkpoint", {"next_action": f"waiting {i}"})
               for i in range(cap + 3)]
            + [tool_use_stream("stage_complete", {"outputs": []}), text_stream("ack")])
        c = clients(ac)
        out = driver.handler(driver_event(stage="eval", task="gate",
                                         harness_id="llmops_eval"), clients=c)
        assert out["status"] == "completed", (
            f"the cap ended the stage instead of only the rows: {out!r}")
        waits = [json.loads(r["detail"])["waiting_turn"]
                 for r in c["ddb"].Table("llmops-stage-events").items
                 if str(r.get("sk", "")).endswith("#" + driver.WAIT_EVENT)]
        assert len(waits) == cap, f"{cap + 3} waiting checkpoints wrote {len(waits)} rows"
        assert max(waits) == cap, f"the newest row is not the floor the console prints: {waits}"

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

    # --- #72: an unanswered triage is the LAST line, not the first -------------
    #
    # The escalation bus has one rule with one target (this driver), and the state
    # machine's EscalateFail is a bare putEvents with no SNS on the path. So when a
    # triage ends without resolving, dispatching or paging, the owner is never told at
    # all. Measured 2026-08-05..08: 11 of 11 directives ever parked were undeliverable,
    # and 4 of the 9 triaged runs produced no HumanPaged event.

    def test_the_rejection_does_not_name_an_exit_that_cannot_work(self):
        """launch_run needs a KMS-verifiable approval record, from args["approval"] or
        from params.approval_context. A bus triage has NEITHER: nothing in the repo
        writes approval_context, and `approval` is not a declared property of launch_run
        in the orchestrator's harness, so the agent cannot supply one either.

        The rejection nonetheless told the conductor to "relaunch the work with
        launch_run" -- two doors, one painted on. Live: 4 of the 9 triaged escalations
        produced no page at all, the conductor having been sent to a tool that refuses.

        So when dispatch is impossible the rejection must name page_human ONLY."""
        ac = FakeAgentCore([
            tool_use_stream("resolve_escalation",
                            {"run_id": "run-dead-7", "decision": "raise_cap",
                             "rationale": "teacher cap too low"}),
            tool_use_stream("page_human", {"run_id": "run-dead-7", "situation": "s",
                                           "recommendation": "r"}),
            text_stream("ack")])
        c = clients(ac)
        self._seed_run(c, "run-dead-7", "failed")
        driver.handler(driver_event(stage="orchestrator", task="triage",
                                    run_id="run-orch-1"), clients=c)
        answer = json.loads(
            ac.calls[1]["messages"][-1]["content"][0]["toolResult"]["content"][0]["text"])
        assert answer["can_dispatch"] is False, \
            "a bus triage has no signed approval; launch_run cannot dispatch from it"
        assert "launch_run CANNOT" in answer["reason"], \
            "the rejection still sends the conductor to a tool that will refuse it"
        assert "page_human" in answer["reason"], \
            "the rejection must name the ONE exit that works"

    def test_the_rejection_still_offers_dispatch_when_dispatch_can_work(self):
        """The guard must not amputate the working case. An invocation that DOES carry a
        signed approval context can relaunch, and telling it to page a human instead
        would hand the owner a decision the conductor was authorized to make."""
        ac = FakeAgentCore([
            tool_use_stream("resolve_escalation",
                            {"run_id": "run-dead-8", "decision": "relaunch_stage",
                             "rationale": "retry with a smaller batch"}),
            tool_use_stream("page_human", {"run_id": "run-dead-8", "situation": "s",
                                           "recommendation": "r"}),
            text_stream("ack")])
        c = clients(ac)
        self._seed_run(c, "run-dead-8", "failed")
        driver.handler(driver_event(
            stage="orchestrator", task="triage", run_id="run-orch-1",
            params={"escalation": {"run_id": "run-dead-8"},
                    "approval_context": {"approval": {"plan_sha256": "abc",
                                                      "signature": "sig"}}}), clients=c)
        answer = json.loads(
            ac.calls[1]["messages"][-1]["content"][0]["toolResult"]["content"][0]["text"])
        assert answer["can_dispatch"] is True
        assert "relaunch the work with launch_run" in answer["reason"], \
            "an authorized conductor was denied the dispatch path it could have used"

    def test_dispatch_is_impossible_on_a_triage_built_from_the_bus(self):
        """Derived from the real emitter rather than asserted about a hand-built dict: the
        event triage_event_from_bus produces IS what every state-machine escalation
        becomes, and it carries no approval_context. If a future change starts seeding
        one, this test goes red and the rejection wording should follow it."""
        built = driver.triage_event_from_bus(
            {"detail-type": ev.ESCALATED_TO_HUMAN,
             "detail": {"run_id": "run-stuck-3", "stage": "eval", "reason": "gate failed"}},
            "llmops-data-test")
        assert driver.dispatch_is_possible(built) is False, \
            "a bus triage looks dispatchable but service_launch_run will refuse it"

    def test_a_triage_that_answers_nothing_still_reaches_the_owner(self):
        """The third and deepest layer. The conductor is not the FIRST line to a human on
        this path -- it is the ONLY one: `llmops-escalation-triage` is the bus's single
        rule, its single target is this Lambda, and EscalateFail is a bare putEvents with
        no SNS anywhere after it.

        So a triage that ends in prose after its re-asks tells NOBODY. Live: run
        c8b13faa, 86ab8a14 and b56281da each died with their scientific work complete,
        their run row reading `failed`, and zero HumanPaged events -- the only trace was
        a log stream."""
        ac = FakeAgentCore([text_stream("I have analyzed the failure."),
                            text_stream("Still analyzing."),
                            text_stream("My analysis is complete.")])
        c = clients(ac)
        out = driver.handler(driver_event(
            stage="orchestrator", task="triage", run_id="run-orch-1", task_token=None,
            params={"escalation": {"run_id": "run-stuck-4"}}), clients=c)
        assert c["sns"].published, \
            "a triage ended having decided nothing and the owner was never told"
        brief = json.loads(c["sns"].published[0]["Message"])
        assert brief["run_id"] == "run-stuck-4", \
            "the backstop page was filed against the conductor, not the stuck run"
        assert "backstop" in brief["situation"], \
            "the owner must know this page is the driver's, not the conductor's judgment"
        assert out.get("backstop_paged") is True

    def test_a_triage_that_did_its_job_is_not_paged_about(self):
        """The backstop must not page on every triage, or it becomes noise and the owner
        stops reading it -- which would recreate the silence it exists to break. A page
        already reaching the owner must not produce a second one."""
        ac = FakeAgentCore([
            tool_use_stream("page_human", {"run_id": "run-stuck-5", "situation": "s",
                                           "recommendation": "r"}),
            text_stream("ack")])
        c = clients(ac)
        out = driver.handler(driver_event(
            stage="orchestrator", task="triage", run_id="run-orch-1", task_token=None,
            params={"escalation": {"run_id": "run-stuck-5"}}), clients=c)
        assert out["status"] == "paged"
        assert len(c["sns"].published) == 1, \
            "the conductor paged and the backstop paged again about the same triage"
        assert "backstop_paged" not in out

    def test_a_delivered_verdict_is_not_paged_about_either(self):
        """A resolved escalation reached a listening agent; that IS the answer. Paging on
        top of it would tell the owner a working path had failed."""
        ac = FakeAgentCore([
            tool_use_stream("resolve_escalation",
                            {"run_id": "run-live-7", "decision": "option_B",
                             "rationale": "raise the cap"}),
            text_stream("ack")])
        c = clients(ac)
        self._seed_run(c, "run-live-7", "running")
        out = driver.handler(driver_event(
            stage="orchestrator", task="triage", run_id="run-orch-1", task_token=None,
            params={"escalation": {"run_id": "run-live-7"}}), clients=c)
        assert out["status"] == "resolved"
        assert not c["sns"].published, "a delivered verdict was paged about as a failure"

    def test_a_stage_run_that_ends_without_stage_complete_is_not_paged_about(self):
        """The backstop is triage-only. A data-prep stage that misses stage_complete
        already has a listener -- its task token fails and the state machine reacts -- so
        paging there would put an email in front of the owner for every stage retry."""
        ac = FakeAgentCore([text_stream("done"), text_stream("done"), text_stream("done")])
        c = clients(ac)
        out = driver.handler(driver_event(), clients=c)
        assert out["status"] == "failed"
        assert c["sfn"].failures, "the stage token was left parked"
        assert not c["sns"].published, \
            "a stage failure paged the owner; only an unanswered TRIAGE should"

    def test_a_crashed_triage_reaches_the_owner_too(self):
        """A bus triage has no task token, so the crash path's send_task_failure carries
        the news to nobody and no state machine is waiting to hear it. Without a page, a
        crashed triage is indistinguishable from one that never fired."""
        class Exploding:
            calls = []

            def invoke_harness(self, **kw):
                raise RuntimeError("harness runtime unavailable")

        c = clients(Exploding())
        with pytest.raises(RuntimeError):
            driver.handler(driver_event(
                stage="orchestrator", task="triage", run_id="run-orch-1",
                task_token=None,
                params={"escalation": {"run_id": "run-stuck-6"}}), clients=c)
        assert c["sns"].published, "a crashed triage told nobody but CloudWatch"
        assert "harness runtime unavailable" in \
            json.loads(c["sns"].published[0]["Message"])["situation"]

    def test_a_failed_backstop_page_does_not_mask_the_real_outcome(self):
        """The backstop runs on the way out, after the outcome is decided. An SNS failure
        there must not turn a merely-unanswered triage into a crashed invocation -- that
        would trade a silent failure for a louder wrong one."""
        class DeadSns:
            published = []

            def publish(self, **kw):
                raise RuntimeError("SNS unavailable")

        ac = FakeAgentCore([text_stream("a"), text_stream("b"), text_stream("c")])
        c = clients(ac)
        c["sns"] = DeadSns()
        out = driver.handler(driver_event(
            stage="orchestrator", task="triage", run_id="run-orch-1", task_token=None,
            params={"escalation": {"run_id": "run-stuck-7"}}), clients=c)
        assert out["status"] == "failed"
        assert "backstop_paged" not in out

    def test_the_answered_statuses_are_the_ones_the_driver_can_return(self):
        """TRIAGE_ANSWERED is a hand-kept tuple guarding a return value, so it can drift
        out of step with the returns it names. Every entry must be a status some `return`
        in the driver actually produces -- otherwise an entry is a typo that silently
        turns the backstop off for a case nobody notices."""
        src = (REPO / "orchestration/harness_driver/handler.py").read_text()
        returned = set(re.findall(r'"status":\s*"([a-z_]+)"', src))
        unknown = [s for s in driver.TRIAGE_ANSWERED if s not in returned]
        assert not unknown, (
            f"{unknown} appears in TRIAGE_ANSWERED but no return in the driver produces "
            "it; the backstop is silently disabled for a status that never occurs")
        for must in ("resolved", "paged", "dispatched"):
            assert must in driver.TRIAGE_ANSWERED, \
                f"{must} answers an escalation; paging on top of it is noise"
        assert "failed" not in driver.TRIAGE_ANSWERED, \
            "a failed triage is exactly the case the backstop exists for"

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

    # --- #24: a called stage_complete reported as never called -----------------
    #
    # The two halves, again. Half one: only stopReason == "tool_use" means the harness
    # is waiting for a result, so a toolUse riding with end_turn must not be answered --
    # true, and it is why the resume-rejection bug on the console's dispatch path was
    # cured. Half two: an inline function is BY DEFINITION one the harness cannot
    # service, so the runtime emits the block and waits for the driver. Both correct,
    # never connected: the stop_reason check was applied to inline functions too, and
    # every inline call that arrived with end_turn was silently dropped and counted as
    # prose. Live cost: run-20260810T174626Z-3f08b4c6 failed MissingStageComplete at
    # DataPrepGenerate with 300 verified customer rows already written to S3.

    def test_a_stage_complete_riding_with_end_turn_is_serviced_not_discarded(self):
        """The whole bug in one turn: the agent does the work, writes the artifact, calls
        stage_complete, and adds a closing sentence -- so the runtime stops the message
        with end_turn instead of tool_use. The stage IS complete and the driver must
        settle it. Before the fix this returned failed/MissingStageComplete after three
        such turns, which is a compliant agent being told it never called the tool."""
        uri = "s3://llmops-data-test/runs/run-test-1/distillation/generated.jsonl"
        ac = FakeAgentCore([tool_use_stream_ending_in_prose(
            "stage_complete", {"outputs": [uri]})])
        c = clients(ac, FakeS3(existing=[uri]))
        out = driver.handler(driver_event(), clients=c)
        assert out["status"] == "completed", (
            f"a called stage_complete was discarded because it rode with end_turn: "
            f"{out}")
        assert c["sfn"].successes, "the task token was never settled"
        assert not c["sfn"].failures, \
            "MissingStageComplete fired on a turn that called stage_complete"

    def test_the_serviced_tool_set_matches_the_dispatch_branches(self):
        """SERVICED_TOOLS licenses the override above, so it has to BE the dispatch
        table. Derived from this module's source, in both directions, because both
        skews are silent:

          * a name in the set with no branch reaches `{"status": "unsupported"}`, and
            the override made that reachable from an end_turn turn too;
          * a branch missing from the set gets discarded whenever it rides with
            end_turn -- which is the bug this whole section exists to cure, reappearing
            for one tool instead of all of them.

        Scraped rather than restated: a hand-kept second copy of the list is exactly
        the "one model, four names" defect, and a guard that lists the names itself
        would agree with a stale set."""
        src = (REPO / "orchestration/harness_driver/handler.py").read_text()
        body = src[src.index("def _run_stage"):]
        branches = set(re.findall(r'if name == "([a-z_]+)"', body))
        # the finops trio is dispatched through a tuple membership test, not an
        # equality branch; derive it from that tuple rather than naming it here
        finops = re.search(r"FINOPS_TERMINAL_TOOLS = \(([^)]*)\)", src)
        assert finops, "FINOPS_TERMINAL_TOOLS moved; this guard can no longer see it"
        branches |= set(re.findall(r'"([a-z_]+)"', finops.group(1)))
        assert len(branches) > 5, \
            f"the branch scrape found only {branches} -- the parse is broken, not clean"
        declared = set(driver.SERVICED_TOOLS)
        assert branches == declared, (
            "SERVICED_TOOLS and the dispatch branches have drifted.\n"
            f"  branches with no entry in the set (dropped when they ride with "
            f"end_turn): {sorted(branches - declared)}\n"
            f"  entries with no branch (serviced as 'unsupported'): "
            f"{sorted(declared - branches)}")

    def test_a_rejected_courtesy_ack_cannot_un_complete_a_settled_stage(self):
        """Servicing the call is only half of it. The ack that follows re-invokes the
        harness with a toolResult, and by then the token is settled and the artifacts are
        verified -- so if that invoke raises, the stage genuinely finished and the Lambda
        reports a crash anyway, leaving the state machine with a settled token AND an
        invocation error for the same stage. The two halves reassembled one state on.

        The rejection scripted below is a hazard, not a recorded event: servicing a call
        that arrived with end_turn sends a resume for a turn the runtime has closed, and
        whether it accepts one is untested (_tool_result_content echoes the toolUse, so
        it may). It does not matter which way that goes -- throttling and 5xx reach the
        same line, and nothing downstream reads an ack. Checked through the shared helper
        rather than one branch, because all eight terminal branches have this shape."""
        uri = "s3://llmops-data-test/runs/run-test-1/distillation/generated.jsonl"

        class _RejectingAck(FakeAgentCore):
            def invoke_harness(self, **kw):
                last = kw["messages"][-1]["content"][0]
                if "toolResult" in last:
                    raise RuntimeError("ValidationException: The number of toolResult "
                                       "blocks at messages.1.content exceeds the number "
                                       "of toolUse blocks of previous turn")
                return super().invoke_harness(**kw)

        ac = _RejectingAck([tool_use_stream_ending_in_prose(
            "stage_complete", {"outputs": [uri]})])
        c = clients(ac, FakeS3(existing=[uri]))
        out = driver.handler(driver_event(), clients=c)
        assert out["status"] == "completed", (
            "a rejected courtesy ack turned a finished stage into a crashed one; the "
            "token was already settled when it fired")
        assert c["sfn"].successes, "the task token was never settled"

        src = (REPO / "orchestration/harness_driver/handler.py").read_text()
        body = src[src.index("def _run_stage"):]
        # Every terminal branch acks then returns. Any that still calls _invoke
        # directly is one control-plane hiccup away from the same bug.
        raw = re.findall(r'^\s+_invoke\(c\["agentcore"\]', body, re.M)
        assert not raw, (
            f"{len(raw)} terminal branch(es) still ack with a bare _invoke instead of "
            "_ack_terminal, so a rejected ack there still raises after the effect landed")

    # --- #28: a call the model TYPED instead of made ---------------------------
    #
    # #24 recovered an inline function the runtime EMITTED and the driver discarded.
    # This is the other way for the same stage to die: the model never emits a block at
    # all, it writes the call out as text. Live on rehearsal run-20260811T005043Z-320cc47e,
    # whose finetune agent prepared the data, launched SageMaker job
    # llmops-qlora-...-i0, verified it InProgress, updated the manifest, and then ended
    # two consecutive turns with the literal string `<invoke name="job_launched">`.
    # Counted as prose both times -> MissingStageComplete, while the job it had correctly
    # launched ran on to Completed (442 billable seconds). Measured scale: across 25 days
    # of driver logs `tool=job_launched` appears ZERO times, `tool=stage_complete` three.

    #: The exact text of the 08:20:26Z turn, trimmed to the invoke block. A synthesised
    #: fixture would test the regex I wrote rather than the output that broke the run.
    TYPED_JOB_LAUNCHED = (
        'Job `llmops-qlora-run-20260811T005043Z-320cc47e-i0` confirmed **InProgress** '
        '(Pending capacity), manifest entry `stages.finetune` already written.\n'
        'Re-issuing the launch signal per launch-and-release:\n\n'
        '<invoke name="job_launched">\n'
        '<parameter name="job_name">llmops-qlora-run-20260811T005043Z-320cc47e-i0'
        '</parameter>\n'
        '<parameter name="job_arn">arn:aws:sagemaker:us-east-1:123456789012:'
        'training-job/llmops-qlora-run-20260811T005043Z-320cc47e-i0</parameter>\n'
        '<parameter name="status">InProgress</parameter>\n'
        '<parameter name="iteration">0</parameter>\n'
        '</invoke>')

    def test_a_typed_job_launched_parks_the_token_instead_of_failing_the_stage(self):
        """The live failure, end to end: the agent typed the call and the stage died.

        This must release, not fail. The job really was running -- so the alternative
        the driver actually chose (MissingStageComplete) both fails a stage that
        succeeded AND orphans a live SageMaker job with no parked token, meaning
        nothing settles when it finishes."""
        ac = FakeAgentCore([text_stream(self.TYPED_JOB_LAUNCHED),
                            text_stream("acknowledged")])
        c = clients(ac)
        out = driver.handler(driver_event(stage="finetune", task="launch"), clients=c)
        assert out["status"] == "released", (
            f"a typed job_launched was still invisible: {out}")
        assert not c["sfn"].failures, \
            "MissingStageComplete fired on a turn that announced a real launched job"
        parked = next(u for u in c["ddb"].Table(ENV["RUNS_TABLE"]).updates
                      if ":j" in (u.get("ExpressionAttributeValues") or {}))
        assert parked["ExpressionAttributeValues"][":j"] == \
            "llmops-qlora-run-20260811T005043Z-320cc47e-i0", \
            "the job name came from somewhere other than the parameters the model wrote"
        assert parked["ExpressionAttributeValues"][":t"] == "tok-123", \
            "the task token was not parked, so nothing will settle on job completion"

    def test_a_typed_stage_complete_still_has_to_prove_its_outputs_exist(self):
        """Recovering the call must not upgrade the claim. A typed stage_complete goes
        through verify_outputs exactly like a real one: claim an object that is not in
        the bucket and the stage is rejected, not settled. Otherwise the recovery path
        becomes a way to pass verification by writing prose -- strictly worse than the
        bug, because a run would then report success having produced nothing."""
        missing = "s3://llmops-data-test/runs/run-test-1/distillation/never-written.jsonl"
        typed = ('All done.\n<invoke name="stage_complete">\n'
                 '<parameter name="stage">data-prep</parameter>\n'
                 '<parameter name="task">generate</parameter>\n'
                 f'<parameter name="outputs">["{missing}"]</parameter>\n</invoke>')
        ac = FakeAgentCore([text_stream(typed), text_stream("retrying"),
                            text_stream("still nothing")])
        c = clients(ac, FakeS3(existing=[]))
        out = driver.handler(driver_event(), clients=c)
        assert out["status"] == "failed", \
            "a typed stage_complete settled a stage whose claimed output does not exist"
        assert not c["sfn"].successes, "the token was settled on an unverified claim"
        rejection = json.dumps(ac.calls[1]["messages"])
        assert "rejected" in rejection and "never-written" in rejection, (
            "the agent was not told WHICH claimed output was missing, so it cannot fix "
            f"it: {rejection[:300]}")

    def test_a_typed_outputs_list_arrives_as_a_list_not_as_text(self):
        """The #27 shape, one layer up. A typed `outputs` parameter holding
        '["s3://a", "s3://b"]' must be parsed into a list before it reaches
        verify_outputs -- as one string it starts with '[', so head_object skips it and
        every URI inside passes unchecked. Both URIs below are absent from the bucket,
        so a run that "passes" here is a run whose verification did nothing."""
        a = "s3://llmops-data-test/runs/run-test-1/a.jsonl"
        b = "s3://llmops-data-test/runs/run-test-1/b.json"
        call = driver.parse_typed_call(
            '<invoke name="stage_complete">'
            f'<parameter name="outputs">["{a}", "{b}"]</parameter></invoke>')
        assert call["input"]["outputs"] == [a, b], (
            f"a typed JSON list stayed text: {call['input']['outputs']!r}")
        assert driver.verify_outputs(FakeS3(existing=[]), call["input"]["outputs"]) \
            == [a, b], "both missing URIs were skipped -- the check is vacuous again"

    def test_a_typed_shell_call_is_never_recovered(self):
        """The boundary, and it is a security one, not a tidiness one. shell runs INSIDE
        the harness, so a typed shell is at best a transcript of a call the runtime
        already served and at worst text the agent quoted from a log or a customer
        document. A driver that executes either is executing prose. Only functions this
        driver alone can answer -- SERVICED_TOOLS -- are recoverable, which is the same
        rule the end_turn override follows."""
        assert driver.parse_typed_call(
            '<invoke name="shell"><parameter name="command">rm -rf /</parameter>'
            '</invoke>') is None
        assert driver.parse_typed_call(
            '<invoke name="code_interpreter"><parameter name="code">1</parameter>'
            '</invoke>') is None
        for name in sorted(driver.SERVICED_TOOLS):
            assert driver.parse_typed_call(
                f'<invoke name="{name}"><parameter name="x">1</parameter></invoke>'
                )["name"] == name, f"{name} is dispatchable but not recoverable"

    def test_a_real_tool_call_always_wins_over_a_typed_one(self):
        """A turn can do both: emit a real block AND narrate a call in its text. The
        structured one is what the runtime actually did, so it must win -- otherwise a
        recovered transcript of an EARLIER call could override the current one, settling
        a stage on stale parameters."""
        uri = "s3://llmops-data-test/runs/run-test-1/distillation/generated.jsonl"
        ac = FakeAgentCore([tool_use_stream_ending_in_prose(
            "stage_complete", {"outputs": [uri]},
            text=self.TYPED_JOB_LAUNCHED)])
        c = clients(ac, FakeS3(existing=[uri]))
        out = driver.handler(driver_event(), clients=c)
        assert out["status"] == "completed", (
            "the typed job_launched in the same turn's text displaced a real "
            f"stage_complete block: {out}")
        assert c["sfn"].successes, "the real stage_complete was not the one serviced"

    def test_a_recovered_call_is_answered_as_text_not_as_a_tool_result(self):
        """There is no toolUseId to answer: the model typed the call, so the runtime
        never minted one. Echoing a null id back would be rejected ("the number of
        toolResult blocks ... exceeds the number of toolUse blocks of previous turn"),
        which on a NON-terminal branch (checkpoint, a rejected stage_complete) kills the
        next turn rather than a courtesy message. The agent still has to learn the
        outcome, so it arrives as plain text."""
        recovered = driver.parse_typed_call(
            '<invoke name="checkpoint"><parameter name="progress_uri">s3://b/p'
            '</parameter></invoke>')
        assert recovered["toolUseId"] is None
        msgs = driver._tool_result_content(recovered, {"status": "continue"})
        assert len(msgs) == 1 and msgs[0]["role"] == "user", \
            f"a recovered call was answered with a toolUse/toolResult pair: {msgs}"
        blob = json.dumps(msgs)
        assert "toolResult" not in blob and "toolUse" not in blob, \
            f"a null toolUseId still reaches the runtime: {blob}"
        assert "continue" in blob, "the agent was not told the outcome at all"
        # A REAL call keeps the pair — this must not have become text for everyone.
        real = {"toolUseId": "tu-1", "name": "checkpoint", "input": {}}
        assert len(driver._tool_result_content(real, {"status": "continue"})) == 2

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

    # --- #26: the deadline handoff needed a chunk in order to notice no chunk came ---

    def test_a_stream_that_goes_quiet_at_the_wall_still_hands_off(self):
        """The trickling-stream test above passes because a chunk keeps ARRIVING, which
        is the only moment `out_of_wall` is evaluated. A stream that goes QUIET reaches
        neither escape hatch, and the interval where that is true is almost the whole
        invocation: after a chunk at elapsed t, boto's read_timeout restarts and is next
        due at t + 870, past the 900s wall for every t > 30; `out_of_wall` needs a chunk
        after 855. So a last chunk anywhere in (30, 855)s -- 825 of the 900 -- left the
        runtime to hard-kill the invocation.

        Live: run-20260810T182807Z-e394ada9's curate turn,
        `REPORT RequestId: 925119d7-11e3-4fc2-b106-4cb55d83b9ac Duration: 900000.00 ms
        Billed Duration: 900000 ms Memory Size: 512 MB Max Memory Used: 117 MB
        Status: timeout`, with ZERO application log lines. The agent had already written
        curated.jsonl (36,151 B), generated.jsonl (194,799 B) and stats.json (2,546 B)
        and had already called stage_complete; the call died with the invocation and the
        stage failed MissingStageComplete with its own outputs in S3. Its recorded cause
        was literally true: "is complete and was verified in my previous turn".

        A real blocking read, not a fake that returns: the defect IS that nothing returns.
        A double whose __next__ hands back control could not express it.
        """
        import socket
        import threading

        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        threading.Thread(target=lambda: (srv.accept(), time.sleep(30)),
                         daemon=True).start()
        cli = socket.create_connection(("127.0.0.1", srv.getsockname()[1]))

        class QuietStream:
            """Blocks in recv() forever — botocore's urllib3 read on a silent stream."""

            def __iter__(self):
                return self

            def __next__(self):
                cli.recv(4096)
                raise StopIteration  # unreachable: nothing will ever be sent

        margin = driver.DRAIN_DEADLINE_MARGIN_MS
        driver.DRAIN_DEADLINE_MARGIN_MS = 3_000
        try:
            t0 = time.time()
            out = driver._drain({"stream": QuietStream()},
                                remaining_ms=lambda: 5_000)
        finally:
            driver.DRAIN_DEADLINE_MARGIN_MS = margin
            cli.close()
            srv.close()
        elapsed = time.time() - t0
        assert out["error"] == driver.DEADLINE_CUT, (
            f"a quiet stream returned {out['error']!r}, not a deadline cut — the "
            "invocation would run to the wall and be hard-killed with the agent's "
            "pending inline function unanswered")
        assert elapsed < 4.0, (
            f"_drain took {elapsed:.1f}s to give up on a stream it had 2s of budget "
            "for; the watchdog did not interrupt the blocking read")

    def test_a_quiet_stream_is_a_deadline_cut_not_a_stream_death(self):
        """The two are handled differently on purpose: a death burns the one same-session
        salvage retry, a deadline cut hands the turn to a fresh invocation with the whole
        900s. Relabelling matters because the harness turn is still running server-side
        (840s cap) and will finish without us — spending the retry here would leave a
        REAL death later in the same stage unprotected."""
        class _Quiet:
            def __iter__(self):
                raise driver._StreamWatchdogFired("no stream progress")

        out = driver._drain({"stream": _Quiet()})
        assert out["error"] == driver.DEADLINE_CUT, (
            f"the watchdog's own exception leaked as a stream death ({out['error']!r}), "
            "which burns the salvage retry on a turn that never failed")

    def test_a_triage_heartbeat_is_refused_by_design_and_says_nothing(self, capsys):
        """A triage runs under `triage-<subject>` and only start_pipeline creates run
        rows, so a triage HAS no row and cannot have one: `attribute_exists(run_id)` must
        refuse every triage heartbeat. The condition is right -- without it update_item's
        upsert would mint a row carrying driver_beat_at and driver_beat_payload, which is
        precisely what the resurrector sweeps for, so the driver would manufacture
        resurrectable ghost runs for every non-run invocation (the {run_id, status}
        version of that already left `sweep-2026-08-01` in the live table).

        What was wrong is calling the refusal a failure. Live, run-20260810T182807Z-
        e394ada9's triage printed 11 x "heartbeat write failed (continuing):
        ConditionalCheckFailedException" in 2 minutes, every one of them describing
        correct behaviour -- which is how the line that would mean "the beat is actually
        broken" stopped meaning anything.
        """
        ev_ = driver_event()
        ev_["run_id"] = "triage-run-20260810T182807Z-e394ada9"
        ev_["stage"] = "orchestrator"
        ev_["task"] = "triage"
        ev_.pop("task_token", None)
        c = clients(FakeAgentCore([text_stream("no verdict")] * 4))
        driver.handler(ev_, clients=c)
        out = capsys.readouterr().out
        assert "heartbeat write failed" not in out, (
            "a by-design refusal is still reported as a failure:\n" + "\n".join(
                ln for ln in out.splitlines() if "heartbeat" in ln))
        rows = c["ddb"].Table(os.environ["RUNS_TABLE"]).items
        assert not [r for r in rows if str(r.get("run_id", "")).startswith("triage-")], (
            f"the driver minted a ghost run row for a triage: {rows} — the resurrector "
            "sweeps on driver_beat_at, so this row would be 'resurrected' forever")

    def test_a_watchdog_that_cannot_arm_does_not_kill_the_turn(self):
        """signal.signal raises ValueError off the main thread. A watchdog that cannot be
        installed must degrade to the old per-chunk behaviour, not take down a turn that
        would otherwise have succeeded: the guard exists to catch a hang, and a guard
        that converts a working path into a crash is worse than the hang it prevents."""
        import threading

        result = {}

        def run():
            try:
                result["out"] = driver._drain(
                    {"stream": text_stream("hello")}, remaining_ms=lambda: 600_000)
            except Exception as exc:  # noqa: BLE001 — the point of the test
                result["exc"] = exc

        t = threading.Thread(target=run)
        t.start()
        t.join(timeout=10)
        assert "exc" not in result, (
            f"_drain raised off the main thread ({result.get('exc')!r}) — arming the "
            "watchdog must never be able to fail a turn")
        assert result["out"]["text"] == "hello", (
            "the stream was not drained when the watchdog could not be armed")


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

    # -- the audit emit must never be able to abort the settle ------------------
    #
    # run-20260811T040003Z-3548116f: this Lambda's role shipped without
    # events:PutEvents, emit_event(MODEL_TRAINED) raised AccessDeniedException, and
    # because the emit sat BEFORE send_task_success inside the same try, describing the
    # training killed settling it -- EventBridge retried twice into the same wall and
    # the third delivery became an AsyncEventsDropped. A successful training job whose
    # stage never learned it had finished. The grant is fixed; these pin the ordering,
    # the way #52's test_a_failed_bus_emit_still_settles_the_task_token pins the
    # driver's four settle sites.

    @pytest.mark.parametrize("event,outcome,settled", [
        (sm_event("Completed", ModelArtifacts={"S3ModelArtifacts": "s3://b/m.tar.gz"}),
         "resumed", "successes"),
        (sm_event("Stopped", BillingSecondsUsed=0), "capacity-relaunch", "failures"),
        (sm_event("Failed", FailureReason="OOM"), "failed", "failures"),
    ])
    def test_a_dead_bus_still_settles_every_branch_and_clears_the_token(
            self, event, outcome, settled):
        """All three branches, because all three emit before their settle and any one of
        them stranding a token costs the stage's full 86400 s timeout."""
        c = self._clients({"run_id": "run-1", "task_token": "tok-9"})

        def boom(**kw):
            raise RuntimeError("AccessDeniedException: events:PutEvents")
        c["events"].put_events = boom

        out = resume_pipeline.handler(event, clients=c)
        assert out["outcome"] == outcome
        assert getattr(c["sfn"], settled), (
            f"a failed audit emit aborted the {outcome} settle")
        updates = c["ddb"].Table(ENV["RUNS_TABLE"]).updates
        assert updates and "REMOVE task_token" in updates[0]["UpdateExpression"], \
            "the token stayed parked because an audit event failed"

    def test_the_skipped_audit_event_says_so(self, capsys):
        """The trade this makes is a lost bus event for a settled token, and nothing
        consumes those three events today (one bus rule, EscalatedToHuman, which this
        handler never emits; no archive; the driver writes llmops-stage-events itself).
        So the print IS the record -- and llmops-resume-pipeline-errors no longer needs
        it to be a traceback to notice."""
        c = self._clients({"run_id": "run-1", "task_token": "tok-9"})

        def boom(**kw):
            raise RuntimeError("bus unreachable")
        c["events"].put_events = boom
        resume_pipeline.handler(sm_event("Completed"), clients=c)
        out = capsys.readouterr().out
        assert ev.MODEL_TRAINED in out and "FAILED" in out, \
            f"a dropped audit event left no trace: {out!r}"

    def test_no_bus_emit_in_this_handler_bypasses_the_audit_wrapper(self):
        """Derived, not enumerated: the defect was one unwrapped emit, so a future
        fourth emit written the old way must red here rather than wait for the next
        stranded token. _audit is the only legal caller of ev.emit_event in this file."""
        tree = ast.parse((REPO / "orchestration/resume_pipeline/handler.py").read_text())
        offenders = []
        for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
            if fn.name == "_audit":
                continue
            for node in ast.walk(fn):
                if (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "emit_event"):
                    offenders.append(f"{fn.name}:{node.lineno}")
        assert not offenders, (
            f"bus emit outside _audit, so a PutEvents failure can abort a settle again: "
            f"{offenders}")
        assert "_audit(" in (REPO / "orchestration/resume_pipeline/handler.py").read_text(), \
            "the wrapper is unused -- this guard would pass on a handler that emits nothing"


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


# --- the field-propagation model, used by the path-walking guard below ----------
#
# Written because asserting Next transitions is not enough. IncrementIteration was a
# Pass with Parameters and no ResultPath: its constructed object REPLACED the whole
# state, dropping task_id, so every run that remediated even once died at closeout on
# States.Runtime -- which no Catch can intercept. test_remediation_loop_wiring asserted
# every transition on that path and passed the entire time, because transitions were
# never the thing that broke.

#: Keys whose `$.x` values are NOT reads of the state document, at any depth.
#: ResultPath is where a result is WRITTEN -- counting it as a read made the walk
#: report every state as missing the field it produces. ResultSelector is evaluated
#: against the task RESULT, not the state document.
_NOT_A_READ = ("ResultPath", "ResultSelector")


def _scrub_writes(node):
    """Copy of `node` with write-target keys removed at every depth.

    Recursive because Catch entries carry their own ResultPath, and a Catch's
    ResultPath is the classic false positive: `DataPrepGenerate` "reads" $.error
    only in the sense that it writes one there when it fails.
    """
    if isinstance(node, dict):
        return {k: _scrub_writes(v) for k, v in node.items() if k not in _NOT_A_READ}
    if isinstance(node, list):
        return [_scrub_writes(v) for v in node]
    return node


def _root_fields_read(state: dict) -> set:
    """Root-level `$.field` references this state reads.

    Only reads that resolve against the state document count. Context-object paths
    ($$.…) come from Step Functions, not the state, and `$` alone is the whole
    document; write targets are stripped by _scrub_writes.
    """
    out = set()
    for m in re.findall(r'"\$\.([A-Za-z_][A-Za-z0-9_.\[\]]*)"',
                        json.dumps(_scrub_writes(state))):
        out.add(m.split(".")[0].split("[")[0])
    return out


def _fields_after(name: str, state: dict, incoming: set) -> set:
    """What the state document holds AFTER this state runs.

    Mirrors the four ASL rules that actually govern propagation:
      * Parameters without ResultPath on a Pass  -> the object REPLACES the state.
      * ResultPath "$"                           -> the result replaces the state.
      * ResultPath "$.x"                         -> the result is grafted at x.
      * ResultPath null / absent on a Pass       -> the state passes through.
    """
    stype = state.get("Type")
    if stype == "Pass" and "Parameters" in state and "ResultPath" not in state:
        return {k[:-2] if k.endswith(".$") else k for k in state["Parameters"]}
    if "ResultPath" in state:
        rp = state["ResultPath"]
        if rp is None:                      # discard the result, keep the state
            return set(incoming)
        if rp == "$":                       # result becomes the whole state
            return set()
        return set(incoming) | {rp[2:].split(".")[0]}
    if stype == "Task":
        # A Task with no ResultPath replaces the state with its result. Every Task
        # here sets one; if one ever does not, this models the real loss.
        return set()
    return set(incoming)


def _seeded_execution_input_fields() -> set:
    """The fields start_pipeline actually puts in the execution input.

    Derived from the handler rather than transcribed: this set IS the machine's
    entry contract, and a hand-copied list of it silently rots the day someone adds
    a sixth field. Parsed the same way the starter-contract guards below parse it.
    """
    src = (REPO / "orchestration/start_pipeline/handler.py").read_text()
    start_input = src[src.index("input=json.dumps("):]
    start_input = start_input[:start_input.index("\n\n")]
    fields = set(re.findall(r'"([a-z_]+)":', start_input))
    assert "run_id" in fields and "task_id" in fields, (
        f"failed to parse start_pipeline's execution input; got {fields}")
    return fields


def _walk_field_availability(states: dict, start: str, seed: set):
    """Walk every path from `start`, returning [(state, missing_fields, path)].

    A state entered along several paths is checked with the INTERSECTION of what
    those paths provide -- a field is only safe to read if every way in supplies it.
    """
    catch_adds = {}   # state -> field a Catch grafts on entry
    for n, st in states.items():
        for c in st.get("Catch", []) or []:
            rp = c.get("ResultPath")
            if rp and rp.startswith("$."):
                catch_adds.setdefault(c["Next"], set()).add(rp[2:].split(".")[0])

    incoming = {start: set(seed)}
    problems, queue, guard = [], [(start, tuple())], 0
    while queue:
        guard += 1
        assert guard < 10_000, "field walk did not converge"
        name, path = queue.pop()
        if name not in states:
            continue
        st = states[name]
        have = incoming[name]
        missing = _root_fields_read(st) - have
        if missing:
            problems.append((name, sorted(missing), path + (name,)))
        after = _fields_after(name, st, have)
        for nxt in _exits(st):
            arriving = set(after) | catch_adds.get(nxt, set())
            if nxt in incoming:
                merged = incoming[nxt] & arriving
                if merged == incoming[nxt]:
                    continue          # nothing new to learn on this edge
                incoming[nxt] = merged
            else:
                incoming[nxt] = arriving
            queue.append((nxt, path + (name,)))
    return problems


def _job_launching_tasks() -> set:
    """(stage, task) for every prompt bullet that promises to emit job_launched.

    Which states can be hit by a training-job error is a property of the AGENTS, not
    of the ASL: resume_pipeline reads the parked token off the run row and settles it
    with CapacityStopped / TrainingJobFailed without ever asking which state parked
    it. So the set of states needing those Catches is derived here from the side that
    decides it, and a launch task added to a prompt later drags the ASL with it.

    Two parsing details, both load-bearing:

      * The text is CUT at the global `Rules:` block first. That block ends with the
        turn-end invariant, which mentions job_launched, and the LAST task bullet's
        chunk runs to end-of-text -- so without the cut, whichever task happens to be
        listed last is credited with launching jobs. The naive version of this
        derivation over-collected exactly FinetuneAnalyze and EvalGate that way, and
        it would have "passed" while asserting something false.
      * systemPrompt is a list of blocks, so the blocks are joined before matching:
        a bullet split across two blocks is otherwise invisible.
    """
    out = set()
    for cfg in sorted((REPO / "agents").glob("*/harness.json")):
        doc = json.loads(cfg.read_text())
        text = "".join(b.get("text", "") for b in (doc.get("systemPrompt") or []))
        cut = re.search(r"(?m)^\s*Rules:", text)
        tasks_only = text[:cut.start()] if cut else text
        hits = [(m.start(), m.group(1)) for m in
                re.finditer(r'(?m)^\s*[-*]\s*\\?"([a-z_]+)\\?"\s*:', tasks_only)]
        assert hits, f"{cfg}: parsed no task bullets before Rules: -- the parse broke"
        for i, (pos, task) in enumerate(hits):
            end = hits[i + 1][0] if i + 1 < len(hits) else len(tasks_only)
            if "job_launched" in tasks_only[pos:end]:
                out.add((cfg.parent.name, task))
    return out


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

    def test_no_state_reads_a_field_some_path_does_not_supply(self, asl):
        """Every `$.field` a state reads must be present however the state was entered.

        This is the guard that was missing. A JSONPath that is not present raises
        States.Runtime, which NO Catch can intercept -- start_pipeline's NO_TASK
        sentinel exists for exactly this reason and says so. So a dropped field is not
        a degraded run, it is an uncatchable death mid-execution.

        IncrementIteration dropped task_id (Pass + Parameters + no ResultPath replaces
        the whole state), so MarkTaskDone/MarkTaskFailed read $.task_id off a document
        that no longer had it. Every run that entered remediation once died at closeout
        with its conductor task record left open -- the self-healing loop the README
        leads with. It was never caught because no run had remediated yet, and because
        the only guard asserted transitions.

        Deliberately generic: it walks EVERY path and checks EVERY read, so the next
        state that drops a field fails here instead of in production.
        """
        states = asl["States"]
        # What start_pipeline actually puts in the execution input. task_id is always
        # set (NO_TASK when there is no conductor task) precisely so it can be read.
        seed = _seeded_execution_input_fields()
        problems = _walk_field_availability(states, asl["StartAt"], seed)
        assert not problems, "states read fields no path supplies:\n" + "\n".join(
            f"  {name} reads {miss} -- reachable via {' -> '.join(path[-4:])}"
            for name, miss, path in problems)

    def test_the_field_walk_would_notice_a_dropped_field(self, asl):
        """A guard that cannot fail is not a guard.

        Mutate the document the way the real bug looked and assert the walk reports it.
        Without this, the test above could pass because the model is too permissive
        rather than because the machine is correct.
        """
        states = json.loads(json.dumps(asl["States"]))
        seed = _seeded_execution_input_fields()
        assert not _walk_field_availability(states, asl["StartAt"], seed)

        # Reintroduce the exact defect: a Pass that rebuilds the state without task_id.
        states["IncrementIteration"] = {
            "Type": "Pass",
            "Parameters": {"run_id.$": "$.run_id",
                           "manifest_uri.$": "$.manifest_uri",
                           "iteration.$": "States.MathAdd($.iteration, 1)"},
            "Next": "RemediateFinetune",
        }
        problems = _walk_field_availability(states, asl["StartAt"], seed)
        offenders = {name: miss for name, miss, _ in problems}
        assert offenders, "the walk did not notice a state rebuilt without task_id"
        assert any("task_id" in miss for miss in offenders.values()), \
            f"the walk missed the dropped task_id; it reported {offenders}"

    def test_the_remediation_path_still_carries_the_conductor_task(self, asl):
        """Named explicitly, because this is the field whose loss was fatal.

        The generic walk above would catch it, but a run through remediation is the
        scenario that actually happened, and it deserves a test that says so by name.
        """
        states = asl["States"]
        inc = states["IncrementIteration"]
        assert _reaches(states, "IncrementIteration", "MarkTaskFailed")
        assert _reaches(states, "IncrementIteration", "MarkTaskDone"), (
            "remediation must still be able to reach the success closer")
        if inc.get("Type") == "Pass" and "Parameters" in inc and "ResultPath" not in inc:
            carried = {k[:-2] if k.endswith(".$") else k for k in inc["Parameters"]}
            assert "task_id" in carried, (
                "IncrementIteration rebuilds the state, so it must carry task_id "
                "forward -- MarkTaskDone/MarkTaskFailed read it and a missing "
                "JSONPath raises States.Runtime, which no Catch can intercept")

    def test_a_state_replacing_pass_hands_back_the_whole_entry_contract(self, asl):
        """The general form of the bug above, so the next one fails here too.

        Any Pass with Parameters and no ResultPath replaces the entire state document,
        which makes it a second writer of the machine's entry contract -- and it has to
        honour all of it, not the subset the state it feeds happens to read. Checking
        only "does the immediate Next read this" would have let the original bug
        through: RemediateFinetune does not read task_id; MarkTaskDone, four hops
        later, does. Derived from start_pipeline so adding a sixth seeded field breaks
        here rather than in an execution.
        """
        seed = _seeded_execution_input_fields()
        rebuilders = {n: st for n, st in asl["States"].items()
                      if st.get("Type") == "Pass" and "Parameters" in st
                      and "ResultPath" not in st}
        assert rebuilders, "no state-replacing Pass found; has the loop been rewritten?"
        for name, st in rebuilders.items():
            carried = {k[:-2] if k.endswith(".$") else k for k in st["Parameters"]}
            assert seed <= carried, (
                f"{name} replaces the state document but drops "
                f"{sorted(seed - carried)}; every field start_pipeline seeds must be "
                "handed back, because a read of a path that is not there raises "
                "States.Runtime and no Catch can intercept it")

    def test_every_harness_task_uses_task_token(self, asl):
        for name, st in asl["States"].items():
            if st.get("Resource", "").endswith("lambda:invoke.waitForTaskToken"):
                payload = st["Parameters"]["Payload"]
                assert payload["task_token.$"] == "$$.Task.Token", name
                assert payload["iteration.$"] == "$.iteration", name

    def test_a_failed_eval_job_re_enters_eval_not_the_finetune_loop(self, asl):
        """run-20260811T040003Z-3548116f: the eval inference job failed on a defect in
        the eval agent's OWN inference code (an SDK-encoded hyperparameter read raw),
        and the only catch was EscalateFail -- the run died with zero reflection while
        FinetuneLaunch's identical failure class gets three remediation attempts.
        Routing eval failures into that existing loop would be worse than none:
        RemediateFinetune re-TRAINS, and no amount of retraining removes the quotes
        from a bucket name."""
        cats = {tuple(c["ErrorEquals"]): c for c in asl["States"]["EvalGenerate"]["Catch"]}
        tj = cats[("TrainingJobFailed",)]
        assert tj["Next"] == "RemediationChoiceEval"
        assert tj["ResultPath"] == "$.error"
        choice = asl["States"]["RemediationChoiceEval"]
        assert choice["Default"] == "EscalateFail"
        (only,) = choice["Choices"]
        assert (only["Variable"], only["NumericLessThan"]) == ("$.iteration", 3), (
            "the eval loop must spend the SAME iteration budget as the finetune loop; "
            "a separate counter would let the two loops interleave past 3 total")
        inc = asl["States"][only["Next"]]
        assert inc["Next"] == "EvalGenerate", (
            f"eval remediation re-enters {inc['Next']}; the failure lives in the eval "
            "agent's own code, so the same agent must get the retry")

    def test_every_job_launching_prompt_carries_the_hyperparameter_decoding_rule(self):
        """The defect the rule prevents: the SageMaker Python SDK json.dumps-encodes
        every hyperparameter value, the training toolkit decodes them only for argv and
        SM_HP_* env, and an entry script that reads hyperparameters.json raw gets the
        still-encoded values. Derived from tools[]: any harness that can job_launched
        authors entry scripts, so it must carry the rule; a harness that cannot is not
        forced to mention it."""
        for f in sorted((REPO / "agents").glob("*/harness.json")):
            h = json.loads(f.read_text())
            tools = {t.get("name") for t in h.get("tools", [])}
            text = h["systemPrompt"][0]["text"]
            has_rule = ("never read /opt/ml/input/config/hyperparameters.json raw" in text
                        and "SM_HP_" in text)
            if "job_launched" in tools:
                assert has_rule, (
                    f"{f.parent.name} declares job_launched but its prompt never warns "
                    "that SDK-submitted hyperparameters arrive JSON-encoded -- the "
                    "defect that killed run-20260811T040003Z-3548116f's eval job")

    def test_every_memory_wired_prompt_subordinates_memory_to_the_plan(self):
        """The shared semantic memory is the cross-run learning channel, and live it
        injected another run's MissingStageComplete post-mortem and an unrelated
        dataset's hyperparameters into every finetune invocation as bare "facts". The
        retrieval threshold fix bounds HOW MUCH crosses; this rule governs what the
        agent DOES with whatever still does.

        This assertion used to pin `wired == {data-prep, finetune, eval, deploy, monitor}`
        -- the five names 04_wire_memory.py listed -- which made the guard AGREE with the
        omission it should have caught: llmops_finops and llmops_orchestrator are wired to
        the same memory live, and finops's prompt had no precedence rule at all. A guard
        that encodes the shipped list is not evidence the list is right. It now derives
        from every agent config that exists, so an eighth agent fails here by existing."""
        for d in sorted(p.parent.name for p in (REPO / "agents").glob("*/harness.json")):
            text = json.loads((REPO / f"agents/{d}/harness.json").read_text()
                              )["systemPrompt"][0]["text"]
            assert "Retrieved memory is BACKGROUND" in text and "ALWAYS outrank" in text, (
                f"{d} is wired to the shared memory but its prompt never subordinates "
                "retrieved facts to this run's signed plan")

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
        assert set(found) == self._launch_states(asl), (
            f"the capacity exemption covers {sorted(found)}, but the states that "
            f"launch tracked jobs are {sorted(self._launch_states(asl))}")

    #: The set that was hardcoded here until 2026-08-13 was
    #: `>= {"FinetuneLaunch", "EvalGenerate"}` -- a floor over the two states that
    #: already had the Catch, so it could only ever agree with the ASL it was reading.
    #: RemediateFinetune launches a job too (its prompt bullet promises job_launched)
    #: and had ONLY States.ALL -> EscalateFail, so a $0-billed capacity stop killed a
    #: run in the middle of recovering, and this test said nothing for months.
    @staticmethod
    def _launch_states(asl: dict) -> set:
        """ASL states dispatching a (stage, task) whose prompt promises job_launched."""
        launchers = _job_launching_tasks()
        out = set()
        for name, st in asl["States"].items():
            payload = (st.get("Parameters") or {}).get("Payload") or {}
            if (payload.get("stage"), payload.get("task")) in launchers:
                out.add(name)
        assert out, "no state dispatches a launch task -- the derivation is broken"
        return out

    def test_a_job_launching_state_can_survive_a_failed_job(self, asl):
        """The other half: a launch state must also Catch TrainingJobFailed into the
        remediation Choice. States.ALL -> EscalateFail is the correct LAST resort, but
        when it is the ONLY clause, the two errors resume_pipeline actually sends --
        CapacityStopped and TrainingJobFailed, the two most likely outcomes of the
        very job this state launched -- both skip the iteration budget the pipeline
        was built around and escalate on the first try."""
        for name in sorted(self._launch_states(asl)):
            catches = asl["States"][name].get("Catch", [])
            routes = {e: c["Next"] for c in catches for e in c.get("ErrorEquals", [])}
            nxt = routes.get("TrainingJobFailed")
            assert nxt, (
                f"{name} launches a tracked job but has no TrainingJobFailed catch, so "
                f"a failed job hits {routes.get('States.ALL')} without ever consulting "
                "the iteration budget")
            assert asl["States"][nxt]["Type"] == "Choice", (
                f"{name}'s TrainingJobFailed catch goes to {nxt}, which is a "
                f"{asl['States'][nxt]['Type']}; it must reach a Choice that re-checks "
                "$.iteration, or the remediation budget is unreachable")
            # States.ALL must be the LAST CLAUSE: Step Functions matches Catch clauses
            # in order, so a States.ALL anywhere above shadows every clause below it.
            # Asserted on the clause index, not on the flattened error list -- a
            # duplicated catch-all ("States.ALL first, and again at the bottom") leaves
            # the last error looking correct while the first clause swallows everything.
            catchall = [i for i, c in enumerate(catches)
                        if "States.ALL" in c.get("ErrorEquals", [])]
            assert catchall in ([], [len(catches) - 1]), (
                f"{name}'s States.ALL sits at clause(s) {catchall} of "
                f"{len(catches)}; every clause after the first one can never match")

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


#: Params a signed plan carries that name the CUSTOMER'S OWN BYTES, mapped to the
#: promise the platform makes about each. Not a general "every param is read" guard:
#: these two are the ones whose silent absence is indistinguishable from success.
CUSTOMER_DATA_PARAMS = {
    "source_uri": "the data the customer signed for us to train on",
    "customer_eval_uri": "the acceptance set the customer's gate is anchored to",
    "ood_eval_uri": "the OOD layer that is measured and reported but never gated",
}


def _tasks_naming(param: str, dispatched: dict) -> set:
    """Which DISPATCHED (harness, task) prompts name `param`.

    Scoped to the task bullet the param appears inside, because "the prompt file
    mentions source_uri somewhere" is exactly the false pass this guard exists to
    prevent: bug #23 had the string present in `audit`'s bullet while `generate` --
    the only task on the full path -- never read it, and a file-level grep was green.
    """
    found = set()
    for cfg in sorted((REPO / "agents").glob("*/harness.json")):
        doc = json.loads(cfg.read_text())
        hid = doc.get("name") or ("llmops_" + cfg.parent.name.replace("-", "_"))
        text = "".join(b.get("text", "") for b in (doc.get("systemPrompt") or []))
        # Split into per-task bullets: from one `- "task":` to the next, or to Rules.
        bullets = re.findall(r'- \\?"([a-z_]+)\\?":(.*?)(?=\n- \\?"[a-z_]+\\?":|\nRules:|$)',
                             text, re.S)
        for task, body in bullets:
            if param in body and (hid, task) in dispatched:
                found.add((hid, task))
    return found


class TestTheCustomersOwnDataIsActuallyRead:
    """A plan param naming the customer's bytes must be read by a task on the run.

    Bug #23: `pipeline_mode: "full"` starts at DataPrepGenerate, whose prompt said
    "produce seed prompts per self-instruct patterns for the domain in params.domain"
    and never mentioned params.source_uri. The ONLY task in any of the seven prompts
    that read source_uri was data-prep's `audit`, reachable only from DataAudit, whose
    Next is Complete. So the mode that reads the customer's data cannot train, and the
    mode that trains cannot read the customer's data -- the sixth instance of two
    correct halves never connected.

    start_pipeline._plan_params already flattened plan.data.source_uri into params
    correctly (the bug #21 cure). The param ARRIVED and nothing consumed it, which is
    why no error was ever raised: a customer signed "fine-tune on my 300 tickets", the
    run trained on 300 teacher-invented samples for the domain STRING, and the
    manifest, the curated corpus, the eval report and the cost report all agreed.

    Asserted against the DISPATCHED task set rather than the declared one, because a
    task nobody can invoke reading the param is the defect, not the cure.
    """

    def test_every_customer_data_param_is_read_by_some_dispatched_task(self, asl):
        dispatched = _dispatched_tasks(asl)
        unread = {p: why for p, why in CUSTOMER_DATA_PARAMS.items()
                  if not _tasks_naming(p, dispatched)}
        assert not unread, (
            "no dispatchable task's prompt reads these plan params: "
            + "; ".join(f"params.{p} ({why})" for p, why in sorted(unread.items()))
            + ". The signed plan carries the value, start-pipeline puts it in params, "
            "and no stage on any path consumes it -- so the run executes on data no "
            "human chose while every artifact agrees with the plan.")

    def test_the_full_path_and_not_only_the_audit_reads_the_source_uri(self, asl):
        """source_uri must be read by a task the FULL pipeline reaches.

        The narrower half of the guard above, and the one that actually caught #23:
        `audit` alone satisfies "some dispatched task reads it" while leaving every
        training run blind. So the readers are intersected with what is reachable from
        the state machine's start WITHOUT taking the data_audit branch.
        """
        states = asl["States"]
        start = asl["StartAt"]
        choice = states[start]
        audit_only = {c.get("Next") for c in (choice.get("Choices") or [])}
        full_entry = choice.get("Default")
        assert full_entry and full_entry not in audit_only, (
            f"{start} has no Default distinct from its data_audit branch; this guard "
            "assumes the full path is the Default one")

        on_full_path = set()
        for name, st in states.items():
            payload = (st.get("Parameters") or {}).get("Payload") or {}
            if payload.get("harness_id") and _reaches(states, full_entry, name):
                on_full_path.add((payload["harness_id"], payload.get("task")))
        assert on_full_path, "no harness tasks are reachable on the full path"

        readers = _tasks_naming("source_uri", _dispatched_tasks(asl))
        assert readers & on_full_path, (
            f"params.source_uri is read only by {sorted(readers)}, none of which the "
            f"full pipeline reaches (it dispatches {sorted(on_full_path)}). A run in "
            "'full' mode would train on self-instructed data and never open the file "
            "the customer signed for.")

    def test_the_generate_task_prefers_customer_data_over_inventing_it(self):
        """Reading the param is not enough: precedence has to be stated.

        A prompt that mentions source_uri while still leading with "produce seed
        prompts" leaves the choice to the model, and the failure mode of guessing wrong
        is invisible. So the generate bullet must make customer data the primary branch
        and self-instruction the fallback conditioned on the param's ABSENCE.
        """
        doc = json.loads((REPO / "agents/data-prep/harness.json").read_text())
        text = "".join(b.get("text", "") for b in doc["systemPrompt"])
        bullet = re.search(r'- \\?"generate\\?":(.*?)(?=\n- \\?"[a-z_]+\\?":|\nRules:)',
                           text, re.S)
        assert bullet, "could not isolate the generate bullet -- the parse is broken"
        body = bullet.group(1)
        assert "params.source_uri" in body, (
            "generate is the only data-prep task on the full path and it does not name "
            "params.source_uri")
        # The fallback must be gated on absence, not offered as an equal alternative.
        assert re.search(r"(?:if|when)\s+params\.source_uri\s+is\s+absent", body, re.I), (
            "generate does not condition self-instruction on params.source_uri being "
            "ABSENT, so both branches read as available and the model picks one")
        primacy = body.index("params.source_uri") < body.index("self-instruct")
        assert primacy, (
            "the self-instruct instruction precedes the customer-data instruction in "
            "the generate bullet; the first branch a model reads is the one it takes")

    def test_the_scoring_task_anchors_the_gate_to_the_customers_acceptance_set(self):
        """customer_eval_uri has to be read by the task that SCORES, not just by curate.

        The broad guard above is satisfied by data-prep's curate, which reads the param
        to decontaminate the training corpus against it. That is a real use and it is
        not the one the customer bought: if eval keeps scoring the 10% val split, the
        gate measures agreement with the TEACHER on data the customer never saw, and the
        decontamination merely guarantees the acceptance set went unused. Both halves
        would be individually defensible and the pair would be the bug.
        """
        docs = {}
        for cfg in sorted((REPO / "agents").glob("*/harness.json")):
            doc = json.loads(cfg.read_text())
            hid = doc.get("name") or ("llmops_" + cfg.parent.name.replace("-", "_"))
            docs[hid] = "".join(b.get("text", "") for b in (doc.get("systemPrompt") or []))

        # Which eval task produces the score is derived from the prompt, not assumed:
        # PR C split "evaluate" into evaluate+score and the producer moved once already.
        eval_text = docs["llmops_eval"]
        bullets = dict(re.findall(
            r'- \\?"([a-z_]+)\\?":(.*?)(?=\n- \\?"[a-z_]+\\?":|\nRules:|$)', eval_text, re.S))
        assert bullets, "could not isolate the eval task bullets -- the parse is broken"
        readers = {t for t, b in bullets.items() if "customer_eval_uri" in b}
        assert readers, (
            "no eval task names params.customer_eval_uri, so the gate is scored on the "
            f"val split whatever the plan says (eval declares {sorted(bullets)}). "
            "data-prep's curate reading it only proves the acceptance set was excluded "
            "from training, not that anything was ever measured against it.")
        # And the fallback must be conditional on absence, same reason as generate's.
        #
        # Scoped to the sentences that actually mention the val split. Searching the whole
        # bullet for /fall back/ made this half of the guard VACUOUS: the same bullet says
        # "never fall back to the newest artifact you can find in the bucket" about
        # eval_only's model_artifact_uri, which satisfied the regex no matter what the
        # prompt said about the two evaluation sets. Measured: control m189 -- which strips
        # the ranking out of the val-split sentence, the exact defect this line exists to
        # catch -- was UNCAUGHT, while the documented control count reported it passing.
        # A guard an unrelated sentence can satisfy is not a guard.
        body = "".join(bullets[t] for t in sorted(readers))
        val = [s for s in re.split(r"(?<=\.)\s+", body) if "val split" in s]
        assert val, (
            "the eval prompt no longer mentions the val split at all, so this guard cannot "
            "tell whether it is still ranked below the customer's set")
        assert any(re.search(r"fall\s*back|only when no customer", s, re.I) for s in val), (
            "the eval prompt names customer_eval_uri without stating that the val "
            "split is the FALLBACK; two eligible sets and no precedence means the "
            "score's provenance is decided per-run by the model. Sentences naming the "
            "val split: " + " | ".join(s.strip()[:120] for s in val))

    def test_every_acceptance_layer_is_decontaminated_and_its_count_recorded(self):
        """Both acceptance layers, not just the gated one.

        curate decontaminated against customer_eval_uri and named ood_eval_uri nowhere,
        while `_plan_params` flattens all of `plan.data` -- so the param ARRIVED at
        data-prep and no task read it. The gated layer is the one that looks like it
        matters and it is the one that needs this LEAST: contamination there inflates a
        number something checks, while contamination in the report-only layer fails
        nothing at all and simply reads HIGHER, which is the evidence someone would cite
        to say the student generalises. Both directions of "bigger student" and "synthesis
        closes the OOD gap" were refuted 0-3 in our own research pass, so the OOD number
        is the thing being measured, not a decoration.

        Measured with curate's own rule (prompt trigram-Jaccard >= 0.6) on the live files:
        0 of the 40 OOD rows overlap the 300-row source, max 0.1882, 23 OOD categories
        against 12 source categories with an empty intersection. So the layer is clean
        TODAY, by hand, and no artifact anywhere records that anyone checked -- which is
        the other half of this guard: a 0 that was written is a different fact from a 0
        nobody computed.

        The layers are derived from CUSTOMER_DATA_PARAMS rather than listed, so a third
        acceptance layer reds this instead of quietly skipping decontamination.
        """
        layers = sorted(p for p in CUSTOMER_DATA_PARAMS if p.endswith("_eval_uri"))
        assert len(layers) >= 2, (
            f"this guard derives the acceptance layers from CUSTOMER_DATA_PARAMS and found "
            f"{layers}; with fewer than two it cannot tell 'decontaminates every layer' "
            "from 'decontaminates the only layer there is'")
        doc = json.loads((REPO / "agents/data-prep/harness.json").read_text())
        text = "".join(b.get("text", "") for b in doc["systemPrompt"])
        bullet = re.search(r'- \\?"curate\\?":(.*?)(?=\n- \\?"[a-z_]+\\?":|\nRules:)',
                           text, re.S)
        assert bullet, "could not isolate the curate bullet -- the parse is broken"
        body = bullet.group(1)
        unchecked = [p for p in layers if p not in body]
        assert not unchecked, (
            "curate does not decontaminate the training corpus against "
            + ", ".join(f"params.{p}" for p in unchecked)
            + ". The param is flattened into params for every stage and no data-prep task "
            "reads it, so overlap between the training corpus and that acceptance layer is "
            "neither prevented nor visible afterwards.")
        assert "decontamination_dropped" in body and "one key per URI" in body, (
            "curate reports no per-layer drop count in stats.json; a single aggregate (or "
            "none) cannot distinguish a layer checked and found clean from a layer skipped")
        assert "escalate_human" in body, (
            "an unreadable acceptance URI must stop curate, not pass as a decontamination "
            "that dropped 0 rows -- those two outcomes write the same number")


#: A bedrock inference-profile id or a Hugging Face repo -- what a model
#: IDENTITY looks like, as opposed to a token count or an instance type.
_MODEL_ID = re.compile(r"^(?:(?:global|us|eu|apac)\.[\w-]+\.[\w.:-]+"
                       r"|[A-Za-z][\w.-]*/[\w.-]+)$")


def _fields_the_estimator_prices_models_from():
    """{plan field -> is it nested under `models`} for every model cost_model prices.

    Scraped from cost_model.py's own source, and a field is recognised by the model id
    it DEFAULTS to rather than by its name. Matching on names makes a guard blind in
    exactly the direction this bug travels: intersecting the estimator's field names
    with the dispatcher's own alias list means renaming the estimator's field to one
    the dispatcher does not know makes the mismatch invisible, and the guard green.
    A scrape that can only see fields both sides agree on cannot detect the two sides
    disagreeing. Measured: both directions escaped a name-matching version of this
    guard, which is why controls m161 and m163 exist.
    """
    src = (REPO / "pipeline/contracts/cost_model.py").read_text()
    lines = src.splitlines()
    priced = {}
    for i, line in enumerate(lines):
        if "= str(" not in line:
            continue
        stmt, j = line, i
        while stmt.count("(") > stmt.count(")") and j + 1 < len(lines):
            j += 1
            stmt += " " + lines[j].strip()
        if not [s for s in re.findall(r'"([^"]+)"', stmt) if _MODEL_ID.match(s)]:
            continue  # not a model id -- a token count or an instance type
        for field in re.findall(r'plan\.get\(\s*"([a-z_]+)"', stmt):
            if field != "models":
                priced.setdefault(field, False)
        for field in re.findall(r'\)\s*\.get\(\s*"([a-z_]+)"', stmt):
            priced[field] = True
    assert priced, ("no model-identity field found in cost_model.py -- either the "
                    "estimator stopped pricing per model, or this scrape broke; "
                    "either way the two paths are no longer being compared")
    return priced


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

    def test_every_model_param_a_prompt_reads_is_one_the_driver_supplies(self):
        """The prompts say "model id in params.teacher_model_id". NOTHING wrote it.

        start-pipeline resolves the signed plan into `manifest.models`; no prompt
        mentions `manifest.models` at all. So the agents read an absent param and fell
        back to the only model id in front of them -- the one hardcoded in their own
        persona line ("teacher DeepSeek-R1 on Bedrock"). That is boilerplate standing in
        for consent, which is the whole bug: the approval path resolved a teacher and
        the execution path never saw it.

        Derived in the direction that matters: every `params.*_model_id` any prompt
        reads must be a key the driver actually injects.

        The match is deliberately wider than `*_model_id` so a prompt reading
        `params.judge_model` cannot slip past it, which means it also catches model params
        that are legitimately NOT the driver's to inject. Those are named one at a time
        below with the authority that supplies them -- an exception list, not a loosened
        pattern, and each entry is checked to be real in both directions.
        """
        # A model param a SIGNED PLAN supplies. `model_artifact_uri` is not a model
        # identity `manifest.models` could resolve: it is the S3 location of an artifact a
        # PREVIOUS run already produced and paid for, and the only authority that can name
        # WHICH artifact a re-judge re-scores is the plan authorising that re-judge. The
        # driver injecting it would be the bug #20 shape inverted -- execution-path
        # boilerplate standing in for a human's choice of what is being measured.
        plan_supplied = {"model_artifact_uri"}
        for name in sorted(plan_supplied):
            assert name not in start_pipeline.PLAN_META_KEYS, (
                f"{name} is exempted here as plan-supplied but PLAN_META_KEYS strips it "
                "out of a plan's params, so nothing would reach the agent")
            assert any(f"params.{name}" in
                       json.loads(cfg.read_text())["systemPrompt"][0]["text"]
                       for cfg in (REPO / "agents").glob("*/harness.json")), (
                f"{name} is exempted here but no prompt reads it -- drop the exemption "
                "rather than leaving a hole shaped like a param that no longer exists")

        supplied = set(driver.MODEL_PARAM_FOR_ROLE.values())
        assert supplied, "the driver no longer injects any approved model param"
        for cfg in sorted((REPO / "agents").glob("*/harness.json")):
            text = json.loads(cfg.read_text())["systemPrompt"][0]["text"]
            read = set(re.findall(r"params\.([a-z_]*model[a-z_]*)", text))
            unsupplied = sorted(read - supplied - plan_supplied)
            assert not unsupplied, (
                f"{cfg.parent.name}: the prompt reads {unsupplied} but the driver "
                f"supplies only {sorted(supplied)}. An agent that reads an absent model "
                "param substitutes one it has seen, and the model a human approved is "
                "not the model that gets billed.")

    def test_the_driver_injects_the_manifest_models_under_the_prompt_names(self):
        """The consent recorded by start-pipeline has to reach the agent turn.

        Roles the manifest is silent about are OMITTED, not defaulted: a stage that
        needs a teacher and finds no param must fail visibly. A default here would
        recreate the bug one layer down."""
        got = driver.model_params_from_manifest(
            {"models": {"teacher": "global.anthropic.claude-fable-5",
                        "student": "meta-llama/Llama-3.2-1B"}})
        assert got == {"teacher_model_id": "global.anthropic.claude-fable-5",
                       "student_model_id": "meta-llama/Llama-3.2-1B"}, got
        assert driver.model_params_from_manifest({}) == {}
        assert driver.model_params_from_manifest({"models": {}}) == {}
        # A junk manifest must not crash the stage that reads it.
        assert driver.model_params_from_manifest({"models": "deepseek"}) == {}

    @staticmethod
    def _stage_params(event, models, manifest=None):
        """The params the REAL `_run_stage` builds, for a manifest holding `models`.

        Drives the actual function rather than recomputing its merge: an assertion on a
        dict this test built itself passes no matter what the driver does, which is how
        the "two correct halves, never connected" shape of this bug survives tests.

        `manifest` supplies the REST of the document (notably `stages`, which carries the
        facts earlier stages of the run reported) so the same real code path can be driven
        for prior-stage facts as for signed models. `models` still wins over
        `manifest["models"]` so every existing caller keeps its meaning.
        """
        seen = {}
        doc = {**(manifest or {}), "models": models}

        class _S3:
            def get_object(self, Bucket, Key):
                return {"Body": io.BytesIO(json.dumps(doc).encode())}

        def _fake_user_text(text):
            seen.update(json.loads(text))
            raise RuntimeError("stop once the payload has been built")

        real = driver._user_text
        driver._user_text = _fake_user_text
        try:
            driver._run_stage(event, c={"s3": _S3()})
        except RuntimeError:
            pass
        finally:
            driver._user_text = real
        return seen.get("params", {})

    def test_a_stage_payload_carries_the_approved_models(self):
        """Wiring test, not a unit test: the resolver and the injector were both correct
        in the previous bug too, and the defect was that nothing connected them."""
        params = self._stage_params(
            {"run_id": "run-x", "stage": "data-prep", "task": "generate",
             "harness_id": "llmops_data_prep",
             "manifest_uri": "s3://b/runs/run-x/manifest.json"},
            {"teacher": "global.anthropic.claude-fable-5",
             "student": "meta-llama/Llama-3.2-1B"})
        assert params.get("teacher_model_id") == "global.anthropic.claude-fable-5", (
            f"the stage payload does not carry the approved teacher: {params}")
        assert params.get("student_model_id") == "meta-llama/Llama-3.2-1B"
        assert params.get("task") == "generate", "the task must still be there"

    def test_a_caller_supplied_model_overrides_the_manifest_but_is_not_the_default(self):
        """A remediation iteration may legitimately name a different model, so an
        explicit event param still wins. What must NOT happen is the reverse: the
        manifest being ignored whenever the event says nothing.

        Both directions are asserted through the real `_run_stage`, because the merge
        ORDER is the whole content of this test -- and a merge order restated in the
        test body is satisfied by any order in the code."""
        base = {"run_id": "run-x", "stage": "data-prep", "task": "generate",
                "harness_id": "llmops_data_prep",
                "manifest_uri": "s3://b/runs/run-x/manifest.json"}
        approved = {"teacher": "global.anthropic.claude-fable-5"}
        override = self._stage_params(
            {**base, "params": {"teacher_model_id": "us.deepseek.r1-v1:0"}}, approved)
        assert override.get("teacher_model_id") == "us.deepseek.r1-v1:0", (
            "an explicit caller override must win: a remediation iteration that names a "
            f"model is a deliberate act, and the driver overrode it -> {override}")
        silent = self._stage_params(base, approved)
        assert silent.get("teacher_model_id") == "global.anthropic.claude-fable-5", (
            "with the event silent the manifest's approved model must be supplied — "
            "otherwise the agent falls back to whatever its prompt names")

    #: Params a prompt reads that something OTHER than the signed plan supplies, so the
    #: derivation below does not demand the manifest carry them. Each is delivered by a
    #: mechanism with its own test above: the task and iteration by the dispatching
    #: event, the model roles by MODEL_PARAM_FOR_ROLE, the endpoint by STAGE_FACT_PARAMS,
    #: and the conductor's two triage keys by the bus event that wakes it.
    _NOT_FROM_THE_PLAN = frozenset({"task", "iteration", "escalation", "approval_context"})

    @classmethod
    def _plan_only_params(cls) -> set:
        """Params the prompts read that ONLY `manifest["params"]` can supply."""
        read = set()
        for cfg in sorted((REPO / "agents").glob("*/harness.json")):
            text = json.loads(cfg.read_text())["systemPrompt"][0]["text"]
            read |= set(re.findall(r"params\.([a-z_][a-z_0-9]*)", text))
        assert read, "parsed no params at all -- the derivation is broken, not the code"
        out = (read - cls._NOT_FROM_THE_PLAN
               - set(driver.MODEL_PARAM_FOR_ROLE.values())
               - set(driver.STAGE_FACT_PARAMS))
        assert len(out) > 10, f"only {sorted(out)} left; the exclusions have eaten the set"
        return out

    def test_the_settings_a_human_signed_reach_the_agent_that_reads_them(self):
        """#20/#21/#22 one level up, and the reason it hid for so long.

        start-pipeline merges DEFAULT_PARAMS < trigger params < signed plan into
        `manifest.params` -- 19 keys on the r5 run. The payload the driver handed the
        agent carried SIX. The other 23 existed only in the manifest, under a key ALSO
        spelled `params`, so `params.gates` meant two different things depending on which
        document you were holding.

        It worked anyway: the eval prompt orders the agent to read the manifest first, so
        it found the other `params` and used it -- r5's gate-i1.json says so outright
        ("invocation params carried no gates key, manifest is source of truth") and
        applied the signed 0.55. But the same prompt also says "if params.gates is absent
        entirely, do NOT invent a threshold: escalate_human", and by the payload's literal
        `params` that was TRUE. The only thing between a correctly signed plan and a
        spurious escalation was the agent noticing the collision for us.

        Derived from the prompts, so a `params.X` added to a prompt tomorrow is covered
        without editing this test -- the failure mode being guarded is precisely a prompt
        promising a param that no half of the system delivers.
        """
        wanted = sorted(self._plan_only_params())
        signed = {p: f"signed-value-for-{p}" for p in wanted}
        delivered = self._stage_params(
            {"run_id": "run-x", "stage": "eval", "task": "gate",
             "harness_id": "llmops_eval",
             "manifest_uri": "s3://b/runs/run-x/manifest.json"},
            {}, manifest={"params": signed})
        missing = [p for p in wanted if delivered.get(p) != signed[p]]
        assert not missing, (
            f"the prompts read params.{{{','.join(missing)}}} and the payload the agent "
            f"receives does not carry them, so every one of those sentences describes a "
            f"key that is absent. Delivered: {sorted(delivered)}")

    def test_a_signed_plan_setting_never_overwrites_a_fact_the_run_discovered(self):
        """The merge is additive BY CONSTRUCTION or it is a regression.

        `manifest.params` is merged at the lowest precedence, so every key that resolved
        before it was merged at all must still resolve the same way. Asserted on the three
        sources that can collide with it -- a stale plan value silently replacing the
        endpoint the deploy stage actually created is the failure this ordering prevents,
        and it would look exactly like a monitor sweep reporting metrics for the wrong
        model: evidence, not an error."""
        base = {"run_id": "run-x", "stage": "eval", "task": "score",
                "harness_id": "llmops_eval",
                "manifest_uri": "s3://b/runs/run-x/manifest.json"}
        stale = {"teacher_model_id": "plan-stale-teacher",
                 "student_endpoint": "plan-stale-endpoint",
                 "task": "plan-stale-task"}
        params = self._stage_params(
            {**base, "params": {"teacher_model_id": "caller-override"}},
            {"teacher": "signed-teacher"},
            manifest={"params": stale,
                      "stages": {"deploy": {"metrics": {"endpoint_name": "real-ep"}}}})
        assert params["teacher_model_id"] == "caller-override", (
            "the dispatching caller's override must still win over both the manifest's "
            f"models and its params -> {params}")
        assert params["student_endpoint"] == "real-ep", (
            "the endpoint the deploy stage REPORTED must win over a plan setting of the "
            f"same name; no plan can be signed with an endpoint that did not exist -> {params}")
        assert params["task"] == "score", (
            f"the dispatched task must survive a plan param of the same name -> {params}")
        silent = self._stage_params(base, {"teacher": "signed-teacher"},
                                   manifest={"params": stale})
        assert silent["teacher_model_id"] == "signed-teacher", (
            "with the caller silent, the SIGNED model must still beat a params entry of "
            f"the same name -- model consent does not come from a settings dict -> {silent}")

    def test_no_prompt_hardcodes_a_model_id_as_the_one_to_use(self):
        """Every prompt's persona line used to name DeepSeek-R1 and Qwen3-1.7B, which is
        what an agent falls back to when its model param is absent. The platform has to
        run a customer's own open-weight distillation and a YOLO fine-tune, so a model
        id in a prompt is either dead weight or a wrong default."""
        banned = ("DeepSeek-R1", "Qwen3-1.7B", "Qwen/Qwen3", "us.deepseek")
        for cfg in sorted((REPO / "agents").glob("*/harness.json")):
            text = json.loads(cfg.read_text())["systemPrompt"][0]["text"]
            # Scoped to the persona line and the task list -- the part that tells the
            # agent WHAT TO USE. finops's prompt names DeepSeek-R1 inside a measured
            # finding about the AWS Price List API ("it CANNOT price Claude Fable 5"),
            # which is a fact about a pricing source, not an instruction to use a model.
            # A file-wide ban would force deleting that measurement to satisfy a guard
            # about defaults.
            parts = re.split(r"\nRules\b", text, maxsplit=1)
            assert len(parts) == 2, (
                f"{cfg.parent.name}: no 'Rules' section, so this guard cannot tell the "
                "instructions from the measured findings and would silently check "
                "nothing. Restore the section or narrow the scope deliberately.")
            directive = parts[0]
            for term in banned:
                assert term not in directive, (
                    f"{cfg.parent.name}: the prompt still names {term!r} where it says "
                    "what to use. That is the id an agent substitutes when "
                    "params.*_model_id is missing, so it reads as the default rather "
                    "than as an example.")

    def test_every_param_a_prompt_reads_has_something_that_writes_it(self):
        """The generalised form of bugs #20, #21 and #22 — derived, so it finds the NEXT one.

        Three consecutive bugs were one shape: a prompt reads `params.X`, some other half
        of the system knows X, and nothing connects them. Each was found by hand, one param
        at a time. This enumerates every `params.X` any of the 7 prompts reads and asserts
        that at least one writer exists for it, so instance #6 fails here instead of in a
        run nobody can explain.

        The four writers, which are the complete set:

        - `DEFAULT_PARAMS` in start-pipeline (a run nobody planned)
        - a signed plan, via `_plan_params` minus `PLAN_META_KEYS` (a run a human designed)
        - the driver, from the manifest: `MODEL_PARAM_FOR_ROLE` (what was approved) and
          `STAGE_FACT_PARAMS` (what an earlier stage of this run discovered)
        - the dispatch event itself, for the few params that only exist at dispatch time

        Every read param is classified EXPLICITLY below, and the classification is pinned:
        a param appearing in a prompt without an entry here fails, and an entry for a param
        no prompt reads fails too. That pin is the point. The first version of this guard
        allowed "a plan may carry it" to satisfy the assertion, which made it load-bearing
        on exactly ONE of the 25 params -- `student_endpoint` would have passed with the
        driver's injection deleted, because a plan is not barred from naming it. A plan
        being *permitted* to carry a field is not the same as anything *writing* it, and an
        endpoint name is a fact no plan can be signed with.

        So each category is checked against the mechanism that actually supplies it:
        `default` against `DEFAULT_PARAMS`, `model` against `MODEL_PARAM_FOR_ROLE`,
        `stage_fact` against `STAGE_FACT_PARAMS`, and `plan` against `PLAN_META_KEYS` (the
        denylist that would bar it). `dispatch` is the escape hatch -- keep it short, and
        every name in it must be a value that cannot exist before the dispatch that carries
        it, not merely one nobody has wired yet.
        """
        WRITER = {
            # dispatch-time values: they do not exist before the invocation carries them
            "task": "dispatch",            # which task of the stage this invocation is
            "iteration": "dispatch",       # remediation loop counter, from the ASL
            "escalation": "dispatch",      # the EscalatedToHuman event being triaged
            "goal": "dispatch",            # the human's request, for a conductor task
            "plan_uri": "dispatch",        # where to write (or read) this task's plan
            "rate_card": "dispatch",       # read fresh at invoke; a stale card misquotes
            "report_uri": "dispatch",      # where an ops report is to be written
            "sweep_uri": "dispatch",       # monitor_sweep's own dispatch writes it
            "budget_usd": "dispatch",      # advisory and explicitly optional ("if given"):
                                           # no structured field captures it, the customer
                                           # states it in prose in the goal
            # facts an earlier stage of THIS run discovered (bug #22)
            "student_endpoint": "stage_fact",
            # the models a human signed, injected by the driver (bug #20)
            "teacher_model_id": "model",
            "student_model_id": "model",
            # settings with a fallback for a run nobody planned
            "gates": "default",
            "keep_reasoning": "default",
            "training_instance": "default",
            "inference_instance": "default",
            # How many rows the corpus should end up with. Read by data-prep's generate
            # since the bug #23 cure, which needs it to decide whether the customer's
            # file is short of what the plan priced and the teacher must top it up.
            "sample_count": "default",
            # settings only a signed plan supplies (bug #21)
            "source_uri": "plan",
            "customer_eval_uri": "plan",
            "ood_eval_uri": "plan",
            "domain": "plan",
            "sample_size": "plan",
            "hf_token_secret": "plan",
            "keep_endpoint": "plan",
            "latency_p50_target_ms": "plan",
            "pipeline_mode": "plan",
            "variance_threshold_pct": "plan",
            # eval_only's two prerequisites. Only a plan can name WHICH artifact a re-judge
            # re-scores, and start-pipeline refuses the dispatch outright when either is
            # missing (MODE_REQUIRED_PARAMS) rather than starting a run whose only legal
            # ending is an escalation.
            "model_artifact_uri": "plan",
            "source_run_id": "plan",
        }
        read = {}
        for cfg in sorted((REPO / "agents").glob("*/harness.json")):
            text = json.loads(cfg.read_text())["systemPrompt"][0]["text"]
            for param in set(re.findall(r"params\.([a-z_][a-z_0-9]*)", text)):
                read.setdefault(param, set()).add(cfg.parent.name)
        assert read, "this guard parsed no params at all, so it is checking nothing"

        unclassified = sorted(set(read) - set(WRITER))
        assert not unclassified, (
            f"prompts read {unclassified} and nothing here says what writes them. Triage "
            "each one: bugs #20, #21 and #22 were all a prompt reading a param no half of "
            "the system delivered, and each was found by hand after a run had already "
            "spent money. Add it to WRITER with the mechanism that supplies it.")
        stale = sorted(set(WRITER) - set(read))
        assert not stale, (
            f"{stale} is classified here but no prompt reads it. Either a prompt lost the "
            "param (so whatever writes it is now dead wiring) or this list drifted.")

        for param, kind in sorted(WRITER.items()):
            where = sorted(read[param])
            if kind == "default":
                assert param in start_pipeline.DEFAULT_PARAMS, (
                    f"params.{param} (read by {where}) is classified as having a default, "
                    f"but DEFAULT_PARAMS does not define it.")
            elif kind == "model":
                assert param in set(driver.MODEL_PARAM_FOR_ROLE.values()), (
                    f"params.{param} (read by {where}) is the model consent a human "
                    "signed, but the driver no longer injects it -- so the agent falls "
                    "back to the model named in its own persona line (#20).")
            elif kind == "stage_fact":
                assert param in driver.STAGE_FACT_PARAMS, (
                    f"params.{param} (read by {where}) is a fact an earlier stage of the "
                    "run produced, and the driver no longer carries it forward. No plan "
                    "can be signed with it and no default can stand in for it, so the "
                    "agent must guess or refuse (#22).")
            elif kind == "plan":
                assert param not in start_pipeline.PLAN_META_KEYS, (
                    f"params.{param} (read by {where}) is supplied only by a signed plan, "
                    "and PLAN_META_KEYS now excludes it from the params a plan carries. "
                    "Nothing else writes it, so the stage runs on a value no human chose.")
            else:
                assert kind == "dispatch", f"unknown writer kind {kind!r}"

    @staticmethod
    def _drive_stage_complete(manifest, metrics, *, store=None, run_id="run-1", s3=None,
                              stage="deploy", task="deploy", iteration=None,
                              evidence="InService"):
        """Drive the REAL `handle_stage_complete` and return (result, S3 store).

        The whole content of bug #22 is whether the assembled `stages` entry reaches S3, so
        this asserts on the bytes a fake S3 was HANDED rather than on a dict the test built.

        `s3` overrides the default fake for the cases whose whole content is which write
        fails: #25 was one refused PutObject silently suppressing a second, permitted one,
        and a fake that accepts every key cannot show that.
        """
        key = f"runs/{run_id}/manifest.json"
        out = {f"runs/{run_id}/deploy/endpoint.json": b"{}"} if store is None else store
        if manifest is not None:
            out[key] = json.dumps(manifest).encode()

        class _S3:
            def get_object(self, Bucket, Key):
                if Key not in out:
                    raise RuntimeError("NoSuchKey")
                return {"Body": io.BytesIO(out[Key])}

            def put_object(self, Bucket, Key, Body, **kw):
                out[Key] = Body

            def head_object(self, Bucket, Key):
                if Key not in out:
                    raise RuntimeError("404")
                return {}

        class _DDB:
            def Table(self, name):
                class _T:
                    def put_item(self, **kw):
                        pass

                    def update_item(self, **kw):
                        return {}
                return _T()

        class _EV:
            def put_events(self, **kw):
                return {"FailedEntryCount": 0}

        for k, v in {"DATA_BUCKET": "b", "RUNS_TABLE": "r", "EVENT_BUS": "e",
                     "EVENTS_TABLE": "ev"}.items():
            os.environ[k] = v
        event = {"run_id": run_id, "stage": stage, "task": task,
                 "manifest_uri": f"s3://b/{key}"}
        if iteration is not None:
            event["iteration"] = iteration
        res = driver.handle_stage_complete(
            {"s3": s3 or _S3(), "ddb": _DDB(), "events": _EV(), "sfn": None},
            event,
            {"stage": stage,
             "outputs": [f"s3://b/runs/{run_id}/deploy/endpoint.json"],
             "metrics": metrics, "evidence": evidence})
        return res, out

    @staticmethod
    def _signed_manifest():
        """A freshly built signed manifest per call, so no test can mutate another's."""
        return {"run_id": "run-1", "created_at": "2026-08-10T00:00:00Z",
                "trigger_source": "conductor",
                "models": {"student": "Qwen/Qwen3-1.7B"},
                "plan": {"budget_usd": 450, "sample_size": 40000},
                "approval": {"approved_by": "tim"},
                "params": {"gates": {"map50": 0.75}}, "stages": {}}

    def test_a_completed_stages_results_persist_to_the_manifest(self):
        """Bug #22. The driver assembled `stages[stage]` into a LOCAL variable, handed it to
        `write_run_report`, and dropped it -- it had no `put_object` for the manifest at all.

        Measured before the fix: after a deploy stage reported
        `metrics.endpoint_name=llmops-student-run-1`, `manifest.stages` was still `{}`, while
        the run REPORT carried every output and metric. So the write reached the document
        humans read and not the one every prompt calls "the single source of truth".

        That is worse than a stale field. A stage cannot see what the stage before it
        produced, so an agent asked to diagnose a run has only its own turn to look at -- a
        pipeline whose stages cannot read each other's results cannot iterate on a run, it
        can only redo it."""
        res, store = self._drive_stage_complete(
            self._signed_manifest(), {"endpoint_name": "llmops-student-run-1"})
        assert res["ok"] and not res.get("report_error"), res
        saved = json.loads(store["runs/run-1/manifest.json"])
        entry = saved.get("stages", {}).get("deploy")
        assert entry, (
            "the deploy stage completed and the manifest's `stages` block is still empty: "
            f"{saved.get('stages')!r}. Nothing downstream can learn what this stage did.")
        assert entry["status"] == "completed"
        assert entry["metrics"]["endpoint_name"] == "llmops-student-run-1", entry
        assert entry["outputs"] == ["s3://b/runs/run-1/deploy/endpoint.json"], entry

    def test_both_iterations_of_a_remediated_stage_survive_in_the_manifest(self):
        """The remediation loop was overwriting the "before" half of its own answer.

        `stages` is keyed by STAGE NAME, so the second iteration's finetune entry replaces
        the first's -- and the loop exists precisely to answer "did the one targeted change
        help?". Measured on r5 (run-20260811T101948Z-f9d34d27, 2 iterations): the surviving
        manifest holds `stages.finetune.metrics.iteration == 1` and an `eval` entry carrying
        `delta_judge_win_rate_vs_i0` with no iteration-0 row left to subtract from. The i0
        training losses and judge counts existed nowhere in the manifest; they survived only
        because the eval agent happened to archive report-i0.json by hand.

        Asserted on the bytes a fake S3 was handed, across two REAL invocations sharing one
        store, because a lost update is only visible in the second write's result.
        """
        store = {"runs/run-1/deploy/endpoint.json": b"{}"}
        _, store = self._drive_stage_complete(
            self._signed_manifest(), {"final_eval_loss": 1.5, "iteration": 0},
            store=store, stage="finetune", task="analyze", iteration=0,
            evidence="baseline run")
        res, store = self._drive_stage_complete(
            None, {"final_eval_loss": 0.9, "iteration": 1},
            store=store, stage="finetune", task="analyze", iteration=1,
            evidence="post-remediation")
        assert res["ok"] and not res.get("report_error"), res
        saved = json.loads(store["runs/run-1/manifest.json"])

        # The stage pointer still holds the LATEST entry -- unchanged on purpose, because
        # stage_fact_params and every specialist prompt read manifest.stages[<stage>] by
        # bare stage name and must keep seeing the current result.
        assert saved["stages"]["finetune"]["metrics"]["final_eval_loss"] == 0.9

        history = saved.get("stage_history")
        assert isinstance(history, list), (
            "the manifest carries no stage_history, so the only record of a stage is the "
            "one entry under its own name and iteration 1 has erased iteration 0")
        losses = [(h.get("iteration"), h["metrics"].get("final_eval_loss"))
                  for h in history if h.get("stage") == "finetune"]
        assert losses == [(0, 1.5), (1, 0.9)], (
            f"both iterations of finetune must be recoverable in order, got {losses!r}. "
            "Without the i0 row nothing can say whether the remediation helped.")
        assert [h["evidence"] for h in history] == ["baseline run", "post-remediation"], \
            "each iteration keeps its OWN evidence text, not the last one twice"
        assert all(h.get("recorded_at") for h in history), \
            "an append-only record with no timestamp cannot be ordered against the run's log"

    def test_the_durable_history_is_appended_to_the_copy_on_s3(self):
        """A record that landed while the driver was working must not be dropped.

        `stages` is replaced wholesale from the caller's snapshot, so anything written into
        it during the turn is lost -- a real but bounded gap this function's docstring
        already admits. `stage_history` must not inherit that gap, because it is the ONLY
        place a per-iteration result survives: losing a record there loses it permanently,
        while losing a `stages` entry loses a duplicate of the latest one.

        So the append goes onto the list just re-read from S3. Simulated by handing
        `_save_manifest` a caller snapshot that does not contain a record the S3 copy does.
        """
        landed = {"stage": "eval", "task": "score", "iteration": 0, "status": "completed",
                  "metrics": {"judge_win_rate": 0.0}, "recorded_at": "2026-08-11T10:00:00Z"}
        on_s3 = self._signed_manifest()
        on_s3["stage_history"] = [landed]
        store = {"runs/run-1/manifest.json": json.dumps(on_s3).encode()}
        snapshot = self._signed_manifest()          # loaded BEFORE `landed` was written
        assert "stage_history" not in snapshot

        class _S3:
            def get_object(self, Bucket, Key):
                return {"Body": io.BytesIO(store[Key])}

            def put_object(self, Bucket, Key, Body, **kw):
                store[Key] = Body

        record = {"stage": "eval", "task": "gate", "iteration": 1, "status": "completed",
                  "metrics": {"gate_passed": False}, "recorded_at": "2026-08-11T12:00:00Z"}
        driver._save_manifest(_S3(), "s3://b/runs/run-1/manifest.json", snapshot,
                              history_record=record)
        saved = json.loads(store["runs/run-1/manifest.json"])
        assert saved["stage_history"] == [landed, record], (
            "the record on S3 was overwritten by the driver's stale snapshot: "
            f"{saved.get('stage_history')!r}")

    def test_a_completion_appends_exactly_one_history_record(self):
        """The record is appended to two dicts and must reach S3 once.

        `handle_stage_complete` appends to its own copy (that copy is what
        `write_run_report` reads) and ALSO hands the record to `_save_manifest`, which
        appends to the copy it re-reads. Both are needed -- the report would otherwise omit
        the stage that just finished, and the durable list would otherwise inherit the
        snapshot race -- so the one thing that must be checked is that the two appends do
        not become two rows.
        """
        _, store = self._drive_stage_complete(
            self._signed_manifest(), {"endpoint_name": "llmops-student-run-1"})
        saved = json.loads(store["runs/run-1/manifest.json"])
        history = saved["stage_history"]
        assert len(history) == 1, f"one completion, {len(history)} rows: {history!r}"
        report = json.loads(store["reports/run-1/test-report.json"])
        assert report["stage_history"] == history, (
            "the report a human opens must show the same history as the manifest; "
            f"report={report.get('stage_history')!r}")

    # ---- the judge instrument is checked, not taken on trust (#D4) ----------------
    #
    # deploy/03_storage.py mirrors the canonical pairwise judge prompt and says "report.json
    # carries judge_prompt_sha256, and comparing two runs is then a digest comparison rather
    # than an argument" -- and nothing compared it to anything. The digest was a string the
    # scoring agent wrote beside its own score, so it had exactly the standing of the score
    # it was supposed to corroborate. r5 is the failure: the eval prompt said "fixed judge
    # prompts" and fixed none, that run authored its own A-or-B-only instrument, and its
    # `judge_ties: 0` was read as a property of the student when it was a property of a
    # prompt that offered no tie.

    #: The canonical instrument's bytes, so a test can compute the digest the driver must
    #: arrive at rather than hardcoding one -- a pinned digest would turn every future edit
    #: of pipeline/eval/judge_prompt_pairwise.md into a red test about nothing.
    INSTRUMENT = b"# Pairwise judge\nAnswer A or B or tie.\n"

    @classmethod
    def _instrument_store(cls, run_id="run-1", body=None):
        store = {f"runs/{run_id}/deploy/endpoint.json": b"{}"}
        if body is not None:
            store[driver.JUDGE_PROMPT_KEY] = body
        return store

    def test_a_score_claiming_the_canonical_instrument_is_checked_against_its_bytes(self):
        digest = hashlib.sha256(self.INSTRUMENT).hexdigest()
        _, store = self._drive_stage_complete(
            self._signed_manifest(),
            {"judge_score": 0.48, "judge_n": 120, "judge_prompt_sha256": digest},
            store=self._instrument_store(body=self.INSTRUMENT),
            stage="eval", task="score")
        saved = json.loads(store["runs/run-1/manifest.json"])
        att = saved["attestations"]
        assert len(att) == 1 and att[0]["verified"] is True, f"{att!r}"
        # The value a human compares two runs on, so it is asserted here even though the
        # assertion is redundant in THIS case: with a correct claim, a digest derived from
        # the object and one echoed back from the claim are byte-identical, and `verified is
        # True` above kills every code mutant either way. Derivation is pinned in
        # test_a_score_that_names_no_instrument_is_reported_as_such, where `verified` is
        # False regardless and this is the only line that can tell the two apart.
        assert att[0]["canonical_sha256"] == digest, f"{att[0]!r}"
        report = json.loads(store["reports/run-1/test-report.json"])
        assert report["findings"] == [], f"a verified instrument is not a finding: {report['findings']!r}"
        assert report["attestations"] == att, (
            "a PASSED attestation is the digest two runs are compared on, so the report "
            "must carry it too, not only the failures")

    def test_a_self_authored_instrument_is_a_high_finding_not_a_passing_score(self):
        """r5's defect, reproduced: a digest that is not the canonical object's."""
        _, store = self._drive_stage_complete(
            self._signed_manifest(),
            {"judge_score": 0.61, "judge_ties": 0, "judge_prompt_sha256": "d" * 64},
            store=self._instrument_store(body=self.INSTRUMENT),
            stage="eval", task="score")
        saved = json.loads(store["runs/run-1/manifest.json"])
        assert saved["attestations"][0]["verified"] is False
        report = json.loads(store["reports/run-1/test-report.json"])
        high = [f for f in report["findings"] if f["severity"] == "high"]
        assert len(high) == 1, f"{report['findings']!r}"
        assert "d" * 64 in high[0]["detail"] and \
               hashlib.sha256(self.INSTRUMENT).hexdigest() in high[0]["detail"], (
            "the finding must print BOTH digests: 'not comparable' without the two values "
            "is a claim the reader has to re-derive to act on")
        assert "not comparable" in high[0]["detail"]

    def test_a_score_that_names_no_instrument_is_reported_as_such(self):
        """Distinct from a mismatch, and NOT repaired by stamping the canonical digest.

        Stamping would convert an absent attestation into a false one -- asserting the
        canonical instrument for a score that may have been produced with another.
        """
        _, store = self._drive_stage_complete(
            self._signed_manifest(), {"judge_score": 0.48, "judge_n": 120},
            store=self._instrument_store(body=self.INSTRUMENT),
            stage="eval", task="score")
        saved = json.loads(store["runs/run-1/manifest.json"])
        assert saved["attestations"][0]["claimed_sha256"] == "", (
            "the driver filled in the digest the agent did not report, which is a claim "
            "nobody made")
        # Where the derivation is actually pinned. In the MATCHING case the same assertion
        # is redundant -- a record that echoed the claim back is byte-identical to one
        # derived from the object when the claim is correct -- and `verified is True` kills
        # every code mutant there anyway. Here `verified` is False either way, so this line
        # is the only thing that can tell "hashed the object" from "hashed something else".
        assert saved["attestations"][0]["canonical_sha256"] == \
            hashlib.sha256(self.INSTRUMENT).hexdigest(), (
            "the canonical digest must come from the object's bytes; a run whose report "
            "carries some other hash gives two runs nothing to compare")
        report = json.loads(store["reports/run-1/test-report.json"])
        assert [f["severity"] for f in report["findings"]] == ["medium"], \
            f"{report['findings']!r}"
        assert "does not name its instrument" in report["findings"][0]["title"]

    def test_an_unmirrored_instrument_says_the_check_did_not_run(self):
        """Today's live state: `code/eval/` does not exist in the data bucket, because the
        mirror ships in the same change as the prompt clause that reads it. A check that
        could not run must not read as a check that passed -- and must not read as a
        mismatch either, which would blame the run for the deploy's gap.
        """
        _, store = self._drive_stage_complete(
            self._signed_manifest(),
            {"judge_score": 0.48, "judge_prompt_sha256": "a" * 64},
            store=self._instrument_store(), stage="eval", task="score")
        saved = json.loads(store["runs/run-1/manifest.json"])
        att = saved["attestations"][0]
        assert att["verified"] is False and att["error"], f"{att!r}"
        assert att["canonical_sha256"] == "", (
            "there was nothing to hash, so a digest here would be invented")
        report = json.loads(store["reports/run-1/test-report.json"])
        assert [f["severity"] for f in report["findings"]] == ["medium"], \
            f"{report['findings']!r}"
        assert "could not be verified" in report["findings"][0]["title"]
        assert "03_storage" in report["findings"][0]["detail"], (
            "a finding about a missing deploy artifact should name the deploy step that "
            "would fix it")

    def test_a_stage_making_no_judge_claim_is_not_asked_to_attest(self):
        """The attestation is owed by whoever claims to have judged, and a deploy stage
        does not. An unconditional check would file a failed attestation against every
        stage of every run, which is how a real finding stops being read."""
        _, store = self._drive_stage_complete(
            self._signed_manifest(), {"endpoint_name": "llmops-student-run-1"},
            store=self._instrument_store(body=self.INSTRUMENT))
        saved = json.loads(store["runs/run-1/manifest.json"])
        assert "attestations" not in saved, f"{saved.get('attestations')!r}"
        report = json.loads(store["reports/run-1/test-report.json"])
        assert report["findings"] == [], f"{report['findings']!r}"

    def test_the_claim_is_recognised_by_the_judge_numbers_not_by_the_task_name(self):
        """The eval prompt lets a small enough prompt set be scored inside the "evaluate"
        task instead of "score", so a (stage, task) name list would quietly stop checking
        the run that took that branch. Derived from the claim instead."""
        _, store = self._drive_stage_complete(
            self._signed_manifest(),
            {"judge_wins": 12, "judge_losses": 28, "judge_ties": 0},
            store=self._instrument_store(body=self.INSTRUMENT),
            stage="eval", task="evaluate")
        saved = json.loads(store["runs/run-1/manifest.json"])
        assert saved["attestations"], (
            "a stage that reported judge counts was not asked which instrument produced "
            "them, because its task was spelled 'evaluate'")

    def test_one_unverified_instrument_is_one_finding_however_many_stages_echo_it(self):
        """The gate task reads report.json and can echo the score task's digest. Two records
        of one claim is still one unverified instrument, and a reader told twice starts
        discounting the telling."""
        report = build_run_report({"run_id": "run-1", "stages": {}, "attestations": [
            {"kind": "judge_instrument", "stage": "eval", "claimed_sha256": "b" * 64,
             "canonical_sha256": "c" * 64, "verified": False},
            {"kind": "judge_instrument", "stage": "eval", "claimed_sha256": "b" * 64,
             "canonical_sha256": "c" * 64, "verified": False},
        ]})
        assert len(report["findings"]) == 1, f"{report['findings']!r}"

    def test_a_stage_write_cannot_restate_the_signed_blocks(self):
        """The write-back is narrowed to `stages`, so a driver bug cannot rewrite consent.

        Bugs #9, #20 and #21 were one defect -- a default standing in for intent that WAS
        present in a signed artifact. Writing the manifest back from the driver, on every
        stage_complete, from data assembled around an agent's tool call, is exactly the shape
        that reintroduces it a fourth time. So the driver re-reads and replaces ONLY `stages`,
        and this drives the real `_save_manifest` with a tampered copy to prove the copy on
        S3 wins."""
        on_s3 = self._signed_manifest()
        store = {"runs/run-1/manifest.json": json.dumps(on_s3).encode()}
        tampered = self._signed_manifest()
        tampered["models"] = {"student": "somebody-elses/model"}
        tampered["approval"] = {"approved_by": "nobody"}
        tampered["plan"] = {"budget_usd": 999999}
        tampered["stages"] = {"deploy": {"status": "completed"}}

        class _S3:
            def get_object(self, Bucket, Key):
                return {"Body": io.BytesIO(store[Key])}

            def put_object(self, Bucket, Key, Body, **kw):
                store[Key] = Body

        driver._save_manifest(_S3(), "s3://b/runs/run-1/manifest.json", tampered)
        saved = json.loads(store["runs/run-1/manifest.json"])
        for block in sorted(driver.IMMUTABLE_MANIFEST_KEYS):
            if block in on_s3:
                assert saved[block] == on_s3[block], (
                    f"a stage write changed the signed block {block!r}: "
                    f"{on_s3[block]!r} -> {saved[block]!r}. The driver must never be able "
                    "to restate what a human signed.")
        assert saved["stages"] == {"deploy": {"status": "completed"}}, (
            "the stage results are what this write is FOR and they did not land")

    def test_a_concurrent_agent_write_survives_the_drivers_stage_write(self):
        """Read-modify-write, not a blind put, because the driver is the SECOND writer.

        5 of the 7 specialist prompts say "read it first, append your results to it, never
        overwrite other stages' entries", and the harness role really can: `S3PipelineObjects`
        grants `s3:PutObject` on `runs/*`. So a blind put of the copy loaded at the top of
        `handle_stage_complete` would silently erase the human-readable stage summary the
        prompt just asked the agent to write."""
        store = {"runs/run-1/manifest.json": json.dumps(
            self._signed_manifest()).encode(),
            "runs/run-1/deploy/endpoint.json": b"{}"}
        stale = json.loads(store["runs/run-1/manifest.json"])

        # The agent writes its own note WHILE the stage runs, after the driver's copy was
        # loaded. S3 has no compare-and-swap, so the guarantee is scoped: keys the driver
        # does not write must survive.
        concurrent = json.loads(store["runs/run-1/manifest.json"])
        concurrent["agent_notes"] = {"deploy": "merged adapters, endpoint InService"}
        store["runs/run-1/manifest.json"] = json.dumps(concurrent).encode()

        stale["stages"] = {"deploy": {"status": "completed", "outputs": [],
                                      "metrics": {}, "evidence": ""}}

        class _S3:
            def get_object(self, Bucket, Key):
                return {"Body": io.BytesIO(store[Key])}

            def put_object(self, Bucket, Key, Body, **kw):
                store[Key] = Body

        driver._save_manifest(_S3(), "s3://b/runs/run-1/manifest.json", stale)
        saved = json.loads(store["runs/run-1/manifest.json"])
        assert saved.get("agent_notes") == {
            "deploy": "merged adapters, endpoint InService"}, (
            "the driver's stage write erased what the agent wrote during the turn -- and "
            "every specialist prompt instructs the agent to write exactly that.")
        assert saved["stages"]["deploy"]["status"] == "completed"

    def test_a_stage_write_with_no_manifest_to_merge_into_is_refused(self):
        """An absent manifest is refused and REPORTED, never manufactured, and the token
        still settles.

        Two failure modes are being avoided at once. Writing a stages-only document would
        manufacture a manifest with no plan, no approval and no models, which reads
        downstream as "this run was never planned". But skipping the write silently -- which
        is what an `if manifest:` guard around it did -- is bug #22's own failure mode
        surviving inside the fix for it: stage results vanish and the call returns ok.

        So it raises, `handle_stage_complete`'s report isolation reports it, and the task
        token is still settled: the report is a convenience, the token is the pipeline's only
        way to learn a paid-for stage succeeded."""
        res, store = self._drive_stage_complete(None, {"endpoint_name": "e"})
        assert res["ok"], "an unwritable manifest must not withhold the task token"
        assert "report_error" in res and "ValueError" in res["report_error"], (
            "an absent manifest was skipped SILENTLY: the stage's results are gone and the "
            f"driver reported success -> {res}")
        assert "runs/run-1/manifest.json" not in store, (
            "a manifest was manufactured with no plan, no approval and no models")

    # --- #25: one missing grant took down a second, permitted write ------------------
    def test_a_refused_manifest_write_still_publishes_the_run_report(self):
        """The two writes are independent artifacts and must fail independently.

        They shared one `try`, manifest first. So when the driver's role turned out to lack
        PutObject on `runs/*/manifest.json` (#25), the AccessDenied left the block before
        `write_run_report` -- and the report write, which the role HAS allowed since bug
        #22's predecessor, never ran. Measured in production: 8 pipeline runs from
        2026-08-08 to 2026-08-10 wrote zero per-run reports under reports/run-*, while the
        nightly monitor sweep (a different code path) wrote one every day. The log line said
        "canonical report FAILED", naming the one of the two writes that had not been
        refused.

        A missing grant on artifact A must not be able to delete artifact B."""
        store = {"runs/run-1/deploy/endpoint.json": b"{}",
                 "runs/run-1/manifest.json": json.dumps(self._signed_manifest()).encode()}

        class _DenyManifest:
            """The live failure exactly: PutObject refused on the manifest key only."""

            def get_object(self, Bucket, Key):
                if Key not in store:
                    raise RuntimeError("NoSuchKey")
                return {"Body": io.BytesIO(store[Key])}

            def head_object(self, Bucket, Key):
                if Key not in store:
                    raise RuntimeError("404")
                return {}

            def put_object(self, Bucket, Key, Body, **kw):
                if Key.endswith("manifest.json"):
                    raise RuntimeError(
                        "An error occurred (AccessDenied) when calling the PutObject "
                        f"operation: not authorized to perform: s3:PutObject on {Key}")
                store[Key] = Body

        res, out = self._drive_stage_complete(None, {"endpoint_name": "e"},
                                              store=store, s3=_DenyManifest())
        from pipeline.contracts.report import report_key_for
        assert report_key_for("run-1") in out, (
            "the manifest write was refused and took the run report down with it -- the "
            f"report write was permitted the whole time. keys written: {sorted(out)}")
        assert res["ok"], "neither write may withhold the task token"
        assert "AccessDenied" in (res.get("report_error") or ""), (
            f"the refused manifest write must still be reported: {res}")

    def test_an_unreadable_manifest_does_not_overwrite_the_published_report_alias(self):
        """Splitting the writes made this reachable, so it is stated rather than assumed.

        `report_key_for("")` falls back to the alias key by design -- a report filed under a
        blank run id is worse than one under the shared alias. But with the writes split, a
        manifest that failed to LOAD no longer stops the report, and reporting an empty
        manifest would publish a document describing nothing to
        reports/run-latest/test-report-latest.json: destroying the last real run's published
        report to announce a run whose manifest could not even be read."""
        res, out = self._drive_stage_complete(None, {"endpoint_name": "e"})
        from pipeline.contracts.report import REPORT_KEY
        assert REPORT_KEY not in out, (
            "an empty manifest was published to the run-latest alias, overwriting the last "
            "real run's report with a report about nothing")
        assert not [k for k in out if k.startswith("reports/")], (
            f"a report was written for a run with no manifest: {sorted(out)}")
        assert "skipped" in (res.get("report_error") or ""), (
            f"the skip must be recorded, not silent: {res}")

    def test_the_endpoint_a_deploy_stage_created_reaches_the_stages_that_measure_it(self):
        """Bug #22's consumer half, and the reason it blocks autonomy.

        `params.student_endpoint` is read by eval ("a live endpoint in
        params.student_endpoint") and by monitor ("name in the manifest or
        params.student_endpoint"), and it was written by NOTHING -- not the console, not
        start-pipeline, not the driver. It is not a signed value that went missing either:
        an endpoint name does not exist until the deploy stage creates one, so no human
        can sign it in advance and no default can stand in for it.

        Driven through the REAL `_run_stage`, because the whole content of this bug is
        whether two correct halves are connected."""
        params = self._stage_params(
            {"run_id": "run-x", "stage": "monitor", "task": "health",
             "harness_id": "llmops_monitor",
             "manifest_uri": "s3://b/runs/run-x/manifest.json"},
            {"student": "Qwen/Qwen3-1.7B"},
            manifest={"stages": {"deploy": {
                "status": "completed",
                "metrics": {"endpoint_name": "llmops-student-run-x"}}}})
        assert params.get("student_endpoint") == "llmops-student-run-x", (
            "the monitor stage was not told which endpoint the deploy stage created, so "
            f"it must either guess or refuse: {params}. A CloudWatch metric attributed to "
            "the wrong endpoint is worse than a missing one -- it reads as evidence.")

    def test_a_stage_fact_the_run_never_produced_is_omitted_not_guessed(self):
        """Absent means absent. A default here would recreate bug #21 one layer down.

        A stage that needs the endpoint and finds no param must fail visibly. Junk in the
        manifest must not crash the stage that reads it either -- `stages` is written by
        agents as well as by the driver."""
        assert driver.stage_fact_params({}) == {}
        assert driver.stage_fact_params({"stages": {}}) == {}
        assert driver.stage_fact_params({"stages": "deployed"}) == {}
        assert driver.stage_fact_params({"stages": {"deploy": "done"}}) == {}
        assert driver.stage_fact_params({"stages": {"deploy": {"metrics": None}}}) == {}
        # Reported, but empty: an empty endpoint name is not a name.
        assert driver.stage_fact_params(
            {"stages": {"deploy": {"metrics": {"endpoint_name": ""}}}}) == {}

    def test_no_tool_description_calls_the_terminal_exit_a_pause(self):
        """`escalate_human` ENDS the run. Five descriptions said "The pipeline pauses."

        Not a missing feature -- a mislabelling, and the platform already has the pause
        those five sentences promised. `checkpoint` yields the turn, keeps the run alive
        and is the channel a directive arrives on (`take_directive` in the driver's
        checkpoint branch, `put_directive` from the console or the conductor). Every one
        of the 7 harnesses declares it.

        `escalate_human` is the opposite: `handle_escalate` writes status=`escalated` and
        `send_task_failure(error="EscalatedToHuman")` drives EscalateFail -> MarkRunFailed
        -> Fail. And `escalated` is in `UNREACHABLE_RUN_STATES`, so once it fires no
        directive can ever reach that run again -- `put_directive` returns
        `reachable: False` and the driver tells the conductor the decision "CHANGES
        NOTHING". So the tool advertised as the pause was the one call guaranteeing the
        run could never resume, and an agent that wanted to wait for a human picked the
        ending. The audit called this "escalate_human is one-way" and proposed building a
        HumanGate state; that would add a SECOND pause mechanism beside a working one.

        Derived from the driver, not from a word list: the terminal states come from
        `UNREACHABLE_RUN_STATES`, so if someone makes `escalated` resumable this guard
        stops demanding the warning instead of going stale.
        """
        driver = (REPO / "orchestration/harness_driver/handler.py").read_text()
        assert '"escalated"' in driver.split("UNREACHABLE_RUN_STATES = ", 1)[1][:120], (
            "escalate_human's run state is no longer unreachable for directives — if the "
            "run can now hear a verdict after escalating, these descriptions may "
            "legitimately promise a pause, and this guard should be rewritten, not deleted")

        pause_words = ("pauses", "paused", "pause the pipeline")
        for cfg in sorted((REPO / "agents").glob("*/harness.json")):
            h = json.loads(cfg.read_text())
            tools = {t["name"]: t for t in h.get("tools", [])
                     if t.get("type") == "inline_function"}
            esc = tools.get("escalate_human")
            if esc is None:
                continue  # the orchestrator has no escalate_human at all
            desc = esc["config"]["inlineFunction"]["description"]
            named = [w for w in pause_words if w in desc]
            assert not named, (
                f"{cfg.parent.name}: escalate_human's description calls it a {named} — "
                "it sets status=escalated and fails the execution, and `escalated` is an "
                "UNREACHABLE_RUN_STATE, so nothing can resume it. Point the agent at "
                "checkpoint for anything a human could answer.")
            # Saying what it is NOT is not enough: the agent needs the call that actually
            # does what it wanted, or it just avoids both and ends the turn in prose --
            # which the driver treats as a stage failure anyway.
            assert "checkpoint" in desc, (
                f"{cfg.parent.name}: escalate_human's description does not point at "
                "checkpoint. An agent blocked on a human decision needs the name of the "
                "call that keeps the run alive, not just a warning about this one")
            assert "checkpoint" in tools, (
                f"{cfg.parent.name}: declares escalate_human but not checkpoint, so it "
                "has a terminal exit and no way to wait for a human at all")

            # And the invariant bullet must not send a blocked agent to the exit either:
            # "escalate_human when blocked" was the same error one layer up.
            sentence = h["systemPrompt"][0]["text"].split(
                "TURN-END INVARIANT")[1].split("\n- ")[0]
            assert "escalate_human when blocked" not in sentence, (
                f"{cfg.parent.name}: the turn-end invariant still routes 'blocked' to "
                "escalate_human. Blocked-but-answerable is checkpoint; escalate_human is "
                "blocked-and-unanswerable, and the two are not interchangeable")

    def test_checkpoint_is_documented_as_the_directive_channel(self):
        """The other half: the pause must SAY it is the pause, on every harness.

        Paired with the guard above so the pair cannot be satisfied by weakening both
        descriptions into vagueness -- one forbids escalate_human claiming the pause, this
        one requires checkpoint to claim it. Derived from the driver's own contract: the
        checkpoint branch returns {"status": "directive", ...} from take_directive, so
        that shape is what the prompt has to teach.
        """
        driver = (REPO / "orchestration/harness_driver/handler.py").read_text()
        assert '"status": "directive"' in driver, (
            "the driver no longer returns a directive from the checkpoint branch — the "
            "human-in-the-loop channel moved, and every checkpoint description is now "
            "describing a mechanism that does not exist")
        for cfg in sorted((REPO / "agents").glob("*/harness.json")):
            h = json.loads(cfg.read_text())
            cp = next((t for t in h.get("tools", [])
                       if t.get("name") == "checkpoint"), None)
            assert cp is not None, f"{cfg.parent.name} declares no checkpoint"
            desc = cp["config"]["inlineFunction"]["description"]
            for needed in ("directive", "status"):
                assert needed in desc, (
                    f"{cfg.parent.name}: checkpoint's description does not mention "
                    f"{needed!r}. If the agent does not know a directive arrives here, "
                    "the channel exists and nobody uses it — which is how "
                    "resolve_escalation spent five phases writing into the void")

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

    def test_state_machine_eval_only_re_judges_without_reaching_a_training_stage(self):
        """eval_only exists to re-score an artifact an earlier run already paid for.

        Its whole value is that it spends no GPU on training, so the guard has to be
        mode-aware: a plain reachability walk from the eval entry sees BOTH branches of
        every Choice and therefore reaches RemediateFinetune, which is exactly the state
        this mode must not be able to enter. So Choice states that test `$.pipeline_mode`
        are resolved for this mode and the rest are left open -- meaning the walk still
        explores a gate pass and a gate fail, and only the mode conditions are honoured.

        Red without `EvalOnlyStopChoice`: a gate fail falls into RemediationChoice ->
        IncrementIteration -> RemediateFinetune, launching a training job in a mode whose
        entry deliberately skipped training, against a manifest with no finetune stage in
        it to remediate. A gate pass falls into Deploy, standing up an endpoint off a
        re-measurement nobody approved as a deployment.
        """
        asl = json.loads((REPO / "orchestration/state_machine.asl.json").read_text())
        states = asl["States"]

        def reachable_in_mode(mode, start=None, seen=None):
            """Reachability with `$.pipeline_mode`-testing Choices resolved to `mode`."""
            if seen is None:
                seen = set()
            if start is None:
                start = asl["StartAt"]
            if start in seen:
                return seen
            seen.add(start)
            st = states[start]
            exits = _exits(st)
            if st.get("Type") == "Choice":
                mode_branches = [c for c in st.get("Choices", [])
                                 if _tests_pipeline_mode(c)]
                if mode_branches:
                    taken = [c["Next"] for c in mode_branches if _mode_of(c) == mode]
                    exits = taken or ([st["Default"]] if "Default" in st else [])
            for nxt in exits:
                reachable_in_mode(mode, nxt, seen)
            return seen

        def _tests_pipeline_mode(choice):
            return any(r.get("Variable") == "$.pipeline_mode"
                       for r in choice.get("And", [choice]))

        def _mode_of(choice):
            for r in choice.get("And", [choice]):
                if r.get("Variable") == "$.pipeline_mode" and "StringEquals" in r:
                    return r["StringEquals"]
            return None

        eval_only = reachable_in_mode("eval_only")
        # The guard is only meaningful if the mode is actually routed somewhere distinct.
        assert "EvalGenerate" in eval_only, "eval_only never reaches the eval stage"
        full = reachable_in_mode("full")
        assert "DataPrepGenerate" in full and "RemediateFinetune" in full, (
            "the mode-resolving walk lost the full path, so it proves nothing about "
            "eval_only being narrower")
        # The line above is NOT sufficient, and a mutation proved it: pointing this new
        # Choice's Default at Complete stops the FULL pipeline at the gate -- never
        # deploying, never remediating -- and the assertion still passed, because
        # RemediateFinetune stays reachable through FinetuneLaunch's Catch. A sanity check
        # satisfied by an unrelated path is not a sanity check. What must survive is the
        # GATE's own routing, so name the states only a gate verdict leads to.
        assert "QualityGateChoice" in full, (
            "in full mode the gate verdict is no longer consulted: EvalOnlyStopChoice's "
            "Default must fall through to it, or every run ends at the report regardless "
            "of whether it passed")
        assert "Deploy" in full, "a passing full run can no longer reach Deploy"

        def harnesses(names):
            return {((states[n].get("Parameters") or {}).get("Payload") or {})
                    .get("harness_id")
                    for n in names} - {None}

        forbidden = {"llmops_data_prep", "llmops_finetune"} & harnesses(eval_only)
        assert not forbidden, (
            f"eval_only can reach {sorted(forbidden)}: this mode is dispatched with no "
            "corpus and no training stage in its manifest, so a GPU stage here spends "
            "money on a run that cannot use the result")
        assert "Deploy" not in eval_only, (
            "a passing re-judge reaches Deploy -- re-measuring an artifact is not "
            "approval to serve it, and the plan a human signed in this mode bought a "
            "number, not an endpoint")
        # and it must still be able to finish: a mode that cannot succeed is not a mode
        assert _reaches(states, "EvalGenerate", "Succeed")

    def test_every_pipeline_mode_the_machine_routes_on_is_one_the_dispatcher_knows(self):
        """The ASL's modes and start-pipeline's knowledge of them must not drift apart.

        The machine routes on a string it reads out of the execution input; the dispatcher
        is the only thing that puts it there and the only place that can refuse a mode
        whose inputs are missing. A mode in one and not the other is the `data_audit`
        regression again -- that key was dropped on the way in while the Choice still
        branched on it, so a customer who bought a cheap audit had GPUs provisioned.
        """
        asl = json.loads((REPO / "orchestration/state_machine.asl.json").read_text())
        routed = set()
        for st in asl["States"].values():
            for c in st.get("Choices", []):
                for rule in c.get("And", [c]):
                    if rule.get("Variable") == "$.pipeline_mode" and "StringEquals" in rule:
                        routed.add(rule["StringEquals"])
        assert routed, "no state routes on pipeline_mode at all"
        known = set(start_pipeline.MODE_REQUIRED_PARAMS) | {"full", "data_audit"}
        assert routed <= known, (
            f"the state machine routes on {sorted(routed - known)}, which start-pipeline "
            "does not know: nothing validates that mode's inputs, and nothing documents "
            "what it needs")
        # every mode with prerequisites must be one the machine actually routes on
        assert set(start_pipeline.MODE_REQUIRED_PARAMS) <= routed, (
            f"{sorted(set(start_pipeline.MODE_REQUIRED_PARAMS) - routed)} has "
            "prerequisites declared but no Choice branches on it, so the dispatcher "
            "gatekeeps a mode that would run the default full pipeline anyway")

    def test_eval_only_is_refused_without_the_artifact_it_would_re_judge(self):
        """A mode that can only escalate must be refused before it becomes a run.

        eval_only skips data-prep and finetune, so the eval agent has neither a fallback
        val split (curate writes it) nor a finetune stage entry naming the model artifact.
        Starting anyway produces a manifest, a runs-table row and a PipelineStarted event
        that all assert work is under way that the pipeline cannot do -- and the failure
        surfaces inside an agent turn, where it reads as an agent defect.
        """
        good = {"pipeline_mode": "eval_only",
                "model_artifact_uri": "s3://b/runs/run-x/finetune/model.tar.gz",
                "customer_eval_uri": "s3://b/customer-data/t/eval.jsonl"}
        m = start_pipeline.seed_manifest("run-y", "conductor", {}, good)
        assert m["params"]["model_artifact_uri"] == good["model_artifact_uri"]

        for drop in ("model_artifact_uri", "customer_eval_uri"):
            plan = {k: v for k, v in good.items() if k != drop}
            with pytest.raises(ValueError) as e:
                start_pipeline.seed_manifest("run-y", "conductor", {}, plan)
            msg = str(e.value)
            assert drop in msg and "eval_only" in msg, (
                f"the refusal for a missing {drop} must name both the param and the "
                f"mode, got: {msg}")
        # an empty string is the same absence: a plan template with the key left blank
        blank = {**good, "model_artifact_uri": ""}
        with pytest.raises(ValueError):
            start_pipeline.seed_manifest("run-y", "conductor", {}, blank)
        # and the prerequisites must not leak onto the modes that do not declare them
        for mode in ("full", "data_audit"):
            start_pipeline.seed_manifest("run-y", "conductor", {},
                                         {"pipeline_mode": mode, "source_uri": "s3://b/d"})

    def test_the_eval_prompt_is_told_where_the_artifact_comes_from_in_eval_only(self):
        """The mode is only real if the agent knows what changes about its inputs.

        The prompt normally finds the model through this run's own finetune stage. In
        eval_only that stage does not exist, and the failure mode if the prompt is silent
        is the worst kind of plausible: the agent picks the newest artifact in the bucket
        and reports a score that looks like a re-measurement of the artifact the plan
        named. So the prompt must name the param, and must forbid the fallback.
        """
        text = json.loads((REPO / "agents/eval/harness.json").read_text())
        text = text["systemPrompt"][0]["text"]
        assert "eval_only" in text and "params.model_artifact_uri" in text, (
            "the eval prompt does not mention the mode or the param it depends on")
        # the sentence that forbids guessing, and the one that says the run stops here
        low = text.lower()
        for phrase, why in (("escalate", "an absent artifact uri must escalate"),
                            ("never fall back", "guessing an artifact must be forbidden"),
                            ("neither deploys", "the prompt must say the run stops at the "
                                                "verdict, so the report IS the deliverable")):
            assert phrase in low, f"{why}: {phrase!r} is missing from the eval prompt"

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

    def test_the_console_form_field_name_is_the_one_consent_is_read_from(self):
        """The console UI is the ONLY path a customer has to sign a plan, and its form
        posts `teacher_model` (create_estimate's STR_KEYS) -- which is also the field
        cost_model.py prices the run from. The consent check read `models.teacher`.

        So the field the human's money was quoted against and the field the dispatcher
        obeyed were different fields, and the mismatch failed SILENTLY: `models` absent
        means "the plan is silent", which falls through to DEFAULT_MODELS. Red before
        the fix: teacher = us.deepseek.r1-v1:0 for a plan signed for Fable-5 -- priced
        as one model, executed on another, with every artifact agreeing. The bug #9
        class, reintroduced through a name rather than a precedence rule.

        Derived from the console's own STR_KEYS so the two cannot drift apart again --
        and checked as a THREE-way agreement (form posts -> estimator prices ->
        dispatcher obeys), because a field only one of the three knows about is the bug.
        Skipping a field this guard does not recognise is what let control m163 escape:
        renaming the console's `teacher_model` to `teacher_mdl` left the form posting a
        field the estimator never prices, and the whole suite stayed green.
        """
        console = (REPO / "deploy/console/lambda_function.py").read_text()
        str_keys = console.split("STR_KEYS = (", 1)[1].split(")", 1)[0]
        posted = [k.strip().strip('"\'') for k in str_keys.split(",") if "model" in k]
        assert posted, "the console form no longer posts any *_model field"

        priced = _fields_the_estimator_prices_models_from()
        vocabulary = {a: role for role, aliases in start_pipeline.ROLE_ALIASES.items()
                      for a in aliases}

        # Every field cost_model prices from must be one the form can actually post,
        # or the quote is computed from a default the customer never chose.
        unpostable = sorted(f for f, nested in priced.items() if not nested
                            and f not in posted)
        assert not unpostable, (
            f"cost_model prices a model from plan.{unpostable} but the console form "
            f"posts only {sorted(posted)}. The estimate a customer signs would be "
            "computed from a default they were never shown.")

        for field in posted:
            assert field in priced, (
                f"the console form posts {field!r} but cost_model prices no model from "
                f"it (it prices from {sorted(priced)}). The field the customer fills in "
                "is not the field their money is quoted against.")
            assert field in vocabulary, (
                f"the console form posts {field!r} but the dispatcher's ROLE_ALIASES "
                f"does not list it, so a plan signed through the UI dispatches on "
                "DEFAULT_MODELS -- priced as one model, executed on another.")
            role = vocabulary[field]
            m = start_pipeline.seed_manifest(
                "run-x", "conductor", {}, {field: "global.anthropic.claude-fable-5"})
            assert m["models"].get(role) == "global.anthropic.claude-fable-5", (
                f"the console signs plans with {field!r} and cost_model prices from it, "
                f"but the manifest resolved {role} to {m['models'].get(role)!r} -- the "
                "run would be priced as one model and executed on another")

    def test_a_plan_that_names_one_model_twice_with_two_ids_is_refused(self):
        """`teacher` and `teacher_model` are aliases for one fact. A document that uses
        both with different ids contradicts itself, and no precedence rule makes one
        reading more defensible -- preferring either would be this bug's own shape
        (a silent choice where a signature exists to settle the question)."""
        with pytest.raises(ValueError) as e:
            start_pipeline.seed_manifest("run-x", "conductor", {}, {
                "teacher_model": "us.deepseek.r1-v1:0",
                "models": {"teacher": "global.anthropic.claude-fable-5"}})
        msg = str(e.value)
        assert "more than once" in msg and "deepseek" in msg and "fable-5" in msg, (
            "the refusal must name the role and BOTH ids, or the operator is sent back "
            f"to diffing JSON: {msg!r}")

    def test_a_conflict_is_caught_across_two_different_alias_spellings(self):
        """The consent check must compare FACTS, not field names: `params.teacher_model`
        contradicting `plan.models.teacher` is the same unapproved spend as the
        same-spelling case, and an alias-blind check would wave it through."""
        with pytest.raises(ValueError) as e:
            start_pipeline.seed_manifest(
                "run-x", "conductor", {"teacher_model": "us.deepseek.r1-v1:0"},
                {"models": {"teacher": "global.anthropic.claude-fable-5"}})
        assert "contradicts the signed plan" in str(e.value)

    def test_the_supply_chain_block_is_not_mistaken_for_role_assignments(self):
        """The conductor prompt tells the orchestrator to write
        `models: {hf_repo, revision, files_sha256, license, mirror_uri}` for any
        open-weight model. Those describe WHERE a model came from, not WHICH role it
        fills, and the old check treated every key as a role -- so `hf_repo` became a
        fake role, and because no key was shared with `params`, a dispatcher-supplied
        teacher passed the conflict check unopposed."""
        plan = {"models": {"student": "Qwen/Qwen3-1.7B", "hf_repo": "Qwen/Qwen3-1.7B",
                           "revision": "0e0f4b6", "license": "apache-2.0",
                           "mirror_uri": "s3://b/models-mirror/x"}}
        m = start_pipeline.seed_manifest("run-x", "conductor", {}, plan)
        assert set(m["models"]) <= set(start_pipeline.MODEL_ROLES), (
            f"provenance keys leaked into the role map: {sorted(m['models'])}")
        assert m["models"]["student"] == "Qwen/Qwen3-1.7B"
        # ...and with the roles now shared, a contradicting dispatch is caught.
        with pytest.raises(ValueError):
            start_pipeline.seed_manifest(
                "run-x", "conductor", {"models": {"student": "Qwen/Qwen3-4B"}}, plan)

    def test_a_mirrored_repo_that_fills_no_role_is_refused(self):
        """The supply-chain block is where the LICENCE was checked and the bytes pinned.
        A plan that mirrors `meta-llama/Llama-3.2-1B` and assigns it to no role produced
        `student = Qwen/Qwen3-1.7B` (measured): the run trains on a model nobody cleared
        while the cleared one sits unused in the mirror. Silent, because "the plan is
        silent about the student" is indistinguishable from "there is no plan"."""
        with pytest.raises(ValueError) as e:
            start_pipeline.seed_manifest("run-x", "conductor", {}, {
                "models": {"hf_repo": "meta-llama/Llama-3.2-1B",
                           "revision": "9213a19", "license": "llama3.2"}})
        assert "meta-llama/Llama-3.2-1B" in str(e.value) and "no role" in str(e.value)
        # A role naming a DIFFERENT model from the same publisher is the near-miss that
        # matters, and it must be refused too: `meta-llama/Llama-3.1-70B` is not the
        # model whose revision was pinned or whose licence was read, and it is 70x the
        # size. A guard that compared publishers (or any substring) rather than model
        # identities would wave this through -- control m159 escaped exactly here.
        with pytest.raises(ValueError) as near:
            start_pipeline.seed_manifest("run-x", "conductor", {}, {
                "models": {"student": "meta-llama/Llama-3.1-70B",
                           "hf_repo": "meta-llama/Llama-3.2-1B",
                           "revision": "9213a19", "license": "llama3.2"}})
        assert "no role" in str(near.value), (
            "the mirrored repo fills no role -- a sibling model from the same publisher "
            f"is a different model, different bytes, different size: {near.value}")
        # Naming the role is all it takes -- the guard is on the gap, not on mirroring.
        m = start_pipeline.seed_manifest("run-x", "conductor", {}, {
            "models": {"student": "meta-llama/Llama-3.2-1B",
                       "hf_repo": "meta-llama/Llama-3.2-1B",
                       "revision": "9213a19", "license": "llama3.2"}})
        assert m["models"]["student"] == "meta-llama/Llama-3.2-1B"

    def test_a_misspelled_role_is_refused_rather_than_read_as_silence(self):
        """`teachr` used to mean "the plan is silent about the teacher", so the run
        spent on DEFAULT_MODELS. A typo must cost one visible error, not a run."""
        with pytest.raises(ValueError) as e:
            start_pipeline.seed_manifest("run-x", "conductor", {},
                                         {"models": {"teachr": "x"}})
        assert "teachr" in str(e.value)

    def test_every_plan_field_the_estimator_prices_from_is_one_the_dispatcher_obeys(self):
        """A plan is PRICED by cost_model.py and EXECUTED from the manifest. When those
        two read different field names the quote and the run can disagree with no
        artifact showing a contradiction -- which is exactly how this bug hid for a
        console-signed plan.

        The field names are scraped out of cost_model.py's own source rather than
        restated here: a test that re-types the expression it is checking passes by
        construction, and would have passed on the broken code too. Every plan field
        cost_model reads for a model role must resolve to that same model in the
        manifest.
        """
        priced = _fields_the_estimator_prices_models_from()
        vocabulary = {a: role for role, aliases in start_pipeline.ROLE_ALIASES.items()
                      for a in aliases}
        for field, nested in sorted(priced.items()):
            where = f"plan.models.{field}" if nested else f"plan.{field}"
            assert field in vocabulary, (
                f"cost_model prices a model from {where}, but the dispatcher has no "
                f"such field: ROLE_ALIASES does not list {field!r}. A plan signed "
                "against that quote would dispatch on DEFAULT_MODELS instead -- priced "
                "as one model, executed on another, which is this bug exactly.")
            role = vocabulary[field]
            plan = ({"models": {field: "global.anthropic.claude-fable-5"}} if nested
                    else {field: "global.anthropic.claude-fable-5"})
            resolved = start_pipeline._resolve_models({}, plan).get(role)
            assert resolved == "global.anthropic.claude-fable-5", (
                f"cost_model prices the {role} from {where}, but the dispatcher "
                f"resolved it to {resolved!r}. The quote and the run would name "
                "different models with nothing to flag the disagreement.")

    def test_every_plan_field_the_estimator_prices_reaches_the_stage_that_spends_it(self):
        """A plan is PRICED field by field and EXECUTED from `manifest.params`. Every
        non-model field the estimator reads must therefore arrive in params, or the run
        spends on something other than what was quoted.

        Derived from cost_model.py's own `plan.get("...")` calls rather than listed here,
        for the reason the model-field guard above gives: a list restated in a test is a
        list that drifts, and the fields that went missing are exactly the ones nobody
        remembered to add. Measured before the fix, on a signed industrial-defect plan:
        training_instance ml.p4d.24xlarge -> ml.g5.2xlarge, sample_count 40000 -> 2000,
        gates {"map50": 0.75} -> ARC's relative_solve_rate, because seed_manifest read
        `plan` for models and nothing else.
        """
        src = (REPO / "pipeline/contracts/cost_model.py").read_text()
        priced = {f for f in re.findall(r'plan\.get\(\s*"([a-z_0-9]+)"', src)}
        # `models` is priced through the role map and lands in manifest.models, not params
        # -- carrying the raw block into params too would be a second, un-normalised copy
        # of model consent, which is the four-names defect this repo already paid for.
        priced -= {"models"} | set(start_pipeline.PLAN_META_KEYS)
        priced -= {a for aliases in start_pipeline.ROLE_ALIASES.values() for a in aliases}
        assert len(priced) >= 10, (
            f"only {len(priced)} priced plan fields scraped from cost_model.py -- the "
            "scrape broke, and a guard that checks nothing passes loudest")
        for field in sorted(priced):
            probe = {"map50": 0.75} if field == "gates" else f"probe-{field}"
            m = start_pipeline.seed_manifest("run-x", "conductor", {}, {field: probe})
            assert m["params"].get(field) == probe, (
                f"cost_model prices a run from plan.{field}, but a signed plan naming it "
                f"produced params.{field}={m['params'].get(field)!r}. The quote and the "
                "run describe different spends, and every artifact afterwards agrees with "
                "the run -- the variance report joins them and reads the gap as an "
                "underspend rather than as two different runs.")

    def test_the_plan_can_displace_the_arc_specific_defaults(self):
        """DEFAULT_PARAMS is ARC-shaped (`dataset: arc-agi-2`, a relative_solve_rate gate)
        and that is only harmful if a plan cannot displace it. This is the genericity
        property the platform is for: one signed plan must be able to describe a YOLO
        detector run without the pipeline substituting an ARC one.
        """
        for field, arc_default in sorted(start_pipeline.DEFAULT_PARAMS.items()):
            other = ({"map50": 0.75} if isinstance(arc_default, dict)
                     else not arc_default if isinstance(arc_default, bool)
                     else arc_default + 1 if isinstance(arc_default, (int, float))
                     else f"not-{arc_default}")
            m = start_pipeline.seed_manifest("run-x", "conductor", {}, {field: other})
            assert m["params"][field] == other, (
                f"a signed plan set {field}={other!r} and the run used "
                f"{m['params'][field]!r} -- the ARC default outranked the human. Every "
                "non-ARC workload (customer distillation, YOLO fine-tuning) is this case.")
            # ...and where the plan is silent the default must still stand: absent is not
            # a licence to leave a stage unconfigured.
            silent = start_pipeline.seed_manifest("run-y", "scheduler", {}, None)
            assert silent["params"][field] == arc_default

    def test_a_dispatch_contradicting_the_signed_plans_settings_is_refused(self):
        """The same rule the models already had, for the fields that spend the money.
        Refusing rather than picking a side: a disagreement means the approval path and
        the dispatch path describe different runs, and choosing either one silently makes
        the artifacts agree with a spend no human authorised."""
        with pytest.raises(ValueError) as e:
            start_pipeline.seed_manifest("run-x", "conductor",
                                         {"training_instance": "ml.g5.2xlarge"},
                                         {"training_instance": "ml.p4d.24xlarge"})
        msg = str(e.value)
        assert "training_instance" in msg and "p4d" in msg and "g5.2xlarge" in msg, (
            f"the refusal must name the field AND both values: {msg}")
        # Echoing the plan's own value is the common belt-and-braces dispatch, not an
        # error: the check is on DISAGREEMENT, not on presence.
        m = start_pipeline.seed_manifest("run-y", "conductor",
                                         {"training_instance": "ml.p4d.24xlarge"},
                                         {"training_instance": "ml.p4d.24xlarge"})
        assert m["params"]["training_instance"] == "ml.p4d.24xlarge"
        # And params may still fill what the plan is silent about.
        m2 = start_pipeline.seed_manifest("run-z", "conductor", {"sample_count": 500},
                                          {"gates": {"map50": 0.9}})
        assert m2["params"]["sample_count"] == 500
        assert m2["params"]["gates"] == {"map50": 0.9}
        # A field the plan states INSIDE `data` is stated by the plan just as much as a
        # top-level one, so contradicting it must refuse too. The check has to run against
        # the FLATTENED plan for that: comparing params against the plan's top-level keys
        # alone leaves the nested half of every signed plan silently overridable -- and
        # `source_uri` is the field a data audit is entirely about, which bytes it reads.
        with pytest.raises(ValueError) as e2:
            start_pipeline.seed_manifest(
                "run-w", "conductor", {"source_uri": "s3://arc-agi-2/old-run/"},
                {"data": {"source_uri": "s3://customer-a/defect-photos/"}})
        assert "source_uri" in str(e2.value) and "customer-a" in str(e2.value), (
            f"a params key contradicting a nested plan field was not refused: {e2.value}")

    def test_the_plans_data_block_reaches_the_prompt_that_reads_it_flat(self):
        """data-prep's "audit" task reads `params.source_uri` and
        `params.customer_eval_uri`; the orchestrator prompt has the plan carry them
        NESTED, inside a `data` block. Two correct halves, never connected: an audit run
        dispatched from a signed plan arrived with no data URI at all, so the agent could
        only refuse or guess a customer-data/ path -- and the prompt forbids guessing.

        The keys are taken from the console's own readiness-panel list, which is already
        derived from the orchestrator prompt, so this cannot drift out of step with what
        plans really contain.
        """
        console = (REPO / "deploy/console/lambda_function.py").read_text()
        block = re.search(r"DATA_READINESS_FIELDS = \((.*?)\n\)", console, re.S).group(1)
        flat = sorted({k for k in re.findall(r'\(\s*"([a-z_.]+)"', block) if "." not in k})
        assert len(flat) >= 4, f"readiness-field scrape found only {flat}"
        plan = {"data": {k: f"probe-{k}" for k in flat}}
        m = start_pipeline.seed_manifest("run-x", "conductor", {}, plan)
        for k in flat:
            assert m["params"].get(k) == f"probe-{k}", (
                f"plan.data.{k} did not reach params.{k}; the prompt that consumes it "
                f"reads the flat name, so the stage sees nothing: {m['params'].get(k)!r}")
        # A nested key must NOT overwrite a same-named top-level one: the top level is the
        # more specific statement, and a silent overwrite is this same defect one layer in.
        m2 = start_pipeline.seed_manifest("run-y", "conductor", {}, {
            "source_uri": "s3://b/explicit", "data": {"source_uri": "s3://b/nested"}})
        assert m2["params"]["source_uri"] == "s3://b/explicit"

    def test_pipeline_mode_in_a_signed_plan_reaches_the_choice_state(self):
        """`pipeline_mode` is the most expensive field in a plan: the Choice state at the
        top of the machine reads it out of the EXECUTION INPUT to decide whether any GPU
        stage runs. A data_audit run is the conductor's cheap starter, sold to a customer
        as "we audit your data before quoting the training". Signed in the plan and
        dropped by seed_manifest, it defaulted to the full pipeline -- GPU stages on a
        customer who bought an audit."""
        c = {"s3": FakeS3(), "ddb": FakeDDB(), "sfn": FakeSfn(), "events": FakeEvents()}
        start_pipeline.handler({"trigger_source": "conductor",
                                "plan": {"pipeline_mode": "data_audit"}}, clients=c)
        sent = json.loads(c["sfn"].executions[0]["input"])
        assert sent["pipeline_mode"] == "data_audit", (
            f"the plan bought an audit and the execution input says "
            f"{sent.get('pipeline_mode')!r} -- the Choice state defaults to "
            "DataPrepGenerate, so the run provisions GPUs nobody paid for")

    def test_the_console_launch_forwards_the_priced_plan_not_two_integers(self):
        """The console is the only path a customer has, and its approve->launch step
        scraped the priced plan for `task_count` and `sample_count` alone. The other
        fields the estimator priced -- both instance types, teacher_model, harness_model,
        endpoint_hours, keep_reasoning, teardown -- were dropped, and the run executed on
        ARC defaults while the estimate record said otherwise.

        Derived from the console's OWN key lists, so a field added to the form is covered
        without editing this test.
        """
        console = (REPO / "deploy/console/lambda_function.py").read_text()
        body = re.search(r"def start_run\(body\):(.*?)\ndef ", console, re.S).group(1)
        assert re.search(r'payload\["plan"\]\s*=', body), (
            "start_run builds no `plan` key, so start-pipeline sees the priced plan as "
            "absent and every field it named falls to DEFAULT_PARAMS")
        priced_keys = set()
        for kind in ("INT_KEYS", "FLOAT_KEYS", "STR_KEYS", "BOOL_KEYS"):
            m = re.search(kind + r" = \((.*?)\)", console, re.S)
            priced_keys |= set(re.findall(r'"([a-z_0-9]+)"', m.group(1)))
        # Whatever the form priced must survive seed_manifest -- as a param, or (for a
        # model role) normalised into manifest.models.
        roles = {a: r for r, aliases in start_pipeline.ROLE_ALIASES.items()
                 for a in aliases}
        for k in sorted(priced_keys):
            probe = "global.anthropic.claude-fable-5" if k in roles else f"probe-{k}"
            m = start_pipeline.seed_manifest("run-x", "console", {}, {k: probe})
            got = (m["models"].get(roles[k]) if k in roles else m["params"].get(k))
            assert got == probe, (
                f"the console form prices {k}, and a plan naming it produced {got!r}. "
                "The estimate record and the run would describe different spends, and "
                "the variance report would call the difference an underspend.")

    def test_the_gate_prompt_reads_the_thresholds_the_plan_named(self):
        """The consumer half. `params.gates` now arrives from the signed plan, and the eval
        agent still gated on `student judge-score >= 0.80 x teacher score` -- a bar written
        into its prompt, not the one the customer signed. So a defect-detector run whose
        plan says {"map50": 0.75} would be judged on ARC's teacher-ratio, and a plan naming
        a metric the report does not carry would pass by never being checked.

        Same shape as bug #20's third defect: the resolver was fixed, and nothing consumed
        it. A gate is the one place where "the agent used its judgment" is not acceptable,
        because the gate is what the signature is FOR.
        """
        text = json.loads((REPO / "agents/eval/harness.json").read_text())
        prompt = text["systemPrompt"][0]["text"]
        gate_line = [l for l in prompt.splitlines() if l.startswith('- "gate"')]
        assert len(gate_line) == 1, f"eval's gate task line has moved: {len(gate_line)}"
        line = gate_line[0]
        assert "params.gates" in line, (
            "the gate task does not read params.gates, so the thresholds the plan named "
            f"reach the manifest and are then ignored: {line[:200]}")
        # An absent gates block must escalate, not fall back to a remembered number: an
        # unnamed bar is a missing approval, and a default here promotes an unjudged run.
        assert "escalate_human" in line, (
            "the gate task must escalate when params.gates is absent rather than invent a "
            "threshold -- a default bar is how a run passes a gate nobody set")
        # The ARC gate names may appear only as examples, never as THE rule: the whole
        # point is that a YOLO plan's map50 gate is as valid as ARC's.
        for arc in ("relative_solve_rate", "format_validity"):
            if arc in line:
                assert "params.gates" in line[:line.index(arc)], (
                    f"{arc} is named before params.gates in the gate line, which reads as "
                    "the rule rather than as one example of it")

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

    from pipeline.contracts.report import REPORT_KEY, report_key_for

    # Every key the writer can actually produce must be permitted -- the per-run object
    # and the alias. Derived from report_key_for() rather than restating the shape, so a
    # change to the key fails HERE, at the grant, instead of live on AccessDenied after
    # the stage has already been paid for.
    for key in (report_key_for("run-20260731T183103Z-8b864805"), REPORT_KEY):
        assert _driver_may_write(key), \
            f"the driver writes {key} but no statement allows it"

    # The property the narrow scope exists for: a pipeline that can rewrite the customer's
    # data can destroy the held-out set its own quality gates are judged against. These are
    # the bucket's real top-level prefixes, plus the two artifacts under runs/ that the
    # driver VERIFIES -- it head_objects the curated dataset and the eval report to decide
    # whether to settle a stage's token, and a role that can rewrite what it verifies can
    # launder its own evidence. That is why #25 granted runs/*/manifest.json and not runs/*.
    for forbidden in ("customer-data/held-out.jsonl",
                      "runs/r/distillation/curated.jsonl",
                      "runs/r/evaluation/report.json",
                      "contracts/x.json", "plans/p.json", "code/train.py",
                      "tasks/t.json", "finops/rates.json"):
        assert not _driver_may_write(forbidden), (
            f"the driver must not be able to write {forbidden}; the grant has widened "
            "past the objects the driver authors")


def _driver_written_keys() -> list:
    """Concrete S3 object keys the driver's own source can put, derived from that source.

    Not a hand-kept list: a second copy of the write sites is the "one model, four names"
    defect (#20) in miniature, and bug #25 IS the version of it where the copy lived in a
    test's forbidden-keys tuple. So the keys come from two derivations, each anchored to the
    code that produces them:

    * `Key=` expressions the driver puts directly, scraped out of the handler and out of any
      module it hands its S3 client to (bug #22's write is in the handler, the report writes
      are in pipeline/contracts/report.py -- a per-file scan sees only one of them, which is
      how the driver's FIRST missing grant hid).
    * the report keys, taken from `report_key_for()`/`REPORT_KEY` by calling them, because a
      key built by a function is only knowable by running it.

    f-string placeholders are substituted with a realistic run id rather than matched as
    globs: IAM globs and Python format fields both use braces-and-stars in ways that look
    similar and mean nothing alike, and the question here is whether one CONCRETE key the
    code can emit is inside the grant.
    """
    from pipeline.contracts.report import REPORT_KEY, report_key_for

    run = "run-20260810T182807Z-e394ada9"
    keys = {report_key_for(run), REPORT_KEY}
    sources = ["orchestration/harness_driver/handler.py",
               "pipeline/contracts/report.py"]
    for src in sources:
        text = (REPO / src).read_text()
        # put_object(...Key=<expr>...) -- the expression as written, then f-string fields
        # resolved. Only put_object: get/head are reads and their keys are not this grant's
        # business.
        for call in re.findall(r"put_object\((.*?)\)", text, re.S):
            m = re.search(r'Key=(?:f?")([^"]*)"', call) or re.search(r"Key=(\w+)", call)
            if not m:
                continue
            raw = m.group(1)
            if raw in ("run_key", "REPORT_KEY"):
                continue  # the report contract's own keys, already resolved above
            if raw == "key":
                # `_save_manifest` splits its key out of `manifest_uri`, so the key it can
                # write is whatever the driver puts in that field. Derived from the
                # f-strings that BUILD manifest_uri rather than restated, because a
                # restatement here is the same second copy that made #25 invisible: the
                # forbidden-keys tuple in the guard above said runs/*/manifest.json was one
                # the driver never writes, and it was right until _save_manifest existed.
                uris = re.findall(r'manifest_uri["\']?\s*[:=]\s*f"s3://\{[a-z_]+\}/([^"]+)"',
                                  text)
                assert uris, (
                    f"{src} writes Key=key from a manifest_uri and no f-string in it builds "
                    "one -- the derivation is blind, not clean")
                for u in uris:
                    keys.add(re.sub(r"\{[a-z_]+\}", run, u))
                continue
            keys.add(re.sub(r"\{[a-z_]+\}", run, raw))
    keys.update(_dispatched_manifest_keys())
    return sorted(keys)


#: Every Lambda that hands the driver a `manifest_uri`, and the harness that dispatch
#: targets. `_save_manifest` writes whichever key it is GIVEN, so the set of keys the
#: driver can write is not knowable from the driver's own source -- the f-string scrape
#: above only ever sees `runs/<run_id>/manifest.json`, which is why the scheduled sweep's
#: manifest was ungranted for two live sweeps after bug #25 was declared fixed.
_MANIFEST_DISPATCHERS = (
    ("monitor_sweep", "orchestration/monitor_sweep/handler.py"),
    ("finops_reconcile", "orchestration/finops_reconcile/handler.py"),
)


def _harness_reaches_stage_complete(harness_id: str) -> bool:
    """Whether that harness's prompt can call the tool that triggers the manifest write.

    The filter matters in BOTH directions. finops_reconcile builds
    `finops/manifests/<period>.json` exactly like the sweep does, but the finops prompt has
    no `stage_complete` -- it ends at `publish_cost_report` -- so the driver never writes
    that key, and demanding a grant for it would push this file's other half (no granted
    pattern nothing writes) into failing. Read from the harness JSON rather than listed
    here, so adding `stage_complete` to a prompt demands the grant in the same PR.
    """
    for path in (REPO / "agents").glob("*/harness.json"):
        spec = json.loads(path.read_text())
        if spec.get("harnessName") == harness_id:
            return "stage_complete" in json.dumps(spec)
    raise AssertionError(f"no harness declares harnessName {harness_id!r}")


def _dispatched_manifest_keys() -> set:
    """Manifest keys the dispatchers name, obtained by CALLING their payload builders.

    Not by pattern-matching their source: monitor_sweep interpolates a module constant
    (`{SWEEP_PREFIX}`) into its URI, so the f-string scrape used above would have derived
    the literal key `{SWEEP_PREFIX}/manifests/...`, matched it against no grant, and failed
    for a reason that has nothing to do with IAM. A key built by a function is only knowable
    by running it.
    """
    keys = set()
    for name, rel in _MANIFEST_DISPATCHERS:
        mod = _load(name, rel)
        args = {"project": "llmops-agentic-system", "bucket": "llmops-data-test",
                "region": "us-east-1", "run_id": "sweep-2026-08-12",
                "task": "reconcile", "period": "2026-08-11", "runs": []}
        params = inspect.signature(mod.build_payload).parameters
        payload = mod.build_payload(**{k: v for k, v in args.items() if k in params})
        assert payload.get("manifest_uri", "").startswith("s3://"), \
            f"{name}.build_payload no longer names a manifest_uri; the derivation is blind"
        if not _harness_reaches_stage_complete(payload["harness_id"]):
            continue
        keys.add(payload["manifest_uri"][5:].partition("/")[2])
    assert keys, ("no dispatcher contributed a manifest key -- either every scheduled "
                  "harness lost stage_complete or this derivation stopped seeing them")
    return keys


def _driver_write_patterns() -> list:
    """Object-key globs the driver role's s3:PutObject statements permit.

    fnmatch, not hand-rolled prefix arithmetic: an IAM resource is a glob, and a helper
    clever enough to parse one is clever enough to crash on the very input it is meant to
    reject (this logic's first draft raised IndexError on a bucket-wide grant -- a test that
    fails for the wrong reason is a test that will pass for the wrong one).
    """
    doc = json.loads((REPO / "deploy/iam/lambda_roles.json").read_text())
    patterns = []
    for st in doc["roles"]["driver"]["permissionsPolicy"]["Statement"]:
        acts = st.get("Action")
        if "s3:PutObject" not in ([acts] if isinstance(acts, str) else acts or []):
            continue
        res = st["Resource"]
        for r in ([res] if isinstance(res, str) else res):
            tail = r.split(":::", 1)[1]
            patterns.append(tail.split("/", 1)[1] if "/" in tail else "")
    return patterns


def _driver_may_write(key: str) -> bool:
    return any(p and fnmatch.fnmatch(key, p) for p in _driver_write_patterns())


def test_every_s3_key_the_driver_writes_is_inside_a_granted_prefix():
    """Bug #25, and the shape of it is why this guard is derived rather than listed.

    `test_the_driver_role_can_write_the_report_the_driver_always_writes` was GREEN
    throughout: the driver does have s3:PutObject, and the report keys it checked are
    permitted. It went further and asserted `runs/r/manifest.json` was FORBIDDEN -- a
    correct claim when written, which bug #22's cure turned into a green pin on a live
    defect. `_save_manifest` began writing exactly that key, no grant was added, and the
    action-level guard above could not see it because the action was present and only the
    RESOURCE was wrong.

    Live: rehearsal run-20260810T182807Z-e394ada9 completed DataPrepGenerate -- 300 rows
    generated and paid for -- and logged `not authorized to perform: s3:PutObject on
    .../manifest.json`. Bug #22 was "no stage can read what the stage before it produced";
    an IAM gap that makes its write fail is not a smaller bug, it is #22 still open. And
    because both writes shared one try block, the report write that WAS permitted never ran
    either: 8 runs since 2026-08-08 produced no per-run report.

    So the contract is checked at the level the defect lives at -- keys, not actions. Every
    S3 key the driver's own source can produce is derived from that source and matched
    against the grant, in both directions: a key with no grant fails here instead of live
    after the spend, and a grant no key needs is flagged too, because that is how the
    verifier of the customer's held-out data quietly becomes its writer.
    """
    keys = _driver_written_keys()
    assert len(keys) >= 3, (
        f"only {len(keys)} driver write sites derived ({keys}) -- the scrape has gone "
        "blind and would pass whatever the role allowed")
    ungranted = sorted(k for k in keys if not _driver_may_write(k))
    assert not ungranted, (
        f"the driver writes these keys and its role permits none of them: {ungranted}. "
        "The stage does its work, gets paid for, and dies on AccessDenied.")

    # The other direction: no granted pattern may exist that nothing writes. A statement
    # kept "just in case" is how a read-only role becomes a writer -- and reviewing it later
    # is impossible, because there is no code to point at that needs it.
    unused = [p for p in _driver_write_patterns()
              if not any(fnmatch.fnmatch(k, p) for k in keys)]
    assert not unused, (
        f"the driver role grants PutObject on {unused} and writes nothing that matches. "
        "Either a write was removed and the grant outlived it, or the grant is wider than "
        "the code and nothing will ever tell you.")


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
    # Added with the ev.emit_event detection below: both emit events, and neither was
    # under the guard at all -- the resurrector's grant exists but nothing pinned it.
    ("resurrector", "orchestration/resurrector/handler.py"),
    ("finops_reconcile", "orchestration/finops_reconcile/handler.py"),
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
    # The shared events helper is a client handoff every handler uses the same way, so
    # it is detected generically rather than listed per handler in _CLIENT_HANDOFFS.
    # This is where the fourth instance of the defect class hid: resume's
    # emit(MODEL_TRAINED) rides ev.emit_event, so no `c["events"].put_events(` literal
    # appears in the handler, and the three roles that DID hold the grant held it
    # because their emits had already failed live -- the scan itself saw none of them.
    if "ev.emit_event(" in text:
        needed["events:PutEvents"] = "ev.emit_event(...) hands c[\"events\"] to the shared helper"

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
    # `aws s3 cp` from the bucket is s3:GetObject on the exact key (downloads only in
    # any prompt today -- the canonical-trainer fetch; an upload spelling would need
    # PutObject and should fail here first).
    ("s3", "cp"): "s3:GetObject",
    ("sagemaker", "list-training-jobs"): "sagemaker:ListTrainingJobs",
    ("sagemaker", "list-endpoints"): "sagemaker:ListEndpoints",
    ("sagemaker", "list-tags"): "sagemaker:ListTags",
    ("sagemaker", "describe-endpoint"): "sagemaker:DescribeEndpoint",
    ("sagemaker", "describe-endpoint-config"): "sagemaker:DescribeEndpointConfig",
    ("sagemaker", "delete-endpoint"): "sagemaker:DeleteEndpoint",
    ("sagemaker", "delete-endpoint-config"): "sagemaker:DeleteEndpointConfig",
    ("sagemaker", "delete-model"): "sagemaker:DeleteModel",
    ("sagemaker", "list-endpoint-configs"): "sagemaker:ListEndpointConfigs",
    ("sagemaker", "list-models"): "sagemaker:ListModels",
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
    a DECISION, not an oversight.

    This test used to end by asserting only that emitted names are known, on the
    reasoning that "fire-and-forget is recorded by EVENTS_NEEDING_A_RULE's absence".
    Absence records nothing: an oversight is absent from that dict too, and the two are
    indistinguishable -- the same "no rule vs rule missing" ambiguity the routed half of
    the guard exists to remove, reintroduced one level down. So the classification is now
    TOTAL: every event names its half, and DRIFT_DETECTED -- an accusation about a
    deployed model, consumed by nothing -- had to be argued for in writing rather than
    inherited by default.
    """
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
    assert ev.ESCALATED_TO_HUMAN in emitted

    routed, forget = set(ev.EVENTS_NEEDING_A_RULE), set(ev.EVENTS_FIRE_AND_FORGET)
    unclassified = set(ev.ALL_EVENTS) - routed - forget
    assert not unclassified, (
        f"{sorted(unclassified)} is in ALL_EVENTS but in neither EVENTS_NEEDING_A_RULE "
        "nor EVENTS_FIRE_AND_FORGET. Say which: a detail-type that nothing listens to by "
        "ACCIDENT is invisible on a live bus -- it lands, and nothing happens, with no "
        "failure and no metric to notice.")
    assert not routed & forget, (
        f"{sorted(routed & forget)} is declared both routed and fire-and-forget")
    assert not (routed | forget) - set(ev.ALL_EVENTS), (
        f"{sorted((routed | forget) - set(ev.ALL_EVENTS))} is classified but is not a "
        "detail-type in ALL_EVENTS")
    for name, why in ev.EVENTS_FIRE_AND_FORGET.items():
        assert len(why) > 20, (
            f"{name}'s fire-and-forget reason is {why!r} -- too short to be a decision "
            "anyone can review or disagree with")


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


# ── the failover that never came back ─────────────────────────────────────────
# UpdateHarness is a control-plane write on a resource all seven agents and every
# concurrent run share. _maybe_failover_model used it to serve a need that is neither
# shared nor durable -- one salvage retry, one invocation -- and never reverted, so a
# single vendor 5xx burst repointed the deployed fleet PERMANENTLY. Every later run then
# executed on a model the human never signed (the KMS approval spine's whole purpose),
# that the cost model priced at a different tier, and that ARCHITECTURE.md §9.3
# explicitly asserts is not what is deployed -- invisible to every guard in the tree,
# because the divergence lives in the control plane while agents/*/harness.json reads
# exactly as it always did.
#
# These drive the real handler() through a real stream death. Asserting on the source
# text (the test above) could not have caught this: the swap was there, correct, and
# emitted its event; what was missing was the second half.
def _failover_clients(scripts, s3=None, ctl=None):
    c = clients(FakeAgentCore(scripts), s3)
    c["agentcore_control"] = ctl or FakeAgentCoreControl()
    return c


def _model_5xx_stream():
    """A stream death whose error carries a vendor 5xx signature, which is what
    _is_model_5xx keys on -- a plain ConnectionError salvages without failing over."""
    class _S:
        def __iter__(self):
            yield {"contentBlockDelta": {"delta": {"text": "partial"}}}
            raise RuntimeError("InternalServerException: model unavailable")
    return _S()


def test_a_model_failover_is_reverted_before_the_invocation_ends():
    uri = "s3://llmops-data-test/runs/run-test-1/raw/data.jsonl"
    ctl = FakeAgentCoreControl(model_id="global.anthropic.claude-fable-5")
    c = _failover_clients([_model_5xx_stream(),
                           tool_use_stream("stage_complete", {"outputs": [uri]}),
                           text_stream("ack")],
                          s3=FakeS3(existing=[uri]), ctl=ctl)
    out = driver.handler(driver_event(), clients=c)

    assert out["status"] == "completed", "the salvage retry must still succeed"
    arn = driver._resolve_harness_arn("llmops_data_prep").rsplit("/", 1)[-1]
    assert ctl.model_of(arn) == "global.anthropic.claude-fable-5", (
        f"the harness was left on {ctl.model_of(arn)}; a failover is scoped to one "
        "invocation, and an unreverted one silently repoints every future run of all "
        "seven agents to a model no human signed")
    # Both directions happened, in order: swap out, then back.
    assert [m for _, m in ctl.updates] == ["global.anthropic.claude-opus-5",
                                           "global.anthropic.claude-fable-5"], ctl.updates


def test_the_revert_happens_even_when_the_stage_crashes():
    """The restore is in `finally` for this reason. A failover followed by a crash is
    the likeliest shape in production -- the 5xx burst that triggered the swap is
    exactly the condition that goes on to kill the stage -- and it is the one where
    leaving the fleet swapped would persist longest, because nothing succeeds to
    prompt anyone to look."""
    ctl = FakeAgentCoreControl()
    # stage_complete naming an output that does not exist in S3: the driver rejects it,
    # and with the FakeS3 empty the verification raises inside _run_stage.
    c = _failover_clients([_model_5xx_stream()], ctl=ctl)

    class _ExplodingSfn(FakeSfn):
        def send_task_success(self, **kw):
            raise RuntimeError("boom in settle")

    c["sfn"] = _ExplodingSfn()
    c["agentcore"] = FakeAgentCore([_model_5xx_stream(),
                                    tool_use_stream("stage_complete", {"outputs": []}),
                                    text_stream("ack")])
    with pytest.raises(Exception):
        driver.handler(driver_event(), clients=c)

    arn = driver._resolve_harness_arn("llmops_data_prep").rsplit("/", 1)[-1]
    assert ctl.model_of(arn) == "global.anthropic.claude-fable-5", (
        "a crash after failover left the fleet on the fallback model")


def test_an_unrevertable_failover_is_recorded_and_said_out_loud(capsys):
    """The restore can fail -- the control plane is the same one that was 5xxing.

    Then the fleet really is diverged, and the only remaining defences are the run
    row and the log line. Both must exist, or the divergence is undetectable.
    """
    uri = "s3://llmops-data-test/runs/run-test-1/raw/data.jsonl"
    ctl = FakeAgentCoreControl(fail_update_on={"global.anthropic.claude-fable-5"})
    c = _failover_clients([_model_5xx_stream(),
                           tool_use_stream("stage_complete", {"outputs": [uri]}),
                           text_stream("ack")],
                          s3=FakeS3(existing=[uri]), ctl=ctl)
    c["ddb"].tables.setdefault("llmops-pipeline-runs", FakeTable()).items.append(
        {"run_id": "run-test-1", "status": "running"})
    out = driver.handler(driver_event(), clients=c)

    assert out["status"] == "completed", "a failed restore must not fail the stage"
    printed = capsys.readouterr().out
    assert "FAILOVER NOT RESTORED" in printed, printed[-800:]
    row = c["ddb"].tables["llmops-pipeline-runs"].items[0]
    assert "model_failover" in row, (
        "nothing on the run row records that this run swapped the fleet's model; a "
        "driver that dies mid-failover would leave no witness at all")
    rec = json.loads(row["model_failover"])
    assert rec["to_model"] == "global.anthropic.claude-opus-5"
    assert rec["restored"] is False


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
    legitimate driver invocations (finops, console dispatch) carry no token.

    The stub returns a status the driver really produces. It used to return a synthetic
    {"status": "ok"} -- which #72's backstop then correctly read as an UNANSWERED triage
    and paged about, because no return in the driver produces "ok". A double answering
    with a value production never emits is a double that tests a path production does not
    have."""
    seen = {}

    def _fake_run_stage(event, context=None, c=None):
        seen.update(event)
        return {"status": "resolved"}

    real = driver._run_stage
    try:
        driver._run_stage = _fake_run_stage
        out = driver.handler({"detail-type": "EscalatedToHuman", "source": "llmops.pipeline",
                              "detail": {"run_id": "run-xyz", "stage": "finetune"}},
                             None, clients())
    finally:
        driver._run_stage = real
    assert out == {"status": "resolved"}
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
           "EVENTS_TABLE": "llmops-stage-events",
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


# --- #73: a dead task token is an answer, not a crash ---------------------------------
def _gone(code="TaskTimedOut", op="SendTaskFailure"):
    from botocore.exceptions import ClientError
    msg = "Provided task does not exist anymore"
    return ClientError({"Error": {"Code": code, "Message": msg}}, op)


class _SfnRaising(FakeSfn):
    """A Step Functions client whose settles raise. Both settles, one exception."""

    def __init__(self, exc):
        super().__init__()
        self.exc = exc
        self.attempts = []

    def send_task_success(self, **kw):
        self.attempts.append(kw)
        raise self.exc

    def send_task_failure(self, **kw):
        self.attempts.append(kw)
        raise self.exc


class TestDeadTaskTokenSettle:
    """Live: the driver crashed FOUR times settling a token Step Functions had already
    discarded -- 2026-08-05T15:39:51Z, then 05:50:48Z / 05:52:03Z / 05:54:28Z on
    2026-08-09. The last three are ONE incident: the raise made Lambda mark the async
    invocation failed, Lambda retried it twice, and each retry was a fresh billed
    AgentCore turn re-running an agent whose stage had already been decided, against a
    token none of them could settle. resume_pipeline had known this since 2026-07-29;
    the driver did not.
    """

    @pytest.mark.parametrize("code", ["TaskTimedOut", "TaskDoesNotExist"])
    def test_a_dead_token_does_not_take_the_re_asks_exhausted_path_down(self, code):
        """The exact live crash. Three prose turns exhaust the re-asks, the driver
        settles MissingStageComplete, and the token is gone -- which changes nothing
        about the verdict: the stage still failed, PipelineFailed must still be emitted,
        and the invocation must still return rather than raise into Lambda's retry."""
        ac = FakeAgentCore([text_stream("one"), text_stream("two"), text_stream("three")])
        c = clients(ac)
        c["sfn"] = _SfnRaising(_gone(code))
        out = driver.handler(driver_event(), clients=c)
        assert out["status"] == "failed", "a dead token turned a decided stage into a crash"
        assert c["sfn"].attempts, "guard reads the wrong thing: no settle was attempted"
        assert any(e["DetailType"] == ev.PIPELINE_FAILED for e in c["events"].entries), \
            "the settle exception ate the only durable record that the stage failed"

    @pytest.mark.parametrize("code", ["ThrottlingException", "InternalServerError",
                                      "InvalidToken", "AccessDeniedException"])
    def test_a_settle_that_might_still_land_still_raises(self, code):
        """The discriminating half, and the reason this is code-matched rather than a
        bare `except Exception`. A throttle or a 5xx means the settle may yet succeed, so
        the invocation must fail and let Lambda retry it. Swallowing those would strand
        the token for its full TimeoutSeconds -- 86400s, a day, on every long-work state
        -- which is the zombie MarkRunDone and MarkRunFailed exist to prevent."""
        ac = FakeAgentCore([text_stream("one"), text_stream("two"), text_stream("three")])
        c = clients(ac)
        c["sfn"] = _SfnRaising(_gone(code))
        with pytest.raises(Exception) as ei:
            driver.handler(driver_event(), clients=c)
        assert code in str(ei.value)

    def test_a_dead_token_does_not_stop_a_completed_stage_from_reporting(self):
        """The success settle. A stage that DID the work and wrote its artifacts must
        still return completed when the execution that asked for it has since ended:
        the S3 outputs and the stage event are the durable record, and the token was
        only ever the way to tell the state machine. Nothing to tell is not a failure."""
        uri = "s3://llmops-data-test/runs/run-test-1/raw/data.jsonl"
        ac = FakeAgentCore([tool_use_stream("stage_complete", {"outputs": [uri]}),
                            text_stream("ack")])
        c = clients(ac, FakeS3(existing=[uri]))
        c["sfn"] = _SfnRaising(_gone(op="SendTaskSuccess"))
        out = driver.handler(driver_event(), clients=c)
        assert out["status"] == "completed"

    def test_a_dead_token_does_not_stop_an_escalation_from_reaching_a_human(self):
        """handle_escalate's settle is LAST for a reason (#72's third layer: the
        conductor is the only line to a human on this path). If the settle raising took
        the return down, the caller would see a crash on an escalation that had already
        published to SNS and emitted its event -- and would retry all of it."""
        c = clients()
        c["sfn"] = _SfnRaising(_gone())
        out = driver.handle_escalate(c, driver_event(), {"reason": "budget exhausted"})
        assert out["escalated"] is True
        assert c["sns"].published, "guard reads the wrong thing: nothing was published"

    def test_the_crash_settle_still_re_raises_the_original_cause(self):
        """The handler wrapper's settle reports a crash. If the token is gone, the
        REAL exception must still surface -- swallowing it would turn a genuine driver
        bug into a silent success, which is the failure the wrapper was built to end
        (run-...-8b864805 held its token 90 minutes while a log stream was the only
        participant who knew). The dead token changes who hears, not whether."""
        class _Dead:
            def invoke_harness(self, **kw):
                raise RuntimeError("AccessDeniedException on InvokeHarness")

        c = clients(_Dead())
        c["sfn"] = _SfnRaising(_gone())
        with pytest.raises(RuntimeError) as ei:
            driver.handler(driver_event(), clients=c)
        assert "AccessDenied" in str(ei.value), \
            "the settle's own exception replaced the crash it was reporting"
        assert c["sfn"].attempts[-1]["error"] == "DriverCrashed", \
            "guard reads the wrong thing: the crash settle was never attempted"

    def test_settle_token_picks_the_call_from_the_arguments_it_was_given(self):
        """A funnel that took a boolean would let a failure path report success on a
        typo. `output=` means success, its absence means failure -- so the wrong call
        cannot be reached by passing the wrong value, only by passing the wrong key."""
        sfn = FakeSfn()
        assert driver.settle_token(sfn, "tok", output="{}") is True
        assert sfn.successes and not sfn.failures
        assert driver.settle_token(sfn, "tok", error="E", cause="c") is True
        assert len(sfn.failures) == 1 and sfn.failures[0]["error"] == "E"

    def test_settle_token_reports_whether_it_settled(self):
        """False is not cosmetic: it is how a caller that must act differently on a
        dead token (a future one -- none does today) can, without re-deriving the
        error code its own except block already threw away."""
        assert driver.settle_token(_SfnRaising(_gone()), "tok", error="E") is False
        assert driver.settle_token(FakeSfn(), "tok", error="E") is True

    def test_every_settle_in_the_driver_goes_through_the_funnel(self):
        """The bug was one unguarded settle out of four. Guarding the one that crashed
        would leave three, and the next one to be hit would read as a new bug. Derived
        from the source so a fifth settle added later fails HERE rather than in
        production -- the same reason #71 derives env_keys instead of listing them."""
        src = (REPO / "orchestration/harness_driver/handler.py").read_text()
        body = src.split("def settle_token(", 1)[1].split("\ndef ", 1)[0]
        outside = [ln.strip() for ln in src.splitlines()
                   if re.search(r"\bsend_task_(success|failure)\s*\(", ln)
                   and ln not in body.splitlines()
                   and not ln.lstrip().startswith("#")]
        assert not outside, (
            "these settles bypass settle_token(), so a dead token still crashes the "
            f"driver there: {outside}")

    def test_the_two_lambdas_that_settle_tokens_share_one_definition_of_gone(self):
        """resume_pipeline had TASK_GONE_CODES for ten days before the driver crashed on
        the same error. Copying the constant a second time would make the two agree only
        until someone edited one of them, so both import it -- and neither redefines it."""
        for rel in ("orchestration/harness_driver/handler.py",
                    "orchestration/resume_pipeline/handler.py"):
            src = (REPO / rel).read_text()
            assert "from pipeline.contracts.task_tokens import is_task_gone" in src, \
                f"{rel} does not import the shared definition"
            assert not re.search(r"^TASK_GONE_CODES\s*=", src, re.M), \
                f"{rel} re-defines TASK_GONE_CODES instead of importing it"

    def test_is_task_gone_survives_an_exception_with_no_response(self):
        """It is called from inside except blocks whose whole job is deciding what to do
        next. A classifier that throws while classifying turns one failure into two."""
        from pipeline.contracts.task_tokens import is_task_gone
        assert is_task_gone(ValueError("not a botocore error")) is False
        assert is_task_gone(_gone()) is True

    def test_the_bundle_carries_every_module_the_handlers_import(self):
        """task_tokens.py is imported by two handlers, and the bundle's file list was
        hand-maintained: nothing would have noticed. The deploy would have reported
        success and the driver would have died at cold start on ModuleNotFoundError --
        on the code path added to stop it dying. Derived now, from the `except
        ImportError` branch, because that branch IS the flat bundle layout."""
        dep = _load("deploy_lambdas_bundle", "deploy/07_lambdas.py")
        vendored = set(dep.vendored_modules())
        assert {"events", "report", "conductor_tools", "task_tokens",
                "manifest.schema.json"} <= vendored, vendored
        import zipfile as _zip
        for key, cfg in dep.LAMBDAS.items():
            names = set(_zip.ZipFile(io.BytesIO(dep.bundle(cfg["src"]))).namelist())
            assert "handler.py" in names
            for mod in dep.flat_imports(cfg["src"].read_text()):
                assert f"{mod}.py" in names, \
                    f"{key}'s bundle omits {mod}.py, which its handler imports"

    def test_the_bundle_refuses_a_module_it_cannot_find(self):
        """The alternative to a hand-list is not a silent skip. A fallback import naming
        a module that is in neither vendor directory is unshippable, and saying so at
        build time is the only place anyone will read it -- update_function_code returns
        200 for a zip that cannot import itself."""
        dep = _load("deploy_lambdas_missing", "deploy/07_lambdas.py")
        src = pathlib.Path(str(REPO / "tests/negative_controls")) / "_nc_bundle_handler.py"
        src.write_text("try:\n    import x\nexcept ImportError:\n"
                       "    import definitely_not_a_module\n")
        try:
            with pytest.raises(SystemExit) as ei:
                dep.vendored_modules([src])
            assert "definitely_not_a_module" in str(ei.value)
        finally:
            src.unlink()

    def test_flat_imports_reads_the_fallback_branch_not_every_indented_import(self):
        """The first attempt was a regex on indented import lines. It matched 22 names
        across the seven handlers -- every function-local `import json`, `import boto3`,
        and the `pipeline`/`orchestration` package heads from the TRY branch. Indentation
        says nested; it does not say nested in the fallback."""
        dep = _load("deploy_lambdas_flat", "deploy/07_lambdas.py")
        got = dep.flat_imports(
            "import os\n"
            "try:\n"
            "    from pipeline.contracts import events as ev\n"
            "except ImportError:\n"
            "    import events as ev\n"
            "    from task_tokens import is_task_gone\n"
            "def f():\n"
            "    import boto3\n"
            "    try:\n"
            "        pass\n"
            "    except ValueError:\n"
            "        import decimal\n")
        assert got == ["events", "task_tokens"], got

    def test_every_bundle_actually_imports_with_only_its_own_contents(self):
        """The static guard above checks the file LIST. This one does what cold start
        does: unzip, put only that directory on the path, `import handler`.

        Proven non-vacuous by rebuilding the driver and resume bundles with
        task_tokens.py withheld -- the zip a hand-maintained list would have shipped --
        which fails here with `ModuleNotFoundError: No module named 'task_tokens'`, the
        exact error production would have raised on the first invocation after deploy.
        A list-comparison test can only ever confirm the list I wrote; an import
        confirms the zip.
        """
        import subprocess
        import tempfile
        import zipfile as _zip
        dep = _load("deploy_lambdas_coldstart", "deploy/07_lambdas.py")
        for key, cfg in dep.LAMBDAS.items():
            d = tempfile.mkdtemp()
            _zip.ZipFile(io.BytesIO(dep.bundle(cfg["src"]))).extractall(d)
            r = subprocess.run([sys.executable, "-c", "import handler"],
                               cwd=d, capture_output=True, text=True,
                               env={**os.environ, "PYTHONPATH": d,
                                    "AWS_REGION": "us-east-1"})
            assert r.returncode == 0, (
                f"{key}'s bundle cannot import itself, so the function is dead at cold "
                f"start however green the deploy looked:\n{r.stderr[-2000:]}")


# ── teardown must be the inverse of provisioning, not a region-wide sweep ──────
# `project=llmops-agentic-system` is a tag any principal in the account can apply, and
# `ensure_endpoints` scopes its own read to `vpc-id`. The destroy path filtered on the
# tag ALONE, so it was not the create's inverse: it deleted every same-tagged interface
# endpoint in the region, including ones another team's VPC was resolving through. An
# interface endpoint is load-bearing, its deletion is a silent outage for its consumers,
# and re-running this script cannot undo it -- the replacement gets a new id and new DNS.

@pytest.fixture(scope="module")
def network_mod():
    """deploy/02_network.py as a module (name starts with a digit). Import-time safe."""
    return _load("llmops_02_network", "deploy/02_network.py")


class _FakeEc2:
    """Answers describe_* from a fixed inventory, honouring the filters it is given.

    Honouring them is the entire point: a double that ignored `vpc-id` would report the
    scoped and unscoped queries as identical and pass either implementation.
    """

    def __init__(self, vpcs, endpoints):
        self.vpcs, self.endpoints = vpcs, endpoints
        self.deleted = []

    @staticmethod
    def _matches(item, filters):
        for f in filters or []:
            name, values = f["Name"], f["Values"]
            if name.startswith("tag:"):
                key = name.split(":", 1)[1]
                tags = {t["Key"]: t["Value"] for t in item.get("Tags", [])}
                if tags.get(key) not in values:
                    return False
            elif name == "vpc-id":
                if item.get("VpcId") not in values:
                    return False
            else:  # an unmodelled filter would silently match everything
                raise AssertionError(f"_FakeEc2 does not model filter {name!r}")
        return True

    def describe_vpcs(self, Filters=None):
        return {"Vpcs": [v for v in self.vpcs if self._matches(v, Filters)]}

    def describe_vpc_endpoints(self, Filters=None):
        return {"VpcEndpoints": [e for e in self.endpoints if self._matches(e, Filters)]}

    def delete_vpc_endpoints(self, VpcEndpointIds):
        self.deleted.extend(VpcEndpointIds)
        return {"Unsuccessful": []}


def _tagged(**kw):
    return {"Tags": [{"Key": "project", "Value": "llmops-agentic-system"}], **kw}


def test_destroy_only_touches_endpoints_in_our_own_vpc(network_mod):
    ec2 = _FakeEc2(
        vpcs=[_tagged(VpcId="vpc-ours")],
        endpoints=[
            _tagged(VpcEndpointId="vpce-ours-1", VpcId="vpc-ours",
                    VpcEndpointType="Interface"),
            _tagged(VpcEndpointId="vpce-ours-gw", VpcId="vpc-ours",
                    VpcEndpointType="Gateway"),
            # Same tag, different VPC: another deployment, or another team that happens
            # to use the same project tag. Not ours to delete.
            _tagged(VpcEndpointId="vpce-theirs", VpcId="vpc-theirs",
                    VpcEndpointType="Interface"),
        ])
    victims = network_mod.destroy_interface_endpoints(ec2, dry=False)
    assert victims == ["vpce-ours-1"], victims
    assert ec2.deleted == ["vpce-ours-1"], (
        f"destroy deleted {ec2.deleted}; anything beyond vpce-ours-1 is someone "
        "else's load-bearing endpoint and its deletion is a silent outage")


def test_destroy_with_no_vpc_of_ours_deletes_nothing(network_mod):
    """The dangerous shape: nothing of ours exists, so an unscoped query returns other
    people's endpoints and every one of them looks like a victim. No VPC means no
    endpoints of ours are being billed, so the correct teardown is a no-op."""
    ec2 = _FakeEc2(
        vpcs=[],
        endpoints=[_tagged(VpcEndpointId="vpce-theirs", VpcId="vpc-theirs",
                           VpcEndpointType="Interface")])
    assert network_mod.destroy_interface_endpoints(ec2, dry=False) == []
    assert ec2.deleted == [], ec2.deleted


def test_a_dry_run_destroy_reports_without_deleting(network_mod):
    ec2 = _FakeEc2(
        vpcs=[_tagged(VpcId="vpc-ours")],
        endpoints=[_tagged(VpcEndpointId="vpce-ours-1", VpcId="vpc-ours",
                           VpcEndpointType="Interface")])
    assert network_mod.destroy_interface_endpoints(ec2, dry=True) == ["vpce-ours-1"]
    assert ec2.deleted == [], "a --dry-run destroy deleted for real"


def test_find_our_vpc_never_creates_one(network_mod):
    """A teardown whose discovery step can CREATE the thing it is about to delete always
    finds something to delete. ensure_vpc creates; find_our_vpc must not."""
    ec2 = _FakeEc2(vpcs=[], endpoints=[])
    assert network_mod.find_our_vpc(ec2) is None
    src = (REPO / "deploy/02_network.py").read_text()
    fn = src.split("def find_our_vpc", 1)[1].split("\ndef ", 1)[0]
    assert "create_vpc" not in fn, "the teardown's discovery step can create a VPC"


# ── the only billed resource here, and the two ways its cost went unreported ────────
# An interface endpoint bills per endpoint per hour PER AVAILABILITY ZONE -- AWS bills
# "for each hour that your VPC endpoint remains provisioned in each Availability Zone",
# because `SubnetIds` creates one endpoint network interface per subnet and the ENI is
# the billed unit (Pricing API `USE1-VpcEndpoint-Hours` = $0.01/hr, measured 2026-08-10).
# The script printed `0.01 * len(INTERFACE_SERVICES) * 24` -- the one-AZ figure -- while
# attaching every endpoint to BOTH subnets, so its own cost note was exactly half.
#
# And it provisioned all 11 for a consumer that does not exist: no
# agents/*/harness.prod.json, no VpcConfig in 07_lambdas.py, /llmops/network/* read by
# nothing. Then printed a warm success. The two defects compound -- an unused resource
# whose price is understated is the one nobody thinks to check.

class _CountingEc2(_FakeEc2):
    """_FakeEc2 plus the create/describe surface `ensure_endpoints` needs.

    Records what would be created so a test can tell "skipped the billed half" from
    "skipped everything" -- the distinction the gate turns on, since the free substrate
    is what a harness.prod.json has to be written against.
    """

    def __init__(self, vpcs=(), endpoints=()):
        super().__init__(list(vpcs), list(endpoints))
        self.created = []

    def describe_route_tables(self, Filters=None):
        return {"RouteTables": [{"RouteTableId": "rtb-main"}]}

    def create_vpc_endpoint(self, **kw):
        self.created.append((kw["VpcEndpointType"], kw["ServiceName"],
                             tuple(kw.get("SubnetIds") or ())))
        return {"VpcEndpoint": {"VpcEndpointId": f"vpce-{len(self.created)}"}}


def test_the_endpoint_cost_note_counts_every_az_not_every_endpoint(network_mod):
    """11 endpoints x 2 AZs x $0.01 x 24h = $5.28/day. The old note said $2.64."""
    per_day = network_mod.endpoint_cost_per_day(
        len(network_mod.INTERFACE_SERVICES), 2)
    assert per_day == pytest.approx(5.28), per_day
    # The exact wrong answer, pinned: $2.64 is the ONE-AZ figure, and the script attaches
    # to two subnets. A regression that drops the AZ factor lands back on this number, so
    # naming it here makes that mutation fail with its own history attached.
    assert network_mod.endpoint_cost_per_day(
        len(network_mod.INTERFACE_SERVICES), 1) == pytest.approx(2.64)
    # Linear in BOTH dimensions -- a hardcoded total, or one that ignores either list,
    # cannot satisfy all three of these.
    assert network_mod.endpoint_cost_per_day(1, 1) == pytest.approx(0.24)
    assert network_mod.endpoint_cost_per_day(12, 3) == pytest.approx(8.64)


def test_the_printed_cost_is_derived_from_both_lists(network_mod):
    """Structural, because the arithmetic being right does not mean it is the arithmetic
    that gets PRINTED. The original bug was a correct-looking expression inlined in the
    print call, so a guard on the function alone would have passed against it."""
    src = (REPO / "deploy/02_network.py").read_text()
    body = src.split("def main(", 1)[1]
    assert "endpoint_cost_per_day(" in body, (
        "main() no longer calls endpoint_cost_per_day; if the cost is computed inline "
        "again it is unguarded again")
    assert "len(subnet_ids)" in body.split("endpoint_cost_per_day(", 1)[1][:80], (
        "endpoint_cost_per_day is called without the AZ count derived from subnet_ids — "
        "a literal 2 here is the same drift the function exists to prevent")
    # Scoped to main()'s body, not the whole file: `endpoint_cost_per_day`'s docstring
    # QUOTES the old expression to explain what was wrong with it, and a file-wide check
    # fails on its own explanation -- which would push the next person to delete the
    # history rather than keep the guard.
    assert "0.01*len(INTERFACE_SERVICES)*24" not in body.replace(" ", ""), (
        "the one-AZ expression is back in main(): it counts endpoints and not AZs, so it "
        "prints exactly half the real bill")


def test_no_consumer_means_no_billed_endpoints_but_the_free_vpc_is_still_built(network_mod):
    """The gate is on the billing line, not on the script.

    Refusing outright would make the missing consumer unfixable: a harness.prod.json has
    to be written against a VPC, subnets and security groups that exist. So the free
    substrate is built either way and only the 11 billed endpoints are withheld.
    """
    ec2 = _CountingEc2(vpcs=[_tagged(VpcId="vpc-ours")])
    created = network_mod.ensure_endpoints(
        ec2, "vpc-ours", ["subnet-a", "subnet-b"], "sg-1", "us-east-1", dry=False,
        interface=False)
    kinds = {k for k, _, _ in ec2.created}
    assert kinds == {"Gateway"}, (
        f"created {ec2.created}; with no consumer the Interface endpoints are the whole "
        "cost and must not be created, and the free Gateway ones must still be")
    assert len(created) == len(network_mod.GATEWAY_SERVICES), created
    assert not any("Interface" == k for k, _, _ in ec2.created)


def test_every_interface_endpoint_is_attached_to_every_subnet(network_mod):
    """The fact the cost note has to reflect, asserted against the create call itself.

    If a future change attaches to one subnet, $2.64/day becomes correct and this test is
    the thing that says so -- the arithmetic and the attachment must move together.
    """
    ec2 = _CountingEc2(vpcs=[_tagged(VpcId="vpc-ours")])
    subnets = ["subnet-a", "subnet-b"]
    network_mod.ensure_endpoints(ec2, "vpc-ours", subnets, "sg-1", "us-east-1",
                                 dry=False, interface=True)
    ifaces = [(s, sn) for k, s, sn in ec2.created if k == "Interface"]
    assert len(ifaces) == len(network_mod.INTERFACE_SERVICES), ifaces
    for svc, attached in ifaces:
        assert attached == tuple(subnets), (
            f"{svc} attached to {attached}, not all of {subnets}: the per-AZ cost "
            "arithmetic in endpoint_cost_per_day no longer matches what is created")


def test_main_withholds_the_billed_endpoints_when_nothing_consumes_them(
        network_mod, monkeypatch, capsys):
    """Driven through main(), because that is where the DECISION lives.

    The guards above prove `ensure_endpoints(interface=False)` withholds the billed half
    and that `find_endpoint_consumers` reads the right files -- and both still passed when
    `want_interface` was mutated to a bare `True`. Two correct components wired together
    wrongly is the shape of the original bug, so the wiring needs its own test.
    """
    calls = {}

    def fake_ensure_endpoints(ec2, vpc_id, subnet_ids, sg_id, region, dry, interface=True):
        calls["interface"] = interface
        return []

    monkeypatch.setattr(network_mod, "ensure_vpc", lambda *a, **k: ("vpc-1", False))
    monkeypatch.setattr(network_mod, "ensure_subnets",
                        lambda *a, **k: ["subnet-a", "subnet-b"])
    monkeypatch.setattr(network_mod, "ensure_sg", lambda *a, **k: ("sg-1", False))
    monkeypatch.setattr(network_mod, "ensure_endpoints", fake_ensure_endpoints)
    monkeypatch.setattr(network_mod.boto3, "client", lambda *a, **k: object())

    def run(argv, consumers):
        calls.clear()
        monkeypatch.setattr(network_mod, "find_endpoint_consumers",
                            lambda *a, **k: list(consumers))
        monkeypatch.setattr(sys, "argv", ["02_network.py", "--region", "us-east-1",
                                          "--dry-run"] + argv)
        rc = network_mod.main()
        return rc, calls["interface"], capsys.readouterr()

    rc, interface, out = run([], consumers=[])
    assert interface is False, (
        "main() asked for the 11 billed interface endpoints with no consumer — the "
        "consumer check runs but its answer is not what the deploy branches on")
    # Skipping is not a failure: nothing was half-applied and the free substrate is up.
    # The signal is the stderr line plus `interface_endpoints: false` in the JSON, which
    # is what a caller can actually branch on -- an exit code cannot say "6 of 7 things".
    assert rc == 0, rc
    assert "SKIPPED" in out.err, out.err
    assert json.loads(out.out)["interface_endpoints"] is False

    # A real consumer unlocks them, and the reason is reported rather than implied.
    rc, interface, out = run([], consumers=["agents/eval/harness.prod.json runs VPC"])
    assert interface is True and rc == 0
    assert "5.28" in out.out, f"the per-AZ daily cost is not printed: {out.out}"
    assert "harness.prod.json runs VPC" in out.out

    # And the override works without one -- deliberately paying ahead of need is allowed,
    # it just has to be deliberate.
    rc, interface, out = run(["--force-unused-endpoints"], consumers=[])
    assert interface is True and rc == 0
    assert "no consumer" in out.out, out.out


def test_the_consumer_check_reads_the_files_a_deploy_reads(network_mod, tmp_path):
    """Derived from the same configs `05_harnesses.py --prod` and `07_lambdas.py` use.

    A hand-set flag would be the same optimism whose absence let 11 billed endpoints be
    provisioned for nobody; the check has to be able to go green on its own when someone
    actually builds a VPC-mode harness.
    """
    # Today's repo: measured, not assumed.
    assert network_mod.find_endpoint_consumers() == [], (
        "something now routes through the interface endpoints — that is a real change of "
        "state, and ARCHITECTURE §11 plus deploy/README must say so in the same commit")

    def repo_with(prod_cfg=None, lambdas_src="def main(): pass\n"):
        (tmp_path / "agents" / "eval").mkdir(parents=True, exist_ok=True)
        (tmp_path / "deploy").mkdir(exist_ok=True)
        (tmp_path / "deploy" / "07_lambdas.py").write_text(lambdas_src)
        p = tmp_path / "agents" / "eval" / "harness.prod.json"
        if prod_cfg is None:
            p.unlink(missing_ok=True)
        else:
            p.write_text(json.dumps(prod_cfg))
        return tmp_path

    def cfg(mode):
        return {"environment": {"agentCoreRuntimeEnvironment": {
            "networkConfiguration": {"networkMode": mode}}}}

    assert network_mod.find_endpoint_consumers(repo_with()) == []
    # A prod config that is still PUBLIC routes over the internet, not the endpoints:
    # its mere existence must not unlock the bill.
    assert network_mod.find_endpoint_consumers(repo_with(cfg("PUBLIC"))) == []
    vpc_mode = network_mod.find_endpoint_consumers(repo_with(cfg("VPC")))
    assert len(vpc_mode) == 1 and "networkMode=VPC" in vpc_mode[0], vpc_mode
    # The other consumer, independent of the harness configs.
    lam = network_mod.find_endpoint_consumers(
        repo_with(lambdas_src="fn(VpcConfig={'SubnetIds': s})\n"))
    assert len(lam) == 1 and "07_lambdas.py" in lam[0], lam
    # Unreadable is not absent: a broken prod config counts as a consumer, because
    # silently skipping the endpoints it needs breaks a deploy instead of costing money,
    # and of the two failure modes that is the one to avoid.
    # repo_with() resets 07_lambdas.py too — the previous case left VpcConfig in it, and
    # without the reset this asserts on two reasons and reads as a code failure.
    repo_with(cfg("VPC"))
    (tmp_path / "agents" / "eval" / "harness.prod.json").write_text("{not json")
    broken = network_mod.find_endpoint_consumers(tmp_path)
    assert len(broken) == 1 and "unreadable" in broken[0], broken


# ── a global name with a regional body: the second-region deploy ───────────────────
# IAM roles are global and these names are constants, but their policies are not: 71
# resource ARNs across deploy/iam/*.json carry <REGION>. put_role_policy REPLACES by
# name, so `01_iam.py --region <second>` rewrote every role's document with second-region
# ARNs and took the first region's permissions away -- an outage in a live deployment,
# printed as an ordinary "[update] role ..." line and exit 0.
#
# The obvious fix is measured out of reach rather than argued about: a role's AGGREGATE
# inline policy is capped at 10,240 characters and llmops-harness-execution substitutes
# to ~7.4k, so "one inline policy per region" cannot hold two regions on one role.
# test_two_regions_of_policy_cannot_fit_on_one_role pins that, so the day someone
# proposes per-region policy names the arithmetic answers instead of a code review.

@pytest.fixture(scope="module")
def iam_mod():
    """deploy/01_iam.py as a module (its name starts with a digit)."""
    return _load("llmops_01_iam", "deploy/01_iam.py")


class _FakeIam:
    """IAM with the one property the bug lives in: put_role_policy PERSISTS, by name.

    A double that only counted calls could not express this defect. What went wrong was
    not "a write happened" -- writes are the job -- it was that the write REPLACED a
    document another region depended on, so the damage is only visible by reading the
    policy back afterwards and finding the other region's ARNs gone.
    """

    def __init__(self, roles=()):
        #: role name -> {"tags": {...}, "policy": doc or None}
        self.roles = {n: {"tags": dict(t), "policy": p} for n, t, p in roles}
        self.puts = []

    # -- reads -------------------------------------------------------------------
    def get_role(self, RoleName):
        if RoleName not in self.roles:
            raise RuntimeError(f"NoSuchEntity: {RoleName}")
        r = self.roles[RoleName]
        return {"Role": {"Arn": f"arn:aws:iam::123456789012:role/{RoleName}",
                         "AssumeRolePolicyDocument": {"Version": "2012-10-17"},
                         "Tags": [{"Key": k, "Value": v} for k, v in r["tags"].items()]}}

    def get_role_policy(self, RoleName, PolicyName):
        pol = self.roles.get(RoleName, {}).get("policy")
        if pol is None:
            raise RuntimeError(f"NoSuchEntity: {PolicyName}")
        return {"PolicyDocument": pol}

    # -- writes ------------------------------------------------------------------
    def create_role(self, RoleName, Tags, **kw):
        self.roles[RoleName] = {"tags": {t["Key"]: t["Value"] for t in Tags},
                                "policy": None}

    def get_waiter(self, _name):
        return type("W", (), {"wait": lambda self, **kw: None})()

    def update_assume_role_policy(self, **kw):
        pass

    def put_role_policy(self, RoleName, PolicyName, PolicyDocument):
        self.puts.append(RoleName)
        self.roles.setdefault(RoleName, {"tags": {}, "policy": None})
        self.roles[RoleName]["policy"] = json.loads(PolicyDocument)

    def tag_role(self, RoleName, Tags):
        self.roles[RoleName]["tags"].update({t["Key"]: t["Value"] for t in Tags})

    # -- helper for assertions ---------------------------------------------------
    def regions_in_policy(self, role):
        """Every AWS region id appearing in the role's attached document."""
        pol = self.roles[role]["policy"]
        return set(re.findall(r"\b([a-z]{2}(?:-[a-z]+)+-\d)\b", json.dumps(pol)))


def _role_owned_by(region, arn_region=None):
    """A deployed role, tagged for `region`, whose policy names `arn_region`'s ARNs."""
    arn_region = arn_region or region
    return ({"project": "llmops-agentic-system", "llmops:region": region},
            {"Version": "2012-10-17", "Statement": [{
                "Effect": "Allow", "Action": "s3:GetObject",
                "Resource": f"arn:aws:s3:::llmops-agentic-123456789012-{arn_region}/*"}]})


def _run_iam_main(iam_mod, monkeypatch, iam, argv):
    """Drive 01_iam.py's main() against a fake IAM. Returns (exit_code, stdout+stderr).

    Through main(), not through find_region_conflicts: the first version of the test
    below called the checker directly, and deleting main()'s entire refusal block left
    it green. A checker nothing consults is not a guard, so the guard has to be driven
    the way a deploy drives it.
    """
    # ssm is stubbed rather than withheld so that a REGRESSION fails on the damage --
    # policies stripped, iam.puts non-empty -- instead of tripping over a missing client
    # on the way there. A double that blocks the broken path early hides what broke.
    clients = {"iam": iam,
               "sts": type("S", (), {
                   "get_caller_identity": lambda self: {"Account": "123456789012"}})(),
               "ssm": type("P", (), {"put_parameter": lambda self, **kw: None})()}

    def fake_client(name, **kw):
        if name not in clients:
            raise AssertionError(f"main() reached for an unexpected client: {name}")
        return clients[name]

    monkeypatch.setattr(iam_mod.boto3, "client", fake_client)
    monkeypatch.setattr(iam_mod.sys, "argv", ["01_iam.py"] + argv)
    try:
        rc = iam_mod.main()
    except SystemExit as e:      # argparse
        rc = e.code
    return rc


def test_a_second_region_deploy_is_refused_before_it_strips_the_first(
        iam_mod, monkeypatch, capsys):
    """The whole bug, end to end: region 2 must not take region 1's permissions away.

    Asserted by reading the policies BACK and finding us-east-1 still in them -- not by
    checking that a refusal was printed. A loud refusal with the writes happening anyway
    is precisely the failure mode worth ruling out.
    """
    roles = [(n, *_role_owned_by("us-east-1")) for n in iam_mod.ROLE_NAMES]
    iam = _FakeIam(roles)

    rc = _run_iam_main(iam_mod, monkeypatch, iam,
                       ["--region", "us-west-2", "--account-id", "123456789012"])

    assert rc == 2, f"a deploy that would strip us-east-1 exited {rc}, not 2"
    assert iam.puts == [], f"IAM was written despite the refusal: {iam.puts}"
    for role in iam_mod.ROLE_NAMES:
        assert iam.regions_in_policy(role) == {"us-east-1"}, (
            f"{role} lost us-east-1 from its policy")
    err = capsys.readouterr().err
    assert "us-east-1" in err and "REFUSING" in err, (
        f"the refusal does not name the region it is protecting:\n{err}")


def test_the_takeover_flag_is_the_only_way_past_the_refusal(iam_mod, monkeypatch):
    """The override has to work, and has to be the ONLY thing that opens this door.

    Kept because a refusal with no escape hatch gets removed wholesale the first time a
    region is legitimately decommissioned -- and then the protection is gone for good.
    Naming it --force-region-takeover rather than --force is the point: the flag states
    what it destroys.
    """
    roles = [(n, *_role_owned_by("us-east-1")) for n in iam_mod.ROLE_NAMES]
    iam = _FakeIam(roles)

    rc = _run_iam_main(iam_mod, monkeypatch, iam,
                       ["--region", "us-west-2", "--account-id", "123456789012",
                        "--dry-run", "--force-region-takeover"])

    assert rc == 0, f"--force-region-takeover was still refused (exit {rc})"
    assert iam.puts == [], "--dry-run wrote to IAM"


def test_the_check_covers_every_role_before_any_role_is_written(iam_mod):
    """Pre-flight, not per-role. A check inside the write loop is worse than none here:
    by the time role 7 objected, roles 1-6 would already be rewritten -- the same outage,
    now half-committed, with no record of the documents that were replaced.

    Driven through the one role that is NOT tagged: the conflict is on a LATER role, so
    an implementation that checked as it went would have written this one first.
    """
    names = list(iam_mod.ROLE_NAMES)
    fresh, owned = names[0], names[-1]
    iam = _FakeIam([(fresh, {"project": "llmops-agentic-system"}, None),
                    (owned, *_role_owned_by("us-east-1"))])
    conflicts = iam_mod.find_region_conflicts(iam, names, "us-west-2")
    assert conflicts == [(owned, "us-east-1")]
    assert iam.puts == [], "IAM was written during a check that ends in a refusal"
    src = (REPO / "deploy/01_iam.py").read_text()
    body = src.split("def main(", 1)[1]
    assert body.index("find_region_conflicts") < body.index("ensure_role("), (
        "main() calls ensure_role before checking for a region conflict, so the first "
        "roles are already stripped by the time the conflict is found")


def test_redeploying_the_owning_region_is_never_blocked(iam_mod):
    """The guard must not lock the deployment out of its own redeploys. Idempotent
    re-runs of the owning region are the common case, and a guard that blocks them gets
    deleted rather than fixed."""
    roles = [(n, *_role_owned_by("us-east-1")) for n in iam_mod.ROLE_NAMES]
    iam = _FakeIam(roles)
    assert iam_mod.find_region_conflicts(iam, iam_mod.ROLE_NAMES, "us-east-1") == []


def test_an_untagged_or_absent_role_does_not_block_a_deploy(iam_mod):
    """Absent, pre-tag, and unreadable roles all mean "no evidence of a conflict".

    Only a tag naming a DIFFERENT region is evidence. Blocking on absence would make
    the very first deploy into a clean account fail, and blocking on an unreadable role
    would let a missing iam:GetRole permission masquerade as a region conflict.
    """
    names = list(iam_mod.ROLE_NAMES)
    iam = _FakeIam([(names[0], {"project": "llmops-agentic-system"}, None)])  # pre-tag
    assert iam_mod.find_region_conflicts(iam, names, "us-west-2") == []
    assert iam_mod.find_region_conflicts(None, names, "us-west-2") == [], (
        "an offline dry-run with no IAM client reported a conflict it cannot know about")


def test_the_region_owner_tag_is_written_with_the_policy(iam_mod):
    """A tag nothing writes is a guard nothing arms: the check reads llmops:region, so
    every path that attaches a document must stamp it -- create AND update alike.

    Also pins the ORDER. Stamped after put_role_policy, so a failed policy write leaves
    the tag naming whoever owns the document actually attached; a tag written first
    would, on that failure, block the owning region's own corrective redeploy while the
    other region's permissions are still live -- refusing the one deploy that fixes it.
    """
    name = iam_mod.ROLE_NAMES[0]
    spec = {"trust": {"Version": "2012-10-17"}, "policy": {"Version": "2012-10-17",
            "Statement": [{"Effect": "Allow", "Action": "s3:GetObject",
                           "Resource": "arn:aws:s3:::b-us-west-2/*"}]}}

    absent = _FakeIam()                                    # create path
    iam_mod.ensure_role(absent, name, spec, dry=False, region="us-west-2")
    assert absent.roles[name]["tags"].get("llmops:region") == "us-west-2"

    present = _FakeIam([(name, *_role_owned_by("us-west-2"))])   # update path
    iam_mod.ensure_role(present, name, spec, dry=False, region="us-west-2")
    assert present.roles[name]["tags"].get("llmops:region") == "us-west-2"

    src = (REPO / "deploy/01_iam.py").read_text()
    fn = src.split("def ensure_role(", 1)[1].split("\ndef ", 1)[0]
    assert fn.index("put_role_policy") < fn.index("tag_role"), (
        "ensure_role tags the owning region before the policy it describes has landed")


def test_a_dry_run_predicts_the_refusal_instead_of_a_clean_diff(iam_mod):
    """--dry-run exists to be trusted before the real run. If it printed a tidy diff for
    a deploy that the real run refuses -- or worse, that the real run performs
    destructively -- it would be actively misleading, so the check runs on both paths."""
    src = (REPO / "deploy/01_iam.py").read_text()
    body = src.split("def main(", 1)[1]
    guard = body.split("find_region_conflicts", 1)[1].split("any_change = False", 1)[0]
    assert "dry_run" not in guard, (
        "the region-conflict check is conditioned on dry_run, so one of the two modes "
        "does not report it")


def test_two_regions_of_policy_cannot_fit_on_one_role(iam_mod):
    """Why the fix REFUSES instead of splitting the policy per region.

    Measured, not asserted from memory: IAM caps a role's aggregate inline policy at
    10,240 characters. If a single region's documents already exceed half of that, then
    per-region policy names cannot hold two regions, and distinct role names are the
    only real multi-region answer. This test is what makes that a fact in the repo
    rather than a claim in a comment -- and it will start failing the moment the
    policies shrink enough to make the split viable, which is exactly when someone
    should revisit the refusal.
    """
    specs = iam_mod.build_role_specs(
        {"<ACCOUNT_ID>": "123456789012", "<REGION>": "us-east-1",
         "<DATA_BUCKET>": "llmops-agentic-123456789012-us-east-1"}, None)
    biggest = max((len(json.dumps(s["policy"])), n) for n, s in specs.items())
    size, name = biggest
    assert size * 2 > 10240, (
        f"{name} is {size} chars, so two regions ({size * 2}) now fit under IAM's "
        "10,240-char inline limit -- the refusal in 01_iam.py could become a "
        "per-region policy split; revisit it deliberately rather than deleting this")
    assert size < 10240, (
        f"{name} substitutes to {size} chars, over IAM's 10,240 inline-policy limit: "
        "put_role_policy will reject it outright")


def test_the_role_names_the_guard_checks_are_the_ones_the_script_deploys(iam_mod):
    """ROLE_NAMES is only meaningful if it IS the deployed set. Derived from
    build_role_specs so a role added to deploy/iam/ cannot quietly skip the check."""
    specs = iam_mod.build_role_specs(
        {"<ACCOUNT_ID>": "123456789012", "<REGION>": "us-east-1",
         "<DATA_BUCKET>": "b"}, None)
    assert sorted(iam_mod.ROLE_NAMES) == sorted(specs), (
        "ROLE_NAMES has drifted from the roles 01_iam.py actually provisions")


# ── a comment key IAM has never heard of ────────────────────────────────────────────
# The policy documents carry `_comment` keys and substitute() strips them before
# PutRolePolicy. Live, #84 annotated two statements as `_comment_teardown` and
# `_comment_orphans`; the strip matched `k != "_comment"` exactly, both keys sailed
# through, and the deploy died on MalformedPolicyDocument — after the dry run printed
# a clean diff, because a dry run never reaches IAM's grammar. The strip is now a
# prefix match, and this test renders the REAL documents so the next new spelling
# fails here instead of mid-deploy.

def test_no_comment_key_of_any_spelling_reaches_iam(iam_mod):
    specs = iam_mod.build_role_specs(
        {"<ACCOUNT_ID>": "TESTACCTID00", "<REGION>": "us-east-1",
         "<DATA_BUCKET>": "b"}, "mem-0000000000")

    def comment_keys(obj, path="$"):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k.startswith("_comment"):
                    yield f"{path}.{k}"
                yield from comment_keys(v, f"{path}.{k}")
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                yield from comment_keys(v, f"{path}[{i}]")

    leaked = [p for name, spec in sorted(specs.items())
              for doc in (spec["trust"], spec["policy"])
              for p in comment_keys(doc, name)]
    assert leaked == [], (
        f"comment keys would reach PutRolePolicy and fail as MalformedPolicyDocument: {leaked}")


# ── the id UpdateHarness actually accepts ───────────────────────────────────────────
# 04_wire_memory.py passed the bare harness NAME to update_harness; the control plane
# requires `<name>-<10 char suffix>` (live: ValidationException naming the pattern
# `[a-zA-Z][a-zA-Z0-9_]{0,39}-[a-zA-Z0-9]{10}`), so the very first attach died before
# any harness was wired. 05_harnesses.py already resolved names through
# list_harnesses; the fix gives 04 the same resolution, and this test drives the
# attach the way the deploy does — through a control plane that enforces the pattern.

@pytest.fixture(scope="module")
def wire_memory_mod():
    """deploy/04_wire_memory.py as a module (its name starts with a digit)."""
    return _load("llmops_04_wire_memory", "deploy/04_wire_memory.py")


def test_attach_sends_the_resolved_harness_id_not_the_name(wire_memory_mod):
    class _FakeCtl:
        def __init__(self):
            self.sent = []

        def list_harnesses(self):
            return {"harnesses": [
                {"harnessId": "llmops_finetune-Ab1Cd2Ef3G", "name": "llmops_finetune"},
                {"harnessId": "llmops_eval-Hj4Kl5Mn6P", "name": "llmops_eval"},
            ]}

        def get_harness(self, harnessId):
            # never wired: the control plane raises rather than returning an empty block.
            # Spelled out instead of omitted, so the "not wired yet" branch is reached for
            # the reason the live API gives and not because the double lacks the method.
            raise RuntimeError("ResourceNotFoundException")

        def update_harness(self, harnessId, **kw):
            if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{0,39}-[a-zA-Z0-9]{10}", harnessId):
                raise RuntimeError(f"ValidationException: {harnessId!r}")
            self.sent.append(harnessId)

    ctl = _FakeCtl()
    out = wire_memory_mod.attach_to_harness(
        ctl, "llmops_finetune", "arn:aws:bedrock-agentcore:us-east-1:TESTACCTID00:memory/m",
        {"SEMANTIC": "sem-1", "EPISODIC": "epi-1"}, dry=False)
    assert out == {"harness": "llmops_finetune", "actorId": "llmops_finetune",
                   "attached": True,
                   # no data plane was handed in, so the other spelling's partition was
                   # not read — and an unread partition must not print as an empty one.
                   "stranded_check": "SKIPPED: no data plane -- unknown is not zero"}
    assert ctl.sent == ["llmops_finetune-Ab1Cd2Ef3G"]


def test_an_unknown_harness_name_refuses_instead_of_sending_garbage(wire_memory_mod):
    class _EmptyCtl:
        def list_harnesses(self):
            return {"harnesses": []}

        def update_harness(self, **kw):  # pragma: no cover — must not be reached
            raise AssertionError("update_harness called for a harness that does not exist")

    with pytest.raises(SystemExit):
        wire_memory_mod.attach_to_harness(
            _EmptyCtl(), "llmops_ghost",
            "arn:aws:bedrock-agentcore:us-east-1:TESTACCTID00:memory/m",
            {"SEMANTIC": "sem-1"}, dry=False)


# ── the two harnesses the fix could not reach, and the 43 records it would have burned ─
# 04_wire_memory.py's DEFAULT_HARNESSES was a hand-written list of the five pipeline
# workers, so #83's retrieval tightening (semantic topK 10/0.2 -> 5/0.6) never reached
# llmops_finops or llmops_orchestrator: measured live 2026-08-13, both still sat at
# 10/0.2 — the exact setting that injected another run's post-mortem as a bare fact.
# The obvious fix (derive the list) is destructive on its own, because those two are
# wired under their FULL harness IDs and actorId is the partition key of
# /users/{actorId}/facts: /users/llmops_finops-eDJtU9PvKh/facts holds 13 records,
# /users/llmops_orchestrator-GsIqHZ4viJ/facts holds 30, and every bare-name partition
# holds 0. Rewriting actorId to the bare name abandons all 43 and UpdateHarness returns
# success. So the live actorId wins, and a repartition is opt-in per harness and priced.


def _wired_ctl(live_actor_id=None, harness_id="llmops_finops-eDJtU9PvKh"):
    """A control plane that already carries `live_actor_id` on `harness_id`."""
    class _Ctl:
        def __init__(self):
            self.sent = []

        def list_harnesses(self):
            return {"harnesses": [{"harnessId": harness_id,
                                   "name": harness_id.rsplit("-", 1)[0]}]}

        def get_harness(self, harnessId):
            assert harnessId == harness_id, harnessId
            if live_actor_id is None:
                raise RuntimeError("ResourceNotFoundException")
            return {"harness": {"memory": {"agentCoreMemoryConfiguration": {
                "actorId": live_actor_id}}}}

        def update_harness(self, harnessId, memory, **kw):
            self.sent.append((harnessId, memory))

    return _Ctl()


def _dp(counts):
    """A data plane whose /users/<actor>/facts partitions hold `counts[actor]` records."""
    class _Dp:
        def __init__(self):
            self.pages = 0

        def list_memory_records(self, memoryId, namespace, maxResults, nextToken=None):
            actor = namespace.split("/")[2]
            recs = [{"memoryRecordId": f"r{i}"} for i in range(counts.get(actor, 0))]
            start = int(nextToken or 0)
            page = recs[start:start + maxResults]
            self.pages += 1
            out = {"memoryRecordSummaries": page}
            if start + maxResults < len(recs):
                out["nextToken"] = str(start + maxResults)
            return out

    return _Dp()


def _dp_ns(counts):
    """A data plane keyed by NAMESPACE, so the two channels can hold different counts.

    `_dp` keys on the actor segment, which collapses `/users/<a>/facts` and
    `/episodes/<a>` into one number — fine for the repartition price, useless for
    asserting that both channels are actually looked at."""
    class _Dp:
        def __init__(self):
            self.pages = 0
            self.seen = []

        def list_memory_records(self, memoryId, namespace, maxResults, nextToken=None):
            self.pages += 1
            if nextToken is None:
                self.seen.append(namespace)
            recs = [{"memoryRecordId": f"r{i}"} for i in range(counts.get(namespace, 0))]
            start = int(nextToken or 0)
            out = {"memoryRecordSummaries": recs[start:start + maxResults]}
            if start + maxResults < len(recs):
                out["nextToken"] = str(start + maxResults)
            return out

    return _Dp()


_MEM_ARN = "arn:aws:bedrock-agentcore:us-east-1:TESTACCTID00:memory/llmops_shared-abc"
_SIDS = {"SEMANTIC": "sem-1", "EPISODIC": "epi-1"}


def test_memory_wires_every_harness_this_repo_defines_not_a_hand_written_five(
        wire_memory_mod):
    """The list is derived from the configs, so an eighth agent is wired by existing.

    The two names asserted by name are the two the hand-written list omitted — they are
    named here because a derived list that happened to be derived from the same wrong
    producer would still pass a pure count check."""
    names = wire_memory_mod.harness_names()
    on_disk = {json.loads(p.read_text())["harnessName"]
               for p in (REPO / "agents").glob("*/harness.json")}
    assert set(names) == on_disk, (set(names) ^ on_disk)
    assert {"llmops_finops", "llmops_orchestrator"} <= set(names), names
    assert len(names) == len(set(names)) == len(on_disk)


def test_a_harness_config_with_no_name_refuses_instead_of_wiring_blind(
        wire_memory_mod, tmp_path, monkeypatch):
    (tmp_path / "agents" / "ghost").mkdir(parents=True)
    (tmp_path / "agents" / "ghost" / "harness.json").write_text('{"systemPrompt": []}')
    monkeypatch.setattr(wire_memory_mod, "REPO", tmp_path)
    with pytest.raises(SystemExit):
        wire_memory_mod.harness_names()


def test_no_harness_configs_refuses_instead_of_wiring_nothing(
        wire_memory_mod, tmp_path, monkeypatch):
    """Wiring zero harnesses must not read as a successful deploy — the failure mode
    the whole finding is about is a wiring step that reports success and reaches nobody."""
    (tmp_path / "agents").mkdir()
    monkeypatch.setattr(wire_memory_mod, "REPO", tmp_path)
    with pytest.raises(SystemExit):
        wire_memory_mod.harness_names()


def test_an_actor_id_already_live_survives_a_redeploy(wire_memory_mod):
    ctl = _wired_ctl(live_actor_id="llmops_finops-eDJtU9PvKh")
    out = wire_memory_mod.attach_to_harness(ctl, "llmops_finops", _MEM_ARN, _SIDS,
                                            dry=False, dp=_dp({}), memory_id="m-1")
    assert out["actorId"] == "llmops_finops-eDJtU9PvKh", out
    assert out.get("kept_live_actor_id") is True, out
    assert "repartitioned_from_records" not in out, out
    sent_actor = ctl.sent[0][1]["optionalValue"]["agentCoreMemoryConfiguration"]["actorId"]
    assert sent_actor == "llmops_finops-eDJtU9PvKh", sent_actor


def test_the_retrieval_config_is_rewritten_even_when_the_actor_id_is_kept(
        wire_memory_mod):
    """Keeping the actorId must not turn the whole attach into a no-op.

    This is the finding's entire point: the two harnesses that keep a live actorId are
    exactly the two whose retrieval thresholds were never tightened, so a fix that
    preserved their partition but skipped the UpdateHarness would leave them at 10/0.2
    forever — and would look, from the outside, like a deploy that touched all seven."""
    ctl = _wired_ctl(live_actor_id="llmops_finops-eDJtU9PvKh")
    wire_memory_mod.attach_to_harness(ctl, "llmops_finops", _MEM_ARN, _SIDS,
                                      dry=False, dp=_dp({}), memory_id="m-1")
    assert len(ctl.sent) == 1, ctl.sent
    cfg = ctl.sent[0][1]["optionalValue"]["agentCoreMemoryConfiguration"]
    sem = cfg["retrievalConfig"]["/users/{actorId}/facts"]
    assert (sem["topK"], sem["relevanceScore"]) == (5, 0.6), sem


def test_a_never_wired_harness_gets_the_stable_bare_name(wire_memory_mod):
    """The bare name is the only spelling that survives a harness recreation (the id
    suffix is regenerated), so it stays the choice wherever no data is at stake."""
    ctl = _wired_ctl(live_actor_id=None)
    out = wire_memory_mod.attach_to_harness(ctl, "llmops_finops", _MEM_ARN, _SIDS,
                                            dry=False, dp=_dp({}), memory_id="m-1")
    assert out["actorId"] == "llmops_finops", out
    assert "kept_live_actor_id" not in out, out


def test_moving_an_actor_id_must_be_asked_for_by_name(wire_memory_mod):
    """--repartition is per harness: naming one must not migrate the other."""
    ctl = _wired_ctl(live_actor_id="llmops_finops-eDJtU9PvKh")
    out = wire_memory_mod.attach_to_harness(
        ctl, "llmops_finops", _MEM_ARN, _SIDS, dry=False,
        dp=_dp({"llmops_finops-eDJtU9PvKh": 13}), memory_id="m-1",
        repartition=["llmops_orchestrator"])
    assert out["actorId"] == "llmops_finops-eDJtU9PvKh", out


def test_a_repartition_reports_the_records_it_abandons(wire_memory_mod):
    ctl = _wired_ctl(live_actor_id="llmops_finops-eDJtU9PvKh")
    out = wire_memory_mod.attach_to_harness(
        ctl, "llmops_finops", _MEM_ARN, _SIDS, dry=False,
        dp=_dp({"llmops_finops-eDJtU9PvKh": 13}), memory_id="m-1",
        repartition=["llmops_finops"])
    assert out["actorId"] == "llmops_finops", out
    assert out["repartitioned_from_records"] == 13, out


def test_a_repartition_with_no_data_plane_refuses_to_price_itself_at_zero(
        wire_memory_mod):
    """Silence is the one answer a destructive move must not accept: an unknown record
    count reads exactly like a count of 0."""
    ctl = _wired_ctl(live_actor_id="llmops_finops-eDJtU9PvKh")
    with pytest.raises(SystemExit):
        wire_memory_mod.attach_to_harness(ctl, "llmops_finops", _MEM_ARN, _SIDS,
                                          dry=False, repartition=["llmops_finops"])


def test_the_abandoned_record_count_reads_past_the_first_page(wire_memory_mod):
    """maxResults caps a page, not the partition — a count that stops at the first page
    under-reports exactly the partitions large enough to matter."""
    dp = _dp({"llmops_orchestrator-GsIqHZ4viJ": 250})
    assert wire_memory_mod.count_facts(dp, "m-1", "llmops_orchestrator-GsIqHZ4viJ") == 250
    assert dp.pages == 3, dp.pages


def test_a_repartition_for_a_harness_not_being_wired_refuses(wire_memory_mod):
    """`--harness llmops_eval --repartition llmops_finops` would print a clean success
    having moved nothing — a no-op that reads as done."""
    wire_memory_mod.check_repartition(["llmops_eval"], None)
    wire_memory_mod.check_repartition(["llmops_eval"], ["llmops_eval"])
    with pytest.raises(SystemExit):
        wire_memory_mod.check_repartition(["llmops_eval"], ["llmops_finops"])


def test_the_semantic_channel_stays_tighter_than_the_episodic_one(wire_memory_mod):
    """Both were 10/0.2; only the semantic one was wrong at that setting, because
    `/episodes/{actorId}/{sessionId}` scopes episodic recall to the agent's OWN session
    while `/users/{actorId}/facts` is the cross-RUN channel. A fix that tightened both
    to look consistent would have cut an agent off from its own history."""
    ctl = _wired_ctl(live_actor_id=None)
    out = wire_memory_mod.attach_to_harness(ctl, "llmops_finops", _MEM_ARN, _SIDS,
                                            dry=True, dp=_dp({}), memory_id="m-1")
    rc = out["would_attach"]["agentCoreMemoryConfiguration"]["retrievalConfig"]
    sem = rc["/users/{actorId}/facts"]
    epi = rc["/episodes/{actorId}/{sessionId}"]
    assert sem["relevanceScore"] > epi["relevanceScore"], (sem, epi)
    assert sem["topK"] < epi["topK"], (sem, epi)
    assert (epi["topK"], epi["relevanceScore"]) == (10, 0.2), epi


# ── the 63 records the guard above arrived one deploy too late for ──────────────────
# The measurement that produced this block was taken AFTER the guard above shipped, and
# it corrected it. Only two harnesses' full-harness-ID partitions had been counted (13 +
# 30 = 43) because only those two still POINTED at one. Counting all seven:
#
#   /users/llmops_data_prep-KuSKXUaxyP/facts      2      live actorId = bare name
#   /users/llmops_finetune-xXl7jsACZO/facts      25      live actorId = bare name
#   /users/llmops_eval-iuIIs96fFM/facts          16      live actorId = bare name
#   /users/llmops_deploy-nLLNWairTc/facts        11      live actorId = bare name
#   /users/llmops_monitor-YCXC5hcXzu/facts        9      live actorId = bare name
#   /users/llmops_finops-eDJtU9PvKh/facts        13      live actorId = that partition
#   /users/llmops_orchestrator-GsIqHZ4viJ/facts  30      live actorId = that partition
#   /users/<every bare harness name>/facts        0
#
# 106 semantic records, of which 63 are ALREADY unreachable by the agent that wrote them.
# llmops_monitor's newest orphan is dated 2026-08-08, so the move is days old, done by an
# earlier run of this very script, with no failed call anywhere. The episodic channel is
# stranded the same way (105 records under the five workers' /episodes/<full id>).
#
# Keeping a live actorId cannot undo that and no API moves a record between namespaces.
# What was missing is smaller and duller: nobody ever counted the OTHER spelling, so "this
# agent has no memory" and "this agent's memory is 25 records away from here" printed
# identically. These tests are the check that would have said so.


def test_every_attach_reports_the_records_the_other_spelling_still_holds(
        wire_memory_mod):
    """The check that would have caught the 63 — and it runs without --repartition,
    because the harnesses that lost their records are precisely the ones nobody was
    repartitioning: their actorId had ALREADY been rewritten."""
    ctl = _wired_ctl(live_actor_id="llmops_finops", harness_id="llmops_finops-eDJtU9PvKh")
    dp = _dp_ns({"/users/llmops_finops-eDJtU9PvKh/facts": 25})
    out = wire_memory_mod.attach_to_harness(ctl, "llmops_finops", _MEM_ARN, _SIDS,
                                            dry=False, dp=dp, memory_id="m-1")
    assert out["actorId"] == "llmops_finops", out
    assert out["stranded"] == {"/users/llmops_finops-eDJtU9PvKh/facts": 25}, out


def test_the_stranded_check_reads_the_episodic_partition_too(wire_memory_mod):
    """`/episodes/{actorId}` is the episodic strategy's reflection namespace and holds
    real records (live: 105 under the five workers' full ids). A check that only looked
    at `/users/.../facts` would report the smaller half of the loss."""
    ctl = _wired_ctl(live_actor_id="llmops_finops", harness_id="llmops_finops-eDJtU9PvKh")
    dp = _dp_ns({"/episodes/llmops_finops-eDJtU9PvKh": 11})
    out = wire_memory_mod.attach_to_harness(ctl, "llmops_finops", _MEM_ARN, _SIDS,
                                            dry=False, dp=dp, memory_id="m-1")
    assert out["stranded"] == {"/episodes/llmops_finops-eDJtU9PvKh": 11}, out
    assert "/users/llmops_finops-eDJtU9PvKh/facts" in dp.seen, dp.seen


def test_a_partition_with_nothing_in_it_is_not_reported_as_stranded(wire_memory_mod):
    """A warning on every attach of a fleet with nothing stranded is a warning nobody
    reads by the third deploy."""
    ctl = _wired_ctl(live_actor_id="llmops_finops", harness_id="llmops_finops-eDJtU9PvKh")
    out = wire_memory_mod.attach_to_harness(ctl, "llmops_finops", _MEM_ARN, _SIDS,
                                           dry=False, dp=_dp_ns({}), memory_id="m-1")
    assert "stranded" not in out, out
    assert "stranded_check" not in out, out


def test_an_unchecked_partition_does_not_report_as_an_empty_one(wire_memory_mod):
    """With no data plane the answer is 'not checked', never 'nothing there' — the two
    reading the same is how 63 records left without a failed call."""
    ctl = _wired_ctl(live_actor_id="llmops_finops", harness_id="llmops_finops-eDJtU9PvKh")
    out = wire_memory_mod.attach_to_harness(ctl, "llmops_finops", _MEM_ARN, _SIDS,
                                            dry=False)
    assert "stranded" not in out, out
    assert "SKIPPED" in out["stranded_check"], out


def test_the_stranded_count_reads_past_the_first_page(wire_memory_mod):
    """Same trap as the repartition price, one call further out: a partition big enough
    to matter is exactly the one a first-page count under-reports."""
    ctl = _wired_ctl(live_actor_id="llmops_finops", harness_id="llmops_finops-eDJtU9PvKh")
    dp = _dp_ns({"/users/llmops_finops-eDJtU9PvKh/facts": 250})
    out = wire_memory_mod.attach_to_harness(ctl, "llmops_finops", _MEM_ARN, _SIDS,
                                            dry=False, dp=dp, memory_id="m-1")
    assert out["stranded"] == {"/users/llmops_finops-eDJtU9PvKh/facts": 250}, out


def test_both_spellings_are_checked_when_the_live_actor_id_is_neither(wire_memory_mod):
    """A hand-set or renamed actorId leaves BOTH candidate partitions behind. Checking
    only the full harness ID assumes the bare name is the one this script chose, which is
    true in six of seven live harnesses and false in the case worth catching."""
    ctl = _wired_ctl(live_actor_id="legacy-actor",
                     harness_id="llmops_finops-eDJtU9PvKh")
    dp = _dp_ns({"/users/llmops_finops/facts": 4,
                 "/users/llmops_finops-eDJtU9PvKh/facts": 13})
    out = wire_memory_mod.attach_to_harness(ctl, "llmops_finops", _MEM_ARN, _SIDS,
                                            dry=False, dp=dp, memory_id="m-1")
    assert out["actorId"] == "legacy-actor", out
    assert out["stranded"] == {"/users/llmops_finops/facts": 4,
                              "/users/llmops_finops-eDJtU9PvKh/facts": 13}, out


# ── the SECOND spelling of a typed call ─────────────────────────────────────────────
# #28's fix taught the driver to read `<invoke name="x">` written out as prose. Live,
# the triage of run-20260811T101948Z-f9d34d27 then wrote a complete, correct verdict,
# announced "emitting the completion signal now" -- and printed the call as PYTHON
# SOURCE: `stage_complete(**{...})`, a JSON object behind a kwargs splat. The XML regex
# cannot see that shape, so the stage died MissingStageComplete with its decision doc
# already verified in S3 and the driver's backstop paged a human about a triage that
# had, in substance, succeeded. Same bug, second grammar.

def test_a_call_typed_as_python_source_is_recovered():
    text = (
        "Nothing further is owed this turn; emitting the completion signal now.\n\n"
        "stage_complete(**{\n"
        '  "run_id": "triage-run-x",\n'
        '  "stage": "orchestrator",\n'
        '  "status": "complete",\n'
        '  "outputs": [\n    "s3://b/runs/x/triage/decision.json"\n  ],\n'
        '  "summary": "closed as gate-FAIL (0.00 vs 0.55); a brace } inside a string '
        'must not close the object early"\n'
        "})\n")
    call = driver.parse_typed_call(text)
    assert call is not None, "the python spelling of a typed call was read as prose"
    assert call["name"] == "stage_complete"
    assert call["input"]["outputs"] == ["s3://b/runs/x/triage/decision.json"]
    assert call["input"]["status"] == "complete"
    # without the splat is the same commitment
    assert driver.parse_typed_call(
        'stage_complete({"run_id": "r", "outputs": []})')["input"] == {
            "run_id": "r", "outputs": []}


def test_the_python_spelling_is_gated_and_strict():
    """Held to a stricter bar than the XML shape: only SERVICED_TOOLS, and the whole
    argument must be one valid JSON object -- in Python source, anything less is
    indistinguishable from code the agent is merely quoting."""
    # a typed `shell` belongs to the harness, not this driver
    assert driver.parse_typed_call('shell({"command": "rm -rf /"})') is None
    # unquoted keys are Python, not JSON: quoting-a-snippet territory, stays prose
    assert driver.parse_typed_call("stage_complete({run_id: 'x'})") is None
    # kwargs form stays prose for the same reason
    assert driver.parse_typed_call('stage_complete(run_id="x", status="complete")') is None
    # an unbalanced object never resolves
    assert driver.parse_typed_call('stage_complete(**{"run_id": "x"') is None


def test_the_last_typed_call_wins_across_spellings():
    """Position decides, matching _drain's one-slot capture: a turn that narrates one
    plan and then commits to another ends on the commitment."""
    xml_then_py = (
        '<invoke name="checkpoint"><parameter name="next_action">resume</parameter>'
        '</invoke>\nOn reflection, the stage is done:\n'
        'stage_complete(**{"run_id": "r", "outputs": []})')
    assert driver.parse_typed_call(xml_then_py)["name"] == "stage_complete"
    py_then_xml = (
        'stage_complete(**{"run_id": "r", "outputs": []})\nActually, not yet:\n'
        '<invoke name="checkpoint"><parameter name="next_action">resume</parameter>'
        '</invoke>')
    assert driver.parse_typed_call(py_then_xml)["name"] == "checkpoint"


# ── a gagged turn is not a prose turn ───────────────────────────────────────────────
# EvalGate of run-20260811T101948Z-f9d34d27 resumed from a between-turns handoff and
# the next turn arrived stop_reason=content_filtered, ZERO text, no tool: the platform
# suppressed the model's output. re_asks was already at its cap from two real prose
# turns, so the empty turn fell straight through the exhaustion branch and the stage
# died MissingStageComplete -- the agent blamed for a sentence it was not allowed to
# say. A suppressed turn gets its own bounded budget, and exhausting THAT is named
# ContentFiltered, so the operator reads "the platform gagged the agent three times"
# instead of hunting a transcript for prose that never existed.

def filtered_stream():
    return [{"messageStop": {"stopReason": "content_filtered"}}]


def test_a_filtered_turn_does_not_spend_the_prose_budget():
    uri = "s3://llmops-data-test/runs/run-test-1/raw/data.jsonl"
    ac = FakeAgentCore([
        text_stream("narrating instead of calling"),   # re_ask 1
        text_stream("still narrating"),                # re_ask 2 (cap -- the live state)
        filtered_stream(),                             # the platform gags the model
        tool_use_stream("stage_complete", {"outputs": [uri]}),
        text_stream("ack")])
    c = clients(ac, FakeS3(existing=[uri]))
    out = driver.handler(driver_event(), clients=c)
    assert out["status"] == "completed", (
        "a platform-suppressed turn was billed to the prose budget and killed a "
        "stage whose very next turn completed the protocol")
    assert not c["sfn"].failures
    # the retry prompt must say the reply was never delivered, or the agent reasons
    # as if its filtered text is on the record
    nudge = ac.calls[3]["messages"][-1]["content"][0]["text"]
    assert "suppressed" in nudge and "NEVER delivered" in nudge


def test_three_consecutive_filtered_turns_settle_as_content_filtered():
    ac = FakeAgentCore([filtered_stream(), filtered_stream(), filtered_stream()])
    c = clients(ac)
    out = driver.handler(driver_event(), clients=c)
    assert out == {"status": "failed", "reason": "content_filtered"}
    assert c["sfn"].failures[0]["error"] == "ContentFiltered", (
        "a run killed by the platform filter must not be labelled "
        "MissingStageComplete -- the two need different operator responses")
    assert any(e["DetailType"] == ev.PIPELINE_FAILED for e in c["events"].entries)


def test_the_filtered_budget_survives_a_self_reinvoke():
    """Same argument as _re_asks: 'consecutive' is counted across Lambda invocations,
    and the live failure happened ON a resumed invocation -- if the counter does not
    ride the continuation payload, every handoff refills it."""
    src = (REPO / "orchestration/harness_driver/handler.py").read_text()
    body = src[src.index("def _run_stage"):]
    assert '"_filtered_turns": filtered_turns' in body, \
        "_self_reinvoke stopped carrying the filtered-turn counter"
    assert 'int(event.get("_filtered_turns", 0))' in body, \
        "a continuation no longer restores the filtered-turn counter"


# ── the gate is decided by an interval, not a point ─────────────────────────────────
# r5 exposed the old gate's arithmetic as theater: at n=40 the minimum detectable
# effect is ~22pp and the power to distinguish 0.55 from 0.50 is ~15% -- a coin flip
# weighted against the student -- while a raw win-rate is swingable by verbosity
# alone. The r6 reform (deploy/evidence/GATE_POWER_ANALYSIS_r6.md) moves the gate to
# judge_score = (wins + 0.5*ties)/n decided by its Wilson 95% interval, records
# answer lengths for a future length correction that cannot be FITTED yet (r5 has
# zero outcome variance), and adds an OOD layer that is measured and never gated.
# The methodology's only home is the eval harness prompt, so these guards pin the
# prompt the way the driver tests pin code.

def _eval_prompt_bullet(task):
    doc = json.loads((REPO / "agents/eval/harness.json").read_text())
    text = "".join(b.get("text", "") for b in doc["systemPrompt"])
    m = re.search(rf'- "{task}":(.*?)(?=\n- "[a-z_]+"|\nRules:)', text, re.S)
    assert m, f"eval prompt has no {task!r} bullet"
    return m.group(1)


def test_the_score_bullet_defines_the_tie_credited_metric():
    b = _eval_prompt_bullet("score")
    assert "judge_score = (wins + 0.5*ties) / n" in b, (
        "the gate metric's formula is gone from the score bullet -- ties fold back "
        "into losses and the plan's judge_score threshold gates a different quantity")
    for field in ("judge_score_ci_low", "judge_score_ci_high", "judge_n",
                  "student_answer_chars", "reference_answer_chars"):
        assert field in b, f"the score bullet no longer requires {field}"
    assert "ood_eval_uri" in b and "NEVER gated" in b, (
        "the OOD layer must be evaluated by score and marked report-only at the "
        "point where it is produced")


def test_the_score_bullet_keys_two_layers_apart_and_reconciles_each_denominator():
    """Two acceptance layers, one details file, and both number their rows from zero.

    Not hypothetical: the offline analysis behind deploy/evidence/SCALING_DIAGNOSIS_r6c_8B.md
    hit it under exactly the conditions this bullet now creates. 40 of 137 items shared an
    index across layers, so keying on the index cross-paired an ID item's A-position verdict
    with an OOD item's B-position verdict and dropped 40 ID items out of their own layer --
    the aggregate reported 57 items for a 97-item layer while every judgment behind it was
    correct. Bookkeeping, not judging, which is precisely why nothing looked wrong.

    The reconciliation is the load-bearing half. judge_n counts SCORED items, so a lost item
    shrinks the denominator silently, and a Wilson interval on a shrunken denominator is
    NARROWER around the wrong sample -- the one direction that turns a bookkeeping slip into a
    decisive gate verdict on a set nobody chose. `judge_n + judge_unscorable == items_in_layer`
    is the cheapest audit available here and it is what caught both defects in the first pass.
    """
    b = _eval_prompt_bullet("score")
    for token in ("item_id", 'layer ("id" or "ood")'):
        assert token in b, (
            f"the score bullet no longer requires {token} on every judge_details.jsonl row, so "
            "two layers sharing one file cannot be told apart")
    assert "never on a row number or a per-file index" in b, (
        "the bullet must forbid the key that actually broke, not merely suggest a better one")
    assert "items_in_layer" in b and "judge_n + judge_unscorable == items_in_layer" in b, (
        "the score bullet no longer reconciles each layer's denominator against the acceptance "
        "file it was scored on -- a silently shrunken n reads as a tighter interval")
    assert "escalate_human" in b, (
        "a denominator that does not reconcile must stop the stage, not annotate it")


def test_the_score_bullet_may_not_omit_the_ood_object_it_was_asked_for():
    """A report-only layer that can vanish withdraws the trade the gate was built on.

    The dual-layer design deliberately lets the OOD layer never block a deploy, and the
    only thing that makes that honest is that it is always measured and always reported.
    Nothing enforced the second half: `params.ood_eval_uri` set with no `report.json.ood`
    was byte-identical to a run that never asked for the layer -- the driver reads only
    `gate_passed`, and the console drew the block on presence alone. So an unreadable file,
    an exhausted budget, or a model that simply skipped it all landed as silence, and the
    page that would have shown the omission is the same page that shows the gate PASS.

    Absence must therefore be illegal rather than discouraged: the failure path writes the
    object WITH its error, because an instruction to report the layer is satisfied by a
    model that could not and said nothing.
    """
    b = _eval_prompt_bullet("score")
    assert "ABSENCE is never a legal outcome" in b, (
        "the score bullet asks for an `ood` object without making its omission illegal; a "
        "conditional instruction is silence-shaped when the condition is hard to meet")
    assert 'STILL write "ood"' in b and "ood_error" in b, (
        "there is no failure path that reports the OOD layer -- an unreadable or unscored "
        "layer has nothing to write, so it writes nothing and reads as never requested")
    assert "ID layer ALONE" in b, (
        "the bullet no longer says WHY the object is mandatory (the gate blocks on the ID "
        "layer alone), which is the sentence that makes the rule survive an edit")


def test_the_gate_bullet_decides_by_the_wilson_interval():
    b = _eval_prompt_bullet("gate")
    assert "LOWER bound (judge_score_ci_low)" in b and \
           "UPPER bound (judge_score_ci_high)" in b, (
        "the CI decision rule is gone -- the gate is back to point-estimate theater")
    assert "escalate_human" in b
    assert "no plan signed" in b, (
        "the gate bullet no longer forbids gating on the OOD report")


# ── D12: the gate's borderline verdict had nowhere to go ──────────────────────────────
# The CI rule above produces three outcomes, and the third one -- borderline -- was routed
# to `escalate_human`, which is the ONE call that makes a human answer undeliverable:
# `escalated` is in the driver's UNREACHABLE_RUN_STATES, so resolve_escalation refuses to
# park a verdict for it and no checkpoint will ever run again to receive one. The only
# other channel, `checkpoint`, notifies nobody. So the design asked a question through the
# door it had just locked. These pin the third channel and the protocol that uses it.

def test_the_eval_agent_can_ask_a_question_that_can_still_be_answered():
    """A protocol naming a tool the harness does not declare is a protocol the agent cannot
    follow -- it reaches for the nearest declared thing instead, which here is the call that
    ends the run."""
    doc = json.loads((REPO / "agents/eval/harness.json").read_text())
    tools = {t["name"]: t for t in doc["tools"] if t.get("type") == "inline_function"}
    assert "page_human" in tools, "the eval harness declares no way to notify a human"
    assert "page_human" in doc["allowedTools"], (
        "page_human is declared and not allowed, so every call answers 'unsupported'")
    schema = tools["page_human"]["config"]["inlineFunction"]["inputSchema"]
    assert set(schema["required"]) == {"situation", "recommendation"}, (
        f"a page must carry the analysis the agent already did: {schema['required']}")
    assert "run_id" not in schema["properties"], (
        "the eval page declares run_id, so a model can address a page at a run it was not "
        "invoked for -- the mis-addressing the triage path already paid for; the driver "
        "reads the subject off the invocation")
    driver_src = (REPO / "orchestration/harness_driver/handler.py").read_text()
    assert 'name == "page_human"' in driver_src, (
        "the eval prompt tells the agent to page and the driver does not service it")


def test_a_borderline_gate_score_no_longer_routes_to_the_call_that_ends_the_run():
    """The defect, in one sentence: escalate_human ENDS the run, and a borderline score is
    the one gate outcome a human answer can unblock. Sending it there destroyed the run the
    answer was for, so the operator's verdict had nowhere to land -- the same
    undeliverable-verdict shape as #16, arrived at from the prompt instead of the code."""
    b = _eval_prompt_bullet("gate")
    # The third outcome of the CI rule, not the first mention of the word: the missing-gates
    # sentence above says "NOT the borderline protocol below", and slicing from there would
    # read the terminal case as the borderline one.
    proto = b[b.index("statistically borderline"):]
    assert "page_human" in proto and "checkpoint" in proto, (
        f"the borderline branch still has no way to be answered: {proto[:300]}")
    assert "do NOT reach for escalate_human" in proto, (
        "the borderline branch no longer rules out the call that makes the answer "
        f"undeliverable: {proto[:300]}")
    assert proto.index("page_human") < proto.index("after 6 checkpoints"), (
        "the unanswered-page fallback is offered before the page itself, which reads as "
        "the primary choice")
    # ...and the wait is BOUNDED. Every waiting turn bills real model tokens against this
    # run, so an unbounded wait is a cost with no owner -- and a page nobody answers has to
    # end somewhere.
    assert "6 checkpoints" in b, (
        "the borderline protocol does not bound the wait, so an unanswered page waits "
        "until maxIterations at model-token cost")
    # The two terminal cases stay terminal, and say why -- a directive must not be able to
    # supply a bar the signed plan does not carry, and a bar inside its own ceiling band is
    # a defect in the signed number, which is re-signed rather than directed.
    assert b.count("terminal on purpose") == 2, (
        f"the two deliberately-terminal gate exits no longer say so: {b.count('terminal on purpose')}")


def test_the_escalation_tool_no_longer_advertises_the_verdict_it_cannot_deliver():
    """Its own description claimed borderline gate scores -- 'including borderline gate
    scores' -- which is exactly the case it cannot serve. A tool description is the shortest
    path an agent has to a wrong choice."""
    doc = json.loads((REPO / "agents/eval/harness.json").read_text())
    esc = [t for t in doc["tools"] if t.get("name") == "escalate_human"][0]
    desc = json.dumps(esc["config"]["inlineFunction"])
    assert "borderline gate scores" not in desc, (
        "escalate_human still advertises itself for borderline gate scores")
    assert "page_human" in desc, (
        "escalate_human's description does not point at the channel that CAN be answered, "
        "so an agent reading only this tool has no alternative to reach for")


def test_the_turn_end_invariant_lists_the_third_channel():
    """The invariant is the sentence an agent re-reads when it does not know how to end a
    turn. A channel missing from it is a channel that gets used by accident, if at all."""
    doc = json.loads((REPO / "agents/eval/harness.json").read_text())
    text = "".join(b.get("text", "") for b in doc["systemPrompt"])
    assert text.count("TURN-END INVARIANT") == 1
    sentence = text.split("TURN-END INVARIANT")[1].split("\n- ")[0]
    for call in ("stage_complete", "checkpoint", "escalate_human", "page_human"):
        assert call in sentence, f"the turn-end invariant does not mention {call}"
    # A page is NOT a way to end a turn -- the driver keeps the turn precisely because the
    # invocation still holds the task token -- so the invariant has to say what follows it.
    assert "followed by a checkpoint" in sentence, (
        f"the invariant lists page_human without saying a page does not end a turn: "
        f"{sentence[:400]}")


def test_the_power_analysis_numbers_are_recomputable():
    """The numbers the r6 plan's gates are signed against, recomputed rather than
    trusted -- a number in prose is unchecked, and this doc is the approval's basis."""
    doc = (REPO / "deploy/evidence/GATE_POWER_ANALYSIS_r6.md").read_text()
    za, zb = 1.96, 0.8416
    mde40 = (za + zb) * math.sqrt(0.25 / 40)
    assert f"{mde40*100:.1f}pp" in doc, "the n=40 MDE in the doc no longer matches Eq 9"

    def power(p0, p1, n):
        z = 1.6449  # one-sided 5%
        crit = p0 + z * math.sqrt(p0 * (1 - p0) / n)
        return 0.5 * (1 + math.erf((p1 - crit) / math.sqrt(p1 * (1 - p1) / n) / math.sqrt(2)))

    assert f"{power(0.50, 0.55, 40)*100:.1f}%" in doc, "the n=40 power figure drifted"
    assert f"{power(0.45, 0.60, 100)*100:.0f}% power at n=100" in doc, \
        "the n=100 power behind the recommended design drifted"
    assert f"{power(0.45, 0.60, 150)*100:.0f}% at n=150" in doc, \
        "the n=150 power behind the recommended design drifted"
    # rule-of-3 upper bounds for the observed 0/40 and 0/80
    assert f"{3/40*100:.1f}%" in doc and f"{3/80*100:.1f}%" in doc, \
        "the observed-zero upper bounds drifted"


def test_the_scaling_diagnosis_numbers_reconcile_with_each_other():
    """The r6c 8B diagnosis is the document that says "stop buying bigger students", so
    every number in it is recomputed from another number in it rather than trusted.

    Same rule as the power analysis above, one step stricter: the raw judgments live
    outside the repo (137 items x 2 positions, judged offline against surviving S3
    artifacts), so what CAN be checked here is internal consistency -- and that is exactly
    what caught this analysis's real bugs. The first aggregate said 57 items in a 97-item
    layer, and the tell was not a bad score, it was two counts that would not reconcile.
    A doc whose parts agree only in prose has the same defect at rest.
    """
    doc = (REPO / "deploy/evidence/SCALING_DIAGNOSIS_r6c_8B.md").read_text()

    # 1. The headline table: n must be the outcomes it is made of, judge_score must be the
    #    tie-credited formula the gate uses, and the interval must be the Wilson interval
    #    the reformed gate decides by -- read out of the table, not restated here.
    rows = re.findall(r"^\| (?:in|out-of)-distribution \(`(\w+)`\) \| (\d+) \| (\d+) \| (\d+) \| "
                      r"(\d+) \| \*\*([0-9.]+)\*\* \| \[([0-9.]+), ([0-9.]+)\]",
                      doc, re.M)
    assert len(rows) == 2, "the diagnosis' headline table no longer parses; nothing below is checked"
    judged = {}
    for layer, n, w, t, l, score, lo, hi in rows:
        n, w, t, l = int(n), int(w), int(t), int(l)
        assert w + t + l == n, f"{layer}: {w}+{t}+{l} outcomes reported under n={n}"
        successes = w + 0.5 * t
        assert f"{successes / n:.4f}" == score, (
            f"{layer}: judge_score {score} is not (wins + 0.5*ties)/n = {successes / n:.4f} "
            "-- the doc reports a quantity the gate does not compute")
        z, p = 1.96, successes / n
        d = 1 + z * z / n
        centre = (p + z * z / (2 * n)) / d
        half = (z / d) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
        assert f"{centre - half:.3f}" == lo and f"{centre + half:.3f}" == hi, (
            f"{layer}: interval [{lo}, {hi}] is not Wilson 95% at ({successes}, {n}) = "
            f"[{centre - half:.3f}, {centre + half:.3f}]")
        judged[layer] = n
        if layer == "id":
            assert centre + half < 0.45, (
                "the ID upper bound no longer clears the 0.45 bar, so the doc's decisive "
                "FAIL is not what its own numbers say")

    # 2. Judged + unscorable must equal the layer. This is the count that caught the
    #    idx-collision bug, so it is the one a reader is owed: the acceptance set is
    #    97 ID + 40 OOD = the 137 answers the run produced, and the four items no judge
    #    could settle are named individually rather than subtracted silently.
    unscorable = re.search(r"Unscorable and reported, not scored: (.+?) \(see", doc).group(1)
    named = re.findall(r"`(id|ood)#\d+`", unscorable)
    assert "137 student answers" in doc and "97-row ID" in doc
    for layer, size in (("id", 97), ("ood", 40)):
        assert judged[layer] + named.count(layer) == size, (
            f"{layer}: {judged[layer]} judged + {named.count(layer)} unscorable != {size} "
            "in the layer -- items are vanishing between the table and the set")
    assert judged["id"] + judged["ood"] + len(named) == 97 + 40 == 137

    # 3. The decontamination chain, arithmetic and all: this is the doc's actual argument,
    #    and "41%" is the number the four options are ranked against.
    chain = [int(x) for x in re.findall(r"^ ?-?(\d+) (?:input rows|exact|near|dropped|quality)",
                                        doc, re.M)]
    assert chain[:5] == [300, 39, 33, 94, 13], f"the chain no longer reads as stated: {chain}"
    start, ex, near, decon, qual = chain[:5]
    assert f"-> {start - ex - near}" in doc and f"-> {start - ex - near - decon}" in doc
    assert f"{start - ex - near - decon - qual} output rows" in doc
    train, val = (int(x) for x in re.search(r"\((\d+) train / (\d+) val\)", doc).groups())
    assert train + val == start - ex - near - decon - qual, (
        f"{train} train + {val} val != the {start - ex - near - decon - qual} rows curated")
    assert f"{decon} of the {start - ex - near} surviving rows — " \
           f"{round(decon / (start - ex - near) * 100)}%" in doc, \
        "the share deleted for resembling the acceptance set drifted from its own operands"

    # 4. The guardrail-refusal rate, and the cost. A judged item costs two calls, so the
    #    slot count is derivable; the cost is derivable from the token counts and Opus 5
    #    list price, which is what makes "I estimated $5 and spent $9.08" auditable rather
    #    than an apology.
    slots, pct = re.search(r"\*\*(\d+) of the (?:\d+) \(item, position\) slots — ([0-9.]+)%",
                           doc).groups()
    assert f"of the {137 * 2} (item, position) slots" in doc, \
        "274 slots is 137 items judged in both positions; the doc no longer says so"
    assert f"{int(slots) / (137 * 2) * 100:.1f}" == pct, "the content_filtered rate drifted"
    tin, tout = (int(x.replace(",", "")) for x in
                 re.search(r"\(([\d,]+) input \+ ([\d,]+) output tokens", doc).groups())
    assert f"${tin * 15 / 1e6 + tout * 75 / 1e6:.2f} of judge" in doc, (
        "the stated cost is not the token counts at Opus 5 list price ($15/$75 per Mtok) "
        "-- a cost claim nobody can rederive is the estimate all over again")


# ── the measuring instrument itself is pinned, and n stops shrinking silently ─────────
# Two defects the 8B diagnosis surfaced, both about the ruler rather than the student.
# (1) The pairwise judge prompt had no home: the eval prompt said "fixed judge prompts"
# and fixed none, and the mirrored llm-evaluation skill implements only a 1-5 absolute
# score, so the instrument was re-authored every run. r5's `judge_ties: 0` was read as a
# fact about the student and was a fact about that run's A-or-B-only prompt. (2) A judge
# call that is content-filtered, unparseable or truncated produced no verdict, and
# nothing said what happens to the item -- so `n` shrank and the report still looked
# complete. Both are now prompt-shape guards, because the prompt is where the
# methodology lives.

def _eval_instrument_mirror():
    """The S3 URI the deploy actually uploads the instrument to, derived from the deploy.

    Read out of `ensure_eval_instrument`'s own dry-run rather than restated, so a prompt
    naming a key nothing mirrors fails here. That failure mode is not hypothetical in this
    repo: the finetune agent authored its own trainer on every run because the canonical
    script it was told to download was unreachable, and the prompt and the deploy each
    looked correct in isolation.
    """
    storage = _load("llmops_03_storage_for_eval", "deploy/03_storage.py")
    got = storage.ensure_eval_instrument(None, "<bucket>", dry=True)
    files = sorted(p.name for p in (REPO / "pipeline/eval").glob("*") if p.is_file())
    assert files, "pipeline/eval/ is empty, so there is no instrument to pin"
    assert len(files) == 1, (
        f"pipeline/eval/ holds {files}; this guard pins ONE canonical instrument and the "
        "eval prompt names it by name -- decide which is canonical or teach both sides")

    # The DRY report is not evidence about the upload. Both branches had their own
    # f-string at first and a mutation proved they could disagree with every guard green:
    # --dry-run promising code/eval/ while the real PutObject wrote elsewhere is a deploy
    # path that lies, which is worse than none. So run the real branch against a fake
    # client and assert the key it actually writes is the one the dry report named.
    class _FakeS3:
        def __init__(self):
            self.written = {}

        def upload_file(self, local, bucket, key):
            self.written[key] = pathlib.Path(local).read_bytes()

        def get_object(self, Bucket, Key):  # noqa: N803 - boto3's own spelling
            return {"Body": io.BytesIO(self.written[Key])}

    fake = _FakeS3()
    real = storage.ensure_eval_instrument(fake, "<bucket>", dry=False)
    assert sorted(fake.written) == [f"code/eval/{n}" for n in files], (
        f"the upload branch wrote {sorted(fake.written)}, the dry branch promised "
        f"{got['to']} -- the two spellings have diverged")
    for key in fake.written:
        assert got["to"] + key.rsplit("/", 1)[1] == f"s3://<bucket>/{key}", (
            f"dry-run reports {got['to']}, the upload writes {key}")
    # Every field a human reads out of either branch, not just the one this helper returns.
    # `to` and the file count are what the deploy log shows and what a reviewer checks the
    # mirror against; both survived a mutation while the upload itself stayed correct, which
    # is a deploy that did the right thing and reported a different one.
    assert real["to"] == got["to"], (
        f"the upload branch reports {real['to']}, the dry branch {got['to']} -- the deploy "
        "log would name a prefix the deploy did not write")
    assert got["would"] == f"upload {len(files)} eval instrument files", (
        f"--dry-run says {got['would']!r} for {len(files)} file(s) in pipeline/eval/")
    # The digests, recomputed -- not just present. `verified[name] = ""` survived the first
    # version of this assertion, which checked the KEYS only: a deploy log full of empty
    # digests would then have read as "instrument pinned, digest recorded".
    assert set(real["uploaded_and_verified"]) == set(files), \
        "the deploy no longer reports a digest per instrument file"
    for name, digest in real["uploaded_and_verified"].items():
        want = hashlib.sha256((REPO / "pipeline/eval" / name).read_bytes()).hexdigest()
        assert digest == want, (
            f"the deploy reports {digest!r} for {name}, its bytes hash to {want} -- "
            "report.json's judge_prompt_sha256 comes from here, so a wrong or empty digest "
            "makes 'the same ruler' unfalsifiable")
    return got["to"] + files[0]


def test_the_score_bullet_reads_the_canonical_judge_prompt_instead_of_writing_one():
    b = _eval_prompt_bullet("score")
    uri = _eval_instrument_mirror()
    assert uri in b, (
        f"the score bullet no longer names {uri} -- the pairwise instrument is then "
        "re-authored every run and two runs' judge_score are not comparable (r5 reported "
        "judge_ties: 0 from a prompt that offered no tie)")
    assert "judge_prompt_sha256" in b, (
        "the report must carry the instrument's digest; without it 'same ruler' is a claim "
        "about the past rather than a check a reader can run")
    assert "do NOT fall back to authoring your own judge prompt" in b, (
        "an unreadable instrument must stop the stage. Falling back is worse than failing: "
        "the run continues and emits a number that looks like the last one")


def test_the_score_bullet_will_not_let_an_unjudgeable_item_vanish():
    b = _eval_prompt_bullet("score")
    for field in ("judge_unscorable", "judge_unscorable_ids"):
        assert field in b, f"the score bullet no longer requires {field}"
    assert "counts SCORED items only" in b, (
        "judge_n must exclude unscorable items explicitly -- the harm here is a silently "
        "shrinking denominator, and a report that does not say so cannot be audited")
    assert "never counted as a tie, a win or a loss" in b, (
        "an unscorable item folded into any verdict is a made-up measurement")
    assert "stop reason" in b and "maxTokens" in b, (
        "a reasoning judge bills reasoning as output tokens: at 400 it returns an EMPTY "
        "text block with stopReason max_tokens, which is not a tie. The prompt must make "
        "that visible rather than leave it to be inferred")


def test_the_gate_bullet_asks_whether_the_unscored_items_could_change_its_answer():
    b = _eval_prompt_bullet("gate")
    for imputation in ("as a win", "as a loss", "as a tie"):
        assert imputation in b, (
            f"the gate bullet no longer imputes unscorable items {imputation} -- a fixed "
            "tolerance cannot answer whether the missing items matter, and the missingness "
            "is not random (the filter clusters on credential and access content)")
    assert "identical under all three" in b and "escalate_human" in b, (
        "the rule must be decision-relevance: proceed when the verdict cannot move, "
        "escalate when it can")


def test_the_deploy_refuses_an_instrument_that_did_not_land_intact():
    """A read-back that is never allowed to differ is not a verification.

    `_eval_instrument_mirror`'s fake client echoes back exactly what it stored, so its
    `got != body` branch is unreachable there -- defanging that check to `got is None`
    survived a mutation with the whole eval suite green. This is the other half: a client
    whose GetObject returns something else must stop the deploy, because the point of the
    read-back is the case where S3 holds bytes the repo does not.
    """
    storage = _load("llmops_03_storage_readback", "deploy/03_storage.py")

    class _LyingS3:
        def upload_file(self, local, bucket, key):
            pass

        def get_object(self, Bucket, Key):  # noqa: N803 - boto3's own spelling
            return {"Body": io.BytesIO(b"not what the repo holds")}

    with pytest.raises(SystemExit) as e:
        storage.ensure_eval_instrument(_LyingS3(), "<bucket>", dry=False)
    assert "read-back mismatch" in str(e.value), (
        f"the deploy exited with {e.value!r} instead of naming the mismatch -- the judge "
        "prompt's digest is recorded from local bytes, so an upload that silently did not "
        "land makes every run's judge_prompt_sha256 a claim about the wrong file")


def test_the_deploy_refuses_to_mirror_an_empty_instrument_directory(tmp_path):
    """An empty mirror is the failure this whole file exists to prevent, so it must be loud.

    The eval prompt is told to escalate rather than author its own judge prompt, so a
    successful deploy that uploaded nothing would stall every scoring stage with a message
    about S3 rather than about the deploy. Exercised by loading the deploy module beside an
    empty pipeline/eval/, since the source directory is derived from the module's own path.
    """
    d = tmp_path / "deploy"
    d.mkdir()
    (tmp_path / "pipeline" / "eval").mkdir(parents=True)
    copy = d / "03_storage.py"
    copy.write_bytes((REPO / "deploy/03_storage.py").read_bytes())
    storage = _load("llmops_03_storage_empty_eval", str(copy))

    for dry in (True, False):
        with pytest.raises(SystemExit) as e:
            storage.ensure_eval_instrument(None, "<bucket>", dry=dry)
        assert "pipeline/eval/ is empty" in str(e.value), (
            f"dry={dry} exited with {e.value!r}; the refusal must name the empty directory")


def test_the_canonical_judge_instrument_is_internally_consistent():
    """The instrument file's own numbers, recomputed. Same rule as the diagnosis doc it
    was extracted from: a worked example nobody derives is prose, and this one exists to
    let a reader calibrate a rule that deliberately did NOT fire on the run that motivated
    it -- so the rows must genuinely all reach the same verdict."""
    doc = (REPO / "pipeline/eval/judge_prompt_pairwise.md").read_text()

    # The four substitutions, and only those four: anything else varying between runs is
    # the drift this file exists to stop.
    assert re.search(r"there are exactly four", doc)
    placeholders = set(re.findall(r"\{(\w+)\}", doc))
    assert placeholders == {"task_description", "prompt", "a", "b"}, (
        f"the instrument's substitution set is {sorted(placeholders)}; the prompt body and "
        "the rule above it must agree on exactly which four vary")
    assert '"winner": "A" | "B" | "tie"' in doc, (
        "the verdict set is the whole finding: an A-or-B-only instrument produced r5's "
        "judge_ties: 0 and a 38-point shift in the reported tie rate")

    rows = re.findall(r"^\| (ID|OOD) \| ([a-z= ]+) \| (\d+) \| (\d+) \| (\d+) \| (\d+) \| "
                      r"([0-9.]+) \| \[([0-9.]+), ([0-9.]+)\] \| (\w+) \|", doc, re.M)
    assert len(rows) == 8, (
        f"the worked example parses as {len(rows)} rows, not 8 (2 layers x as-scored plus "
        "three imputations); nothing below is checked otherwise")
    verdicts = {}
    for layer, imputation, n, w, t, l, score, lo, hi, verdict in rows:
        n, w, t, l = int(n), int(w), int(t), int(l)
        assert w + t + l == n, f"{layer}/{imputation}: {w}+{t}+{l} outcomes under n={n}"
        successes = w + 0.5 * t
        assert f"{successes / n:.4f}" == score, (
            f"{layer}/{imputation}: {score} is not (wins + 0.5*ties)/n = {successes / n:.4f}")
        z, p = 1.96, successes / n
        d = 1 + z * z / n
        centre = (p + z * z / (2 * n)) / d
        half = (z / d) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
        assert f"{centre - half:.3f}" == lo and f"{centre + half:.3f}" == hi, (
            f"{layer}/{imputation}: [{lo}, {hi}] is not Wilson 95% at ({successes}, {n}) = "
            f"[{centre - half:.3f}, {centre + half:.3f}]")
        # The verdict column must be what the gate's own rule returns at the 0.45 bar,
        # not a label. This is the assertion that makes the table an example of the RULE
        # rather than a table that happens to sit under it.
        expect = "PASS" if centre - half >= 0.45 else \
                 ("FAIL" if centre + half < 0.45 else "BORDERLINE")
        assert verdict == expect, (
            f"{layer}/{imputation}: the row says {verdict}, the Wilson rule at bar 0.45 "
            f"says {expect}")
        verdicts.setdefault(layer, set()).add(verdict)
    for layer, seen in verdicts.items():
        assert len(seen) == 1, (
            f"{layer}: the imputations disagree ({sorted(seen)}), so this run WOULD have "
            "escalated and the doc's claim that the rule stayed quiet is false")
    assert "would NOT have fired" in doc, (
        "the calibration point is that the rule was silent here; if the numbers ever say "
        "otherwise, the prose has to change with them")


# ── the non-run heartbeat and its resurrection (#37) ─────────────────────────────────
# A triage runs under `triage-<subject>` and deliberately has no run row, so for as
# long as the resurrector's only eye was the runs table, a dead triage was unrevivable:
# the async self-reinvoke that Lambda dropped on 2026-08-08 for a RUN would, dropped
# for a TRIAGE, leave the escalation unanswered forever with nothing whose job it was
# to notice. Widening attribute_exists(run_id) was rejected -- a minted row carrying
# driver_beat_at IS a resurrectable ghost run -- so the beat routes into EVENTS_TABLE's
# dedicated `__liveness__` partition, and the resurrector reads that one partition.

def test_the_two_liveness_partition_spellings_are_one():
    """Driver and resurrector ship in separate bundles and each declares the constant;
    same argument as the console's DIRECTIVE_SK pin."""
    assert driver.LIVENESS_PK == resurrector.LIVENESS_PK


class TestNonRunLivenessBeat:
    def _triage_event(self):
        ev_ = driver_event()
        ev_["run_id"] = "triage-run-x"
        ev_["stage"] = "orchestrator"
        ev_["task"] = "triage"
        ev_["params"] = {"escalation": {"run_id": "run-subject-7", "reason": "gate"}}
        ev_.pop("task_token", None)
        return ev_

    def _beats(self, c):
        return [i for i in c["ddb"].Table(os.environ["EVENTS_TABLE"]).items
                if i.get("run_id") == driver.LIVENESS_PK]

    def test_a_crashed_triage_leaves_a_revivable_beat_carrying_its_work_order(self):
        """The beat exists FOR the crash: a dropped async invoke or a hard death must
        leave (a) an item the sweep can see and (b) the params -- a triage's work order
        lives nowhere else, and a revival without params.escalation triages blind and
        files its pages under an empty subject (review finding 1)."""
        class _CrashingAgentCore(FakeAgentCore):
            def invoke_harness(self, **kw):
                raise RuntimeError("hard crash mid-stage")

        c = clients(_CrashingAgentCore([]))
        with pytest.raises(Exception):
            driver.handler(self._triage_event(), clients=c)
        beats = self._beats(c)
        assert beats and beats[0]["sk"] == "beat#triage-run-x", (
            "a crashed triage left no liveness item -- it is unrevivable again")
        payload = json.loads(beats[0]["payload"])
        assert payload["params"]["escalation"]["run_id"] == "run-subject-7", (
            "the beat payload dropped params -- a revived triage loses its entire "
            "work order and triages blind")

    def test_a_finished_triage_deletes_its_beat(self):
        """An ending is not a death. A done_at MARK was the first design and the review
        killed it twice over: a marked item is immortal (96 sweeps/day re-read all
        history forever) and its resurrection count leaks into the subject's next
        incarnation, which would arrive dead at an inherited cap."""
        c = clients(FakeAgentCore([text_stream("no verdict")] * 4))
        driver.handler(self._triage_event(), clients=c)
        assert not self._beats(c), (
            "a FINISHED triage's liveness item survived -- the resurrector will "
            "revive it and page a second human about an answered question")

    def test_a_run_with_a_row_writes_no_liveness_item(self):
        uri = "s3://llmops-data-test/runs/run-test-1/raw/data.jsonl"
        ac = FakeAgentCore([tool_use_stream("stage_complete", {"outputs": [uri]}),
                            text_stream("ack")])
        c = clients(ac, FakeS3(existing=[uri]))
        ev_ = driver_event()
        c["ddb"].Table(os.environ["RUNS_TABLE"]).items.append(
            {"run_id": ev_["run_id"], "status": "running"})
        driver.handler(ev_, clients=c)
        assert not self._beats(c), (
            "a run with a run row also beat into __liveness__ -- two heartbeats for "
            "one invocation means two resurrectors can each claim one")

    def test_a_scheduled_job_is_not_made_revivable(self):
        """Review finding 4: monitor_sweep (sweep-<date>) and finops_reconcile
        (finops-<period>) also invoke the driver with non-run ids -- and their work is
        NOT idempotent (variance pages, audit rows). A crashed scheduled job waits for
        its next schedule; only a triage, whose escalation exists nowhere else, earns
        a revival."""
        ev_ = driver_event()
        ev_["run_id"] = "sweep-2026-08-12"
        ev_["stage"] = "monitor"
        ev_["task"] = "sweep"
        ev_.pop("task_token", None)
        c = clients(FakeAgentCore([text_stream("no call")] * 4))
        driver.handler(ev_, clients=c)
        assert not self._beats(c), (
            "a scheduled sweep became revivable -- five async re-runs of "
            "non-idempotent work, 20 minutes apart")


def _liveness_item(subject="triage-run-x", minutes_old=45, **over):
    at = (datetime.datetime.now(datetime.timezone.utc)
          - datetime.timedelta(minutes=minutes_old)).isoformat()
    item = {"run_id": resurrector.LIVENESS_PK, "sk": f"beat#{subject}",
            "beat_at": at,
            "payload": json.dumps({"run_id": subject, "stage": "orchestrator",
                                   "task": "triage", "harness_id": "llmops_orchestrator",
                                   "manifest_uri": "", "iteration": 0,
                                   "params": {"escalation":
                                              {"run_id": "run-subject-7"}}})}
    item.update(over)
    return item


class TestNonRunResurrection:
    def _run(self, items, env_over=None):
        env = {**RES_ENV, **(env_over or {})}
        for k, v in env.items():
            os.environ[k] = v
        c = _res_clients([])
        c["ddb"].Table(RES_ENV["EVENTS_TABLE"]).items.extend(items)
        out = resurrector.handler({}, clients=c)
        return out, c

    def test_a_stale_liveness_beat_is_resurrected_with_its_own_payload(self):
        out, c = self._run([_liveness_item()])
        assert out["acted"] and out["acted"][0]["action"] == "resurrected"
        assert out["acted"][0]["run_id"] == "triage-run-x"
        sent = json.loads(c["lambda"].calls[0]["Payload"])
        assert sent["run_id"] == "triage-run-x" and sent["task"] == "triage"
        assert any(e["DetailType"] == ev.DRIVER_RESURRECTED
                   for e in c["events"].entries)

    def test_an_ended_triage_left_nothing_to_revive(self):
        """Deletion IS the done mark now: after _settle_liveness the partition holds
        no item, so an empty sweep proves the ending was honored."""
        out, c = self._run([])
        assert not out["acted"] and not c["lambda"].calls

    def test_a_fresh_liveness_beat_is_left_alone(self):
        out, c = self._run([_liveness_item(minutes_old=5)])
        assert not out["acted"] and not c["lambda"].calls

    def test_the_liveness_cap_escalates_once_against_the_original_subject(self):
        """Two review findings in one: (a) the escalation subject must be the run the
        triage was ABOUT (its own `triage-<x>` id would mint a recursive
        `triage-triage-<x>` against a manifest that does not exist), and (b) the item
        must be DELETED with the escalation, or the 15-minute sweep re-escalates it
        forever -- 96 billed triages a day."""
        out, c = self._run([_liveness_item(resurrections=5)])
        assert out["acted"] and out["acted"][0]["action"] == "escalated"
        assert not c["lambda"].calls
        esc = [json.loads(e["Detail"]) for e in c["events"].entries
               if e["DetailType"] == ev.ESCALATED_TO_HUMAN]
        assert esc and esc[0]["run_id"] == "run-subject-7", (
            f"the cap escalation is addressed to {esc and esc[0]['run_id']!r}, not to "
            "the original subject -- triage_event_from_bus will nest triage-triage-")
        left = [i for i in c["ddb"].Table(RES_ENV["EVENTS_TABLE"]).items
                if i.get("run_id") == resurrector.LIVENESS_PK]
        assert not left, (
            "the cap-exhausted item survived its escalation -- every 15-minute sweep "
            "re-escalates it forever")

    def test_the_sweep_reports_what_it_checked_because_nothing_reads_its_return(
            self, capsys):
        """Verified live on 2026-08-12: 23 post-deploy invocations, 0 errors -- which
        proves the Query is PERMITTED, and nothing more. The schedule invokes this
        Lambda asynchronously, so the returned counts are discarded, and an idle sweep
        was indistinguishable in CloudWatch from a Query against the wrong partition:
        both are START/END/REPORT and silence. The non-run half's healthy state is
        `0 beats` on every day no triage is dead, so a count nobody can read is the
        whole audit trail missing."""
        self._run([_liveness_item(minutes_old=5)])
        line = [l for l in capsys.readouterr().out.splitlines()
                if l.startswith("[resurrector] ")]
        assert len(line) == 1, f"expected exactly one summary line, got {line}"
        got = json.loads(line[0][len("[resurrector] "):])
        assert got["checked_liveness"] == 1, (
            "the printed count must be the number of beats the Query actually returned; "
            f"got {got}")
        assert got["acted"] == [], "a fresh beat is not an action"

    def test_the_summary_line_names_the_beat_it_revived(self, capsys):
        """The counts alone would let a resurrection go unlogged, which is the one event
        in this Lambda worth reconstructing after the fact."""
        self._run([_liveness_item()])
        out = capsys.readouterr().out
        assert "triage-run-x" in out and "resurrected" in out, out


# ── a platform outage is not the agent's answer ──────────────────────────────────────
# r6c's EvalScore met a Bedrock ServiceUnavailable storm (02:18-02:36Z, 2026-08-12)
# after its one salvage retry was spent: every further dead-stream turn was billed to
# the PROSE budget (re_asks 0->1->2, text_chars=0, error= on every log line) and the
# stage died MissingStageComplete with its inference outputs already in S3; the triage
# then suffocated in the same storm. Same disease as content_filtered, same cure.

class TestModelOutageBudget:
    def test_a_dead_stream_turn_does_not_spend_the_prose_budget(self, monkeypatch):
        monkeypatch.setattr(driver.time, "sleep", lambda s: None)
        uri = "s3://llmops-data-test/runs/run-test-1/raw/data.jsonl"
        ac = FakeAgentCore([
            text_stream("narrating instead of calling"),   # re_ask 1
            text_stream("still narrating"),                # re_ask 2 (cap)
            DyingStream(),                                 # salvage retry
            DyingStream(),                                 # outage budget, not prose
            tool_use_stream("stage_complete", {"outputs": [uri]}),
            text_stream("ack")])
        c = clients(ac, FakeS3(existing=[uri]))
        out = driver.handler(driver_event(), clients=c)
        assert out["status"] == "completed", (
            "a dead-stream turn was billed to the prose budget and killed a stage "
            "whose very next turn completed the protocol")
        assert not c["sfn"].failures

    def test_a_sustained_outage_settles_as_model_unavailable(self, monkeypatch):
        monkeypatch.setattr(driver.time, "sleep", lambda s: None)
        ac = FakeAgentCore([DyingStream() for _ in range(6)])
        c = clients(ac)
        out = driver.handler(driver_event(), clients=c)
        assert out == {"status": "failed", "reason": "model_unavailable"}
        assert c["sfn"].failures[0]["error"] == "ModelUnavailable", (
            "a vendor outage must not be labelled MissingStageComplete -- one sends "
            "the operator to the AWS status page, the other into a transcript that "
            "never existed")
        # 6 deaths: salvage + budget 1-4 + the exhausting one. The first cause string
        # said 5 -- an operator correlating against the vendor status page would
        # reconstruct the outage window one turn short (review finding 9).
        assert "6 consecutive turns" in c["sfn"].failures[0]["cause"]
        assert any(e["DetailType"] == ev.PIPELINE_FAILED for e in c["events"].entries)

    def test_the_outage_budget_survives_a_self_reinvoke(self):
        """Same argument as _re_asks and _filtered_turns: the live failure spanned
        several invocations' worth of wall clock, so a counter that resets on handoff
        can never exhaust and the stage hangs in retry forever."""
        src = (REPO / "orchestration/harness_driver/handler.py").read_text()
        body = src[src.index("def _run_stage"):]
        assert '"_infra_error_turns": infra_error_turns' in body, \
            "_self_reinvoke stopped carrying the outage counter"
        assert 'int(event.get("_infra_error_turns", 0))' in body, \
            "a continuation no longer restores the outage counter"


def test_a_job_launched_without_a_name_is_rejected_not_crashed():
    """Live (r6a): a remediating agent's job_launched carried job_name="" -- a GSI key
    on the runs table, which DynamoDB rejects -- and the unguarded write took down the
    whole invocation as DriverCrashed. The empty call is the AGENT's protocol slip; it
    gets the same treatment as a stage_complete with missing outputs: rejected into
    the same turn with instructions, never written."""
    ac = FakeAgentCore([
        tool_use_stream("job_launched", {"job_name": ""}),
        tool_use_stream("job_launched", {"job_name": "llmops-qlora-run-test-1-i0"}),
        text_stream("ack")])
    c = clients(ac)
    out = driver.handler(driver_event(), clients=c)
    assert out["status"] == "released" and out["job_name"] == "llmops-qlora-run-test-1-i0", (
        f"the empty job_launched was not rejected-and-retried: {out}")
    written = [u["ExpressionAttributeValues"].get(":j")
               for u in c["ddb"].Table(os.environ["RUNS_TABLE"]).updates
               if ":j" in (u.get("ExpressionAttributeValues") or {})]
    assert "" not in written, (
        "an empty job_name reached the runs-table write -- in production that write "
        "is a ValidationException that kills the invocation")


def test_the_outage_backoff_respects_the_lambda_wall(monkeypatch):
    """A real stream death can arrive with ~45s of wall left (DRAIN_DEADLINE_MARGIN_MS);
    sleeping 60s there gets the invocation hard-killed mid-sleep -- no reinvoke, no
    settle, the counter's progress lost (review finding 7). When the backoff does not
    fit, the retry is handed to a fresh invocation; its cold start IS the backoff."""
    slept = []
    monkeypatch.setattr(driver.time, "sleep", lambda s: slept.append(s))
    ac = FakeAgentCore([DyingStream(), DyingStream()])  # salvage, then outage branch

    class _TightCtx:
        function_name = "llmops-harness-driver"
        def get_remaining_time_in_millis(self):
            # Plenty of wall until the second turn has actually run (so the loop-top
            # _out_of_time check lets both turns start), then the late-turn squeeze:
            # the stream death arrived with 70s left, the way a real death at the
            # ~840s turn cap does.
            return 900_000 if len(ac.calls) < 2 else 70_000

    class _ReinvokeLambda:
        def __init__(self):
            self.calls = []
        def invoke(self, **kw):
            self.calls.append(kw)
            return {"StatusCode": 202}

    c = clients(ac)
    c["lambda"] = _ReinvokeLambda()
    out = driver.handler(driver_event(task_token=None), clients=c, context=_TightCtx())
    assert out == {"status": "self_reinvoked_between_turns"}, (
        f"{out} -- the outage branch slept into the Lambda wall instead of handing off")
    assert not slept, f"slept {slept} with only 70s of wall left"


# ── the canonical trainer and the end of codegen roulette ───────────────────────────
# The finetune agent authored train_qlora.py from scratch on every run because the
# canonical script was IAM-unreadable (its own generated docstring said so on
# run-20260811T165529Z-ce628817). Same agent, same prompt: r5 and r6c got working
# trainers, r6a got an UnboundLocalError and died at 39s. The cure is the skills
# argument again -- a canonical artifact the role can READ and may not WRITE -- and
# these guards pin its three legs: the mirror, the grant, and the prompt that names it.

def test_the_canonical_trainer_mirror_verifies_what_it_uploads(storage_mod, tmp_path):
    class _S3:
        def __init__(self):
            self.objects = {}
        def upload_file(self, path, bucket, key):
            self.objects[key] = open(path, "rb").read()
        def get_object(self, Bucket, Key):
            class _B:
                def __init__(self, b): self._b = b
                def read(self): return self._b
            return {"Body": _B(self.objects[Key])}
    s3 = _S3()
    out = storage_mod.ensure_code(s3, "bkt", dry=False)
    assert "code/distill/train_qlora.py" in s3.objects
    assert "code/distill/requirements.txt" in s3.objects
    # The preflight ships in the same sourcedir as the trainer it gates. It used to live
    # in pipeline/training/, which ensure_code does not read, so the one script written to
    # stop a job from burning GPU hours and delivering nothing had no deploy path at all --
    # no prompt could name it because no bucket ever had it. A file nothing uploads is not
    # a component, however good its arithmetic.
    assert "code/distill/validate_job_config.py" in s3.objects
    assert set(out["uploaded_and_verified"]) == {"train_qlora.py", "requirements.txt",
                                                 "validate_job_config.py"}


def test_the_canonical_trainer_grant_is_read_only():
    doc = json.loads((REPO / "deploy/iam/harness_execution_role.json").read_text())
    stmts = {s.get("Sid"): s for s in doc["permissionsPolicy"]["Statement"]}
    s = stmts.get("S3CanonicalCodeReadOnly")
    assert s, "the code/* read grant is gone -- every launch re-enters codegen roulette"
    acts = s["Action"] if isinstance(s["Action"], list) else [s["Action"]]
    assert acts == ["s3:GetObject"], (
        f"{acts}: an agent that can WRITE the script it trains with can rewrite the "
        "thing its gate judges")


def test_the_launch_bullet_names_the_canonical_trainer_and_the_declared_fallback():
    doc = json.loads((REPO / "agents/finetune/harness.json").read_text())
    text = "".join(b.get("text", "") for b in doc["systemPrompt"])
    m = re.search(r'- "launch":(.*?)(?=\n- "[a-z_]+"|\nRules:)', text, re.S)
    assert m, "finetune has no launch bullet"
    b = m.group(1)
    assert "code/distill/train_qlora.py" in b, (
        "the launch bullet no longer names the canonical trainer -- the agent is back "
        "to authoring one per run")
    assert "FALLBACK" in b and "stage_complete" in b, (
        "improvising a trainer must remain a DECLARED fallback, not a silent choice")


#: Filled in by SageMaker's own environment (SM_CHANNEL_*, SM_MODEL_DIR, SM_OUTPUT_DATA_DIR),
#: so the launch bullet has no business naming them as hyperparameters even though the script
#: declares them. Every OTHER declared knob must appear in the contract the prompt states.
_SM_PROVIDED = {"--train_dir", "--val_dir", "--model_dir", "--output_data_dir"}


def test_the_prompts_hyperparameter_contract_matches_the_scripts_argparse():
    """The launch bullet lists the knobs; the script defines them. Two files, two
    claims -- this derives both sides so a flag added to one cannot silently miss
    the other (the SDK json.dumps lesson made hyperparameter plumbing load-bearing).

    BOTH directions, and the one that was missing is the one that cost something: the
    contract listed 12 knobs while the deliverability trio (--save_steps,
    --max_train_seconds, --checkpoint_dir) sat undeclared in the prompt, so no launch ever
    passed them and every real job ran with no checkpoint config and no time budget --
    4/4 measured on live jobs. A knob a script accepts and a prompt never mentions is a
    knob nothing sets, which is indistinguishable from a knob that does not exist.

    Flags are read from the parenthesised contract, not the whole bullet, because the
    bullet also quotes the preflight's OWN command line (--sec-per-it, --rows). Those are
    not silently exempted: every flag anywhere in the bullet has to be defined by one of
    the two scripts the bullet tells the agent to download, and both sides of that are
    derived from the scripts themselves.
    """
    mirrored = REPO / "pipeline/training/distill"
    script = (mirrored / "train_qlora.py").read_text()
    defined = set(re.findall(r'add_argument\("(--[a-z_]+)"', script))
    doc = json.loads((REPO / "agents/finetune/harness.json").read_text())
    text = "".join(b.get("text", "") for b in doc["systemPrompt"])
    b = re.search(r'- "launch":(.*?)(?=\n- "[a-z_]+"|\nRules:)', text, re.S).group(1)

    contract = re.search(r"argparse contract:(.*?)\)", b, re.S)
    assert contract, "the launch bullet no longer states an argparse contract at all"
    named = set(re.findall(r"(--[a-z_]+)", contract.group(1)))

    assert not named - defined, (
        f"the launch bullet names {sorted(named - defined)} but the canonical script's "
        "argparse does not define them -- the launch will pass hyperparameters the "
        "trainer crashes on before a single step runs")
    assert not defined - named - _SM_PROVIDED, (
        f"the canonical script accepts {sorted(defined - named - _SM_PROVIDED)} and the "
        "launch bullet never names them, so no run will ever set them; if a knob is "
        "deliberately left at its default, say so in the bullet rather than omitting it")

    # Every other flag the bullet mentions must belong to a script the agent downloads.
    # Hyphens are allowed here (the preflight has --sec-per-it) and underscores are not,
    # so a contract flag re-matches truncated (--checkpoint_dir -> --checkpoint); anything
    # that is a prefix of a contract flag was already checked above.
    other = {f for f in re.findall(r"(--[a-z-]+)", b)
             if not any(n.startswith(f) for n in named)}
    preflight = set(re.findall(r'add_argument\("(--[a-z-]+)"',
                               (mirrored / "validate_job_config.py").read_text()))
    for flag in sorted(other):
        assert any(flag.startswith(p) for p in defined | preflight), (
            f"the launch bullet names {flag}, which neither the canonical trainer nor the "
            "preflight it tells the agent to run declares")


# ── the alarms: who notices, and how long the silence may last ──────────────────────
# Measured before any of this was written: between 2026-07-29 and 2026-08-12 Lambda
# DROPPED 19 async invocations (llmops-harness-driver 11, llmops-resume-pipeline 8) and
# there was not one CloudWatch alarm on any function in this system. Every failure was
# found by a human reading logs -- the 2026-08-08 drop nine hours later, the 2026-08-11
# PutEvents AccessDenied a day later. These guards pin the three things the alarm deploy
# must not get wrong: coverage, the silence rule, and where the message goes.

def _obs_mod():
    return _load("deploy_observability", "deploy/06_observability.py")


def _alarm_plans(topic="arn:aws:sns:us-east-1:TESTACCTID00:llmops-escalations"):
    """The exact kwargs the deploy would hand CloudWatch (dry=False, fake client)."""
    class _CW:
        def __init__(self):
            self.puts = []

        def put_metric_alarm(self, **kw):
            self.puts.append(kw)

    cw = _CW()
    _obs_mod().alarms(cw, topic, dry=False)
    return cw.puts


def _default_enabled_schedules():
    """The schedules 08_triggers.py creates ENABLED when run with no flags.

    Derived from the call sites, not copied from them: a schedule passed
    `not args.no_*` (a store_true defaulting False) ships enabled, while one passed
    `args.enable_schedule` ships DISABLED and has to be opted into. Flipping either
    default in the deploy script therefore reds this file instead of silently
    changing what the alarms mean.
    """
    tree = ast.parse(_deploy_src_orch("08_triggers.py"))
    consts = {n.targets[0].id: n.value.value for n in ast.walk(tree)
              if isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant)
              and isinstance(n.targets[0], ast.Name)
              and n.targets[0].id.endswith("SCHEDULE_NAME")}
    named = {}
    for fn in [n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name.startswith("ensure_")
               and "schedule" in n.name]:
        used = [consts[x.id] for x in ast.walk(fn)
                if isinstance(x, ast.Name) and x.id in consts]
        if used:
            named[fn.name] = used[0]
    assert len(named) == 4, f"expected 4 schedule creators in 08_triggers.py, got {named}"
    enabled = set()
    for call in [n for n in ast.walk(tree) if isinstance(n, ast.Call)]:
        fname = getattr(call.func, "id", "")
        if fname in named and len(call.args) >= 4:
            if ast.unparse(call.args[3]).startswith("not args."):
                enabled.add(named[fname])
    return enabled


#: Every deploy script that creates a Lambda, and how to read the names out of it. Two
#: entries because the system has two deployers, which is the finding: the alarm census
#: was derived from 07_lambdas.py alone and the console's function -- created by a shell
#: script, 17,007 invocations in three days -- was the eighth Lambda nobody watched. The
#: patterns deliberately differ from the deployer's own parse routes (ast for LAMBDAS, a
#: shell variable for the console) so a parser that quietly returns a short list is
#: caught by a second reader rather than agreed with.
_LAMBDA_DEPLOYERS = {
    "07_lambdas.py": r'"fn":\s*"([^"]+)"',
    "console/deploy.sh": r"^FN=([A-Za-z0-9_.-]+)\s*$",
}


def _all_deployed_lambda_names():
    found = {}
    for path, pattern in _LAMBDA_DEPLOYERS.items():
        names = set(re.findall(pattern, _deploy_src_orch(path), re.M))
        assert names, f"{path}: /{pattern}/ matched no function name -- the regex broke"
        found[path] = names
    return found


def test_every_lambda_any_deploy_script_creates_has_an_errors_alarm():
    """Coverage is DERIVED from every deployer, not from the biggest one.

    A hand-kept alarm list is how the eighth Lambda becomes the one nobody watches: the
    function ships, the list is not touched, and nothing anywhere says a name is
    missing. Deriving the list fixed the hand-keeping and left the eighth Lambda
    unwatched anyway, because the derivation read ONE deploy script: measured
    2026-08-12, the account ran 8 llmops functions against 7 alarms, and the missing one
    was llmops-admin -- the console every plan signature and human verdict goes through,
    17,007 invocations in three days, no alarm of any family. "What 07_lambdas.py
    deploys" was never a wrong answer; it was an answer to a narrower question than the
    one the docstring claimed.
    """
    per_file = _all_deployed_lambda_names()
    assert len(per_file["07_lambdas.py"]) >= 7, per_file["07_lambdas.py"]
    fns = set().union(*per_file.values())
    assert len(fns) >= 8, f"expected at least 8 deployed functions, found {sorted(fns)}"
    alarmed = {p["Dimensions"][0]["Value"] for p in _alarm_plans()
               if p["MetricName"] == "Errors"}
    assert alarmed == fns, (
        f"Lambdas with no errors alarm: {sorted(fns - alarmed)}; alarms for functions "
        f"the deploy does not create: {sorted(alarmed - fns)}")


def test_no_third_deploy_script_creates_a_lambda_the_census_cannot_see():
    """The guard against the NINTH Lambda, which is the same bug one deployer later.

    _LAMBDA_DEPLOYERS is two entries of knowledge about where Lambdas come from, and
    knowledge like that goes stale silently -- a new deployer means a new function with
    no alarm, and nothing in the census can notice a file it was never told to read. So
    the set of files that create Lambdas is derived from the deploy tree itself: any file
    calling create-function / create_function must be a file the census knows how to
    read. Adding a deployer therefore reds here instead of shipping an unwatched
    function, which is exactly what happened the first time.
    """
    creators = set()
    for path in sorted((REPO / "deploy").rglob("*")):
        if not path.is_file() or path.suffix not in (".py", ".sh"):
            continue
        if "evidence" in path.parts:      # write-ups quote commands, they run nothing
            continue
        text = path.read_text(errors="ignore")
        if "create-function" in text or "create_function" in text:
            creators.add(str(path.relative_to(REPO / "deploy")))
    assert creators == set(_LAMBDA_DEPLOYERS), (
        f"deploy scripts that create Lambdas: {sorted(creators)}; the alarm census "
        f"knows: {sorted(_LAMBDA_DEPLOYERS)}. Teach _LAMBDA_DEPLOYERS and "
        "06_observability.py how to read the new one, or its function ships unwatched")


def test_the_console_lambda_gets_the_errors_family_only():
    """It is neither scheduled nor async-invoked, and both of the other families lie.

    A `-silent` alarm on the console would sit in ALARM on any night nobody signs a
    plan, and an `-async-dropped` alarm would sit in INSUFFICIENT_DATA forever because
    API Gateway invokes it synchronously -- there is no async queue to drop from. Both
    failure modes teach an operator to ignore the set, which is what the silence family's
    own comment says about llmops-start-pipeline.
    """
    obs = _obs_mod()
    console = obs.console_function()
    assert console == "llmops-admin", console
    families = {p["AlarmName"].removeprefix(console + "-")
                for p in _alarm_plans()
                if p["Dimensions"][0]["Value"] == console}
    assert families == {"errors"}, (
        f"the console Lambda has alarm families {sorted(families)}; only `errors` can "
        "ever report on a synchronously-invoked, unscheduled function")


#: How each ARCHITECTURE variant states the alarm count, and the words to read it with.
#: Both write it as a WORD, so the check is a lookup rather than a `\d+` search -- and the
#: sentence is pinned by the prose around it because that paragraph also names 19 drops,
#: three families and 17,007 invocations: any number found loose in it proves nothing.
_ALARM_COUNT_CLAIMS = {
    "docs/ARCHITECTURE.md": (r"--alarms` now creates (\w+), in three families", "en"),
    "docs/ARCHITECTURE.zh-TW.md": (r"--alarms` 現在建立([一二三四五六七八九十]+)個 alarm",
                                   "zh"),
}
_ALARM_COUNT_WORDS = {
    11: {"en": "eleven", "zh": "十一"}, 12: {"en": "twelve", "zh": "十二"},
    13: {"en": "thirteen", "zh": "十三"}, 14: {"en": "fourteen", "zh": "十四"},
    15: {"en": "fifteen", "zh": "十五"}, 16: {"en": "sixteen", "zh": "十六"},
}


def test_the_documented_alarm_count_matches_the_alarms_the_deploy_creates():
    """The count in the prose is the one number in this section nothing derived.

    It was written as twelve when the deploy created twelve, and the eighth Lambda's
    errors alarm made it thirteen -- a sentence that was measured once reads as measured
    forever, and this paragraph is where a reader goes to learn what is watched. Both
    languages, in the same commit: a count fixed in one is worse than one stale in both,
    because it reads as verified in whichever the reader happens to open.
    """
    n = len(_alarm_plans())
    assert n in _ALARM_COUNT_WORDS, f"{n} alarms: extend _ALARM_COUNT_WORDS in this guard"
    for name, (pattern, lang) in _ALARM_COUNT_CLAIMS.items():
        m = re.search(pattern, (REPO / name).read_text())
        assert m, f"{name}: no alarm-count sentence for /{pattern}/ to read"
        assert m.group(1) == _ALARM_COUNT_WORDS[n][lang], (
            f"{name} says the deploy creates {m.group(1)!r} alarms; alarms() plans {n} "
            f"({_ALARM_COUNT_WORDS[n][lang]})")


class _FakeLogs:
    """A CloudWatch Logs double.

    `describe_log_groups` pages DELIBERATELY, two pages even for two groups: the real API
    paginates, and a caller that reads only the first page is the defect this system has now
    made twice (list_functions reported 3 of 8 Lambdas, list_agent_runtimes 10 of 19 --
    and the nine it dropped included the largest log producer in the account).
    `put_retention_policy` refuses a group that was never created, because that is what the
    real API does and it is the reason `retention()` creates first.
    """

    class exceptions:
        class ResourceAlreadyExistsException(Exception):
            pass

    def __init__(self, groups=(), existing=None):
        self._groups = list(groups)
        self.existing = set(groups if existing is None else existing)
        self.created, self.put = [], []

    def get_paginator(self, op):
        assert op == "describe_log_groups", f"unexpected paginator {op!r}"
        outer = self

        class _P:
            def paginate(self, logGroupNamePrefix):
                names = [g for g in outer._groups if g.startswith(logGroupNamePrefix)]
                mid = (len(names) + 1) // 2
                for chunk in (names[:mid], names[mid:]):
                    yield {"logGroups": [{"logGroupName": n} for n in chunk]}
        return _P()

    def create_log_group(self, logGroupName):
        if logGroupName in self.existing:
            raise self.exceptions.ResourceAlreadyExistsException(logGroupName)
        self.existing.add(logGroupName)
        self.created.append(logGroupName)

    def put_retention_policy(self, logGroupName, retentionInDays):
        if logGroupName not in self.existing:
            raise AssertionError(f"retention set on a group that does not exist: {logGroupName}")
        self.put.append((logGroupName, retentionInDays))


#: The real names, as this account actually has them (live 2026-08-12). The harness id
#: suffixes are the point: nothing in the repo knows `KuSKXUaxyP` or `D8SPwm7Kog`.
_LIVE_AGENTCORE_GROUPS = [
    "/aws/bedrock-agentcore/llmops_data_prep-KuSKXUaxyP",
    "/aws/bedrock-agentcore/runtimes/harness_llmops_data_prep-D8SPwm7Kog-DEFAULT",
    "/aws/bedrock-agentcore/runtimes/harness_llmops_orchestrator-2sx6hzCapx-DEFAULT",
    "/aws/bedrock-agentcore/evaluations/results/llmops_eval_iuIIs96fFM_online_eval-v5qT6I9Puq",
    # another project in the same account -- shares the prefix, is not ours
    "/aws/bedrock-agentcore/runtimes/katalon_warm-V1jqxYFNFt-DEFAULT",
    "/aws/bedrock-agentcore/uitestagent",
]


def test_retention_covers_every_lambda_any_deploy_script_creates():
    """Lambda creates its own log group on first invoke, with NO retention, forever.

    Nothing in this repo had ever set one: measured 2026-08-12, all eight llmops Lambda log
    groups read `NEVER`, including `llmops-admin` (11 MB in seven days). The census is the
    same one the alarms use, so a Lambda cannot be watched-but-unbounded or vice versa.
    """
    obs = _obs_mod()
    logs = _FakeLogs(_LIVE_AGENTCORE_GROUPS)
    targets = obs.retention_targets(logs)
    for fn in obs.deployed_functions():
        assert f"/aws/lambda/{fn}" in targets, f"{fn} has no log retention target"
    assert len(obs.deployed_functions()) >= 8, obs.deployed_functions()


def test_retention_targets_the_group_with_the_traffic_not_a_name_built_from_a_constant():
    """The first draft of this built the AgentCore names from HARNESSES. It was wrong live.

    The groups that hold the volume are named after ids the repo never sees --
    `llmops_data_prep-KuSKXUaxyP`, `runtimes/harness_llmops_data_prep-D8SPwm7Kog-DEFAULT` --
    so `/aws/bedrock-agentcore/llmops_data_prep` names NOTHING that exists, and putting a
    policy on it would have created five empty groups beside the ones with the traffic while
    the deploy reported success. Measured over the seven days to 2026-08-12: 1236 MB
    ingested, 1225 MB of it into runtime DEFAULT groups with no retention, and 0 bytes into
    the delivery groups that already carry a 30-day policy. So this list is DISCOVERED.
    """
    obs = _obs_mod()
    targets = obs.retention_targets(_FakeLogs(_LIVE_AGENTCORE_GROUPS))
    for real in _LIVE_AGENTCORE_GROUPS:
        if obs.OURS in real:
            assert real in targets, f"{real} holds this system's logs and is not covered"
    for h in obs.HARNESSES:
        assert f"/aws/bedrock-agentcore/{h}" not in targets, (
            f"/aws/bedrock-agentcore/{h} is a name built from HARNESSES, not a group that "
            "exists; a policy on it creates an empty group and reports success")


def test_retention_leaves_another_projects_log_groups_alone():
    """This account is shared. `katalon_*` and `uitestagent` are not this system's data.

    Retention is a data-lifetime decision about someone else's logs, so the discriminator is
    the same one the whole repo uses for cross-system contamination: the name says llmops or
    it is not ours.
    """
    obs = _obs_mod()
    targets = obs.retention_targets(_FakeLogs(_LIVE_AGENTCORE_GROUPS))
    foreign = [g for g in targets if g.startswith(obs.AGENTCORE_LOGS) and obs.OURS not in g]
    assert not foreign, f"would set retention on log groups that are not ours: {foreign}"


def test_retention_creates_a_missing_group_before_setting_its_policy():
    """A policy cannot be put on a group that does not exist yet, and Lambda's does not.

    Without the create, a freshly deployed function keeps its logs forever until someone
    re-runs this AFTER the first invocation — an ordering nobody remembers, and one whose
    failure is invisible (the deploy would report the group it skipped as fine). The
    already-exists path is exercised too: it must be swallowed, not raised.
    """
    obs = _obs_mod()
    logs = _FakeLogs(_LIVE_AGENTCORE_GROUPS)
    out = obs.retention(logs, dry=False)
    assert [r["group"] for r in out] == obs.retention_targets(logs), "plan != what it did"
    assert all(d == obs.RETENTION_DAYS for _, d in logs.put), logs.put
    lam = {f"/aws/lambda/{fn}" for fn in obs.deployed_functions()}
    assert lam <= set(logs.created), f"never created: {sorted(lam - set(logs.created))}"
    assert not (set(logs.created) & set(_LIVE_AGENTCORE_GROUPS)), (
        "re-created a group that already existed instead of swallowing the exception")
    assert {g for g, _ in logs.put} == set(logs.existing) - (
        {g for g in _LIVE_AGENTCORE_GROUPS if obs.OURS not in g})


def test_the_runtime_listing_follows_every_page():
    """`list_agent_runtimes()` returned 10 of 19 with a nextToken nobody followed.

    Its caller in `online_eval` reports `no runtime <name>` and skips creating that
    harness's evaluation config, so a runtime that exists reads as one that does not, purely
    by page position — and the nine dropped on 2026-08-12 included
    `harness_llmops_data_prep`. Same defect as the unpaginated `list_functions()` that
    reported 3 of 8 Lambdas.
    """
    obs = _obs_mod()
    pages = [[{"agentRuntimeName": f"harness_llmops_{i}", "agentRuntimeId": f"id{i}"}]
             for i in range(3)]

    class _Ctl:
        def __init__(self):
            self.tokens = []

        def list_agent_runtimes(self, **kw):
            self.tokens.append(kw.get("nextToken"))
            i = int(kw["nextToken"]) if kw.get("nextToken") else 0
            resp = {"agentRuntimes": pages[i]}
            if i + 1 < len(pages):
                resp["nextToken"] = str(i + 1)
            return resp

    ctl = _Ctl()
    got = obs.list_runtimes(ctl)
    assert [r["agentRuntimeId"] for r in got] == ["id0", "id1", "id2"], got
    assert ctl.tokens == [None, "1", "2"], ctl.tokens


def test_the_retention_period_is_the_one_the_deliveries_already_use():
    """One convention, derived — not a second number that agrees today and drifts later.

    `setup_observability.py` has defaulted to 30 days since the deliveries were first wired;
    --retention exists to make that same policy reach the groups nobody was applying it to,
    so the two must not be able to disagree.
    """
    obs = _obs_mod()
    m = re.search(r'--retention-days",\s*type=int,\s*default=(\d+)',
                  _deploy_src_orch("setup_observability.py"))
    assert m, "setup_observability.py no longer states a --retention-days default"
    assert obs.RETENTION_DAYS == int(m.group(1)), (
        f"--retention would set {obs.RETENTION_DAYS} days while the deliveries set "
        f"{m.group(1)}; two numbers for one convention")


def test_the_silence_alarms_are_exactly_the_schedules_that_ship_enabled():
    """An alarm that is always ALARM trains the operator to ignore all of them.

    `llmops-nightly` ships DISABLED on purpose (--enable-schedule opts in), so
    llmops-start-pipeline is invoked zero times a day BY DESIGN. A silence alarm on it
    would go to ALARM at deploy and stay there, and the first thing anyone learns from
    a permanently red alarm is not to look at this account's alarms at all.
    """
    obs = _obs_mod()
    assert set(obs.SILENCE_ALARMS) == _default_enabled_schedules(), (
        f"silence alarms {sorted(obs.SILENCE_ALARMS)} vs schedules that actually ship "
        f"enabled {sorted(_default_enabled_schedules())}")
    silent = {p["Dimensions"][0]["Value"] for p in _alarm_plans()
              if p["AlarmName"].endswith("-silent")}
    assert "llmops-start-pipeline" not in silent, (
        "a silence alarm on the nightly's target would sit in ALARM forever")
    assert silent == {s["fn"] for s in obs.SILENCE_ALARMS.values()}


def test_a_silence_alarm_treats_missing_data_as_breaching():
    """The one setting this family cannot get wrong.

    A Lambda nobody invoked publishes NO `Invocations` datapoint -- not a zero. With
    the ordinary `notBreaching`, "no data" reads as "fine", the alarm never leaves
    INSUFFICIENT_DATA, and the exact condition it exists to detect (the schedule
    stopped firing) is the condition it cannot see. `breaching` is what makes absence
    the signal.
    """
    for p in _alarm_plans():
        want = "breaching" if p["AlarmName"].endswith("-silent") else "notBreaching"
        assert p["TreatMissingData"] == want, (
            f"{p['AlarmName']} treats missing data as {p['TreatMissingData']!r}, "
            f"expected {want!r}")
        if p["AlarmName"].endswith("-silent"):
            assert p["ComparisonOperator"] == "LessThanThreshold", (
                f"{p['AlarmName']} alarms on {p['ComparisonOperator']} -- a silence "
                "alarm fires when invocations fall BELOW one")


def test_every_alarm_notifies_the_escalation_topic_and_nothing_on_recovery():
    """An alarm with no action is a dashboard nobody opens.

    Same SNS topic as the pipeline's own ESCALATED_TO_HUMAN path, resolved from the
    parameter 03_storage.py writes -- one place an operator watches. AlarmActions only:
    OKActions on a five-minute error alarm turns one flapping invocation into two
    messages, and the recovery of a stage that already lost its work is not news.
    """
    obs = _obs_mod()
    storage = _deploy_src_orch("03_storage.py")
    prefix = re.search(r'Name=f"([^"{]*)\{k\}"', storage)
    assert prefix, "03_storage.py no longer writes its params under an f-string prefix"
    assert '"escalations_topic_arn"' in storage, (
        "03_storage.py does not publish an escalations_topic_arn parameter")
    assert obs.TOPIC_PARAM == prefix.group(1) + "escalations_topic_arn", (
        f"{obs.TOPIC_PARAM} is not the parameter 03_storage.py publishes "
        f"({prefix.group(1)}escalations_topic_arn) -- the alarms would notify nothing")
    topic = "arn:aws:sns:us-east-1:TESTACCTID00:llmops-escalations"
    plans = _alarm_plans(topic)
    assert plans, "the alarm deploy planned nothing at all"
    for p in plans:
        assert p["AlarmActions"] == [topic], f"{p['AlarmName']} notifies {p['AlarmActions']}"
        assert p["ActionsEnabled"] is True, f"{p['AlarmName']} has actions disabled"
        assert "OKActions" not in p, f"{p['AlarmName']} pages on recovery too"
        assert p["AlarmDescription"].strip(), f"{p['AlarmName']} has no description"


def test_the_async_dropped_alarms_name_functions_that_exist():
    """AsyncEventsDropped only exists for a function something invokes ASYNCHRONOUSLY.

    Both of these are async-delivered (the driver's own turn handoff and the
    resurrector's re-invoke; EventBridge's job-state delivery to resume), and both are
    where all 19 observed drops landed. A rename that leaves this list behind gives an
    alarm on a dimension that never reports -- INSUFFICIENT_DATA forever, which looks
    exactly like healthy.
    """
    obs = _obs_mod()
    fns = set(re.findall(r'"fn":\s*"([^"]+)"', _deploy_src_orch("07_lambdas.py")))
    assert set(obs.ASYNC_DELIVERED) <= fns, (
        f"{sorted(set(obs.ASYNC_DELIVERED) - fns)} is not a function 07_lambdas.py "
        "deploys")
    dropped = {p["Dimensions"][0]["Value"] for p in _alarm_plans()
               if p["MetricName"] == "AsyncEventsDropped"}
    assert dropped == set(obs.ASYNC_DELIVERED)
