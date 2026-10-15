"""The console's read-only Data tab: what /api/data and /api/data/run return, and what they
must never do.

The lakehouse tables are SSE-KMS and Bronze holds PII. The tab therefore reads only the Glue
jobs' reports and the S3 Tables metadata APIs; these tests pin that, along with the dataset-id
check that keeps a query parameter from becoming an S3 path of the caller's choosing.
"""
from __future__ import annotations

import io
import json
import pathlib
import re

import pytest

from test_console_cost import console  # noqa: F401  (fixture)

REPO = pathlib.Path(__file__).resolve().parents[1]
DATASET = "seed7-demo-20261014-110922"


class _S3:
    def __init__(self, objects):
        self.objects, self.gets = objects, []

    def get_object(self, Bucket, Key):
        self.gets.append(Key)
        if Key not in self.objects:
            raise RuntimeError("NoSuchKey")
        return {"Body": io.BytesIO(json.dumps(self.objects[Key]).encode())}

    def list_objects_v2(self, Bucket, Prefix, Delimiter=None):
        ids = sorted({k[len(Prefix):].split("/")[0] for k in self.objects if k.startswith(Prefix)})
        return {"CommonPrefixes": [{"Prefix": f"{Prefix}{i}/"} for i in ids]}


class _SSM:
    def get_parameter(self, Name):
        return {"Parameter": {"Value": f"arn:aws:s3tables:us-east-1:123456789012:bucket/"
                                       f"{'llmops-raw' if 'raw' in Name else 'llmops-curated'}"}}


class _S3Tables:
    def __init__(self):
        self.calls = []

    def list_tables(self, tableBucketARN, **kw):
        self.calls.append("list_tables")
        if tableBucketARN.endswith("llmops-raw"):
            return {"tables": [{"name": "conversation_events", "namespace": ["bronze"], "type": "customer",
                                "modifiedAt": "2026-10-14T11:09:22"}]}
        return {"tables": [{"name": "qlora_train", "namespace": ["gold"]},
                           {"name": "instruction_pairs", "namespace": ["silver"]}]}


def _reports():
    base, land = f"lakehouse/reports/{DATASET}/", f"lakehouse/landing/{DATASET}/"
    return {
        base + "land.json": {"tables": {"bronze.conversation_events": {"rows": 924, "snapshot_id": "1453185831"}}},
        base + "silver.json": {"pairs": 283, "rules": {"redact_pii": {"count": 30, "items": ["c1"]},
                                                       "near_dup": {"count": 6, "items": []}},
                               "dq": {"outcomes": [{"rule": "IsComplete \"pair_id\"", "outcome": "Passed"}]},
                               "plant_check": {"redact_pii": {"passed": True, "detail": "caught 30, planted 30"}},
                               "tables": {"silver.instruction_pairs": {"rows": 283, "snapshot_id": "9"}}},
        base + "gold.json": {"train": 251, "eval": 32, "dq": {"qlora_train": [{"rule": "r", "outcome": "Passed"}]},
                             "tables": {"gold.qlora_train": {"rows": 251, "snapshot_id": "7"}}},
        land + "manifest.json": {"seed": 7, "scale": "demo", "dataset_sha256": "7a05b9ad", "counts": {}},
        "lakehouse/reports/seed7-small-20261014-104335/silver.json": {"pairs": 71},
    }


@pytest.fixture
def wired(console, monkeypatch):  # noqa: F811
    s3, s3t = _S3(_reports()), _S3Tables()
    monkeypatch.setattr(console, "s3", s3)
    monkeypatch.setattr(console, "ssm", _SSM())
    monkeypatch.setattr(console, "data_bucket", lambda: "data-bucket")
    monkeypatch.setattr(console.boto3, "client", lambda svc, **kw: s3t if svc == "s3tables" else
                        (_ for _ in ()).throw(AssertionError(f"unexpected client {svc}")))
    return console, s3, s3t


def test_overview_lists_datasets_newest_first_and_the_tables_in_layer_order(wired):
    console, _, _ = wired
    o = console.data_overview()
    assert o["datasets"] == [DATASET, "seed7-small-20261014-104335"] and o["errors"] == {}
    assert [f"{t['namespace']}.{t['name']}" for t in o["tables"]] == [
        "bronze.conversation_events", "silver.instruction_pairs", "gold.qlora_train"]


