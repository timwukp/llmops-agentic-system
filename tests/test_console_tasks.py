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
#: A *different* account, for the tests that check an id is compared rather than ignored.
#: Derived from ACCOUNT rather than written out: hooks/pre-commit rejects bare 12-digit
#: literals and allow-lists only the documentation account above, so a second literal here
#: would block the commit — correctly, since the hook cannot tell a fake id from a real one.
OTHER_ACCOUNT = ACCOUNT[::-1]

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
        expr = kw.get("UpdateExpression", "")
        # SET and REMOVE are separate clauses of one expression. Splitting the string on
        # commas without honouring that made "SET #s = :s REMOVE partial_reply" parse as
        # an assignment whose rhs is ":s REMOVE partial_reply" -- matching no placeholder,
        # so the status write vanished SILENTLY and the task stayed 'thinking'. The real
        # table applies both clauses; so must this.
        expr, _, remove_clause = expr.partition("REMOVE")
        for attr in remove_clause.split(","):
            attr = names.get(attr.strip(), attr.strip())
            if attr:
                it.pop(attr, None)
        expr = expr.replace("SET ", "")
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
        self.signed = []          # every generate_presigned_url call, in order

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

    def generate_presigned_url(self, ClientMethod, Params=None, ExpiresIn=None):
        """Records the call and returns a URL shaped like the real one.

        Deliberately NOT a signature: what these tests assert is which Key and
        ContentType were signed, and that is in `Params`. Returning a plausible URL
        while recording the exact params keeps the assertions on the thing that
        matters -- a key that escapes its prefix -- rather than on boto's crypto.
        """
        self.signed.append({"method": ClientMethod, "params": dict(Params or {}),
                            "expires_in": ExpiresIn})
        p = Params or {}
        return (f"https://{p.get('Bucket')}.s3.us-east-1.amazonaws.com/"
                f"{p.get('Key')}?X-Amz-Signature=fake")


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
    # Captured BEFORE the stub replaces it, so the `audited` fixture can put the real
    # append back. Reaching for console._transcript_append afterwards would only get
    # the stub, which is the trap this hands around.
    real_transcript_append = console._transcript_append
    monkeypatch.setattr(console, "_transcript_append", lambda *a, **k: None)

    import cost_model
    monkeypatch.setattr(console, "_cost_model", lambda: cost_model)
    monkeypatch.setattr(console, "project_to_date_usd", lambda *a, **k: (0.0, "none"))
    return types.SimpleNamespace(console=console, tasks=tasks, events=events,
                                 est=est, s3=fake_s3, lam=fake_lam, kms=fake_kms,
                                 real_transcript_append=real_transcript_append)


@pytest.fixture
def blocking(wired, monkeypatch):
    """The same console with BUDGET_MODE=blocking, so the Cost-queue branch of
    accept_task stays covered. Advisory is the default because this platform's owner is
    its only approver — but the queue path is shipped code one env var away."""
    monkeypatch.setattr(wired.console, "BUDGET_MODE", "blocking")
    return wired


@pytest.fixture
def audited(wired, monkeypatch):
    """`wired` stubs _transcript_append out, which is right for every test that is not
    ABOUT the audit copy -- and is exactly why three defects lived in those 12 lines with
    the suite green. This fixture puts the real function back."""
    monkeypatch.setattr(wired.console, "_transcript_append", wired.real_transcript_append)
    return wired


def _transcript_lines(w, tid):
    """Every audit entry for a task, in key order. Keys are timestamp-prefixed, so key
    order IS chronological order -- the property that lets one-object-per-append replace
    a read-modify-write of a single file."""
    out = []
    for k in sorted(w.s3.objects):
        if f"tasks/{tid}/transcript/" not in k:
            continue
        body = w.s3.objects[k]
        body = body if isinstance(body, bytes) else str(body).encode()
        out += [json.loads(ln) for ln in body.decode().splitlines() if ln.strip()]
    return out


#: A priced plan 50% over w's single-run reference, derived rather than typed.
#:
#: As the literal "3000" this was over a $2,000 reference and became UNDER a $20,000 one on
#: 2026-08-02 -- which does not fail, it silently turns "the overage is signed into the
#: record" into an assertion about a plan that was never over budget. Three tests here read
#: as passing while checking nothing. 1.5x keeps the "50% over" the docstrings describe, and
#: keeps it 50% over whatever the reference becomes next.
def _over_limit_cost(w):
    return str(w.console.APPROVAL_LIMIT_USD * 1.5)


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


def test_accept_over_budget_dispatches_but_signs_the_overage(wired):
    """Advisory mode at the acceptance boundary.

    A plan 50% over the reference dispatches — the owner is the only approver, so a queue
    here could only ask them to approve their own run. What must NOT happen is the overage
    disappearing: it goes into the KMS-signed approval record, which is the artifact a
    third party audits later. A signed record that reads as within-budget when the plan
    was 50% over is a false attestation, not a relaxed policy.
    """
    tid = _mk_task(wired, cost=_over_limit_cost(wired))
    r = wired.console.accept_task(tid, DS_USER)
    assert r["ok"] and r["status"] == "accepting"
    signed = wired.tasks.items[tid]["approvals"][-1]
    gate = signed["record"]["gate"] if "record" in signed else signed["gate"]
    assert gate["budget_mode"] == "advisory"
    assert gate["over_budget"], "the signed record must carry the overage"
    assert any("single-run" in x for x in gate["over_budget"])
    assert gate["approval_required"] is False
    # The signature must cover those bytes, or "signed record" overstates what the
    # artifact proves: erasing the overage afterwards would leave a record that still
    # verifies. verify_record re-derives the digest from the contents, so the edit below
    # must break it -- asserted, not assumed, because a fake that ignored Message would
    # make every signature test above vacuous.
    ct = wired.console.conductor_tools
    assert ct.verify_record(wired.kms, signed) is True
    tampered = json.loads(json.dumps(signed))
    tampered["gate"]["over_budget"] = []
    assert ct.verify_record(wired.kms, tampered) is False


def test_accept_over_limit_goes_to_the_cost_queue(blocking):
    wired = blocking
    tid = _mk_task(wired, cost=_over_limit_cost(wired))
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
    tid = _mk_task(wired, cost=_over_limit_cost(wired))
    r = wired.console.accept_task(tid, NOBODY)
    assert r["status_code"] == 403
    assert not [p for p in wired.est.puts if p.get("task_id") == tid]


def test_over_limit_self_approval_is_blocked_at_the_cost_queue(blocking):
    """The REAL self-approval control lives in decide_approval via
    cost_model.check_approval (requester != approver) — exactly the same rule the
    form-based estimates enforce. Alice created and submitted; Alice may not decide."""
    wired = blocking
    tid = _mk_task(wired, cost=_over_limit_cost(wired))
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
    tid = _mk_task(wired, cost=_over_limit_cost(wired))
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
    tid = _mk_task(wired, cost=_over_limit_cost(wired))
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
    tid = _mk_task(wired, cost=_over_limit_cost(wired))
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


def test_a_dispatch_with_no_verifier_is_refused_rather_than_waved_through():
    """The authorization boundary must fail CLOSED when the verifier is absent.

    This was `if kms is not None and not verify_record(...)`, and the bypass it opened was
    total, not partial: with kms=None an approval record carrying **no signature at all**
    came back `{"ok": True}` and invoked start-pipeline. Nothing depended on it — every call
    site passes a client (`_kms(c)` in the driver, the module-level `kms` in the console) —
    so its only effect was that the one check between a forged "a human said yes" and a
    dispatch disappeared along with the client. A verification that could not run is not a
    verification that passed, which is the rule the deploy read-back and the judge-instrument
    attestation already follow; on an authorization boundary the difference IS the boundary.
    """
    s3f, lamf = FakeS3(), FakeLambda()
    plan = b"{}"
    s3f.objects["test-bucket/tasks/task-abc123/plan.json"] = plan
    unsigned = {"task_id": "task-abc123",
                "plan_uri": "s3://test-bucket/tasks/task-abc123/plan.json",
                "plan_sha256": hashlib.sha256(plan).hexdigest(),
                "decision": "accepted", "approved_by": "nobody", "approved_at": "t",
                "prev_event_sha256": "genesis"}
    assert "signature" not in unsigned, "the fixture has to be genuinely unsigned"
    r = conductor_tools.service_launch_run(
        lamf, s3f, None,
        {"plan_uri": "s3://test-bucket/tasks/task-abc123/plan.json", "approval": unsigned},
        "llmops-start-pipeline")
    assert not r["ok"], f"an unsigned approval dispatched a run with no verifier: {r}"
    assert "could not run" in r["reason"], r["reason"]
    # The load-bearing half. A rejection that still invoked start-pipeline would be a
    # rejection of the toolResult only, and the run would already be going.
    assert not lamf.invokes, "the dispatch happened anyway"


def test_an_approval_that_was_never_signed_fails_verification():
    """The gap between "tampered" and "unsigned". `test_tampered_approval_record_fails_
    verification` edits a signed record, so the signature is present and simply no longer
    matches; nothing covered a record with no `signature` block at all — the shape a forged
    approval would actually arrive in, since a forger has no key to sign with. Both the
    absent block and an empty value must be refused before any KMS call is attempted.
    """
    kmsf = FakeKms()
    plan = b"{}"
    signed = _signed_approval(kmsf, plan)
    assert conductor_tools.verify_record(kmsf, signed) is True, "the fixture must verify"
    for label, rec in (("no signature block", {k: v for k, v in signed.items()
                                               if k != "signature"}),
                       ("empty signature block", {**signed, "signature": {}}),
                       ("empty signature value", {**signed,
                                                  "signature": {**signed["signature"],
                                                                "value": ""}})):
        assert conductor_tools.verify_record(kmsf, rec) is False, (
            f"an approval with {label} verified")


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


def _harness_loop_stream(text, internal_tools=("shell", "shell")):
    """One InvokeHarness response carrying the harness's OWN agent loop.

    Copied from a live orchestrator turn (probe: skill-reading prompt), because the
    fakes above are missing the events that make this measurable -- none of them emit
    `messageStart` or `metadata` at all, so every existing test sees a stream in which
    the harness did no internal work. That is a fake that cannot express the situation
    being tested, and it is why `rounds=0` looked correct in tests for so long.

    Live shape, with 2 internal shell calls:

        messageStart(assistant) -> toolUse shell -> messageStop tool_use -> metadata
        messageStart(user)      -> messageStop tool_result
        messageStart(assistant) -> toolUse shell -> messageStop tool_use -> metadata
        messageStart(user)      -> messageStop tool_result
        messageStart(assistant) -> text          -> messageStop end_turn -> metadata

    3 assistant messages = 3 model round-trips; latencyMs 6973/7764/6950 = 21.7s of a
    29.6s turn. The final messageStop is end_turn, so nothing here reaches the worker's
    own servicing loop -- the whole point is that this turn used to log rounds=0.
    """
    evs, lat = [], [6973, 7764, 6950]
    for i, name in enumerate(internal_tools):
        evs += [
            {"messageStart": {"role": "assistant"}},
            {"contentBlockStart": {"start": {"toolUse": {"toolUseId": f"h{i}",
                                                        "name": name}}}},
            {"messageStop": {"stopReason": "tool_use"}},
            {"metadata": {"usage": {"inputTokens": 11773 + i, "outputTokens": 186 + i},
                          "metrics": {"latencyMs": lat[i % len(lat)]}}},
            {"messageStart": {"role": "user"}},
            {"messageStop": {"stopReason": "tool_result"}},
        ]
    evs += [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockDelta": {"delta": {"text": text}}},
        {"messageStop": {"stopReason": "end_turn"}},
        {"metadata": {"usage": {"inputTokens": 12696, "outputTokens": 386},
                      "metrics": {"latencyMs": lat[-1]}}},
    ]
    return _fake_stream(*evs)


def test_the_log_reports_the_real_model_round_trips_not_just_the_ones_we_service(
        wired, monkeypatch, capsys):
    """`rounds=` structurally could not show what its name claimed.

    It incremented only where the worker services an inline function (stop_reason ==
    "tool_use"), so everything the HARNESS ran on its own -- a skill read, `shell`, the
    browser -- was invisible. Verified live on the orchestrator: one InvokeHarness call
    made 3 model round-trips and 2 internal shell calls, ended in end_turn, and the old
    line logged `rounds=0`. That zero reads as "the agent did no work", which is the
    opposite of what happened, and it is why the latency question could not be measured
    from the logs at all.

    The measured turn spent latencyMs 6973 + 7764 + 6950 = 21.7s of 29.6s wall clock
    inside the model, so the round-trips are not a detail of the turn -- they ARE it.
    """
    tid = _mk_task(wired, status="thinking")

    class _AC:
        def invoke_harness(self, **kw):
            return _harness_loop_stream("Audited. Three findings.")

    monkeypatch.setattr(wired.console, "agentcore_chat", _AC())
    monkeypatch.setattr(wired.console, "_resolve_harness_id",
                        lambda x: "llmops_orchestrator-x")
    wired.console.run_task_turn(tid, accept=False)
    line = [ln for ln in capsys.readouterr().out.splitlines()
            if "[task-chat]" in ln and str(tid) in ln]
    assert line, "the per-turn log line is missing entirely"
    line = line[0]

    # The two counters must be REPORTED SEPARATELY. Renaming one to the other's name
    # would satisfy a test that only looked for the new field, and that rename is
    # exactly the wrong fix: both numbers are real, they just answer different
    # questions (what we serviced vs what the model actually cost).
    m_model = re.search(r"model_rounds=(\d+)", line)
    m_serv = re.search(r"serviced=(\d+)", line)
    assert m_model and m_serv, (
        f"the log must report the harness's own round-trips AND the ones we service, "
        f"as separate fields: {line!r}")
    assert int(m_model.group(1)) == 3, (
        f"3 assistant messages arrived, so 3 model round-trips happened; the log says "
        f"{m_model.group(1)}: {line!r}")
    # This is the whole defect in one assertion: the turn serviced NOTHING (it ended in
    # end_turn) and still cost 3 round-trips. If the two fields are wired to the same
    # counter, this fails.
    assert int(m_serv.group(1)) == 0, (
        f"this turn ended in end_turn and serviced no inline function, so serviced= "
        f"must be 0 -- a nonzero value means both fields read the same counter: {line!r}")
    # The tools the harness ran itself, by name: "2 internal calls" is not diagnosable,
    # "shell, shell" is.
    assert "'shell', 'shell'" in line or '"shell", "shell"' in line, (
        f"the harness's own tool calls must be named, or a slow turn cannot be "
        f"attributed to what it actually ran: {line!r}")
    # And the model's share of the wall clock, which is what #45 needs and what no log
    # line could previously answer.
    ms = re.search(r"model_ms=(\d+)", line)
    assert ms and int(ms.group(1)) == 6973 + 7764 + 6950, (
        f"model_ms must sum every metadata latencyMs in the turn: {line!r}")
    assert re.search(r"tok=\d+/\d+", line), (
        f"token usage is in the stream and is what a cost question needs: {line!r}")
    # `tool=` has the same naming defect one field over. `tool_use` is sticky across the
    # whole drain, so a turn that ended in end_turn still reported the last toolUse the
    # HARNESS ran itself -- the live line read `stop=end_turn tool=shell`, which is
    # indistinguishable from a call we failed to answer, and the servicing condition
    # below deliberately skips exactly those. Only a call we OWE belongs here.
    assert re.search(r"\btool=None\b", line), (
        f"this turn ended in end_turn, so no inline function is owed and tool= must be "
        f"None; naming a tool the harness already ran reads as an unserviced call: "
        f"{line!r}")


