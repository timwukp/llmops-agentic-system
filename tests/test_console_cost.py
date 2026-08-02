"""Unit tests for the console's Cost tab — no AWS, every client patched.

Scope: the HTTP layer and the approval gate as the console enforces them. The cost
arithmetic lives in tests/test_cost_model.py and the agent wiring in
tests/test_finops.py; this file is about what the dashboard will and will not let a
person do.

The console module is imported with boto3 stubbed, because it builds ~12 clients and
calls sts:GetCallerIdentity at import time. That is fine in Lambda and impossible in a
unit test, so the fake is installed before the import rather than patched after.

Run: .venv/bin/python -m pytest tests/test_console_cost.py -q
"""
from __future__ import annotations

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

ACCOUNT = "123456789012"  # documentation account — never a real one, per the repo rule


# ── import-time isolation ─────────────────────────────────────────────────────
class _StubClient:
    """Any boto3 client. Every call raises, so a test that forgets to inject a fake
    fails loudly instead of silently reaching for AWS."""

    def __init__(self, service):
        self._service = service

    def get_caller_identity(self):
        return {"Account": ACCOUNT}

    def __getattr__(self, name):
        def _boom(*a, **kw):
            raise AssertionError(f"unstubbed AWS call {self._service}.{name}")
        return _boom


class _StubTable:
    def __init__(self, name):
        self.name = name
        self.items: dict = {}
        self.puts, self.updates = [], []
        self.query_should_fail = False

    def put_item(self, Item):
        self.puts.append(Item)
        self.items[Item.get("id") or Item.get("sk")] = dict(Item)

    def get_item(self, Key):
        k = Key.get("id") or Key.get("sk")
        it = self.items.get(k)
        return {"Item": dict(it)} if it else {}

    def update_item(self, **kw):
        self.updates.append(kw)
        k = (kw.get("Key") or {}).get("id")
        it = self.items.get(k)
        if it is None:
            return {}
        # Applies only the "SET a = :x" pairs the console actually writes; enough to
        # make a second read observe the new status, which several tests depend on.
        vals = kw.get("ExpressionAttributeValues") or {}
        names = kw.get("ExpressionAttributeNames") or {}
        expr = kw.get("UpdateExpression", "").replace("SET ", "")
        for part in expr.split(","):
            if "=" not in part:
                continue
            lhs, rhs = [p.strip() for p in part.split("=", 1)]
            lhs = names.get(lhs, lhs)
            if rhs in vals:
                it[lhs] = vals[rhs]
        return {}

    def query(self, **kw):
        if self.query_should_fail:
            raise RuntimeError("ValidationException: index not found")
        return {"Items": [dict(v) for v in self.items.values()]}

    def scan(self, **kw):
        return {"Items": [dict(v) for v in self.items.values()]}


class _StubResource:
    def __init__(self):
        self.tables: dict = {}

    def Table(self, name):
        return self.tables.setdefault(name, _StubTable(name))


_RESOURCE = _StubResource()


def _fake_boto3():
    m = types.ModuleType("boto3")
    m.client = lambda service, **kw: _StubClient(service)
    m.resource = lambda service, **kw: _RESOURCE
    conds = types.ModuleType("boto3.dynamodb.conditions")

    class _Cond:
        """A key/filter condition that RECORDS what was asked.

        It used to keep one (op, val) pair and define `__and__` as `return self`, which
        silently discarded the right-hand side of every compound condition. Any table
        stub built on it therefore could not see -- and so could not test -- the sk range
        half of a `PK eq AND SK <op>` query. That is not a harmless simplification: it is
        what let a query returning rows it should have excluded look correct in tests.

        `terms` is the flat list of (attr, op, val) an AND chain contributed, so a stub
        can evaluate the real predicate. Comparison methods are named after the boto3
        ones the console actually calls; an unimplemented one raises on use rather than
        being quietly absent.
        """

        def __init__(self, key):
            self.key = key
            self.op = None
            self.val = None
            self.terms = []

        def _rec(self, op, v):
            self.op, self.val = op, v
            self.terms = [(self.key, op, v)]
            return self

        def eq(self, v):
            return self._rec("eq", v)

        def gte(self, v):
            return self._rec("gte", v)

        def lt(self, v):
            return self._rec("lt", v)

        def begins_with(self, v):
            return self._rec("begins_with", v)

        def __and__(self, other):
            combined = _Cond(self.key)
            combined.op, combined.val = self.op, self.val
            combined.terms = list(self.terms) + list(getattr(other, "terms", []))
            return combined

    conds.Key = _Cond
    conds.Attr = _Cond
    dyn = types.ModuleType("boto3.dynamodb")
    dyn.conditions = conds
    m.dynamodb = dyn
    return m, dyn, conds


