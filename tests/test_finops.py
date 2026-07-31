"""Unit tests for the FinOps orchestration layer — no AWS, all clients injected.

Covers the finops harness config, the driver's handling of the three audit terminal
tools, and the scheduled reconcile Lambda. The cost arithmetic itself is tested in
tests/test_cost_model.py; this file is about the wiring around it.

Run: .venv/bin/python -m pytest tests/test_finops.py -q
"""
from __future__ import annotations

import datetime
import importlib.util
import json
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


driver = _load("harness_driver_fin", "orchestration/harness_driver/handler.py")
reconcile = _load("finops_reconcile", "orchestration/finops_reconcile/handler.py")

HARNESS = json.loads((REPO / "agents/finops/harness.json").read_text())

ENV = {
    "RUNS_TABLE": "llmops-pipeline-runs",
    "EVENTS_TABLE": "llmops-stage-events",
    "ESTIMATES_TABLE": "llmops-cost-estimates",
    "ACTUALS_TABLE": "llmops-cost-actuals",
    "EVENT_BUS": "llmops-pipeline",
    "LLMOPS_SNS_TOPIC": "arn:aws:sns:us-east-1:123456789012:llmops-escalations",
    "DATA_BUCKET": "llmops-data-test",
    "DRIVER_FN": "llmops-harness-driver",
    "PROJECT": "llmops-agentic-system",
    "AWS_REGION": "us-east-1",
}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)


# ── fakes ─────────────────────────────────────────────────────────────────────
class FakeTable:
    def __init__(self, items=None):
        self.items = list(items or [])
        self.puts, self.updates = [], []

    def put_item(self, Item):
        self.puts.append(Item)
        self.items.append(Item)

    def update_item(self, **kw):
        self.updates.append(kw)

    def query(self, **kw):
        return {"Items": self.items}

    def scan(self, **kw):
        return {"Items": self.items}


class FakeDDB:
    def __init__(self, tables=None):
        self.tables = tables or {}

    def Table(self, name):
        return self.tables.setdefault(name, FakeTable())


class FakeS3:
    def __init__(self, present=()):
        self.present = set(present)

    def head_object(self, Bucket, Key):
        if f"s3://{Bucket}/{Key}" not in self.present:
            raise RuntimeError("404")
        return {}


class FakeSNS:
    def __init__(self):
        self.published = []

    def publish(self, **kw):
        self.published.append(kw)


class FakeLambda:
    def __init__(self, payload=None):
        self.calls = []
        self.payload = payload or {"status": "completed"}

    def invoke(self, **kw):
        self.calls.append(kw)
        return {"StatusCode": 202,
                "Payload": json.dumps(self.payload).encode()}


REPORT_URI = "s3://llmops-data-test/finops/cost/report-2026-07-29.json"
RATES_URI = "s3://llmops-data-test/finops/rates/rate_card_latest.json"

FINOPS_EVENT = {"run_id": "finops-2026-07-29", "stage": "finops", "task": "reconcile",
                "harness_id": "llmops_finops",
                "manifest_uri": "s3://llmops-data-test/finops/manifests/2026-07-29.json",
                "params": {"task": "reconcile", "project": "llmops-agentic-system",
                           "period": "2026-07-29"}}


def _clients(present=(REPORT_URI, RATES_URI)):
    return {"s3": FakeS3(present), "ddb": FakeDDB(), "sns": FakeSNS(),
            "sfn": None, "events": None, "lambda": FakeLambda()}


# ── the harness config ────────────────────────────────────────────────────────
def test_harness_declares_the_three_audit_tools_and_the_driver_knows_them():
    """A tool the harness offers but the driver does not service falls into the
    unknown-tool branch and the agent loops until its re-asks run out — the audit
    would silently produce nothing. Asserted as equality, not subset: a shrinking
    driver list is exactly the failure this test exists to catch, and a subset
    assertion is satisfied by it."""
    declared = {t["name"] for t in HARNESS["tools"] if t["type"] == "inline_function"}
    audit = {"publish_cost_report", "update_rate_card", "flag_variance"}
    assert audit <= declared
    assert set(driver.FINOPS_TERMINAL_TOOLS) == audit
    assert audit <= set(HARNESS["allowedTools"])