def test_the_per_turn_totals_accumulate_across_every_invoke_the_turn_makes(
        wired, monkeypatch, capsys):
    """A turn is often several InvokeHarness calls, and the totals must span all of them.

    The single-invoke test above cannot see this: with one invoke, `total += this_invoke`
    and `total = this_invoke` are indistinguishable, and a negative control that swapped
    one for the other passed. A turn that services an inline function makes at least two
    invokes, so it is the only place the accumulation is load-bearing.

    Two invokes, each carrying a harness-internal loop: 3 + 3 model round-trips, 2 + 2
    internal shell calls, and the model_ms of both. Reporting only the last invoke's
    figures would understate a slow turn by exactly the amount already spent.

    THREE invokes and TWO serviced checkpoints, not one of each, because a single
    serviced call cannot distinguish `tool_rounds += 1` from `tool_rounds = 1` -- a
    control that made exactly that substitution passed against an earlier version of
    this test. Every counter here needs at least two contributions to be load-bearing.
    """
    tid = _mk_task(wired, status="thinking")
    calls = {"n": 0}

    def _checkpoint_invoke(tag, ms):
        """A harness-internal loop that ends by asking US for a checkpoint."""
        evs = _harness_loop_stream("thinking...")["stream"][:-4]
        return _fake_stream(
            *evs,
            {"messageStart": {"role": "assistant"}},
            {"contentBlockStart": {"start": {"toolUse": {
                "toolUseId": tag, "name": "checkpoint"}}}},
            {"contentBlockDelta": {"delta": {"toolUse": {"input": "{}"}}}},
            {"messageStop": {"stopReason": "tool_use"}},
            {"metadata": {"usage": {"inputTokens": 100, "outputTokens": 10},
                          "metrics": {"latencyMs": ms}}})

    class _AC:
        def invoke_harness(self, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                return _checkpoint_invoke("cp1", 1000)
            if calls["n"] == 2:
                return _checkpoint_invoke("cp2", 2000)
            return _harness_loop_stream("Done.")

    monkeypatch.setattr(wired.console, "agentcore_chat", _AC())
    monkeypatch.setattr(wired.console, "_resolve_harness_id",
                        lambda x: "llmops_orchestrator-x")
    wired.console.run_task_turn(tid, accept=False)
    assert calls["n"] == 3, "this test needs a turn that invokes more than twice"

    lines = [ln for ln in capsys.readouterr().out.splitlines()
             if "[task-chat]" in ln and str(tid) in ln]
    assert len(lines) == 3, f"one log line per invoke expected, got {len(lines)}"
    last = lines[-1]
    # Each checkpoint invoke: 2 assistant messages from the truncated loop + 1 for the
    # checkpoint itself = 3. The final invoke: 3. The last line reports the TURN, so 9.
    m = re.search(r"model_rounds=(\d+)", last)
    assert m and int(m.group(1)) == 9, (
        f"model_rounds must span every invoke in the turn (3 + 3 + 3 = 9), not just the "
        f"last: {last!r}")
    ms = re.search(r"model_ms=(\d+)", last)
    # 6973 + 7764 + 1000, then + 2000, then the full 6973 + 7764 + 6950
    assert ms and int(ms.group(1)) == (6973 + 7764 + 1000) + (6973 + 7764 + 2000) \
        + (6973 + 7764 + 6950), \
        f"model_ms must span every invoke in the turn: {last!r}"
    # And the serviced counter must still be its own number, accumulated in its own
    # right: TWO checkpoints, which is what makes `+= 1` distinguishable from `= 1`.
    serv = re.search(r"serviced=(\d+)", last)
    assert serv and int(serv.group(1)) == 2, (
        f"two inline functions were serviced, so serviced=2: {last!r}")


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


def test_a_fresh_acceptance_dispatches_despite_a_dead_predecessor_run(wired, monkeypatch):
    """The idempotency guard's unit is the ACCEPTANCE RECORD, not the task.

    Found live: continuation #5's signature (a new approval record on the same
    consultation thread) was refused with already_dispatched because the dead
    continuation #4's run_id still sat on the task row. Keyed on run_id alone,
    the guard made every continuation signed in an existing thread
    undispatchable forever — the operator had to hand-clear the row in DynamoDB
    to honor a signature the human had already given."""
    tid = _mk_task(wired, status="accepting")
    # a prior dispatch left its run and its honored record on the row
    old = _signed_approval(wired.kms, b'{"goal":"old plan"}')
    new = _signed_approval(wired.kms, b'{"goal":"x"}')
    assert old["record_sha256"] != new["record_sha256"]
    wired.tasks.items[tid]["run_id"] = "run-dead-0004"
    wired.tasks.items[tid]["dispatched_record"] = old["record_sha256"]
    wired.tasks.items[tid]["approvals"] = [old, new]

    class _AC:
        def invoke_harness(self, **kw):
            if not wired.lam.invokes:
                return _tool_call_stream("launch_run", {"plan_uri": _PLAN_URI})
            return _text_stream("Dispatched.")

    monkeypatch.setattr(wired.console, "agentcore_chat", _AC())
    monkeypatch.setattr(wired.console, "_resolve_harness_id", lambda x: "llmops_orchestrator-x")
    wired.console.run_task_turn(tid, accept=True)
    starts = [i for i in wired.lam.invokes
              if i.get("InvocationType") == "RequestResponse"]
    assert len(starts) == 1, (
        "a fresh acceptance was refused because a dead predecessor's run_id "
        "still sat on the task row")
    assert wired.tasks.items[tid]["status"] == "dispatched"
    assert wired.tasks.items[tid]["dispatched_record"] == new["record_sha256"], (
        "the dispatch must record WHICH acceptance it honored, or the next "
        "continuation hits the same wall")


def test_a_pre_fix_row_with_no_dispatched_record_stays_blocked(wired, monkeypatch):
    """A row carrying a run_id but no dispatched_record predates the fix: it
    cannot prove the latest signature was never honored, so the guard stays
    conservative and an operator clears it deliberately. Guessing here is how a
    duplicate GPU run happens."""
    tid = _mk_task(wired, status="accepting")
    wired.tasks.items[tid]["run_id"] = "run-prefix-0001"
    wired.tasks.items[tid]["approvals"] = [
        _signed_approval(wired.kms, b'{"goal":"x"}')]
    replies = []

    class _AC:
        def invoke_harness(self, **kw):
            if len(replies) == 0:
                replies.append(1)
                return _tool_call_stream("launch_run", {"plan_uri": _PLAN_URI})
            return _text_stream("Understood.")

    monkeypatch.setattr(wired.console, "agentcore_chat", _AC())
    monkeypatch.setattr(wired.console, "_resolve_harness_id", lambda x: "llmops_orchestrator-x")
    wired.console.run_task_turn(tid, accept=True)
    starts = [i for i in wired.lam.invokes
              if i.get("InvocationType") == "RequestResponse"]
    assert not starts, "an ambiguous pre-fix row must not dispatch"


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


@pytest.mark.parametrize("accept", [True, False])
def test_a_guardrail_block_is_reported_like_a_content_filter_not_as_silence(
        wired, monkeypatch, accept):
    """Bedrock spells a suppressed turn two ways. Handling only `content_filtered`
    left `guardrail_intervened` to fall through to the generic paths, where it read
    as the agent's own behaviour: on an accept turn as "accepted plan was not
    dispatched by the agent" (after burning two dispatch re-asks), and on a consult
    turn as status=drafting with an EMPTY reply — a task that looks healthy and
    tells nobody anything. The operator needs the block, and needs it named: only
    `guardrail_intervened` points at the guardrail they attached and can change."""
    tid = _mk_task(wired, status="accepting" if accept else "drafting")
    if accept:
        wired.tasks.items[tid]["approvals"] = [
            _signed_approval(wired.kms, b'{"goal":"x"}')]
    calls = {"n": 0}

    class _AC:
        def invoke_harness(self, **kw):
            calls["n"] += 1
            return _fake_stream({"messageStop": {"stopReason": "guardrail_intervened"}})

    monkeypatch.setattr(wired.console, "agentcore_chat", _AC())
    monkeypatch.setattr(wired.console, "_resolve_harness_id", lambda x: "llmops_orchestrator-x")
    wired.console.run_task_turn(tid, accept=accept)
    t = wired.tasks.items[tid]
    err = str(t.get("error_msg", ""))
    assert t["status"] == "error", \
        f"a suppressed turn left status={t['status']!r}; nothing tells the operator"
    assert "guardrail_intervened" in err, err
    assert "content_filtered" not in err, \
        ("the message names a model-side filter for a guardrail block, sending the "
         f"operator to the wrong control: {err}")
    assert "was not dispatched by the agent" not in err, \
        "a platform block must not be recorded as the agent refusing to dispatch"
    assert calls["n"] == 1, "do not re-ask a blocked session — nothing can get out"


def test_both_blocked_stop_reasons_are_the_drivers(wired):
    """The worker and the harness driver must agree on what "the platform suppressed
    this turn" looks like — a spelling the driver knows and this worker does not is
    how a guardrail block became "the agent didn't dispatch"."""
    driver = (REPO / "orchestration/harness_driver/handler.py").read_text()
    for reason in wired.console._BLOCKED_STOP_REASONS:
        assert f'"{reason}"' in driver, \
            f"the worker treats {reason} as a platform block; the driver does not"
    assert set(wired.console._BLOCKED_STOP_REASONS) == {
        "content_filtered", "guardrail_intervened"}, \
        "Bedrock's suppressed-turn stop reasons changed; update the driver too"


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


def test_the_reply_is_shown_as_it_streams_and_never_leaks_a_half_sentence(
        wired, monkeypatch, capsys):
    """MEASURED, not assumed: the stream is incremental and we were the ones buffering.

    Probing the live orchestrator harness with a prose-only question: headers at 4.26s,
    first text delta at 8.40s, messageStop at 24.46s, and the 214 deltas spread over
    16.04s -- 65% of wall clock. So the first words existed at 8.4s while the customer
    waited 24.65s for the Lambda plus up to a 3s poll. _drain_chat collected deltas into
    a list nobody could see; nothing upstream was withholding them.

    Four things have to hold, and each has burned somewhere in this file before:

    1. Text is published DURING the turn, not only at the end.
    2. It is throttled -- 214 DynamoDB writes for one turn is throttling risk and cost.
    3. The draft NEVER enters `messages`. _replay_context feeds messages back to the
       agent next turn and an approval is signed against that record; a half-sentence in
       there is corruption, not a cosmetic bug.
    4. The draft is gone the moment the real message lands, in the SAME write -- two
       writes leave a window where a 3s poll sees both and the reply renders twice.
    """
    tid = _mk_task(wired, status="thinking")
    console = wired.console

    # Deltas that would each trigger a write if unthrottled.
    chunks = ["Where is ", "your data, ", "and is it ", "verifiable?"]

    class _AC:
        def invoke_harness(self, **kw):
            return _fake_stream(
                *[{"contentBlockDelta": {"delta": {"text": c}}} for c in chunks],
                {"messageStop": {"stopReason": "end_turn"}})

    monkeypatch.setattr(console, "agentcore_chat", _AC())
    monkeypatch.setattr(console, "_resolve_harness_id", lambda x: "llmops_orchestrator-x")
    # Force every flush to fire so the mid-turn writes are observable at all.
    monkeypatch.setattr(console, "_STREAM_FLUSH_S", 0.0)
    console.run_task_turn(tid, accept=False)

    partials = [str(u.get("ExpressionAttributeValues", {}).get(":p"))
                for u in wired.tasks.updates
                if ":p" in (u.get("ExpressionAttributeValues") or {})
                and "partial_reply" in u.get("UpdateExpression", "")]
    assert partials, ("the reply must be published while the turn runs; with no "
                      "mid-turn write the customer stares at 'thinking…' for the "
                      "whole turn even though the words already arrived")
    # Growing prefixes of the same reply, not independent fragments: the browser
    # replaces the draft each poll, so a fragment would make text disappear.
    assert partials == sorted(partials, key=len), f"draft must only grow: {partials}"
    assert partials[-1] == "".join(chunks)
    # Contract 3 checked at the write itself, not only at the resulting item. Asserting
    # only "no partial text ended up in messages" trusts the fake table to have modelled
    # whatever expression the sink used; a negative control that made the sink append to
    # `messages` still passed, because the fake ignored the malformed clause. The write
    # that carries the draft must not name `messages` at all.
    for u in wired.tasks.updates:
        if ":p" in (u.get("ExpressionAttributeValues") or {}):
            touched = u.get("UpdateExpression", "") + str(u.get(
                "ExpressionAttributeNames") or {})
            assert "messages" not in touched, (
                f"the streaming write must not touch messages: {u!r}")

    # Throttled -- but the FIRST delta must still publish immediately. Time-to-first-word
    # is the entire point of this change; making the customer wait _STREAM_FLUSH_S to see
    # anything would reintroduce a smaller version of the bug. Pin the interval absurdly
    # high: exactly one write may survive, and it must be the first words.
    before = len(wired.tasks.updates)
    monkeypatch.setattr(console, "_STREAM_FLUSH_S", 3600.0)
    console.run_task_turn(_mk_task(wired, status="thinking"), accept=False)
    throttled = [str(u["ExpressionAttributeValues"][":p"])
                 for u in wired.tasks.updates[before:]
                 if ":p" in (u.get("ExpressionAttributeValues") or {})]
    assert throttled == [chunks[0]], (
        f"_STREAM_FLUSH_S must gate every write after the first (got {throttled}); "
        f"one write per delta was 214 writes on a real measured turn, and gating the "
        f"first one too would hide the reply for {console._STREAM_FLUSH_S}s")

    # The committed record is clean: one whole assistant message, no draft left behind.
    item = wired.tasks.items[tid]
    assert "partial_reply" not in item, (
        "the draft must die in the same write that commits the message, or a poll "
        "catches both and the reply renders twice")
    assistant = [m for m in item["messages"] if m.get("role") == "assistant"]
    assert len(assistant) == 1 and assistant[0]["text"] == "".join(chunks)
    for m in item["messages"]:
        for p in partials[:-1]:
            assert m.get("text") != p, (
                f"a partial reached messages: {p!r} -- _replay_context feeds that back "
                f"to the agent and an approval is signed against this record")

    # The turn's log line must say how many flushes happened. Without it, "no
    # partial_reply was ever seen" is ambiguous between "the sink never fired" and "the
    # poll missed a short turn" -- and those need opposite fixes. That ambiguity cost a
    # live diagnosis, so it is a contract now.
    logged = capsys.readouterr().out
    assert re.search(r"\[task-chat\].*flushes=[1-9]", logged), (
        "the per-turn log line must report the flush count, or a missing draft is "
        f"undiagnosable: {logged[-300:]!r}")

    # A failed turn must not leave a draft advertising text the agent never finished.
    tid3 = _mk_task(wired, status="thinking")
    wired.tasks.items[tid3]["partial_reply"] = "half a sentence that never la"
    console._task_fail(tid3, "boom")
    assert "partial_reply" not in wired.tasks.items[tid3]

    # The frontend must render it, and must not render it as a committed message.
    # Comments are stripped first: this file EXPLAINS partial_reply in prose right above
    # the code that uses it, so a bare `"partial_reply" in html` stays true even if the
    # render is gutted to `const partial = ""`. A negative control caught that -- the
    # assertion was reading the comment, not the behaviour.
    html = (REPO / "deploy/console/frontend.html").read_text()
    code = "\n".join(ln for ln in html.splitlines() if not ln.lstrip().startswith("//"))
    assert "t.partial_reply" in code, \
        "the frontend must read the streaming draft off the task record"
    assert re.search(r"t\.partial_reply[\s\S]{0,400}?writing", code), \
        "the draft must be labelled as in-progress, not shown as a finished reply"


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


# ── the S3 audit copy: whole text, one object per append, never gating ─────────

def test_the_audit_copy_keeps_the_full_text_the_ddb_record_has_to_cap(audited):
    """The whole conversation is ONE DynamoDB item under a 400 KB ceiling, so the record
    the UI renders caps each message. The S3 copy exists precisely to be the uncapped one.

    Live evidence this was broken: one assistant reply sat at exactly 8000 characters in
    BOTH copies, because every caller applied `[:8000]` *before* the split -- so the
    "full-text audit copy" in the docstring was a truncated copy of a truncated record,
    and the message it lost was an assistant reply, the kind an acceptance is signed
    against."""
    console = audited.console
    tid = _mk_task(audited, status="thinking")
    long_text = "y" * (console.MSG_TEXT_MAX + 5000)
    audited.console.post_task_message(tid, {"text": long_text}, DS_USER)

    ddb = audited.tasks.items[tid]["messages"][-1]["text"]
    assert len(ddb) == console.MSG_TEXT_MAX, "the DynamoDB copy must still be capped"

    entries = _transcript_lines(audited, tid)
    assert entries, "nothing was written to the audit copy at all"
    assert entries[-1]["text"] == long_text, (
        f"the audit copy is truncated to {len(entries[-1]['text'])} chars; it is the "
        f"only place the full message survives")


def test_the_audit_copy_is_bounded_too(audited):
    """Uncapped in DynamoDB terms is not uncapped: one request must not be able to write
    an unbounded S3 object. TRANSCRIPT_TEXT_MAX is two orders of magnitude above the
    DynamoDB cap, so a real reply is never cut -- the ceiling exists, and is declared."""
    console = audited.console
    tid = _mk_task(audited, status="thinking")
    console.post_task_message(tid, {"text": "z" * (console.TRANSCRIPT_TEXT_MAX + 10)},
                              DS_USER)
    entries = _transcript_lines(audited, tid)
    assert len(entries[-1]["text"]) == console.TRANSCRIPT_TEXT_MAX


def test_a_failed_read_can_never_erase_the_audit_log(audited, monkeypatch):
    """The old append was a read-modify-write: get the whole file, concatenate, put it
    back -- with `except Exception: old = b""` on the read. So ANY read failure (a 503, a
    throttle, a slow-consistency miss) was treated as "no file yet" and the put REPLACED
    the entire history with just the newest lines. The audit log whose one job is to
    survive was one transient error away from erasure.

    One object per append removes the read entirely: there is nothing to fail into
    silence, and nothing to overwrite. Asserted by making every read raise -- history
    must still be there afterwards."""
    console = audited.console
    tid = _mk_task(audited, status="thinking")
    console.post_task_message(tid, {"text": "first, keep me"}, DS_USER)
    before = _transcript_lines(audited, tid)
    assert any("first, keep me" in str(e.get("text")) for e in before)

    def boom(**kw):
        raise RuntimeError("ServiceUnavailable")
    monkeypatch.setattr(audited.s3, "get_object", boom)

    # Back to a stale in-flight turn, so the second message clears the thinking lock
    # (STALE_TURN_MIN) rather than being refused with a 409.
    audited.tasks.items[tid].update({"status": "thinking",
                                     "updated_at": "2020-01-01T00:00:00+00:00"})
    r = console.post_task_message(tid, {"text": "second, also keep me"}, DS_USER)
    assert r.get("ok"), f"precondition: the second message was refused: {r}"
    after = [str(e.get("text")) for e in _transcript_lines(audited, tid)]
    assert any("first, keep me" in t for t in after), (
        "the earlier message is gone: an append re-read and rewrote the whole log")
    assert any("second, also keep me" in t for t in after)


def test_two_writers_do_not_cost_the_audit_copy_a_message(audited):
    """Two writers for one task are reachable: close_task is permitted while a turn is in
    flight, because 'thinking' is not in TASK_TERMINAL. Under read-modify-write the loser
    of that race silently drops its messages from the audit copy while DynamoDB's
    list_append keeps both -- an audit copy missing a message the audited record HAS.

    One object per append cannot lose a writer: distinct keys, no shared object."""
    console = audited.console
    tid = _mk_task(audited, status="thinking")
    console.post_task_message(tid, {"text": "the customer's message"}, DS_USER)
    audited.tasks.items[tid]["status"] = "thinking"
    console.close_task(tid, {"reason": "closed mid-turn"}, DS_USER)

    audit = [str(e.get("text")) for e in _transcript_lines(audited, tid)]
    ddb = [str(m.get("text")) for m in audited.tasks.items[tid]["messages"]]
    for t in ("the customer's message", "closed mid-turn"):
        assert any(t in x for x in ddb), f"precondition: {t!r} should be in the record"
        assert any(t in x for x in audit), (
            f"{t!r} is in the DynamoDB record but not in the audit copy")


def test_a_failed_audit_write_does_not_strand_a_signed_acceptance(wired, monkeypatch):
    """The audit copy is a SECOND channel and must never gate the first.

    It was called unwrapped at the end of _append_messages, so one S3 failure propagated
    out and skipped everything AFTER that call in the caller. In accept_task that is
    _task_event(PlanAccepted) and _enqueue_task_turn(accept=True): a KMS-signed
    acceptance would sit at 'accepting' with no worker ever launched, and the only escape
    is the 20-minute STALE_TURN_MIN hatch. Same shape as the SNS publish that gated the
    other three escalation channels."""
    console = wired.console
    monkeypatch.setattr(console, "_transcript_append",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("S3 down")))
    tid = _mk_task(wired)
    r = console.accept_task(tid, DS_USER)
    assert r.get("ok"), f"the acceptance itself failed on an audit write: {r}"
    assert any(k.get("InvocationType") == "Event" for k in wired.lam.invokes), (
        "no accept turn was enqueued: the signed acceptance is stranded at 'accepting'")
    assert "PlanAccepted" in [p.get("event_name") for p in wired.events.puts]


# ── canonicalization properties ───────────────────────────────────────────────

def test_canonical_json_is_order_insensitive():
    a = {"task_id": "t", "plan_uri": "u", "decision": "accepted"}
    b = {"decision": "accepted", "plan_uri": "u", "task_id": "t"}
    assert conductor_tools.canonical_json(a) == conductor_tools.canonical_json(b)


def test_the_signature_covers_every_field_of_the_record_a_human_actually_signs(wired):
    """SIGNED_KEYS is a hand-written tuple, and a key missing from it is invisible: the
    record still carries the field, the signature still verifies, and the field can be
    rewritten after signing with nothing to notice. Derived from a record the console
    actually produced rather than restated here, and asserted as an EQUALITY in both
    directions -- a subset check passes when a key is dropped from the tuple, which is the
    exact drift being guarded.
    """
    tid = _mk_task(wired, cost="50")
    wired.console.accept_task(tid, DS_USER)
    rec = wired.tasks.items[tid]["approvals"][-1]
    added_after_signing = {"record_sha256", "signature"}
    meaning = set(rec) - added_after_signing
    assert meaning == set(conductor_tools.SIGNED_KEYS), (
        f"uncovered by the signature: {sorted(meaning - set(conductor_tools.SIGNED_KEYS))}; "
        f"claimed but never in the record: "
        f"{sorted(set(conductor_tools.SIGNED_KEYS) - meaning)}")


def test_changing_any_field_the_signature_claims_to_cover_breaks_it(wired):
    """The other half: the tuple naming a key proves nothing about whether the digest
    depends on it. Every key is tampered in turn, so a canonicalization that silently
    skipped one -- or a digest taken over the wrong subset -- fails here rather than in a
    forged dispatch. The key list shrinking is caught by the equality test above; this test
    only proves each listed key is load-bearing.
    """
    tid = _mk_task(wired, cost="50")
    wired.console.accept_task(tid, DS_USER)
    rec = wired.tasks.items[tid]["approvals"][-1]
    assert conductor_tools.verify_record(wired.kms, rec) is True, "the fixture must verify"
    for key in conductor_tools.SIGNED_KEYS:
        tampered = json.loads(json.dumps(rec))
        tampered[key] = "tampered-after-signing"
        assert tampered[key] != rec.get(key), f"{key} was already the tampered value"
        assert conductor_tools.verify_record(wired.kms, tampered) is False, (
            f"{key} is named in SIGNED_KEYS and the signature does not depend on it")


def test_a_verifier_that_raises_is_a_no_and_not_an_error(wired):
    """The branch the forgiving double never reaches. Real KMS raises
    KMSInvalidSignatureException on a bad signature; FakeKms returns
    {"SignatureValid": False}, so every signature test in this file exercises the RETURN
    path and none of them the `except`. A double more permissive than production hides
    exactly the bugs production will have, so the lying double gets written too: whatever
    the verifier raises -- an invalid signature, a deleted key, an AccessDenied -- the
    answer is no.
    """
    tid = _mk_task(wired, cost="50")
    wired.console.accept_task(tid, DS_USER)
    rec = wired.tasks.items[tid]["approvals"][-1]

    class RaisingKms:
        def verify(self, **_kw):
            raise RuntimeError("KMSInvalidSignatureException")

    assert conductor_tools.verify_record(RaisingKms(), rec) is False, (
        "a verifier that raised was read as a valid signature")


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


def _asl_timeout(state):
    """The state's TimeoutSeconds, read from the ASL rather than written as a literal.

    Two tests below assert that a PARTIAL identity resolution still returns the ASL
    half of the hover card, and both pinned the literal 7200. That made them fail for
    the wrong reason the day DataPrepGenerate was raised to 86400: neither test is
    about the number, they are about the console not dropping the ASL config when the
    AgentCore lookup is what failed. A literal in a test whose subject is not that
    literal is a tripwire on the wrong wire -- and rewriting it to 86400 would just
    re-arm it for the next change.
    """
    return json.loads(
        (REPO / "orchestration/state_machine.asl.json").read_text()
    )["States"][state]["TimeoutSeconds"]


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

#: The live suffixes, as SSM and AgentCore actually report them (verified 2026-08-01
#: against get-parameters-by-path and list-agent-runtimes).
#:
#: The shapes matter and are NOT symmetric -- copied here verbatim because a fixture
#: that guesses them tests the guess, not the pipeline:
#:     agentRuntimeName = "harness_llmops_data_prep"             (no suffix; not unique)
#:     agentRuntimeId   = "harness_llmops_data_prep-D8SPwm7Kog"  (the real identity)
#:     SSM value        = "llmops_data_prep-KuSKXUaxyP"          (what the driver invokes)
#: The SSM and runtime suffixes differ in the real account, so they differ here too.
_LIVE_SSM_SUFFIX = {"llmops_data_prep": "KuSKXUaxyP", "llmops_finetune": "xXl7jsACZO",
                    "llmops_eval": "iuIIs96fFM", "llmops_deploy": "nLLNWairTc",
                    "llmops_orchestrator": "GsIqHZ4viJ", "llmops_monitor": "YCXC5hcXzu",
                    "llmops_finops": "eDJtU9PvKh"}
_LIVE_RT_SUFFIX = {"llmops_data_prep": "D8SPwm7Kog", "llmops_finetune": "toAA4REpAu",
                   "llmops_eval": "rjwDnkFGr2", "llmops_deploy": "6xUSao73Lp",
                   "llmops_orchestrator": "2sx6hzCapx", "llmops_monitor": "XsSfiw3c52",
                   "llmops_finops": "7X1rk8DHKz"}


def _stub_identity(console, monkeypatch, *, ssm_ok=True, rt_ok=True, fleet_ok=True,
                   rt_present=True):
    """Stub the three AWS reads harness_identity() makes, shaped like the live ones."""
    # Both caches, or a later denial path is served the earlier healthy answer and the
    # denial tests pass without exercising a denial. This is not test bookkeeping: the
    # same leak in a warm Lambda serves a 40-minute-old status as if it were live.
    console._HARNESS_ID_CACHE.clear()
    console._FLEET_WIDE_CACHE.clear()

    class _Ssm:
        def get_parameter(self, Name):
            if not ssm_ok:
                raise RuntimeError("AccessDenied: ssm:GetParameter")
            agent = Name.rsplit("/", 1)[-1]
            logical = "llmops_" + agent.replace("-", "_")
            return {"Parameter": {"Value": f"{logical}-{_LIVE_SSM_SUFFIX[logical]}"}}

    class _Ctl:
        def list_agent_runtimes(self, **kw):
            if not rt_ok:
                raise RuntimeError("AccessDenied: ListAgentRuntimes")
            if not rt_present:
                return {"agentRuntimes": [{"agentRuntimeName": "someone_elses_agent",
                                           "agentRuntimeId": "someone_elses_agent-zzz",
                                           "status": "READY"}]}
            return {"agentRuntimes": [
                {"agentRuntimeName": f"harness_{lg}",
                 "agentRuntimeId": f"harness_{lg}-{sfx}",
                 "agentRuntimeVersion": "8", "status": "READY"}
                for lg, sfx in _LIVE_RT_SUFFIX.items()]}

    monkeypatch.setattr(console, "ssm", _Ssm())
    monkeypatch.setattr(console, "ctl", _Ctl())

    def _fleet():
        if not fleet_ok:
            raise RuntimeError("AccessDenied: ListHarnesses")
        return [{"name": lg, "id": f"{lg}-{_LIVE_SSM_SUFFIX[lg]}", "status": "READY",
                 "version": "7", "model": "global.anthropic.claude-sonnet-5"}
                for lg in _LIVE_SSM_SUFFIX]
    monkeypatch.setattr(console, "list_fleet", _fleet)


def test_the_stage_hover_config_comes_from_the_deployed_definition(console, monkeypatch):
    """The card answers "which AgentCore runtime is behind this box, with what timeout".

    It is read from DescribeStateMachine, not hardcoded in the console: a second copy
    would answer for whatever the console was packaged with, and a stale answer here is
    undetectable by the operator asking the question.
    """
    console._STAGE_CFG_CACHE.clear()
    _stub_identity(console, monkeypatch)
    monkeypatch.setattr(console, "sfn", _fake_sfn_history(_ESCALATED_HISTORY, "FAILED"))
    out = console.pipeline_detail("run-asked")
    by = {s["key"]: s for s in out["stages"]}

    asl = json.loads((REPO / "orchestration/state_machine.asl.json").read_text())["States"]
    for state, key in console.STATE_TO_STAGE.items():
        pay = (asl[state].get("Parameters") or {}).get("Payload") or {}
        if not pay.get("harness_id"):
            continue
        hid = pay["harness_id"]
        cfg = by[key]["config"]
        assert cfg["harnessId"] == hid, f"{key}: wrong harness id"
        assert state in cfg["states"], f"{key}: {state} missing from states"
        if asl[state].get("TimeoutSeconds"):
            assert cfg["timeoutSeconds"] == asl[state]["TimeoutSeconds"]

        # The identity must be RESOLVED, not assembled. Note what is NOT asserted here:
        # `cfg["runtime"] == "harness_" + hid` is true either way, because that string
        # really is the runtime's display name -- which is exactly why the old test
        # passed while the console verified nothing. The suffixed id and the SSM harness
        # id are unforgeable by concatenation, so they are what this pins.
        assert cfg["harnessFullId"] == f"{hid}-{_LIVE_SSM_SUFFIX[hid]}"
        assert cfg["runtimeId"] == f"harness_{hid}-{_LIVE_RT_SUFFIX[hid]}"
        assert cfg["runtimeStatus"] == "READY", "runtime health is not reported"
        assert cfg["harnessStatus"] == "READY" and cfg["model"], "harness health missing"


def test_the_hover_card_says_unresolved_rather_than_inventing_a_runtime(console, monkeypatch):
    """No live runtime matches -> say so. The failure mode this replaces is worse than
    a blank: a synthesized name looks authoritative, so an operator chasing an outage
    searches the console for an ARN that does not exist and concludes their own eyes
    are wrong. Everything else on the card must still render."""
    console._STAGE_CFG_CACHE.clear()
    _stub_identity(console, monkeypatch, rt_present=False)
    monkeypatch.setattr(console, "sfn", _fake_sfn_history([], "SUCCEEDED"))
    cfg = console.stage_config()["data-prep-generate"]
    assert cfg["runtime"] == "unresolved", cfg.get("runtime")
    assert "runtimeId" not in cfg, "no id may be reported for a runtime we did not find"
    # still useful: the facts that DID resolve are all present
    assert cfg["harnessFullId"].startswith("llmops_data_prep-")
    assert cfg["timeoutSeconds"] == _asl_timeout("DataPrepGenerate") \
        and cfg["harnessStatus"] == "READY"


def test_a_cached_harness_status_expires_instead_of_being_served_forever(console, monkeypatch):
    """Health is a claim about NOW, so it must not be cached without a clock.

    The first cut of harness_identity() cached its result keyed only by harness id, with
    no expiry. Lambda containers live for tens of minutes, so a hover card would keep
    rendering "READY" for a harness that had since gone UPDATING or failed -- a stale
    read presented as a live one, which is the same lie as the concatenated runtime name
    this function was written to remove. Two things are pinned: that a status CHANGE
    becomes visible after the TTL, and that the TTL is short enough (<= one 30s poll of
    the flow diagram, i.e. <= 60s) to be worth calling live.
    """
    assert console._HARNESS_ID_TTL_S <= 60.0, "a status this stale is not a status"

    clock = {"t": 1_000.0}
    monkeypatch.setattr(console.time, "time", lambda: clock["t"])
    _stub_identity(console, monkeypatch)
    first = console.harness_identity("llmops_data_prep")
    assert first["harnessStatus"] == "READY"

    # The fleet now reports the harness as UPDATING. Same container, same cache.
    monkeypatch.setattr(console, "list_fleet",
                        lambda: [{"name": "llmops_data_prep", "id": "llmops_data_prep-x",
                                  "status": "UPDATING", "model": "m", "version": "9"}])
    clock["t"] += console._HARNESS_ID_TTL_S / 2.0
    assert console.harness_identity("llmops_data_prep")["harnessStatus"] == "READY", \
        "within the TTL the cache is expected to serve -- that is what it is for"
    clock["t"] += console._HARNESS_ID_TTL_S
    assert console.harness_identity("llmops_data_prep")["harnessStatus"] == "UPDATING", \
        "past the TTL the card is still reporting a status the fleet no longer has"


def test_the_card_does_not_list_the_whole_account_once_per_stage_box(console, monkeypatch):
    """ListAgentRuntimes and list_fleet answer for the ACCOUNT, so asking per box asks
    the same question four times and bills for four.

    list_fleet is the costly one -- ListHarnesses plus a GetHarness each, ~8 calls -- and
    the 9 boxes carry 4 distinct harness ids, so the naive version made ~40 calls where
    10 do. On the diagram's 30s poll that is a self-inflicted throttling risk on the
    operator's only live view of the pipeline.
    """
    calls = {"fleet": 0, "runtimes": 0}
    _stub_identity(console, monkeypatch)
    real_fleet, real_ctl = console.list_fleet, console.ctl

    monkeypatch.setattr(console, "list_fleet",
                        lambda: (calls.__setitem__("fleet", calls["fleet"] + 1),
                                 real_fleet())[1])

    class _Ctl:
        def list_agent_runtimes(self, **kw):
            calls["runtimes"] += 1
            return real_ctl.list_agent_runtimes(**kw)
    monkeypatch.setattr(console, "ctl", _Ctl())

    console._STAGE_CFG_CACHE.clear()
    monkeypatch.setattr(console, "sfn", _fake_sfn_history([], "SUCCEEDED"))
    cfgs = console.stage_config()
    distinct = {c["harnessId"] for c in cfgs.values()
                if isinstance(c, dict) and c.get("harnessId")}
    assert len(distinct) > 1, "fixture must span several harness ids or this proves nothing"
    assert calls["fleet"] == 1, f"list_fleet called {calls['fleet']}x for one render"
    assert calls["runtimes"] == 1, f"ListAgentRuntimes called {calls['runtimes']}x"


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

    # Same contract for the identity lookups, which are three MORE ways to fail: SSM,
    # ListAgentRuntimes and the fleet listing. Each is denied independently here
    # because a role can hold one grant and not the others, and any single denial
    # taking down the flow diagram would be the same bad trade as above.
    for kw, note in (({"ssm_ok": False}, "harnessIdError"),
                     ({"rt_ok": False}, "runtimeError"),
                     ({"fleet_ok": False}, "fleetError")):
        console._STAGE_CFG_CACHE.clear()
        _stub_identity(console, monkeypatch, **kw)
        monkeypatch.setattr(console, "sfn", _fake_sfn_history([], "SUCCEEDED"))
        out = console.pipeline_detail("run-x")
        assert len(out["stages"]) == len(console.STAGE_FLOW), f"{kw}: flow broke"
        cfg = console.stage_config()["data-prep-generate"]
        assert note in cfg, f"{kw}: the card must say why, not omit silently"
        # a denial on one lookup must not blank the others
        assert cfg["timeoutSeconds"] == _asl_timeout("DataPrepGenerate"), \
            f"{kw}: lost the ASL config too"


