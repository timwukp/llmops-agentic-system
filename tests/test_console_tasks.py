"""Unit tests for the Tasks tab — the goal-driven entry, no AWS, every client faked.

Scope: what the console will and will not let a person (or the orchestrator) do
around a consultation: who may create/chat/accept, how acceptance branches on the
$2000 gate, that approval records are signed and chained, and that launch_run is
serviced only after a signed human acceptance. The signing/verification arithmetic
itself also gets direct unit coverage via conductor_tools.

Run: .venv/bin/python -m pytest tests/test_console_tasks.py -q
"""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import pathlib
import re
import sys
import types

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "pipeline" / "contracts"))
sys.path.insert(0, str(REPO / "orchestration"))

ACCOUNT = "123456789012"

# Reuse the cost-tab test's import-time isolation wholesale: same module under
# test, same reason (the console builds ~14 clients at import).
from test_console_cost import _StubTable, _fake_boto3  # noqa: E402

import conductor_tools  # noqa: E402


def _reject_floats(obj, path="item"):
    """Real DynamoDB refuses Python floats ('Float types are not supported. Use
    Decimal types instead.') — caught live on the first accept click, which the
    original stub happily swallowed. Enforce the same rule offline."""
    if isinstance(obj, float):
        raise TypeError(f"Float types are not supported (at {path}). Use Decimal/str.")
    if isinstance(obj, dict):
        for k, v in obj.items():
            _reject_floats(v, f"{path}.{k}")
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            _reject_floats(v, f"{path}[{i}]")


class TaskTable(_StubTable):
    """The tasks code appends with list_append(if_not_exists(...)) and increments
    with `x = x + :one`; the base stub only applies plain SET pairs. Enough of the
    DynamoDB grammar to make a second read observe appends and bumps. Also enforces
    DynamoDB's no-float rule, which the base stub does not."""

    def put_item(self, Item):
        _reject_floats(Item)
        return super().put_item(Item)

    def update_item(self, **kw):
        _reject_floats(kw.get("ExpressionAttributeValues") or {}, "values")
        self.updates.append(kw)
        k = (kw.get("Key") or {}).get("id")
        it = self.items.get(k)
        if it is None:
            return {}
        vals = kw.get("ExpressionAttributeValues") or {}
        names = kw.get("ExpressionAttributeNames") or {}
        expr = kw.get("UpdateExpression", "").replace("SET ", "")
        for part in _split_top_level(expr):
            if "=" not in part:
                continue
            lhs, rhs = [p.strip() for p in part.split("=", 1)]
            lhs = names.get(lhs, lhs)
            if rhs.startswith("list_append"):
                ph = rhs.rsplit(",", 1)[1].rstrip(") ").strip()
                it.setdefault(lhs, [])
                it[lhs] = list(it[lhs]) + list(vals.get(ph, []))
            elif "+" in rhs:
                base_name, inc_ph = [p.strip() for p in rhs.split("+", 1)]
                it[lhs] = int(it.get(base_name, 0)) + int(vals.get(inc_ph, 1))
            elif rhs in vals:
                it[lhs] = vals[rhs]
        return {}


def _split_top_level(expr):
    """Split on commas that are not inside parentheses (list_append has one)."""
    parts, depth, cur = [], 0, ""
    for ch in expr:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    if cur:
        parts.append(cur)
    return parts


# ── a KMS fake that actually signs (deterministically) ───────────────────────
class FakeKms:
    """Sign = HMAC-free deterministic transform of the digest; Verify recomputes.
    Close enough to KMS semantics for the property that matters: a record whose
    contents changed after signing MUST fail verification."""

    def __init__(self):
        self.sign_calls = []

    def sign(self, **kw):
        self.sign_calls.append(kw)
        assert kw.get("MessageType") == "DIGEST", "must sign the digest, not the message"
        assert kw.get("SigningAlgorithm") == "ECDSA_SHA_256"
        sig = hashlib.sha256(b"fake-key|" + kw["Message"]).digest()
        return {"Signature": sig}

    def verify(self, **kw):
        want = hashlib.sha256(b"fake-key|" + kw["Message"]).digest()
        return {"SignatureValid": want == kw["Signature"]}

    def describe_key(self, **kw):
        return {"KeyMetadata": {"Arn": f"arn:aws:kms:us-east-1:{ACCOUNT}:key/fake"}}


class _Body:
    def __init__(self, s):
        self._s = s.encode() if isinstance(s, str) else s

    def read(self):
        return self._s


class FakeS3:
    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body, **kw):
        self.objects[f"{Bucket}/{Key}"] = Body if isinstance(Body, bytes) else Body

    def get_object(self, Bucket, Key):
        k = f"{Bucket}/{Key}"
        if k not in self.objects:
            raise KeyError(f"NoSuchKey: {k}")
        return {"Body": _Body(self.objects[k])}

    def head_object(self, Bucket, Key):
        if f"{Bucket}/{Key}" not in self.objects:
            raise KeyError("404")
        return {}


class FakeLambda:
    def __init__(self):
        self.invokes = []

    def invoke(self, **kw):
        self.invokes.append(kw)
        if kw.get("InvocationType") == "Event":
            return {"StatusCode": 202}
        return {"StatusCode": 200, "Payload": _Body(json.dumps(
            {"run_id": "run-disp-0001", "manifest_uri": "s3://b/runs/run-disp-0001/manifest.json",
             "execution_arn": "arn:aws:states:::exec"}))}