def test_run_flattens_the_three_reports_and_the_manifest(wired):
    console, _, _ = wired
    r = console.data_run(DATASET)
    assert r["pairs"] == 283 and (r["train"], r["eval"]) == (251, 32)
    assert r["rules"] == {"redact_pii": 30, "near_dup": 6}
    assert r["plant_check"]["redact_pii"]["passed"] is True
    assert r["tables"]["bronze.conversation_events"]["rows"] == 924
    assert set(r["tables"]) == {"bronze.conversation_events", "silver.instruction_pairs", "gold.qlora_train"}
    assert r["manifest"]["dataset_sha256"] == "7a05b9ad" and r["stages"] == {"land": True, "silver": True, "gold": True, "export": False}
    assert [o["outcome"] for os in r["dq"].values() for o in os] == ["Passed", "Passed"]


def test_a_dataset_with_missing_stages_is_shown_as_missing_not_as_an_error(wired):
    console, _, _ = wired
    r = console.data_run("seed7-small-20261014-104335")
    assert r["stages"] == {"land": False, "silver": True, "gold": False, "export": False} and r["train"] is None


@pytest.mark.parametrize("bad", ["../../customer-data", "x/../../y", "SEED7", "", "a" * 81, "seed 7", "seed7%2f..%2f"])
def test_a_dataset_id_that_is_not_one_is_refused_before_any_read(wired, bad):
    console, s3, _ = wired
    with pytest.raises(ValueError):
        console.data_run(bad)
    assert s3.gets == []


def test_the_route_answers_400_for_a_bad_id(wired):
    console, _, _ = wired
    resp = console.handler({"requestContext": {"http": {"method": "GET", "path": "/api/data/run"}},
                            "queryStringParameters": {"dataset": "../x"}}, None)
    assert resp["statusCode"] == 400


def test_the_tab_reads_reports_and_metadata_only_never_table_data():
    """Static, over the console source: no table-data call and no KMS decrypt is reachable from
    the Data tab's functions, so the tab cannot become a path to Bronze's PII."""
    src = (REPO / "deploy/console/lambda_function.py").read_text()
    block = src[src.index("# ── Data tab (read-only"):src.index("def cost_budgets():")]
    code = "\n".join(line.split("#", 1)[0] for line in block.splitlines())  # comments may say "decrypt"
    assert not re.search(r"get_table_data|GetTableData|kms\.|decrypt|start_query_execution", code, re.I)
    assert "list_tables" in code, "the check must be looking at the Data tab's code"
    pol = json.loads((REPO / "deploy/console/iam-policy.json").read_text())
    st = next(s for s in pol["Statement"] if s["Sid"] == "LakehouseTablesMetadata")
    assert all(a.split(":")[1].startswith(("List", "Get")) and a != "s3tables:GetTableData" for a in st["Action"])
    assert all("llmops-raw" in r or "llmops-curated" in r for r in st["Resource"])


def test_the_frontend_has_the_tab_and_routes_it():
    html = (REPO / "deploy/console/frontend.html").read_text()
    assert 'data-tab="data"' in html and '"data"' in html.split("const KNOWN_TABS")[1].split("]")[0]
    assert 'if (t==="data") loadData();' in html
    assert html.count('data-tab-panel="data"') >= 6


def test_the_export_report_is_shown_latest_first(wired):
    console, s3, _ = wired
    base = f"lakehouse/reports/{DATASET}/"
    s3.objects[base + "export-1.json"] = {"prefix": "s3://b/lakehouse/extracts/x/1/", "files": {"train": {"sha256": "old"}}}
    s3.objects[base + "export-2.json"] = {"prefix": "s3://b/lakehouse/extracts/x/1/", "files": {
        "train": {"uri": "s3://b/lakehouse/extracts/x/1/train.jsonl", "sha256": "a" * 64, "rows": 251,
                  "snapshot_id": "1", "table": "gold.qlora_train", "extra": "dropped"}}}
    r = console.data_run(DATASET)
    assert r["export"]["files"]["train"]["sha256"] == "a" * 64 and "extra" not in r["export"]["files"]["train"]
    assert r["stages"]["export"] is True
    del s3.objects[base + "export-1.json"], s3.objects[base + "export-2.json"]
    assert console.data_run(DATASET)["export"] is None
