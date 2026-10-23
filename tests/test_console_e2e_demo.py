"""M1: one console click from raw customer data to a judged model, and a pipeline view that draws
the stages the run actually has.

Offline: the console module is loaded with a faked boto3 (test_console_tasks' fixture), and the
start-pipeline module is loaded straight from the repo so the demo preset is checked against the
dispatch rules it will meet in production, not against a copy of them.
"""
import importlib.util
import json
import pathlib

import pytest

from test_console_tasks import _fake_sfn_history, _stub_identity, console  # noqa: F401 (fixture)

REPO = pathlib.Path(__file__).resolve().parents[1]
ASL = json.loads((REPO / "orchestration/state_machine.asl.json").read_text())


def _start_pipeline():
    spec = importlib.util.spec_from_file_location(
        "start_pipeline_for_demo", REPO / "orchestration/start_pipeline/handler.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _keys(flow):
    return [k for k, _ in flow]


# ── the picture follows the path ──────────────────────────────────────────────
def test_each_run_shape_draws_its_own_stages(console):
    full = _keys(console.stage_flow_for({"pipeline_mode": "full"}))
    assert full == _keys(console.STAGE_FLOW)
    assert _keys(console.stage_flow_for({})) == full, "a run with no mode is a full run"

    ingest = _keys(console.stage_flow_for({"pipeline_mode": "lakehouse_ingest",
                                           "lakehouse": {"seed": "7"}}))
    assert ingest == ["lakehouse-raw", "lakehouse-bronze", "lakehouse-silver",
                      "lakehouse-gold", "complete"]

    build = _keys(console.stage_flow_for({"pipeline_mode": "lakehouse",
                                          "lakehouse": {"seed": "7", "dataset": "d"}}))
    assert build[:5] == ["lakehouse-raw", "lakehouse-bronze", "lakehouse-silver",
                         "lakehouse-gold", "lakehouse-extract"]
    assert build[-1] == "complete" and "eval-gate" in build and "deploy" in build
    assert "data-prep-generate" not in build, "a lakehouse run has no teacher generation"

    assert _keys(console.stage_flow_for({"pipeline_mode": "eval_only"})) == ["eval-gate", "complete"], (
        "a re-judge (or an untrained baseline) has no data-prep or training stage to draw")
    assert _keys(console.stage_flow_for({"pipeline_mode": "deploy_only"}))[0] == "deploy"

    train = _keys(console.stage_flow_for({"pipeline_mode": "lakehouse",
                                          "lakehouse": {"dataset": "d"}}))
    assert train[0] == "lakehouse-extract" and "lakehouse-raw" not in train


def test_every_lakehouse_state_has_a_card_in_the_flow_of_the_modes_that_reach_it(console):
    glue = {n for n, s in ASL["States"].items()
            if s.get("Resource", "").startswith("arn:aws:states:::glue:")}
    assert glue, "no Glue state found -- the derivation is broken"
    build = _keys(console.stage_flow_for({"pipeline_mode": "lakehouse", "lakehouse": {"seed": "7"}}))
    for name in sorted(glue):
        key = console.STATE_TO_STAGE.get(name)
        assert key, f"{name} has no card: the data engineering half of the run is invisible again"
        assert key in build, f"{name}'s card {key} is not drawn for a build-then-train run"


def test_a_running_build_shows_the_data_stages_live(console, monkeypatch):
    console._STAGE_CFG_CACHE.clear()
    _stub_identity(console, monkeypatch)
    history = [{"stateEnteredEventDetails": {"name": "LakehouseGenerate"}, "timestamp": "t0"},
               {"stateExitedEventDetails": {"name": "LakehouseGenerate"}, "timestamp": "t1"},
               {"stateEnteredEventDetails": {"name": "LakehouseLand"}, "timestamp": "t2"}]
    monkeypatch.setattr(console, "sfn", _fake_sfn_history(history, "RUNNING", execution_input={
        "pipeline_mode": "lakehouse", "lakehouse": {"seed": "7", "dataset": "d"}}))
    st = {s["key"]: s for s in console.pipeline_detail("run-build")["stages"]}
    assert st["lakehouse-raw"]["status"] == "succeeded"
    assert st["lakehouse-bronze"]["status"] == "running"
    assert st["eval-gate"]["status"] == "pending"
    assert st["lakehouse-bronze"]["config"].get("glueJob") == "llmops-lakehouse-land-bronze"


# ── one click ────────────────────────────────────────────────────────────────
class _Lam:
    def __init__(self):
        self.calls = []

    def invoke(self, **kw):
        self.calls.append(kw)

        class _P:
            def read(self_inner):
                return json.dumps({"run_id": "run-20261023T000000Z-0a1b2c3d"}).encode()
        return {"Payload": _P()}


def test_the_demo_button_dispatches_a_build_then_train_run(console, monkeypatch):
    lam = _Lam()
    monkeypatch.setattr(console, "lam", lam)
    out = console.start_run({"preset": "lakehouse-e2e", "seed": 42})
    assert out["ok"], out
    sent = json.loads(lam.calls[0]["Payload"])
    assert sent["trigger_source"] == "console-demo"
    p = sent["params"]
    assert p["pipeline_mode"] == "lakehouse" and p["generator_seed"] == 42
    assert "lakehouse_dataset" not in p, "the demo builds its own dataset; naming one too is refused"


def test_the_demo_preset_passes_the_dispatch_rules_it_will_meet(console):
    """The preset is checked against start-pipeline itself: a preset that the dispatcher refuses
    is a button that fails with an error message on the first click."""
    sp = _start_pipeline()
    params = console._demo_params({"seed": 42})
    manifest = sp.seed_manifest("run-20261023T000000Z-0a1b2c3d", "console-demo", params, {})
    assert sp.lakehouse_builds(manifest["params"])
    assert manifest["params"]["gates"] == console.LAKEHOUSE_E2E_PRESET["gates"]


def test_two_clicks_do_not_share_a_preset_object(console):
    a = console._demo_params({"seed": 1})
    a["gates"]["answer_match"] = 0.0
    assert console.LAKEHOUSE_E2E_PRESET["gates"]["answer_match"] == 0.6


@pytest.mark.parametrize("body", [{"preset": "everything"}, {"preset": "lakehouse"}])
def test_an_unknown_preset_is_refused_not_ignored(console, monkeypatch, body):
    lam = _Lam()
    monkeypatch.setattr(console, "lam", lam)
    out = console.start_run(body)
    assert "error" in out and not lam.calls, (
        "an unknown preset must not fall through to a FULL pipeline run with default params")


# ── the sidebar: grouped, every section with an icon, collapsible ─────────────
def test_every_section_in_the_sidebar_has_an_icon_and_survives_collapsing():
    """A tab added without an icon shows as a blank square once the sidebar is collapsed to icons."""
    import re
    html = (REPO / "deploy/console/frontend.html").read_text()
    tabs = re.findall(r'<button data-tab="([a-z-]+)"', html)
    assert len(tabs) >= 10, tabs
    for tab in tabs:
        assert f'nav.tabs button[data-tab="{tab}"] {{ --ic:' in html, f"{tab} has no sidebar icon"
    assert 'id="navToggle"' in html and "function toggleNav" in html
    assert 'localStorage.setItem("navCollapsed"' in html, "the collapsed state must survive a reload"
    assert "b.title = b.textContent.trim()" in html, "collapsed icons need their label as a tooltip"
