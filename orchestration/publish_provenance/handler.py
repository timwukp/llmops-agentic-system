"""publish-provenance Lambda — writes a run's provenance card onto the model it registered.

The state machine invokes it after MonitorReport (`PublishProvenance`); invoked directly with the
same input it backfills a run that finished before this existed. Input `{run_id}`.

1. Reads the run's artifacts from S3, hashing each file as read (the card cites every one by URI
   and sha256).
2. Finds the model package this run registered: the `llmops-student-*` package whose
   `CustomerMetadataProperties.run_id` is this run (the deploy agent writes that key).
3. Finds the untrained baseline, if one exists: an `eval_only` run whose own manifest names this
   run as `source_run_id` and judged an `hf://` model (the Outcome tab's rule).
4. Prices the run's SageMaker jobs from the rate card by billable seconds.
5. Builds the card (pipeline/contracts/provenance.py), writes it with UpdateModelPackage, keeps a
   copy at runs/<id>/provenance/card.json, and reads the package back.

A run with no package (a run that stopped at the gate, an eval_only run) is not an error:
`{"published": false, "reason": ...}`. The state machine catches anything else and completes the
run, because a card that cannot be written must not fail a run that succeeded.

Env: DATA_BUCKET, RUNS_TABLE.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone

import boto3

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))  # repo layout
try:
    from pipeline.contracts import provenance as pv
except ImportError:  # Lambda bundle layout
    import provenance as pv  # type: ignore

RUN_ID = re.compile(r"^run-\d{8}T\d{6}Z-[0-9a-f]{8}$")
DATASET_ID = re.compile(r"^[a-z0-9][a-z0-9-]{2,80}$")
GROUP_PREFIX = "llmops-student"
#: Newest versions looked at per group. A run's package is registered at its own Deploy, so it is
#: among the newest; the bound keeps a long-lived group from turning one lookup into hundreds.
VERSIONS_PER_GROUP = 25
CARD_STATUS = "PendingReview"  # approval of the model version is a human step
COST_SCOPE = "SageMaker training and eval-inference jobs, by billable seconds"


def _clients():
    region = os.environ.get("AWS_REGION", "us-east-1")
    return {"s3": boto3.client("s3", region_name=region), "sm": boto3.client("sagemaker", region_name=region),
            "ddb": boto3.resource("dynamodb", region_name=region)}


class Reader:
    """S3 JSON reads under the data bucket, each hashed and recorded as evidence."""

    def __init__(self, s3, bucket):
        self.s3, self.bucket, self.sources = s3, bucket, {}

    def json(self, key: str, name: str | None = None):
        try:
            body = self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except Exception:  # noqa: BLE001 -- a missing artifact is recorded as missing, not fatal
            return None
        if name:
            self.sources[name] = {"uri": f"s3://{self.bucket}/{key}",
                                  "sha256": hashlib.sha256(body).hexdigest()}
        return json.loads(body)


def find_package(sm, run_id: str) -> dict | None:
    groups, token = [], None
    while True:
        r = sm.list_model_package_groups(NameContains=GROUP_PREFIX, MaxResults=100,
                                         **({"NextToken": token} if token else {}))
        groups += [g["ModelPackageGroupName"] for g in r.get("ModelPackageGroupSummaryList", [])]
        token = r.get("NextToken")
        if not token:
            break
    for g in groups:
        for p in sm.list_model_packages(ModelPackageGroupName=g, SortOrder="Descending",
                                        SortBy="CreationTime", MaxResults=VERSIONS_PER_GROUP
                                        ).get("ModelPackageSummaryList", []):
            d = sm.describe_model_package(ModelPackageName=p["ModelPackageArn"])
            if (d.get("CustomerMetadataProperties") or {}).get("run_id") == run_id:
                return d
    return None


def find_baseline(table, reader: Reader, run_id: str) -> dict | None:
    rows, kw = [], {"ProjectionExpression": "run_id, pipeline_mode"}
    while True:
        page = table.scan(**kw)
        rows += [r for r in page.get("Items", []) if r.get("pipeline_mode") == "eval_only"]
        if "LastEvaluatedKey" not in page:
            break
        kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    for r in sorted(rows, key=lambda r: r.get("run_id", ""), reverse=True):
        rid = r.get("run_id", "")
        if not RUN_ID.match(rid):
            continue
        p = (reader.json(f"runs/{rid}/manifest.json") or {}).get("params") or {}
        if p.get("source_run_id") == run_id and str(p.get("model_artifact_uri", "")).startswith("hf://"):
            return {"run_id": rid, "model": p["model_artifact_uri"],
                    "gate": reader.json(f"runs/{rid}/evaluation/gate.json", "baseline_gate")}
    return None


def run_cost(sm, reader: Reader, run_id: str) -> dict:
    rates = (reader.json("finops/rates/rate_card_latest.json", "rate_card") or {}).get("rates") or {}
    jobs, token = [], None
    while True:
        r = sm.list_training_jobs(NameContains=run_id, MaxResults=100, **({"NextToken": token} if token else {}))
        jobs += [j["TrainingJobName"] for j in r.get("TrainingJobSummaries", [])]
        token = r.get("NextToken")
        if not token:
            break
    out, unpriced = [], []
    for name in jobs:
        d = sm.describe_training_job(TrainingJobName=name)
        rc = d.get("ResourceConfig") or {}
        inst = rc.get("InstanceType", "")
        secs = float(d.get("BillableTimeInSeconds") or 0) * int(rc.get("InstanceCount") or 1)
        price = (rates.get(f"sagemaker:training:{inst}") or {}).get("unit_price")
        if price is None:
            unpriced.append(name)
            continue
        out.append({"job": name, "instance": inst, "billable_seconds": secs,
                    "cost_usd": round(secs / 3600.0 * float(price), 4)})
    # No total while any job is unpriced: a partial sum on a card reads as the run's cost.
    return {"jobs": out, "unpriced": unpriced, "scope": COST_SCOPE,
            "total_usd": None if unpriced else round(sum(j["cost_usd"] for j in out), 2)}


def gather(c, run_id: str) -> tuple[dict, Reader]:
    reader = Reader(c["s3"], os.environ["DATA_BUCKET"])
    manifest = reader.json(f"runs/{run_id}/manifest.json", "manifest") or {}
    params = manifest.get("params") or {}
    dataset = params.get("lakehouse_dataset") or (manifest.get("lakehouse") or {}).get("dataset")
    lake = {}
    if dataset and DATASET_ID.match(dataset):
        for name in ("export", "silver", "gold"):
            lake[name] = reader.json(f"lakehouse/reports/{dataset}/{name}.json", name)
    artifacts = {
        "run_id": run_id, "manifest": manifest,
        "metrics": reader.json(f"runs/{run_id}/training/metrics.json", "metrics"),
        "report": reader.json(f"runs/{run_id}/evaluation/report.json", "report"),
        "gate": reader.json(f"runs/{run_id}/evaluation/gate.json", "gate"),
        **lake,
        "baseline": find_baseline(c["ddb"].Table(os.environ["RUNS_TABLE"]), reader, run_id),
        "cost": run_cost(c["sm"], reader, run_id),
    }
    return artifacts, reader


def publish(c, run_id: str) -> dict:
    if not RUN_ID.match(run_id or ""):
        raise ValueError(f"run_id must look like run-20261023T160741Z-65fec5d8, got {run_id!r}")
    package = find_package(c["sm"], run_id)
    if package is None:
        return {"published": False, "run_id": run_id, "reason": "no llmops-student model package names this run"}
    existing = package.get("ModelCard") or {}
    if existing.get("ModelCardStatus") == "Archived":
        return {"published": False, "run_id": run_id, "reason": "the model card is Archived and cannot be updated"}
    artifacts, reader = gather(c, run_id)
    artifacts["sources"] = reader.sources
    card = pv.build_card(artifacts)
    body = pv.card_json(card)
    sha = hashlib.sha256(body.encode()).hexdigest()
    key = f"runs/{run_id}/provenance/card.json"
    c["s3"].put_object(Bucket=os.environ["DATA_BUCKET"], Key=key, ContentType="application/json",
                       Body=json.dumps({"run_id": run_id, "model_package_arn": package["ModelPackageArn"],
                                        "card_sha256": sha, "card": card["content"], "summary": card["summary"],
                                        "cost": artifacts["cost"],
                                        "written_at": datetime.now(timezone.utc).isoformat()},
                                       indent=1, default=str).encode())
    # The first card may set its status; a later update must carry the content alone. Live,
    # 2026-10-24: "Both ModelCardContent and ModelCardStatus are provided. Cannot determine which to
    # use." on the second write to a package, while the same call had succeeded on the first.
    c["sm"].update_model_package(
        ModelPackageArn=package["ModelPackageArn"],
        ModelCard={"ModelCardContent": body} if existing else {"ModelCardContent": body, "ModelCardStatus": CARD_STATUS},
        CustomerMetadataProperties={"provenance_card_uri": f"s3://{os.environ['DATA_BUCKET']}/{key}",
                                    "provenance_card_sha256": sha})
    back = c["sm"].describe_model_package(ModelPackageName=package["ModelPackageArn"])
    written = (back.get("ModelCard") or {}).get("ModelCardContent")
    confirmed = bool(written) and json.loads(written) == card["content"]
    return {"published": True, "confirmed": confirmed, "run_id": run_id,
            "model_package_arn": package["ModelPackageArn"], "card_uri": f"s3://{os.environ['DATA_BUCKET']}/{key}",
            "card_sha256": sha, "summary": card["summary"]}


def handler(event, context=None):
    out = publish(_clients(), (event or {}).get("run_id"))
    print(json.dumps({k: v for k, v in out.items() if k != "model_package_arn"}, default=str))
    if out.get("published") and not out.get("confirmed"):
        raise RuntimeError("the registry does not hold the card that was written")
    return out
