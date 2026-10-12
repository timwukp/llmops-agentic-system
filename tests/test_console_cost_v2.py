"""Cost v2 governance. The enforcement policy is two visible switches, both OFF by default:
with both off a breach is an alarm (alert row + notification) and nothing is refused or
stopped. These tests pin that default, what each switch does when ON, and that the sweep's
alarm fires once per threshold rather than every 15 minutes. Reuses test_console_cost's
import-time isolation (boto3 stubbed before the console module loads)."""
from __future__ import annotations

import json
import pathlib
import re

import pytest

from test_console_cost import _StubTable, console  # noqa: F401  (fixture)

REPO = pathlib.Path(__file__).resolve().parents[1]
APPROVER = {"username": "boss", "groups": ["llmops-approver"]}


class _FakeSfn:
    def __init__(self, running=()):
        self.running, self.stopped = list(running), []

    def list_executions(self, **kw):
        return {"executions": [{"executionArn": f"arn:exec:{r}", "name": r} for r in self.running]}

    def describe_execution(self, executionArn):
        return {"input": json.dumps({"run_id": executionArn.split(":")[-1]})}

    def stop_execution(self, **kw):
        self.stopped.append(kw["executionArn"])


class _FakeSm:
    def __init__(self):
        self.stopped = []

    def stop_training_job(self, TrainingJobName):
        self.stopped.append(TrainingJobName)


@pytest.fixture
def gov(console, monkeypatch):  # noqa: F811
    tbl = _StubTable("LlmopsAdminRuns")
    monkeypatch.setattr(console, "console_tbl", tbl)
    monkeypatch.setattr(console, "LLMOPS_SNS_TOPIC", "")
    state = {"mtd": 90.0, "jobs": []}
    monkeypatch.setattr(console, "cost_v2", lambda days=30: {
        "overview": {"mtd_usd": state["mtd"]}, "detail": {"line_items": state["jobs"]}})
    sfn, sm = _FakeSfn(), _FakeSm()
    monkeypatch.setattr(console, "sfn", sfn)
    monkeypatch.setattr(console, "sm", sm)
    return console, tbl, state, sfn, sm


def _budget(tbl, name, scope, limit):
    tbl.put_item({"id": f"budget#{name}", "name": name, "scope": scope,
                  "limit_usd": str(limit), "thresholds": "[50,80,100]"})


def _alerts(tbl):
    return [v for k, v in tbl.items.items() if str(k).startswith("budget-alert#")]


# ── the policy ────────────────────────────────────────────────────────────────
def test_policy_defaults_to_both_switches_off(gov):
    c, *_ = gov
    pol = c.get_cost_policy()
    assert pol["block_launches"] is False and pol["stop_running"] is False


def test_only_an_approver_may_flip_a_switch(gov):
    c, tbl, *_ = gov
    r = c.set_cost_policy({"block_launches": True}, {"username": "ds", "groups": ["llmops-datascience"]})
    assert r["status_code"] == 403
    assert c.get_cost_policy()["block_launches"] is False


def test_a_flip_is_persisted_and_written_to_the_alert_feed(gov):
    c, tbl, *_ = gov
    r = c.set_cost_policy({"stop_running": "on"}, APPROVER)
    assert r["ok"] and r["changed"] == ["stop_running ON"]
    assert c.get_cost_policy()["stop_running"] is True
    assert any("boss set stop_running ON" in a["message"] for a in _alerts(tbl))


# ── the launch gate ───────────────────────────────────────────────────────────
def test_no_budgets_means_the_gate_allows(gov):
    c, tbl, *_ = gov
    assert c.budget_gate(1_000_000)["allowed"] is True
    assert _alerts(tbl) == []


def test_breach_with_block_off_alarms_and_lets_the_launch_proceed(gov):
    c, tbl, *_ = gov
    _budget(tbl, "monthly", "project_monthly", 100)
    g = c.budget_gate(20, "start-run")          # 90 MTD + 20 > 100
    assert g["allowed"] is True and g["breaches"][0]["blocked"] is False
    assert [a["severity"] for a in _alerts(tbl)] == ["WARNING"]


def test_breach_with_block_on_refuses_with_409(gov):
    c, tbl, *_ = gov
    _budget(tbl, "monthly", "project_monthly", 100)
    c.set_cost_policy({"block_launches": True}, APPROVER)
    g = c.budget_gate(20, "start-run")
    assert g["allowed"] is False
    r = c._gate_refusal(g)
    assert r["status_code"] == 409 and "monthly" in r["error"]


def test_block_on_within_limit_allows(gov):
    c, tbl, *_ = gov
    _budget(tbl, "monthly", "project_monthly", 100)
    c.set_cost_policy({"block_launches": True}, APPROVER)
    assert c.budget_gate(5)["allowed"] is True     # 90 + 5 <= 100


def test_per_task_budget_checks_the_launch_estimate_only(gov):
    c, tbl, *_ = gov
    _budget(tbl, "task-cap", "per_task", 50)
    c.set_cost_policy({"block_launches": True}, APPROVER)
    assert c.budget_gate(49)["allowed"] is True
    assert c.budget_gate(51)["allowed"] is False


# ── the sweep: the running-job alarm ──────────────────────────────────────────
def test_sweep_alarms_each_threshold_once_per_month(gov):
    c, tbl, state, *_ = gov
    _budget(tbl, "monthly", "project_monthly", 100)
    state["mtd"] = 85.0                         # crosses 50 and 80, not 100
    assert c.budget_sweep()["alerts"] == 2
    assert c.budget_sweep()["alerts"] == 0      # the schedule must not re-alarm every 15 min
    state["mtd"] = 101.0
    assert c.budget_sweep()["alerts"] == 1      # only the new 100% crossing


