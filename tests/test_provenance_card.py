"""The provenance card: the builder (pipeline/contracts/provenance.py), its publisher
(orchestration/publish_provenance), and the console reading runs through the same reader.

Offline. The fixtures are the shapes agents have actually written (three gate.json shapes, two
metrics.json shapes, recorded in provenance.py); what is pinned is what the card claims:
- every gate shape reads as the same metrics, and an unknown one is reported, not read as empty;
- the card holds only keys the model package model card schema allows, within its limits;
- every value is copied from an artifact, and every artifact is cited by URI and sha256;
- the publisher writes only the card (never an approval), keeps a copy, and reads it back.
"""
from __future__ import annotations

import importlib.util
import io
import json
import pathlib

import pytest

from test_console_cost import console  # noqa: F401  (fixture)

REPO = pathlib.Path(__file__).resolve().parents[1]


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pv = _load("provenance_under_test", "pipeline/contracts/provenance.py")
RUN = "run-20261023T160741Z-65fec5d8"
BASE = "run-20261024T000000Z-0a1b2c3d"
DATASET = "run-20261023t160741z-65fec5d8"
HF = "hf://Qwen/Qwen3-1.7B@70d244cc86ccca08cf5af4e1e306ecf908b1ad5e"


def _g(value, low, high, verdict="decisive_pass", thr=0.6):
    return {"value": value, "threshold": thr, "verdict": verdict, "low": low, "high": high}


# The three gate.json shapes on record, same numbers.
GATES = {
    "gate_results": {"gate_passed": True, "gate_results": {"answer_match": {
        "value": 0.725, "threshold": 0.6, "wilson95_ci_low": 0.639, "wilson95_ci_high": 0.797, "n": 120,
        "verdict": "decisive_pass"}}},
    "decisions": {"gate_passed": True, "decisions": {"answer_match": {
        "value": 0.725, "threshold": 0.6, "ci_low": 0.639, "ci_high": 0.797, "counts": "87/120",
        "verdict": "decisive_pass"}}},
    "results": {"gate_passed": True, "decision_rule": "Wilson 95% interval: decisive pass iff ci_low >= threshold",
                "results": {"answer_match": {"value": 0.725, "threshold": 0.6, "ci_low": 0.639, "ci_high": 0.797,
                                             "count": "87/120", "verdict": "decisive_pass"},
                            "format_validity": {"value": 1.0, "threshold": 0.95, "ci_low": 0.969, "ci_high": 1.0,
                                                "count": "120/120", "verdict": "decisive_pass"}}},
}
METRICS = {
    "2026-10-16": {"trainer_metrics": {"final_train_loss": 0.24, "final_eval_loss": 0.3, "model_id": "Qwen/Qwen3-1.7B"},
                   "training_job": {"instance_type": "ml.g5.2xlarge", "billable_time_seconds": 410},
                   "loss_curve_assessment": {"verdict": "healthy"}},
    "2026-10-23": {"trainer_metrics": {"final_train_loss": 0.23, "final_eval_loss": 0.31, "model_id": "Qwen/Qwen3-1.7B",
                                       "model_revision": "70d244cc", "max_length": 1024, "learning_rate": 0.0002,
                                       "epochs_completed": 3.0, "global_step": 90,
                                       "lora": {"r": 16, "alpha": 32, "dropout": 0.05, "target_modules": ["q", "v"]}},
                   "job": {"name": "llmops-qlora-x", "arn": "arn:aws:sagemaker:us-east-1:123456789012:training-job/x",
                           "instance_type": "ml.g5.2xlarge", "billable_seconds": 408},
                   "health_assessment": {"verdict": "healthy"}},
}