@pytest.fixture(scope="module")
def console():
    saved = {k: sys.modules.get(k) for k in
             ("boto3", "boto3.dynamodb", "boto3.dynamodb.conditions")}
    m, dyn, conds = _fake_boto3()
    sys.modules["boto3"] = m
    sys.modules["boto3.dynamodb"] = dyn
    sys.modules["boto3.dynamodb.conditions"] = conds
    try:
        spec = importlib.util.spec_from_file_location(
            "console_lambda_tasks", REPO / "deploy/console/lambda_function.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
    return mod



def _tool_results(messages):
    """Every toolResult block in a messages list, regardless of which message holds
    it. A resume is [assistant toolUse, user toolResult], so indexing messages[0]
    (as these fakes originally did) looks past the result entirely -- which is how
    the missing assistant echo survived offline while breaking every live dispatch."""
    return [blk["toolResult"] for m in messages
            for blk in (m.get("content") or []) if "toolResult" in blk]


def _last_text(messages):
    """The text of the final user message in a messages list."""
    for m in reversed(messages):
        for blk in reversed(m.get("content") or []):
            if "text" in blk:
                return blk["text"]
    return ""

DS_USER = {"username": "alice", "groups": ["llmops-datascience"], "sub": "sub-alice",
           "source_ip": "10.0.0.1"}
APPROVER = {"username": "boss", "groups": ["llmops-approver"], "sub": "sub-boss",
            "source_ip": "10.0.0.2"}
NOBODY = {"username": "eve", "groups": [], "sub": "sub-eve", "source_ip": "10.0.0.3"}


@pytest.fixture
def wired(console, monkeypatch):
    tasks = TaskTable("llmops-tasks")
    events = _StubTable("llmops-stage-events")
    est = _StubTable("llmops-cost-estimates")
    fake_s3, fake_lam, fake_kms = FakeS3(), FakeLambda(), FakeKms()
    monkeypatch.setattr(console, "tasks_tbl", tasks)
    monkeypatch.setattr(console, "events_tbl", events)
    monkeypatch.setattr(console, "estimates_tbl", est)
    monkeypatch.setattr(console, "s3", fake_s3)
    monkeypatch.setattr(console, "lam", fake_lam)
    monkeypatch.setattr(console, "kms", fake_kms)
    monkeypatch.setattr(console, "SELF_FUNCTION", "llmops-admin")
    monkeypatch.setattr(console, "data_bucket", lambda: "test-bucket")
    monkeypatch.setattr(console, "_transcript_append", lambda *a, **k: None)

    import cost_model
    monkeypatch.setattr(console, "_cost_model", lambda: cost_model)
    monkeypatch.setattr(console, "project_to_date_usd", lambda *a, **k: (0.0, "none"))
    return types.SimpleNamespace(console=console, tasks=tasks, events=events,
                                 est=est, s3=fake_s3, lam=fake_lam, kms=fake_kms)


def _mk_task(w, status="plan_proposed", cost="50", plan_body=b'{"goal":"x"}'):
    tid = "task-abc123"
    uri = f"s3://test-bucket/tasks/{tid}/plan.json"
    w.s3.objects[f"test-bucket/tasks/{tid}/plan.json"] = plan_body
    w.tasks.items[tid] = {
        "id": tid, "status": status, "created_by": "alice", "goal": "goal",
        "messages": [{"role": "user", "text": "goal", "at": "t0", "by": "alice"}],
        "plan_uri": uri, "plan_summary": "the plan", "cost_estimate_usd": cost,
        "session_seq": 0, "created_at": "t0", "updated_at": "2020-01-01T00:00:00+00:00"}
    return tid


# ── creation & chat authz ─────────────────────────────────────────────────────

def test_create_task_requires_a_console_group(wired):
    r = wired.console.create_task({"goal": "make me a model"}, NOBODY)
    assert r["status_code"] == 403


def test_create_task_enqueues_the_first_turn(wired):
    r = wired.console.create_task({"goal": "make me a model"}, DS_USER)
    assert r["ok"] and r["task"]["status"] == "thinking"
    payload = json.loads(wired.lam.invokes[-1]["Payload"])
    assert payload == {"mode": "task-chat", "task_id": r["task"]["id"], "accept": False}


def test_message_while_thinking_is_409(wired):
    tid = _mk_task(wired, status="thinking")
    wired.tasks.items[tid]["updated_at"] = wired.console._now_iso()  # fresh turn
    r = wired.console.post_task_message(tid, {"text": "hi"}, DS_USER)
    assert r["status_code"] == 409


def test_zombie_thinking_can_be_reenqueued(wired):
    tid = _mk_task(wired, status="thinking")  # updated_at is 2020 — stale
    r = wired.console.post_task_message(tid, {"text": "hi"}, DS_USER)
    assert r.get("ok")


# ── acceptance: gate branching, signatures, no replay ─────────────────────────

def test_accept_under_limit_dispatches_and_writes_shadow_estimate(wired):
    tid = _mk_task(wired, cost="50")
    r = wired.console.accept_task(tid, DS_USER)
    assert r["ok"] and r["status"] == "accepting"
    # a PLAN ACCEPTED system message reached the record
    msgs = wired.tasks.items[tid]["messages"]
    assert any(m["role"] == "system" and m["text"].startswith("PLAN ACCEPTED by alice")
               for m in msgs)
    # shadow estimate: finops variance covers under-limit conductor runs too
    shadows = [p for p in wired.est.puts if p.get("status") == "launched"]
    assert shadows and shadows[0]["task_id"] == tid
    # the accept turn was enqueued with the accept flag
    payload = json.loads(wired.lam.invokes[-1]["Payload"])
    assert payload["accept"] is True


def test_accept_over_limit_goes_to_the_cost_queue(wired):
    tid = _mk_task(wired, cost="3000")
    r = wired.console.accept_task(tid, DS_USER)
    assert r["ok"] and r["status"] == "pending_approval"
    pend = [p for p in wired.est.puts if p.get("status") == "pending_approval"]
    assert pend and pend[0]["task_id"] == tid
    # nothing dispatched yet
    assert not any(json.loads(i["Payload"]).get("accept")
                   for i in wired.lam.invokes if i.get("InvocationType") == "Event")


def test_accept_requires_a_console_group_even_over_limit(wired):
    """Over-limit acceptance is 'submit to the queue' — but a user in NO group must
    not be able to enqueue spend requests at all."""
    tid = _mk_task(wired, cost="3000")
    r = wired.console.accept_task(tid, NOBODY)
    assert r["status_code"] == 403
    assert not [p for p in wired.est.puts if p.get("task_id") == tid]


def test_over_limit_self_approval_is_blocked_at_the_cost_queue(wired):
    """The REAL self-approval control lives in decide_approval via
    cost_model.check_approval (requester != approver) — exactly the same rule the
    form-based estimates enforce. Alice created and submitted; Alice may not decide."""
    tid = _mk_task(wired, cost="3000")
    wired.console.accept_task(tid, DS_USER)
    eid = [p for p in wired.est.puts if p.get("task_id") == tid][-1]["id"]
    wired.est.items[eid] = [p for p in wired.est.puts if p.get("id") == eid][-1]
    alice_as_approver = {"username": "alice", "groups": ["llmops-approver"],
                         "sub": "sub-alice", "source_ip": "ip"}
    r = wired.console.decide_approval({"estimate_id": eid, "decision": "approve"},
                                      alice_as_approver, "2026-08-01T00:00:00+00:00")
    assert r.get("status_code") in (403, 409), "self-approval must be denied"
    assert wired.tasks.items[tid]["status"] == "pending_approval"


def test_accept_is_not_replayable(wired):
    tid = _mk_task(wired, status="dispatched")
    r = wired.console.accept_task(tid, DS_USER)
    assert r["status_code"] == 409


def test_accept_without_a_priced_plan_is_409(wired):
    tid = _mk_task(wired)
    wired.tasks.items[tid]["cost_estimate_usd"] = ""
    r = wired.console.accept_task(tid, DS_USER)
    assert r["status_code"] == 409


def test_approval_record_is_signed_and_bound_to_the_plan_bytes(wired):
    tid = _mk_task(wired, cost="50", plan_body=b'{"exact":"bytes"}')
    wired.console.accept_task(tid, DS_USER)
    rec = wired.tasks.items[tid]["approvals"][-1]
    assert rec["plan_sha256"] == hashlib.sha256(b'{"exact":"bytes"}').hexdigest()
    assert rec["approved_by"] == "alice" and rec["cognito_sub"] == "sub-alice"
    assert rec["signature"]["algorithm"] == "ECDSA_SHA_256"
    # and it verifies — through the same code the dispatch path uses
    assert conductor_tools.verify_record(wired.kms, rec)


def test_tampered_approval_record_fails_verification(wired):
    tid = _mk_task(wired, cost="50")
    wired.console.accept_task(tid, DS_USER)
    rec = dict(wired.tasks.items[tid]["approvals"][-1])
    rec["cost_estimate_usd"] = "999999"  # the tamper
    assert not conductor_tools.verify_record(wired.kms, rec)


def test_hash_chain_links_successive_records(wired, monkeypatch):
    tid = _mk_task(wired, cost="3000")
    wired.console.accept_task(tid, DS_USER)          # record 1: submitted
    first = wired.tasks.items[tid]["approvals"][-1]
    # approver decides on the Cost tab
    eid = [p for p in wired.est.puts if p.get("task_id") == tid][-1]["id"]
    wired.est.items[eid] = [p for p in wired.est.puts if p.get("id") == eid][-1]
    import cost_model
    monkeypatch.setattr(cost_model, "check_approval",
                        lambda *a, **k: {"allowed": True})
    r = wired.console.decide_approval({"estimate_id": eid, "decision": "approve"},
                                      APPROVER, "2026-07-31T00:00:00+00:00")
    assert r["ok"]
    second = wired.tasks.items[tid]["approvals"][-1]
    assert second["prev_event_sha256"] == first["record_sha256"]
    assert second["approved_by"] == "boss"


# ── the Cost-tab decision routes back through the orchestrator ────────────────

def test_budget_approval_enqueues_the_accept_turn_not_start_run(wired, monkeypatch):
    tid = _mk_task(wired, cost="3000")
    wired.console.accept_task(tid, DS_USER)
    eid = [p for p in wired.est.puts if p.get("task_id") == tid][-1]["id"]
    wired.est.items[eid] = [p for p in wired.est.puts if p.get("id") == eid][-1]
    import cost_model
    monkeypatch.setattr(cost_model, "check_approval", lambda *a, **k: {"allowed": True})
    start_run_calls = []
    monkeypatch.setattr(wired.console, "start_run",
                        lambda body: start_run_calls.append(body) or {"ok": True})
    r = wired.console.decide_approval({"estimate_id": eid, "decision": "approve"},
                                      APPROVER, "2026-07-31T00:00:00+00:00")
    assert r["ok"] and r["task"]["status"] == "accepting"
    assert not start_run_calls, "conductor plans dispatch via launch_run, never start_run"
    payload = json.loads(wired.lam.invokes[-1]["Payload"])
    assert payload == {"mode": "task-chat", "task_id": tid, "accept": True}


def test_budget_rejection_feeds_the_reason_back_to_the_orchestrator(wired, monkeypatch):
    tid = _mk_task(wired, cost="3000")
    wired.console.accept_task(tid, DS_USER)
    eid = [p for p in wired.est.puts if p.get("task_id") == tid][-1]["id"]
    wired.est.items[eid] = [p for p in wired.est.puts if p.get("id") == eid][-1]
    import cost_model
    monkeypatch.setattr(cost_model, "check_approval", lambda *a, **k: {"allowed": True})
    r = wired.console.decide_approval(
        {"estimate_id": eid, "decision": "reject", "reason": "too expensive, halve it"},
        APPROVER, "2026-07-31T00:00:00+00:00")
    assert r["ok"] and r["task"]["status"] == "thinking"
    msgs = wired.tasks.items[tid]["messages"]
    assert any("REJECTED" in m["text"] and "halve it" in m["text"] for m in msgs)


# ── launch_run servicing (conductor_tools, direct) ────────────────────────────

def _signed_approval(kms_, plan_bytes, cost="50"):
    rec = {"task_id": "task-abc123", "plan_uri": "s3://test-bucket/tasks/task-abc123/plan.json",
           "plan_sha256": hashlib.sha256(plan_bytes).hexdigest(),
           "cost_estimate_usd": cost, "gate": {"approval_required": False},
           "decision": "accepted", "approved_by": "alice", "cognito_sub": "s",
           "source_ip": "ip", "approved_at": "t", "prev_event_sha256": "genesis"}
    return conductor_tools.sign_record(kms_, rec)


def test_launch_run_dispatches_with_conductor_payload():
    s3f, lamf, kmsf = FakeS3(), FakeLambda(), FakeKms()
    plan = b'{"data":{"source_uri":"s3://b/customer-data/x"},"models":{}}'
    s3f.objects["test-bucket/tasks/task-abc123/plan.json"] = plan
    appr = _signed_approval(kmsf, plan)
    r = conductor_tools.service_launch_run(
        lamf, s3f, kmsf,
        {"plan_uri": "s3://test-bucket/tasks/task-abc123/plan.json",
         "approval": appr, "params": {"sample_count": 100}},
        "llmops-start-pipeline")
    assert r["ok"] and r["run_id"] == "run-disp-0001"
    sent = json.loads(lamf.invokes[0]["Payload"])
    assert sent["trigger_source"] == "conductor"
    assert sent["plan"] == json.loads(plan)
    assert sent["approval"]["approved_by"] == "alice"
    assert sent["params"] == {"sample_count": 100}


def test_launch_run_without_an_approval_is_rejected():
    s3f, lamf, kmsf = FakeS3(), FakeLambda(), FakeKms()
    s3f.objects["test-bucket/tasks/task-abc123/plan.json"] = b"{}"
    r = conductor_tools.service_launch_run(
        lamf, s3f, kmsf,
        {"plan_uri": "s3://test-bucket/tasks/task-abc123/plan.json"},
        "llmops-start-pipeline")
    assert not r["ok"] and "approval" in r["reason"]
    assert not lamf.invokes


def test_launch_run_with_a_forged_approval_is_rejected():
    s3f, lamf, kmsf = FakeS3(), FakeLambda(), FakeKms()
    plan = b"{}"
    s3f.objects["test-bucket/tasks/task-abc123/plan.json"] = plan
    appr = _signed_approval(kmsf, plan)
    appr["cost_estimate_usd"] = "1"  # forged after signing
    r = conductor_tools.service_launch_run(
        lamf, s3f, kmsf,
        {"plan_uri": "s3://test-bucket/tasks/task-abc123/plan.json", "approval": appr},
        "llmops-start-pipeline")
    assert not r["ok"] and "signature" in r["reason"]


def test_launch_run_rejects_a_plan_that_changed_after_signing():
    s3f, lamf, kmsf = FakeS3(), FakeLambda(), FakeKms()
    appr = _signed_approval(kmsf, b'{"v":1}')
    s3f.objects["test-bucket/tasks/task-abc123/plan.json"] = b'{"v":2}'  # swapped
    r = conductor_tools.service_launch_run(
        lamf, s3f, kmsf,
        {"plan_uri": "s3://test-bucket/tasks/task-abc123/plan.json", "approval": appr},
        "llmops-start-pipeline")
    assert not r["ok"] and "plan_sha256" in r["reason"]


def test_launch_run_rejects_cost_drift_over_20_percent():
    s3f, lamf, kmsf = FakeS3(), FakeLambda(), FakeKms()
    plan = b"{}"
    s3f.objects["test-bucket/tasks/task-abc123/plan.json"] = plan
    appr = _signed_approval(kmsf, plan, cost="100")
    r = conductor_tools.service_launch_run(
        lamf, s3f, kmsf,
        {"plan_uri": "s3://test-bucket/tasks/task-abc123/plan.json",
         "approval": appr, "cost_estimate_usd": "200"},
        "llmops-start-pipeline",
        expected={"approval": appr, "cost_estimate_usd": "100"})
    assert not r["ok"] and "drifted" in r["reason"]


def test_missing_plan_uri_is_rejected_before_any_invoke():
    s3f, lamf, kmsf = FakeS3(), FakeLambda(), FakeKms()
    r = conductor_tools.service_launch_run(
        lamf, s3f, kmsf, {"plan_uri": "s3://test-bucket/nowhere.json"},
        "llmops-start-pipeline")
    assert not r["ok"] and not lamf.invokes


# ── the chat worker's tool discipline ─────────────────────────────────────────

# where _mk_task stages the plan; launch_run tests must dispatch THIS uri so the
# signed plan_sha256 matches the bytes on the fake bucket
_PLAN_URI = "s3://test-bucket/tasks/task-abc123/plan.json"


def _fake_stream(*events_):
    return {"stream": list(events_)}


def _tool_call_stream(name, args):
    return _fake_stream(
        {"contentBlockStart": {"start": {"toolUse": {"toolUseId": "t1", "name": name}}}},
        {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(args)}}}},
        {"messageStop": {"stopReason": "tool_use"}})