def test_every_field_the_hover_card_renders_is_supplied_by_the_api(console, monkeypatch):
    """stageTipRows() reads st.<field> and st.config.<field>. A field the API never
    sends renders as a silently-absent row, so the card quietly loses the very fact
    the operator hovered to find."""
    console._STAGE_CFG_CACHE.clear()
    _stub_identity(console, monkeypatch)
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

    # Union across stages, not one stage: a config key set from the ASL exists only on
    # the states whose ASL entry carries that field (retryPolicy, catchTargets), so
    # requiring every field on data-prep would force a fake key onto stages that
    # genuinely do not have one. `heartbeatSeconds` used to be the example here, and it
    # is the reason this union matters: the field was removed from the ASL on
    # 2026-08-03 and THIS test is what caught the card still reading `c.heartbeatSeconds`
    # with nothing left to produce it.
    every_cfg = set()
    for s in out["stages"]:
        every_cfg |= set(s["config"])
    # ...and across the DENIAL paths too, because the *Error notes only exist when a
    # lookup fails. Unioning real code paths rather than exempting those names keeps the
    # guard honest: a misspelled c.runtimeErorr in the card still fails here, which an
    # exemption list would have waved through.
    for kw in ({"ssm_ok": False}, {"rt_ok": False}, {"fleet_ok": False}):
        console._STAGE_CFG_CACHE.clear()
        _stub_identity(console, monkeypatch, **kw)
        for s in console.pipeline_detail("run-asked")["stages"]:
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


# ── the presigned data upload: the customer's only way to hand us a dataset ────
# Before this route the consult prompt opened every consultation by asking for "an S3
# URI under customer-data/", which the product had no way to help answer: the console's
# IAM could PutObject only into tasks/*, and the UI had no file input at all. The live
# customer-data/arc-demo/ files were uploaded by CLI, not by the product. So these tests
# guard a route whose whole purpose is to close that hole -- and the key-composition
# tests below are what make it safe to expose at all.

def test_upload_url_requires_a_console_group(wired):
    tid = _mk_task(wired, status="plan_proposed")
    r = wired.console.data_upload_url(
        {"task_id": tid, "filename": "train.jsonl", "content_length": 10}, NOBODY)
    assert r["status_code"] == 403
    assert not wired.s3.signed, "a refused caller must not get a URL signed for them"


def test_upload_url_signs_a_key_under_the_task_prefix(wired):
    tid = _mk_task(wired, status="plan_proposed")
    r = wired.console.data_upload_url(
        {"task_id": tid, "filename": "train.jsonl", "content_length": 4096}, DS_USER)
    assert r["ok"], r
    assert r["key"] == f"customer-data/{tid}/train.jsonl"
    assert r["uri"] == f"s3://test-bucket/customer-data/{tid}/train.jsonl"
    assert r["expires_in"] == wired.console.UPLOAD_URL_TTL_S
    call = wired.s3.signed[-1]
    assert call["method"] == "put_object"
    assert call["params"]["Key"] == r["key"]
    # ContentType is signed IN so the browser cannot store a dataset as text/html, and
    # SSE matches the bucket default rather than relying on it.
    assert call["params"]["ContentType"] == "application/x-ndjson"
    assert call["params"]["ServerSideEncryption"] == "AES256"


def test_an_approver_may_also_upload(wired):
    """Same bar as create_task: _user_may_task, not merely authenticated. The approver
    group is in that set, so an approver walking a customer through onboarding is not
    blocked by an authorization rule nobody intended."""
    tid = _mk_task(wired, status="plan_proposed")
    r = wired.console.data_upload_url(
        {"task_id": tid, "filename": "eval.csv", "content_length": 10}, APPROVER)
    assert r["ok"] and r["content_type"] == "text/csv"


@pytest.mark.parametrize("filename", [
    "../../runs/run-x/manifest.json",          # climb out of the prefix
    "../../../finops/cost_model.json",         # into the ledger's prefix
    "a/b/train.jsonl",                         # a nested path
    "/etc/passwd.txt",                         # absolute
    "C:\\Users\\me\\data\\set.jsonl",           # a Windows client's idea of a name
    "..%2f..%2fruns%2fx.json",                 # pre-encoded traversal
    "x" * 500 + ".jsonl",                      # a name longer than any key we want
])
def test_no_filename_can_escape_the_customer_data_prefix(wired, filename):
    """The test that makes this route safe to ship. The key is composed server-side from
    the task id and a sanitised name; nothing the client sent may reach it verbatim. Each
    input must either be refused or land under customer-data/<task_id>/ with no path
    separators left in the final segment.

    Negative-control result worth recording: removing the basename split ALONE, or the
    [A-Za-z0-9._-] whitelist ALONE, leaves this test green -- each guard is independently
    sufficient, which is the point of having both. Removing BOTH fails 5 of these 7 cases.
    So do not read a green run here as evidence that either guard is redundant; read it as
    the redundancy working. (Verified by patching both out, 2026-08-01.)
    """
    tid = _mk_task(wired, status="plan_proposed")
    r = wired.console.data_upload_url(
        {"task_id": tid, "filename": filename, "content_length": 10}, DS_USER)
    if r.get("ok"):
        prefix = f"customer-data/{tid}/"
        assert r["key"].startswith(prefix), f"{filename!r} escaped to {r['key']!r}"
        tail = r["key"][len(prefix):]
        assert "/" not in tail and "\\" not in tail and ".." not in tail, tail
        assert len(r["key"]) < 300, "an unbounded name makes an unbounded key"
        # and the SIGNED key must be the same one we reported
        assert wired.s3.signed[-1]["params"]["Key"] == r["key"]
    else:
        assert r["status_code"] == 400
        assert not wired.s3.signed, "a rejected name must not reach the signer"


def test_upload_url_refuses_an_extension_the_pipeline_cannot_read(wired):
    """Refused by extension rather than sniffed: this console never opens the bytes, so
    the extension is the only honest signal. .html matters most -- a bucket that also
    serves content plus an uploaded document is a stored-XSS shape."""
    tid = _mk_task(wired, status="plan_proposed")
    for name in ("payload.html", "logo.svg", "run.sh", "noextension"):
        r = wired.console.data_upload_url(
            {"task_id": tid, "filename": name, "content_length": 10}, DS_USER)
        assert r.get("status_code") == 400, name
    assert not wired.s3.signed


def test_upload_url_for_an_unknown_task_is_404_not_400(wired):
    """The upload is scoped to a consultation. An unknown task is not a complaint about a
    field -- there is nothing to attach the data to, and minting a URL anyway would write
    an orphan object into customer-data/ that no consultation ever reads."""
    r = wired.console.data_upload_url(
        {"task_id": "task-nope", "filename": "t.jsonl", "content_length": 10}, DS_USER)
    assert r["status_code"] == 404
    r2 = wired.console.data_upload_url({"filename": "t.jsonl", "content_length": 10}, DS_USER)
    assert r2["status_code"] == 404
    assert not wired.s3.signed


@pytest.mark.parametrize("status", ["dispatched", "closed", "error", "completed", "failed"])
def test_upload_url_is_refused_once_the_consultation_is_over(wired, status):
    """Both lifecycle tuples, deliberately. TASK_TERMINAL predates the state machine,
    which closes a task row out with completed/failed (TASK_SETTLED) -- a check that
    tested only TASK_TERMINAL would happily hand out a write URL for a finished
    consultation, and the object would land where no one is looking for it."""
    tid = _mk_task(wired, status=status)
    r = wired.console.data_upload_url(
        {"task_id": tid, "filename": "t.jsonl", "content_length": 10}, DS_USER)
    # .get, not [...]: when the guard is missing this returns a successful mint, and a
    # bare KeyError reads as a broken test rather than as the open write grant it is.
    assert r.get("status_code") == 409, \
        f"status {status!r} still got an upload URL minted: {r}"
    assert not wired.s3.signed


def test_the_settled_statuses_are_the_ones_the_state_machine_writes(console):
    """Guards the pair above against drift: if the state machine starts writing a third
    closing status, this fails rather than the upload route silently accepting it.
    Derived from the ASL, not from a hand-copied list."""
    asl = json.loads((REPO / "orchestration/state_machine.asl.json").read_text())
    written = set()
    for st in asl["States"].values():
        p = st.get("Parameters", {})
        if p.get("TableName") != "llmops-tasks":
            continue
        for name, val in p.get("ExpressionAttributeValues", {}).items():
            if f"#s = {name}" in p.get("UpdateExpression", ""):
                written.add(val["S"])
    assert written, "no llmops-tasks closer found -- did the state names change?"
    covered = set(console.TASK_TERMINAL) | set(console.TASK_SETTLED)
    missing = sorted(s for s in written if s not in covered)
    assert not missing, (
        f"the state machine closes a task with status {missing}, which is in neither "
        "TASK_TERMINAL nor TASK_SETTLED -- data_upload_url would hand out a write URL "
        "for a finished consultation and the object would land where nobody looks")


def test_upload_url_needs_a_real_size(wired):
    tid = _mk_task(wired, status="plan_proposed")
    for cl in (None, 0, "", "abc"):
        r = wired.console.data_upload_url(
            {"task_id": tid, "filename": "t.jsonl", "content_length": cl}, DS_USER)
        assert r.get("status_code") == 400, cl
    assert not wired.s3.signed


def test_upload_url_refuses_more_than_one_put_can_carry(wired):
    """5 GiB is S3's single-PUT ceiling. Declared with a number rather than left to fail
    at the END of a long upload, which is the worst possible time to learn it."""
    tid = _mk_task(wired, status="plan_proposed")
    r = wired.console.data_upload_url(
        {"task_id": tid, "filename": "big.parquet",
         "content_length": wired.console.UPLOAD_MAX_BYTES + 1}, DS_USER)
    assert r["status_code"] == 413
    assert not wired.s3.signed


def test_upload_url_is_short_lived(console):
    """A presigned PUT is a bearer write grant. It rides in browser history, proxy logs,
    and any error report the customer pastes to us, so its lifetime is the blast radius."""
    assert 0 < console.UPLOAD_URL_TTL_S <= 3600


def test_upload_url_leaves_an_audit_event(wired):
    tid = _mk_task(wired, status="plan_proposed")
    wired.console.data_upload_url(
        {"task_id": tid, "filename": "train.jsonl", "content_length": 4096}, DS_USER)
    ev = [p for p in wired.events.puts if p.get("event_name") == "DataUploadUrlIssued"]
    assert ev, ("an issued write grant that leaves no trace cannot be investigated later; "
                f"events were {[p.get('event_name') for p in wired.events.puts]}")
    detail = json.loads(ev[-1]["detail"])
    assert detail["actor"] == "alice"
    assert detail["key"] == f"customer-data/{tid}/train.jsonl"
    assert detail["bytes"] == "4096"


def test_a_signing_failure_is_a_502_not_a_broken_url(wired, monkeypatch):
    """If S3 cannot sign, the caller must learn that instead of receiving something
    URL-shaped that fails in the browser as an opaque CORS error."""
    def _boom(*a, **k):
        raise RuntimeError("kms/sts unavailable")
    monkeypatch.setattr(wired.s3, "generate_presigned_url", _boom)
    tid = _mk_task(wired, status="plan_proposed")
    r = wired.console.data_upload_url(
        {"task_id": tid, "filename": "t.jsonl", "content_length": 10}, DS_USER)
    assert r["status_code"] == 502 and "url" not in r


def test_the_upload_route_is_registered_inside_the_authenticated_post_block(console):
    """An upload-URL minter above the auth check is an unauthenticated write grant on the
    data bucket. Asserted structurally because the route list is long enough that a new
    entry can land on the wrong side of `if user is None` unnoticed."""
    src = (REPO / "deploy/console/lambda_function.py").read_text()
    assert '"/api/data-upload-url"' in src
    post_block = src.split('if user is None')[-1]
    assert "/api/data-upload-url" in post_block


# ── the CSP must permit the upload it is deployed alongside ────────────────────

def test_the_csp_names_the_upload_origin_so_our_own_header_does_not_block_it(wired):
    """connect-src 'self' alone blocks a browser PUT to S3, and the failure reads as a
    broken S3 permission rather than as our own header -- the single most expensive way
    this feature could fail. Scoped to the one bucket: 'https://*.s3.amazonaws.com'
    would authorise every bucket on earth as a fetch target from this page."""
    csp = wired.console._csp()
    assert "connect-src 'self' https://test-bucket.s3." in csp
    assert "*.s3." not in csp, "a wildcard S3 origin is not a scope"
    # the rest of the policy must survive the edit
    for d in ("default-src 'self'", "base-uri 'none'", "object-src 'none'",
              "frame-ancestors 'self'"):
        assert d in csp, d


def test_the_csp_is_built_per_response_not_frozen_at_import(console):
    """data_bucket() may resolve through SSM. A module-level constant means one transient
    cold-start failure bakes an upload-less CSP into that container for its whole life.
    Asserted by changing the bucket and re-reading the header, which a constant cannot
    reflect -- and by the header NOT being in _SEC_HEADERS, which is the shape that
    would silently reintroduce the freeze."""
    assert "content-security-policy" not in console._SEC_HEADERS
    import unittest.mock as _m
    with _m.patch.object(console, "data_bucket", lambda: "bucket-one"):
        first = console._resp(200, {})["headers"]["content-security-policy"]
    with _m.patch.object(console, "data_bucket", lambda: "bucket-two"):
        second = console._resp(200, {})["headers"]["content-security-policy"]
    assert "bucket-one" in first and "bucket-two" in second


def test_an_unresolvable_bucket_degrades_to_self_and_never_to_a_wildcard(console):
    """Uploads are broken either way if the bucket cannot be resolved; a wildcard would
    trade a real security boundary for nothing."""
    import unittest.mock as _m
    def _boom():
        raise RuntimeError("ssm unavailable")
    with _m.patch.object(console, "data_bucket", _boom):
        csp = console._csp()
    assert "connect-src 'self';" in csp and "s3." not in csp


# ── the infrastructure the upload silently depends on ─────────────────────────

def test_the_console_role_may_write_customer_data(console):
    """generate_presigned_url signs with the CALLER's credentials: a URL signed by a role
    without s3:PutObject on the key is minted happily and then 403s in the browser at the
    end of the upload. So the grant is part of the feature, not an afterthought."""
    pol = json.loads((REPO / "deploy/console/iam-policy.json").read_text())
    puts = [s for s in pol["Statement"]
            if "s3:PutObject" in (s["Action"] if isinstance(s["Action"], list) else [s["Action"]])]
    res = [r for s in puts for r in (s["Resource"] if isinstance(s["Resource"], list) else [s["Resource"]])]
    assert any("customer-data/*" in r for r in res), \
        f"no PutObject grant on customer-data/*; found {res}"
    # and it must stay scoped -- a bucket-wide PutObject lets the console overwrite
    # manifests and signed approval records under runs/.
    assert not any(r.rstrip("*").endswith(":::") or r.endswith(":::*") for r in res)


def test_the_bucket_is_configured_for_browser_uploads(console):
    """A presigned PUT from a page on another origin is a cross-origin request: without a
    CORS rule the browser fails at the preflight no matter how correct the URL is. The
    bucket had NO CORS configuration (NoSuchCORSConfiguration) and 03_storage.py had no
    step to add one, so this is asserted rather than assumed."""
    src = (REPO / "deploy/03_storage.py").read_text()
    assert "put_bucket_cors" in src, "ensure_bucket never configures CORS"
    block = src[src.index("put_bucket_cors") - 2000:src.index("put_bucket_cors") + 1200]
    assert '"PUT"' in block or "'PUT'" in block, "the CORS rule must allow PUT"
    assert "AllowedOrigins" in block
    assert '"*"' not in block.split("AllowedOrigins")[1][:200], \
        "AllowedOrigins '*' lets any page on the internet use a leaked presigned URL"


def test_the_pipeline_role_still_cannot_write_customer_data(console):
    """The console signs writes; the harness only reads. deploy/iam/harness_execution_role.json
    explains why: a pipeline that can rewrite the customer's data can destroy the held-out
    set its own gates are judged on. Adding an upload path must not have relaxed that."""
    doc = json.loads((REPO / "deploy/iam/harness_execution_role.json").read_text())
    # The file holds BOTH policies keyed separately (trustPolicy / permissionsPolicy);
    # deploy/01_iam.py applies each. Reading doc["Statement"] would KeyError, and a
    # `.get("Statement", [])` would silently iterate nothing and pass vacuously.
    pol = doc["permissionsPolicy"]
    for s in pol["Statement"]:
        acts = s["Action"] if isinstance(s["Action"], list) else [s["Action"]]
        res = s["Resource"] if isinstance(s["Resource"], list) else [s["Resource"]]
        writes = [a for a in acts if a.startswith("s3:Put") or a.startswith("s3:Delete")
                  or a == "s3:*"]
        if writes:
            assert not any("customer-data" in r for r in res), \
                f"{writes} granted on {res} -- customer data must stay read-only here"


# ── the CORS step, exercised rather than grepped ──────────────────────────────
# The guard above proves put_bucket_cors exists in the source; these prove it does the
# right thing. A structural assertion alone would pass on a step that wildcards its
# origin, skips silently, or allows DELETE.