def _artifacts(**over):
    a = {
        "run_id": RUN,
        "manifest": {"models": {"student": "Qwen/Qwen3-1.7B"},
                     "params": {"pipeline_mode": "lakehouse", "lakehouse_dataset": DATASET}},
        "metrics": METRICS["2026-10-23"],
        "report": {"eval_set": {"which": "acceptance", "rows": 120, "sha256": "bce1"},
                   "model_artifact_uri": f"s3://data-bucket/runs/{RUN}/training/model"},
        "gate": GATES["results"],
        "export": {"files": {
            "train": {"uri": "s3://data-bucket/lakehouse/extracts/d/1/train.jsonl", "sha256": "e59b", "rows": 234,
                      "snapshot_id": "41"},
            "eval": {"uri": "s3://data-bucket/lakehouse/extracts/d/1/val.jsonl", "sha256": "76c3", "rows": 19,
                     "snapshot_id": "42"},
            "acceptance": {"uri": "s3://data-bucket/lakehouse/extracts/d/1/acceptance.jsonl", "sha256": "bce1",
                           "rows": 120}}},
        "silver": {"pairs": 283, "rules": {"redact_pii": {"count": 30}}},
        "gold": {"train": 234, "eval": 19},
        "baseline": None,
        "cost": {"total_usd": 0.6, "scope": "SageMaker jobs", "jobs": [], "unpriced": []},
        "sources": {"gate": {"uri": f"s3://data-bucket/runs/{RUN}/evaluation/gate.json", "sha256": "219b"}},
    }
    a.update(over)
    return a


# ── readers ──────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("shape", sorted(GATES))
def test_every_gate_shape_reads_as_the_same_metric(shape):
    v = pv.gate_view(GATES[shape])
    m = v["metrics"]["answer_match"]
    assert (m["value"], m["ci_low"], m["ci_high"], m["threshold"]) == (0.725, 0.639, 0.797, 0.6)
    assert m["n"] == "120" and v["shape"] == shape and v["gate_passed"] is True


def test_the_reader_knows_every_shape_it_claims_to():
    assert set(GATES) == set(pv.GATE_SHAPES), "a gate shape is named in provenance.py but has no fixture here"


def test_an_unknown_gate_shape_is_reported_not_read_as_no_gate():
    v = pv.gate_view({"gate_passed": True, "verdicts": {"answer_match": {"value": 0.7}}})
    assert v["shape"] == "unknown" and v["metrics"] == {}
    card = pv.build_card(_artifacts(gate={"gate_passed": True, "verdicts": {}}))
    obs = card["content"]["evaluation_details"][0]["evaluation_observation"]
    assert "unknown shape" in obs and card["content"]["additional_information"]["custom_details"]["gate_shape"] == "unknown"


@pytest.mark.parametrize("day", sorted(METRICS))
def test_both_metrics_shapes_give_the_instance_and_billable_seconds(day):
    t = pv.training_view(METRICS[day])
    assert t["job"]["instance"] == "ml.g5.2xlarge" and t["job"]["billable_seconds"] in (408, 410)
    assert t["loss_verdict"] == "healthy"


# ── the card's shape: the schema is additionalProperties:false throughout ────
def _walk_keys(card):
    c = card["content"]
    yield "", set(c)
    yield "model_overview", set(c["model_overview"])
    yield "intended_uses", set(c["intended_uses"])
    yield "training_details", set(c["training_details"])
    yield "training_job_details", set(c["training_details"]["training_job_details"])
    for e in c["evaluation_details"]:
        yield "evaluation_detail", set(e)
    yield "additional_information", set(c["additional_information"])


@pytest.mark.parametrize("baseline", [False, True])
def test_the_card_holds_only_keys_the_schema_allows(baseline):
    b = {"run_id": BASE, "model": HF, "gate": GATES["decisions"]} if baseline else None
    card = pv.build_card(_artifacts(baseline=b))
    for where, keys in _walk_keys(card):
        assert keys <= pv.ALLOWED_KEYS[where], f"{where}: {keys - pv.ALLOWED_KEYS[where]} would be rejected"


