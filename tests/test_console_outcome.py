"""The Outcome tab: one run, from raw customer data to a judged model, compared with the untrained
model on the same held-out questions.

Offline. S3 is a dict, the runs table is a list; what is pinned is what the page claims:
- the baseline is the eval_only run that says, in its own manifest, which run it is the baseline of,
  and that judged an hf:// model;
- the answer pairs come from the same prompt ids, disagreements first;
- every read stays under runs/ and lakehouse/reports/;
- a bad run id is refused before any read.
"""
from __future__ import annotations

import io
import json
import pathlib

import pytest

from test_console_cost import console  # noqa: F401  (fixture)

REPO = pathlib.Path(__file__).resolve().parents[1]
RUN = "run-20261016T012010Z-9de45cbf"
BASE = "run-20261023T101023Z-5e8b449b"
OTHER = "run-20261020T000000Z-00000000"
DATASET = "run-20261015t125141z-bcb61189"
HF = "hf://Qwen/Qwen3-1.7B@70d244cc86ccca08cf5af4e1e306ecf908b1ad5e"


class _S3:
    def __init__(self, objects):
        self.objects, self.gets, self.lists = objects, [], []

    def get_object(self, Bucket, Key):
        self.gets.append(Key)
        if Key not in self.objects:
            raise RuntimeError("NoSuchKey")
        v = self.objects[Key]
        body = "\n".join(json.dumps(x) for x in v) if isinstance(v, list) else json.dumps(v)
        return {"Body": io.BytesIO(body.encode())}

    def list_objects_v2(self, Bucket, Prefix, Delimiter=None):
        self.lists.append(Prefix)
        if Delimiter:
            ids = sorted({k[len(Prefix):].split("/")[0] for k in self.objects if k.startswith(Prefix)})
            return {"CommonPrefixes": [{"Prefix": f"{Prefix}{i}/"} for i in ids]}
        return {"Contents": [{"Key": k, "LastModified": "2026-10-16"} for k in self.objects
                             if k.startswith(Prefix)]}


def _gate(answer, fmt, passed):
    def g(v, thr):
        return {"value": v, "threshold": thr, "wilson95_ci_low": v - 0.08, "wilson95_ci_high": v + 0.06,
                "n": 3, "verdict": "decisive_pass" if v - 0.08 >= thr else "decisive_fail"}
    return {"gate_passed": passed, "decided_at": "t",
            "gate_results": {"answer_match": g(answer, 0.6), "format_validity": g(fmt, 0.95)}}


def _pred(pid, answer, lang="en"):
    return {"prompt_id": pid, "intent": "complaint", "lang": lang, "prompt": f"q {pid}",
            "reference": f"ref {pid}", "answer": answer}


