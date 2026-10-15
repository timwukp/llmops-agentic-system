#!/usr/bin/env python3
"""Gold → a training extract, reproducibly (Phase 1 PR-08; research report §4.4).

Training never reads Iceberg directly. It reads a FILE whose bytes are pinned to one Gold snapshot
and named by their sha256. Three reasons, all from the report:
- a snapshot may be expired after its retention;
- a network-isolated training job may have no route to `s3tables`;
- Macie cannot scan a table bucket, but it can scan this prefix.

The extract is also what a right-to-be-forgotten purge has to reach.

  1. `UNLOAD (SELECT … FROM gold.<table> FOR VERSION AS OF <snapshot> ORDER BY pair_id)` to a fresh
     prefix (an UNLOAD into a non-empty prefix fails, as spike S7 measured).
  2. The UNLOAD's JSON parts → chat `messages` JSONL, one canonical line per pair, sorted by pair_id.
     The sha256 is a property of the snapshot's content, not of how Athena split its files (S7:
     two UNLOADs of one snapshot hash identically only once lines are sorted and decompressed).
  3. A row in gold.dataset_manifest, and `extract.json` beside the files.

Extracts live under `lakehouse/extracts/<dataset>/<snapshot>/`, a top-level prefix of their own, because
a Macie job can be scoped only by key prefix (`OBJECT_KEY STARTS_WITH`).
"""
from __future__ import annotations

import gzip
import hashlib
import json
import re

EXTRACTS_PREFIX = "lakehouse/extracts"
SPLITS = {"train": "qlora_train", "eval": "qlora_eval"}
FILE_FOR_SPLIT = {"train": "train.jsonl", "eval": "val.jsonl"}  # the trainer's names
_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")


def extract_prefix(dataset: str, snapshot_id: int) -> str:
    if not _ID.match(dataset or ""):
        raise ValueError("dataset must be a lowercase id")
    return f"{EXTRACTS_PREFIX}/{dataset}/{int(snapshot_id)}"


def unload_sql(table: str, snapshot_id: int, dest: str) -> str:
    """The snapshot must be an int and the table one of the Gold splits: both are formatted into
    SQL, so anything else is refused rather than quoted."""
    if table not in SPLITS.values():
        raise ValueError(f"not a Gold split table: {table!r}")
    if not isinstance(snapshot_id, int) or isinstance(snapshot_id, bool) or snapshot_id <= 0:
        raise TypeError("snapshot_id must be a positive int")
    if not dest.startswith("s3://") or "'" in dest or not dest.endswith("/"):
        raise ValueError("dest must be an s3:// prefix ending in / and without quotes")
    return (f'UNLOAD (SELECT pair_id, instruction, input, output FROM "gold"."{table}" '
            f"FOR VERSION AS OF {snapshot_id} ORDER BY pair_id) TO '{dest}' WITH (format = 'JSON')")


def rows_from_unload(objects: list[bytes]) -> list[dict]:
    rows = []
    for body in objects:
        raw = gzip.decompress(body) if body[:2] == b"\x1f\x8b" else body
        rows += [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
    return rows


def to_messages(row: dict) -> dict:
    """Same mapping as transform.to_messages (instruction/input/output → user/assistant), kept
    here so the extract has no dependency the export job would have to ship twice."""
    user = row["instruction"] + (f"\n\n{row['input']}" if row.get("input") else "")
    return {"messages": [{"role": "user", "content": user}, {"role": "assistant", "content": row["output"]}]}


def to_jsonl(rows: list[dict]) -> bytes:
    """Canonical messages JSONL: sorted by pair_id, keys sorted, no whitespace, UTF-8, one
    trailing newline per line. Same rows in any order and any file split → same bytes."""
    lines = [json.dumps(to_messages(r), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
             for r in sorted(rows, key=lambda r: r["pair_id"])]
    return "".join(line + "\n" for line in lines).encode("utf-8")


def sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _sql_str(v) -> str:
    return "NULL" if v is None else "'" + str(v).replace("'", "''") + "'"


def manifest_insert_sql(row: dict) -> str:
    """INSERT INTO gold.dataset_manifest. Strings are quoted and escaped; numbers are validated
    as numbers, never interpolated as text."""
    if not re.fullmatch(r"[0-9a-f]{64}", row["sha256"]):
        raise ValueError("sha256 must be 64 lowercase hex characters")
    nums = {k: int(row[k]) for k in ("snapshot_id", "row_count")}
    dq = "NULL" if row.get("dq_score") is None else repr(float(row["dq_score"]))
    return ('INSERT INTO "gold"."dataset_manifest" (dataset_version, table_name, snapshot_id, export_uri, '
            "sha256, row_count, dq_score, pii_report_uri, run_id, created_ts) VALUES ("
            f"{_sql_str(row['dataset_version'])}, {_sql_str(row['table_name'])}, {nums['snapshot_id']}, "
            f"{_sql_str(row['export_uri'])}, {_sql_str(row['sha256'])}, {nums['row_count']}, {dq}, "
            f"{_sql_str(row.get('pii_report_uri'))}, {_sql_str(row.get('run_id'))}, "
            f"from_iso8601_timestamp({_sql_str(row['created_ts'])}))")