def test_every_value_is_within_the_schema_limits_even_from_a_long_artifact():
    long = "x" * 5000
    card = pv.build_card(_artifacts(
        silver={"pairs": long, "rules": {}}, report={"eval_set": {"sha256": long}},
        gate={"gate_passed": True, "decision_rule": long, "results": {f"m{i}": {"value": 0.5, "count": "1/2"}
                                                                       for i in range(80)}}))
    c = card["content"]
    jd = c["training_details"]["training_job_details"]
    assert len(c["training_details"]["training_observations"]) <= pv.LIMITS["observation"]
    assert len(jd["training_metrics"]) <= pv.LIMITS["metrics"]
    assert all(len(m["notes"]) <= pv.LIMITS["string"] for m in jd["training_metrics"])
    assert all(0 < len(h["value"]) <= pv.LIMITS["hyper_value"] for h in jd["hyper_parameters"])
    assert len(c["evaluation_details"][0]["evaluation_observation"]) <= pv.LIMITS["evaluation_observation"]
    assert all(isinstance(v, str) and len(v) <= pv.LIMITS["string"]
               for v in c["additional_information"]["custom_details"].values())
    assert all(isinstance(m["value"], float) for g in c["evaluation_details"][0]["metric_groups"]
               for m in g["metric_data"]), "a simple metric of type number must carry a number"
    assert all(len(g["metric_data"]) <= pv.LIMITS["metrics"] for g in c["evaluation_details"][0]["metric_groups"])
    assert len(pv.card_json(card)) <= 100000, "ModelCardContent is capped at 100000 characters"


# ── what the card claims ─────────────────────────────────────────────────────
def test_the_gate_metric_carries_its_interval_count_threshold_verdict_and_eval_set():
    jd = pv.build_card(_artifacts())["content"]["training_details"]["training_job_details"]
    m = next(x for x in jd["training_metrics"] if x["name"] == "gate.answer_match")
    assert m["value"] == 0.725
    for must in ("87/120", "Wilson 95% [63.9%, 79.7%]", "threshold 0.6", "decisive_pass", "sha256 bce1"):
        assert must in m["notes"], must


def test_the_card_writes_the_fields_the_smus_page_renders():
    """Measured with a probe version, 2026-10-24: SMUS's Custom metrics shows training_metrics and its
    Hyperparameters shows hyper_parameters; the user_provided_* twins it does not show at all."""
    jd = pv.build_card(_artifacts())["content"]["training_details"]["training_job_details"]
    assert jd["training_metrics"] and jd["hyper_parameters"]
    assert "user_provided_training_metrics" not in jd and "user_provided_hyper_parameters" not in jd


def test_the_interval_threshold_cost_and_pii_are_rows_the_page_shows():
    """The page shows a metric's name and score, never its notes."""
    jd = pv.build_card(_artifacts())["content"]["training_details"]["training_job_details"]
    rows = {m["name"]: m["value"] for m in jd["training_metrics"]}
    assert (rows["gate.answer_match"], rows["gate.answer_match.wilson95_low"],
            rows["gate.answer_match.wilson95_high"], rows["gate.answer_match.threshold"]) == (0.725, 0.639, 0.797, 0.6)
    assert rows["data.pii_redactions"] == 30 and rows["run.cost_usd"] == 0.6


def test_the_baseline_sits_beside_the_tuned_score_with_its_own_run():
    card = pv.build_card(_artifacts(baseline={"run_id": BASE, "model": HF, "gate": {
        "gate_passed": False, "decisions": {"answer_match": {"value": 0.0, "counts": "0/120", "ci_low": 0.0,
                                                             "ci_high": 0.031, "threshold": 0.6}}}}))
    jd = card["content"]["training_details"]["training_job_details"]
    b = next(x for x in jd["training_metrics"] if x["name"] == "baseline.answer_match")
    assert b["value"] == 0.0 and BASE in b["notes"] and HF in b["notes"]
    assert [g["name"] for g in card["content"]["evaluation_details"][0]["metric_groups"]] == [
        "acceptance gate", "untrained baseline"]
    assert card["content"]["additional_information"]["custom_details"]["baseline_run_id"] == BASE