def _text_stream(text):
    return _fake_stream({"contentBlockDelta": {"delta": {"text": text}}},
                        {"messageStop": {"stopReason": "end_turn"}})


def test_worker_rejects_launch_run_before_acceptance(wired, monkeypatch):
    tid = _mk_task(wired, status="thinking")
    calls = {"n": 0}
    results_seen = []

    class _AC:
        def invoke_harness(self, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                return _tool_call_stream("launch_run", {"plan_uri": "s3://b/p.json"})
            # capture the toolResult the worker sent back
            content = kw["messages"][-1]["content"]
            results_seen.extend(c.get("toolResult", {}) for c in content
                                if "toolResult" in c)
            return _text_stream("understood, waiting for acceptance")

    monkeypatch.setattr(wired.console, "agentcore_chat", _AC())
    monkeypatch.setattr(wired.console, "_resolve_harness_id", lambda x: "llmops_orchestrator-x")
    wired.console.run_task_turn(tid, accept=False)
    rejected = [r for r in results_seen
                if json.loads((r.get("content") or [{"text": "{}"}])[0]["text"])
                .get("status") == "rejected"]
    assert rejected, "pre-acceptance launch_run must come back rejected"
    # Pin WHICH gate fired: the acceptance gate, not the deeper missing-approval
    # fallback in service_launch_run. Both reject, but only the first tells the
    # agent what to do (wait for the human) instead of what it lacks (a record).
    reason = json.loads(rejected[0]["content"][0]["text"]).get("reason", "")
    assert "PLAN ACCEPTED" in reason
    assert wired.tasks.items[tid]["status"] == "drafting"


def test_worker_parses_the_plan_trailer_into_plan_proposed(wired, monkeypatch):
    tid = _mk_task(wired, status="thinking")
    reply = ('Here is my proposal.\n\n```json\n'
             '{"plan_uri": "s3://b/tasks/t/plan.json", "plan_summary": "distill 2k", '
             '"cost_estimate_usd": 42.5}\n```')

    class _AC:
        def invoke_harness(self, **kw):
            return _text_stream(reply)

    monkeypatch.setattr(wired.console, "agentcore_chat", _AC())
    monkeypatch.setattr(wired.console, "_resolve_harness_id", lambda x: "llmops_orchestrator-x")
    wired.console.run_task_turn(tid, accept=False)
    t = wired.tasks.items[tid]
    assert t["status"] == "plan_proposed"
    assert t["plan_summary"] == "distill 2k" and t["cost_estimate_usd"] == "42.5"
    assert t["messages"][-1]["role"] == "assistant"


def test_worker_session_expiry_replays_and_bumps_seq(wired, monkeypatch):
    tid = _mk_task(wired, status="thinking")
    # give the task history so the replay has something to reconstruct
    wired.tasks.items[tid]["messages"] += [
        {"role": "assistant", "text": "what data do you have?", "at": "t1", "by": "orchestrator"},
        {"role": "user", "text": "2k rows in s3", "at": "t2", "by": "alice"}]
    calls = {"n": 0}
    seen_bodies = []

    class _AC:
        def invoke_harness(self, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("RuntimeSessionExpired: session not found")
            seen_bodies.append(_last_text(kw["messages"]))
            return _text_stream("continuing")

    monkeypatch.setattr(wired.console, "agentcore_chat", _AC())
    monkeypatch.setattr(wired.console, "_resolve_harness_id", lambda x: "llmops_orchestrator-x")
    wired.console.run_task_turn(tid, accept=False)
    assert calls["n"] == 2
    assert wired.tasks.items[tid]["session_seq"] == 1
    assert "session restarted" in seen_bodies[0]


def test_accepted_turn_that_never_dispatches_is_an_error(wired, monkeypatch):
    tid = _mk_task(wired, status="accepting")
    wired.tasks.items[tid]["approvals"] = [
        _signed_approval(wired.kms, b'{"goal":"x"}')]
    calls = {"n": 0}

    class _AC:
        def invoke_harness(self, **kw):
            calls["n"] += 1
            return _text_stream("I decline to dispatch for no reason")

    monkeypatch.setattr(wired.console, "agentcore_chat", _AC())
    monkeypatch.setattr(wired.console, "_resolve_harness_id", lambda x: "llmops_orchestrator-x")
    wired.console.run_task_turn(tid, accept=True)
    assert wired.tasks.items[tid]["status"] == "error"
    # error only AFTER the re-asks are spent — a refusal is distinguishable from a
    # slip only by asking again.
    assert calls["n"] == 3


def test_narrated_dispatch_without_a_tool_call_is_re_asked(wired, monkeypatch):
    """The live failure: the model says "Dispatching exactly once with that URI:"
    and ends the turn without emitting launch_run. The driver has re-asked since
    Phase 5; the chat worker went straight to error and stranded a signed approval."""
    tid = _mk_task(wired, status="accepting")
    wired.tasks.items[tid]["approvals"] = [
        _signed_approval(wired.kms, b'{"goal":"x"}')]
    calls = {"n": 0}
    prompts = []

    class _AC:
        def invoke_harness(self, **kw):
            calls["n"] += 1
            prompts.append(_last_text(kw["messages"]))
            if calls["n"] == 1:
                return _text_stream("Acceptance received. Dispatching exactly "
                                    "once with that URI:")
            if calls["n"] == 2:
                return _tool_call_stream("launch_run", {"plan_uri": _PLAN_URI})
            return _text_stream("Dispatched — run_id run-disp-0001.")

    monkeypatch.setattr(wired.console, "agentcore_chat", _AC())
    monkeypatch.setattr(wired.console, "_resolve_harness_id", lambda x: "llmops_orchestrator-x")
    wired.console.run_task_turn(tid, accept=True)
    t = wired.tasks.items[tid]
    assert t["status"] == "dispatched", t.get("error_msg")
    assert t["run_id"]
    # the nudge must name the tool, or the model re-narrates instead of calling it
    assert "launch_run" in prompts[1]


def test_a_dispatched_turn_is_not_re_asked_afterwards(wired, monkeypatch):
    """Once the run is launched the obligation is discharged: the agent's closing
    "your run_id is …" message must end the turn, not trigger another nudge."""
    tid = _mk_task(wired, status="accepting")
    wired.tasks.items[tid]["approvals"] = [
        _signed_approval(wired.kms, b'{"goal":"x"}')]
    calls = {"n": 0}

    class _AC:
        def invoke_harness(self, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                return _tool_call_stream("launch_run",
                                         {"plan_uri": _PLAN_URI})
            return _text_stream("Dispatched — your run_id is run-123.")

    monkeypatch.setattr(wired.console, "agentcore_chat", _AC())
    monkeypatch.setattr(wired.console, "_resolve_harness_id", lambda x: "llmops_orchestrator-x")
    wired.console.run_task_turn(tid, accept=True)
    assert wired.tasks.items[tid]["status"] == "dispatched"
    assert calls["n"] == 2


def test_accept_turn_names_the_signed_plan_uri_not_the_default(wired, monkeypatch):
    """The envelope's plan_uri is a SUGGESTION for where to write a new plan; the
    approval binds a specific URI. When the agent wrote its plan somewhere else
    (live: under runs/, not tasks/), telling it the default invites it to dispatch
    a URI that fails the plan_sha256 check — or to stall asking which one is real."""
    tid = _mk_task(wired, status="accepting")
    signed_uri = "s3://test-bucket/runs/task-abc123/plan.json"
    appr = _signed_approval(wired.kms, b'{"goal":"x"}')
    appr["plan_uri"] = signed_uri
    appr = conductor_tools.sign_record(wired.kms, appr)  # re-sign: uri is signed
    wired.tasks.items[tid]["approvals"] = [appr]
    wired.tasks.items[tid]["plan_uri"] = signed_uri
    wired.s3.objects["test-bucket/runs/task-abc123/plan.json"] = b'{"goal":"x"}'
    bodies = []

    class _AC:
        def invoke_harness(self, **kw):
            bodies.append(_last_text(kw["messages"]))
            if len(bodies) == 1:
                return _tool_call_stream("launch_run", {"plan_uri": signed_uri})
            return _text_stream("Dispatched.")

    monkeypatch.setattr(wired.console, "agentcore_chat", _AC())
    monkeypatch.setattr(wired.console, "_resolve_harness_id", lambda x: "llmops_orchestrator-x")
    wired.console.run_task_turn(tid, accept=True)
    env = json.loads(bodies[0].split("\n\n")[0])
    assert env["params"]["plan_uri"] == signed_uri
    assert wired.tasks.items[tid]["status"] == "dispatched"


def test_accept_turn_resent_after_a_failed_attempt_still_carries_the_acceptance(
        wired, monkeypatch):
    """A re-enqueued accept turn has the acceptance message BEFORE the agent's last
    (failed) reply. Slicing "everything after the last assistant message" then sends
    "(continue)" — the agent is told to dispatch by a message it can no longer see,
    and stalls. The acceptance must be restated whenever accept=True."""
    tid = _mk_task(wired, status="accepting")
    wired.tasks.items[tid]["approvals"] = [
        _signed_approval(wired.kms, b'{"goal":"x"}')]
    wired.tasks.items[tid]["messages"] += [
        {"role": "system", "text": "PLAN ACCEPTED by admin (record abc123def456) at t",
         "at": "t1", "by": "system"},
        {"role": "assistant", "text": "Dispatching now — locating launch_run.",
         "at": "t2", "by": "orchestrator"}]
    bodies = []

    class _AC:
        def invoke_harness(self, **kw):
            bodies.append(_last_text(kw["messages"]))
            if len(bodies) == 1:
                return _tool_call_stream("launch_run", {"plan_uri": _PLAN_URI})
            return _text_stream("Dispatched.")

    monkeypatch.setattr(wired.console, "agentcore_chat", _AC())
    monkeypatch.setattr(wired.console, "_resolve_harness_id", lambda x: "llmops_orchestrator-x")
    wired.console.run_task_turn(tid, accept=True)
    assert "PLAN ACCEPTED" in bodies[0], "a resent accept turn lost its acceptance"
    assert "launch_run" in bodies[0]
    assert wired.tasks.items[tid]["status"] == "dispatched"


def test_a_second_launch_run_does_not_start_a_second_run(wired, monkeypatch):
    """One acceptance authorizes one run. "Call it exactly once" is a prompt rule,
    and prompt rules are broken under retry pressure — so the worker enforces it:
    a repeat call is answered with the existing run_id, never a second dispatch."""
    tid = _mk_task(wired, status="accepting")
    wired.tasks.items[tid]["approvals"] = [
        _signed_approval(wired.kms, b'{"goal":"x"}')]
    calls = {"n": 0}
    second = []

    class _AC:
        def invoke_harness(self, **kw):
            calls["n"] += 1
            if calls["n"] <= 2:
                return _tool_call_stream("launch_run", {"plan_uri": _PLAN_URI})
            for tr in _tool_results(kw["messages"]):
                second.append(json.loads(tr["content"][0]["text"]))
            return _text_stream("Understood — run-disp-0001 is already running.")

    monkeypatch.setattr(wired.console, "agentcore_chat", _AC())
    monkeypatch.setattr(wired.console, "_resolve_harness_id", lambda x: "llmops_orchestrator-x")
    wired.console.run_task_turn(tid, accept=True)
    starts = [i for i in wired.lam.invokes
              if i.get("InvocationType") == "RequestResponse"]
    assert len(starts) == 1, "a second launch_run must not reach start-pipeline"
    assert second and second[0]["status"] == "already_dispatched"
    assert second[0]["run_id"] == "run-disp-0001"
    assert wired.tasks.items[tid]["status"] == "dispatched"


def test_tool_results_go_back_as_text_not_json_blocks(wired, monkeypatch):
    """Live: the harness runtime rejected every tool result we sent —
    "runtimeClientError ... content_type=<json_> | unsupported type" — so the loop
    could never acknowledge a launch_run and the dispatch died on the round after
    it succeeded. Converse accepts a text block everywhere; send JSON as text."""
    tid = _mk_task(wired, status="thinking")
    seen = []

    class _AC:
        def invoke_harness(self, **kw):
            seen.extend(_tool_results(kw["messages"]))
            if not seen:
                return _tool_call_stream("checkpoint", {"next_action": "keep going"})
            return _text_stream("done")

    monkeypatch.setattr(wired.console, "agentcore_chat", _AC())
    monkeypatch.setattr(wired.console, "_resolve_harness_id", lambda x: "llmops_orchestrator-x")
    wired.console.run_task_turn(tid, accept=False)
    assert seen, "the worker must send a tool result back"
    blocks = seen[0]["content"]
    assert all("json" not in b for b in blocks), \
        "a json content block is rejected by the harness runtime"
    body = json.loads(blocks[0]["text"])   # still machine-readable
    assert body["status"] == "continue"


def test_a_tool_use_block_with_end_turn_is_not_answered_with_a_tool_result(
        wired, monkeypatch):
    """The harness awaits a toolResult only when it stopped FOR one (stopReason=
    tool_use). A toolUse block that streams alongside end_turn is something the
    harness already ran itself (live: a `shell` call), and answering it makes the
    NEXT ConverseStream invalid: "The number of toolResult blocks at
    messages.N.content exceeds the number of toolUse blocks of previous turn."
    That ValidationException killed three consecutive accept turns.

    So the stop reason is the gate, not a hint. An accept turn that ends this way
    owes a dispatch, and gets a plain-text re-ask instead."""
    tid = _mk_task(wired, status="accepting")
    wired.tasks.items[tid]["approvals"] = [
        _signed_approval(wired.kms, b'{"goal":"x"}')]
    sent = []

    class _AC:
        def invoke_harness(self, **kw):
            sent.append(kw["messages"][0]["content"])
            if len(sent) == 1:
                return _fake_stream(
                    {"contentBlockStart": {"start": {"toolUse": {
                        "toolUseId": "t1", "name": "launch_run"}}}},
                    {"contentBlockDelta": {"delta": {"toolUse": {
                        "input": json.dumps({"plan_uri": _PLAN_URI})}}}},
                    {"messageStop": {"stopReason": "end_turn"}})
            return _fake_stream(
                {"contentBlockStart": {"start": {"toolUse": {
                    "toolUseId": "t2", "name": "launch_run"}}}},
                {"contentBlockDelta": {"delta": {"toolUse": {
                    "input": json.dumps({"plan_uri": _PLAN_URI})}}}},
                {"messageStop": {"stopReason": "tool_use"}})

    monkeypatch.setattr(wired.console, "agentcore_chat", _AC())
    monkeypatch.setattr(wired.console, "_resolve_harness_id", lambda x: "llmops_orchestrator-x")
    wired.console.run_task_turn(tid, accept=True)

    # nothing the worker ever sent may be a toolResult for the end_turn call
    for content in sent[1:2]:
        assert not any("toolResult" in blk for blk in content), \
            "answered a toolUse the harness did not stop for"
    # ...and the genuine tool_use turn still dispatches
    assert wired.tasks.items[tid]["status"] == "dispatched"
    assert wired.tasks.items[tid]["run_id"] == "run-disp-0001"


def test_resuming_a_paused_tool_call_echoes_the_tooluse_before_the_toolresult(
        wired, monkeypatch):
    """The InvokeHarness resume contract takes TWO messages: an assistant message
    echoing the toolUse the agent emitted, then a user message with the matching
    toolResult. Sending the toolResult alone is what produced, live and on every
    fresh session, "The number of toolResult blocks at messages.N.content exceeds
    the number of toolUse blocks of previous turn" — the harness sees a result for a
    call that, in the history it was handed, was never made. Three sessions of
    dispatch attempts died here, one ValidationException per launch_run."""
    tid = _mk_task(wired, status="accepting")
    wired.tasks.items[tid]["approvals"] = [
        _signed_approval(wired.kms, b'{"goal":"x"}')]
    sent = []

    class _AC:
        def invoke_harness(self, **kw):
            sent.append(kw["messages"])
            if len(sent) == 1:
                return _fake_stream(
                    {"contentBlockStart": {"start": {"toolUse": {
                        "toolUseId": "tu-42", "name": "launch_run"}}}},
                    {"contentBlockDelta": {"delta": {"toolUse": {
                        "input": json.dumps({"plan_uri": _PLAN_URI})}}}},
                    {"messageStop": {"stopReason": "tool_use"}})
            return _text_stream("Dispatched run-disp-0001.")

    monkeypatch.setattr(wired.console, "agentcore_chat", _AC())
    monkeypatch.setattr(wired.console, "_resolve_harness_id", lambda x: "llmops_orchestrator-x")
    wired.console.run_task_turn(tid, accept=True)

    resume = sent[1]
    assert [m["role"] for m in resume] == ["assistant", "user"], \
        "resume must echo the assistant toolUse before the user toolResult"
    echoed = resume[0]["content"][0]["toolUse"]
    assert echoed["toolUseId"] == "tu-42"
    assert echoed["name"] == "launch_run"
    assert echoed["input"] == {"plan_uri": _PLAN_URI}
    result = resume[1]["content"][0]["toolResult"]
    assert result["toolUseId"] == "tu-42", "toolUseId must match or the resume is rejected"
    assert json.loads(result["content"][0]["text"])["status"] == "dispatched"
    assert wired.tasks.items[tid]["status"] == "dispatched"


def test_a_content_filtered_turn_fails_loudly_instead_of_blaming_the_agent(
        wired, monkeypatch):
    """stopReason=content_filtered with zero output means the model was blocked, not
    that it refused to dispatch. Re-asking into the same poisoned session just burns
    the budget; the operator needs the real reason in error_msg."""
    tid = _mk_task(wired, status="accepting")
    wired.tasks.items[tid]["approvals"] = [
        _signed_approval(wired.kms, b'{"goal":"x"}')]
    calls = {"n": 0}

    class _AC:
        def invoke_harness(self, **kw):
            calls["n"] += 1
            return _fake_stream({"messageStop": {"stopReason": "content_filtered"}})

    monkeypatch.setattr(wired.console, "agentcore_chat", _AC())
    monkeypatch.setattr(wired.console, "_resolve_harness_id", lambda x: "llmops_orchestrator-x")
    wired.console.run_task_turn(tid, accept=True)
    t = wired.tasks.items[tid]
    assert t["status"] == "error"
    assert "content_filtered" in str(t.get("error_msg", "")), t.get("error_msg")
    assert calls["n"] == 1, "do not re-ask a filtered session — nothing can get out"


def test_a_dispatched_run_survives_a_runaway_tool_loop(wired, monkeypatch):
    """If the agent keeps calling tools past the round cap AFTER dispatching, the
    run exists and is spending money. Reporting the task as `error` would hide a
    live run from the operator — the cap must not erase what already happened."""
    tid = _mk_task(wired, status="accepting")
    wired.tasks.items[tid]["approvals"] = [
        _signed_approval(wired.kms, b'{"goal":"x"}')]

    class _AC:
        def invoke_harness(self, **kw):
            return _tool_call_stream("launch_run", {"plan_uri": _PLAN_URI})

    monkeypatch.setattr(wired.console, "agentcore_chat", _AC())
    monkeypatch.setattr(wired.console, "_resolve_harness_id", lambda x: "llmops_orchestrator-x")
    wired.console.run_task_turn(tid, accept=True)
    t = wired.tasks.items[tid]
    assert t["status"] == "dispatched", "the cap must not bury a launched run"
    assert t["run_id"] == "run-disp-0001"
    starts = [i for i in wired.lam.invokes if i.get("InvocationType") == "RequestResponse"]
    assert len(starts) == 1


def test_consult_turn_ending_in_prose_is_not_re_asked(wired, monkeypatch):
    """Re-asking is scoped to turns that OWE a tool call. A consult reply that is
    just questions for the customer is a complete turn, not a missing signal."""
    tid = _mk_task(wired, status="thinking")
    calls = {"n": 0}

    class _AC:
        def invoke_harness(self, **kw):
            calls["n"] += 1
            return _text_stream("Where is your data, and is it verifiable?")

    monkeypatch.setattr(wired.console, "agentcore_chat", _AC())
    monkeypatch.setattr(wired.console, "_resolve_harness_id", lambda x: "llmops_orchestrator-x")
    wired.console.run_task_turn(tid, accept=False)
    assert calls["n"] == 1
    assert wired.tasks.items[tid]["status"] == "drafting"


def test_worker_salvages_an_interrupted_stream_once(wired, monkeypatch):
    """Stream death mid-drain is routine in production. The driver retries in the
    same session once; without that, an accept turn dies as a false non-dispatch."""
    tid = _mk_task(wired, status="accepting")
    wired.tasks.items[tid]["approvals"] = [
        _signed_approval(wired.kms, b'{"goal":"x"}')]
    calls = {"n": 0}
    sessions = []

    def _dying_stream():
        yield {"contentBlockDelta": {"delta": {"text": "Dispatching"}}}
        raise RuntimeError("connection reset by peer")

    class _AC:
        def invoke_harness(self, **kw):
            calls["n"] += 1
            sessions.append(kw["runtimeSessionId"])
            if calls["n"] == 1:
                return {"stream": _dying_stream()}
            if calls["n"] == 2:
                return _tool_call_stream("launch_run", {"plan_uri": _PLAN_URI})
            return _text_stream("Dispatched — run_id run-disp-0001.")

    monkeypatch.setattr(wired.console, "agentcore_chat", _AC())
    monkeypatch.setattr(wired.console, "_resolve_harness_id", lambda x: "llmops_orchestrator-x")
    wired.console.run_task_turn(tid, accept=True)
    assert wired.tasks.items[tid]["status"] == "dispatched"
    # SAME session: the salvage resumes the conversation, it does not restart it
    # (a new session would lose the plan the agent just wrote).
    assert sessions[0] == sessions[1]
    assert wired.tasks.items[tid]["session_seq"] == 0


# ── close & audit surface ─────────────────────────────────────────────────────

def test_close_requires_a_reason_and_the_right_person(wired):
    tid = _mk_task(wired)
    assert wired.console.close_task(tid, {}, DS_USER)["status_code"] == 400
    r = wired.console.close_task(tid, {"reason": "changed my mind"},
                                 {"username": "mallory", "groups": ["llmops-datascience"]})
    assert r["status_code"] == 403  # not the creator, not an approver
    r = wired.console.close_task(tid, {"reason": "changed my mind"}, DS_USER)
    assert r["ok"] and wired.tasks.items[tid]["status"] == "closed"


def test_task_events_feed_the_lifecycle_timeline(wired):
    wired.console.create_task({"goal": "g"}, DS_USER)
    names = [p.get("event_name") for p in wired.events.puts]
    assert "TaskCreated" in names


# ── canonicalization properties ───────────────────────────────────────────────

def test_canonical_json_is_order_insensitive():
    a = {"task_id": "t", "plan_uri": "u", "decision": "accepted"}
    b = {"decision": "accepted", "plan_uri": "u", "task_id": "t"}
    assert conductor_tools.canonical_json(a) == conductor_tools.canonical_json(b)


def test_signature_field_is_excluded_from_the_signed_digest():
    kmsf = FakeKms()
    rec = {"task_id": "t", "plan_uri": "u", "plan_sha256": "h", "decision": "accepted",
           "approved_by": "a", "approved_at": "t", "prev_event_sha256": "genesis"}
    signed = conductor_tools.sign_record(kmsf, rec)
    # verifying uses only SIGNED_KEYS, so the signature's presence must not change it
    assert conductor_tools.record_sha256(signed) == conductor_tools.record_sha256(rec)


# ── the lifecycle flow must know every status something can write ─────────────

def test_the_lifecycle_flow_renders_every_terminal_status_the_pipeline_writes():
    """The state machine now closes tasks, and it writes statuses the UI never saw.

    taskStageStates() maps a status onto the 7 lifecycle nodes via an `order`
    lookup with `(s in order) ? order[s] : 0` -- so an UNKNOWN status silently
    becomes position 0 and the task renders as *active at Requirements*. A run
    that finished an hour ago would look like a consultation that had barely
    started, which is worse than the zombie 'dispatched' the closers were added
    to fix: that at least said something true.

    The statuses are read out of the ASL closers rather than listed here, so
    adding a third closer (or renaming a status) fails this instead of quietly
    reintroducing the mis-render.
    """
    asl = json.loads((REPO / "orchestration/state_machine.asl.json").read_text())
    written = set()
    for st in asl["States"].values():
        p = st.get("Parameters", {})
        if p.get("TableName") != "llmops-tasks":
            continue
        for name, val in p.get("ExpressionAttributeValues", {}).items():
            # only the values the UpdateExpression assigns to #s (status)
            if f"#s = {name}" in p.get("UpdateExpression", ""):
                written.add(val["S"])
    assert written, "no llmops-tasks closer found -- did the state names change?"

    front = (REPO / "deploy/console/frontend.html").read_text()
    order = front[front.index("const order = {"):]
    order = order[:order.index("};") + 1]
    missing = sorted(s for s in written if f"{s}:" not in order)
    assert not missing, (
        f"the state machine writes task status {missing} but taskStageStates()'s "
        "order map has no entry, so `(s in order) ? order[s] : 0` renders a "
        "finished task as active at the first node")


# ── the stage flow must not call a budget stop a failure ──────────────────────

def _fake_sfn_history(events, exec_status, run_id="run-x", definition=None):
    asl = definition if definition is not None else json.dumps(
        json.loads((REPO / "orchestration/state_machine.asl.json").read_text()))

    class _Sfn:
        def describe_execution(self, executionArn):
            return {"status": exec_status, "input": json.dumps({"run_id": run_id}),
                    "startDate": "s", "stopDate": "e"}

        def get_execution_history(self, **kw):
            return {"events": events}

        def describe_state_machine(self, stateMachineArn):
            return {"definition": asl}
    return _Sfn()


#: The live history of run-20260731T183103Z-8b864805, event for event (verified against
#: GetExecutionHistory: 16 events). The .waitForTaskToken task TIMED OUT after 7200s
#: with States.Timeout, the Catch ran EscalateFail, and MarkRunFailed then died on
#: States.Runtime because EscalateFail had no ResultPath at the time.
#:
#: What the agent was doing is NOT visible here and that is the whole lesson: the
#: manifest's last entry is complete-at-cap and stage-events holds two stage_complete
#: rows (19:23:49, 19:26:20). The driver crashed writing the canonical report -- it had
#: no s3:PutObject until 19:30 -- BEFORE it settled the token. So the stage finished and
#: the token parked. A timeout is a crash, not a question.
_LIVE_TIMEOUT_HISTORY = [
    {"stateEnteredEventDetails": {"name": "DataPrepGenerate"}},
    {"taskTimedOutEventDetails": {"error": "States.Timeout"}},
    {"stateExitedEventDetails": {"name": "DataPrepGenerate"}},
    {"stateEnteredEventDetails": {"name": "EscalateFail"}},
    {"stateExitedEventDetails": {"name": "EscalateFail"}},
    {"stateEnteredEventDetails": {"name": "MarkRunFailed"}},
]

#: The same shape, but the driver reported the error handle_escalate sends when the
#: agent called escalate_human. This -- not the state name -- is an escalation.
_ESCALATED_HISTORY = [
    {"stateEnteredEventDetails": {"name": "DataPrepGenerate"}},
    {"taskFailedEventDetails": {"error": "EscalatedToHuman",
                                "cause": "teacher token cap infeasible; options A-D"}},
    {"stateExitedEventDetails": {"name": "DataPrepGenerate"}},
    {"stateEnteredEventDetails": {"name": "EscalateFail"}},
    {"stateExitedEventDetails": {"name": "EscalateFail"}},
    {"stateEnteredEventDetails": {"name": "MarkRunFailed"}},
]


def test_a_stage_that_asked_a_human_is_not_painted_as_a_crash(console, monkeypatch):
    """A stage that stopped to ASK something is waiting on us, not broken.

    The signal is the driver's own error string (handle_escalate sends
    error="EscalatedToHuman"), because that is the only place the two cases differ.
    """
    monkeypatch.setattr(console, "sfn", _fake_sfn_history(_ESCALATED_HISTORY, "FAILED"))
    out = console.pipeline_detail("run-asked")
    gen = next(s for s in out["stages"] if s["key"] == "data-prep-generate")
    assert out["escalated"] is True
    assert gen["status"] == "escalated", (
        f"the stage that escalated for a human decision reads {gen['status']!r}; "
        "'failed' sends the operator to debug a stage that did the right thing")


def test_reaching_escalatefail_is_not_by_itself_an_escalation(console, monkeypatch):
    """The trap that a first pass at this fell into, kept as a test.

    EscalateFail is the Catch target of 9 of the 11 stages, so EVERY crash routes
    through it. Deriving `escalated` from `name in TERMINAL_FAIL_STATES` therefore
    painted crashes amber -- and the crash test written alongside it passed only
    because its hand-written history omitted EscalateFail, which no real crashed run
    does. This is the live run: it timed out, it never asked anything, it is red.
    """
    states = json.loads((REPO / "orchestration/state_machine.asl.json").read_text())["States"]
    catchers = [n for n, st in states.items()
                if any(c.get("Next") == "EscalateFail" for c in st.get("Catch", []))]
    assert len(catchers) > 1, (
        f"EscalateFail is the catch-all for {catchers}; a single-stage EscalateFail "
        "would make the state name a valid escalation signal, and this test moot")

    monkeypatch.setattr(console, "sfn", _fake_sfn_history(_LIVE_TIMEOUT_HISTORY, "FAILED"))
    out = console.pipeline_detail("run-20260731T183103Z-8b864805")
    gen = next(s for s in out["stages"] if s["key"] == "data-prep-generate")
    assert out["escalated"] is False, (
        "EscalateFail ran because the Catch fired, not because anyone was asked")
    assert gen["status"] == "failed", (
        "States.Timeout is a crash: the driver died writing the report before it "
        "settled the token. Amber would tell the operator to wait for nothing")


def test_a_genuine_crash_still_reads_failed(console, monkeypatch):
    """The counterweight: softening the escalation case must not soften a real crash."""
    crash = [{"stateEnteredEventDetails": {"name": "FinetuneLaunch"}},
             {"taskFailedEventDetails": {"error": "DriverCrashed", "cause": "boom"}},
             {"stateEnteredEventDetails": {"name": "EscalateFail"}}]
    monkeypatch.setattr(console, "sfn", _fake_sfn_history(crash, "FAILED"))
    out = console.pipeline_detail("run-crash")
    ft = next(s for s in out["stages"] if s["key"] == "finetune-launch")
    assert out["escalated"] is False
    assert ft["status"] == "failed", "a crash with no escalation must still read failed"


def test_one_stage_asking_does_not_repaint_another_stages_crash(console, monkeypatch):
    """The status is per stage, so it must be decided from THAT stage's error.

    A run-wide `escalated` flag repaints every stopped stage the same colour: one
    stage politely asking a question would hide a different stage's crash behind
    amber, which is the original bug with the sign flipped.
    """
    mixed = [{"stateEnteredEventDetails": {"name": "DataPrepGenerate"}},
             {"taskFailedEventDetails": {"error": "EscalatedToHuman", "cause": "budget?"}},
             {"stateExitedEventDetails": {"name": "DataPrepGenerate"}},
             {"stateEnteredEventDetails": {"name": "FinetuneLaunch"}},
             {"taskFailedEventDetails": {"error": "DriverCrashed", "cause": "AccessDenied"}}]
    monkeypatch.setattr(console, "sfn", _fake_sfn_history(mixed, "FAILED"))
    out = console.pipeline_detail("run-mixed")
    by = {s["key"]: s for s in out["stages"]}
    assert by["finetune-launch"]["status"] == "failed", (
        "the crashed stage must stay red even though another stage escalated")


# ── the hover card: visibility into what is behind each stage box ─────────────

def test_the_stage_hover_config_comes_from_the_deployed_definition(console, monkeypatch):
    """The card answers "which AgentCore runtime is behind this box, with what timeout".

    It is read from DescribeStateMachine, not hardcoded in the console: a second copy
    would answer for whatever the console was packaged with, and a stale answer here is
    undetectable by the operator asking the question.
    """
    console._STAGE_CFG_CACHE.clear()
    monkeypatch.setattr(console, "sfn", _fake_sfn_history(_ESCALATED_HISTORY, "FAILED"))
    out = console.pipeline_detail("run-asked")
    by = {s["key"]: s for s in out["stages"]}

    asl = json.loads((REPO / "orchestration/state_machine.asl.json").read_text())["States"]
    for state, key in console.STATE_TO_STAGE.items():
        pay = (asl[state].get("Parameters") or {}).get("Payload") or {}
        if not pay.get("harness_id"):
            continue
        cfg = by[key]["config"]
        assert cfg["harnessId"] == pay["harness_id"], f"{key}: wrong harness id"
        assert cfg["runtime"] == "harness_" + pay["harness_id"]
        assert state in cfg["states"], f"{key}: {state} missing from states"
        if asl[state].get("TimeoutSeconds"):
            assert cfg["timeoutSeconds"] == asl[state]["TimeoutSeconds"]


def test_the_remediation_stage_reports_both_states_it_can_run_as(console, monkeypatch):
    """RemediateFinetune and FinetuneLaunch map to ONE box. Whichever the dict happens
    to visit second must not overwrite the first, or the card names the wrong state for
    half the runs -- silently, since both are plausible."""
    console._STAGE_CFG_CACHE.clear()
    monkeypatch.setattr(console, "sfn", _fake_sfn_history([], "SUCCEEDED"))
    cfg = console.stage_config()["finetune-launch"]
    assert set(cfg["states"]) == {"FinetuneLaunch", "RemediateFinetune"}, cfg["states"]


def test_a_hover_card_never_breaks_the_flow_diagram(console, monkeypatch):
    """If DescribeStateMachine is denied or the definition is unparseable, the stage
    flow itself must still render. A tooltip is an enhancement; taking the operator's
    only live view of the pipeline down to serve one would be a bad trade."""
    console._STAGE_CFG_CACHE.clear()

    class _Broken:
        def describe_execution(self, executionArn):
            return {"status": "SUCCEEDED", "input": "{}", "startDate": "s", "stopDate": "e"}

        def get_execution_history(self, **kw):
            return {"events": []}

        def describe_state_machine(self, stateMachineArn):
            raise RuntimeError("AccessDenied: states:DescribeStateMachine")
    monkeypatch.setattr(console, "sfn", _Broken())
    out = console.pipeline_detail("run-x")
    assert len(out["stages"]) == len(console.STAGE_FLOW), "the flow must still render"
    assert "_error" in console.stage_config(), "and it must SAY the config is missing"


def test_every_field_the_hover_card_renders_is_supplied_by_the_api(console, monkeypatch):
    """stageTipRows() reads st.<field> and st.config.<field>. A field the API never
    sends renders as a silently-absent row, so the card quietly loses the very fact
    the operator hovered to find."""
    console._STAGE_CFG_CACHE.clear()
    monkeypatch.setattr(console, "sfn", _fake_sfn_history(_ESCALATED_HISTORY, "FAILED"))
    out = console.pipeline_detail("run-asked")
    gen = next(s for s in out["stages"] if s["key"] == "data-prep-generate")

    front = (REPO / "deploy/console/frontend.html").read_text()
    body = front[front.index("function stageTipRows"):front.index("function showStageTip")]
    stage_fields = set(re.findall(r"\bst\.([A-Za-z_]\w*)", body)) - {"config"}
    cfg_fields = set(re.findall(r"\bc\.([A-Za-z_]\w*)", body)) - {"_error"}
    assert stage_fields, "regex found no st.<field> reads -- did the function move?"
    missing = sorted(f for f in stage_fields if f not in gen)
    assert not missing, f"the card renders st.{missing} but pipeline_detail never sends it"

    # Union across stages, not one stage: heartbeatSeconds exists only on the two
    # long-running SageMaker states, so requiring every field on data-prep would force
    # a fake key onto stages that genuinely do not have one.
    every_cfg = set()
    for s in out["stages"]:
        every_cfg |= set(s["config"])
    missing = sorted(f for f in cfg_fields if f not in every_cfg)
    assert not missing, f"the card renders config.{missing} but stage_config never sets it"


def test_the_frontend_has_a_colour_for_every_status_the_api_can_return():
    """stageColor() ends in `return "#5a6491"; // pending`, so an unknown status is
    painted as PENDING -- the exact opposite of what an escalation means. Adding
    'escalated' server-side without adding it here would have turned a red-but-wrong
    node into a grey-and-invisible one, which is worse: nobody chases grey.
    """
    src = (REPO / "deploy/console/lambda_function.py").read_text()
    # Scope to pipeline_detail AND its nested _stop_status: the console assigns plenty
    # of `status = "..."` for TASK records too, and those are the Tasks tab's business.
    # Sliced to the next TOP-LEVEL def so the nested helper stays inside.
    fn = src[src.index("def pipeline_detail("):]
    fn = fn[:fn.index("\ndef ", 1)]
    # Any bare string literal the function can hand back as a stage status. Written
    # against literals rather than one expression's syntax: the previous version
    # matched the exact `"x" if escalated else "y"` ternary and broke the moment that
    # per-run flag was replaced by a per-stage helper -- a guard that fails on a
    # refactor of the thing it guards teaches people to delete guards.
    produced = set(re.findall(r'status = "([a-z-]+)"', fn))
    produced |= set(re.findall(r'return "([a-z-]+)" if ', fn))
    produced |= set(re.findall(r'else "([a-z-]+)"', fn))
    assert {"failed", "escalated", "succeeded", "running"} <= produced, (
        f"only found statuses {sorted(produced)} -- the extraction missed some, so this "
        "test would pass with an uncoloured status live")
    front = (REPO / "deploy/console/frontend.html").read_text()
    body = front[front.index("function stageColor("):]
    body = body[:body.index("\n}")]
    missing = sorted(s for s in produced if f'"{s}"' not in body)
    assert not missing, (
        f"pipeline_detail can return stage status {missing}, and stageColor() has no "
        "branch for it, so it falls through to the pending colour")
