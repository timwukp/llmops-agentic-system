"""The monitor sweep's output contract: sweep.schema.json plus the rules a schema cannot say.

The sweep wrote whatever shape it liked, and every run liked a different one -- see the schema's
own $comment for the four live variants. Nothing read those files programmatically yet, so nothing
broke, but a human reading one already did: a field that was absent was counted as 0 memory orphans.
The driver now refuses a sweep stage_complete whose file fails `check`, and tells the agent why.

Dependency-free on purpose: this module is vendored flat into the driver's Lambda bundle (it is
imported in handler.py's `except ImportError` block, which is what makes 07_lambdas.py ship it), and
the bundle has no jsonschema. The validator implements exactly the keywords the schema uses;
tests/test_sweep_contract.py fails if the schema starts using one it does not.
"""
from __future__ import annotations

import json
import pathlib
import re

SCHEMA_VERSION = "sweep/1"
SCHEMA = json.loads(pathlib.Path(__file__).with_name("sweep.schema.json").read_text())

#: Keywords validate() enforces; anything else in the schema would be silently ignored.
VALIDATED = {"type", "enum", "pattern", "minimum", "required", "properties", "items",
             "additionalProperties"}
ANNOTATIONS = {"$comment", "description"}

_TYPES = {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None)}

#: The agentcore counts. Null means "could not measure", which is allowed -- and must be explained.
NULLABLE_COUNTS = (("endpoints", "total"), ("endpoints", "hourly_cost_usd"),
                   ("agentcore", "memories_total"), ("agentcore", "harnesses_total"),
                   ("agentcore", "llmops_harnesses_inspected"),
                   ("agentcore", "memory_orphans_count"),
                   ("agentcore", "other_project_memories_count"))


def _is(value, typ: str) -> bool:
    if typ == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if typ == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, _TYPES[typ])


def validate(value, schema: dict, path: str = "$") -> list:
    """Schema errors as readable strings; [] when the value conforms."""
    errs = []
    types = schema.get("type")
    if types is not None:
        types = types if isinstance(types, list) else [types]
        if not any(_is(value, t) for t in types):
            return [f"{path}: {type(value).__name__} is not {types}"]
    if "enum" in schema and value not in schema["enum"]:
        errs.append(f"{path}: {value!r} not in {schema['enum']}")
    if "pattern" in schema and isinstance(value, str) and not re.search(schema["pattern"], value):
        errs.append(f"{path}: {value!r} does not match {schema['pattern']}")
    if "minimum" in schema and _is(value, "number") and value < schema["minimum"]:
        errs.append(f"{path}: {value} < {schema['minimum']}")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        errs += [f"{path}: missing {k!r}" for k in schema.get("required", []) if k not in value]
        extra = schema.get("additionalProperties", True)
        for k, v in value.items():
            if k in props:
                errs += validate(v, props[k], f"{path}.{k}")
            elif extra is False:
                errs.append(f"{path}: unexpected key {k!r}")
            elif isinstance(extra, dict):
                errs += validate(v, extra, f"{path}.{k}")
    if isinstance(value, list) and "items" in schema:
        for i, v in enumerate(value):
            errs += validate(v, schema["items"], f"{path}[{i}]")
    return errs


def check(doc, run_id: str | None = None) -> list:
    """Schema errors, then the cross-field rules. [] means the sweep may be accepted."""
    errs = validate(doc, SCHEMA)
    if errs or not isinstance(doc, dict):
        return errs  # the rules below assume the shape; report the shape first
    if run_id and doc["run_id"] != run_id:
        errs.append(f"$.run_id: {doc['run_id']!r} but this sweep was dispatched as {run_id!r}")
    ep = doc["endpoints"]
    listed = len(ep["ours"]) + len(ep["other_projects"]) + len(ep["untagged"])
    if ep["total"] is not None and ep["total"] != listed:
        errs.append(f"$.endpoints.total: {ep['total']} but ours+other_projects+untagged list "
                    f"{listed} endpoints")
    ac = doc["agentcore"]
    if ac["memory_orphans_count"] is not None and ac["memory_orphans_count"] != len(ac["memory_orphans"]):
        errs.append(f"$.agentcore.memory_orphans_count: {ac['memory_orphans_count']} but "
                    f"memory_orphans lists {len(ac['memory_orphans'])}")
    unmeasured = [f"{a}.{b}" for a, b in NULLABLE_COUNTS if doc[a][b] is None]
    if unmeasured and not doc["permission_gaps"]:
        errs.append(f"{unmeasured} are null (not measured) but permission_gaps is empty: say what "
                    "could not be read. Unknown is not zero, and it is not unexplained either.")
    return errs


def sweep_uri_from(params: dict, outputs: list) -> str | None:
    """Where the sweep was told to write, else the output that is its sweep file."""
    uri = (params or {}).get("sweep_uri")
    if uri:
        return uri
    for o in outputs or []:
        if isinstance(o, str) and o.startswith("s3://") and (
                o.endswith("sweep.json") or "/sweeps/" in o):
            return o
    return None