def test_the_data_is_pinned_by_sha256_and_snapshot_and_its_journey_is_told():
    c = pv.build_card(_artifacts())["content"]
    hp = {h["name"]: h["value"] for h in c["training_details"]["training_job_details"]["hyper_parameters"]}
    assert hp["data.train.sha256"] == "e59b" and hp["data.train.snapshot_id"] == "41"
    assert hp["data.acceptance.sha256"] == "bce1" and hp["lora.r"] == "16"
    assert "lora.target_modules" not in hp, "a list is not a hyperparameter value"
    assert c["training_details"]["training_job_details"]["training_datasets"] == [
        "s3://data-bucket/lakehouse/extracts/d/1/train.jsonl", "s3://data-bucket/lakehouse/extracts/d/1/val.jsonl"]
    obs = c["training_details"]["training_observations"]
    for must in ("283 pairs", "30 PII redactions", "234 train / 19 eval", "408 billable s", "healthy"):
        assert must in obs, must
    assert c["evaluation_details"][0]["datasets"] == ["s3://data-bucket/lakehouse/extracts/d/1/acceptance.jsonl"]


def test_every_artifact_read_is_cited_and_the_cost_states_its_scope():
    d = pv.build_card(_artifacts())["content"]["additional_information"]["custom_details"]
    assert d["evidence.gate"] == f"s3://data-bucket/runs/{RUN}/evaluation/gate.json sha256 219b"
    assert d["cost_usd"] == "0.60 (SageMaker jobs)" and d["builder"] == pv.BUILDER


def test_a_partial_cost_is_not_put_on_the_card():
    d = pv.build_card(_artifacts(cost={"total_usd": None, "unpriced": ["job"], "jobs": []}))
    assert "cost_usd" not in d["content"]["additional_information"]["custom_details"]


def test_the_card_never_claims_an_approval_or_a_risk_nobody_assessed():
    iu = pv.build_card(_artifacts())["content"]["intended_uses"]
    assert iu["risk_rating"] == "Unknown" and "human approval" in iu["intended_uses"]


def test_the_build_is_deterministic():
    assert pv.card_json(pv.build_card(_artifacts())) == pv.card_json(pv.build_card(_artifacts()))


# ── the console reads runs through the same reader ───────────────────────────
@pytest.mark.parametrize("shape", sorted(GATES))
def test_the_outcome_tab_and_the_card_read_a_gate_alike(console, shape):
    page = console._gate_view(GATES[shape])["metrics"]["answer_match"]
    card = pv.gate_view(GATES[shape])["metrics"]["answer_match"]
    assert {k: page[k] for k in ("value", "ci_low", "ci_high", "n")} == {k: card[k] for k in ("value", "ci_low", "ci_high", "n")}, (
        f"the {shape} shape reads differently on the page than on the card")


ANSWERS = {  # the two answer-file shapes on record, same question
    "predictions.jsonl": ({"prompt_id": "q1", "answer": "A", "reference": "R", "prompt": "P", "lang": "en"},
                          {"item_id": "q1", "verdict": "match", "format_valid": True}),
    "student_answers.jsonl": ({"item_id": "q1", "student_answer": "A", "reference": "R", "prompt": "P", "lang": "en"},
                              {"item_id": "q1", "answer_match": "True", "format_valid": "True"}),
}


@pytest.mark.parametrize("name", sorted(ANSWERS))
def test_both_answer_shapes_read_as_one(name):
    pred, judge = ANSWERS[name]
    assert pv.prediction_view(pred) == {"prompt_id": "q1", "intent": None, "lang": "en", "prompt": "P",
                                        "reference": "R", "answer": "A"}
    assert pv.judgement_view(judge) == {"item_id": "q1", "verdict": "match", "format_valid": True}
    assert name in pv.ANSWER_FILES


def test_a_false_string_is_a_miss_not_a_match():
    assert pv.judgement_view({"item_id": "q", "answer_match": "False", "format_valid": "False"}) == {
        "item_id": "q", "verdict": "no_match", "format_valid": False}


