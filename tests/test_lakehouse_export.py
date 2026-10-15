"""Gold → training extract (pipeline/lakehouse/export.py) and the driver's re-verification of it.

The reproducibility claim this phase rests on is "this model was trained on gold.qlora_train at
snapshot N, and these are its bytes". So the tests pin the three things that make the claim
checkable: the extract's bytes depend on the snapshot's rows and nothing else (order, file split,
compression), the SQL that produces them cannot be steered by its inputs, and the driver measures
the file itself rather than accepting the digest the stage wrote.
"""
from __future__ import annotations

import gzip
import hashlib
import importlib.util
import io
import json
import pathlib
import random

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ex = _load("lh_export", "pipeline/lakehouse/export.py")
tf = _load("lh_transform_for_export", "pipeline/lakehouse/transform.py")
gen = _load("lh_gen_for_export", "pipeline/lakehouse/generate.py")
lh = _load("lh_deploy_for_export", "deploy/10_lakehouse.py")
driver = _load("driver_for_export", "orchestration/harness_driver/handler.py")
storage = _load("storage_for_export", "deploy/03_storage.py")


def _gold_rows(seed=7, scale="small"):
    ds = gen.generate(seed, scale)
    names = {i["value"] for i in ds["expected_defects"]["pii"]["items"] if i["type"] == "NAME"}
    rows, _ = tf.bronze_to_silver(ds["tables"]["conversation_events"], [p["prompt"] for p in ds["eval_prompts"]],
                                  name_detector=lambda t: [n for n in names if n in t])
    return tf.silver_to_gold(rows)


# ── determinism ──────────────────────────────────────────────────────────────
def test_the_extract_bytes_depend_on_the_rows_not_their_order_or_file_split():
    train, _ = _gold_rows()
    shuffled = list(train)
    random.Random(3).shuffle(shuffled)
    cut = len(shuffled) // 3
    parts = [gzip.compress("\n".join(json.dumps(r) for r in shuffled[:cut]).encode()),
             "\n".join(json.dumps(r) for r in shuffled[cut:]).encode()]  # one gzipped, one plain
    assert ex.to_jsonl(ex.rows_from_unload(parts)) == ex.to_jsonl(train)
    assert ex.sha256(ex.to_jsonl(train)) == ex.sha256(ex.to_jsonl(list(reversed(train))))


def test_the_extract_is_one_messages_record_per_pair_in_the_trainer_format():
    train, _ = _gold_rows()
    lines = ex.to_jsonl(train).decode().splitlines()
    assert len(lines) == len(train)
    first = json.loads(lines[0])
    assert [m["role"] for m in first["messages"]] == ["user", "assistant"]


def test_the_export_mapping_is_the_transform_mapping():
    """export.py carries its own copy so the export job ships one file; the two must not drift."""
    for row in _gold_rows()[0][:20] + [{"instruction": "Q", "input": "ctx", "output": "A", "pair_id": "p"}]:
        assert ex.to_messages(row) == tf.to_messages(row)


# ── SQL that cannot be steered ───────────────────────────────────────────────
#: An Iceberg snapshot id is a 64-bit int, past 2**53, where a float round-trip would corrupt it.
#: Computed rather than written out: a literal 19-digit run reads as an account-id candidate.
SNAP = 2**62 + 7


def test_unload_sql_pins_the_snapshot_and_orders_by_pair_id():
    sql = ex.unload_sql("qlora_train", SNAP, "s3://b/lakehouse/extracts/d/1/unload/x/")
    assert f"FOR VERSION AS OF {SNAP}" in sql and "ORDER BY pair_id" in sql
    assert float(SNAP) != SNAP, "the id must be one a float would corrupt"
    assert "TO 's3://b/lakehouse/extracts/d/1/unload/x/'" in sql


@pytest.mark.parametrize("table,snap,dest", [
    ("qlora_train; DROP", 1, "s3://b/x/"), ("silver.instruction_pairs", 1, "s3://b/x/"),
    ("qlora_train", "1 OR 1=1", "s3://b/x/"), ("qlora_train", True, "s3://b/x/"), ("qlora_train", 0, "s3://b/x/"),
    ("qlora_train", 1, "s3://b/x'/"), ("qlora_train", 1, "/tmp/x/"), ("qlora_train", 1, "s3://b/x"),
])
def test_unload_sql_refuses_what_it_would_have_to_quote(table, snap, dest):
    with pytest.raises((ValueError, TypeError)):
        ex.unload_sql(table, snap, dest)


def test_the_manifest_insert_escapes_strings_and_validates_numbers():
    row = {"dataset_version": "seed7-demo-x", "table_name": "gold.qlora_train", "snapshot_id": 42,
           "export_uri": "s3://b/k'; DROP TABLE x; --", "sha256": "a" * 64, "row_count": 251,
           "dq_score": None, "pii_report_uri": None, "run_id": "r", "created_ts": "2026-10-15T00:00:00.000Z"}
    sql = ex.manifest_insert_sql(row)
    assert "'s3://b/k''; DROP TABLE x; --'" in sql and ", 42, " in sql and ", 251, NULL, NULL, 'r', " in sql
    with pytest.raises(ValueError):
        ex.manifest_insert_sql({**row, "sha256": "not-a-digest"})
    with pytest.raises(ValueError):
        ex.manifest_insert_sql({**row, "row_count": "251; --"})


