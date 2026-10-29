"""An oversized agent-written file is refused, not read whole into a 512 MB Lambda.

A harness agent can write a file of any size under its run prefix. The driver and the
publish-provenance Lambda read several of those files; read whole, a multi-GB file OOMs the Lambda,
and an OOM is not catchable, so the task token never settles and the stage hangs to its timeout.
The caps are shrunk via monkeypatch here so the tests allocate nothing large.
"""
from __future__ import annotations

import importlib.util
import io
import json
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


driver = _load("driver_caps", "orchestration/harness_driver/handler.py")
conductor = _load("conductor_caps", "orchestration/conductor_tools.py")
prov = _load("prov_caps", "orchestration/publish_provenance/handler.py")


class _Body:
    def __init__(self, data): self._d = data
    def read(self): return self._d
    def iter_chunks(self, n=1 << 20):
        for i in range(0, len(self._d), n):
            yield self._d[i:i + n]


class _S3:
    """Honours Range like real S3 (slices)."""
    def __init__(self, data): self._d = data
    def get_object(self, Bucket, Key, Range=None):
        d = self._d
        if Range:
            hi = int(Range.split("-")[1])
            d = d[:hi + 1]
        return {"Body": _Body(d)}


class _FullS3:
    """Accepts Range but ignores it, returning the whole object — so the refusal under test is
    the explicit length check, not a lucky JSON-parse failure on a truncated slice."""
    def __init__(self, data): self._d = data
    def get_object(self, Bucket, Key, Range=None):
        return {"Body": _Body(self._d)}


# Valid JSON, but larger than the (shrunk) caps the tests set — so without the cap it parses.
BIG = json.dumps({"ok": True, "pad": "x" * 400}).encode()
SMALL = json.dumps({"ok": True}).encode()


# ── driver: manifest ────────────────────────────────────────────────────────────
def test_load_manifest_refuses_an_oversized_file(monkeypatch):
    monkeypatch.setattr(driver, "MAX_AGENT_JSON_BYTES", 100)
    assert driver._load_manifest(_FullS3(BIG), "s3://b/runs/r/manifest.json") == {}


def test_load_manifest_reads_a_normal_file(monkeypatch):
    monkeypatch.setattr(driver, "MAX_AGENT_JSON_BYTES", 100)
    assert driver._load_manifest(_S3(SMALL), "s3://b/runs/r/manifest.json") == {"ok": True}


# ── driver: sweep ───────────────────────────────────────────────────────────────
def test_check_sweep_output_rejects_an_oversized_file(monkeypatch):
    monkeypatch.setattr(driver, "MAX_AGENT_JSON_BYTES", 100)
    event = {"run_id": "r", "params": {"sweep_uri": "s3://b/monitoring/sweeps/r.json"}}
    errs = driver.check_sweep_output(_FullS3(BIG), event, ["s3://b/monitoring/sweeps/r.json"])
    assert errs and "over" in errs[0] and "bytes" in errs[0]


# ── driver: gold extract (streamed) ───────────────────────────────────────────────
def test_verify_gold_extract_refuses_an_oversized_extract(monkeypatch):
    monkeypatch.setattr(driver, "MAX_EXTRACT_BYTES", 100)
    uri = f"s3://b/{driver.GOLD_EXTRACTS_PREFIX}gold.jsonl"
    att = driver._verify_gold_extract(_FullS3(BIG), "b", {"extract_uri": uri, "extract_sha256": "z"})
    assert att["verified"] is False and "exceeds" in att.get("error", "")


def test_verify_gold_extract_hashes_a_normal_extract(monkeypatch):
    import hashlib
    monkeypatch.setattr(driver, "MAX_EXTRACT_BYTES", 10000)
    data = b'{"a":1}\n{"b":2}\n'
    uri = f"s3://b/{driver.GOLD_EXTRACTS_PREFIX}gold.jsonl"
    att = driver._verify_gold_extract(_S3(data), "b",
                                      {"extract_uri": uri, "extract_sha256": hashlib.sha256(data).hexdigest()})
    assert att["verified"] is True and att["rows"] == 2


# ── conductor: plan ───────────────────────────────────────────────────────────────
def test_read_s3_uri_raises_on_an_oversized_plan(monkeypatch):
    monkeypatch.setattr(conductor, "MAX_PLAN_BYTES", 100)
    with pytest.raises(ValueError, match="exceeds"):
        conductor._read_s3_uri(_FullS3(BIG), "s3://b/tasks/t/plan.json")


def test_read_s3_uri_reads_a_normal_plan(monkeypatch):
    monkeypatch.setattr(conductor, "MAX_PLAN_BYTES", 100)
    assert conductor._read_s3_uri(_S3(SMALL), "s3://b/tasks/t/plan.json") == SMALL


# ── provenance: card inputs ───────────────────────────────────────────────────────
def test_reader_json_returns_none_for_an_oversized_input(monkeypatch):
    monkeypatch.setattr(prov.Reader, "MAX_BYTES", 100)
    assert prov.Reader(_FullS3(BIG), "b").json("runs/r/evaluation/gate.json") is None


def test_reader_json_reads_a_normal_input(monkeypatch):
    monkeypatch.setattr(prov.Reader, "MAX_BYTES", 100)
    assert prov.Reader(_S3(SMALL), "b").json("runs/r/evaluation/gate.json") == {"ok": True}


# ── a double that predates Range still works (the TypeError fallback) ──────────────
class _OldS3:
    def __init__(self, data): self._d = data
    def get_object(self, Bucket, Key):  # no Range kwarg
        return {"Body": _Body(self._d)}


def test_the_fallback_keeps_the_cap_when_a_double_has_no_range(monkeypatch):
    monkeypatch.setattr(driver, "MAX_AGENT_JSON_BYTES", 100)
    # full body returned (Range ignored), but the length check still refuses it
    assert driver._load_manifest(_OldS3(BIG), "s3://b/runs/r/manifest.json") == {}
    assert driver._load_manifest(_OldS3(SMALL), "s3://b/runs/r/manifest.json") == {"ok": True}