def test_the_outcome_tab_finds_answers_in_either_file(console, monkeypatch):
    for name, (pred, judge) in ANSWERS.items():
        files = {name: name, "judge_details.jsonl": "judge_details.jsonl"}
        monkeypatch.setattr(console, "_eval_files", lambda run_id, f=files: f)
        monkeypatch.setattr(console, "_s3_jsonl", lambda key, p=pred, j=judge: [j] if key.endswith("judge_details.jsonl") else [p])
        preds, judged = console._answers(RUN)
        assert preds["q1"]["answer"] == "A" and judged["q1"]["verdict"] == "match", (
            f"{name}: the Outcome tab said 'no evaluated answers' over 120 judged answers in this shape")


def test_the_cost_line_mentions_a_baseline_only_when_there_is_one():
    html = (REPO / "deploy/console/frontend.html").read_text()
    assert '(r.baseline?", incl. the baseline":"")' in html and 'job(s), incl. the baseline · "' not in html


def test_the_console_bundle_carries_the_reader():
    sh = (REPO / "deploy/console/deploy.sh").read_text()
    assert 'pipeline/contracts/provenance.py" "$BUILD/provenance.py"' in sh


# ── the publisher ────────────────────────────────────────────────────────────
class _S3:
    def __init__(self, objects):
        self.objects, self.puts, self.gets = objects, {}, []

    def get_object(self, Bucket, Key):
        self.gets.append(Key)
        if Key not in self.objects:
            raise RuntimeError("NoSuchKey")
        return {"Body": io.BytesIO(json.dumps(self.objects[Key]).encode())}

    def put_object(self, Bucket, Key, Body, ContentType=None):
        self.puts[Key] = json.loads(Body)


class _SM:
    def __init__(self, packages, jobs=(), echo=True):
        self.packages, self.jobs, self.echo, self.updates = packages, list(jobs), echo, []

    def list_model_package_groups(self, NameContains, MaxResults, **_):
        return {"ModelPackageGroupSummaryList": [{"ModelPackageGroupName": g} for g in sorted(
            {p["ModelPackageGroupName"] for p in self.packages}) if NameContains in g]}

    def list_model_packages(self, ModelPackageGroupName, **_):
        return {"ModelPackageSummaryList": [{"ModelPackageArn": p["ModelPackageArn"]} for p in self.packages
                                            if p["ModelPackageGroupName"] == ModelPackageGroupName]}

    def describe_model_package(self, ModelPackageName):
        p = next(p for p in self.packages if p["ModelPackageArn"] == ModelPackageName)
        return dict(p)

    def update_model_package(self, **kw):
        self.updates.append(kw)
        p = next(p for p in self.packages if p["ModelPackageArn"] == kw["ModelPackageArn"])
        # As the live API answers (2026-10-24): once a card exists, content and status together
        # are refused. A double that accepted it is how the second backfill failed in production.
        if p.get("ModelCard") and {"ModelCardContent", "ModelCardStatus"} <= set(kw["ModelCard"]):
            raise RuntimeError("ValidationException: Both ModelCardContent and ModelCardStatus are provided")
        if self.echo:
            p["ModelCard"] = {**p.get("ModelCard", {}), **kw["ModelCard"]}

    def list_training_jobs(self, NameContains, **_):
        return {"TrainingJobSummaries": [{"TrainingJobName": j["name"]} for j in self.jobs if NameContains in j["name"]]}

    def describe_training_job(self, TrainingJobName):
        j = next(j for j in self.jobs if j["name"] == TrainingJobName)
        return {"ResourceConfig": {"InstanceType": j["inst"], "InstanceCount": 1}, "BillableTimeInSeconds": j["secs"]}


class _Table:
    def __init__(self, rows):
        self.rows = rows

    def scan(self, **_):
        return {"Items": self.rows}