def test_extracts_live_under_the_macie_scannable_prefix():
    assert ex.extract_prefix("seed7-demo-x", 42) == "lakehouse/extracts/seed7-demo-x/42"
    assert ex.EXTRACTS_PREFIX == lh.EXTRACTS_PREFIX == storage.GOLD_EXTRACTS_PREFIX
    assert driver.GOLD_EXTRACTS_PREFIX == ex.EXTRACTS_PREFIX + "/"
    with pytest.raises(ValueError):
        ex.extract_prefix("../customer-data", 1)


# ── the driver measures the file ─────────────────────────────────────────────
class _S3:
    def __init__(self, objects):
        self.objects, self.reads = objects, []

    def get_object(self, Bucket, Key):
        self.reads.append((Bucket, Key))
        if (Bucket, Key) not in self.objects:
            raise RuntimeError("NoSuchKey")
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}


BODY = b'{"messages":[1]}\n{"messages":[2]}\n'
KEY = "lakehouse/extracts/seed7-demo-x/42/train.jsonl"


def _claim(sha=hashlib.sha256(BODY).hexdigest(), uri=f"s3://data/{KEY}"):
    return {"extract_uri": uri, "extract_sha256": sha, "extract_snapshot_id": 42}


def test_the_driver_verifies_a_true_claim_from_the_bytes():
    att = driver._verify_gold_extract(_S3({("data", KEY): BODY}), "data", _claim())
    assert att["verified"] is True and att["rows"] == 2 and att["snapshot_id"] == "42"
    assert att["computed_sha256"] == hashlib.sha256(BODY).hexdigest()


def test_the_driver_records_a_false_claim_as_false():
    att = driver._verify_gold_extract(_S3({("data", KEY): BODY + b"tampered\n"}), "data", _claim())
    assert att["verified"] is False and att["computed_sha256"] != att["claimed_sha256"]


@pytest.mark.parametrize("uri", [
    f"s3://someone-elses-bucket/{KEY}", "s3://data/customer-data/raw.jsonl", "s3://data/runs/x/train.jsonl",
    "s3://data/lakehouse/extracts/../../customer-data/x", "https://example.com/train.jsonl",
])
def test_the_driver_refuses_to_read_anything_but_its_own_extracts(uri):
    s3 = _S3({})
    att = driver._verify_gold_extract(s3, "data", _claim(uri=uri))
    assert att["verified"] is False and "refused" in att["error"] and s3.reads == []


def test_an_unreadable_extract_is_a_finding_not_a_crash():
    att = driver._verify_gold_extract(_S3({}), "data", _claim())
    assert att["verified"] is False and "NoSuchKey" in att["error"]


# ── the job and its grants ───────────────────────────────────────────────────
def test_the_export_job_is_a_python_shell_job_shipping_export_py():
    name, script = lh.JOBS["export"]
    d = lh.job_definition(name, script, "r", "data")
    assert d["Command"]["Name"] == "pythonshell" and d["MaxCapacity"] == 0.0625 and "WorkerType" not in d
    assert d["DefaultArguments"]["--extra-py-files"].endswith(f"{lh.GLUE_PREFIX}/export.py")
    src = (lh.GLUE_SRC / script).read_text()
    assert "import export" in src and "unload_sql" in src and "manifest_insert_sql" in src


def test_the_export_grants_are_the_workgroup_and_the_extract_prefix():
    pol = lh.glue_role_policy("us-east-1", "123456789012", "data", {lh.RAW: "k1", lh.CURATED: "k2"})
    by = {st["Sid"]: st for st in pol["Statement"]}
    assert by["ExportQueries"]["Resource"].endswith(f":workgroup/{lh.WORKGROUP}")
    assert f"arn:aws:s3:::data/{lh.EXTRACTS_PREFIX}/*" in by["ExtractsReadWrite"]["Resource"]
    assert all("Delete" not in a for st in pol["Statement"] for a in (st["Action"] if isinstance(st["Action"], list) else [st["Action"]]))


def test_macie_coverage_of_extracts_is_a_job_of_its_own():
    class _Macie:
        def get_macie_session(self):
            return {"status": "ENABLED"}

        def list_classification_jobs(self):  # the production shape: no scoping, no s3JobDefinition
            return {"items": [{"name": "llmops-customer-data-pii", "jobId": "j1", "jobStatus": "RUNNING",
                               "jobType": "SCHEDULED",
                               "bucketDefinitions": [{"accountId": "1", "buckets": ["data"]}]}]}

        def describe_classification_job(self, jobId):
            return {"s3JobDefinition": {"bucketDefinitions": [{"accountId": "1", "buckets": ["data"]}],
                                        "scoping": {"includes": {"and": [{"simpleScopeTerm": {
                                            "key": "OBJECT_KEY", "values": ["customer-data/"]}}]}}}}
    res = storage.ensure_pii_scan(_Macie(), "data", "1", dry=False, enable=False, prefix=storage.GOLD_EXTRACTS_PREFIX)
    assert res["job"] == "llmops-lakehouse-extracts-pii"
    assert "NO JOB SCANS lakehouse/extracts/" in res["coverage"] and "Silver rules" in res["coverage"]
    assert "also_covered_by" not in res, "a customer-data job must not count as extract coverage"
    res = storage.ensure_pii_scan(_Macie(), "data", "1", dry=True, enable=True, prefix=storage.GOLD_EXTRACTS_PREFIX)
    assert res["coverage"] == "would create SCHEDULED daily job over lakehouse/extracts/"
