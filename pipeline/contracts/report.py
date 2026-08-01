"""Canonical run-report writer + stage_complete normalizer.

The ops console reads exactly one contract file per run:
    s3://<bucket>/reports/run-latest/test-report-latest.json
The harness driver is the only writer — agents CLAIM results via the
stage_complete inline function, the driver verifies and publishes the
canonical report (orchestrator-side canonical publish; trust but verify).

normalize_stage_complete() absorbs field drift from the LLM side: agents
occasionally emit ``artifacts`` for ``outputs``, ``results`` for ``metrics``,
``summary`` for ``evidence``. Normalizing before consumption keeps one schema
downstream instead of N tolerant readers.

Only stdlib + boto3 (Lambda-safe, no external deps).
"""
from __future__ import annotations

import datetime
import json
from typing import Any

#: Where the ops console expects the canonical report.
REPORT_KEY = "reports/run-latest/test-report-latest.json"

# Accepted aliases, in priority order (canonical name first).
_OUTPUT_KEYS = ("outputs", "artifacts", "output", "artifact_uris", "s3_uris")
_METRIC_KEYS = ("metrics", "results", "measurements", "stats")
_EVIDENCE_KEYS = ("evidence", "summary", "proof", "notes")


def _first_present(args: dict, keys: tuple) -> Any:
    """Return the value of the first key PRESENT in args (even if falsy).

    Presence matters: outputs=[] is a valid, deliberate claim of
    "empty but complete" and must not fall through to a later alias.
    """
    for key in keys:
        if key in args:
            return args[key]
    return None


def normalize_stage_complete(args: dict | None) -> dict:
    """Coerce a stage_complete toolUse input to the canonical contract shape.

    Returns a dict with exactly: stage, task, outputs (list[str]),
    metrics (dict), evidence (str). Tolerates alias drift and scalar/list
    type drift; never raises on messy-but-parseable input.
    """
    args = dict(args or {})

    outputs = _first_present(args, _OUTPUT_KEYS)
    if outputs is None:
        outputs = []
    if isinstance(outputs, str):
        outputs = [outputs]
    outputs = [str(o) for o in outputs if o is not None]

    metrics = _first_present(args, _METRIC_KEYS)
    if metrics is None:
        metrics = {}
    if isinstance(metrics, str):
        # agents sometimes emit metrics as a JSON string — parse before wrapping
        try:
            parsed = json.loads(metrics)
            metrics = parsed if isinstance(parsed, dict) else {"value": parsed}
        except json.JSONDecodeError:
            metrics = {"value": metrics}
    elif not isinstance(metrics, dict):
        metrics = {"value": metrics}

    evidence = _first_present(args, _EVIDENCE_KEYS)
    if evidence is None:
        evidence = ""
    if not isinstance(evidence, str):
        evidence = json.dumps(evidence, default=str)

    return {
        "stage": str(args.get("stage", "")),
        "task": str(args.get("task", "")),
        "outputs": outputs,
        "metrics": metrics,
        "evidence": evidence,
    }


def build_run_report(manifest: dict) -> dict:
    """Derive the ops-console report document from a run manifest."""
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    stages: dict = manifest.get("stages", {}) or {}

    findings = []
    passed = failed = 0
    stage_summaries = {}
    for name, entry in stages.items():
        entry = entry or {}
        status = str(entry.get("status", "unknown"))
        stage_summaries[name] = {
            "status": status,
            "started_at": entry.get("started_at"),
            "ended_at": entry.get("ended_at"),
            "outputs": entry.get("outputs", []),
            "metrics": entry.get("metrics", {}),
        }
        if status in ("completed", "passed", "succeeded"):
            passed += 1
        elif status in ("failed", "error"):
            failed += 1
            findings.append({
                "severity": "high",
                "stage": name,
                "title": f"Stage {name} failed",
                "detail": entry.get("evidence") or entry.get("error") or "no detail recorded",
            })
        elif status == "escalated":
            failed += 1
            findings.append({
                "severity": "critical",
                "stage": name,
                "title": f"Stage {name} escalated to human",
                "detail": entry.get("evidence") or "see SNS notification / DDB stage events",
            })

    return {
        "generated_at": now,
        "run_id": manifest.get("run_id", "unknown"),
        "iteration": manifest.get("iteration", 0),
        "trigger_source": manifest.get("trigger_source"),
        "stages": stage_summaries,
        "findings": findings,
        "pass_counts": {
            "total": len(stages),
            "passed": passed,
            "failed": failed,
        },
        "models": manifest.get("models", {}),
    }


def write_run_report(s3: Any, bucket: str, manifest: dict) -> dict:
    """Build the report from the manifest and publish it to the console key.

    ``s3`` is an injected boto3 S3 client (injectable for tests).
    Returns the report document that was written.
    """
    report = build_run_report(manifest)
    s3.put_object(
        Bucket=bucket,
        Key=REPORT_KEY,
        Body=json.dumps(report, indent=2, default=str).encode("utf-8"),
        ContentType="application/json",
    )
    return report
