"""The console's intake for the two lakehouse modes: the catalog entries, what their forms ask, how a
named dataset is verified, and what the orchestrator is told.

Each guard is derived from the dispatcher (start_pipeline's MODE_* tables) or from the reports the
lakehouse jobs write, not restated here. A form that cannot ask for what dispatch demands fails in
front of a customer. An intake that accepts a dataset without a held-out acceptance set signs off a GPU
run whose gate measures recall (PR-11).
"""
from __future__ import annotations

import io
import json
import pathlib
import re

import pytest

from test_console_cost import console  # noqa: F401  (fixture)
from test_console_tasks import _load_start_pipeline

REPO = pathlib.Path(__file__).resolve().parents[1]
MODES = {"lakehouse-ingest": "lakehouse_ingest", "finetune-lakehouse": "lakehouse"}


def test_both_modes_are_offered_and_supported(console):  # noqa: F811
    for key, mode in MODES.items():
        t = console._task_type(key)
        assert t["supported"] is True and t["pipeline_mode"] == mode, key


@pytest.mark.parametrize("key", sorted(MODES))
def test_each_form_requires_what_the_dispatcher_refuses_without(console, key):  # noqa: F811
    sp = _load_start_pipeline()
    t = console._task_type(key)
    form = {f["param"] for f in t["fields"] if f["required"]}
    assert {k for k, _ in sp.MODE_REQUIRED_PARAMS[t["pipeline_mode"]]} <= form
    for role, _ in sp.MODE_REQUIRED_ROLES.get(t["pipeline_mode"], ()):
        assert form & set(sp.ROLE_ALIASES[role]), f"{key} must name the {role} role explicitly"


def test_every_new_form_param_is_one_intake_can_name(console):  # noqa: F811
    for key in MODES:
        for f in console._task_type(key)["fields"]:
            assert f["param"] in console.INTAKE_ALIASES, f"{key}: intake cannot extract {f['param']}"
    assert "generator_seed" in console._INTAKE_NUM_PARAMS


# ── the dataset verifier ─────────────────────────────────────────────────────
class _S3:
    def __init__(self, objects):
        self.objects, self.gets = objects, []

    def get_object(self, Bucket, Key):
        self.gets.append(Key)
        if Key not in self.objects:
            raise RuntimeError("An error occurred (NoSuchKey) when calling the GetObject operation")
        return {"Body": io.BytesIO(json.dumps(self.objects[Key]).encode())}


DS = "run-20261015t125141z-bcb61189"


def _objects(version="2"):
    return {f"lakehouse/reports/{DS}/gold.json": {"train": 230, "eval": 23},
            f"lakehouse/landing/{DS}/manifest.json": {"generator_version": version, "seed": 7, "scale": "demo",
                                                      "counts": {"eval_prompts": 120}}}


@pytest.fixture
def wired(console, monkeypatch):  # noqa: F811
    def wire(objects):
        s3 = _S3(objects)
        monkeypatch.setattr(console, "s3", s3)
        monkeypatch.setattr(console, "data_bucket", lambda: "data-bucket")
        return s3
    return wire


def test_a_v2_dataset_with_a_gold_report_is_verified_with_its_sizes(console, wired):  # noqa: F811
    wired(_objects())
    f = console._verify_candidate("lakehouse_dataset", DS)
    assert f["status"] == "verified" and "230/23" in f["detail"] and "120 items" in f["detail"]


def test_a_generator_v1_dataset_is_refused_because_it_has_no_acceptance_set(console, wired):  # noqa: F811
    wired(_objects(version="1"))
    f = console._verify_lakehouse_dataset(DS)
    assert f["status"] == "failed" and "no held-out acceptance set" in f["detail"]


def test_an_unknown_dataset_is_refused_and_a_read_error_is_not_a_verdict(console, wired):  # noqa: F811
    wired({})
    assert console._verify_lakehouse_dataset(DS)["status"] == "failed"

    class _Boom(_S3):
        def get_object(self, Bucket, Key):
            raise RuntimeError("ThrottlingException")
    console.s3 = _Boom({})
    assert console._verify_lakehouse_dataset(DS)["status"] == "unverified"


@pytest.mark.parametrize("bad", ["../customer-data", "Seed7", "a/b", ""])
def test_a_malformed_id_is_refused_before_any_read(console, wired, bad):  # noqa: F811
    s3 = wired(_objects())
    assert console._verify_lakehouse_dataset(bad)["status"] == "failed" and s3.gets == []


# ── what the orchestrator is told ────────────────────────────────────────────
def test_the_orchestrator_knows_both_modes_and_every_key_their_dispatch_requires():
    sp = _load_start_pipeline()
    text = json.loads((REPO / "agents/orchestrator/harness.json").read_text())["systemPrompt"][0]["text"]
    for key, mode in MODES.items():
        m = re.search(rf'params\.pipeline_mode="{mode}", catalog key {key}\):(.*?)(?=\n\s+- [A-Z])', text, re.S)
        assert m, f"the orchestrator has no menu item for {mode}"
        for k, _ in sp.MODE_REQUIRED_PARAMS[mode]:
            assert k in m.group(1), f"{mode}: the menu item never names {k}"
        for k, _ in sp.MODE_REQUIRED_CHOSEN.get(mode, ()):
            assert k in m.group(1), f"{mode}: the menu item never says {k} must be chosen"
    assert "n >= 73" in text, "the gate-decidability arithmetic is what PR-09's borderline run lacked"
