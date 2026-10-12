"""Cost v2 governance: the budget gate is the one part of the new Cost tab that can refuse
a launch, so its decisions are pinned here offline. Reuses test_console_cost's import-time
isolation (boto3 stubbed before the console module loads)."""
from __future__ import annotations

import json

import pytest

from test_console_cost import _StubTable, console  # noqa: F401  (fixture)


@pytest.fixture
def gov(console, monkeypatch):  # noqa: F811
    tbl = _StubTable("LlmopsAdminRuns")
    monkeypatch.setattr(console, "console_tbl", tbl)
    monkeypatch.setattr(console, "LLMOPS_SNS_TOPIC", "")
    monkeypatch.setattr(console, "cost_v2", lambda days=30: {"overview": {"mtd_usd": 90.0}})
    return console, tbl


def _budget(tbl, name, scope, limit, mode):
    tbl.put_item({"id": f"budget#{name}", "name": name, "scope": scope,
                  "limit_usd": str(limit), "mode": mode, "thresholds": "[50,80,100]"})


def _alerts(tbl):
    return [v for k, v in tbl.items.items() if str(k).startswith("budget-alert#")]


def test_no_budgets_means_the_gate_allows(gov):
    c, tbl = gov
    assert c.budget_gate(1_000_000)["allowed"] is True
    assert _alerts(tbl) == []


def test_notify_budget_breach_allows_but_records_an_alert(gov):
    c, tbl = gov
    _budget(tbl, "monthly", "project_monthly", 100, "notify")
    g = c.budget_gate(20, "start-run")          # 90 MTD + 20 > 100
    assert g["allowed"] is True and g["breaches"][0]["blocked"] is False
    assert [a["severity"] for a in _alerts(tbl)] == ["WARNING"]


def test_block_budget_breach_refuses_with_409(gov):
    c, tbl = gov
    _budget(tbl, "monthly", "project_monthly", 100, "block")
    g = c.budget_gate(20, "start-run")
    assert g["allowed"] is False
    r = c._gate_refusal(g)
    assert r["status_code"] == 409 and "monthly" in r["error"]
    assert [a["severity"] for a in _alerts(tbl)] == ["CRITICAL"]


def test_block_budget_within_limit_allows(gov):
    c, tbl = gov
    _budget(tbl, "monthly", "project_monthly", 100, "block")
    assert c.budget_gate(5)["allowed"] is True     # 90 + 5 <= 100
    assert _alerts(tbl) == []


def test_per_task_budget_checks_the_launch_estimate_only(gov):
    c, tbl = gov
    _budget(tbl, "task-cap", "per_task", 50, "block")
    assert c.budget_gate(49)["allowed"] is True
    assert c.budget_gate(51)["allowed"] is False


def test_a_notify_breach_does_not_mask_a_block_breach(gov):
    c, tbl = gov
    _budget(tbl, "a-notify", "project_monthly", 50, "notify")
    _budget(tbl, "b-block", "per_task", 10, "block")
    g = c.budget_gate(20)
    assert g["allowed"] is False and len(g["breaches"]) == 2


def test_save_budget_rejects_an_unknown_mode(gov):
    c, _ = gov
    r = c.save_budget({"name": "x", "scope": "project_monthly", "limit_usd": 10,
                       "mode": "hard-stop"}, {"username": "u"})
    assert r["status_code"] == 400


def test_save_budget_round_trips(gov):
    c, tbl = gov
    r = c.save_budget({"name": "LLMOps Monthly", "scope": "project_monthly",
                       "limit_usd": "500", "mode": "notify"}, {"username": "u"})
    assert r["ok"] and "budget#llmops-monthly" in tbl.items
    assert json.loads(tbl.items["budget#llmops-monthly"]["thresholds"]) == [50, 80, 100]


@pytest.mark.parametrize("mtd,state", [(10, "on_track"), (85, None), (120, "over")])
def test_monthly_budget_state(gov, mtd, state):
    c, _ = gov
    v = c._budget_view({"id": "budget#m", "name": "m", "scope": "project_monthly",
                        "limit_usd": "100", "mode": "notify"}, mtd)
    if state:
        assert v["state"] == state
    else:  # 85% used: warning, or forecast_over if the run-rate already projects past 100
        assert v["state"] in ("warning", "forecast_over")
