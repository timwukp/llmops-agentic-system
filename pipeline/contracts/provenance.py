"""The provenance card: one run's evidence, shaped as the model card a registered model carries.

Every model version in the SageMaker Model Registry includes a simplified model card
(`ModelPackageModelCard`, "Model package model card schema" in sagemaker/latest/dg/
model-registry-details.html), and SageMaker Unified Studio renders a version's page from it. The
registry's `CustomerMetadataProperties`, which the deploy agent fills today, are not shown there at
all (read on the live SMUS page, 2026-10-24). So the evidence goes into the card.

Pure: no AWS call, no clock. The publisher (orchestration/publish_provenance) reads the run's
artifacts and passes them in; the console's Outcome tab reads gates through `gate_view` too, so the
page and the registry cannot disagree about a run.

The artifacts are written by agents, and their shapes drift between runs of the same prompt. Three
gate shapes and two metrics shapes are on record (below). Each reader names the shapes it accepts
and reports which one it met; a shape it does not know is reported as such on the card, never read
as "no gate".
"""
from __future__ import annotations

import json

BUILDER = "provenance-card/1"

#: The model package model card schema's limits, for the fields this builder writes. The schema is
#: `additionalProperties: false` throughout, so a key outside ALLOWED_KEYS is a rejected card.
LIMITS = {"string": 1024, "observation": 1024, "evaluation_observation": 2096, "purpose": 2048,
          "caveats": 2048, "hyper_value": 255, "metric_name": 255, "simple_string": 63,
          "metrics": 50, "hyper_parameters": 100, "datasets": 15, "eval_datasets": 10,
          "group_name": 63, "content": 100000}
ALLOWED_KEYS = {
    "": {"model_overview", "intended_uses", "training_details", "evaluation_details",
         "additional_information"},
    "model_overview": {"model_creator", "model_artifact"},
    "intended_uses": {"purpose_of_model", "intended_uses", "factors_affecting_model_efficiency",
                      "risk_rating", "explanations_for_risk_rating"},
    "training_details": {"objective_function", "training_observations", "training_job_details"},
    "training_job_details": {"training_arn", "training_datasets", "training_environment",
                             "training_metrics", "user_provided_training_metrics",
                             "hyper_parameters", "user_provided_hyper_parameters"},
    "evaluation_detail": {"name", "evaluation_observation", "evaluation_job_arn", "datasets",
                          "metadata", "metric_groups"},
    "additional_information": {"ethical_considerations", "caveats_and_recommendations",
                               "custom_details"},
}

#: gate.json, as the eval agent has written it:
#:   gate_results  -- wilson95_ci_low/high, n            (2026-10-16 trained run)
#:   decisions     -- ci_low/high, counts "47/120"       (2026-10-23 untrained baseline)
#:   results       -- ci_low/high, count  "120/120"      (2026-10-23 demo run)
#:   gates_applied -- ci_low/high, n, detail "85/120 …"   (2026-10-26 identifier-grounding run)
GATE_SHAPES = ("gate_results", "decisions", "results", "gates_applied")
#: training/metrics.json, as the analyze agent has written it (the trainer's own file is flat):
#:   trainer_metrics + training_job + loss_curve_assessment   (2026-10-16)
#:   trainer_metrics + job + health_assessment                (2026-10-23)
METRICS_JOB_KEYS = ("training_job", "job")
METRICS_VERDICT_KEYS = ("loss_curve_assessment", "health_assessment")


def _cut(s, n) -> str:
    s = "" if s is None else str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _pct(v) -> str:
    return "?" if v is None else f"{100 * v:.1f}%"


# ── readers: one shape out, whichever shape went in ──────────────────────────
def gate_view(gate: dict | None) -> dict | None:
    """A gate verdict in one shape. `shape` names the source key it was read from, or `unknown`."""
    if not gate:
        return None
    found = [k for k in GATE_SHAPES if isinstance(gate.get(k), dict)]
    metrics = {}
    for key in found:
        for name, g in gate[key].items():
            if not (isinstance(g, dict) and "value" in g):
                continue
            counts = g.get("counts") if isinstance(g.get("counts"), str) else g.get("count")
            k = n = None
            if isinstance(counts, str) and "/" in counts:
                k, n = (x.strip() for x in counts.split("/", 1))
            n = g.get("n", n)
            metrics[name] = {"value": _num(g.get("value")), "threshold": _num(g.get("threshold")),
                             "ci_low": _num(g.get("wilson95_ci_low", g.get("ci_low"))),
                             "ci_high": _num(g.get("wilson95_ci_high", g.get("ci_high"))),
                             "k": k, "n": None if n is None else str(n), "verdict": g.get("verdict")}
    return {"gate_passed": gate.get("gate_passed"), "metrics": metrics,
            "decided_at": gate.get("decided_at"), "shape": "+".join(found) or "unknown"}