@pytest.fixture(scope="module")
def storage():
    """deploy/03_storage.py loaded as a module. Its name starts with a digit so it is
    not importable normally; nothing at import time calls AWS."""
    spec = importlib.util.spec_from_file_location(
        "llmops_03_storage", REPO / "deploy/03_storage.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _CorsS3:
    def __init__(self):
        self.cors = None

    def put_bucket_cors(self, Bucket, CORSConfiguration):
        self.cors = {"bucket": Bucket, "config": CORSConfiguration}


def test_cors_allows_the_upload_and_nothing_more(storage):
    s3 = _CorsS3()
    note = storage.ensure_cors(s3, "b", "https://api.example.com", dry=False)
    rule = s3.cors["config"]["CORSRules"][0]
    assert rule["AllowedOrigins"] == ["https://api.example.com"]
    assert "PUT" in rule["AllowedMethods"]
    # No DELETE: nothing in the product asks a browser to remove a customer's dataset,
    # and a presigned URL is a bearer token -- a DELETE rule widens what a leaked one does.
    assert "DELETE" not in rule["AllowedMethods"]
    assert "POST" not in rule["AllowedMethods"]
    # the preflight must be allowed to ask for the headers the signature covers
    assert "content-type" in rule["AllowedHeaders"]
    assert "x-amz-server-side-encryption" in rule["AllowedHeaders"]
    assert "https://api.example.com" in note


def test_cors_is_skipped_and_reported_never_wildcarded(storage):
    """The whole point of a 15-minute presigned URL is that it is a narrow grant.
    AllowedOrigins '*' would let any page on the internet spend one it got hold of, so an
    unresolved origin must skip the rule and SAY so -- the deploy output is the only place
    a skip is visible, because the symptom is a browser failure with no server-side trace.
    """
    s3 = _CorsS3()
    note = storage.ensure_cors(s3, "b", "", dry=False)
    assert s3.cors is None, "no CORS rule may be written without a known origin"
    assert "skip" in note.lower() and "console-origin" in note


def test_cors_dry_run_writes_nothing(storage):
    s3 = _CorsS3()
    note = storage.ensure_cors(s3, "b", "https://api.example.com", dry=True)
    assert s3.cors is None and "would" in note


def test_console_origin_prefers_an_explicit_override_over_any_aws_call(storage):
    """--console-origin must work with no credentials at all: the offline dry-run path
    (--account-id, no STS) is the one people use to review a deploy, and an AWS call in
    that path turns a review into a failure."""
    assert storage.console_origin("us-east-1", dry=True,
                                  override="https://x.example.com/") == "https://x.example.com"


def test_console_origin_matches_the_api_the_console_deploy_actually_creates(storage):
    """deploy/console/deploy.sh names its HTTP API "$FN-api" with FN=llmops-admin. If
    these two spellings drift, CORS is silently skipped on every deploy and browser
    upload breaks with no error anywhere on the server side."""
    sh = (REPO / "deploy/console/deploy.sh").read_text()
    assert f"FN={storage.CONSOLE_FN}" in sh
    assert '--name "$FN-api"' in sh


def test_console_origin_is_empty_rather_than_wrong_when_the_api_is_absent(storage, monkeypatch):
    class _Api:
        def get_apis(self):
            return {"Items": [{"Name": "some-other-api", "ApiId": "zzz"}]}
    monkeypatch.setattr(storage.boto3, "client", lambda *a, **k: _Api())
    assert storage.console_origin("us-east-1", dry=False) == ""


def test_console_origin_resolves_the_real_api(storage, monkeypatch):
    # The id is deliberately a made-up one that reads as made up. This test used to hard-code
    # the account's actual API id, which is how the address of the live admin console became a
    # string this repo contains in six places -- the resolver's behaviour does not depend on
    # which id comes back, so nothing was bought by using the real one.
    class _Api:
        def get_apis(self):
            return {"Items": [{"Name": "unrelated", "ApiId": "aaa"},
                              {"Name": f"{storage.CONSOLE_FN}-api", "ApiId": "exampleapi1"}]}
    monkeypatch.setattr(storage.boto3, "client", lambda *a, **k: _Api())
    assert storage.console_origin("us-east-1", dry=False) == \
        "https://exampleapi1.execute-api.us-east-1.amazonaws.com"


def test_the_deploy_reports_cors_on_its_own_line(storage):
    """Folded into the `settings` string it would be one word in a sentence nobody reads.
    A skipped CORS step has no other visible symptom until a customer's upload fails."""
    src = (REPO / "deploy/03_storage.py").read_text()
    main = src[src.index("def main("):]
    assert '"cors": ensure_cors(' in main


# ── does anything actually scan the customer's data for PII? ───────────────────
# The readiness panel now links the Data Readiness Report, and that report's PII section
# is a HEURISTIC regex scan -- the data-prep prompt says so in as many words. The account
# looks like it has more than that: the Macie session is ENABLED and list_classification_jobs
# returns a COMPLETE job named `scan`. Live, that job is ONE_TIME, was created 2021-02-23,
# names 25 unrelated buckets, and processed 0 objects. So "Macie: ENABLED" plus "a job
# exists" is exactly the shape of coverage that isn't, and these tests pin the distinction:
# only the bucket list plus the scoping answers the question.

class _Macie:
    """A Macie double whose create_classification_job records the exact request."""

    def __init__(self, session="ENABLED", jobs=None):
        self._session, self._jobs, self.created = session, jobs or [], []

    def get_macie_session(self):
        if self._session is None:
            raise RuntimeError("AccessDeniedException: Macie is not enabled")
        return {"status": self._session}

    def list_classification_jobs(self):
        return {"items": self._jobs}

    def create_classification_job(self, **kw):
        self.created.append(kw)
        return {"jobId": "job-new"}


def _job(name, buckets, acct=ACCOUNT, jtype="SCHEDULED", status="RUNNING",
         scoping=None, criteria=None):
    defn = {"bucketDefinitions": [{"accountId": acct, "buckets": buckets}]}
    if scoping is not None:
        defn["scoping"] = scoping
    if criteria is not None:
        defn = {"bucketCriteria": criteria}
    return {"name": name, "jobId": f"id-{name}", "jobType": jtype,
            "jobStatus": status, "s3JobDefinition": defn}


def test_a_job_over_other_buckets_is_not_coverage(storage):
    """The live failure mode, reduced. The account's one job named 25 buckets, none ours,
    and every console-level signal (session ENABLED, job COMPLETE) read as healthy."""
    other = _job("scan", ["someone-elses-bucket", "another-one"])
    assert storage.macie_job_covers(other["s3JobDefinition"], "our-bucket",
                                    ACCOUNT) is False
    assert storage.macie_job_covers(other["s3JobDefinition"], "another-one",
                                    ACCOUNT) is True


def test_a_job_in_another_account_does_not_count(storage):
    """bucketDefinitions carries an accountId. A same-named bucket in a different account
    is a different bucket, and treating it as ours would report coverage we do not have."""
    defn = _job("x", ["our-bucket"], acct=OTHER_ACCOUNT)["s3JobDefinition"]
    assert storage.macie_job_covers(defn, "our-bucket", ACCOUNT) is False


def test_naming_the_bucket_is_not_enough_if_the_prefix_is_scoped_out(storage):
    """A job can name our bucket and still never read customer-data/ -- either by
    including only some other prefix or by excluding ours. Both are 'not covered'."""
    only_runs = _job("x", ["our-bucket"], scoping={"includes": {"and": [
        {"simpleScopeTerm": {"comparator": "STARTS_WITH", "key": "OBJECT_KEY",
                             "values": ["runs/"]}}]}})
    assert storage.macie_job_covers(only_runs["s3JobDefinition"], "our-bucket",
                                    ACCOUNT) is False
    excluded = _job("x", ["our-bucket"], scoping={"excludes": {"and": [
        {"simpleScopeTerm": {"comparator": "STARTS_WITH", "key": "OBJECT_KEY",
                             "values": ["customer-data/"]}}]}})
    assert storage.macie_job_covers(excluded["s3JobDefinition"], "our-bucket",
                                    ACCOUNT) is False
    ours = _job("x", ["our-bucket"], scoping={"includes": {"and": [
        {"simpleScopeTerm": {"comparator": "STARTS_WITH", "key": "OBJECT_KEY",
                             "values": ["customer-data/"]}}]}})
    assert storage.macie_job_covers(ours["s3JobDefinition"], "our-bucket",
                                    ACCOUNT) is True


def test_a_scope_filter_that_is_not_about_keys_does_not_decide_coverage(storage):
    """An includes-block scoped by OBJECT_EXTENSION says nothing about which prefix is
    read. Reading it as 'not customer-data/' would report a covered bucket as uncovered."""
    by_ext = _job("x", ["our-bucket"], scoping={"includes": {"and": [
        {"simpleScopeTerm": {"comparator": "EQ", "key": "OBJECT_EXTENSION",
                             "values": ["jsonl"]}}]}})
    assert storage.macie_job_covers(by_ext["s3JobDefinition"], "our-bucket",
                                    ACCOUNT) is True


def test_a_criteria_based_job_is_undecidable_not_covered_and_not_uncovered(storage):
    """bucketCriteria matches buckets by tag/attribute, so the definition alone cannot say
    whether ours is in scope -- and it could start matching later. None, not a guess."""
    crit = _job("x", [], criteria={"includes": {"and": [
        {"tagCriterion": {"tagValues": [{"key": "env", "value": "prod"}]}}]}})
    assert storage.macie_job_covers(crit["s3JobDefinition"], "our-bucket",
                                    ACCOUNT) is None


def test_the_deploy_reports_the_gap_loudly_when_nothing_scans(storage):
    """Silence here is the defect. Nothing else in the product would show it: the audit's
    own report is the only PII check, the readiness panel links it, and a customer reading
    a filled-in panel has every reason to think classification ran."""
    macie = _Macie(jobs=[_job("scan", ["someone-elses-bucket"])])
    res = storage.ensure_pii_scan(macie, "our-bucket", ACCOUNT, dry=False)
    assert "NO JOB SCANS customer-data/" in res["coverage"]
    assert "heuristic regex" in res["coverage"]
    assert "--enable-pii-scan" in res["coverage"]
    assert macie.created == [], "reporting a gap must not create billable work"


def test_a_gap_is_reported_even_though_the_session_says_enabled(storage):
    """The two facts must not be conflated in the output: session ENABLED sits right next
    to NO JOB SCANS, because the first is what made the second invisible for so long."""
    res = storage.ensure_pii_scan(_Macie(), "our-bucket", ACCOUNT, dry=False)
    assert res["session"] == "ENABLED"
    assert "NO JOB SCANS" in res["coverage"]


def test_nothing_is_created_without_the_flag_or_in_a_dry_run(storage):
    """A scheduled classification job is recurring paid work in someone's account. It is
    the one step here that is opt-in, and --dry-run must not create it either."""
    m1 = _Macie()
    storage.ensure_pii_scan(m1, "our-bucket", ACCOUNT, dry=False, enable=False)
    assert m1.created == []
    m2 = _Macie()
    res = storage.ensure_pii_scan(m2, "our-bucket", ACCOUNT, dry=True, enable=True)
    assert m2.created == [] and "would create" in res["coverage"]


def test_the_created_job_is_scoped_to_customer_data_only(storage):
    """Unscoped, the job would also read runs/, finops/ and models-mirror/ -- our own
    artifacts, billed per GB, none of them the customer's data."""
    macie = _Macie()
    res = storage.ensure_pii_scan(macie, "our-bucket", ACCOUNT,
                                  dry=False, enable=True)
    (req,) = macie.created
    defn = req["s3JobDefinition"]
    assert defn["bucketDefinitions"] == [{"accountId": ACCOUNT,
                                          "buckets": ["our-bucket"]}]
    terms = defn["scoping"]["includes"]["and"]
    assert terms == [{"simpleScopeTerm": {"comparator": "STARTS_WITH",
                                          "key": "OBJECT_KEY",
                                          "values": ["customer-data/"]}}]
    # RECOMMENDED, not ALL: all 166 managed identifiers include ones for regions and
    # document types this pipeline never sees, and each is a false positive to triage.
    assert req["managedDataIdentifierSelector"] == "RECOMMENDED"
    assert req["jobType"] == "SCHEDULED" and req["scheduleFrequency"] == {"dailySchedule": {}}
    assert req["initialRun"] is True, "a daily job that waits a day scans nothing today"
    assert res["job_id"] == "job-new"
    # And the job it creates must satisfy our own coverage predicate, or the next deploy
    # would report NO JOB SCANS against the job this one just made.
    assert storage.macie_job_covers(defn, "our-bucket", ACCOUNT) is True


def test_the_job_is_idempotent_by_name_because_create_is_not(storage):
    """CreateClassificationJob takes a clientToken and a repeat with a fresh token creates
    a SECOND job -- so a deploy that did not look for its own job first would add another
    paid scanner on every run."""
    existing = _job("llmops-customer-data-pii", ["our-bucket"],
                    scoping={"includes": {"and": [
                        {"simpleScopeTerm": {"comparator": "STARTS_WITH",
                                             "key": "OBJECT_KEY",
                                             "values": ["customer-data/"]}}]}})
    macie = _Macie(jobs=[existing])
    res = storage.ensure_pii_scan(macie, "our-bucket", ACCOUNT,
                                  dry=False, enable=True)
    assert macie.created == [], "a second scanner per deploy is the failure here"
    assert "job exists" in res["coverage"] and res["job_id"] == "id-llmops-customer-data-pii"
    src = (REPO / "deploy/03_storage.py").read_text()
    assert "clientToken=" in src, "create_classification_job requires a clientToken"


def test_a_wrongly_scoped_job_of_ours_says_so_instead_of_claiming_an_update(storage):
    """UpdateClassificationJob accepts only (jobId, jobStatus): a job's bucket list and
    scoping are IMMUTABLE. So unlike every other ensure_* here this cannot converge an
    existing job onto the right scope, and reporting 'exists' would be a false all-clear."""
    wrong = _job("llmops-customer-data-pii", ["our-bucket"],
                 scoping={"excludes": {"and": [
                     {"simpleScopeTerm": {"comparator": "STARTS_WITH",
                                          "key": "OBJECT_KEY",
                                          "values": ["customer-data/"]}}]}})
    res = storage.ensure_pii_scan(_Macie(jobs=[wrong]), "our-bucket", ACCOUNT,
                                  dry=False, enable=True)
    assert "does NOT cover" in res["coverage"]
    assert "immutable" in res["coverage"] and "cancel" in res["coverage"]


def test_someone_elses_job_that_does_cover_us_is_credited(storage):
    """Coverage is coverage even if we did not create it. Ignoring a third-party job would
    push an operator to pay for a duplicate scan of the same objects."""
    theirs = _job("security-baseline", ["our-bucket", "other"])
    res = storage.ensure_pii_scan(_Macie(jobs=[theirs]), "our-bucket", ACCOUNT,
                                  dry=False)
    assert res["also_covered_by"] == ["security-baseline (RUNNING)"]


def test_an_undecidable_job_is_never_counted_as_coverage(storage):
    """A bucketCriteria job is reported separately, NOT folded into 'covered' -- otherwise
    a tag-matching job nobody has checked would silence the gap warning."""
    crit = _job("tagged-scan", [], criteria={"includes": {"and": []}})
    res = storage.ensure_pii_scan(_Macie(jobs=[crit]), "our-bucket", ACCOUNT,
                                  dry=False)
    assert res["undecidable_jobs"] == ["tagged-scan"]
    assert "NO JOB SCANS" in res["coverage"], \
        "an unverified criteria job must not be treated as coverage"


def test_macie_being_unreachable_is_reported_not_swallowed(storage):
    """Macie is not enabled in every region, and this step must not break a deploy. But it
    must also not go quiet: 'unknown' plus the reason, never an implied all-clear."""
    res = storage.ensure_pii_scan(_Macie(session=None), "our-bucket", ACCOUNT,
                                  dry=False, enable=True)
    assert res["coverage"] == "unknown"
    assert "unavailable" in res["session"]
    assert "regex" in res["note"], "the report must name what the only real check is"


def test_the_offline_dry_run_path_needs_no_macie_client(storage):
    """--account-id with no credentials is the review path; safe_client returns None there
    and an AttributeError would turn a review into a crash."""
    res = storage.ensure_pii_scan(None, "our-bucket", ACCOUNT, dry=True)
    assert "unknown" in res["coverage"]


def test_the_scan_prefix_matches_where_uploads_actually_land(storage):
    """Two spellings of the same prefix: the console signs PUTs to CUSTOMER_DATA_PREFIX and
    the job pays to read this one. If they drift, the scan covers an empty prefix and still
    reports 'created' -- coverage of nothing, reported as coverage."""
    fn = (REPO / "deploy/console/lambda_function.py").read_text()
    assert f'CUSTOMER_DATA_PREFIX = "{storage.CUSTOMER_DATA_PREFIX}"' in fn
    role = json.loads((REPO / "deploy/iam/harness_execution_role.json").read_text())
    reads = [s for s in role["permissionsPolicy"]["Statement"]
             if s.get("Sid") == "S3CustomerDataReadOnly"]
    assert reads and f"/{storage.CUSTOMER_DATA_PREFIX}/*" in json.dumps(reads)


def test_the_audit_agent_can_actually_read_macie(storage):
    """A classification job whose findings the agent cannot read changes nothing a customer
    sees. The audit writes the Data Readiness Report, the readiness panel links it as the
    answer behind `PII disposition`, and simulate_principal_policy on
    llmops-harness-execution returned implicitDeny for ListFindings/GetFindings/
    GetFindingStatistics/DescribeClassificationJob -- nothing in this repo granted macie2 at
    all. So the scan would have run, cost money, and been invisible."""
    doc = json.loads((REPO / "deploy/iam/harness_execution_role.json").read_text())
    acts = set()
    for st in doc["permissionsPolicy"]["Statement"]:
        a = st["Action"] if isinstance(st["Action"], list) else [st["Action"]]
        acts |= {x for x in a if x.startswith("macie2:")}
    for need in ("macie2:ListFindings", "macie2:GetFindings",
                 "macie2:ListClassificationJobs", "macie2:DescribeClassificationJob"):
        assert need in acts, f"{need} not granted; the audit cannot cite classification"


def test_the_audit_agent_cannot_start_or_stop_a_scan(storage):
    """Read-only on purpose, in both directions. A per-GB billable job is the deploy's
    decision (--enable-pii-scan), not something an agent starts mid-turn; and an agent that
    could disable the session could switch off the check it is judged by."""
    doc = json.loads((REPO / "deploy/iam/harness_execution_role.json").read_text())
    for st in doc["permissionsPolicy"]["Statement"]:
        a = st["Action"] if isinstance(st["Action"], list) else [st["Action"]]
        for act in a:
            if not act.startswith("macie2:"):
                continue
            assert act.split(":", 1)[1].startswith(("Get", "List", "Describe")), \
                f"{act} is not a read: the agent must not create or disable a scan"


def test_the_audit_prompt_refuses_to_read_enabled_as_coverage(storage):
    """The grant is useless if the prompt never looks, and worse than useless if the agent
    reports 'Macie: ENABLED' as though that meant scanned -- the exact live trap. It must
    also state plainly when NO job covers the prefix, because that sentence is the only
    thing standing between a heuristic regex pass and a customer reading the panel as
    'someone classified my data'."""
    cfg = json.loads((REPO / "agents/data-prep/harness.json").read_text())
    prompt = cfg["systemPrompt"][0]["text"]
    assert "macie2 list-classification-jobs" in prompt
    assert "list-findings" in prompt
    assert "READ-ONLY" in prompt and "cannot start a job" in prompt
    assert "no Macie classification job covers this data" in prompt, \
        "the report must say so explicitly when nothing scanned the data"
    assert "ENABLED as coverage" in prompt or "session being ENABLED" in prompt
    # The heuristic disclaimer must survive alongside the new text, not be replaced by it.
    assert "not a compliance-grade scan" in prompt


def test_the_deploy_reports_pii_coverage_on_its_own_line(storage):
    """Same reason as CORS and skills: when nothing scans the customer's data, the deploy
    output is the ONLY place that gap is visible."""
    src = (REPO / "deploy/03_storage.py").read_text()
    main = src[src.index("def main("):]
    assert 'results["pii_scan"] = ensure_pii_scan(' in main


# ── the session: login, reload-survival, sign-out ──────────────────────────────
# Before this block the whole auth path had zero test coverage, which is how the
# refresh-logout bug shipped: the access token lived in one JS variable and a reload
# wiped it, so "signed in" lasted exactly as long as the page did. The fix is an
# httpOnly refresh cookie, and each of its properties (httpOnly, Secure, SameSite,
# narrow Path, cleared-on-failure, revoked-on-sign-out) is a separate assertion below —
# a cookie that survives a reload but is readable by script would be the bug traded for
# a worse one.

class _CognitoAuth:
    """Enough of cognito-idp to exercise both auth flows and revocation.

    Refresh tokens are tracked as a live set, so revocation is observable: a revoked
    token must make REFRESH_TOKEN_AUTH fail, which is the only thing that makes
    sign-out more than a cosmetic cookie delete.
    """

    class exceptions:
        class NotAuthorizedException(Exception):
            pass

    def __init__(self, password="pw-correct"):
        self.password = password
        self.live_refresh = set()
        self.revoked = []
        self.auth_calls = []
        self.n = 0

    def initiate_auth(self, ClientId, AuthFlow, AuthParameters):
        self.auth_calls.append((AuthFlow, dict(AuthParameters)))
        self.n += 1
        if AuthFlow == "USER_PASSWORD_AUTH":
            if AuthParameters.get("PASSWORD") != self.password:
                raise self.exceptions.NotAuthorizedException("bad password")
            rt = f"refresh-{self.n}"
            self.live_refresh.add(rt)
            return {"AuthenticationResult": {"AccessToken": f"access-{self.n}",
                                             "ExpiresIn": 28800, "RefreshToken": rt}}
        if AuthFlow == "REFRESH_TOKEN_AUTH":
            rt = AuthParameters.get("REFRESH_TOKEN", "")
            if rt not in self.live_refresh:
                raise self.exceptions.NotAuthorizedException("Refresh Token has been revoked")
            # Cognito returns NO new refresh token here (rotation off) — modelled, so a
            # test cannot accidentally depend on one appearing.
            return {"AuthenticationResult": {"AccessToken": f"access-{self.n}",
                                             "ExpiresIn": 28800}}
        raise AssertionError(f"unexpected auth flow {AuthFlow}")

    def revoke_token(self, Token, ClientId):
        self.revoked.append(Token)
        self.live_refresh.discard(Token)
        return {}


@pytest.fixture
def auth(console, monkeypatch):
    """The console with a working Cognito and the session routes reachable."""
    cog = _CognitoAuth()
    monkeypatch.setattr(console, "cognito", cog)
    monkeypatch.setattr(console, "COGNITO_CLIENT_ID", "client-test")
    monkeypatch.setattr(console, "COGNITO_POOL_ID", "us-east-1_test")
    monkeypatch.setattr(console, "data_bucket", lambda: "test-bucket")
    return types.SimpleNamespace(console=console, cog=cog)


def _post(console, path, body=None, cookies=None):
    """Drive handler() the way API Gateway does, payload format 2.0.

    Deliberately through handler() rather than the helpers: format 2.0 carries cookies in
    event["cookies"] (a list) and Set-Cookie in the response's "cookies" key, NOT in
    headers. A test that called cognito_refresh() directly would pass while the live
    route read a cookie that is never there.
    """
    ev = {"requestContext": {"http": {"method": "POST", "path": path,
                                      "sourceIp": "10.0.0.9"}},
          "headers": {}, "body": json.dumps(body or {})}
    if cookies is not None:
        ev["cookies"] = list(cookies)
    return console.handler(ev, None)


def _set_cookies(resp):
    return list(resp.get("cookies") or [])


def _cookie_named(resp, name):
    for c in _set_cookies(resp):
        if c.split("=", 1)[0] == name:
            return c
    return ""


def test_login_returns_a_token_and_sets_the_refresh_cookie(auth):
    r = _post(auth.console, "/api/login", {"username": "admin", "password": "pw-correct"})
    assert r["statusCode"] == 200
    body = json.loads(r["body"])
    assert body["accessToken"] == "access-1" and body["expiresIn"] == 28800
    c = _cookie_named(r, auth.console.REFRESH_COOKIE)
    assert c, f"login must set {auth.console.REFRESH_COOKIE}; got {_set_cookies(r)}"
    assert "refresh-1" in c


def test_the_refresh_token_never_reaches_the_response_body(auth):
    """The entire point of the cookie. A refresh token in the JSON body is readable by
    any script on the page, which would make the httpOnly flag decorative: an XSS bug
    would harvest 30 days of re-issue instead of one 8-hour access token."""
    r = _post(auth.console, "/api/login", {"username": "admin", "password": "pw-correct"})
    raw = r["body"]
    assert "refresh-1" not in raw, "the refresh token must not be in the response body"
    assert "refreshToken" not in json.loads(raw)


def test_the_refresh_cookie_is_unreachable_from_script_and_from_other_sites(auth):
    """Three flags, three distinct attacks, so three assertions rather than one regex.

    HttpOnly: script cannot read it (the XSS blast radius).
    Secure: it never rides a plaintext hop.
    SameSite=Strict: no other origin can make the browser attach it — the CSRF answer
    for a route whose entire input is a cookie.
    """
    r = _post(auth.console, "/api/login", {"username": "admin", "password": "pw-correct"})
    c = _cookie_named(r, auth.console.REFRESH_COOKIE)
    assert "HttpOnly" in c
    assert "Secure" in c
    assert "SameSite=Strict" in c


def test_the_refresh_cookie_is_scoped_to_the_route_that_consumes_it(auth):
    """Path=/api/refresh means the browser never attaches the 30-day credential to
    /api/tasks, /api/cost-approval or the dashboard HTML. A Path=/ cookie would ride
    along on every request for no benefit, widening where it can leak."""
    r = _post(auth.console, "/api/login", {"username": "admin", "password": "pw-correct"})
    c = _cookie_named(r, auth.console.REFRESH_COOKIE)
    assert f"Path={auth.console.REFRESH_COOKIE_PATH}" in c
    assert auth.console.REFRESH_COOKIE_PATH == "/api/refresh"
    # ...and the revoke route must path-match it, or sign-out arrives with no cookie to
    # revoke and silently degrades to a client-side forget.
    assert "/api/refresh/revoke".startswith(auth.console.REFRESH_COOKIE_PATH)


def test_a_failed_login_sets_no_cookie(auth):
    """A cookie holding "" would be a session by another name: /api/refresh would find
    the cookie present, call Cognito with an empty token, and answer 401 — turning a
    typo'd password into a doomed round-trip on every later page load."""
    r = _post(auth.console, "/api/login", {"username": "admin", "password": "wrong"})
    assert json.loads(r["body"])["error"] == "invalid username or password"
    assert _set_cookies(r) == []


def test_a_reload_restores_the_session_without_a_password(auth):
    """The bug, stated as a test: sign in, throw the page away, and come back with only
    the cookie. Before the fix there was nothing to come back with."""
    login = _post(auth.console, "/api/login", {"username": "admin", "password": "pw-correct"})
    cookie = _cookie_named(login, auth.console.REFRESH_COOKIE).split(";")[0]
    r = _post(auth.console, "/api/refresh", cookies=[cookie])
    assert r["statusCode"] == 200
    assert json.loads(r["body"])["accessToken"].startswith("access-")
    assert ("REFRESH_TOKEN_AUTH", {"REFRESH_TOKEN": "refresh-1"}) in auth.cog.auth_calls


def test_refresh_reads_the_cookie_from_where_api_gateway_puts_it(auth):
    """Payload format 2.0 delivers cookies in event["cookies"], never in headers.
    Reading headers["cookie"] would pass any hand-written test and find nothing live —
    and the symptom would be "the fix didn't work", with no error anywhere.

    Both halves use a token that is genuinely VALID, which is what makes this test able
    to fail. The first draft sent a bogus token and asserted 401 on both channels — but a
    header-reading implementation also answers 401 for a bogus token (it just finds
    nothing), so the test passed with the guard removed. Only a live token distinguishes
    "read it and Cognito said no" from "never read it at all".
    (Verified by patching the reader over to headers, 2026-08-01.)
    """
    login = _post(auth.console, "/api/login", {"username": "admin", "password": "pw-correct"})
    cookie = _cookie_named(login, auth.console.REFRESH_COOKIE).split(";")[0]
    assert "refresh-1" in cookie
    # the right channel: a valid token restores the session
    assert _post(auth.console, "/api/refresh", cookies=[cookie])["statusCode"] == 200
    # the wrong channel: the SAME valid token in a header must not be honoured
    ev = {"requestContext": {"http": {"method": "POST", "path": "/api/refresh"}},
          "headers": {"cookie": cookie}, "body": "{}"}
    r = auth.console.handler(ev, None)
    assert r["statusCode"] == 401 and json.loads(r["body"])["error"] == "no session"


def test_refresh_without_a_cookie_is_401_and_never_calls_cognito(auth):
    """A first-time visitor must not cost a Cognito round-trip on page load — the page
    calls this route unconditionally."""
    r = _post(auth.console, "/api/refresh")
    assert r["statusCode"] == 401
    assert json.loads(r["body"])["error"] == "no session"
    assert auth.cog.auth_calls == []


def test_a_rejected_refresh_cookie_is_cleared_so_it_cannot_fail_forever(auth):
    """A refresh token Cognito rejects will be rejected on every future reload. Leaving
    it makes each page load pay a doomed round-trip; clearing it means the next load
    goes straight to the sign-in prompt."""
    r = _post(auth.console, "/api/refresh", cookies=["llmops_rt=refresh-bogus"])
    assert r["statusCode"] == 401
    c = _cookie_named(r, auth.console.REFRESH_COOKIE)
    assert "Max-Age=0" in c, f"a rejected cookie must be expired; got {c!r}"
    assert f"Path={auth.console.REFRESH_COOKIE_PATH}" in c, \
        "cleared with a different Path, the browser keeps the original alongside it"


def test_sign_out_revokes_the_token_server_side_and_clears_the_cookie(auth):
    """Without RevokeToken, sign-out on a shared machine only hides the credential:
    the refresh token stays valid in Cognito for the rest of its 30 days."""
    login = _post(auth.console, "/api/login", {"username": "admin", "password": "pw-correct"})
    cookie = _cookie_named(login, auth.console.REFRESH_COOKIE).split(";")[0]
    r = _post(auth.console, "/api/refresh/revoke", cookies=[cookie])
    assert r["statusCode"] == 200 and json.loads(r["body"])["revoked"] is True
    assert auth.cog.revoked == ["refresh-1"]
    assert "Max-Age=0" in _cookie_named(r, auth.console.REFRESH_COOKIE)
    # ...and the revoked cookie no longer restores anything.
    assert _post(auth.console, "/api/refresh", cookies=[cookie])["statusCode"] == 401


def test_sign_out_still_clears_the_cookie_when_revocation_fails(auth, monkeypatch):
    """A sign-out that reports failure and leaves the browser signed in is worse than one
    that clears locally while an unreachable token runs out its window."""
    def _boom(**kw):
        raise RuntimeError("ThrottlingException")
    monkeypatch.setattr(auth.cog, "revoke_token", _boom)
    r = _post(auth.console, "/api/refresh/revoke", cookies=["llmops_rt=refresh-1"])
    assert r["statusCode"] == 200 and json.loads(r["body"])["revoked"] is False
    assert "Max-Age=0" in _cookie_named(r, auth.console.REFRESH_COOKIE)


def test_the_session_routes_are_the_only_unauthenticated_posts(console):
    """Structural, because this is the one place in the file where being above the auth
    check is CORRECT — which makes it the easiest place for a fourth route to be added
    above it by accident and become an unauthenticated write."""
    src = (REPO / "deploy/console/lambda_function.py").read_text()
    # Everything from the body parse to the POST auth check: the unauthenticated zone.
    # The END is anchored on the POST chokepoint itself, not on the first `if user is
    # None` — the consult-READ gate above it has one of those too, so splitting on the
    # first match truncated this zone to nothing. That raised rather than passing, but
    # only because the expected set is pinned; an empty zone with a subset assertion
    # would have read as "no unauthenticated POSTs at all".
    start = src.index('raw = event.get("body")')
    zone = src[start:src.index('if method == "POST":\n', start)]
    paths = set(re.findall(r'path == "(/api/[^"]+)"', zone))
    assert paths == {"/api/login", "/api/refresh", "/api/refresh/revoke"}, \
        f"unauthenticated POST routes drifted: {sorted(paths)}"


def test_a_non_session_post_is_unaffected_by_the_cookie(auth):
    """The cookie authenticates the session routes and nothing else. If a Bearer-less
    POST to /api/tasks started succeeding because a cookie was present, the cookie would
    have quietly become a second credential on every write route."""
    r = _post(auth.console, "/api/tasks", {"goal": "x"}, cookies=["llmops_rt=refresh-1"])
    assert r["statusCode"] == 401


class _CognitoGroups:
    """GetUser plus a per-user group list, so 401 and 403 are separable outcomes.

    A double that only answered "valid / invalid" could not tell the two apart, and the
    difference is the whole reason the read gate calls `_user_may_task` as well as
    `_authed_user`: a token proves who you are, not that you may read a customer's thread.
    """

    def __init__(self, groups=("llmops-datascience",)):
        self.groups = list(groups)

    def get_user(self, AccessToken):
        if AccessToken != "good-token":
            raise RuntimeError("NotAuthorizedException")
        return {"Username": "alice"}

    def admin_list_groups_for_user(self, **kw):
        return {"Groups": [{"GroupName": g} for g in self.groups]}


def _get(console, path, token=None):
    """Drive handler() with a GET, the way API Gateway does."""
    ev = {"requestContext": {"http": {"method": "GET", "path": path,
                                      "sourceIp": "10.0.0.9"}},
          "headers": {"authorization": f"Bearer {token}"} if token else {}}
    return console.handler(ev, None)


#: Every consult read, as (path, what an anonymous caller would have been handed).
CONSULT_READS = [
    ("/api/tasks", "every thread's goal, created_by, cost estimate and plan summary"),
    ("/api/tasks/task-1", "the whole DynamoDB item: the customer's transcript"),
    ("/api/tasks/task-1/approval", "approved_by, cognito_sub and source_ip"),
    ("/api/tasks/task-1/readiness", "the customer's data description from plan.json"),
]


@pytest.mark.parametrize("path,leaked", CONSULT_READS)
def test_an_anonymous_consult_read_is_refused(console, monkeypatch, path, leaked):
    """Driven through handler(), because a source-text guard cannot prove the 401 lands.

    All four of these returned 200 to a caller with no credentials at all, on a public API
    Gateway URL, for the platform's whole life -- the auth chokepoint was keyed on the HTTP
    METHOD (`if method == "POST"`), so the design property the docs boasted ("adding a
    route cannot accidentally add an unauthenticated write") was true and beside the point.
    """
    monkeypatch.setattr(console, "cognito", _CognitoGroups())
    monkeypatch.setattr(console, "COGNITO_POOL_ID", "us-east-1_test")
    r = _get(console, path)
    assert r["statusCode"] == 401, (
        f"GET {path} returned {r['statusCode']} to an anonymous caller, handing over "
        f"{leaked}")


@pytest.mark.parametrize("path,_leaked", CONSULT_READS)
def test_a_valid_token_outside_the_group_gets_403_not_401(console, monkeypatch, path,
                                                          _leaked):
    """Authentication is not authorisation, and the two codes must not be merged.

    An operator provisioned only to watch the Pipeline tab has a perfectly valid token.
    401 would send them round a re-login that cannot help; it also would not be true.
    """
    monkeypatch.setattr(console, "cognito", _CognitoGroups(groups=()))
    monkeypatch.setattr(console, "COGNITO_POOL_ID", "us-east-1_test")
    r = _get(console, path, token="good-token")
    assert r["statusCode"] == 403, (
        f"GET {path} answered {r['statusCode']} for a valid token with no consultation "
        "group membership; 403 is the honest code and the frontend routes it differently")


def test_the_operational_read_plane_stays_public(console, monkeypatch):
    """The fix must not gate the tabs an operator opens fifty times a day.

    Named individually rather than asserted as "everything else": a gate that crept onto
    the operational reads would be a regression in the opposite direction, and the reason
    those are public -- already-reconciled fact, all of it in the diagrams -- is a
    different argument from the one that protects a customer's conversation.
    """
    monkeypatch.setattr(console, "cognito", _CognitoGroups())
    monkeypatch.setattr(console, "COGNITO_POOL_ID", "us-east-1_test")
    for path in ("/api/overview", "/api/pipeline", "/api/observability",
                 "/api/cost-overview", "/api/evaluations"):
        r = _get(console, path)
        assert r["statusCode"] != 401, f"GET {path} is meant to be public, got 401"
        assert r["statusCode"] != 403, f"GET {path} is meant to be public, got 403"


def test_a_consult_read_with_a_good_token_and_group_is_served(console, monkeypatch):
    """The gate must let the legitimate reader through — otherwise the Tasks tab is dead.

    A 401/403-only suite would pass against a gate that refused everyone, which is the
    other way to make this feature "secure".
    """
    monkeypatch.setattr(console, "cognito", _CognitoGroups())
    monkeypatch.setattr(console, "COGNITO_POOL_ID", "us-east-1_test")
    r = _get(console, "/api/tasks", token="good-token")
    assert r["statusCode"] == 200, (
        f"a datascience-group member was refused the thread list ({r['statusCode']}); the "
        "gate is now denying the people it exists to admit")


def test_cookies_are_absent_from_responses_that_set_none(auth):
    """Format 2.0 accepts the key only as a list; emitting "cookies": [] or None on every
    response would add a field to all 40-odd routes to prove one route works."""
    r = _post(auth.console, "/api/tasks", {"goal": "x"})
    assert "cookies" not in r
    ok = auth.console._resp(200, {"a": 1})
    assert "cookies" not in ok


def test_set_cookie_rides_the_payload_2_0_cookies_list_not_a_header(auth):
    """Duplicated in headers AND cookies, a browser could apply the value twice; put
    only in headers, format 2.0 drops it when there is more than one. This asserts the
    single correct channel."""
    r = _post(auth.console, "/api/login", {"username": "admin", "password": "pw-correct"})
    assert isinstance(r.get("cookies"), list)
    hdrs = {k.lower() for k in r["headers"]}
    assert "set-cookie" not in hdrs


def test_login_without_cognito_configured_sets_no_cookie(console, monkeypatch):
    """Unconfigured must fail closed. The tuple return exists so this path cannot
    accidentally return a cookie built from a missing token."""
    monkeypatch.setattr(console, "COGNITO_CLIENT_ID", "")
    out, rt = console.cognito_login("admin", "pw")
    assert out == {"error": "Cognito not configured"} and rt == ""
    assert console.cognito_refresh("refresh-1")["error"] == "Cognito not configured"


def test_the_console_role_may_revoke_a_refresh_token(console):
    """boto3 signs RevokeToken with this role's credentials, so IAM authorizes it even
    though Cognito accepts unauthenticated callers. Without the grant, sign-out clears
    the cookie and silently fails to invalidate anything — the failure is a print in a
    log, not an error the user sees."""
    doc = json.loads((REPO / "deploy/console/iam-policy.json").read_text())
    st = next(s for s in doc["Statement"] if s.get("Sid") == "CognitoAuth")
    for action in ("cognito-idp:InitiateAuth", "cognito-idp:RevokeToken"):
        assert action in st["Action"], action


def test_the_deploy_reports_the_client_settings_the_session_depends_on(console):
    """ALLOW_REFRESH_TOKEN_AUTH missing = every reload forces a re-login, with no error
    anywhere. Token revocation off = sign-out cannot invalidate. Both are client-level,
    so the existing pool-level drift query cannot see them."""
    src = (REPO / "deploy/console/deploy.sh").read_text()
    assert "describe-user-pool-client" in src
    assert "ALLOW_REFRESH_TOKEN_AUTH" in src.split("describe-user-pool-client")[1]
    assert "EnableTokenRevocation" in src


# ── the browser half: what a reload actually does ─────────────────────────────

def test_the_page_restores_its_session_on_load(auth):
    """Without a call at load time the server half is dead code and the bug is unfixed.
    setAuthUi() alone renders "Sign in" and stops there.

    Asserted against COMMENT-STRIPPED source. The first draft searched the init block's
    raw text, which passed with the call deleted because the comment above it still said
    "restoreSession()" -- a test satisfied by prose describing the code it is meant to
    check. (Verified by deleting the call, 2026-08-01.)
    """
    front = (REPO / "deploy/console/frontend.html").read_text()
    assert "async function restoreSession()" in front
    assert '"/api/refresh"' in front
    code = "\n".join(ln for ln in front.splitlines() if not ln.lstrip().startswith("//"))
    init = code[code.rindex("setAuthUi();"):]
    assert "restoreSession()" in init, "restoreSession must be CALLED during page init"


def test_an_expired_access_token_does_not_throw_away_the_refresh_cookie(auth):
    """The subtlest way to reintroduce the bug: reuse signOut() for token expiry. The
    access token lasts 8 hours and the session 30 days, so treating expiry as sign-out
    would revoke a good session eight hours into it."""
    front = (REPO / "deploy/console/frontend.html").read_text()
    assert "function clearSession()" in front
    # signOut is the only thing allowed to hit the revoke route
    revoke_users = [ln for ln in front.splitlines() if "/api/refresh/revoke" in ln]
    assert len(revoke_users) == 1, revoke_users
    sign_out = front[front.index("function signOut()"):]
    sign_out = sign_out[:sign_out.index("\n}")]
    assert "/api/refresh/revoke" in sign_out
    # the 401 handler and the expiry check must both use clearSession, not signOut
    for marker in ("if (resp.status===401) { clearSession();",
                   "Date.now() > SESSION.exp) clearSession();"):
        assert marker in front, marker
    assert 'r.error==="unauthorized") { signOut()' not in front


def test_ensureToken_tries_the_cookie_before_prompting_for_a_password(auth):
    """A password prompt the cookie could have avoided is the user-visible symptom of
    this whole bug. Order matters: restoreSession must come before signIn."""
    front = (REPO / "deploy/console/frontend.html").read_text()
    body = front[front.index("async function ensureToken()"):]
    body = body[:body.index("\n}")]
    assert body.index("restoreSession()") < body.index("signIn()"), body


def test_the_session_fetches_send_the_cookie(auth):
    """fetch() omits cookies unless credentials is set. Without this the cookie is set
    by login and then never sent back — the feature silently does nothing."""
    front = (REPO / "deploy/console/frontend.html").read_text()
    for route in ("/api/login", "/api/refresh", "/api/refresh/revoke"):
        i = front.index('API+"' + route + '"')
        assert 'credentials:"same-origin"' in front[i:i + 200], route


def test_the_in_memory_token_comment_still_describes_the_truth(auth):
    """The comment at the top of the auth block was the diagnosis for this bug ("a
    reload costs one sign-in"). Left unchanged it would now be a false claim sitting
    directly above the code that contradicts it."""
    front = (REPO / "deploy/console/frontend.html").read_text()
    assert "A reload costs one sign-in" not in front
    assert "httpOnly refresh cookie" in front


# ── the single thread: one composer, one conversation ─────────────────────────
# The tab used to be four cards: a lifecycle diagram, a task list WITH ITS OWN
# textarea, a chat with its own input, and a plan card. Nothing on screen said the
# left box created a thread and the right box continued one, so two typing targets
# read as a bug. These guard the merge -- structurally, because "it looks right" is
# not something a test can see.

def _front():
    return (REPO / "deploy/console/frontend.html").read_text()


def _strip_comments(src):
    """Drop // lines and /* */ blocks.

    Every structural assertion below runs on this, because a guard that greps raw
    source is satisfiable by a COMMENT that mentions the thing -- that exact failure
    already happened once on this file (see
    test_the_page_restores_its_session_on_load), and the comments here deliberately
    name the very functions and ids the tests look for.
    """
    out, in_block = [], False
    for ln in src.splitlines():
        s = ln.strip()
        if in_block:
            if "*/" in s:
                in_block = False
            continue
        if s.startswith("/*"):
            in_block = "*/" not in s
            continue
        if s.startswith("//"):
            continue
        out.append(ln)
    return "\n".join(out)


def _js_fn_src(code, name):
    """Brace-match one named function out of already-comment-stripped frontend source.

    Separate from _js_fn (which reads the raw file and returns several bodies for node to
    execute): these are structural assertions about ONE function, and they must not be
    satisfiable by a neighbouring function that happens to contain the same statement.
    """
    i = code.index("function " + name + "(")
    depth, k = 0, code.index("{", i)
    while True:
        if code[k] == "{":
            depth += 1
        elif code[k] == "}":
            depth -= 1
            if depth == 0:
                return code[i:k + 1]
        k += 1


def test_the_tasks_tab_has_exactly_one_text_input_for_the_conversation():
    """The whole defect in one assertion. Two composers meant one of them was always
    the wrong place to type, and the customer could not tell which.

    Every <textarea>/<input> in the panel is collected, then the hidden file picker is
    subtracted BY NAME: a `type="file"` input is not a place to type -- it is opened by
    clicking the drop zone and has no caret -- but it is also the only exception this
    test will ever grant. A second textarea, or a text input, still fails, which is the
    failure that matters: that is the shape the defect came back as.
    """
    code = _strip_comments(_front())
    panel = code[code.index('data-tab-panel="tasks"'):code.index('data-tab-panel="architecture"')]
    fields = re.findall(r'<(textarea|input)([^>]*)id="([A-Za-z0-9_]+)"', panel)
    typing = [i for tag, attrs, i in fields
              if not (tag == "input" and 'type="file"' in attrs)]
    assert typing == ["taskMsg"], f"expected one composer, found {typing}"
    # And the exception is real: taskFile must actually BE a file input, so relabelling a
    # second composer `type="file"` to get past the line above does not work either.
    pickers = [i for tag, attrs, i in fields
               if tag == "input" and 'type="file"' in attrs]
    assert pickers == ["taskFile"], pickers


def test_the_tasks_tab_is_a_rail_plus_a_conversation():
    """A col-3 rail and a col-9 thread. Four cards down to two is what makes the
    conversation, not the machinery, the thing on screen."""
    code = _strip_comments(_front())
    cols = re.findall(r'data-tab-panel="tasks" class="card (col-\d+)"', code)
    assert cols == ["col-3", "col-9"], cols
    # the grid must actually define the spans, or both cards silently render full-width
    assert ".col-3 { grid-column:span 3; }" in code
    assert ".col-9 { grid-column:span 9; }" in code


def test_the_narrow_breakpoint_stacks_the_new_columns():
    """col-3/col-9 added to the grid but not to the media query is a rail squeezed to
    a quarter of a phone screen -- unreadable, and invisible on a desktop review."""
    code = _strip_comments(_front())
    mq = code[code.index("@media (max-width:900px)"):]
    mq = mq[:mq.index("}")]
    for c in (".col-3", ".col-9"):
        assert c in mq, f"{c} must stack at the narrow breakpoint: {mq}"


def test_the_one_composer_creates_when_no_thread_is_selected_and_replies_otherwise():
    """One box, two behaviours -- and the branch must be on TASK_SEL, because that is
    the only thing that distinguishes "new consultation" from "reply"."""
    code = _strip_comments(_front())
    body = code[code.index("async function composerSend()"):]
    body = body[:body.index("\n}")]
    assert "!TASK_SEL" in body and "createTask(" in body and "sendTaskMsg(" in body, body
    # createTask must take the goal as an argument: reading it back out of the DOM is
    # how the second textarea would grow back.
    assert "async function createTask(goal)" in code
    assert "async function sendTaskMsg(text)" in code


def test_the_composer_sends_on_enter_and_keeps_shift_enter_for_newlines():
    """A textarea that submits on any Enter cannot express a two-line requirement; one
    that never submits on Enter is not a chat."""
    code = _strip_comments(_front())
    i = code.index('id="taskMsg"')
    handler = code[i:i + 320]
    assert "event.key==='Enter'" in handler and "!event.shiftKey" in handler, handler
    assert "composerSend()" in handler
    assert "preventDefault()" in handler, "without it Enter also inserts a newline"


def test_the_plan_is_rendered_inside_the_thread():
    """The plan is an artifact of the conversation. In its own card its cost figure sat
    a scroll away from the turn that justified it."""
    code = _strip_comments(_front())
    assert 'id="taskPlanCard"' not in code, "the separate plan card must be gone"
    chat = code[code.index("function renderChat(t)"):]
    chat = chat[:chat.index("\n}")]
    assert "planArtifactHtml(t)" in chat, "the plan must be composed into the thread"
    art = code[code.index("function planArtifactHtml(t)"):]
    art = art[:art.index("\n}")]
    assert "m-artifact" in art
    assert 'onclick="acceptTask()"' in art, "signing belongs on the plan itself"


def test_the_plan_artifact_only_offers_signing_while_the_plan_is_signable():
    """A sign button on an already-dispatched plan is an action that can only fail."""
    code = _strip_comments(_front())
    art = code[code.index("function planArtifactHtml(t)"):]
    art = art[:art.index("\n}")]
    sign = art[art.index("const signBtn"):]
    sign = sign[:sign.index(";")]
    assert 'status==="plan_proposed"' in sign, sign


def test_the_lifecycle_strip_keeps_the_stage_mapping_and_the_run_link():
    """Collapsing the diagram must not drop the information the icons cannot carry:
    which run, why it failed, who holds the approval. That prose is the only place a
    closed task's reason lives."""
    code = _strip_comments(_front())
    flow = code[code.index("function renderTaskFlow(t)"):]
    flow = flow[:flow.index("\nfunction ")]
    assert "taskStageStates(t)" in flow, "the status->stage mapping must be reused, not rewritten"
    assert "taskFlowNote(t, st)" in flow, "the note must still be rendered"
    note = code[code.index("function taskFlowNote(t, st)"):]
    note = note[:note.index("\n}")]
    for marker in ("pending_approval", "loadRun(", "error_msg"):
        assert marker in note, marker


def test_the_thread_has_one_render_entry_point():
    """The old code called three renderers from two places, so the strip and the plan
    could disagree about which task they were showing."""
    code = _strip_comments(_front())
    assert "function renderThread(t)" in code
    for fn in ("renderTaskFlow(", "renderChat(", "renderTaskActions(", "renderChips("):
        callers = [ln for ln in code.splitlines() if fn in ln and "function " not in ln]
        assert len(callers) == 1, f"{fn} should be called only from renderThread: {callers}"


def test_no_native_dialog_survives_in_the_task_flow():
    """A native prompt() blocks the poll that keeps the thread live, cannot be styled,
    and throws away what was typed. alert() truncated a KMS approval record at 1800
    chars and could not be copied out -- for a signature that is the one thing needed.
    """
    code = _strip_comments(_front())
    for fn in ("acceptTask", "taskFeedback", "closeTask", "viewApproval"):
        body = code[code.index("function " + fn + "()"):]
        body = body[:body.index("\n}")]
        for bad in ("prompt(", "alert(", "confirm("):
            assert bad not in body, f"{fn} still uses {bad}"
        assert "askInThread(" in body or 'id="taskAsk"' in body or "taskAsk" in body, fn


def test_a_failed_in_thread_ask_keeps_what_was_typed():
    """The one thing prompt() could never do. If the widget tore itself down on every
    submit, a rejected close-reason would have to be retyped from memory."""
    code = _strip_comments(_front())
    body = code[code.index("async function askSubmit()"):]
    body = body[:body.index("\n}")]
    assert "=== true" in body, "only a true result may clear the widget"
    assert 'askMsg' in body, "a failure must be reported in place"


def test_signing_still_requires_a_deliberate_confirmation():
    """Replacing confirm() with an inline widget must not make signing a single click:
    this dispatches real spend against a KMS-signed acceptance."""
    code = _strip_comments(_front())
    body = code[code.index("async function acceptTask()"):]
    body = body[:body.index("\n}")]
    assert "ACCEPT" in body, "an explicit typed confirmation must remain"
    assert 'toUpperCase() !== "ACCEPT"' in body, body


def test_starter_chips_exist_and_only_show_on_a_new_thread():
    """An empty composer asks the customer to invent the shape of a request they have
    never made. Inside a conversation the orchestrator is already asking, so chips
    there would compete with its question."""
    code = _strip_comments(_front())
    assert "const TASK_CHIPS = [" in code
    chips = code[code.index("const TASK_CHIPS = ["):]
    assert chips[:chips.index("];")].count('",') + 1 >= 3, "at least three starters"
    body = code[code.index("function renderChips(t)"):]
    body = body[:body.index("\n}")]
    assert "if (t)" in body and 'innerHTML = ""' in body, body


def test_a_chip_fills_the_composer_rather_than_sending_it():
    """A chip that sends immediately spends a real orchestrator turn on text the
    customer never read, and they cannot edit the budget figure in it."""
    code = _strip_comments(_front())
    body = code[code.index("function useChip(i)"):]
    body = body[:body.index("\n}")]
    assert 'value = c' in body, body
    for bad in ("composerSend(", "createTask(", "postApi("):
        assert bad not in body, f"a chip must not {bad}"


def test_new_consultation_clears_the_selection_instead_of_creating_a_task():
    """A + button that POSTs would create an empty goalless task on every stray click."""
    code = _strip_comments(_front())
    body = code[code.index("function newThread()"):]
    body = body[:body.index("\n}")]
    assert "TASK_SEL = null" in body, body
    for bad in ("postApi(", "createTask("):
        assert bad not in body, f"newThread must not {bad}"


def test_the_rail_marks_which_thread_is_open():
    """Selecting a thread with no visible selection leaves the reader unable to tell
    which conversation the panel is showing."""
    code = _strip_comments(_front())
    body = code[code.index("async function loadTasks()"):]
    body = body[:body.index("\n}\n")]
    assert "t.id===TASK_SEL" in body and '" sel"' in body, body
    assert ".threads .th.sel" in code, "the selected class needs a style or it is invisible"


def test_selecting_a_thread_repaints_the_rail():
    """Without this the highlight lags a poll behind the panel, so for up to 15
    seconds the rail points at the previous conversation."""
    code = _strip_comments(_front())
    body = code[code.index("function selectTask(id)"):]
    body = body[:body.index("\n}")]
    assert "loadTasks()" in body and "loadTaskDetail()" in body, body


def test_thread_actions_are_offered_only_when_they_can_succeed():
    """Close on a closed task and "view approval" with no record are buttons whose only
    outcome is an error.

    Each button is matched together with the guard on its own line, not by asking
    whether the word "terminal" appears somewhere in the function. The first draft
    asserted the latter and stayed green with `if (!terminal)` deleted, because the
    `const terminal = [...]` declaration -- and the string "closed" inside it -- still
    satisfied it. A test that a variable was *declared* says nothing about it being
    *used*. (Verified by deleting the guard, 2026-08-01.)
    """
    code = _strip_comments(_front())
    body = code[code.index("function renderTaskActions(t)"):]
    body = body[:body.index("\n}")]
    for guard, action in ((r"if \(!terminal\)", "closeTask()"),
                          (r"if \(t\.approvals && t\.approvals\.length\)", "viewApproval()"),
                          (r'if \(PLAN_STATUSES\.includes\(s\)', "taskFeedback()")):
        hit = [ln for ln in body.splitlines() if re.search(guard, ln)]
        assert hit, f"{action} must be guarded by {guard}: {body}"
        # the guard and the button it protects must be the same statement, or the guard
        # is decorative and the button renders unconditionally anyway
        stmt = "\n".join(body.splitlines()[body.splitlines().index(hit[0]):][:2])
        assert action in stmt, f"{action} is not inside {guard}: {stmt}"


def test_every_task_handler_the_markup_names_is_defined():
    """The merge rewired every button on the tab. A typo'd handler is a button that
    does nothing at all, and the browser reports it only in the console."""
    front = _front()
    code = _strip_comments(front)
    panel = code[code.index('data-tab-panel="tasks"'):code.index('data-tab-panel="architecture"')]
    handlers = set(re.findall(r'on(?:click|keydown)="([A-Za-z0-9_]+)\(', panel))
    defined = set(re.findall(r'(?:async\s+)?function\s+([A-Za-z0-9_]+)', code))
    missing = sorted(h for h in handlers if h not in defined and h != "if")
    assert not missing, f"markup calls undefined functions: {missing}"


def test_no_task_code_addresses_an_element_the_merge_deleted():
    """Four cards became two. A $("taskFlow") left behind throws on every poll and
    silently stops the thread from updating."""
    code = _strip_comments(_front())
    gone = ("taskFlow", "taskGoal", "taskCreateMsg", "taskPlanCard", "taskPlanBody",
            "taskApprovalLink", "taskPlanMsg")
    for g in gone:
        assert f'$("{g}")' not in code, f'$("{g}") refers to a removed element'


def test_every_element_the_task_code_reads_actually_exists():
    """The general form of the above: any $("id") with no matching id= in the markup is
    a TypeError on the next poll."""
    front = _front()
    code = _strip_comments(front)
    used = set(re.findall(r'\$\("([A-Za-z0-9_]+)"\)', code))
    declared = set(re.findall(r'id="([A-Za-z0-9_]+)"', front))
    # ids created by innerHTML rather than static markup are still real elements
    declared |= set(re.findall(r"id=\\?[\"']([A-Za-z0-9_]+)\\?[\"']", code))
    missing = sorted(u for u in used if u not in declared)
    assert not missing, f"code reads elements that do not exist: {missing}"


# ── data readiness: what the plan says about the data, and what it does not ────
# Answered from plan.json rather than from the chat, because the plan is the artifact
# the customer signs: a fact stated in conversation and then absent from the plan is
# exactly the gap this panel exists to surface.

def _plan_with(data):
    return json.dumps({"goal": "x", "data": data}).encode()


def _prompt_data_block_keys():
    """The data-block keys the orchestrator's prompt actually names, parsed from it.

    DERIVED, not restated. The previous version of this test hand-copied seven paths and
    asserted the console contained them -- so it agreed with the console and with itself
    while the prompt specified NINE, and `datasheet.provenance` and `readiness_report_uri`
    were missing from the panel with every test green. A checklist guard that carries its
    own copy of the checklist cannot detect the drift it exists to detect: the same lesson
    as the documented-test-count guard, which is derived from pytest for exactly this
    reason. The prompt is the authority here because it is what the agent is told to write.
    """
    import re
    cfg = json.loads((REPO / "agents/orchestrator/harness.json").read_text())
    prompt = cfg["systemPrompt"][0]["text"]
    m = re.search(r'a "data" block \{(.*?)\}; and for any', prompt)
    assert m, "the consult protocol's data block is no longer in the prompt; re-anchor"
    spec, keys = m.group(1), []
    for nested in re.finditer(r'(\w+)\{([^}]*)\}', spec):
        keys += [f"{nested.group(1)}.{k.strip()}" for k in nested.group(2).split(",")]
    for flat in re.sub(r'\w+\{[^}]*\}', '', spec).split(","):
        if flat.strip():
            keys.append(flat.strip())
    return set(keys)


def test_readiness_names_every_field_the_consult_protocol_asks_for(wired):
    """The panel's checklist and the orchestrator's step-2 data block must be ONE list.

    If the console drops a field the agent is told to write, the customer sees a
    complete-looking readiness panel for an incomplete consultation and can sign with an
    open question invisible. That was live for `readiness_report_uri` -- the pointer to
    the Data Readiness Report, which is where the audit's PII scan lands. The panel showed
    "PII disposition" as answered from a claim in the plan while omitting the only link to
    the artifact that examined the data.
    """
    tid = _mk_task(wired, plan_body=_plan_with({}))
    r = wired.console.task_readiness(tid)
    paths = [f["field"] for f in r["fields"]]
    missing = _prompt_data_block_keys() - set(paths)
    assert not missing, (
        f"the orchestrator is told to write {sorted(missing)}, and the readiness panel "
        f"never asks about them: {paths}")
    assert r["total"] == len(paths) == len(wired.console.DATA_READINESS_FIELDS)
    assert len(paths) == len(set(paths)), f"a field is listed twice: {paths}"


def test_the_readiness_guard_is_derived_from_the_prompt(wired):
    """A guard on the guard above, because a restated checklist is how this got through.

    The test above is only worth anything if its expected list comes from the prompt. If
    someone replaces the derivation with a literal set, every assertion still passes and
    the panel can silently fall behind the prompt again -- which is exactly the state that
    hid `readiness_report_uri`. So assert the derivation itself: the parsed keys must
    include the nested datasheet paths and the report pointer, and must be more than the
    one field a hardcoded stub would likely name.
    """
    keys = _prompt_data_block_keys()
    assert "readiness_report_uri" in keys and "datasheet.provenance" in keys, (
        f"the data-block parse lost keys the prompt names: {sorted(keys)}")
    assert len(keys) >= 9, f"the prompt names 9 data-block keys; parsed {sorted(keys)}"
    assert all(k.startswith("datasheet.") for k in keys if "provenance" in k)
    # And the panel is measured against ALL of them, not a subset.
    tid = _mk_task(wired, plan_body=_plan_with({}))
    paths = {f["field"] for f in wired.console.task_readiness(tid)["fields"]}
    assert keys <= paths, f"not measured against the full spec: {sorted(keys - paths)}"


def test_the_readiness_docstring_states_the_real_number_of_questions(wired):
    """The prose count must be the tuple's count. It was "six" against a nine-item tuple.

    Both numbers describe DATA_READINESS_FIELDS, and only one of them was checked, so the
    unchecked one drifted -- the commit that grew the list to nine to match the consult
    prompt left the sentence at six. The panel was correct; the explanation of the panel
    was not, and the explanation is what a reader believes.

    Read out of the docstring by NUMBER WORD, not by substring anywhere in the module:
    "nine" appears in `test_the_readiness_guard_is_derived_from_the_prompt`'s own prose,
    so a module-wide search for the right word would pass on a comment. And the docstring
    must contain exactly one count word -- an edit that adds a second sentence with a
    different number is the same defect again, with both numbers present.
    """
    words = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six",
             7: "seven", 8: "eight", 9: "nine", 10: "ten", 11: "eleven", 12: "twelve"}
    n = len(wired.console.DATA_READINESS_FIELDS)
    assert n in words, f"{n} fields: extend the number-word map in this guard"
    doc = wired.console.task_readiness.__doc__ or ""
    assert doc, "task_readiness lost its docstring; this guard reads it"

    # Only the sentence that counts the questions, so the retrospective paragraph naming
    # the old numbers ("six", "seven") is not what gets asserted against.
    claim = [ln for ln in doc.splitlines() if "data questions" in ln]
    assert len(claim) == 1, (
        f"expected exactly one 'data questions' sentence to check, found {len(claim)}")
    sentence = claim[0].lower()

    found = [w for k, w in words.items() if re.search(rf"\b{w}\b", sentence)]
    assert found == [words[n]], (
        f"the docstring's count sentence says {found or 'no number'} while "
        f"DATA_READINESS_FIELDS holds {n} ({words[n]}): {claim[0].strip()!r}")

    # A digit is as wrong as the wrong word, and would slip past the word search above.
    digits = {int(d) for d in re.findall(r"\b(\d+)\b", sentence)}
    assert digits <= {n}, (
        f"the count sentence names {sorted(digits - {n})} but there are {n} fields")