def test_sweep_with_stop_off_never_stops_anything(gov):
    c, tbl, state, sfn, sm = gov
    _budget(tbl, "monthly", "project_monthly", 100)
    state["mtd"] = 150.0
    sfn.running = ["run-a"]
    state["jobs"] = [{"run_id": "run-a", "status": "InProgress", "resource_id": "llmops-qlora-a",
                      "cost_usd": 3.0}]
    r = c.budget_sweep()
    assert r["stopped"] == [] and sfn.stopped == [] and sm.stopped == []
    assert any("not stopped (stop_running OFF)" in a["action"] for a in _alerts(tbl))


def test_sweep_with_stop_on_stops_the_run_and_its_running_job(gov):
    c, tbl, state, sfn, sm = gov
    _budget(tbl, "monthly", "project_monthly", 100)
    c.set_cost_policy({"stop_running": True}, APPROVER)
    state["mtd"] = 150.0
    sfn.running = ["run-a"]
    state["jobs"] = [{"run_id": "run-a", "status": "InProgress", "resource_id": "llmops-qlora-a",
                      "cost_usd": 3.0},
                     {"run_id": "run-a", "status": "Completed", "resource_id": "llmops-qlora-old",
                      "cost_usd": 9.0}]
    r = c.budget_sweep()
    assert sfn.stopped == ["arn:exec:run-a"]
    assert sm.stopped == ["llmops-qlora-a"]      # a completed job is not "stopped"
    assert r["stopped"][0]["run_id"] == "run-a"


def test_per_task_sweep_stops_only_the_run_that_overspent(gov):
    c, tbl, state, sfn, sm = gov
    _budget(tbl, "task-cap", "per_task", 10)
    c.set_cost_policy({"stop_running": True}, APPROVER)
    sfn.running = ["run-a", "run-b"]
    state["jobs"] = [{"run_id": "run-a", "status": "InProgress", "resource_id": "j-a", "cost_usd": 12.0},
                     {"run_id": "run-b", "status": "InProgress", "resource_id": "j-b", "cost_usd": 2.0}]
    c.budget_sweep()
    assert sfn.stopped == ["arn:exec:run-a"] and sm.stopped == ["j-a"]


def test_the_scheduled_event_reaches_the_sweep(gov):
    c, *_ = gov
    out = c.handler({"mode": "budget-sweep"}, None)
    assert out["trigger"] == "schedule"


# ── budgets ───────────────────────────────────────────────────────────────────
def test_save_budget_rejects_an_unknown_scope(gov):
    c, *_ = gov
    r = c.save_budget({"name": "x", "scope": "forever", "limit_usd": 10}, {"username": "u"})
    assert r["status_code"] == 400


def test_save_budget_round_trips(gov):
    c, tbl, *_ = gov
    r = c.save_budget({"name": "LLMOps Monthly", "scope": "project_monthly",
                       "limit_usd": "500"}, {"username": "u"})
    assert r["ok"] and "budget#llmops-monthly" in tbl.items
    assert json.loads(tbl.items["budget#llmops-monthly"]["thresholds"]) == [50, 80, 100]


@pytest.mark.parametrize("mtd,state", [(10, "on_track"), (85, None), (120, "over")])
def test_monthly_budget_state(gov, mtd, state):
    c, *_ = gov
    v = c._budget_view({"id": "budget#m", "name": "m", "scope": "project_monthly",
                        "limit_usd": "100"}, mtd)
    if state:
        assert v["state"] == state
    else:  # 85% used: warning, or forecast_over if the run-rate already projects past 100
        assert v["state"] in ("warning", "forecast_over")


# ── the wiring the switches depend on ─────────────────────────────────────────
def test_stop_grant_is_scoped_and_cannot_delete_endpoints():
    pol = json.loads((REPO / "deploy/console/iam-policy.json").read_text())
    st = [s for s in pol["Statement"] if s["Sid"] == "CostGuardrailStop"]
    assert len(st) == 1
    assert sorted(st[0]["Action"]) == ["sagemaker:StopTrainingJob", "states:StopExecution"]
    assert all("*" != r and ("llmops" in r or "arc2v2" in r) for r in st[0]["Resource"])
    actions = [a for s in pol["Statement"] for a in
               (s["Action"] if isinstance(s["Action"], list) else [s["Action"]])]
    assert not any(a.endswith(("DeleteEndpoint", "sagemaker:*", "states:*")) for a in actions)


def test_deploy_schedules_the_sweep_with_the_input_the_handler_reads():
    sh = (REPO / "deploy/console/deploy.sh").read_text()
    assert "rate(15 minutes)" in sh and "llmops-budget-sweep" in sh
    m = re.search(r'Input=\'(\{.*?\})\'', sh)
    assert m and json.loads(m.group(1).replace('\\"', '"')) == {"mode": "budget-sweep"}


def test_portal_shows_both_switches_off_by_default():
    fe = (REPO / "deploy/console/frontend.html").read_text()
    for sid, key in (("swBlock", "block_launches"), ("swStop", "stop_running")):
        assert f'id="{sid}" role="switch" aria-checked="false"' in fe
        assert f"togglePolicy('{key}')" in fe
    assert "Run budget check now" in fe
