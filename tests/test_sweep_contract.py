"""The sweep's output has one shape, and the driver holds the agent to it.

Four live sweep files (2026-10-14, -15, -16 and the 10-15 verification) named the same facts
four ways -- `live_endpoints`/`endpoints`, `orphaned_endpoint_configs`/`orphan_endpoint_configs`,
`timestamp_utc`/`generated_at`, `memory_sweep`/`agentcore` -- and a reader of them counted an
absent field as "0 memory orphans". The renames below are those live variants, so the validator is
tested against the drift it exists to stop, not against an invented one.
"""
from __future__ import annotations

import copy
import importlib.util
import io
import json
import pathlib
import re
import zipfile

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sc = _load("sweep_contract_t", "pipeline/contracts/sweep_contract.py")
driver = _load("harness_driver_sweep_contract", "orchestration/harness_driver/handler.py")
EXAMPLE = json.loads((REPO / "pipeline/contracts/sweep.example.json").read_text())


def _doc(**over):
    d = copy.deepcopy(EXAMPLE)
    d.update(over)
    return d


# --- the instrument ----------------------------------------------------------------------

def _keywords(schema, out):
    for k, v in schema.items():
        out.add(k)
        if k == "properties":
            for sub in v.values():
                _keywords(sub, out)
        elif k in ("items", "additionalProperties") and isinstance(v, dict):
            _keywords(v, out)
    return out


def test_the_schema_uses_only_keywords_the_validator_enforces():
    unknown = _keywords(sc.SCHEMA, set()) - sc.VALIDATED - sc.ANNOTATIONS
    assert not unknown, f"sweep.schema.json uses {sorted(unknown)}, which validate() ignores"


def test_the_example_satisfies_the_contract():
    """The example is documentation AND the fixture every driver test builds on; it must pass."""
    assert sc.check(EXAMPLE, run_id=EXAMPLE["run_id"]) == []


def test_an_empty_or_non_object_file_is_rejected():
    assert sc.check({})
    assert sc.check([])
    assert sc.check("sweep")


# --- the live drift ----------------------------------------------------------------------

#: (path to the key, the name a live sweep actually used instead). Every one of these appeared in
#: a real monitoring/sweeps/*.json between 2026-10-14 and 2026-10-16.
LIVE_RENAMES = [
    ((), "endpoints", "live_endpoints"),
    ((), "orphan_endpoint_configs", "orphaned_endpoint_configs"),
    ((), "orphan_models", "orphaned_models"),
    ((), "generated_at", "timestamp_utc"),
    ((), "open_findings", "findings"),
    ((), "agentcore", "memory_sweep"),
    (("endpoints",), "total", "live_count"),
    (("endpoints",), "total", "total_live"),
    (("endpoints",), "hourly_cost_usd", "live_cost_per_hour_usd"),
    (("agentcore",), "other_project_memories_count", "non_llmops_memories_not_assessed"),
]


@pytest.mark.parametrize("parent, key, renamed", LIVE_RENAMES)
def test_each_live_rename_is_rejected_by_name(parent, key, renamed):
    d = _doc()
    node = d
    for p in parent:
        node = node[p]
    node[renamed] = node.pop(key)
    errs = sc.check(d, run_id=d["run_id"])
    assert any(repr(key) in e and "missing" in e for e in errs), (
        f"renaming {'.'.join(parent + (key,))} to {renamed!r} -- a spelling a live sweep used -- "
        f"was not reported as a missing {key!r}: {errs}")


def test_a_free_form_idle_rule_string_is_rejected():
    """10-16 wrote idle_rule as one prose string; nothing can read a threshold out of that."""
    assert sc.check(_doc(idle_rule="flag any idle >2 hours"))


# --- rules a schema cannot say -----------------------------------------------------------

def test_the_dispatched_run_id_is_the_one_that_counts():
    errs = sc.check(_doc(run_id="sweep-2026-10-16"), run_id="sweep-2026-10-17")
    assert any("dispatched" in e for e in errs)


def test_endpoint_total_must_equal_the_endpoints_it_lists():
    d = _doc()
    d["endpoints"]["total"] = 3
    assert any("endpoints.total" in e for e in sc.check(d, run_id=d["run_id"]))


def test_the_orphan_count_must_equal_the_orphans_it_lists():
    d = _doc()
    d["agentcore"]["memory_orphans_count"] = 1
    assert any("memory_orphans_count" in e for e in sc.check(d, run_id=d["run_id"]))


def test_an_unmeasured_count_is_null_and_must_be_explained():
    """Unknown is not zero -- and the schema allows null for exactly that -- but a null with no
    permission gap is a silence, which is the same failure in a different spelling."""
    d = _doc()
    d["agentcore"]["memories_total"] = None
    assert any("permission_gaps" in e for e in sc.check(d, run_id=d["run_id"]))
    d["permission_gaps"] = ["bedrock-agentcore:ListMemories AccessDenied"]
    assert sc.check(d, run_id=d["run_id"]) == []