def test_an_unanswered_field_says_so_and_says_why(wired):
    """A blank row reads as "fine". Every unanswered field comes back answered=False
    WITH the reason it matters, because the customer is being asked to go find the
    answer -- "license: (blank)" does not tell anyone that training on data they have
    no licence for is the risk."""
    tid = _mk_task(wired, plan_body=_plan_with({}))
    r = wired.console.task_readiness(tid)
    assert r["answered"] == 0
    for f in r["fields"]:
        assert f["answered"] is False
        assert f["value"] == "", "an unanswered field must not carry a value"
        assert f["why"].strip(), f"{f['field']} has no explanation of why it matters"
        assert f["label"].strip()


def test_readiness_reads_nested_datasheet_answers(wired):
    """Three of the seven fields live under plan.data.datasheet. A flat lookup would
    report a fully-documented dataset as having no licence, consent or PII answer --
    the panel would tell the customer to go re-answer what they already answered."""
    tid = _mk_task(wired, plan_body=_plan_with({
        "source_uri": "s3://test-bucket/customer-data/t/x.jsonl",
        "datasheet": {"license": "CC-BY-4.0", "pii_disposition": "redacted",
                      "consent": "granted 2026-07-01"}}))
    r = wired.console.task_readiness(tid)
    by = {f["field"]: f for f in r["fields"]}
    assert by["datasheet.license"]["answered"] and by["datasheet.license"]["value"] == "CC-BY-4.0"
    assert by["datasheet.pii_disposition"]["answered"]
    assert by["datasheet.consent"]["answered"]
    assert r["answered"] == 4
    # and the ones nobody answered are still counted as open
    assert by["decontamination"]["answered"] is False