def training_view(raw: dict | None) -> dict | None:
    if not raw:
        return None
    trainer = raw.get("trainer_metrics") or raw
    job_key = next((k for k in METRICS_JOB_KEYS if isinstance(raw.get(k), dict)), None)
    verdict_key = next((k for k in METRICS_VERDICT_KEYS if isinstance(raw.get(k), dict)), None)
    job = raw.get(job_key) or {}
    return {
        "trainer": trainer,
        "job": {"name": job.get("name") or job.get("job_name"), "arn": job.get("arn"),
                "instance": job.get("instance_type"),
                "billable_seconds": job.get("billable_seconds", job.get("billable_time_seconds"))},
        "loss_verdict": (raw.get(verdict_key) or {}).get("verdict"),
        "shape": "+".join(filter(None, ("trainer_metrics" if "trainer_metrics" in raw else "flat",
                                        job_key, verdict_key))),
    }


#: The eval agent's answer files, as written:
#:   evaluation/i0/predictions.jsonl  prompt_id, answer        + judge verdict "match"/"no_match"  (2026-10-16)
#:   evaluation/student_answers.jsonl item_id, student_answer  + judge answer_match "True"/"False" (2026-10-23)
ANSWER_FILES = ("predictions.jsonl", "student_answers.jsonl")


def _truthy(v):
    return v is True or str(v).strip().lower() == "true"


def prediction_view(row: dict) -> dict:
    """One answered question in one shape, from either answers file."""
    return {"prompt_id": row.get("prompt_id") or row.get("item_id"), "intent": row.get("intent"),
            "lang": row.get("lang"), "prompt": row.get("prompt"), "reference": row.get("reference"),
            "answer": row.get("answer") if row.get("answer") is not None else row.get("student_answer")}


def judgement_view(row: dict) -> dict:
    """A judged answer in one shape: `verdict` is "match"/"no_match" whichever field carried it."""
    verdict = row.get("verdict")
    if verdict is None and "answer_match" in row:
        verdict = "match" if _truthy(row["answer_match"]) else "no_match"
    fv = row.get("format_valid")
    return {"item_id": row.get("item_id") or row.get("prompt_id"), "verdict": verdict,
            "format_valid": None if fv is None else _truthy(fv)}


# ── the card ─────────────────────────────────────────────────────────────────
def _metric_note(m: dict, eval_sha: str | None) -> str:
    ci = (f"Wilson 95% [{_pct(m['ci_low'])}, {_pct(m['ci_high'])}]"
          if m.get("ci_low") is not None else "no interval recorded")
    count = f"{m['k']}/{m['n']}" if m.get("k") else (f"n={m['n']}" if m.get("n") else "n=?")
    return _cut(f"held-out acceptance gate: {count}, {ci}, threshold {m.get('threshold')}, "
                f"verdict {m.get('verdict')}; eval set sha256 {eval_sha or '?'}", LIMITS["string"])


def _short_note(m: dict) -> str:
    """The metric groups' note: the full one is already on the custom metric of the same name."""
    count = f"{m['k']}/{m['n']}" if m.get("k") else f"n={m.get('n') or '?'}"
    ci = f"[{_pct(m['ci_low'])}, {_pct(m['ci_high'])}]" if m.get("ci_low") is not None else "no interval"
    return _cut(f"{count}, Wilson 95% {ci}, {m.get('verdict')}", LIMITS["string"])


def _group(name: str, gate: dict) -> dict:
    return {"name": name, "metric_data": [
        {"name": _cut(k, LIMITS["metric_name"]), "type": "number", "value": m["value"], "notes": _short_note(m)}
        for k, m in sorted(gate["metrics"].items()) if m["value"] is not None][: LIMITS["metrics"]]}


def _hyper(name, value):
    v = _cut(value, LIMITS["hyper_value"])
    return {"name": _cut(name, LIMITS["metric_name"]), "value": v} if v else None