def _objects():
    return {
        f"runs/{RUN}/manifest.json": {"created_at": "c", "trigger_source": "x", "models": {"student": "Qwen/Qwen3-1.7B"},
                                      "params": {"pipeline_mode": "lakehouse", "lakehouse_dataset": DATASET}},
        # As the analyze agent writes it live: the trainer's metrics wrapped beside the job record.
        f"runs/{RUN}/training/metrics.json": {
            "trainer_metrics": {"final_train_loss": 0.24, "final_eval_loss": 0.3, "train_rows": 230,
                                "sec_per_step": {"p50_steady": 0.92}, "model_id": "Qwen/Qwen3-1.7B"},
            "training_job": {"instance_type": "ml.g5.2xlarge", "billable_time_seconds": 410},
            "loss_curve_assessment": {"verdict": "healthy"}},
        f"runs/{RUN}/evaluation/report.json": {"eval_set": {"which": "acceptance", "rows": 3, "sha256": "abc"}},
        f"runs/{RUN}/evaluation/gate.json": _gate(0.72, 1.0, True),
        f"runs/{RUN}/evaluation/i0/predictions.jsonl": [_pred("a", "ref a"), _pred("b", "ref b"), _pred("c", "nope", "zh-TW")],
        f"runs/{RUN}/evaluation/judge_details.jsonl": [
            {"item_id": "a", "verdict": "match", "format_valid": True},
            {"item_id": "b", "verdict": "match", "format_valid": True},
            {"item_id": "c", "verdict": "no_match", "format_valid": True}],
        f"runs/{BASE}/manifest.json": {"params": {"pipeline_mode": "eval_only", "source_run_id": RUN,
                                                  "model_artifact_uri": HF}},
        # The baseline's gate.json in the OTHER shape the eval agent writes (decisions, ci_low,
        # "n/N" counts): live on 2026-10-23.
        f"runs/{BASE}/evaluation/gate.json": {"gate_passed": False, "decisions": {
            "answer_match": {"value": 0.1, "counts": "0/3", "ci_low": 0.0, "ci_high": 0.5,
                             "threshold": 0.6, "verdict": "fail_decisive"},
            "format_validity": {"value": 0.4, "counts": "1/3", "ci_low": 0.1, "ci_high": 0.8,
                                "threshold": 0.95, "verdict": "fail_decisive"}}},
        f"runs/{BASE}/evaluation/i0/predictions.jsonl": [_pred("a", "rambling"), _pred("b", "ref b"), _pred("c", "nope")],
        f"runs/{BASE}/evaluation/judge_details.jsonl": [
            {"item_id": "a", "verdict": "no_match", "format_valid": False},
            {"item_id": "b", "verdict": "match", "format_valid": True},
            {"item_id": "c", "verdict": "no_match", "format_valid": True}],
        # An eval_only run of the same run's ARTIFACT (a re-judge, not a baseline): must not be picked.
        f"runs/{OTHER}/manifest.json": {"params": {"pipeline_mode": "eval_only", "source_run_id": RUN,
                                                   "model_artifact_uri": "s3://b/model.tar.gz"}},
        f"lakehouse/reports/{DATASET}/silver.json": {"pairs": 283, "rules": {"redact_pii": {"count": 30}}},
        f"lakehouse/reports/{DATASET}/gold.json": {"train": 230, "eval": 23},
    }


@pytest.fixture
def wired(console, monkeypatch):
    s3 = _S3(_objects())
    monkeypatch.setattr(console, "s3", s3)
    monkeypatch.setattr(console, "data_bucket", lambda: "data-bucket")
    rows = [{"run_id": OTHER, "pipeline_mode": "eval_only", "status": "completed"},
            {"run_id": BASE, "pipeline_mode": "eval_only", "status": "completed"},
            {"run_id": RUN, "pipeline_mode": "lakehouse", "status": "failed"}]
    monkeypatch.setattr(console, "_runs_ordered", lambda: (rows, False))
    monkeypatch.setattr(console, "_rate_card_doc", lambda: {})
    monkeypatch.setattr(console, "_training_job_costs", lambda rates, days: [
        {"resource_id": "job-1", "instance": "ml.g5.2xlarge", "quantity": 0.1, "cost_usd": 0.4, "run_id": RUN},
        {"resource_id": "job-b", "instance": "ml.g5.xlarge", "quantity": 0.1, "cost_usd": 0.2, "run_id": BASE},
        {"resource_id": "job-x", "instance": "ml.g5.xlarge", "quantity": 1, "cost_usd": 9.0, "run_id": OTHER}])
    return console, s3


def test_the_outcome_compares_the_fine_tune_with_its_own_untrained_baseline(wired):
    console, _ = wired
    o = console.outcome(RUN)
    assert o["baseline"]["run_id"] == BASE and o["baseline"]["model"] == HF, (
        "the baseline is the eval_only run of the UNTRAINED model that names this run as its source; "
        "a re-judge of the trained artifact is not a baseline")
    assert o["tuned"]["metrics"]["answer_match"]["value"] == 0.72
    b = o["baseline"]["gate"]["metrics"]["answer_match"]
    assert (b["value"], b["ci_high"], b["n"]) == (0.1, 0.5, "3"), (
        "the baseline's gate is written as `decisions`; the page must read that shape too")
    assert o["tuned"]["metrics"]["answer_match"]["ci_low"] == pytest.approx(0.64)
    assert o["tuned"]["gate_passed"] is True


def test_answer_pairs_join_on_the_prompt_and_show_what_the_fine_tune_changed_first(wired):
    console, _ = wired
    o = console.outcome(RUN)
    first = o["samples"][0]
    assert first["prompt_id"] == "a", "the item the fine-tune turned from a miss into a match leads"
    assert first["baseline"]["verdict"] == "no_match" and first["tuned"]["verdict"] == "match"
    assert first["reference"] == "ref a"
    assert o["flips"] == {"items": 3, "flipped_to_match": 1, "flipped_to_miss": 0}