def test_readiness_treats_an_empty_string_as_unanswered(wired):
    """An LLM writing the plan fills the shape it was given, so `"license": ""` and
    `"consent": {}` are the common way a field arrives unanswered. Truth-testing the
    KEY instead of the VALUE would count those as answered and show the customer a
    7/7 panel over a plan that answers nothing."""
    tid = _mk_task(wired, plan_body=_plan_with({
        "source_uri": "", "verification_method": [], "datasheet": {"license": {}},
        "decontamination": None}))
    r = wired.console.task_readiness(tid)
    assert r["answered"] == 0, [f for f in r["fields"] if f["answered"]]


def test_readiness_before_any_plan_exists_is_a_200_with_a_reason(wired):
    """A consultation two turns old has no plan.json yet. That is the NORMAL state, not
    an error: a 4xx here would make the panel vanish from the thread exactly when the
    customer most needs to see which questions are still open."""
    tid = _mk_task(wired, plan_body=b"{}")
    wired.tasks.items[tid]["plan_uri"] = ""
    r = wired.console.task_readiness(tid)
    assert "status_code" not in r
    assert "no plan yet" in r["note"]
    assert r["answered"] == 0 and len(r["fields"]) == r["total"]


def test_readiness_reports_an_unreadable_plan_instead_of_failing(wired):
    """"The plan could not be read" is itself a readiness answer. Raising would drop the
    whole panel and leave the customer with no signal at all."""
    tid = _mk_task(wired, plan_body=b"{}")
    del wired.s3.objects[f"test-bucket/tasks/{tid}/plan.json"]
    r = wired.console.task_readiness(tid)
    assert "status_code" not in r
    assert "could not be read" in r["note"]
    assert len(r["fields"]) == r["total"]


def test_readiness_survives_a_plan_whose_data_block_is_not_an_object(wired):
    """A model that writes `"data": "see above"` must not 500 the panel."""
    for bad in (b'{"data":"see above"}', b'{"data":[1,2]}', b'{"data":null}', b'[]'):
        tid = _mk_task(wired, plan_body=bad)
        r = wired.console.task_readiness(tid)
        assert "status_code" not in r, f"{bad!r} broke the panel: {r}"
        assert r["answered"] == 0


def test_readiness_for_an_unknown_task_is_404(wired):
    r = wired.console.task_readiness("task-nope")
    assert r["status_code"] == 404


def test_readiness_value_is_capped_so_one_field_cannot_flood_the_panel(wired):
    tid = _mk_task(wired, plan_body=_plan_with({"source_uri": "s3://" + "a" * 5000}))
    r = wired.console.task_readiness(tid)
    val = [f for f in r["fields"] if f["field"] == "source_uri"][0]["value"]
    assert len(val) <= 300


def test_the_readiness_route_is_registered(console):
    """A handler nothing routes to is dead code, and the panel would render the tab's
    own 404 HTML as though the plan were unreadable."""
    src = (REPO / "deploy/console/lambda_function.py").read_text()
    assert 'seg[1] == "readiness"' in src
    assert "task_readiness(seg[0])" in src


def test_dig_does_not_explode_on_a_scalar_midway_down_the_path(console):
    """`datasheet.license` where datasheet is the string "unknown" is a real shape from a
    real model. _dig must return None, not raise AttributeError."""
    assert console._dig({"datasheet": "unknown"}, "datasheet.license") is None
    assert console._dig({}, "a.b.c") is None
    assert console._dig({"a": {"b": {"c": 1}}}, "a.b.c") == 1


# ── the upload drop zone ──────────────────────────────────────────────────────

def test_the_thread_has_a_drop_zone_and_a_file_picker():
    """Before this the tab had NO file input at all (grep for `input type="file"`
    returned 0), while the consult prompt opened every conversation by asking for an S3
    URI under customer-data/. Someone with AWS credentials had to upload out of band."""
    front = _front()
    code = _strip_comments(front)
    panel = code[code.index('data-tab-panel="tasks"'):code.index('data-tab-panel="architecture"')]
    assert 'type="file"' in panel and 'id="taskFile"' in panel
    assert 'id="taskDrop"' in panel
    # drag-and-drop as well as the picker: a dataset is dragged out of a file manager
    for handler in ("ondragover", "ondragleave", "ondrop"):
        assert handler in panel, f"the drop zone has no {handler}"


def test_the_upload_sends_the_exact_content_type_the_server_signed():
    """ContentType and ServerSideEncryption are signed INTO the presigned URL. Sending
    the browser's own guess at content-type instead is a SignatureDoesNotMatch at the
    END of the upload -- the worst possible time -- and reads as a permissions problem.
    """
    code = _strip_comments(_front())
    body = code[code.index("async function uploadDataset"):]
    body = body[:body.index("\nfunction renderChat")]
    # The HEADER, not merely a mention of d.content_type anywhere in the function: the
    # first draft asserted the latter and stayed green when the header was switched to
    # `file.type`, because the auto-post message a few lines below still names
    # d.content_type. (Verified by making that exact swap, 2026-08-01.)
    hdr = re.search(r'\{"content-type":\s*([A-Za-z0-9_.]+)', body)
    assert hdr, f"no content-type header on the PUT: {body}"
    assert hdr.group(1) == "d.content_type", \
        (f"the PUT sends {hdr.group(1)}; ContentType is signed INTO the URL, so anything "
         "other than the value the route returned is a SignatureDoesNotMatch")
    assert '"x-amz-server-side-encryption": "AES256"' in body
    # and it must be the URL the server signed, not one composed here
    assert re.search(r"putWithProgress\(d\.url", body), \
        "the PUT must go to the URL the server signed"


def test_the_upload_announces_itself_in_the_conversation():
    """An upload the agent cannot see is a silent side-effect in a bucket. The auto-post
    is what turns it into a fact of the consultation -- and it goes through sendTaskMsg
    so the transcript shows who said it."""
    code = _strip_comments(_front())
    body = code[code.index("async function uploadDataset"):]
    body = body[:body.index("\nfunction renderChat")]
    assert "sendTaskMsg(" in body, "a successful upload must post into the thread"
    assert "data uploaded" in body and "d.uri" in body, \
        "the auto-post must name the s3:// URI the agent has to audit"
    # ordering: the announcement happens after the PUT succeeded, never before
    assert body.index("putWithProgress") < body.index("sendTaskMsg("), \
        "the thread must not be told about an upload that has not happened"


def test_a_failed_put_does_not_announce_an_upload_that_did_not_happen():
    """The failure branch must return before the auto-post. A thread claiming
    "data uploaded: s3://..." for a key that does not exist sends the data-prep audit
    after a nonexistent object, and the customer believes their data is in."""
    code = _strip_comments(_front())
    body = code[code.index("async function uploadDataset"):]
    body = body[:body.index("\nfunction renderChat")]
    fail = body[body.index("if (!put.ok)"):]
    fail = fail[:fail.index("\n")]
    assert "return" in fail, f"the failed-PUT branch must return: {fail}"


def test_the_drop_zone_is_hidden_when_there_is_nowhere_to_upload_to():
    """The key is customer-data/<task_id>/..., so with no consultation selected there is
    no prefix to write into; and the server answers 409 for a task that is dispatched,
    closed, completed, failed or errored. Offering the zone in those states is a click
    whose only possible outcome is an error."""
    code = _strip_comments(_front())
    body = code[code.index("function renderDrop(t)"):]
    body = body[:body.index("\n}")]
    assert "!!t" in body or "!t" in body, "the zone must depend on a task existing"
    assert "UPLOAD_CLOSED" in body and "display" in body


def test_the_frontends_closed_statuses_match_the_statuses_the_route_refuses(console):
    """The list is duplicated in the browser, so it can drift. If the server grows a new
    terminal status and this list does not, the tab offers an upload that 409s."""
    code = _strip_comments(_front())
    listed = re.search(r'const UPLOAD_CLOSED = \[([^\]]*)\]', code)
    assert listed, "UPLOAD_CLOSED not found"
    front_set = set(re.findall(r'"([a-z_]+)"', listed.group(1)))
    server_set = set(console.TASK_TERMINAL) | set(console.TASK_SETTLED)
    assert front_set == server_set, \
        (f"drift: the frontend hides the drop zone for {sorted(front_set)} but the route "
         f"refuses {sorted(server_set)}")


def test_an_upload_during_an_agent_turn_is_queued_not_lost(console):
    """The worst of the three defects the live runs found, because it lost data silently.

    data_upload_url deliberately allows an upload while a turn is in flight -- a 5 GiB PUT
    must not wait on a 60s agent turn. But post_task_message 409s during TASK_ACTIVE. So a
    file dropped mid-turn went to S3 and its announcement was refused, and uploadDataset
    then set "uploaded <key>" over the error: the customer read success, the agent was
    never told, and the next question was still "where is your data?".

    The asymmetry is intentional on both sides, so the client has to carry the note. This
    asserts the queue exists, that success is not claimed on a refusal, and that delivery
    is wired to the poll that knows when the turn ended."""
    # The asymmetry this whole mechanism exists for. If a later change makes the two
    # routes agree, the queue is dead code and this test should be revisited.
    assert set(console.TASK_ACTIVE) - (set(console.TASK_TERMINAL) | set(console.TASK_SETTLED)), \
        ("post_task_message no longer refuses any status data_upload_url allows -- the "
         "mid-turn upload race is gone and PENDING_POST may be removable")
    code = _strip_comments(_front())
    # Anchored on the DECLARATION, not on the name appearing anywhere: a bare
    # `"PENDING_POST" in code` is satisfied by the prefix of PENDING_POST_UNUSED, which is
    # how a control that renamed the slot into oblivion left this guard green.
    assert re.search(r"^(let|var|const)\s+PENDING_POST\s*=", code, re.M), \
        "no queue for an announcement refused mid-turn"

    up = _js_fn_src(code, "uploadDataset")
    # The refusal must be detected. Awaiting sendTaskMsg and ignoring what it returns is
    # exactly the bug: the error line is overwritten one statement later.
    assert re.search(r"(const|let|var)\s+\w+\s*=\s*await sendTaskMsg\(", up), \
        "uploadDataset must capture sendTaskMsg's result, not fire and forget"
    assert re.search(r"PENDING_POST\s*=\s*\{", up), \
        "a refused announcement must be queued in uploadDataset"
    # Success must be conditional. Anchored on the status-line report itself, not on the
    # word "uploaded": that word also appears in the announcement text ("data uploaded:
    # s3://..."), which sits EARLIER in the function -- so a laxer anchor matched the note
    # instead of the report and this assertion failed against correct code.
    report = '"uploaded " + d.key'
    assert report in up, f"the upload no longer reports {report}; re-check this guard"
    assert re.search(r"if\s*\(\s*\w+\.ok\s*\)", up[:up.index(report)]), \
        "'uploaded' is still reported unconditionally, including when the agent was not told"

    # sendTaskMsg has to report the outcome for any of the above to be possible.
    send = _js_fn_src(code, "sendTaskMsg")
    # EVERY exit must report an outcome, checked on the LAST return rather than on any:
    # the no-task-selected guard returns {ok:false} early, and that alone satisfied a
    # `return {ok:` search even with the success path returning undefined -- so the guard
    # stayed green while the caller could no longer tell a refusal from a send.
    returns = re.findall(r"return\s*([^;]*);", send)
    assert returns, "sendTaskMsg has no return at all"
    assert all(r.strip().startswith("{ok") or r.strip().startswith("{ ok")
               for r in returns), \
        (f"sendTaskMsg has a return that does not report an outcome: {returns} -- the "
         "upload auto-post cannot tell a refusal from a send")

    # ...and the queue needs a delivery point tied to the status the server gates on.
    flush = _js_fn_src(code, "flushPendingPost")
    assert re.search(r'"thinking"', flush) and re.search(r'"accepting"', flush), \
        "delivery must wait for the same statuses post_task_message refuses"
    assert re.search(r"PENDING_POST\s*=\s*null", flush), \
        "the slot must be cleared before the await, or the 3s poll double-posts"
    assert flush.index("PENDING_POST = null") < flush.index("await"), \
        "clearing after the await lets a re-entrant poll send the same note twice"
    assert "p.task_id" in flush and "t.id" in flush, \
        "the note must be matched to its own task, not posted into whichever is selected"
    detail = _js_fn_src(code, "loadTaskDetail")
    assert "flushPendingPost" in detail, \
        "nothing ever delivers the queued note: it is only reachable from the poll"


def test_the_upload_reports_progress_rather_than_appearing_to_hang():
    """fetch() cannot report upload progress. A 5 GiB PUT with no progress bar is
    indistinguishable from a freeze, and the customer retries on top of an upload that
    is already running -- which is why this uses XMLHttpRequest."""
    code = _strip_comments(_front())
    assert "XMLHttpRequest" in code
    # Anchored assignments, not substring hits: renaming the handler to `onprogressX`
    # (i.e. disabling it) left "upload.onprogress" matching as a prefix and this test
    # green. (Verified by that rename, 2026-08-01.)
    assert re.search(r"xhr\.upload\.onprogress\s*=", code), "no upload progress handler"
    assert re.search(r'\$\("taskUpFill"\)\.style\.width', code), \
        "nothing ever moves the progress bar"
    # every terminal outcome must be handled, or a failed PUT leaves the bar at 40%
    for h in ("onload", "onerror", "onabort"):
        assert re.search(r"xhr\." + h + r"\s*=", code), \
            f"xhr.{h} unhandled: a failed upload would look like a stalled one"


def test_a_cors_or_csp_failure_says_what_it_actually_is():
    """A blocked preflight arrives in JS as a zero-information error event. The bucket
    had no CORS configuration at all and the page's CSP is connect-src 'self', so this is
    the single most likely failure in production -- and "network error" would send the
    reader looking at IAM, which is the one thing that is fine."""
    code = _strip_comments(_front())
    err = code[code.index("xhr.onerror"):]
    err = err[:err.index("\n")]
    assert "CORS" in err and "connect-src" in err, \
        f"the browser-blocked case must name CORS/CSP: {err}"


def test_two_uploads_cannot_race_over_one_progress_bar():
    code = _strip_comments(_front())
    body = code[code.index("async function uploadDataset"):]
    body = body[:body.index("\nfunction renderChat")]
    assert "if (UPLOADING)" in body
    assert "finally" in body, "UPLOADING must be cleared even when the PUT throws"


def test_choosing_the_same_file_twice_still_fires():
    """An <input type=file> does not fire change when the same file is chosen again, so
    retrying a failed upload by re-picking the file would silently do nothing."""
    code = _strip_comments(_front())
    body = code[code.index("function pickFiles(ev)"):]
    body = body[:body.index("\n}")]
    assert "ev.target.value" in body


# ── agent choices as buttons (consult protocol step 3b) ───────────────────────

def test_the_choice_block_is_specified_in_the_harness_prompt():
    """The buttons are useless unless the agent knows to emit the block. And it must be
    told to keep the prose complete: a turn whose menu exists ONLY as json renders as an
    empty message on any client that does not parse it."""
    h = json.loads((REPO / "agents/orchestrator/harness.json").read_text())
    # The prompt text itself, not json.dumps of the file: dumps re-escapes every quote,
    # so '"choices"' would not be found in it and this guard would fail on a prompt that
    # is perfectly correct -- or worse, pass on a coincidence.
    prompt = "\n".join(b.get("text", "") for b in h["systemPrompt"])
    assert '{"choices"' in prompt, "the consult protocol never specifies the choices block"
    assert '"label"' in prompt and '"value"' in prompt, \
        "the block's shape must be stated, or the agent invents its own key names"
    assert "accelerator" in prompt, \
        "the prompt must say the block is an accelerator, not the message itself"
    assert "same fence as the plan trailer" in prompt, \
        "the prompt must forbid sharing the plan trailer's fence"
    # The gates rule belongs to PROPOSE. It sat at the end of step 3 and a careless
    # insertion of 3b swept it into the choices paragraph, where it reads as a rule about
    # buttons -- caught by re-reading the diff, and asserted here so it stays put.
    step3 = prompt[prompt.index("3. PROPOSE"):prompt.index("3b. CHOICES")]
    assert "Quality gates must be anchored" in step3, \
        "the held-out-set gate rule drifted out of step 3 PROPOSE"


def test_the_prompt_sends_the_customer_to_the_drop_zone_not_to_a_bucket(console):
    """Found by a live run, not by review.

    The prompt used to say "where is the data (S3 URI under customer-data/)". With the
    drop zone shipped, the agent still answered a real consultation with "I don't see a
    support-tickets prefix under customer-data/ yet -- please upload the JSONL to
    s3://.../customer-data/support-tickets/ and give me the exact URI". That is the
    out-of-band upload this whole feature removed, and it is worse than useless: the
    customer being consulted may hold no AWS credentials at all, and the invented prefix
    is not the one the console writes (customer-data/<task_id>/), so any plan built on it
    points at an object that will never exist.

    So the prompt must name the drop zone, must take the URI from the auto-post verbatim,
    and must not instruct the customer to upload anything themselves."""
    h = json.loads((REPO / "agents/orchestrator/harness.json").read_text())
    prompt = "\n".join(b.get("text", "") for b in h["systemPrompt"])
    step0 = prompt[prompt.index("0. DATA DISCOVERY"):prompt.index("1. GUIDED REQUIREMENTS")]
    assert "DROP ZONE" in step0.upper(), \
        "step 0 must tell the agent the thread has an upload drop zone"
    assert "verbatim" in step0.lower(), \
        "the agent must be told to take the uploaded URI verbatim, not to compose one"
    assert "never invent" in step0.lower(), \
        "the agent must be forbidden from inventing a customer-data/ path"
    # The key layout the agent is told about has to be the one the route actually writes,
    # or the prompt teaches a path that never receives an object.
    assert f"{console.CUSTOMER_DATA_PREFIX}/<task_id>/" in step0, \
        "the prompt must state the real key layout: customer-data/<task_id>/<filename>"
    assert "data uploaded:" in step0, \
        "the prompt must name the auto-post prefix it should read the URI out of"
    # ...and that prefix has to be the one the browser actually sends. The prompt telling
    # the agent to look for one string while the frontend posts another is a drift that
    # nothing else here would catch: both sides stay internally consistent and the agent
    # simply never recognises an upload.
    # Anchored inside uploadDataset rather than on the sendTaskMsg call site: the note is
    # now built into a variable first (so a refusal can queue it), and a guard tied to the
    # call site went stale the moment that refactor landed.
    up = _js_fn_src(_strip_comments(_front()), "uploadDataset")
    posted = re.search(r'=\s*"([^"]+)"\s*\+\s*d\.uri', up)
    assert posted, "the upload announcement was not found in uploadDataset"
    assert posted.group(1) == "data uploaded: ", \
        f"the frontend posts {posted.group(1)!r}, which the prompt does not describe"


