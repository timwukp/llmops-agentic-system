"""The unauthenticated read API returns only what the page renders, and spend/config/agent
routes are approver-only.

Live on 2026-10-28 an anonymous GET /api/overview returned Step Functions task tokens (inside
`driver_beat_payload`) and, via /api/run, the signed approver's identity. A live token plus
`states:SendTaskSuccess` forges a stage's completion. The fix projects both responses to the
fields the frontend actually reads, and the allowlist is DERIVED from frontend.html here so a new
read fails this test instead of silently re-widening the response.
"""
from __future__ import annotations

import json
import pathlib
import re

import pytest

from test_console_cost import console  # noqa: F401  (module fixture)

REPO = pathlib.Path(__file__).resolve().parent.parent
FRONT = (REPO / "deploy/console/frontend.html").read_text(encoding="utf-8")

SENSITIVE = ("task_token", "driver_beat_payload", "signed_gates", "cognito_sub",
             "source_ip", "approved_by", "model_failover")


class _Body:
    def __init__(self, b): self._b = b
    def read(self): return self._b


class _S3:
    def __init__(self, objects): self.objects = objects
    def get_object(self, Bucket, Key):
        return {"Body": _Body(self.objects[f"s3://{Bucket}/{Key}"])}


# ── the projectors ────────────────────────────────────────────────────────────
def test_public_run_keeps_only_the_rendered_fields(console):
    row = {k: "x" for k in console.PUBLIC_RUN_FIELDS}
    row.update({s: "SECRET" for s in SENSITIVE})
    out = console._public_run(row)
    assert set(out) == set(console.PUBLIC_RUN_FIELDS)
    assert not (set(out) & set(SENSITIVE))


def test_public_manifest_keeps_only_per_stage_status_metrics_evidence(console):
    man = {"approval": {"approved_by": "boss", "cognito_sub": "s", "source_ip": "1.2.3.4"},
           "params": {"gates": {"x": 1}}, "plan": {"secret": 1}, "models": {"teacher": "t"},
           "stages": {"eval": {"status": "done", "metrics": {"gate_passed": True},
                               "evidence": "s3://e", "internal": "hide"}}}
    out = console._public_manifest(man)
    assert set(out) == {"stages"}
    assert set(out["stages"]["eval"]) == {"status", "metrics", "evidence"}
    blob = json.dumps(out)
    for leak in ("approved_by", "cognito_sub", "source_ip", "teacher", '"plan"', "hide"):
        assert leak not in blob


def test_public_manifest_is_none_without_stages(console):
    assert console._public_manifest({"params": {}}) is None
    assert console._public_manifest(None) is None


# ── end to end: the responses carry no sensitive key ───────────────────────────
def test_run_detail_does_not_leak_the_manifest_but_still_decides_the_gate(console, monkeypatch):
    man = {"approval": {"approved_by": "boss", "cognito_sub": "sub", "source_ip": "9.9.9.9"},
           "params": {"gates": {"format_validity": 0.95}},
           "stages": {"eval": {"status": "done", "evidence": "s3://e",
                               "metrics": {"format_validity": 1.0, "format_n": 120,
                                           "gate_passed": True}}}}
    monkeypatch.setattr(console, "s3", _S3({"s3://b/runs/run-x/manifest.json": json.dumps(man).encode()}))
    monkeypatch.setattr(console, "data_bucket", lambda: "b")
    monkeypatch.setattr(console, "events_tbl", None)
    monkeypatch.setattr(console, "runs_tbl", None)
    out = console.run_detail("run-x")
    # the gate was still decided from the full local manifest
    assert out["gateVerdict"] == "passed" and out["gates"][0]["name"] == "format_validity"
    # but the response manifest carries only the per-stage view
    assert set(out["manifest"]) == {"stages"}
    blob = json.dumps(out)
    for leak in ("approved_by", "cognito_sub", "9.9.9.9"):
        assert leak not in blob


def test_overview_projects_every_run_row(console, monkeypatch):
    rows = [{"run_id": "r1", "status": "running", "current_stage": "eval",
             "trigger_source": "console", "created_at": "2026-10-28", "pipeline_mode": "full",
             "task_token": "AAAA-live-token", "driver_beat_payload": json.dumps({"task_token": "X"}),
             "signed_gates": "{}", "job_name": "j"}]
    monkeypatch.setattr(console, "_runs_ordered", lambda: (rows, False))
    monkeypatch.setattr(console, "list_fleet", lambda: [])
    monkeypatch.setattr(console, "list_executions", lambda: [])
    out = console.overview()
    blob = json.dumps(out["runs"])
    assert "live-token" not in blob and "driver_beat_payload" not in blob
    assert out["runs"][0]["run_id"] == "r1" and out["runsRead"] == 1