class _DDB:
    def __init__(self, rows):
        self.rows = rows

    def Table(self, name):
        return _Table(self.rows)


ARN = "arn:aws:sagemaker:us-east-1:123456789012:model-package/llmops-student-store/1"


def _wired(monkeypatch, *, packages=None, rows=(), jobs=(), echo=True, extra=None):
    pub = _load("publish_under_test", "orchestration/publish_provenance/handler.py")
    monkeypatch.setenv("DATA_BUCKET", "data-bucket")
    monkeypatch.setenv("RUNS_TABLE", "runs")
    a = _artifacts()
    objects = {f"runs/{RUN}/manifest.json": a["manifest"], f"runs/{RUN}/training/metrics.json": a["metrics"],
               f"runs/{RUN}/evaluation/report.json": a["report"], f"runs/{RUN}/evaluation/gate.json": a["gate"],
               f"lakehouse/reports/{DATASET}/export.json": a["export"],
               f"lakehouse/reports/{DATASET}/silver.json": a["silver"],
               f"lakehouse/reports/{DATASET}/gold.json": a["gold"],
               "finops/rates/rate_card_latest.json": {"rates": {"sagemaker:training:ml.g5.2xlarge": {"unit_price": 1.5}}},
               **(extra or {})}
    pkgs = packages if packages is not None else [
        {"ModelPackageGroupName": "llmops-student-store", "ModelPackageArn": ARN,
         "CustomerMetadataProperties": {"run_id": RUN}},
        {"ModelPackageGroupName": "llmops-student-store", "ModelPackageArn": ARN[:-1] + "0",
         "CustomerMetadataProperties": {"run_id": "run-20261016T012010Z-9de45cbf"}}]
    c = {"s3": _S3(objects), "sm": _SM(pkgs, jobs, echo), "ddb": _DDB(list(rows))}
    return pub, c


def test_the_publisher_writes_the_card_and_never_an_approval(monkeypatch):
    pub, c = _wired(monkeypatch, jobs=[{"name": f"llmops-qlora-{RUN}-i0", "inst": "ml.g5.2xlarge", "secs": 3600}])
    out = pub.publish(c, RUN)
    assert out["published"] and out["confirmed"] and out["model_package_arn"] == ARN
    (upd,) = c["sm"].updates
    assert set(upd) == {"ModelPackageArn", "ModelCard", "CustomerMetadataProperties"}, (
        "the publisher may write the card and two pointers; ModelApprovalStatus is a human's")
    assert upd["ModelCard"]["ModelCardStatus"] == "PendingReview"
    card = json.loads(upd["ModelCard"]["ModelCardContent"])
    assert card["additional_information"]["custom_details"]["cost_usd"].startswith("1.50 ")
    copy = c["s3"].puts[f"runs/{RUN}/provenance/card.json"]
    assert copy["card"] == card and copy["card_sha256"] == upd["CustomerMetadataProperties"]["provenance_card_sha256"]
    assert {"gate", "manifest", "export", "silver", "gold"} <= {
        k.split(".", 1)[1] for k in card["additional_information"]["custom_details"] if k.startswith("evidence.")}


def test_a_second_publish_updates_the_card_it_wrote(monkeypatch):
    pub, c = _wired(monkeypatch)
    assert pub.publish(c, RUN)["confirmed"]
    second = pub.publish(c, RUN)
    assert second["confirmed"] and c["sm"].updates[1]["ModelCard"].keys() == {"ModelCardContent"}, (
        "a re-publish (a backfill, a rerun) must not send the status again")


def test_an_archived_card_is_reported_not_overwritten(monkeypatch):
    pub, c = _wired(monkeypatch)
    c["sm"].packages[0]["ModelCard"] = {"ModelCardStatus": "Archived", "ModelCardContent": "{}"}
    out = pub.publish(c, RUN)
    assert out["published"] is False and "Archived" in out["reason"] and not c["sm"].updates