def test_harness_is_read_only_by_construction_no_browser():
    """The auditor must not be able to change what it audits. It also gets no browser:
    its evidence is billing API text, and a browser widens a read-only role's reach."""
    types = {t["type"] for t in HARNESS["tools"]}
    assert "agentcore_browser" not in types
    assert "agentcore_code_interpreter" in types


def test_harness_prompt_names_every_measured_hazard():
    """These are not decorative warnings — each corresponds to a probe that failed on
    the live account. A prompt that loses one loses the reason the mechanism exists."""
    prompt = HARNESS["systemPrompt"][0]["text"]
    for phrase in ("NEVER BY SERVICE", "Inactive", "LAGS", "provisional",
                   "REALIZED RATES", "NEVER INVENT A RATE", "DO NOT STOP RUNS"):
        assert phrase in prompt, phrase


def test_harness_points_at_the_canonical_cost_model_rather_than_prose_math():
    prompt = HARNESS["systemPrompt"][0]["text"]
    assert "pipeline/contracts/cost_model.py" in prompt


def test_harness_name_and_tags_match_the_seventh_runtime():
    assert HARNESS["harnessName"] == "llmops_finops"
    assert HARNESS["tags"]["agent-type"] == "finops"
    assert HARNESS["tags"]["project"] == "llmops-agentic-system"


# ── IAM: the auditor must not be able to change what it audits ───────────────
def _statements(path, *keys):
    doc = json.loads((REPO / path).read_text())
    for k in keys:
        doc = doc[k]
    return doc["Statement"]


def _actions(statements):
    """Every Action string in a statement list, or in a whole policy document."""
    if isinstance(statements, dict):
        statements = statements["Statement"]
    out = set()
    for s in statements:
        a = s.get("Action", [])
        out.update([a] if isinstance(a, str) else a)
    return out


HARNESS_ROLE = "deploy/iam/harness_execution_role.json"
LAMBDA_ROLES = "deploy/iam/lambda_roles.json"


def test_finops_can_read_the_billing_apis_it_needs():
    """Cost Explorer resource-level is the only path to per-run attribution, and the
    Price List call is what proves the rate feed stale rather than guessing."""
    acts = _actions(_statements(HARNESS_ROLE, "permissionsPolicy"))
    for needed in ("ce:GetCostAndUsage", "ce:GetCostAndUsageWithResources",
                   "pricing:GetProducts", "budgets:DescribeBudgets"):
        assert needed in acts, needed


def test_no_role_can_mutate_billing_configuration():
    """An auditor with write access to cost categories, allocation-tag status, or
    budgets can reshape the numbers it reports on. Every billing verb granted anywhere
    in this repo must be a read."""
    everything = _actions(_statements(HARNESS_ROLE, "permissionsPolicy"))
    lam = json.loads((REPO / LAMBDA_ROLES).read_text())
    for role in lam["roles"].values():
        everything |= _actions(role["permissionsPolicy"])
    console = json.loads((REPO / "deploy/console/iam-policy.json").read_text())
    everything |= _actions(console["Statement"])

    billing = {a for a in everything
               if a.split(":")[0] in ("ce", "pricing", "budgets", "cur", "billing")}
    assert billing, "expected some billing permissions to exist"
    read_verbs = ("Get", "List", "Describe")
    bad = [a for a in billing if not a.split(":", 1)[1].startswith(read_verbs)]
    assert bad == [], f"mutating billing permission granted: {bad}"


def test_finops_cannot_rewrite_an_approval_decision():
    """The gate is only a gate if the audited party cannot flip its own verdict. The
    agent reads estimates to compare against and must never write them."""
    for s in _statements(HARNESS_ROLE, "permissionsPolicy"):
        res = s.get("Resource", "")
        res = [res] if isinstance(res, str) else res
        if any("llmops-cost-estimates" in r for r in res):
            acts = _actions([s])
            assert not {a for a in acts if a.split(":", 1)[1].startswith(
                ("Put", "Update", "Delete", "BatchWrite"))}, acts