@pytest.mark.parametrize("path, bad", [
    (("deletions_performed",), 1),
    (("agentcore", "contents_read"), True),
    (("stage",), "deploy"),
    (("task",), "health"),
    (("schema_version",), "sweep/0"),
    (("generated_at",), "2026-10-17 08:03"),
    (("agentcore", "memories_total"), -1),
])
def test_values_the_sweep_may_never_report_are_rejected(path, bad):
    d = _doc()
    node = d
    for p in path[:-1]:
        node = node[p]
    node[path[-1]] = bad
    assert sc.check(d, run_id=d["run_id"]), f"{'.'.join(path)}={bad!r} was accepted"


def test_extra_keys_are_allowed():
    """The agent may add detail; only the required names are fixed."""
    assert sc.check(_doc(evidence={"commands": ["aws sagemaker list-endpoints"]}),
                    run_id=EXAMPLE["run_id"]) == []


# --- the prompt must state what the driver enforces --------------------------------------

def _required_keys(schema, out):
    for k in schema.get("required", []):
        out.add(k)
    for sub in schema.get("properties", {}).values():
        _required_keys(sub, out)
    if isinstance(schema.get("items"), dict):
        _required_keys(schema["items"], out)
    return out


def test_the_sweep_clause_names_every_required_key():
    """Derived from the schema, so a key added there fails here until the prompt says it too.
    A contract the agent is not told is one it can only learn by being rejected."""
    text = json.loads((REPO / "agents/monitor/harness.json").read_text())["systemPrompt"][0]["text"]
    clause = [ln for ln in text.splitlines() if ln.startswith('- "sweep"')]
    assert len(clause) == 1, "the sweep clause moved; re-anchor this guard"
    missing = sorted(k for k in _required_keys(sc.SCHEMA, set()) if f'"{k}"' not in clause[0])
    assert not missing, f"the sweep clause never names {missing}"
    assert sc.SCHEMA_VERSION in clause[0]
    assert "agentcore.memory_orphans" in clause[0]


# --- the driver's check ------------------------------------------------------------------

class _S3:
    def __init__(self, objects=None, fail=None):
        self.objects, self.fail = dict(objects or {}), fail

    def get_object(self, Bucket, Key):
        if self.fail:
            raise self.fail
        body = self.objects[f"s3://{Bucket}/{Key}"]
        return {"Body": io.BytesIO(body.encode())}


URI = "s3://llmops-data-test/monitoring/sweeps/sweep-2026-10-17.json"
EVENT = {"run_id": "sweep-2026-10-17", "params": {"sweep_uri": URI}}


def test_the_driver_accepts_a_conforming_file():
    assert driver.check_sweep_output(_S3({URI: json.dumps(EXAMPLE)}), EVENT, [URI]) == []


def test_the_driver_rejects_a_drifted_file_with_the_reason():
    d = _doc()
    d["memory_sweep"] = d.pop("agentcore")
    errs = driver.check_sweep_output(_S3({URI: json.dumps(d)}), EVENT, [URI])
    assert any("'agentcore'" in e for e in errs)


@pytest.mark.parametrize("s3, why", [
    (_S3({URI: "not json"}), "not JSON"),
    (_S3(fail=RuntimeError("AccessDenied")), "could not be read back"),
])
def test_an_unreadable_file_is_a_rejection_never_a_pass(s3, why):
    errs = driver.check_sweep_output(s3, EVENT, [URI])
    assert errs and why in errs[0]


def test_the_file_is_found_from_outputs_when_params_name_none():
    ev = {"run_id": "sweep-2026-10-17", "params": {}}
    assert driver.check_sweep_output(_S3({URI: json.dumps(EXAMPLE)}), ev, [URI]) == []
    assert driver.check_sweep_output(_S3(), ev, ["s3://b/x/other.json"])[0].startswith("no sweep")


def test_only_the_sweep_is_held_to_the_sweep_contract():
    """health and report write other files; checking them against this schema would reject
    every run-scoped monitor stage."""
    src = (REPO / "orchestration/harness_driver/handler.py").read_text()
    body = src[src.index("def handle_stage_complete("):]
    body = body[:body.index("\ndef ")]
    assert re.search(r'if stage == "monitor" and task == "sweep":\s*\n\s*invalid = check_sweep_output',
                     body), "the sweep check is no longer scoped to (monitor, sweep)"


def test_the_rejection_tells_the_agent_what_to_fix_and_caps_the_list():
    reason = driver._sweep_contract_rejection([f"err{i}" for i in range(30)])
    assert "sweep/1" in reason and "err0" in reason and "and 10 more" in reason
    assert "err25" not in reason


# --- the Lambda bundle -------------------------------------------------------------------

def test_the_driver_bundle_carries_the_contract_and_its_schema():
    """sweep_contract reads the schema at import time, so a bundle without it fails on the first
    import -- every driver invocation, not just sweeps."""
    l7 = _load("lambdas_sweep_contract", "deploy/07_lambdas.py")
    names = zipfile.ZipFile(io.BytesIO(
        l7.bundle(REPO / "orchestration/harness_driver/handler.py"))).namelist()
    assert {"sweep_contract.py", "sweep.schema.json"} <= set(names), names