def test_the_agent_is_handed_its_prices_instead_of_fetching_them(console, monkeypatch):
    """Found by tracing a slow turn, and it was two defects wearing one coat.

    The prompt said "read s3://<bucket>/finops/rates/rate_card_latest.json FIRST". That
    costs a whole model round-trip: the agent must answer with a tool call, the harness
    runs it, then the model is invoked AGAIN with the result. X-Ray on one 60.6s turn
    (trace 1-6a6d85b1-58ce7d6d0f1c9b177edeeb12) put 8.39s in the call that only decided
    to run a shell and 44.76s in the call that finally answered -- the round-trip IS the
    latency, and TTFT (51.5s) dwarfed generation (1.6s).

    And it did not even work. Traces 1-6a6d85d0-... and 1-6a6d85c5-... show the agent
    fetching litellm's model_prices_and_context_window.json from raw.githubusercontent.com
    instead of our card, so it paid for the round-trip AND quoted the customer a third
    party's list prices rather than what this account is actually billed.

    So: the console must PUT the rates in the invocation, and the prompt must price from
    there and be told not to go fetch prices. Both halves are asserted, plus the join
    between them -- a prompt naming params.rate_card while the console sends
    params.rates is two correct-looking halves that never meet."""
    prompt = "\n".join(b.get("text", "") for b in json.loads(
        (REPO / "agents/orchestrator/harness.json").read_text())["systemPrompt"])

    # -- the prompt half -------------------------------------------------------
    assert "params.rate_card" in prompt, \
        "the prompt must price from the injected card"
    assert "rate_card_latest.json" not in prompt, \
        "the prompt must no longer send the agent to S3 for prices -- that is the " \
        "round-trip this change removes"
    low = prompt.lower()
    assert "do not run a shell" in low or "do not fetch" in low, \
        "the prompt must forbid fetching prices, or the agent keeps doing what it did"
    assert "litellm" in low, \
        "name the wrong source it actually used; a generic 'use our rates' did not stop it"

    # -- the console half, and the join ----------------------------------------
    # Read the key the console really sets rather than trusting the comment: this is the
    # exact drift that makes both halves look right and never meet.
    src = _strip_comments((REPO / "deploy/console/lambda_function.py").read_text())
    turn = src[src.index("def run_task_turn("):]
    turn = turn[:turn.index("\ndef ")]
    m = re.search(r'params\[\s*"([^"]+)"\s*\]\s*=\s*card', turn)
    assert m, "run_task_turn must put the rate card into params"
    assert f"params.{m.group(1)}" in prompt, (
        f"the console sends params[{m.group(1)!r}] but the prompt reads "
        f"params.rate_card -- the halves do not meet")

    # The card must be built per turn from S3, not captured once at module import: a
    # snapshot would go stale the next time pricing_refresh publishes new rates.
    assert re.search(r"card\s*=\s*rate_card_for_prompt\(\)", turn), \
        "the card must be read inside the turn"

    # -- the shape the prompt promises is the shape that is sent ---------------
    doc = {"generated_at": "2026-07-31",
           "rate_precedence": ["ce_realized", "price_list", "fallback_static"],
           "rates": {"sagemaker:training:ml.g5.2xlarge": {
               "unit_price": 1.515, "unit": "hours", "source": "ce_realized",
               "realized_from": {"basis": "provenance the agent never quotes"}}}}
    monkeypatch.setattr(console, "_rate_card_doc", lambda: doc)
    out = console.rate_card_for_prompt()
    assert set(out) == {"generated_at", "rate_precedence", "rates"}
    entry = out["rates"]["sagemaker:training:ml.g5.2xlarge"]
    assert entry == {"unit_price": 1.515, "unit": "hours", "source": "ce_realized"}, \
        "only unit_price/unit/source belong in the prompt -- every injected byte is " \
        "billed on every turn, and realized_from is 8KB the agent never quotes"
    for field in ("unit_price", "unit", "source", "generated_at"):
        assert field in prompt, \
            f"the prompt must name {field}, or the agent cannot read what it is handed"

    # An unreadable card must be distinguishable from a free one. Returning {} here
    # would reach the agent as "rates exist and are empty" and invite an invented price.
    monkeypatch.setattr(console, "_rate_card_doc", lambda: None)
    assert console.rate_card_for_prompt() is None
    monkeypatch.setattr(console, "_rate_card_doc", lambda: {"rates": {}})
    assert console.rate_card_for_prompt() is None
    assert "if params.rate_card is absent" in prompt.lower(), \
        "the prompt must say what to do when no card arrives"


def test_choices_render_only_from_the_latest_agent_turn():
    """A menu from three turns ago answers a question that has already been answered;
    clicking it sends the consultation backwards."""
    code = _strip_comments(_front())
    body = code[code.index("function renderChoices(t)"):]
    body = body[:body.index("\n}")]
    assert "msgs[msgs.length-1]" in body, "choices must come from the last assistant turn"
    assert 'role==="assistant"' in body, "a customer's own message is not a menu"


def test_clicking_a_choice_sends_it_as_a_visible_message():
    """Sent through sendTaskMsg so it appears in the thread as something the customer
    said. A silent side-channel reply would leave the transcript lying about who chose."""
    code = _strip_comments(_front())
    body = code[code.index("function useChoice(i)"):]
    body = body[:body.index("\n}")]
    assert "sendTaskMsg(c.value)" in body


def test_the_choice_fence_is_not_shown_as_raw_json_in_the_chat():
    """Leaving the block in the bubble means the customer reads the same menu twice, the
    second time as unusable JSON."""
    code = _strip_comments(_front())
    body = code[code.index("function renderChat(t)"):]
    body = body[:body.index("\n  el.innerHTML")]
    assert "stripChoiceFences" in body


def test_the_composer_clears_itself_but_sendTaskMsg_does_not():
    """A choice button and the upload auto-post both call sendTaskMsg. If THAT cleared
    the composer, clicking a menu button would delete a half-written reply the customer
    was in the middle of typing."""
    code = _strip_comments(_front())
    send = code[code.index("async function sendTaskMsg(text)"):]
    send = send[:send.index("\n}")]
    assert '$("taskMsg").value = ""' not in send, \
        "sendTaskMsg must not clear the composer -- its other callers are not the composer"
    comp = code[code.index("async function composerSend()"):]
    comp = comp[:comp.index("\n}")]
    assert '$("taskMsg").value = ""' in comp or "createTask(text)" in comp


# ── the choice parser, EXECUTED rather than grepped ───────────────────────────
# Structural greps cannot tell a parser that works from one that throws on the first
# malformed block an LLM emits. These run the real functions out of frontend.html in
# node, so the assertions are about behaviour. Skipped (never silently passed) when
# node is unavailable: a skip is visible in the run, a vacuous pass is not.

def _js_fn(names):
    """Extract named top-level functions from frontend.html by brace matching."""
    js = _front()
    js = js[js.index("<script>"):js.rindex("</script>")]
    out = []
    for n in names:
        i = js.index("function " + n + "(")
        depth, k = 0, js.index("{", i)
        while True:
            if js[k] == "{":
                depth += 1
            elif js[k] == "}":
                depth -= 1
                if depth == 0:
                    break
            k += 1
        out.append(js[i:k + 1])
    return "\n".join(out)


def _run_js(body, call, arg):
    """Run `call` (which reads the string IN) against the real frontend functions.

    The input is handed over as a JSON literal rather than pasted into a JS string:
    every interesting test input here IS json, full of double quotes, and interpolating
    it produced a SyntaxError that failed 11 tests for a reason that had nothing to do
    with the code under test.

    Skipped, never silently passed, when node is missing: a skip is visible in the run.
    """
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    src = f"{body}\nconst IN = {json.dumps(arg)};\n"
    src += "process.stdout.write(JSON.stringify(" + call + "));"
    p = subprocess.run([node, "-e", src], capture_output=True, text=True, timeout=30)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


CHOICE_FNS = ("parseChoices", "stripChoiceFences")

PLAN_FENCE = ('```json\n{"plan_uri":"s3://b/k","plan_summary":"s",'
              '"cost_estimate_usd":"12"}\n```')
MENU_FENCE = '```json\n{"choices":[{"label":"Sign it","value":"accept"}]}\n```'


def test_a_well_formed_choices_block_becomes_buttons():
    turn = ('Three options.\n\n```json\n{"choices":[{"label":"Starter audit",'
            '"value":"I want the starter audit."},{"label":"Set menu","value":"set menu"}]}\n```')
    got = _run_js(_js_fn(CHOICE_FNS), "parseChoices(IN)", turn)
    assert got == [{"label": "Starter audit", "value": "I want the starter audit."},
                   {"label": "Set menu", "value": "set menu"}]


def test_the_plan_trailer_is_not_mistaken_for_a_menu():
    """Both are fenced json blocks. If the plan trailer parsed as choices, every priced
    proposal would sprout a nonsense button -- and the trailer would be stripped out of
    the chat, so the customer would lose the plan text as well."""
    body = _js_fn(CHOICE_FNS)
    turn = "Plan.\n" + PLAN_FENCE
    assert _run_js(body, "parseChoices(IN)", turn) == []
    assert "plan_uri" in _run_js(body, "stripChoiceFences(IN)", turn)


def test_a_turn_with_both_blocks_keeps_the_plan_and_lifts_the_choices():
    body = _js_fn(CHOICE_FNS)
    turn = "Plan.\n" + PLAN_FENCE + "\n\n" + MENU_FENCE
    assert _run_js(body, "parseChoices(IN)", turn) == [{"label": "Sign it", "value": "accept"}]
    shown = _run_js(body, "stripChoiceFences(IN)", turn)
    assert "plan_uri" in shown, "stripping the choices must not remove the plan trailer"
    assert "Sign it" not in shown, "the choices json must not also be shown as text"


@pytest.mark.parametrize("turn,why", [
    ("just prose, no fence at all", "a turn without the block renders as ordinary chat"),
    ('```json\n{not valid json,,\n```', "unparseable json must not throw"),
    ('```json\n{"choices":"starter,set-menu"}\n```', "choices must be an array"),
    ('```json\n{"choices":[{"label":1,"value":"x"}]}\n```', "a non-string label"),
    ('```json\n{"choices":[{"label":"only a label"}]}\n```', "a missing value"),
    ('```json\n{"choices":[{"label":"  ","value":"x"}]}\n```', "a blank label"),
    ('```json\n{"choices":[null]}\n```', "a null entry"),
    ('```json\n{"choices":[]}\n```', "an empty list"),
    ('```json\n[]\n```', "an array where an object was specified"),
    ('a ```json\n{"choices":[{"label":"L","value":"V"}', "a truncated turn"),
])
def test_the_choices_block_degrades_silently(turn, why):
    """Step 3b calls the block an accelerator: an agent that ignores it, or emits it
    wrongly, must not produce a broken tab. Every malformed shape ends as "no buttons"
    or as well-formed buttons -- never as an exception in the customer's face."""
    got = _run_js(_js_fn(CHOICE_FNS), "parseChoices(IN)", turn)
    assert isinstance(got, list), why
    for c in got:
        assert isinstance(c.get("label"), str) and c.get("label").strip(), why
        assert isinstance(c.get("value"), str) and c.get("value").strip(), why


def test_stripping_never_invents_a_closing_fence():
    """A truncated turn ends mid-fence. Appending the "```" that closes it would put
    characters in the transcript the agent never wrote."""
    body = _js_fn(CHOICE_FNS)
    for turn in ("a ```x", 'p ```json\n{"choices":[{"label":"L","value":"V"}]}'):
        assert _run_js(body, "stripChoiceFences(IN)", turn) == turn


def test_a_code_block_in_an_answer_is_left_alone():
    """The orchestrator explains things with code. Stripping every fence would eat it."""
    turn = "Run this:\n```bash\naws s3 ls\n```\nthen tell me."
    assert _run_js(_js_fn(CHOICE_FNS), "stripChoiceFences(IN)", turn) == turn


def test_a_long_label_or_value_is_capped_not_dropped():
    """A model can emit a paragraph as a label. Truncating keeps the button usable;
    dropping it would silently lose an option the customer was offered in the prose."""
    turn = ('```json\n{"choices":[{"label":"' + "L" * 400 + '","value":"' + "V" * 5000
            + '"}]}\n```')
    got = _run_js(_js_fn(CHOICE_FNS), "parseChoices(IN)", turn)
    assert len(got) == 1 and len(got[0]["label"]) <= 90 and len(got[0]["value"]) <= 2000


# ── the readiness panel in the browser ────────────────────────────────────────

def test_the_readiness_panel_exists_and_is_fed_by_the_route():
    code = _strip_comments(_front())
    assert 'id="taskReadiness"' in code
    assert "/readiness" in code, "the panel must consume GET /api/tasks/{id}/readiness"


def test_the_panel_spells_out_an_unanswered_field():
    """A blank cell reads as "fine", and the customer signs a plan whose data questions
    are still open. The words are asserted because the words are the feature."""
    code = _strip_comments(_front())
    body = code[code.index("function renderReadiness(t)"):]
    body = body[:body.index("\n}")]
    assert "not answered yet" in body
    assert "f.answered" in body, "the panel must branch on the server's answered flag"
    assert "f.why" in body, "the reason a field matters must reach the customer"


def test_the_panel_does_not_refetch_the_plan_on_every_poll():
    """The thread polls every 3s while a turn is in flight. Re-reading plan.json from S3
    on each one is a GET per 3 seconds per open tab, for a file that changes once per
    proposal."""
    code = _strip_comments(_front())
    body = code[code.index("async function loadReadiness(t)"):]
    body = body[:body.index("\n}")]
    # The early return, matched as one statement. The first draft asked only whether
    # "READY_KEY" and "return" both appeared somewhere in the function -- satisfied by
    # the assignment `READY_KEY = key` plus the unrelated early return for `!t`, so
    # deleting the cache check entirely kept it green. (Verified 2026-08-01.)
    guard = [ln for ln in body.splitlines() if re.search(r"if \(key === READY_KEY\)", ln)]
    assert guard, f"no cache check against READY_KEY: {body}"
    assert "return" in guard[0], f"the cache check does not skip the fetch: {guard[0]}"
    # and the request must come AFTER it, or the check saves nothing. Matched on either
    # spelling: the consult reads went through authGet() when the plane stopped being
    # anonymous, and a guard anchored on the bare word `fetch(` would have raised
    # ValueError on the rename rather than checking anything.
    req = min((body.index(c) for c in ("authGet(", "fetch(") if c in body), default=-1)
    assert req > 0, f"loadReadiness makes no request at all: {body}"
    assert body.index("if (key === READY_KEY)") < req
    assert "plan_uri" in body and "status" in body, \
        "the cache key must include what can change the answers"


def test_switching_threads_drops_the_previous_readiness_answers():
    """Keeping them would show one consultation's data questions under another's title."""
    code = _strip_comments(_front())
    for fn in ("function selectTask(id)", "function newThread()"):
        body = code[code.index(fn):]
        body = body[:body.index("\n}")]
        assert "READY_DATA = null" in body, f"{fn} leaks the previous thread's readiness"


def test_a_failed_readiness_fetch_leaves_the_thread_working():
    """The panel is an extra, not the thread. An unhandled rejection here would stop the
    render that follows it and freeze the conversation."""
    code = _strip_comments(_front())
    body = code[code.index("async function loadReadiness(t)"):]
    body = body[:body.index("\n}")]
    assert "catch" in body


def test_the_thread_renderer_paints_every_new_piece():
    """renderThread is the panel's only entry point (that is why it exists). A piece it
    forgets to call renders once and then never updates again."""
    code = _strip_comments(_front())
    body = code[code.index("function renderThread(t)"):]
    body = body[:body.index("\n}")]
    for fn in ("renderChoices(t)", "renderDrop(t)", "renderReadiness(t)", "loadReadiness(t)"):
        assert fn in body, f"renderThread never calls {fn}"


# ── the timeline: a directive is an answer, not a moment ─────────────────────
#
# These use their own table stub on purpose. `_StubTable.query` ignores the
# KeyConditionExpression entirely and returns every item it holds, so a test built on
# it would pass no matter what the console queried -- including the broken single-query
# version these guards exist to catch. A stub that cannot fail proves nothing.
class _RangeTable:
    """A (PK, SK) stub that actually honours eq / lt / begins_with and Limit.

    Only the operators the console uses are implemented, and an unrecognised one RAISES
    rather than degrading to "match everything": silently matching is how the stub it
    replaces made the bug invisible.
    """

    def __init__(self, name="llmops-stage-events"):
        self.name = name
        self.rows = []
        self.queries = []          # every KeyConditionExpression seen, in order

    def put_item(self, Item):
        self.rows.append(dict(Item))

    #: Evaluators for the ops the console uses, keyed by the name _Cond records.
    _OPS = {
        "eq": lambda got, want: got == want,
        "lt": lambda got, want: got < want,
        "begins_with": lambda got, want: got.startswith(want),
    }

    def _preds(self, cond):
        terms = getattr(cond, "terms", None)
        # An empty term list means the condition told us nothing -- exactly the failure
        # mode of the stub this replaces. Matching everything here would make every
        # assertion below vacuous, so refuse instead.
        assert terms, "the condition stub recorded no terms; the query is untestable"
        preds = []
        for attr, op, val in terms:
            if op not in self._OPS:
                raise AssertionError(f"_RangeTable does not implement {op!r}")
            f = self._OPS[op]
            preds.append(lambda r, a=attr, v=val, f=f: f(str(r.get(a, "")), v))
        return preds

    def query(self, **kw):
        cond = kw["KeyConditionExpression"]
        self.queries.append(cond)
        preds = self._preds(cond)
        hits = [dict(r) for r in self.rows if all(p(r) for p in preds)]
        hits.sort(key=lambda r: str(r.get("sk", "")),
                  reverse=not kw.get("ScanIndexForward", True))
        if kw.get("Limit"):
            hits = hits[:int(kw["Limit"])]
        return {"Items": hits}


def _seed_timeline(tbl, run_id, n_events=30, n_directives=10):
    for i in range(n_events):
        tbl.put_item({"run_id": run_id,
                      "sk": f"2026-08-01T{i:02d}:00:00Z#eval#stage_complete",
                      "event_name": "stage_complete",
                      "detail": json.dumps({"i": i})})
    for i in range(n_directives):
        tbl.put_item({"run_id": run_id, "sk": f"directive#2026-08-01T{i:02d}:30:00Z",
                      "decision": f"d{i}", "rationale": "because",
                      "actor": "conductor", "deliverable": False, "delivered": False})
    return tbl


def test_a_parked_verdict_never_displaces_a_stage_event(console, monkeypatch):
    """The defect, stated as the thing an operator lost.

    `"d" > "2"`, so every `directive#` row sorts AFTER every ISO-timestamped event --
    landing exactly in the window the frontend renders (`evs.slice(-25)`). A directive
    carries no `detail`, so each one showed as a BLANK row and pushed one real event out
    of view. Measured on the shape below: 10 verdicts cost the operator the 10 newest
    stage events on a run that had 30.
    """
    tbl = _seed_timeline(_RangeTable(), "run-x")
    monkeypatch.setattr(console, "events_tbl", tbl)
    evs, dirs = console._timeline("run-x")
    assert [e for e in evs if str(e["sk"]).startswith("directive#")] == [], \
        "a verdict is being served as a stage event"
    assert len(evs) == 30 and len(dirs) == 10
    # the window the frontend actually paints must be all real events
    assert all(not str(e["sk"]).startswith("directive#") for e in evs[-25:])


def test_the_events_query_is_bounded_in_dynamodb_not_in_python(console, monkeypatch):
    """Filtering after the fact is not a fix.

    One `Limit`-ed query spends its budget on directives before the events reach the
    Lambda, so a Python-side filter yields a SHORT timeline with nothing to indicate
    anything was dropped. Two ranges each get their own budget -- so with a limit of 10
    and 10 directives present, ten real EVENTS still come back.
    """
    tbl = _seed_timeline(_RangeTable(), "run-x")
    monkeypatch.setattr(console, "events_tbl", tbl)
    evs, dirs = console._timeline("run-x", limit=10)
    assert len(evs) == 10, "the Limit was spent on rows that are not stage events"
    assert len(tbl.queries) == 2, "the split must happen in DynamoDB, not after"


def test_an_unknown_sk_prefix_cannot_displace_an_event_either(console, monkeypatch):
    """The bound excludes by SHAPE, so the next prefix added is safe by default.

    `lt(DIRECTIVE_SK)` would have fixed today's symptom and re-armed the bug: any prefix
    sorting after `directive#` (`finding#`, `note#`, ...) would vanish from BOTH lists
    with nothing to notice. Bounding on "A" keeps every `word#` row out of the stage
    timeline, which is what such a row is.
    """
    tbl = _seed_timeline(_RangeTable(), "run-x", n_events=5, n_directives=0)
    # Prefixes on BOTH sides of "directive#" alphabetically. The ones BEFORE it are the
    # discriminating cases and the reason this test is written this way: `finding#` and
    # `note#` sort after `directive#`, so `lt(DIRECTIVE_SK)` excludes them too and a test
    # seeding only those passes against the narrow bound -- vacuous. `audit#` and
    # `checkpoint#` sort BEFORE it, so the narrow bound serves them AS STAGE EVENTS.
    for sk in ("audit#2026-08-01T09:00:00Z", "checkpoint#2026-08-01T09:00:00Z",
               "finding#2026-08-01T09:00:00Z", "note#2026-08-01T09:00:00Z"):
        tbl.put_item({"run_id": "run-x", "sk": sk, "detail": "not a stage event"})
    monkeypatch.setattr(console, "events_tbl", tbl)
    evs, _ = console._timeline("run-x")
    assert len(evs) == 5, f"a non-event row reached the stage timeline: {[e['sk'] for e in evs]}"


def test_the_two_directive_prefixes_have_not_drifted_apart(console):
    """The console re-declares the driver's DIRECTIVE_SK because they ship in separate
    bundles. If the driver ever renames its prefix, its verdicts become invisible to the
    console rather than mis-sorted -- a quieter version of the same bug, so it fails
    here instead.

    Read from the driver's SOURCE rather than by importing it: the driver pulls in the
    AgentCore SDK and its own env at import, and a guard about one string constant must
    not depend on that.
    """
    src = (REPO / "orchestration/harness_driver/handler.py").read_text()
    m = re.search(r'^DIRECTIVE_SK\s*=\s*"([^"]+)"', src, re.M)
    assert m, "the driver no longer declares DIRECTIVE_SK"
    assert console.DIRECTIVE_SK == m.group(1), (
        f"driver parks under {m.group(1)!r}, console reads {console.DIRECTIVE_SK!r}")


def test_a_verdict_that_could_never_be_read_does_not_render_as_one_that_was(console, monkeypatch):
    """The undeliverable-verdict fix is only worth anything if the display preserves it.
    `deliverable` arrives as the STRING "False" from DynamoDB, whose truthiness is not
    the question -- so the projection must carry the field and the renderer must compare
    it properly."""
    tbl = _RangeTable()
    tbl.put_item({"run_id": "run-x", "sk": "directive#2026-08-01T13:45:40Z",
                  "decision": "raise_teacher_cap", "rationale": "why",
                  "actor": "conductor", "deliverable": False, "delivered": False,
                  "run_status_at_put": "escalated"})
    monkeypatch.setattr(console, "events_tbl", tbl)
    monkeypatch.setattr(console, "s3", FakeS3())
    monkeypatch.setattr(console, "data_bucket", lambda: "b")
    monkeypatch.setattr(console, "runs_tbl", None)
    out = console.run_detail("run-x")
    v = out["directives"][0]
    assert v["deliverable"] == "False" and v["run_status_at_put"] == "escalated"
    code = _strip_comments(_front())
    assert 'String(v.deliverable).toLowerCase() === "true"' in code, \
        "the renderer is treating the string \"False\" as a boolean"


def test_the_run_view_paints_the_verdicts_it_is_served(console):
    """Returning directives and never rendering them would repeat the defect one layer
    up: the record exists, and nobody sees it."""
    code = _strip_comments(_front())
    assert "d.directives" in code, "run_detail serves directives that nothing renders"
    body = code[code.index("const dirs = d.directives"):]
    body = body[:body.index("const evs = d.events")]
    for want in ("never delivered", "parked, awaiting pickup", "picked up by the run"):
        assert want in body, f"the verdict table never says {want!r}"


def test_the_gate_row_carries_the_interval_it_is_decided_by(console, monkeypatch):
    """judge_score passes on its Wilson LOWER bound and fails on its UPPER (the eval
    gate bullet), so a row showing only the point estimate renders an escalated
    borderline as an inexplicable n/a. The bounds must ride the judge_score gate row
    only, and the OOD block is served and rendered as a report, never as a gate."""
    man = {"params": {"gates": {"judge_score": 0.45, "format_validity": 0.95}},
           "stages": {"eval": {"metrics": {
               "judge_score": 0.48, "judge_score_ci_low": 0.40,
               "judge_score_ci_high": 0.56, "judge_n": 150,
               "format_validity": 1.0, "format_n": 150, "gate_passed": False,
               "ood": {"judge_score": 0.02, "judge_n": 40}}}}}
    s3f = FakeS3()
    s3f.put_object(Bucket="b", Key="runs/run-x/manifest.json",
                   Body=json.dumps(man).encode())
    monkeypatch.setattr(console, "s3", s3f)
    monkeypatch.setattr(console, "data_bucket", lambda: "b")
    monkeypatch.setattr(console, "events_tbl", None)
    monkeypatch.setattr(console, "runs_tbl", None)
    out = console.run_detail("run-x")
    js = next(g for g in out["gates"] if g["name"] == "judge_score")
    assert (js["judge_score_ci_low"], js["judge_score_ci_high"], js["judge_n"]) == \
        (0.40, 0.56, 150)
    # ...and the row must be DECIDED by them. This fixture is the D6 defect verbatim: the
    # point estimate 0.48 clears the 0.45 bar while the lower bound 0.40 does not, and the
    # console painted PASS on a row whose run was escalated as borderline (gate_passed False).
    assert (js["status"], js["passed"]) == ("borderline", None), (
        f"the console re-derived the verdict from the point estimate: {js!r}")
    fv = next(g for g in out["gates"] if g["name"] == "format_validity")
    assert "judge_score_ci_low" not in fv, "the CI fields leaked onto a scalar gate row"
    # `format_n` IS in the fixture's metrics, on purpose: an assertion that a key is absent
    # from the row is satisfied for free when the key is absent from the source, and the
    # denominator's absence is the thing being tested.
    assert "format_n" in man["stages"]["eval"]["metrics"], "the leak has nothing to leak"
    assert "format_n" not in fv, "a denominator rode a row with no interval to divide"
    # D11: a bare 1.0 against a 0.95 bar is INSIDE the gate bullet's +/-0.05 band, so the
    # pipeline escalates it and this row may not paint a pass. The way this gate becomes a
    # decisive pass is the interval the score bullet now mandates, not a wider comparator --
    # see test_a_proportion_gate_with_no_interval_cannot_pass_a_bar_this_close_to_its_ceiling.
    assert (fv["status"], fv["passed"]) == ("borderline", None), f"{fv!r}"
    assert out["oodReport"] == {"judge_score": 0.02, "judge_n": 40}
    code = _strip_comments(_front())
    assert "d.oodReport" in code, "run_detail serves an OOD report nothing renders"
    assert "never gated" in code, "the OOD block does not say it is not a gate"
    # Derived from the row's own metric name now, so the literal is gone on purpose -- the
    # next interval-bearing gate metric must paint its bounds without an edit here.
    assert '_ci_low' in code, "the CI bounds are served and never painted"
    assert 'g.name+"_ci_low"' in code, (
        "the bounds are painted from a hardcoded metric name, so a second interval-bearing "
        "gate would serve bounds nothing renders")
    # ...and the literal is gone from EVERY site, not just from one of the three the row
    # template reads. Asserting only the derived form is present passes while a hardcoded key
    # sits beside it -- which is how a partly-converted template renders judge_score's bounds
    # and no other metric's.
    assert "judge_score_ci_low" not in code, (
        "a hardcoded metric key survives in the frontend beside the derived one")