def test_the_driver_can_write_the_findings_it_records():
    """handle_finops_tool PutItems into the actuals table; without this grant the
    audit's own findings fail with AccessDenied while the rest of the turn succeeds."""
    lam = json.loads((REPO / LAMBDA_ROLES).read_text())
    acts, tables = set(), set()
    for s in lam["roles"]["driver"]["permissionsPolicy"]["Statement"]:
        res = s.get("Resource", "")
        res = [res] if isinstance(res, str) else res
        if any("llmops-cost-actuals" in r for r in res):
            acts |= _actions([s])
            tables |= {r for r in res if "llmops-cost-actuals" in r}
    assert "dynamodb:PutItem" in acts
    assert tables


def test_the_reconcile_lambda_has_a_role_that_can_invoke_the_driver():
    lam = json.loads((REPO / LAMBDA_ROLES).read_text())
    assert "finops_reconcile" in lam["roles"]
    acts = _actions(lam["roles"]["finops_reconcile"]["permissionsPolicy"])
    assert "lambda:InvokeFunction" in acts


# ── deployability: a config nothing deploys is a component that does not exist ──

def _deploy_src(name):
    return (REPO / "deploy" / name).read_text()


def test_every_harness_config_on_disk_is_named_by_the_deploy_script():
    """05_harnesses.py's AGENTS list is what a bare run creates and what --agent
    validates against, so a config it does not name is a harness that silently never
    exists. This was live: agents/finops/harness.json shipped complete, `--agent
    finops` was rejected as an invalid choice, and the fleet stayed at six while
    every doc said seven."""
    on_disk = {p.parent.name for p in (REPO / "agents").glob("*/harness.json")}
    # Read the list the script itself defines rather than re-parsing the source: a
    # string-split parser has to model Python's comment and quoting rules, and the
    # first version of this test silently dropped the very entry it was written to
    # catch because a `#` comment sits inside the literal.
    spec = importlib.util.spec_from_file_location(
        "harnesses_deploy", REPO / "deploy/05_harnesses.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    missing = on_disk - set(mod.AGENTS)
    assert not missing, f"harness configs with no deploy path: {missing}"


def test_every_scheduled_function_is_one_the_lambda_deployer_creates():
    """08_triggers.py schedules llmops-finops-daily against a function name. If
    07_lambdas.py does not create that function the schedule fires daily into a
    ResourceNotFound — visible only in the scheduler's own metrics, never in the
    dashboard, so the auditor appears to be running while nothing runs."""
    triggers, lambdas = _deploy_src("08_triggers.py"), _deploy_src("07_lambdas.py")
    assert "llmops-finops-reconcile" in triggers, "test guards the wrong name"
    assert '"fn": "llmops-finops-reconcile"' in lambdas, \
        "scheduled function has no entry in LAMBDAS"


def test_a_role_change_reaches_an_existing_function_not_only_a_new_one():
    """update_function_configuration silently ignores a role it is not passed, so
    without Role= a tightened role applies only to functions that do not exist yet:
    every re-run reports "updated" while the live function keeps the role it was born
    with. Measured live — llmops-finops-reconcile stayed on llmops-lambda-driver
    across a successful "updated" run."""
    src = _deploy_src("07_lambdas.py")
    upd = src.split("update_function_configuration(", 1)[1].split(")", 1)[0]
    assert "Role=role_arn" in upd, "role change would not reach an existing function"


def test_the_auditor_lambda_runs_under_its_own_role_not_the_drivers():
    """Reusing the driver role works and is what a first pass reaches for. It also
    hands the auditor every permission the thing it audits has."""
    src = _deploy_src("07_lambdas.py")
    finops = src.split('"finops": {', 1)[1].split("},", 1)[0]
    assert "/llmops/iam/lambda_finops_reconcile_arn" in finops
    assert "/llmops/iam/lambda_driver_arn" not in finops


# ── driver: publish_cost_report ───────────────────────────────────────────────
def test_report_with_a_missing_s3_artifact_is_rejected_not_recorded():
    """Trust-but-verify, same as stage_complete: a report the agent claims but never
    wrote leaves a dashboard panel pointing at a 404."""
    c = _clients(present=())
    out = driver.handle_finops_tool(c, FINOPS_EVENT, "publish_cost_report", {
        "report_uri": REPORT_URI, "period": "2026-07-29", "total_usd": 10.77,
        "settlement": "settled", "headline": "x"})
    assert out["ok"] is False
    assert "not in S3" in out["reason"]
    assert c["ddb"].Table(ENV["ACTUALS_TABLE"]).puts == []


def test_report_without_a_settlement_state_is_rejected():
    """A cost number with no settlement state cannot be read safely: the reader cannot
    tell whether it will still move."""
    c = _clients()
    out = driver.handle_finops_tool(c, FINOPS_EVENT, "publish_cost_report", {
        "report_uri": REPORT_URI, "period": "2026-07-29", "total_usd": 10.77,
        "headline": "x"})
    assert out["ok"] is False
    assert "provisional" in out["reason"]


def test_report_with_an_invented_settlement_value_is_rejected():
    c = _clients()
    out = driver.handle_finops_tool(c, FINOPS_EVENT, "publish_cost_report", {
        "report_uri": REPORT_URI, "period": "2026-07-29", "total_usd": 1.0,
        "settlement": "final", "headline": "x"})
    assert out["ok"] is False


def test_a_valid_provisional_report_is_accepted_and_recorded():
    c = _clients()
    out = driver.handle_finops_tool(c, FINOPS_EVENT, "publish_cost_report", {
        "report_uri": REPORT_URI, "period": "2026-07-29", "total_usd": 10.77,
        "settlement": "provisional", "excluded_usd": 314.18, "headline": "ok"})
    assert out["ok"] is True
    row = c["ddb"].Table(ENV["ACTUALS_TABLE"]).puts[0]
    assert row["project"] == "llmops-agentic-system"
    assert row["sk"].startswith("2026-07-29#finding#publish_cost_report")


# ── driver: flag_variance ─────────────────────────────────────────────────────
def test_variance_without_a_driver_category_is_rejected():
    """One aggregate percentage says the estimate was wrong without saying what to
    fix, and improving the next estimate is the entire point of reconciling."""
    c = _clients()
    out = driver.handle_finops_tool(c, FINOPS_EVENT, "flag_variance", {
        "run_id": "run-a", "actual_usd": 30.0, "variance_pct": 200.0})
    assert out["ok"] is False
    assert "driver" in out["reason"]


def test_a_flagged_variance_notifies_a_human_and_is_recorded():
    c = _clients()
    out = driver.handle_finops_tool(c, FINOPS_EVENT, "flag_variance", {
        "run_id": "run-a", "estimate_usd": 10.0, "actual_usd": 30.0,
        "variance_pct": 200.0, "driver": "sagemaker_training",
        "recommendation": "raise the throughput constant"})
    assert out["ok"] is True
    assert len(c["sns"].published) == 1
    assert "run-a" in c["sns"].published[0]["Subject"]
    assert c["ddb"].Table(ENV["ACTUALS_TABLE"]).puts[0]["sk"].endswith("#run-a")


def test_a_notify_failure_still_keeps_the_variance_row():
    """The finding is the durable artifact; SNS is the convenience. Losing the row
    because a topic was misconfigured would lose the audit."""
    class DeadSNS:
        def publish(self, **kw):
            raise RuntimeError("no topic")
    c = _clients()
    c["sns"] = DeadSNS()
    out = driver.handle_finops_tool(c, FINOPS_EVENT, "flag_variance", {
        "run_id": "run-a", "actual_usd": 30.0, "variance_pct": 200.0,
        "driver": "sagemaker_training"})
    assert out["ok"] is True
    assert len(c["ddb"].Table(ENV["ACTUALS_TABLE"]).puts) == 1


def test_two_runs_flagged_in_one_turn_produce_two_distinct_rows():
    """An audit covers many runs, so flag_variance must not be terminal — the sort key
    carries run_id precisely so a second finding does not overwrite the first."""
    c = _clients()
    for rid in ("run-a", "run-b"):
        driver.handle_finops_tool(c, FINOPS_EVENT, "flag_variance", {
            "run_id": rid, "actual_usd": 1.0, "variance_pct": 50.0,
            "driver": "sagemaker_training"})
    sks = [p["sk"] for p in c["ddb"].Table(ENV["ACTUALS_TABLE"]).puts]
    assert len(set(sks)) == 2


# ── driver: update_rate_card ───────────────────────────────────────────────────
def test_rate_card_must_exist_in_s3_to_be_accepted():
    c = _clients(present=())
    out = driver.handle_finops_tool(c, FINOPS_EVENT, "update_rate_card", {
        "rates_uri": RATES_URI, "n_rates": 9})
    assert out["ok"] is False


def test_rate_card_records_the_skus_it_could_not_price():
    """The Price List API cannot price DeepSeek-R1 or Fable 5 on this account, so a
    refresh reporting zero missing SKUs is the suspicious outcome, not the good one."""
    c = _clients()
    out = driver.handle_finops_tool(c, FINOPS_EVENT, "update_rate_card", {
        "rates_uri": RATES_URI, "n_rates": 7, "n_stale": 1,
        "missing_skus": ["bedrock:tokens:us.deepseek.r1-v1:0:input"],
        "sources": {"ce_realized": 5, "price_list": 2}})
    assert out["ok"] is True
    detail = json.loads(c["ddb"].Table(ENV["ACTUALS_TABLE"]).puts[0]["detail"])
    assert detail["missing_skus"]


# ── driver: the audit invocation has no run and no token ──────────────────────
def test_finops_escalation_does_not_mint_a_phantom_run_row():
    """A scheduled audit has no row in the runs table. Writing one would surface a
    fake run in the console beside real pipeline runs."""
    c = _clients()
    driver.handle_escalate(c, FINOPS_EVENT, {"reason": "r", "details": "d"})
    assert c["ddb"].Table(ENV["RUNS_TABLE"]).updates == []
    assert len(c["sns"].published) == 1


def test_is_finops_only_matches_the_audit_stage():
    assert driver._is_finops(FINOPS_EVENT) is True
    assert driver._is_finops({"stage": "finetune"}) is False
    assert driver._is_finops({}) is False


# ── reconcile Lambda: period selection ────────────────────────────────────────
def test_default_period_is_behind_the_cost_explorer_lag():
    """CE lags ~24 h; a same-day resource-level query returned Estimated=true with zero
    groups. Reconciling today would read an empty period and report $0."""
    d = datetime.date(2026, 7, 31)
    assert reconcile.default_period(d) == "2026-07-29"
    assert reconcile.DEFAULT_LAG_DAYS >= 2


def test_provisional_periods_are_rechecked_not_left_frozen():
    """Without this the first read of a period wins forever: the settled figure exists
    in Cost Explorer but nothing ever goes back for it."""
    ddb = FakeDDB({ENV["ACTUALS_TABLE"]: FakeTable([
        {"project": "p", "sk": "2026-07-28#run-a#sagemaker_training",
         "settlement": "provisional"},
        {"project": "p", "sk": "2026-07-27#run-b#sagemaker_training",
         "settlement": "settled"},
    ])})
    got = reconcile.unsettled_periods(ddb, "p", today=datetime.date(2026, 7, 31))
    assert got == ["2026-07-28"]


def test_scheduled_run_covers_the_new_day_plus_any_still_provisional_day():
    ddb = FakeDDB({ENV["ACTUALS_TABLE"]: FakeTable([
        {"project": "llmops-agentic-system", "sk": "2026-07-28#run-a#x",
         "settlement": "provisional"}]),
        ENV["ESTIMATES_TABLE"]: FakeTable([])})
    c = {"ddb": ddb, "lambda": FakeLambda(), "sns": FakeSNS()}
    out = reconcile.handler({}, clients=c)
    assert len(out["periods"]) >= 2
    assert "2026-07-28" in out["periods"]


def test_an_explicit_period_overrides_the_schedule_default():
    c = {"ddb": FakeDDB(), "lambda": FakeLambda(), "sns": FakeSNS()}
    out = reconcile.handler({"period": "2026-06-01", "runs": []}, clients=c)
    assert out["periods"] == ["2026-06-01"]


def test_an_unknown_task_is_refused_before_any_invocation():
    c = {"ddb": FakeDDB(), "lambda": FakeLambda(), "sns": FakeSNS()}
    out = reconcile.handler({"task": "delete_everything"}, clients=c)
    assert "error" in out
    assert c["lambda"].calls == []


def test_the_three_supported_tasks_are_exactly_the_documented_ones():
    assert reconcile.TASKS == ("reconcile", "pricing_refresh", "report")


# ── reconcile Lambda: payload ─────────────────────────────────────────────────
def test_payload_carries_region_and_bucket_so_nothing_is_hardcoded():
    """The prompt forbids hardcoding account-specific values, so they must arrive."""
    p = reconcile.build_payload("reconcile", "proj", "2026-07-29", ["run-a"],
                                "bkt", "us-east-1")
    assert p["params"]["bucket"] == "bkt"
    assert p["params"]["region"] == "us-east-1"
    assert p["params"]["rates_uri"].startswith("s3://bkt/finops/rates/")
    assert p["harness_id"] == "llmops_finops"
    assert p["stage"] == "finops"


def test_payload_stage_is_finops_so_the_driver_takes_the_audit_path():
    p = reconcile.build_payload("report", "proj", "2026-07", [], "b", "us-east-1")
    assert driver._is_finops(p) is True


def test_a_failed_run_lookup_degrades_the_comparison_not_the_actuals():
    """Attribution is by resource pattern, so actuals survive an empty run list; only
    the estimate-vs-actual comparison narrows. Skipping the whole period instead would
    lose the day's cost data over a metadata read."""
    class AngryDDB(FakeDDB):
        def Table(self, name):
            if name == ENV["ESTIMATES_TABLE"]:
                raise RuntimeError("throttled")
            return super().Table(name)
    c = {"ddb": AngryDDB(), "lambda": FakeLambda(), "sns": FakeSNS()}
    out = reconcile.handler({"period": "2026-07-29"}, clients=c)
    assert out["results"][0]["status"] == "invoked"
    assert out["results"][0]["n_runs"] == 0


def test_outcome_is_recorded_so_a_missed_daily_reconcile_is_visible():
    ddb = FakeDDB()
    c = {"ddb": ddb, "lambda": FakeLambda(), "sns": FakeSNS()}
    reconcile.handler({"period": "2026-07-29", "runs": []}, clients=c)
    puts = ddb.Table(ENV["ACTUALS_TABLE"]).puts
    assert puts and puts[0]["sk"] == "2026-07-29#audit#reconcile"


def test_audit_rows_cannot_collide_with_cost_rows():
    """Both live in the actuals table under (project, period#...). The reserved
    '#audit#'/'#finding#' segments are what keep them apart, since no run_id can
    contain '#'."""
    ddb = FakeDDB()
    c = {"ddb": ddb, "lambda": FakeLambda(), "sns": FakeSNS()}
    reconcile.handler({"period": "2026-07-29", "runs": []}, clients=c)
    audit_sk = ddb.Table(ENV["ACTUALS_TABLE"]).puts[0]["sk"]
    cost_sk = "2026-07-29#run-a#sagemaker_training"
    assert audit_sk != cost_sk
    assert "#audit#" in audit_sk and "#audit#" not in cost_sk


def test_sync_mode_surfaces_the_drivers_own_result():
    """The console's on-demand button needs the outcome, not just an ack."""
    c = {"ddb": FakeDDB(), "lambda": FakeLambda({"status": "completed", "tool": "x"}),
         "sns": FakeSNS()}
    out = reconcile.handler({"period": "2026-07-29", "runs": [], "sync": True},
                            clients=c)
    assert out["results"][0]["status"] == "completed"
    assert c["lambda"].calls[0]["InvocationType"] == "RequestResponse"


def test_scheduled_mode_is_async_so_the_scheduler_is_not_held_open():
    c = {"ddb": FakeDDB(), "lambda": FakeLambda(), "sns": FakeSNS()}
    reconcile.handler({"period": "2026-07-29", "runs": []}, clients=c)
    assert c["lambda"].calls[0]["InvocationType"] == "Event"


def test_only_launched_or_reconciled_estimates_become_reconcile_targets():
    """A draft or rejected estimate never spent anything; comparing against one would
    manufacture a variance for a run that does not exist."""
    ddb = FakeDDB({ENV["ESTIMATES_TABLE"]: FakeTable([
        {"run_id": "run-live", "status": "launched", "project": "p"},
        {"run_id": "run-done", "status": "reconciled", "project": "p"},
        {"run_id": "run-draft", "status": "draft", "project": "p"},
        {"run_id": "run-no", "status": "rejected", "project": "p"},
        {"run_id": "run-wait", "status": "pending_approval", "project": "p"},
    ])})
    assert reconcile.runs_in_period(ddb, "p", "2026-07-29") == ["run-done", "run-live"]