def build_card(a: dict) -> dict:
    """`a`: run_id, manifest, metrics, report, gate, export, silver, gold, baseline, cost, sources.
    Returns {"content": card, "summary": {...}}; `content` is what UpdateModelPackage takes."""
    run_id = a["run_id"]
    manifest, report = a.get("manifest") or {}, a.get("report") or {}
    params = manifest.get("params") or {}
    dataset = params.get("lakehouse_dataset") or (manifest.get("lakehouse") or {}).get("dataset")
    student = (manifest.get("models") or {}).get("student") or "?"
    gate = gate_view(a.get("gate")) or {"metrics": {}, "gate_passed": None, "shape": "missing"}
    train = training_view(a.get("metrics"))
    trainer = (train or {}).get("trainer") or {}
    eval_set = report.get("eval_set") or {}
    eval_sha = eval_set.get("sha256")
    files = (a.get("export") or {}).get("files") or {}
    silver, gold = a.get("silver") or {}, a.get("gold") or {}
    pii = ((silver.get("rules") or {}).get("redact_pii") or {}).get("count")
    base = a.get("baseline")
    base_gate = gate_view((base or {}).get("gate")) if base else None
    cost = a.get("cost")

    # SMUS shows a metric's name and score only (the notes stay in the registry), so the interval,
    # the threshold and the baseline are rows of their own: the approver sees them on the page.
    metrics = []

    def row(name, value, note):
        if _num(value) is not None:
            metrics.append({"name": _cut(name, LIMITS["metric_name"]), "value": _num(value),
                            "notes": _cut(note, LIMITS["string"])})

    for name, m in sorted(gate["metrics"].items()):
        note = _metric_note(m, eval_sha)
        row(f"gate.{name}", m["value"], note)
        row(f"gate.{name}.wilson95_low", m["ci_low"], note)
        row(f"gate.{name}.wilson95_high", m["ci_high"], note)
        row(f"gate.{name}.threshold", m["threshold"], note)
        b = (base_gate or {}).get("metrics", {}).get(name)
        if b:
            row(f"baseline.{name}", b["value"], f"untrained {base.get('model')} on the same questions "
                                                f"(run {base.get('run_id')}): {_metric_note(b, eval_sha)}")
    for key in ("final_train_loss", "final_eval_loss"):
        row(f"train.{key}", trainer.get(key), f"loss curve verdict: {(train or {}).get('loss_verdict') or '?'}")
    row("data.pii_redactions", pii, "Bronze -> Silver redactions (silver.json)")
    row("run.cost_usd", (cost or {}).get("total_usd"), (cost or {}).get("scope") or "")

    lora = trainer.get("lora") if isinstance(trainer.get("lora"), dict) else {}
    hypers = [_hyper("base_model", trainer.get("model_id") or student),
              _hyper("base_model_revision", trainer.get("model_revision")),
              _hyper("max_length", trainer.get("max_length")),
              _hyper("learning_rate", trainer.get("learning_rate")),
              _hyper("epochs_completed", trainer.get("epochs_completed")),
              _hyper("global_step", trainer.get("global_step")),
              *[_hyper(f"lora.{k}", v) for k, v in sorted(lora.items()) if not isinstance(v, (dict, list))]]
    for split in ("train", "eval", "acceptance"):
        f = files.get(split) or {}
        hypers += [_hyper(f"data.{split}.sha256", f.get("sha256")),
                   _hyper(f"data.{split}.rows", f.get("rows")),
                   _hyper(f"data.{split}.snapshot_id", f.get("snapshot_id"))]
    hypers = [h for h in hypers if h][: LIMITS["hyper_parameters"]]

    journey = (f"Data: dataset {dataset or '?'}; Silver {silver.get('pairs', '?')} pairs, "
               f"{pii if pii is not None else '?'} PII redactions; Gold {gold.get('train', '?')} train / "
               f"{gold.get('eval', '?')} eval. Training: {(train or {}).get('job', {}).get('instance') or '?'}, "
               f"{(train or {}).get('job', {}).get('billable_seconds') or '?'} billable s, loss verdict "
               f"{(train or {}).get('loss_verdict') or '?'}.")
    job_details = {
        "training_datasets": [f["uri"] for s in ("train", "eval") if (f := files.get(s)) and f.get("uri")]
        [: LIMITS["datasets"]],
        # Measured with a probe version (2026-10-24): SMUS's "Custom metrics" renders
        # training_metrics and its "Hyperparameters" renders hyper_parameters; the user_provided_*
        # twins, ModelMetrics statistics and both metadata maps are not shown.
        "training_metrics": metrics[: LIMITS["metrics"]],
        "hyper_parameters": hypers,
    }
    if (train or {}).get("job", {}).get("arn"):
        job_details["training_arn"] = _cut(train["job"]["arn"], LIMITS["string"])

    groups = [g for g in (_group("acceptance gate", gate),
                          _group("untrained baseline", base_gate) if base_gate else None) if g and g["metric_data"]]
    verdict = {True: "PASSED", False: "FAILED"}.get(gate.get("gate_passed"), "NOT RECORDED")
    evaluation = {"name": "held-out acceptance set",
                  "evaluation_observation": _cut(
                      f"Gate {verdict} ({gate['shape']} shape). {eval_set.get('rows', '?')} held-out "
                      f"questions, scored against the reference answers in the file. "
                      f"{(a.get('gate') or {}).get('decision_rule') or ''}", LIMITS["evaluation_observation"]),
                  "metadata": {k: _cut(v, LIMITS["string"]) for k, v in (
                      ("eval_set_sha256", eval_sha), ("gate_passed", str(gate.get("gate_passed")).lower()),
                      ("gate_shape", gate["shape"]), ("decided_at", gate.get("decided_at"))) if v is not None},
                  "metric_groups": groups}
    acc = (files.get("acceptance") or {}).get("uri")
    if acc:
        evaluation["datasets"] = [acc][: LIMITS["eval_datasets"]]

    custom = {"run_id": run_id, "dataset": dataset, "builder": BUILDER,
              "gate_shape": gate["shape"], "metrics_shape": (train or {}).get("shape"),
              "pii_redactions": pii, "gold_train_rows": gold.get("train"), "gold_eval_rows": gold.get("eval"),
              "cost_usd": (f"{cost['total_usd']:.2f} ({cost.get('scope', 'SageMaker jobs')})"
                           if cost and cost.get("total_usd") is not None else None),
              "baseline_run_id": (base or {}).get("run_id")}
    for name, src in sorted((a.get("sources") or {}).items()):
        custom[f"evidence.{name}"] = f"{src.get('uri')} sha256 {src.get('sha256')}"
    content = {
        "model_overview": {"model_creator": _cut(f"llmops pipeline (Step Functions + AgentCore harnesses), "
                                                 f"run {run_id}", LIMITS["string"]),
                           "model_artifact": [u for u in (report.get("model_artifact_uri"),) if u]},
        "intended_uses": {
            "purpose_of_model": _cut(f"{student} fine-tuned (QLoRA) on dataset {dataset or '?'}.", LIMITS["purpose"]),
            "intended_uses": _cut(f"Answers in the style of the training data. Acceptance gate {verdict}; "
                                  "deployment requires a human approval of this model version.", LIMITS["purpose"]),
            "risk_rating": "Unknown",
            "explanations_for_risk_rating": "No risk assessment is recorded for this run; a reviewer sets it."},
        "training_details": {"training_observations": _cut(journey, LIMITS["observation"]),
                             "training_job_details": job_details},
        "evaluation_details": [evaluation],
        "additional_information": {
            "caveats_and_recommendations": _cut(
                "Every value on this card was copied by code from the run's own artifacts (see the evidence.* "
                "details for each file and its sha256); none was typed by an agent or a person. answer_match "
                "counts an answer only if it contains the reference verbatim.", LIMITS["caveats"]),
            "custom_details": {k: _cut(v, LIMITS["string"]) for k, v in custom.items() if v is not None}},
    }
    size = len(json.dumps(content, sort_keys=True, separators=(",", ":")))
    if size > LIMITS["content"]:
        # Refused rather than trimmed: a card silently missing its tail reads as complete.
        raise ValueError(f"provenance card is {size} characters; the registry takes {LIMITS['content']}")
    return {"content": content, "summary": {
        "run_id": run_id, "gate_passed": gate.get("gate_passed"), "gate_shape": gate["shape"],
        "metrics": len(metrics), "hyper_parameters": len(hypers), "baseline": bool(base_gate),
        "cost_usd": (cost or {}).get("total_usd")}}


def card_json(card: dict) -> str:
    """The exact string written to the registry; its sha256 is what the read-back compares."""
    return json.dumps(card["content"], sort_keys=True, separators=(",", ":"))