def _detail_with(console, monkeypatch, man):
    """run_detail over a manifest fixture, with S3 the only backing store."""
    s3f = FakeS3()
    s3f.put_object(Bucket="b", Key="runs/run-x/manifest.json",
                   Body=json.dumps(man).encode())
    monkeypatch.setattr(console, "s3", s3f)
    monkeypatch.setattr(console, "data_bucket", lambda: "b")
    monkeypatch.setattr(console, "events_tbl", None)
    monkeypatch.setattr(console, "runs_tbl", None)
    return console.run_detail("run-x")


#: A signed plan that asks for the report-only layer. No account id anywhere in it: the
#: redaction guard reads added lines, and a plausible bucket name is the easiest way in.
_OOD_URI = "s3://BUCKETNAME/customer-data/helpdesk-demo/ood_acceptance.jsonl"


def test_an_ood_layer_the_plan_asked_for_and_the_report_omits_is_not_silence(
        console, monkeypatch):
    """D9: the report-only layer had no floor, so it could vanish and read as absent.

    The OOD layer never blocks a deploy on purpose. That trade is only honest while the
    layer is actually measured, and nothing enforced it: `params.ood_eval_uri` set with no
    `report.json.ood` produced exactly the page a run without an OOD layer produces. The
    driver reads only `gate_passed`, and this function drew the block on presence alone --
    six lines under a comment that draws precisely this distinction for the gate rows.
    """
    man = {"params": {"gates": {"judge_score": 0.45}, "ood_eval_uri": _OOD_URI},
           "stages": {"eval": {"metrics": {"judge_score": 0.48, "judge_score_ci_low": 0.46,
                                           "judge_score_ci_high": 0.56, "judge_n": 150,
                                           "gate_passed": True}}}}
    out = _detail_with(console, monkeypatch, man)
    assert "oodReport" not in out, "the fixture has no ood object to serve"
    assert out.get("oodMissing") == _OOD_URI, (
        "the plan names an OOD set and the eval report carries no `ood` object, and the "
        f"run view says nothing about it: {out.get('oodMissing')!r}. A gate PASS is rendered "
        "over a missing half of the design, on the same page, with no way to tell.")
    code = _strip_comments(_front())
    assert "d.oodMissing" in code, "run_detail serves the omission and nothing renders it"
    assert "NOT REPORTED" in code, "the omission renders without saying what it is"


def test_a_plan_with_no_ood_layer_is_not_accused_of_omitting_one(console, monkeypatch):
    """The other side: `oodMissing` has to mean the PLAN asked. Deriving it from the
    report's shape alone would flag every single-layer run, which is most of them."""
    man = {"params": {"gates": {"judge_score": 0.45}},
           "stages": {"eval": {"metrics": {"judge_score": 0.48, "gate_passed": True}}}}
    out = _detail_with(console, monkeypatch, man)
    assert "oodMissing" not in out, f"a run with no OOD layer was flagged: {out['oodMissing']!r}"


def test_an_eval_stage_still_running_has_not_omitted_its_ood_layer(console, monkeypatch):
    """Same false alarm the gate rows avoid via `eval_reported`, one block down. Before
    eval writes metrics there is nothing to have omitted, and an alarm on every run that
    is merely mid-inference is the one that teaches an operator to ignore the real one.
    Keyed on the `metrics` KEY, not on truthiness: run-20260811T165529Z-ce628817 has a
    `stages.eval` entry with `status: inference_in_progress` and no metrics at all."""
    man = {"params": {"gates": {"judge_score": 0.45}, "ood_eval_uri": _OOD_URI},
           "stages": {"eval": {"status": "inference_in_progress"}}}
    out = _detail_with(console, monkeypatch, man)
    assert "oodMissing" not in out, (
        "a run still generating answers was reported as having omitted its OOD layer")


# ── D6: the gate table's five situations ─────────────────────────────────────
#
# `passed` was a tri-state boolean derived from `actual >= threshold`, and the frontend
# painted PASS / FAIL / n/a. Three different situations shared that n/a: a borderline the
# pipeline escalated rather than decide, a gated metric the eval report never carried --
# which `agents/eval/harness.json` defines as a FAILED gate, not an undecided one -- and a
# run that died before eval ran at all. Measured across the 34 gated runs in this account:
# 30 never reached eval, and run-phase2-main-0001 reports `gate_passed: false` with BOTH of
# its gate metrics absent, so the console shows "gate failed" above a table of blanks and
# cannot answer the one question the table exists for.
def _gate(console, name, threshold, metrics, eval_reported=True):
    return console.gate_row(name, threshold, metrics, eval_reported)


def _eval_prompt():
    """The eval agent's system prompt, joined. The console's gate verdicts are a SECOND
    derivation of a rule this prompt is the first statement of, so the tests below read the
    rule out of the prompt rather than restating it -- two spellings of one rule drift, and
    the drift shows up only on the runs nobody re-reads."""
    h = json.loads((REPO / "agents/eval/harness.json").read_text())
    return "\n".join(b.get("text", "") for b in h["systemPrompt"])


def test_a_gated_metric_the_report_omits_is_a_failed_gate_not_a_blank(console):
    """agents/eval/harness.json: "If params.gates names a metric your report does not
    contain, that is a failed gate, not a passed one." The console rendered it as n/a."""
    row = _gate(console, "relative_solve_rate", 0.80, {"gate_passed": False})
    assert (row["status"], row["passed"]) == ("not_measured", False), f"{row!r}"


def test_a_run_that_never_reached_eval_is_not_a_failed_gate(console):
    """The other half of the same distinction, and the reason `not_measured` could not just
    be folded into `failed`: 30 of 34 gated runs in this account died in data-prep or
    finetune. Painting their gates red claims a measurement that never happened."""
    row = _gate(console, "relative_solve_rate", 0.80, {}, eval_reported=False)
    assert (row["status"], row["passed"]) == ("not_evaluated", None), f"{row!r}"


def test_an_eval_stage_still_running_has_not_failed_its_gates(console, monkeypatch):
    """The third shape of the same distinction, and the one a stage-entry existence check
    gets wrong. Two writers put entries under `stages.eval`: the driver on completion,
    always with a metrics mapping, and the agents mid-stage, without one.
    run-20260811T165529Z-ce628817 is the second kind on S3 right now -- `status:
    inference_in_progress`, an `inference_job` block, no metrics -- so deriving "the eval
    stage reported" from `bool(eval_entry)` paints both of its gates FAILED while the
    inference job is still launching."""
    man = {"params": {"gates": {"judge_score": 0.45, "format_validity": 0.95}},
           "stages": {"eval": {"status": "inference_in_progress", "task": "evaluate",
                               "iteration": 0, "inference_job": {"job_name": "j"}}}}
    s3f = FakeS3()
    s3f.put_object(Bucket="b", Key="runs/run-y/manifest.json",
                   Body=json.dumps(man).encode())
    monkeypatch.setattr(console, "s3", s3f)
    monkeypatch.setattr(console, "data_bucket", lambda: "b")
    monkeypatch.setattr(console, "events_tbl", None)
    monkeypatch.setattr(console, "runs_tbl", None)
    for row in console.run_detail("run-y")["gates"]:
        assert (row["status"], row["passed"]) == ("not_evaluated", None), (
            f"a gate was decided against an eval stage that had not reported yet: {row!r}")
    # The other direction: a report that arrived carrying nothing is a report, and the eval
    # gate bullet makes a gated metric it omits a failed gate.
    man["stages"]["eval"] = {"status": "completed", "metrics": {}}
    s3f.put_object(Bucket="b", Key="runs/run-y/manifest.json",
                   Body=json.dumps(man).encode())
    for row in console.run_detail("run-y")["gates"]:
        assert (row["status"], row["passed"]) == ("not_measured", False), (
            f"an empty eval report read as never having been measured: {row!r}")


def test_an_interval_that_straddles_the_bar_is_borderline_not_a_pass(console):
    """The gate bullet passes on the LOWER bound and fails on the UPPER; anything else it
    escalates with the numbers. A console that decides it anyway publishes a verdict the
    pipeline refused to reach."""
    m = {"judge_score": 0.48, "judge_score_ci_low": 0.40, "judge_score_ci_high": 0.56,
         "judge_n": 150}
    assert _gate(console, "judge_score", 0.45, m)["status"] == "borderline"
    m2 = {**m, "judge_score_ci_low": 0.46}
    assert (_gate(console, "judge_score", 0.45, m2)["status"],
            _gate(console, "judge_score", 0.45, m2)["passed"]) == ("passed", True)
    m3 = {**m, "judge_score_ci_high": 0.44, "judge_score": 0.43}
    assert _gate(console, "judge_score", 0.45, m3)["status"] == "failed"


def test_the_fixed_band_decides_a_gate_that_reports_no_interval(console):
    """A metric with no bounds is decided by the gate bullet's fixed band, not by the interval
    rule -- the interval rule must not turn a scalar gate into an undecidable one, and the band
    must not turn it into an unconditional pass either. Values well clear of the band decide."""
    assert _gate(console, "relative_solve_rate", 0.80,
                 {"relative_solve_rate": 0.95})["status"] == "passed"
    assert _gate(console, "relative_solve_rate", 0.80,
                 {"relative_solve_rate": 0.60})["status"] == "failed"


def test_a_scalar_gate_inside_the_band_is_the_escalation_the_pipeline_makes(console):
    """D11, the disagreement itself. `agents/eval/harness.json`'s gate bullet: a scalar gate with
    no interval is borderline AT or within 0.05 of the threshold, and the agent escalates rather
    than deciding. This branch returned `actual >= bar`, so it painted PASS across the whole band
    -- including AT the bar, where the distance is 0 and the rule is maximally borderline. That is
    D7's defect ("a second derivation of one verdict is a second verdict") surviving in the branch
    D7 did not touch."""
    bar = 0.80
    for value in (bar, 0.82, 0.85, 0.78, 0.75):
        row = _gate(console, "relative_solve_rate", bar, {"relative_solve_rate": value})
        assert (row["status"], row["passed"]) == ("borderline", None), (
            f"{value} is within 0.05 of {bar} and the pipeline escalates it: {row!r}")


def test_a_perfect_rate_is_not_decided_by_a_floating_point_representation(console):
    """`1.0 - 0.95` is 0.050000000000000044 in IEEE-754, so a bare `<= 0.05` puts a PERFECT rate
    OUTSIDE the band by one representation error -- decisive or borderline decided by binary
    floating point, on the single value an operator is most likely to see. "At or within 0.05"
    means the closed interval to a human, so the comparison carries a tolerance."""
    assert 1.0 - 0.95 > 0.05, "the premise of this test stopped being true"
    row = _gate(console, "format_validity", 0.95, {"format_validity": 1.0})
    assert (row["status"], row["passed"]) == ("borderline", None), (
        f"a perfect rate fell out of the band on a representation error: {row!r}")


def test_a_proportion_gate_with_no_interval_cannot_pass_a_bar_this_close_to_its_ceiling(console):
    """The finding, in arithmetic, on the gate the live plans actually carry
    (~/Downloads/r6-plans/plan-r6{a,b,c}.json: `{"judge_score": 0.45, "format_validity": 0.95}`).

    format_validity is a proportion, so 1.0 is its ceiling. bar + band = 1.00, which means the
    ENTIRE passing region [0.95, 1.00] lies inside the borderline band: no value that clears this
    bar can ever pass decisively while the report carries only a point estimate. 96 of 97 valid
    answers -- one malformed answer -- is an escalation the page used to call a pass.

    The fix is upstream, and it is the reason this test exists next to the one below: the score
    bullet now requires a proportion to report its own Wilson interval, and with bounds the SAME
    row is decided by the bounds rule, where 97/97 clears the bar at a lower bound of 0.9619.
    """
    bar = 0.95
    for k in (97, 96, 93):
        row = _gate(console, "format_validity", bar, {"format_validity": k / 97})
        assert row["status"] == "borderline", f"{k}/97 = {k/97:.4f}: {row!r}"
    # With the interval the score bullet mandates, the same 97/97 is a decisive pass.
    low, high = console._wilson(1.0, 97)
    assert round(low, 4) == 0.9619, f"the documented lower bound moved: {low}"
    row = _gate(console, "format_validity", bar, {"format_validity": 1.0, "format_n": 97,
                                                  "format_validity_ci_low": round(low, 4),
                                                  "format_validity_ci_high": round(high, 4)})
    assert (row["status"], row["passed"]) == ("passed", True), (
        f"a proportion that reports its interval is still not decidable: {row!r}")
    # ...and one malformed answer out of 97 is an honest borderline, not a silent pass.
    low96, high96 = console._wilson(96 / 97, 97)
    assert (round(low96, 4), round(high96, 4)) == (0.9439, 0.9982), f"{low96}, {high96}"
    row96 = _gate(console, "format_validity", bar,
                  {"format_validity": 96 / 97, "format_n": 97,
                   "format_validity_ci_low": round(low96, 4),
                   "format_validity_ci_high": round(high96, 4)})
    assert row96["status"] == "borderline", f"{row96!r}"


def test_the_scalar_band_is_the_one_the_eval_prompt_states(console):
    """The band is the PROMPT's number, not the console's. Two spellings of one rule drift, and
    the drift is invisible: a console band of 0.02 against a prompt band of 0.05 disagrees only
    on the runs nobody looks at twice. So read it out of the sentence that states it."""
    prompt = _eval_prompt()
    m = re.search(r"borderline means AT or within ([0-9.]+) of the threshold", prompt)
    assert m, "the eval gate bullet no longer states a scalar band in a form the console can read"
    assert console.GATE_SCALAR_BAND == float(m.group(1)), (
        f"console band {console.GATE_SCALAR_BAND} != prompt band {m.group(1)}")
    # And the prompt must still warn that a bar within a band of the ceiling can never pass.
    assert "can NEVER return a decisive pass" in prompt, (
        "the gate bullet no longer names the band's own failure mode")


def test_the_score_bullet_requires_a_proportion_to_report_its_interval(console):
    """The console cannot invent bounds the report does not carry, so the fix for a proportion
    gate lives in the eval prompt. Derived from the prompt rather than pinned as prose: the
    console's family derivation (`name.rsplit("_", 1)[0]`) is what makes `format_n` the right
    name, so the prompt and the console must agree on that spelling too."""
    prompt = _eval_prompt()
    for needed in ("<metric>_ci_low", "<family>_n", "format_n", "format_validity"):
        assert needed in prompt, f"the score bullet does not mandate {needed}"
    assert "format_validity".rsplit("_", 1)[0] + "_n" == "format_n", (
        "the console's family derivation no longer yields the name the prompt mandates")


def test_a_scalar_borderline_says_which_rule_produced_it(console):
    """A BORDERLINE pill beside a value that plainly clears its bar is the "inexplicable n/a"
    this table was fixed for, one status along. The row has no bounds to show, so it must say
    what decided it -- and only then: a row WITH bounds already prints them."""
    code = _strip_comments(_front())
    assert "function gateBand(" in code, "no annotation for a band-decided row"
    assert "decided by the ±0.05 band" in code, "the annotation does not name the rule"
    assert "gateBand(g)" in code, "gateBand is defined and never called"
    band = code[code.index("function gateBand("):]
    band = band[:band.index("\n}")]
    assert '_ci_low"]!=null' in band, (
        "the annotation is not suppressed on a row whose bounds already explain it")


def test_the_interval_rule_is_keyed_off_the_bounds_and_not_off_a_metric_name(console):
    """Whether a metric is decided by an interval is a property of what the report carries
    about it. A name list ("judge_score") would silently drop the next interval-bearing
    metric back to its point estimate -- which is the defect being fixed, one metric later."""
    m = {"map50": 0.78, "map50_ci_low": 0.71, "map50_ci_high": 0.84}
    assert _gate(console, "map50", 0.75, m)["status"] == "borderline", (
        "an interval-bearing gate that is not judge_score was decided on its point estimate")


def test_a_value_a_threshold_cannot_be_compared_against_is_not_a_pass(console):
    """Fail closed. The old code caught the ValueError and left `passed` None, which the
    frontend painted as the same n/a a never-evaluated run gets."""
    row = _gate(console, "format_validity", 0.95, {"format_validity": "n/a"})
    assert (row["status"], row["passed"]) == ("unreadable", False), f"{row!r}"


def test_every_gate_status_the_server_can_send_has_a_pill_that_renders_it(console):
    """Derived from the module's own constants, so adding a sixth status without a pill is a
    red test rather than a blank cell. The `||` fallback in the row template covers an old
    cached bundle, not a status this server can produce."""
    statuses = {v for k, v in vars(console).items()
                if k.startswith("GATE_") and isinstance(v, str)}
    # A derived census that finds nothing agrees with any frontend at all, so the count is
    # pinned: passed/failed/borderline/not_measured/not_evaluated/unreadable/unreconciled.
    assert len(statuses) == 7, f"the status vocabulary changed: {sorted(statuses)}"
    code = _strip_comments(_front())
    pills = code[code.index("const GATE_PILL"):]
    pills = pills[:pills.index("};")]
    for st in sorted(statuses):
        assert f'"{st}"' in pills, f"run_detail can send status {st!r} and nothing paints it"


# ── D10: the items nobody could score ────────────────────────────────────────
#
# The pipeline's gate rule has THREE clauses and this row ran two of them. `judge_n` counts
# SCORED items only; a judge call can fail to return a verdict for reasons that have nothing to
# do with the answers, and `pipeline/eval/judge_prompt_pairwise.md` measured that on the 8B run:
# 9 of 274 (item, position) slots content_filtered, retrying recovered 5, and the 4 that stayed
# unjudgeable clustered on the credential / MFA / access categories where the student scored
# 0.000. So the missingness is not random and the interval on the survivors UNDERSTATES the
# uncertainty rather than widening to cover it, which is why the gate bullet recomputes its
# verdict with every unscorable item imputed as a win, then a loss, then a tie, and escalates
# when those disagree. The console decided PASS off the survivors-only bound and displayed
# `n=94` for a 97-row layer with the 3 missing items nowhere on the page.
#
# The real ID-layer numbers from that run, which every fixture below is anchored to.
_8B_ID = {"judge_score": 0.2234, "judge_score_ci_low": 0.151, "judge_score_ci_high": 0.318,
          "judge_n": 94, "judge_wins": 3, "judge_ties": 36, "judge_losses": 55,
          "judge_unscorable": 3, "items_in_layer": 97}


def test_a_decisive_pass_the_unscorable_items_could_overturn_is_not_a_pass(console):
    """The failure this fixes, in arithmetic. Survivors: 94 scored, judge_score 0.5532, Wilson
    lower bound 0.4520 -- at or above the 0.45 bar, so the row painted PASS and `passed: True`.
    Impute the 3 unscorable items as losses and the same rule over 97 gives 0.5361 with a lower
    bound of 0.4374, which is BORDERLINE: the verdict moves, so the eval agent escalates with
    all three imputations and `gate_passed` is not True. A console that says PASS there is
    publishing the one verdict the pipeline explicitly declined to reach."""
    near = {**_8B_ID, "judge_score": 0.5532, "judge_score_ci_low": 0.4520,
            "judge_score_ci_high": 0.6499, "judge_wins": 42, "judge_ties": 20,
            "judge_losses": 32}
    row = _gate(console, "judge_score", 0.45, near)
    assert (row["status"], row["passed"]) == ("borderline", None), (
        f"a PASS survived items nobody could score that overturn it: {row!r}")
    # The clause is what did it, not the bounds: with nothing unscorable the SAME bounds pass.
    scored_all = {**near, "judge_unscorable": 0, "judge_n": 97, "judge_wins": 43,
                  "judge_ties": 21, "judge_losses": 33}
    assert _gate(console, "judge_score", 0.45, scored_all)["status"] == "passed", (
        "the imputation clause is rejecting the bounds themselves, not the missing items")


def test_unanimous_imputations_do_not_manufacture_a_borderline(console):
    """The other direction, and the reason this is a recompute and not a tolerance. The 8B run
    is the worked example in the instrument doc: every imputation FAILS decisively, so the 4
    items it could not read were never decision-relevant and escalating would have been noise.
    A rule whose only behaviour is to fire is a rule that teaches an operator to ignore it."""
    row = _gate(console, "judge_score", 0.45, _8B_ID)
    assert (row["status"], row["passed"]) == ("failed", False), f"{row!r}"
    assert console._imputed_verdicts(_8B_ID, "judge", 0.45) == {"failed"}, (
        "the imputations of the run this rule was calibrated on no longer agree")


def test_unscorable_items_nobody_can_account_for_are_not_a_pass(console):
    """`judge_unscorable: 3` with no wins/ties/losses to recompute from is a report saying
    items are missing and withholding what the console would need to check whether they matter.
    Painting PASS there claims a check that could not run."""
    blind = {"judge_score": 0.55, "judge_score_ci_low": 0.46, "judge_score_ci_high": 0.64,
             "judge_n": 94, "judge_unscorable": 3, "items_in_layer": 97}
    row = _gate(console, "judge_score", 0.45, blind)
    assert (row["status"], row["passed"]) == ("borderline", None), f"{row!r}"
    assert console._imputed_verdicts(blind, "judge", 0.45) is None
    # ...and a report from before those fields existed is not retroactively undecidable: 30 of
    # the 34 gated runs in this account predate them, and painting every one of them borderline
    # is how a real borderline stops being visible.
    legacy = {"judge_score": 0.55, "judge_score_ci_low": 0.46, "judge_score_ci_high": 0.64,
              "judge_n": 94}
    assert _gate(console, "judge_score", 0.45, legacy)["status"] == "passed", f"{legacy!r}"
    assert console._imputed_verdicts(legacy, "judge", 0.45) == set()


def test_a_score_that_fails_its_own_denominator_reconciliation_gets_no_verdict(console):
    """D8 made the eval agent assert `judge_n + judge_unscorable == items_in_layer` and refuse
    to report a score that fails it. This page is the only reader that can catch a report which
    asserted it and got it wrong -- and the harm runs the flattering way, because a silently
    shrunken denominator makes the Wilson interval NARROWER around the wrong sample. Its own
    status, not `failed`: a bookkeeping failure and a student that missed a bar call for
    completely different work."""
    wrong = {**_8B_ID, "items_in_layer": 137}
    row = _gate(console, "judge_score", 0.45, wrong)
    assert (row["status"], row["passed"]) == ("unreconciled", False), f"{row!r}"
    # It reconciles at 97 -- so the check is reading the numbers, not rejecting the shape.
    assert _gate(console, "judge_score", 0.45, _8B_ID)["status"] == "failed"


def test_the_row_shows_the_items_its_denominator_leaves_out(console):
    """A number served and never painted is the defect one layer up. `judge_n` on its own is a
    claim with exceptions, and the exceptions are exactly what an operator needs to see."""
    row = _gate(console, "judge_score", 0.45, _8B_ID)
    assert (row["judge_n"], row["judge_unscorable"], row["items_in_layer"]) == (94, 3, 97), (
        f"the row hides the items its denominator excludes: {row!r}")
    code = _strip_comments(_front())
    assert "gateDenom" in code and "unscorable" in code, (
        "run_detail serves the unscorable count and the page never prints it")
    assert 'g["items_in_layer"]' in code, "the layer size is served and never painted"
    # Derived from the row's metric family, like `_n` already is -- a hardcoded "judge_"
    # would print nothing for the next interval-bearing gate metric.
    assert '"judge_unscorable"' not in code, (
        "the unscorable count is painted from a hardcoded metric family")
    # ...and only where there is an interval to qualify. A scalar gate has no denominator.
    scalar = _gate(console, "format_validity", 0.95,
                   {"format_validity": 1.0, "judge_unscorable": 3, "items_in_layer": 97})
    assert "items_in_layer" not in scalar and "judge_unscorable" not in scalar, f"{scalar!r}"


def test_the_console_reads_the_field_names_the_eval_prompt_mandates(console):
    """The two ends have to agree on the spelling or this whole check is vacuous -- and one of
    them is not family-prefixed, which is the kind of detail a rename quietly breaks.
    `items_in_layer` is a property of the LAYER (the row count of the acceptance file), while
    `judge_unscorable` hangs off the metric family, so the names are derived from the eval score
    bullet's own reconciliation formula rather than retyped here."""
    prompt = json.loads((REPO / "agents/eval/harness.json").read_text())
    text = prompt["systemPrompt"][0]["text"]
    formula = re.search(r"assert (\w+) \+ (\w+) == (\w+)", text)
    assert formula, "the eval score bullet no longer states the reconciliation it asserts"
    judged, unscorable, layer = formula.groups()
    src = (REPO / "deploy/console/lambda_function.py").read_text()
    fam, _, suffix = judged.rpartition("_")
    assert f'f"{{family}}_{suffix}"' in src, (
        f"the eval report calls the scored count {judged!r} and the console reads something else")
    assert f'f"{{family}}_{unscorable.split("_", 1)[1]}"' in src, (
        f"the eval report calls the excluded count {unscorable!r} and the console reads "
        "something else")
    assert f'"{layer}"' in src, (
        f"the eval report calls the layer size {layer!r} and the console reads something else")
    assert fam == unscorable.split("_", 1)[0], (
        f"{judged!r} and {unscorable!r} no longer share a family, so deriving one from the "
        "metric name no longer finds the other")