@pytest.fixture(scope="module")
def console():
    """The console module, imported once with AWS stubbed out."""
    saved = {k: sys.modules.get(k) for k in
             ("boto3", "boto3.dynamodb", "boto3.dynamodb.conditions")}
    m, dyn, conds = _fake_boto3()
    sys.modules["boto3"] = m
    sys.modules["boto3.dynamodb"] = dyn
    sys.modules["boto3.dynamodb.conditions"] = conds
    try:
        spec = importlib.util.spec_from_file_location(
            "console_lambda", REPO / "deploy/console/lambda_function.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
    return mod


# ── fixtures: a rate card and a priced plan ───────────────────────────────────
#: Measured live 2026-07-31 via `pricing get-products`; the rate behind the real $10.77.
RATES = {
    "sagemaker:training:ml.g5.2xlarge": {
        "unit_price": 1.515, "unit": "hour", "source": "price_list",
        "as_of": "2026-07-31T00:00:00+00:00"},
    "sagemaker:inference:ml.g5.xlarge": {
        "unit_price": 1.006, "unit": "hour", "source": "price_list",
        "as_of": "2026-07-31T00:00:00+00:00"},
}


@pytest.fixture
def wired(console, monkeypatch):
    """Console with fresh cost tables, a readable rate card, and no real S3/Lambda."""
    est = _StubTable("llmops-cost-estimates")
    act = _StubTable("llmops-cost-actuals")
    monkeypatch.setattr(console, "estimates_tbl", est)
    monkeypatch.setattr(console, "actuals_tbl", act)

    import cost_model
    monkeypatch.setattr(console, "_cost_model", lambda: cost_model)
    monkeypatch.setattr(console, "_rate_card", lambda: cost_model.RateCard(RATES))

    invokes = []

    class _Lam:
        def invoke(self, **kw):
            invokes.append(kw)
            return {"StatusCode": 202,
                    "Payload": _Body(json.dumps(
                        {"run_id": "run-test-0001",
                         "execution_arn": f"arn:aws:states:us-east-1:{ACCOUNT}:"
                                          "execution:llmops-pipeline:run-test-0001"}))}

    class _Body:
        def __init__(self, s):
            self.s = s

        def read(self):
            return self.s.encode()

    monkeypatch.setattr(console, "lam", _Lam())
    return types.SimpleNamespace(m=console, est=est, act=act, invokes=invokes,
                                 cm=cost_model)


def _mk_estimate(w, body=None, user="requester"):
    b = {"sample_count": 2000, "endpoint_hours": 0, "max_iterations": 1}
    b.update(body or {})
    return w.m.create_estimate(b, user, "2026-07-31T00:00:00+00:00")


#: A plan whose EXPECTED total is under the single-run reference while its WORST CASE
#: (three remediation iterations) is over it -- the only arrangement that can tell the two
#: fields apart, and therefore the arrangement every budget test below is built on.
#:
#: Derived from the reference, not typed. At $2,000 the fixture was a literal 2,000,000
#: rows priced at $1,268 expected / $3,804 worst case. Raising the reference to $20,000
#: put BOTH of those under it, which does not fail -- it silently makes every budget test
#: pass by never engaging the budget check at all. The fixtures' own docstrings named this
#: exact hazard ("if a rate change ever drops this under $2000 the launch tests would pass
#: by never engaging the budget check"); a limit change is that hazard arriving on purpose.
#:
#: 0.5 x the reference targets the middle of the straddle: with max_iterations=3 the worst
#: case is ~3x the total, so expected lands at half the reference and worst case at ~1.5x
#: it. Both sides keep real margin, so a rate change has to move by 2x -- not by a
#: rounding error -- before the straddle silently stops holding. `_straddling_plan` asserts
#: the relation on every use anyway, because a derivation is still a claim.
def _straddling_plan(w):
    """{sample_count, max_iterations} priced to straddle w's single-run reference."""
    limit = w.m.APPROVAL_LIMIT_USD
    card = w.m._rate_card()
    probe = w.cm.estimate_run({"sample_count": 1_000_000, "train_rows": 1_000_000,
                               "endpoint_hours": 0, "max_iterations": 3}, card)
    per_sample = probe["total_usd"] / 1_000_000
    return {"sample_count": int(0.5 * limit / per_sample), "max_iterations": 3}


def _submit(w, eid, user="requester"):
    return w.m.request_approval({"estimate_id": eid}, user,
                                "2026-07-31T00:01:00+00:00")


@pytest.fixture
def blocking(wired, monkeypatch):
    """The same console with BUDGET_MODE=blocking.

    The default is advisory: this platform's owner is its only approver, so a gate here
    could only ever ask them to approve their own run. But the enforcement path is still
    shipped code reachable by one env var, so every budget hazard below is asserted
    twice — reported in advisory mode, and blocking under this fixture. Testing only the
    default would leave `blocking` as an untested branch that operators can switch on.
    """
    monkeypatch.setattr(wired.m, "BUDGET_MODE", "blocking")
    return wired


# ── estimating ────────────────────────────────────────────────────────────────
def test_estimate_is_line_itemised_and_persisted_as_a_draft(wired):
    r = _mk_estimate(wired)
    assert r["ok"] and r["estimate_id"].startswith("est-")
    assert r["estimate"]["line_items"], "an estimate with no line items is a bare total"
    assert len(wired.est.puts) == 1
    # draft, not pending_approval: pricing a plan must not file a request. Otherwise an
    # operator exploring numbers spams the approver queue.
    assert wired.est.puts[0]["status"] == "draft"


def test_estimate_refuses_when_no_rate_card_exists(wired, monkeypatch):
    """A $0 total with a warning gets quoted; an explicit refusal does not."""
    monkeypatch.setattr(wired.m, "_rate_card", lambda: None)
    r = _mk_estimate(wired)
    assert r.get("status_code") == 503
    assert "rate card" in r["error"]
    assert not wired.est.puts, "a refused estimate must not be recorded"


def test_estimate_requires_a_row_count(wired):
    r = wired.m.create_estimate({"endpoint_hours": 4}, "u", "t")
    assert r["status_code"] == 400
    assert "sample_count" in r["error"]


def test_estimate_rejects_a_non_numeric_sample_count(wired):
    r = wired.m.create_estimate({"sample_count": "two thousand"}, "u", "t")
    assert r["status_code"] == 400


def test_unpriced_skus_are_reported_not_silently_zeroed(wired):
    """The measured hazard: Price List has no Fable 5 or Opus 5 rate, so the harness
    lines price at $0, and any feed can drop a SKU it once carried. That must be
    visible rather than silently summed as zero."""
    r = _mk_estimate(wired)
    unpriced = r["estimate"]["unpriced"]
    assert unpriced, "expected unpriced SKUs with a training-only rate card"
    assert any("deepseek" in s for s in unpriced)
    assert wired.est.puts[0]["n_unpriced"] == len(unpriced)


def test_estimate_matches_the_measured_e3_run_within_one_percent(wired):
    """Ground truth: the 2026-07-31 QLoRA run billed $10.77 for 16,550 rows on
    ml.g5.2xlarge. A >20% miss on a run whose actual is known is a model bug, not
    noise; 1% is the tolerance the seeded throughput should hold."""
    r = _mk_estimate(wired, {"sample_count": 16550, "train_rows": 16550,
                             "endpoint_hours": 0})
    train = [l for l in r["estimate"]["line_items"]
             if l["category"] == "sagemaker_training"][0]
    assert abs(train["cost_usd"] - 10.77) / 10.77 < 0.01, train


def test_worst_case_exceeds_expected_when_remediation_is_allowed(wired):
    """max_iterations is the param whose cost impact is invisible today."""
    one = _mk_estimate(wired, {"max_iterations": 1})["estimate"]
    three = _mk_estimate(wired, {"max_iterations": 3})["estimate"]
    assert three["worst_case_usd"] > one["worst_case_usd"]


def test_only_plan_keys_the_estimator_reads_are_forwarded(wired):
    """A field the estimator ignores must not reach it: the operator would believe a
    priced input was priced. `epochs` is the tempting one — it is not in the contract."""
    r = _mk_estimate(wired, {"epochs": 9, "instance_type": "ml.p5.48xlarge"})
    plan = json.loads(wired.est.puts[0]["plan"])
    assert "epochs" not in plan and "instance_type" not in plan
    assert r["ok"]


def test_rate_card_as_of_is_stamped_so_old_variances_stay_explainable(wired):
    _mk_estimate(wired)
    assert wired.est.puts[0]["rate_card_as_of"] == "2026-07-31T00:00:00+00:00"


# ── the dual budget check ─────────────────────────────────────────────────────
def test_gate_does_not_fire_under_both_limits(wired):
    g = wired.m.gate_decision(500.0)
    assert g["approval_required"] is False
    assert g["over_budget"] == []
    assert not g.get("budget_unknown")


def test_the_console_default_is_advisory_not_blocking(wired):
    """Asserted on the module constant, not inferred from one response, because the
    default decides what this platform does to every run it prices."""
    assert wired.m.BUDGET_MODE == "advisory"
    assert wired.m.gate_decision(1e9)["budget_mode"] == "advisory"


def test_over_the_single_run_limit_is_reported_and_still_launches(wired):
    """Advisory mode's contract in one test: the overage is named and numbered, and the
    run is not stopped. The `notes`/`over_budget` assertions are the load-bearing half —
    a budget that is neither enforced nor mentioned is not a reference, it is a number
    nobody will ever act on."""
    g = wired.m.gate_decision(wired.m.APPROVAL_LIMIT_USD + 500.0)
    assert g["approval_required"] is False
    assert any("single-run" in r for r in g["over_budget"])
    assert g["over_budget_usd"] == 500.0
    assert g["notes"]

    eid = _mk_estimate(wired, _straddling_plan(wired))["estimate_id"]
    assert wired.m.start_run({"estimate_id": eid})["ok"]
    assert len(wired.invokes) == 1, "advisory mode must not hold the launch"


def test_over_the_single_run_limit_still_blocks_in_blocking_mode(blocking):
    g = blocking.m.gate_decision(blocking.m.APPROVAL_LIMIT_USD + 500.0)
    assert g["approval_required"] is True
    assert any("single-run" in r for r in g["reasons"])


def test_the_cumulative_arm_still_fires_on_its_own(wired):
    """The case a single-limit-only check misses: four runs at a quarter of the reference
    are the same exposure as one run at the whole of it, and each passes on its own. This
    is also the arm advisory mode could most easily have dropped, since no individual run
    ever looks expensive.

    A quarter of the reference EACH, derived: as four literal $500 rows this seeded
    $2,000 of to-date spend, which was the entire reference and is now a tenth of it."""
    quarter = wired.m.CUMULATIVE_LIMIT_USD / 4
    for i in range(4):
        wired.act.put_item(Item={"project": wired.m.PROJECT,
                                 "sk": f"2026-07-2{i}#run-{i}#sagemaker_training",
                                 "cost_usd": str(quarter), "settlement": "settled"})
    g = wired.m.gate_decision(500.0)
    assert any("cumulative" in r or "to-date" in r for r in g["over_budget"])
    assert g["project_to_date_usd"] == wired.m.CUMULATIVE_LIMIT_USD
    assert g["notes"]


def test_the_cumulative_arm_blocks_in_blocking_mode(blocking):
    quarter = blocking.m.CUMULATIVE_LIMIT_USD / 4
    for i in range(4):
        blocking.act.put_item(Item={"project": blocking.m.PROJECT,
                                    "sk": f"2026-07-2{i}#run-{i}#sagemaker_training",
                                    "cost_usd": str(quarter), "settlement": "settled"})
    g = blocking.m.gate_decision(500.0)
    assert g["approval_required"] is True
    assert any("cumulative" in r or "to-date" in r for r in g["reasons"])


def test_the_budget_check_reads_worst_case_not_expected(wired):
    """Reporting half the reference for a plan that can cost one and a half times it,
    because the remediation loop ran three times, misstates the exposure whether or not
    anything blocks on it.

    `_straddling_plan` prices at ~0.5x the reference expected and ~1.5x worst case against
    the measured $1.515/h · 0.664 rows/s — deliberately straddling, which is the only
    arrangement that can tell the two fields apart. Read from total_usd this run is clean.
    """
    limit = wired.m.APPROVAL_LIMIT_USD
    r = _mk_estimate(wired, _straddling_plan(wired))
    est = r["estimate"]
    assert est["total_usd"] < limit < est["worst_case_usd"], est
    assert r["gate"]["gating_basis"] == "worst_case_usd"
    assert r["gate"]["over_budget"], "the worst case is over the limit and must be said"


def test_an_unpriceable_run_is_unknown_not_under_budget(wired, monkeypatch):
    """A bundle without cost_model.py cannot price the run, so it cannot claim the run
    is under budget. Advisory mode does not block it — but "could not check" is a THIRD
    answer, and collapsing it into "under" would render an unchecked run as a
    reassuring green pill on the Cost tab."""
    monkeypatch.setattr(wired.m, "_cost_model", lambda: None)
    g = wired.m.gate_decision(1.0)
    assert g["budget_unknown"] is True
    assert g["approval_required"] is False       # advisory: not stopped
    assert g["over_budget"] == []               # no overage was MEASURED...
    assert g["notes"] and "NOT checked" in g["notes"][0]   # ...and that is stated


def test_an_unpriceable_run_still_fails_closed_in_blocking_mode(blocking, monkeypatch):
    """When the budget IS being enforced, "we could not check" must land on the
    require-approval side, never on the allow side."""
    monkeypatch.setattr(blocking.m, "_cost_model", lambda: None)
    g = blocking.m.gate_decision(1.0)
    assert g["approval_required"] is True
    assert g["budget_unknown"] is True
    assert g["reasons"], "a blocking refusal must say why"


def test_audit_rows_are_excluded_from_project_to_date(wired):
    """The agent's own #finding# notes describe variances; summing them would
    double-count the very spend they describe."""
    wired.act.put_item(Item={"project": wired.m.PROJECT,
                             "sk": "2026-07-30#run-a#sagemaker_training",
                             "cost_usd": "100", "settlement": "settled"})
    wired.act.put_item(Item={"project": wired.m.PROJECT,
                             "sk": "2026-07-30#finding#flag_variance#run-a",
                             "cost_usd": "100"})
    wired.act.put_item(Item={"project": wired.m.PROJECT,
                             "sk": "2026-07-30#audit#reconcile", "cost_usd": "100"})
    total, n = wired.m.project_to_date_usd()
    assert (total, n) == (100.0, 1)


# ── approval: separation of duties ────────────────────────────────────────────
def test_only_a_draft_can_be_submitted_for_approval(wired):
    eid = _mk_estimate(wired)["estimate_id"]
    assert _submit(wired, eid)["ok"]
    again = _submit(wired, eid)
    # Re-submitting a decided estimate is how a rejection gets quietly retried until
    # some approver says yes, with no record that it was already refused.
    assert again["status_code"] == 409


def test_non_approver_group_is_refused(wired):
    eid = _mk_estimate(wired)["estimate_id"]
    _submit(wired, eid)
    r = wired.m.decide_approval({"estimate_id": eid, "decision": "approve"},
                                {"username": "bob", "groups": ["admin"]}, "t")
    assert r["status_code"] == 403
    assert wired.m.APPROVER_GROUP in r["error"]


def test_self_approval_is_rejected_not_merely_flagged(wired):
    eid = _mk_estimate(wired, user="alice")["estimate_id"]
    _submit(wired, eid, user="alice")
    r = wired.m.decide_approval(
        {"estimate_id": eid, "decision": "approve"},
        {"username": "alice", "groups": [wired.m.APPROVER_GROUP]}, "t")
    assert r["status_code"] == 403
    assert "separation of duties" in r["error"]


def test_an_approver_from_the_group_can_approve(wired):
    eid = _mk_estimate(wired, user="alice")["estimate_id"]
    _submit(wired, eid, user="alice")
    r = wired.m.decide_approval(
        {"estimate_id": eid, "decision": "approve"},
        {"username": "boss", "groups": [wired.m.APPROVER_GROUP]}, "t")
    assert r["ok"] and r["status"] == "approved" and r["approved_by"] == "boss"


def test_a_rejection_needs_a_reason(wired):
    eid = _mk_estimate(wired)["estimate_id"]
    _submit(wired, eid)
    approver = {"username": "boss", "groups": [wired.m.APPROVER_GROUP]}
    bad = wired.m.decide_approval({"estimate_id": eid, "decision": "reject"},
                                  approver, "t")
    assert bad["status_code"] == 400
    ok = wired.m.decide_approval(
        {"estimate_id": eid, "decision": "reject", "reason": "not this quarter"},
        approver, "t")
    assert ok["status"] == "rejected"


def test_a_decision_cannot_be_replayed_over_an_earlier_one(wired):
    eid = _mk_estimate(wired)["estimate_id"]
    _submit(wired, eid)
    approver = {"username": "boss", "groups": [wired.m.APPROVER_GROUP]}
    wired.m.decide_approval(
        {"estimate_id": eid, "decision": "reject", "reason": "no"}, approver, "t")
    again = wired.m.decide_approval({"estimate_id": eid, "decision": "approve"},
                                    approver, "t")
    assert again["status_code"] == 409


def test_approval_is_denied_when_the_checker_is_unavailable(wired, monkeypatch):
    """An approval granted because the validator was missing is worse than none."""
    eid = _mk_estimate(wired)["estimate_id"]
    _submit(wired, eid)
    monkeypatch.setattr(wired.m, "_cost_model", lambda: None)
    r = wired.m.decide_approval(
        {"estimate_id": eid, "decision": "approve"},
        {"username": "boss", "groups": [wired.m.APPROVER_GROUP]}, "t")
    assert r["status_code"] == 503


def test_unknown_estimate_id_is_404_on_both_paths(wired):
    assert _submit(wired, "est-nope")["status_code"] == 404
    assert wired.m.decide_approval(
        {"estimate_id": "est-nope", "decision": "approve"},
        {"username": "boss", "groups": [wired.m.APPROVER_GROUP]}, "t"
    )["status_code"] == 404


# ── launching ─────────────────────────────────────────────────────────────────
def _over_limit(w):
    """An estimate whose worst case exceeds the single-run limit.

    The assert is not decoration: if the plan ever prices under the reference, the launch
    tests would pass by never engaging the budget check at all — green, and testing
    nothing. That is not hypothetical; it is what raising the reference to $20,000 did to
    the literal 2,000,000 rows this used to pass, which is why the plan is now derived by
    `_straddling_plan` and this assert is what proves the derivation still holds. It
    asserts on `over_budget` rather than `approval_required` so it holds in both modes;
    `approval_required` is false in advisory mode by design.
    """
    r = _mk_estimate(w, _straddling_plan(w))
    assert r["gate"]["over_budget"], "fixture must actually be over the limit"
    return r["estimate_id"]


def test_an_over_budget_launch_says_so_in_its_own_response(wired):
    """Advisory mode's honesty requirement at the launch boundary. Nothing refuses this
    run, so the launch response is the ONLY place the operator can learn it went over —
    if the verdict is dropped here, the overage is computed and then discarded."""
    eid = _over_limit(wired)
    r = wired.m.start_run({"estimate_id": eid})
    assert r["ok"] and len(wired.invokes) == 1
    assert r["result"]["gate"]["over_budget"]
    assert "single-run" in r["result"]["budget_notice"]


def test_a_gated_estimate_cannot_launch_before_approval(blocking):
    eid = _over_limit(blocking)
    _submit(blocking, eid)
    r = blocking.m.start_run({"estimate_id": eid})
    assert r["status_code"] == 409
    assert not blocking.invokes, "start-pipeline must not be invoked"


def test_a_rejected_estimate_cannot_launch(wired):
    """Terminal status is NOT the budget check, so it holds in advisory mode too.

    Advisory mode is what makes this test load-bearing. It used to be covered only
    incidentally: the estimate was over the limit, so the approval branch refused it via
    can_launch and the terminal check never had to run. Now that advisory mode never
    sets approval_required, this `if` is the only thing standing between a recorded
    rejection and a relaunch.
    """
    eid = _over_limit(wired)
    _submit(wired, eid)
    wired.m.decide_approval(
        {"estimate_id": eid, "decision": "reject", "reason": "too expensive"},
        {"username": "boss", "groups": [wired.m.APPROVER_GROUP]}, "t")
    r = wired.m.start_run({"estimate_id": eid})
    assert r.get("status_code") in (409, 403)
    assert not wired.invokes


def test_a_rejected_estimate_cannot_launch_in_blocking_mode_either(blocking):
    eid = _over_limit(blocking)
    _submit(blocking, eid)
    blocking.m.decide_approval(
        {"estimate_id": eid, "decision": "reject", "reason": "too expensive"},
        {"username": "boss", "groups": [blocking.m.APPROVER_GROUP]}, "t")
    r = blocking.m.start_run({"estimate_id": eid})
    assert r.get("status_code") in (409, 403)
    assert not blocking.invokes


def test_an_approved_estimate_launches_and_is_stamped_with_the_execution(blocking):
    eid = _over_limit(blocking)
    _submit(blocking, eid)
    blocking.m.decide_approval(
        {"estimate_id": eid, "decision": "approve"},
        {"username": "boss", "groups": [blocking.m.APPROVER_GROUP]}, "t")
    wired = blocking
    r = wired.m.start_run({"estimate_id": eid})
    assert r["ok"] and len(wired.invokes) == 1
    # Without this link the variance report holds an estimate and an actual it cannot
    # join — which is the entire point of recording the estimate.
    # Asserted on the resulting ITEM, not on the UpdateExpression text: a grep for the
    # attribute name passes even if the attribute is renamed to something the variance
    # report never reads. What matters is that the stored estimate now carries the join
    # key, under the name the reader uses.
    item = wired.est.items[eid]
    assert item["status"] == "launched"
    assert item["run_id"] == "run-test-0001"
    assert "execution:llmops-pipeline" in item["sfn_execution_arn"]
    # And the read path must surface it — a stamp no endpoint returns is invisible.
    listed = [e for e in wired.m.cost_estimates()["estimates"] if e["id"] == eid][0]
    assert listed["sfn_execution_arn"] == item["sfn_execution_arn"]


def test_an_under_limit_estimate_launches_without_approval(wired):
    """The gate must not become a tollbooth on every run — only on expensive ones."""
    eid = _mk_estimate(wired)["estimate_id"]
    r = wired.m.start_run({"estimate_id": eid})
    assert r["ok"] and len(wired.invokes) == 1


def test_an_over_budget_estimate_cannot_launch_twice(wired):
    """Double-launching attaches two runs to one estimate and double-counts it in the
    variance report. Asserted on an OVER-BUDGET estimate in advisory mode, the newly
    reachable combination: before advisory mode an over-budget estimate could not reach
    the terminal check at all, because the approval branch refused it first."""
    eid = _over_limit(wired)
    assert wired.m.start_run({"estimate_id": eid})["ok"]
    second = wired.m.start_run({"estimate_id": eid})
    assert second.get("status_code") == 409
    assert len(wired.invokes) == 1


def test_a_launched_estimate_cannot_launch_twice(blocking):
    """Two runs against one approval would double-count it in the variance report and
    spend twice what a human authorised once."""
    wired = blocking
    eid = _over_limit(wired)
    _submit(wired, eid)
    wired.m.decide_approval(
        {"estimate_id": eid, "decision": "approve"},
        {"username": "boss", "groups": [wired.m.APPROVER_GROUP]}, "t")
    assert wired.m.start_run({"estimate_id": eid})["ok"]
    second = wired.m.start_run({"estimate_id": eid})
    assert second.get("status_code") == 409
    assert len(wired.invokes) == 1


def test_a_run_with_no_estimate_still_launches(wired):
    """Every run before this feature launched without an estimate; keeping that legal
    is what lets the variance report state honestly what was never estimated."""
    r = wired.m.start_run({"sample_count": 100, "note": "ad hoc"})
    assert r["ok"] and len(wired.invokes) == 1


def test_an_unknown_estimate_id_does_not_launch(wired):
    r = wired.m.start_run({"estimate_id": "est-does-not-exist"})
    assert r["status_code"] == 400
    assert not wired.invokes


def _priced_clean_under_a_high_limit(w, monkeypatch):
    """Price the straddling plan under a DOUBLED limit, so the STORED verdict is clean,
    then restore the real reference so launch has to re-derive.

    Distinguishes WHICH field the launch-time re-derivation reads: only the worst case
    (~1.5x the reference) crosses it — the expected total (~0.5x) does not. Nothing else
    in the suite can tell those two apart, because every other over-limit fixture is
    also over on its stored gate.

    "High" is 2x the reference rather than a literal $5,000, and the restore puts back
    whatever the module actually says rather than a literal $2,000. A literal on either
    side is a second copy of the limit: the old $5,000 stopped being high the moment the
    reference passed it, and the old restore-to-$2,000 would have quietly held this one
    test at the old reference while every other test moved to the new one.
    """
    real = w.m.APPROVAL_LIMIT_USD
    real_cum = w.m.CUMULATIVE_LIMIT_USD
    # Derived BEFORE the limit is raised, on purpose: `_straddling_plan` reads whatever
    # limit is in force when it is called, so deriving it after the monkeypatch would
    # straddle the DOUBLED limit and the stored verdict would not be clean either. The
    # plan has to straddle the real reference and clear the doubled one.
    plan = _straddling_plan(w)
    monkeypatch.setattr(w.m, "APPROVAL_LIMIT_USD", real * 2)
    monkeypatch.setattr(w.m, "CUMULATIVE_LIMIT_USD", real_cum * 2)
    r = _mk_estimate(w, plan)
    assert r["gate"]["over_budget"] == [], "the stored verdict must start clean"
    monkeypatch.setattr(w.m, "APPROVAL_LIMIT_USD", real)
    monkeypatch.setattr(w.m, "CUMULATIVE_LIMIT_USD", real_cum)
    assert r["estimate"]["total_usd"] < real < r["estimate"]["worst_case_usd"]
    return r["estimate_id"]


def test_lowering_the_limit_re_derives_an_already_clean_estimate(wired, monkeypatch):
    """Re-derivation at launch is not a property of blocking — it is what makes the
    REPORTED number true. On a stored-clean estimate whose limit has since dropped,
    advisory mode must still report the overage; a launch that trusted the stored
    verdict, or re-derived from total_usd, would report this run as within budget."""
    eid = _priced_clean_under_a_high_limit(wired, monkeypatch)
    out = wired.m.start_run({"estimate_id": eid})
    assert out["ok"] and len(wired.invokes) == 1
    assert out["result"]["gate"]["over_budget"], out
    assert out["result"]["gate"]["gating_usd"] > wired.m.APPROVAL_LIMIT_USD


def test_lowering_the_limit_re_gates_an_already_clean_estimate(blocking, monkeypatch):
    """The same re-derivation, with enforcement on: the stored verdict cannot clear it."""
    eid = _priced_clean_under_a_high_limit(blocking, monkeypatch)
    out = blocking.m.start_run({"estimate_id": eid})
    assert out.get("status_code") == 409, out
    assert not blocking.invokes


def test_an_under_limit_estimate_cannot_launch_twice_either(wired):
    """The terminal check has two branches — the approval path and the under-limit path.
    A cheap run is the one nobody thinks to guard, and double-launching it still spends
    twice and double-counts once in the variance report."""
    eid = _mk_estimate(wired)["estimate_id"]
    assert wired.m.start_run({"estimate_id": eid})["ok"]
    second = wired.m.start_run({"estimate_id": eid})
    assert second.get("status_code") == 409, second
    assert len(wired.invokes) == 1


def test_an_under_limit_rejected_estimate_cannot_launch(wired):
    """Being under the limit does not make a refusal advisory."""
    eid = _mk_estimate(wired)["estimate_id"]
    _submit(wired, eid)
    wired.m.decide_approval(
        {"estimate_id": eid, "decision": "reject", "reason": "not now"},
        {"username": "boss", "groups": [wired.m.APPROVER_GROUP]}, "t")
    r = wired.m.start_run({"estimate_id": eid})
    assert r.get("status_code") == 409, r
    assert not wired.invokes


def _stale_cumulative(w):
    """An estimate priced when the project had spent nothing, launched after it has spent
    a dollar short of the cumulative reference — the same run, a different exposure.

    A dollar short, derived: as a literal $1,999 this sat just under a $2,000 reference,
    and against $20,000 it stopped crossing anything at all, so the launch succeeded and
    the blocking half of this pair asserted `status_code` on a 200."""
    eid = _mk_estimate(w, {"sample_count": 2000})["estimate_id"]
    assert json.loads(w.est.items[eid]["gate"])["over_budget"] == []
    w.act.put_item(Item={"project": w.m.PROJECT,
                         "sk": "2026-07-30#run-old#sagemaker_training",
                         "cost_usd": str(w.m.CUMULATIVE_LIMIT_USD - 1),
                         "settlement": "settled"})
    return eid


def test_a_stale_under_limit_verdict_is_re_derived_at_launch(wired):
    """Advisory mode still re-reads project-to-date at launch, so the cumulative drip
    is reported on the run that crosses the line rather than on none of them."""
    eid = _stale_cumulative(wired)
    r = wired.m.start_run({"estimate_id": eid})
    assert r["ok"]
    assert any("to-date" in x for x in r["result"]["gate"]["over_budget"]), r


def test_a_stale_under_limit_verdict_is_re_gated_at_launch_when_blocking(blocking):
    eid = _stale_cumulative(blocking)
    r = blocking.m.start_run({"estimate_id": eid})
    assert r["status_code"] == 409, r
    assert not blocking.invokes


# ── rollup and variance ───────────────────────────────────────────────────────
def _seed_actuals(w):
    rows = [
        ("2026-07-29#run-a#sagemaker_training", "sagemaker_training",
         "Amazon SageMaker", "10.77", "settled"),
        ("2026-07-29#run-a#bedrock_teacher", "bedrock_teacher",
         "Amazon Bedrock", "3.20", "settled"),
        ("2026-07-30#run-b#sagemaker_training", "sagemaker_training",
         "Amazon SageMaker", "5.00", "provisional"),
    ]
    for sk, cat, svc, cost, settle in rows:
        w.act.put_item(Item={"project": w.m.PROJECT, "sk": sk, "category": cat,
                             "service": svc, "cost_usd": cost, "settlement": settle,
                             "resource_id": "training-job/llmops-qlora-x"})


def test_rollup_keeps_settled_and_provisional_apart(wired, monkeypatch):
    """Cost Explorer lags ~24 h. One blended total invites quoting a figure that has
    not landed."""
    monkeypatch.setattr(wired.m, "boto3", types.SimpleNamespace(
        client=lambda *a, **k: _StubClient("budgets")))
    _seed_actuals(wired)
    ov = wired.m.cost_overview()
    assert ov["settled_usd"] == 13.97
    assert ov["provisional_usd"] == 5.0
    assert ov["total_usd"] == 18.97


def test_rollup_itemises_by_category_service_and_run(wired):
    _seed_actuals(wired)
    ov = wired.m.cost_overview()
    assert ov["by_category"]["sagemaker_training"] == 15.77
    assert ov["by_service"]["Amazon Bedrock"] == 3.2
    assert ov["by_run"]["run-a"] == 13.97


def test_rollup_states_that_attribution_is_by_resource_not_service(wired):
    """The measured contamination: this account also bills SageMaker Canvas ($296) and
    a JumpStart Whisper endpoint ($36.36/day). A rollup silent about its method invites
    the reader to assume it covers the whole service."""
    ov = wired.m.cost_overview()
    assert "NEVER BY SERVICE" in ov["note"].upper()
    assert "24" in ov["note"]


def test_audit_rows_are_separated_from_cost_rows_in_the_rollup(wired):
    _seed_actuals(wired)
    wired.act.put_item(Item={"project": wired.m.PROJECT,
                             "sk": "2026-07-30#audit#reconcile", "task": "reconcile",
                             "status": "completed"})
    ov = wired.m.cost_overview()
    assert len(ov["audit_rows"]) == 1
    assert all("#audit#" not in r["period"] for r in ov["line_items"])


def test_variance_names_the_driving_category(wired):
    """One aggregate "40% off" tells nobody what to fix."""
    est = [{"id": "est-1", "run_id": "run-a", "total_usd": 8.0, "confidence": "modelled",
            "subtotals": {"sagemaker_training": 8.0, "bedrock_teacher": 0.0}}]
    _seed_actuals(wired)
    v = wired.m.cost_variance(estimates=est, overview=wired.m.cost_overview())
    row = v["variance"][0]
    assert row["run_id"] == "run-a"
    assert row["driver"] == "bedrock_teacher", row
    assert "bedrock_teacher" in row["verdict"]


def test_a_run_with_any_provisional_row_is_not_reported_settled(wired):
    """One provisional row means the total can still move; calling that settled is the
    error this guards."""
    est = [{"id": "e", "run_id": "run-b", "total_usd": 4.0, "subtotals": {}}]
    _seed_actuals(wired)
    v = wired.m.cost_variance(estimates=est, overview=wired.m.cost_overview())
    assert v["variance"][0]["settlement"] == "provisional"
    assert "PROVISIONAL" in v["variance"][0]["verdict"]


def test_variance_reports_runs_that_were_never_estimated(wired):
    """A variance report silent about unestimated spend implies a coverage it lacks."""
    _seed_actuals(wired)
    v = wired.m.cost_variance(estimates=[], overview=wired.m.cost_overview())
    assert v["n_unestimated"] == 2
    assert set(v["unestimated_runs"]) == {"run-a", "run-b"}


def test_estimates_falls_back_to_scan_when_the_gsi_is_missing(wired):
    """A fresh deployment has the table but an unbackfilled GSI. Showing an empty queue
    would read as "nothing awaiting approval"."""
    eid = _mk_estimate(wired)["estimate_id"]
    _submit(wired, eid)
    wired.est.query_should_fail = True
    out = wired.m.cost_estimates()
    assert [e["id"] for e in out["pending"]] == [eid]


def test_estimates_exposes_the_limits_and_the_approver_group(wired):
    out = wired.m.cost_estimates()
    assert out["limits"]["single_usd"] == wired.m.APPROVAL_LIMIT_USD
    assert out["limits"]["approver_group"] == wired.m.APPROVER_GROUP


# ── rate card health ──────────────────────────────────────────────────────────
def test_rate_card_health_reports_required_but_missing_skus(wired):
    """Health is measured against what a plan NEEDS. A card with plenty of irrelevant
    rates and no teacher price is not healthy."""
    h = wired.m.rate_card_health({"sample_count": 100})
    assert h["present"] and h["healthy"] is False
    assert any("deepseek" in s for s in h["missing"])


def test_rate_card_health_says_so_when_there_is_no_card(wired, monkeypatch):
    monkeypatch.setattr(wired.m, "_rate_card", lambda: None)
    h = wired.m.rate_card_health()
    assert h["present"] is False and h["healthy"] is False
    assert "pricing_refresh" in h["warning"]


# ── on-demand finops trigger ──────────────────────────────────────────────────
def test_finops_run_accepts_only_the_three_known_tasks(wired):
    for task in ("reconcile", "pricing_refresh", "report"):
        assert wired.m.finops_run({"task": task})["ok"]
    bad = wired.m.finops_run({"task": "delete_everything"})
    assert bad["status_code"] == 400
    assert len(wired.invokes) == 3


def test_finops_run_does_not_leak_the_lambda_status_as_an_http_code(wired):
    """Lambda's async ack is 202. Named status_code it would become this route's HTTP
    response code, so the field is deliberately called something else."""
    r = wired.m.finops_run({"task": "reconcile"})
    assert "status_code" not in r
    assert r["invoke_status"] == 202


# ── HTTP layer ────────────────────────────────────────────────────────────────
def test_resp_result_honours_an_explicit_status_code(console):
    r = console._resp_result({"error": "nope", "status_code": 403})
    assert r["statusCode"] == 403
    assert "status_code" not in json.loads(r["body"])


def test_resp_result_leaves_pre_existing_routes_at_200(console):
    """The seven POST routes that predate the cost tab all return {"error": ...} with a
    200 and a frontend that reads j.error. Inferring 4xx from an error key would
    silently re-code every one of them."""
    r = console._resp_result({"error": "start-pipeline failed"})
    assert r["statusCode"] == 200


def test_cost_routes_are_registered_and_public_reads_stay_public(console):
    src = (REPO / "deploy/console/lambda_function.py").read_text()
    for p in ("/api/cost-overview", "/api/cost-estimates", "/api/cost-estimate",
              "/api/cost-approval-request", "/api/cost-approval", "/api/finops-run"):
        assert f'"{p}"' in src, p
    # The two GETs are read-only and sit above the POST auth block; the four POSTs sit
    # below it. If a POST route ever moved above, it would become unauthenticated.
    get_block = src.split("raw = event.get(\"body\")")[0]
    assert "/api/cost-overview" in get_block and "/api/cost-estimates" in get_block
    post_block = src.split("if not _authed(headers)")[-1] if "if not _authed(headers)" in src \
        else src.split("if user is None")[-1]
    for p in ("/api/cost-estimate", "/api/cost-approval-request", "/api/cost-approval",
              "/api/finops-run"):
        assert p in post_block, f"{p} must be inside the authenticated POST block"


def test_the_consoles_fallback_limits_equal_the_canonical_ones():
    """The two literals in `lambda_function.py` must equal cost_model's DEFAULT_*_LIMIT_USD.

    They cannot be imported there: `cost_model` is loaded lazily by `_cost_model()` (the
    zip may be built without it) and that needs `_HERE`, defined further down the file. So
    the console keeps its own copy of the number, and this test is the thing that compares
    them. Two copies of a number with nothing comparing them is exactly how one falsified
    figure survived in four files at once; a copy is only safe when a disagreement fails
    loudly.

    Asserted against the SOURCE literal, not `console.APPROVAL_LIMIT_USD`: the module
    attribute is `os.environ.get(..., "20000")`, so an env var set in the runner's shell
    would make this pass while the fallback baked into the deployment package disagreed.
    The fallback is precisely the value that applies when nothing is set.
    """
    import cost_model
    src = (REPO / "deploy/console/lambda_function.py").read_text()
    found = dict(re.findall(
        r'^(APPROVAL_LIMIT_USD|CUMULATIVE_LIMIT_USD) = '
        r'float\(os\.environ\.get\("\1", "([0-9.]+)"\)\)', src, re.M))
    assert set(found) == {"APPROVAL_LIMIT_USD", "CUMULATIVE_LIMIT_USD"}, found
    assert float(found["APPROVAL_LIMIT_USD"]) == cost_model.DEFAULT_SINGLE_RUN_LIMIT_USD
    assert float(found["CUMULATIVE_LIMIT_USD"]) == \
        cost_model.DEFAULT_PROJECT_CUMULATIVE_LIMIT_USD


def test_the_deploy_script_sets_both_limits_and_reads_them_from_cost_model():
    """A deploy that sets neither limit agrees with the code default by luck.

    That was the state until 2026-08-02: `deploy.sh` set `BUDGET_MODE` but neither limit,
    so the live function reported `APPROVAL_LIMIT_USD: null` and fell back to the console
    literal -- which happened to agree, so nothing was wrong and nothing could have told
    us when it stopped agreeing. Retyping the numbers here would be the same hazard with
    an extra copy, so the script DERIVES both from the bundled `cost_model.py`; this test
    pins that it derives rather than types.
    """
    sh = (REPO / "deploy/console/deploy.sh").read_text()
    assert "DEFAULT_SINGLE_RUN_LIMIT_USD" in sh and \
        "DEFAULT_PROJECT_CUMULATIVE_LIMIT_USD" in sh, \
        "deploy.sh must read the limits out of cost_model, not retype them"
    env_line = [l for l in sh.splitlines() if l.startswith("ENV_VARS=")]
    assert len(env_line) == 1, env_line
    assert "APPROVAL_LIMIT_USD=$APPROVAL_LIMIT_USD" in env_line[0]
    assert "CUMULATIVE_LIMIT_USD=$CUMULATIVE_LIMIT_USD" in env_line[0]
    # No bare four-or-five-digit dollar amount assigned to either var: that would be a
    # third copy of the reference, and the one that decides what the LIVE function uses.
    assert not re.search(r'^(APPROVAL|CUMULATIVE)_LIMIT_USD="?[0-9]{4}', sh, re.M), sh


def test_the_finops_runtime_is_in_the_watched_fleet(console):
    assert "llmops_finops" in console.HARNESS_NAMES
    assert len(console.HARNESS_NAMES) == 7
    assert "harness_llmops_finops" in console.WATCHED_RUNTIMES


def test_authed_user_denies_when_the_group_lookup_fails(console, monkeypatch):
    """A throttled Cognito call must not become an approval. GetUser returns the
    username but NOT group membership, and a bearer access token carries no
    cognito:groups claim — so the groups list comes from a second call that can fail."""
    class _Cog:
        def get_user(self, AccessToken):
            return {"Username": "alice"}

        def admin_list_groups_for_user(self, **kw):
            raise RuntimeError("ThrottlingException")

    monkeypatch.setattr(console, "cognito", _Cog())
    monkeypatch.setattr(console, "COGNITO_POOL_ID", "us-east-1_test")
    # sub joined the payload for approval-record identity; empty when Cognito
    # returns no attributes — the assertion below pins groups=[] (the deny), not sub.
    u = console._authed_user({"authorization": "Bearer tok"})
    assert u == {"username": "alice", "groups": [], "sub": ""}
    # The caller then denies, because "we could not prove you are an approver" belongs
    # on the deny side.
    import cost_model
    v = cost_model.check_approval({"requested_by": "bob", "status": "pending_approval"},
                                  "alice", u["groups"])
    assert v["allowed"] is False and v["code"] == 403


def test_authed_user_returns_none_without_a_bearer_token(console):
    assert console._authed_user({}) is None
    assert console._authed_user({"authorization": "Basic abc"}) is None


# ── frontend ──────────────────────────────────────────────────────────────────
FRONTEND = (REPO / "deploy/console/frontend.html").read_text()


def test_frontend_registers_the_cost_tab():
    assert 'data-tab="cost"' in FRONTEND
    assert '"opts","cost"' in FRONTEND.replace(" ", "")
    assert 'if (t==="cost") loadCost();' in FRONTEND


def test_frontend_has_all_five_cost_panels():
    for anchor in ("cEstOut", "cQueue", "cRollup", "cVariance", "cRates"):
        assert f'id="{anchor}"' in FRONTEND, anchor


def test_frontend_grid_classes_used_by_the_cost_panels_exist():
    """col-5/col-7 are new. Without the CSS rule the panels collapse to full width and
    the tab silently looks broken."""
    for cls in ("col-5", "col-7"):
        assert f".{cls} {{ grid-column" in FRONTEND, cls
        assert f'class="card {cls}"' in FRONTEND, cls


def test_frontend_separates_401_from_403():
    """403 means the token is fine but the user lacks the right. Collapsing it into 401
    would sign an approver out of a working session and hide "not in the approver
    group" behind "session expired"."""
    assert "if (resp.status===401)" in FRONTEND
    assert "if (resp.status===403)" in FRONTEND


def test_frontend_renders_cost_values_through_the_escaping_sinks():
    """The file's own CSP comment states escaping at the sink is the primary XSS
    control, so the ids that land in inline onclick handlers must use jstr()."""
    assert "decideCost(${jstr(p.id)}" in FRONTEND


def test_frontend_renders_four_distinct_budget_states():
    """The Cost tab is where the budget stops being arithmetic and becomes something a
    person reads, so all four answers the backend can give must be distinguishable:
    unknown, over-and-blocked, over-and-advisory, within. Any two sharing a rendering
    means the operator cannot tell them apart — and three of the four are the ones that
    should give pause, so collapsing them all lands on the reassuring side.
    """
    box = FRONTEND.split("const gateBox")[1].split("$(\"cEstOut\")")[0]
    assert "budget_unknown" in box, "an unpriceable run must not render as under budget"
    assert "NOT checked" in box
    assert "blocking mode" in box          # over AND enforced
    assert "advisory, not blocked" in box  # over but launching — the new default
    assert "Within budget" in box
    # Distinct pill classes, or the four texts still all look the same at a glance.
    assert box.count("pill warn") == 2 and "pill err" in box and "pill ok" in box
    # .warn must actually exist as CSS; a pill with no rule renders unstyled.
    assert ".warn" in FRONTEND.split("</style>")[0]
    # The unknown branch must be tested FIRST: budget_unknown responses carry
    # over_budget=[] and approval_required=false, so any later position lets them fall
    # through to the green "Within budget" pill.
    assert box.index("budget_unknown") < box.index("approval_required")


def test_frontend_warns_about_unpriced_and_unestimated_rather_than_hiding_them():
    assert "UNPRICED SKU" in FRONTEND
    assert "never estimated" in FRONTEND
    assert "provisional" in FRONTEND
