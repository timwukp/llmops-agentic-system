"""Unit tests for the console's Cost tab — no AWS, every client patched.

Scope: the HTTP layer, the advisory $20,000 reference, rate-card health and finops
dispatch as the console serves them. The v1 estimator, approval queue and rollups were
removed (Cost v2 replaced them; see docs/COST.md section 8); Cost v2 visibility and
governance are tested in tests/test_console_cost_v2.py, the cost arithmetic in
tests/test_cost_model.py and the agent wiring in tests/test_finops.py.

The console module is imported with boto3 stubbed, because it builds ~12 clients and
calls sts:GetCallerIdentity at import time. That is fine in Lambda and impossible in a
unit test, so the fake is installed before the import rather than patched after.

Run: .venv/bin/python -m pytest tests/test_console_cost.py -q
"""
from __future__ import annotations

import ast
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


# ── the advisory $20,000 reference (recorded in every signed approval; never blocks) ──
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
# ── launching ─────────────────────────────────────────────────────────────────
def test_a_run_with_no_estimate_still_launches(wired):
    """Every run before this feature launched without an estimate; keeping that legal
    is what lets the variance report state honestly what was never estimated."""
    r = wired.m.start_run({"sample_count": 100, "note": "ad hoc"})
    assert r["ok"] and len(wired.invokes) == 1


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


V1_COST_ROUTES = ("/api/cost-overview", "/api/cost-estimates", "/api/cost-estimate",
                  "/api/cost-approval-request", "/api/cost-approval")


def test_the_v1_cost_routes_are_gone():
    """Removed with the v1 panels. A route that survives its UI is an unauthenticated read
    (the two GETs) or a write nothing in the portal can reach any more."""
    src = (REPO / "deploy/console/lambda_function.py").read_text()
    for p in V1_COST_ROUTES:
        assert f'path == "{p}"' not in src, p
    for fn in ("create_estimate", "request_approval", "decide_approval", "cost_estimates",
               "cost_overview", "cost_variance"):
        assert f"def {fn}(" not in src, fn
    for p in V1_COST_ROUTES:
        assert f'"{p}"' not in FRONTEND, p


def test_cost_routes_are_registered_and_reads_stay_public_writes_authed(console):
    src = (REPO / "deploy/console/lambda_function.py").read_text()
    get_block = src.split("raw = event.get(\"body\")")[0]
    for p in ("/api/cost/v2", "/api/cost/budgets"):
        assert f'path == "{p}"' in get_block, f"{p} must be a public read"
    post_block = src.split('if method == "POST":')[-1]
    for p in ("/api/cost/budgets", "/api/cost/policy", "/api/cost/sweep", "/api/finops-run"):
        assert f'path == "{p}"' in post_block, f"{p} must be inside the authenticated POST block"


def test_frontend_registers_the_cost_tab():
    assert 'data-tab="cost"' in FRONTEND
    assert '"opts","cost"' in FRONTEND.replace(" ", "")
    assert 'if (t==="cost") loadCostV2();' in FRONTEND
    assert "loadCost()" not in FRONTEND


def test_frontend_keeps_rate_card_health_on_the_detail_sub_tab():
    """Every Cost v2 figure is priced from the rate card, so its health moved next to the
    line items rather than leaving with the v1 panels."""
    i = FRONTEND.index('id="cRates"')
    card = FRONTEND[FRONTEND.rindex("<div data-tab-panel", 0, i):i]
    assert 'data-csub="detail"' in card
    assert "finopsRun('pricing_refresh')" in card and "finopsRun('reconcile')" in card
    assert "renderRateCard((v.detail||{}).rate_card)" in FRONTEND


def test_frontend_renders_task_ids_through_the_escaping_sink():
    """The file's own CSP comment states escaping at the sink is the primary XSS
    control, so an id that lands in an inline onclick handler must use jstr()."""
    assert "selectTask(${jstr(t.task_id)})" in FRONTEND


def test_frontend_says_unpriced_rather_than_pricing_at_zero():
    assert 'pill warn">unpriced' in FRONTEND