# ── the allowlist is derived from the frontend, not hand-copied ────────────────
def _block(fn_name):
    i = FRONT.index(f"function {fn_name}(")
    j = FRONT.index("\nfunction ", i + 1)
    return FRONT[i:j]


def test_the_run_row_allowlist_covers_every_field_the_page_reads(console):
    """renderRuns is the run-table consumer. Every `r.<field>` it reads must be in the
    allowlist, so adding a read of a new field fails here until the allowlist is widened
    deliberately (and the field judged safe to expose)."""
    read = set(re.findall(r"\br\.([a-z_]+)\b", _block("renderRuns")))
    extra = read - set(console.PUBLIC_RUN_FIELDS)
    assert not extra, f"renderRuns reads run fields not in PUBLIC_RUN_FIELDS: {extra}"


def test_the_run_detail_consumer_reads_the_manifest_only_through_stages(console):
    """loadRun is the /api/run consumer. It must touch the manifest only via `.stages` and
    read only status/metrics/evidence off a stage — the shape _public_manifest emits."""
    block = _block("loadRun")
    man_access = set(re.findall(r"\bman\.([a-zA-Z_]+)", block))
    assert man_access <= {"stages"}, f"loadRun reads manifest fields beyond stages: {man_access}"
    st_access = set(re.findall(r"\bst\.([a-zA-Z_]+)", block))
    assert st_access <= {"status", "metrics", "evidence"}, f"loadRun reads stage fields: {st_access}"


# ── spend / config / agent-behaviour routes are approver-only ──────────────────
class _Cognito:
    def __init__(self, groups): self.groups = list(groups)
    def get_user(self, AccessToken):
        return {"Username": "u", "UserAttributes": [{"Name": "sub", "Value": "sub-u"}]}
    def admin_list_groups_for_user(self, **kw):
        return {"Groups": [{"GroupName": g} for g in self.groups]}


def _post(console, monkeypatch, path, groups):
    monkeypatch.setattr(console, "COGNITO_POOL_ID", "pool-test")  # _authed_user gates on this
    monkeypatch.setattr(console, "cognito", _Cognito(groups=groups))
    return console.handler({
        "requestContext": {"http": {"method": "POST", "path": path}, "sourceIp": "1.1.1.1"},
        "headers": {"authorization": "Bearer t"}, "body": "{}"}, None)


@pytest.mark.parametrize("path", sorted({
    "/api/start-run", "/api/cost/budgets", "/api/cost/sweep", "/api/finops-run",
    "/api/batch-eval", "/api/insights-report", "/api/native-rec", "/api/native-rec-apply",
    "/api/optimize", "/api/optimize-apply"}))
def test_a_group_less_user_cannot_reach_a_spend_or_config_route(console, monkeypatch, path):
    r = _post(console, monkeypatch, path, ())
    assert r["statusCode"] == 403, f"{path} reachable by a group-less user: {r['statusCode']}"


def test_the_approver_only_set_matches_the_routes_the_router_guards(console):
    """The set and the central guard must stay together: a spend route added to the router
    but not the set would be reachable by anyone logged in."""
    assert console.APPROVER_ONLY_POSTS and "/api/start-run" in console.APPROVER_ONLY_POSTS
    src = (REPO / "deploy/console/lambda_function.py").read_text()
    assert "if path in APPROVER_ONLY_POSTS and APPROVER_GROUP not in" in src


def test_accept_requires_the_approver_group(console, monkeypatch):
    r = _post(console, monkeypatch, "/api/tasks/task-abc/accept", ("llmops-datascience",))
    assert r["statusCode"] == 403


# ── budgets reject non-finite values; cost days are snapped ────────────────────
@pytest.mark.parametrize("bad", ["nan", "inf", "-inf", "1e400"])
def test_save_budget_rejects_non_finite_limits(console, bad):
    r = console.save_budget({"name": "b", "scope": "project_monthly", "limit_usd": bad}, {"username": "boss"})
    assert r.get("status_code") == 400, f"{bad} accepted as a budget limit"


def test_save_budget_rejects_a_limit_over_the_ceiling(console):
    r = console.save_budget({"name": "b", "scope": "project_monthly",
                             "limit_usd": console.BUDGET_LIMIT_MAX + 1}, {"username": "boss"})
    assert r.get("status_code") == 400


def test_cost_days_are_snapped_to_the_fixed_set(console, monkeypatch):
    seen = {}
    monkeypatch.setattr(console, "_cached", lambda key, fn, ttl=0: (seen.__setitem__(key, True), {})[1])
    for d in (1, 5, 45, 90, 9999, "x", None):
        console.cost_v2(d)
    # every call keyed on one of the three windows — never 90 distinct keys
    windows = {int(re.search(r"(\d+)", k).group(1)) for k in seen if re.search(r"(\d+)", k)}
    assert windows <= set(console.COST_DAYS_CHOICES), f"days not snapped: {windows}"