def test_a_run_without_a_package_is_reported_not_failed(monkeypatch):
    pub, c = _wired(monkeypatch, packages=[])
    out = pub.publish(c, RUN)
    assert out == {"published": False, "run_id": RUN, "reason": "no llmops-student model package names this run"}
    assert not c["sm"].updates and not c["s3"].puts


def test_a_card_the_registry_does_not_hold_fails_loudly(monkeypatch):
    pub, c = _wired(monkeypatch, echo=False)
    monkeypatch.setattr(pub, "_clients", lambda: c)
    with pytest.raises(RuntimeError, match="does not hold the card"):
        pub.handler({"run_id": RUN})


@pytest.mark.parametrize("bad", ["", None, "run-1", f"{RUN}/../x", "../tasks"])
def test_a_bad_run_id_is_refused_before_any_call(monkeypatch, bad):
    pub, c = _wired(monkeypatch)
    with pytest.raises(ValueError):
        pub.publish(c, bad)
    assert not c["s3"].gets and not c["sm"].updates


def test_the_baseline_is_the_untrained_eval_only_run_that_names_this_run(monkeypatch):
    rerun = "run-20261024T010000Z-11111111"
    pub, c = _wired(monkeypatch, rows=[{"run_id": BASE, "pipeline_mode": "eval_only"},
                                       {"run_id": rerun, "pipeline_mode": "eval_only"},
                                       {"run_id": RUN, "pipeline_mode": "lakehouse"}], extra={
        f"runs/{BASE}/manifest.json": {"params": {"source_run_id": RUN, "model_artifact_uri": HF}},
        f"runs/{BASE}/evaluation/gate.json": GATES["decisions"],
        f"runs/{rerun}/manifest.json": {"params": {"source_run_id": RUN, "model_artifact_uri": "s3://b/model.tar.gz"}}})
    card = json.loads(pub.publish(c, RUN) and c["sm"].updates[0]["ModelCard"]["ModelCardContent"])
    assert card["additional_information"]["custom_details"]["baseline_run_id"] == BASE, (
        "a re-judge of the trained artifact is not a baseline")


def test_a_job_the_rate_card_cannot_price_withholds_the_total(monkeypatch):
    pub, c = _wired(monkeypatch, jobs=[{"name": f"llmops-qlora-{RUN}-i0", "inst": "ml.g5.2xlarge", "secs": 60},
                                       {"name": f"llmops-eval-infer-{RUN}-i0", "inst": "ml.p5.48xlarge", "secs": 60}])
    cost = pub.run_cost(c["sm"], pub.Reader(c["s3"], "data-bucket"), RUN)
    assert cost["total_usd"] is None and cost["unpriced"] == [f"llmops-eval-infer-{RUN}-i0"]


# ── the publisher's role ─────────────────────────────────────────────────────
ROLE = json.loads((REPO / "deploy/iam/lambda_roles.json").read_text())["roles"]["publish_provenance"]["permissionsPolicy"]


def _stmts(action):
    return [s for s in ROLE["Statement"] if action in ([s["Action"]] if isinstance(s["Action"], str) else s["Action"])]


def test_the_role_can_update_only_student_packages_and_can_never_approve():
    allows = [s for s in _stmts("sagemaker:UpdateModelPackage") if s["Effect"] == "Allow"]
    assert [s["Resource"] for s in allows] == ["arn:aws:sagemaker:<REGION>:<ACCOUNT_ID>:model-package/llmops-student-*/*"]
    deny = next(s for s in _stmts("sagemaker:UpdateModelPackage") if s["Effect"] == "Deny")
    assert deny["Condition"] == {"Null": {"sagemaker:ModelApprovalStatus": "false"}}


def test_the_role_writes_s3_only_under_the_card_prefix():
    puts = _stmts("s3:PutObject")
    assert [s["Resource"] for s in puts] == ["arn:aws:s3:::<DATA_BUCKET>/runs/*/provenance/*"]
    assert not _stmts("sagemaker:DeleteModelPackage") and not _stmts("sagemaker:CreateModelPackage")