def test_the_data_journey_and_cost_come_from_this_run_and_its_baseline_only(wired):
    console, _ = wired
    o = console.outcome(RUN)
    assert o["dataset"] == DATASET and o["data"]["pairs"] == 283 and o["data"]["rules"]["redact_pii"] == 30
    assert o["cost"]["total_usd"] == 0.6, "an unrelated run's jobs leaked into this run's cost"
    assert o["training"]["sec_per_step_p50"] == 0.92 and o["training"]["final_train_loss"] == 0.24, (
        "the analyze agent wraps the trainer's metrics under trainer_metrics; read through it")
    assert (o["training"]["instance"], o["training"]["billable_seconds"]) == ("ml.g5.2xlarge", 410)


def test_every_outcome_read_stays_inside_the_console_grant(wired):
    console, s3 = wired
    console.outcome(RUN)
    assert s3.gets and all(k.startswith(("runs/", "lakehouse/reports/", "lakehouse/landing/"))
                           for k in s3.gets), s3.gets
    assert all(p.startswith(("runs/", "lakehouse/reports/")) for p in s3.lists), s3.lists


@pytest.mark.parametrize("bad", ["", "../tasks/x", "run-1", f"{RUN}/../../tasks"])
def test_a_bad_run_id_is_refused_before_any_read(wired, bad):
    console, s3 = wired
    with pytest.raises(ValueError):
        console.outcome(bad)
    assert not s3.gets and not s3.lists


def test_a_run_without_a_baseline_still_renders(wired, monkeypatch):
    console, _ = wired
    monkeypatch.setattr(console, "_runs_ordered", lambda: ([{"run_id": RUN, "pipeline_mode": "lakehouse"}], False))
    o = console.outcome(RUN)
    assert o["baseline"] is None and o["samples"] and o["samples"][0]["baseline"] is None


def test_the_route_is_wired_and_the_tab_reads_it():
    src = (REPO / "deploy/console/lambda_function.py").read_text()
    assert 'path == "/api/outcome"' in src
    front = (REPO / "deploy/console/frontend.html").read_text()
    assert "/api/outcome?run_id=" in front and 'data-tab="outcome"' in front and '"outcome"' in front


# ── the baseline is dispatched through the pipeline, pinned ──────────────────
def _start_pipeline():
    import importlib.util
    spec = importlib.util.spec_from_file_location("sp_for_baseline", REPO / "orchestration/start_pipeline/handler.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


BASELINE = {"pipeline_mode": "eval_only", "customer_eval_uri": "s3://b/acceptance.jsonl",
            "source_run_id": RUN, "models": {"student": "Qwen/Qwen3-1.7B"},
            "gates": {"answer_match": 0.6}, "dataset": "store-copilot-lakehouse"}


@pytest.mark.parametrize("uri", [HF, "s3://b/runs/x/model.tar.gz"])
def test_an_eval_only_run_takes_a_trained_artifact_or_a_pinned_base_model(uri):
    sp = _start_pipeline()
    m = sp.seed_manifest("run-x", "test", {**BASELINE, "model_artifact_uri": uri}, {})
    assert m["params"]["model_artifact_uri"] == uri


@pytest.mark.parametrize("uri", ["hf://Qwen/Qwen3-1.7B", "hf://Qwen/Qwen3-1.7B@main",
                                 "hf://Qwen/Qwen3-1.7B@70d244cc", "https://huggingface.co/Qwen/Qwen3-1.7B"])
def test_a_baseline_that_can_silently_change_is_refused(uri):
    sp = _start_pipeline()
    with pytest.raises(ValueError, match="40-hex"):
        sp.seed_manifest("run-x", "test", {**BASELINE, "model_artifact_uri": uri}, {})


def test_the_eval_prompt_judges_the_baseline_exactly_like_the_trained_model():
    text = json.loads((REPO / "agents/eval/harness.json").read_text())["systemPrompt"][0]["text"]
    i = text.find("it is a BASELINE")
    assert i > 0, "the eval prompt no longer says what an hf:// artifact is"
    clause = text[i:i + 900]
    for must in ("from_pretrained", "revision", "change nothing", "same scorers", "baseline: true"):
        assert must in clause, f"the baseline clause lost {must!r}"
