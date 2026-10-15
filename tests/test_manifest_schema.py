"""manifest.schema.json must describe the manifests the code actually writes.

It did not. Surveyed live on 2026-10-13, 40 of 40 run manifests failed it: `run_id` was
specified as `llmops-<ts>-<6 hex>` while `new_run_id` mints `run-<ts>-<8 hex>`;
`trigger_source` was a five-value enum while 15 distinct values are in use, two of them in
the enum; `models.teacher` was an object while every manifest holds a model-id string; and
`stages[*].status` was a six-value enum over a map the agents write into in a vocabulary of
their own. Nothing validated against the schema, so nothing noticed -- a contract that only
documents is a claim, and this one had drifted from every artifact it described.

So the tests here do not check the schema against a copy of what it should say. They build
manifests with the REAL writers -- `start_pipeline.seed_manifest`, then the driver's own
stage-complete and escalation paths over an in-memory S3 -- and validate the result against
the real schema file. Standard library only: a small validator for the draft-07 subset the
schema uses, plus a guard that the schema uses nothing outside that subset, so a keyword the
validator does not implement cannot be silently ignored.
"""
from __future__ import annotations

import importlib.util
import io
import json
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
SCHEMA = json.loads((REPO / "pipeline/contracts/manifest.schema.json").read_text())


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


driver = _load("harness_driver_for_schema", "orchestration/harness_driver/handler.py")
start_pipeline = _load("start_pipeline_for_schema", "orchestration/start_pipeline/handler.py")

# ── a validator for exactly the subset the schema uses ──────────────────────
VALIDATED = {"type", "required", "properties", "additionalProperties", "items", "pattern",
             "enum", "minimum"}
#: Annotations: carry no constraint this validator would be skipping.
ANNOTATIONS = {"$schema", "$id", "title", "description", "format"}
_TYPES = {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None)}


def _is(value, typ: str) -> bool:
    if typ == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if typ == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, _TYPES[typ])


def validate(value, schema: dict, path: str = "$") -> list[str]:
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


def _keywords(schema: dict, out: set) -> set:
    for k, v in schema.items():
        out.add(k)
        if k == "properties":
            for sub in v.values():
                _keywords(sub, out)
        elif k in ("items", "additionalProperties") and isinstance(v, dict):
            _keywords(v, out)
    return out


def test_the_schema_uses_only_keywords_this_validator_enforces():
    unknown = _keywords(SCHEMA, set()) - VALIDATED - ANNOTATIONS
    assert not unknown, (
        f"manifest.schema.json uses {sorted(unknown)}, which validate() does not implement -- "
        "every test below would pass without checking it. Implement the keyword or drop it.")


def test_the_validator_rejects_what_it_should():
    """The validator is the instrument; check it bites before trusting a green result."""
    assert validate({}, SCHEMA), "an empty manifest validated"
    bad = {"run_id": "llmops-20261013T000000Z-abcdef", "created_at": "x",
           "trigger_source": "Bad Source", "iteration": -1, "stages": {"x": 3},
           "models": {"teacher": {"model_id": "m"}, "student": "s", "hf_repo": "r"}}
    errs = "\n".join(validate(bad, SCHEMA))
    for needle in ("run_id", "trigger_source", "iteration", "stages.x", "models.teacher",
                   "'hf_repo'"):
        assert needle in errs, f"{needle} was not rejected:\n{errs}"


# ── run_id ───────────────────────────────────────────────────────────────────
def test_new_run_id_mints_what_the_schema_describes():
    pattern = SCHEMA["properties"]["run_id"]["pattern"]
    for _ in range(50):
        rid = start_pipeline.new_run_id()
        assert re.search(pattern, rid), f"new_run_id() minted {rid!r}, schema says {pattern}"


# ── trigger_source ───────────────────────────────────────────────────────────
#: The forms a trigger_source literal takes in this repo: a JSON/dict key, a jq object
#: key (run-pipeline.yml), a module constant, and start_pipeline's own default.
_TRIGGER_LITERAL = re.compile(
    r"""(?:["']?trigger_source["']?\s*[:=]\s*|TRIGGER_SOURCE\s*=\s*|"""
    r"""get\(\s*["']trigger_source["']\s*,\s*)["']([^"'{}$]+)["']""")


def _trigger_literals() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    roots = [REPO / d for d in ("orchestration", "deploy", "tools", "pipeline")]
    files = [p for r in roots for p in r.rglob("*.py")]
    files += sorted((REPO / ".github/workflows").glob("*.yml"))
    for p in files:
        for m in _TRIGGER_LITERAL.finditer(p.read_text(encoding="utf-8")):
            found.setdefault(m.group(1), set()).add(str(p.relative_to(REPO)))
    return found


def test_every_trigger_source_the_repo_passes_is_valid():
    found = _trigger_literals()
    # A floor, so a scanner regex that stops matching cannot pass by finding nothing.
    expected = {"conductor", "webhook", "console", "scheduler", "resurrector-schedule",
                "github-actions", "protocol-probe", "unknown"}
    assert expected <= set(found), f"scanner missed {sorted(expected - set(found))}"
    schema = SCHEMA["properties"]["trigger_source"]
    bad = {v: sorted(w) for v, w in found.items() if validate(v, schema)}
    assert not bad, f"trigger_source literals the schema rejects: {bad}"


# ── the manifest the writers produce ─────────────────────────────────────────
class _S3:
    """In-memory S3 holding exactly what was put, and failing reads of what was not."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise RuntimeError("NoSuchKey")
        return {"Body": io.BytesIO(self.objects[Key])}

    def put_object(self, Bucket, Key, Body, **kw):
        self.objects[Key] = Body if isinstance(Body, bytes) else Body.encode()

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise RuntimeError("404")
        return {}


class _DDB:
    def Table(self, name):
        class _T:
            def put_item(self, **kw):
                return {}

            def update_item(self, **kw):
                return {}
        return _T()


class _Events:
    def put_events(self, **kw):
        return {"FailedEntryCount": 0}


@pytest.fixture
def env(monkeypatch):
    for k, v in {"DATA_BUCKET": "b", "RUNS_TABLE": "r", "EVENT_BUS": "e",
                 "EVENTS_TABLE": "ev"}.items():
        monkeypatch.setenv(k, v)


#: Carries the supply-chain block the conductor writes for an open-weight model: live, one
#: manifest (2026-08-12) has hf_repo/revision/license inside `models`, which the schema forbids.
#: Seeding this plan proves the current writer keeps them out.
SIGNED_PLAN = {"models": {"teacher": "global.anthropic.claude-fable-5",
                          "student": "Qwen/Qwen3-1.7B", "judge": "global.anthropic.claude-opus-5",
                          "hf_repo": "Qwen/Qwen3-1.7B", "revision": "0" * 40,
                          "license": "apache-2.0"},
               "budget_usd": 450}


@pytest.mark.parametrize("params,plan", [({}, {}), ({"harness_model": "h"}, SIGNED_PLAN)],
                         ids=["defaults", "signed-plan"])
def test_a_seeded_manifest_validates(params, plan):
    m = start_pipeline.seed_manifest(start_pipeline.new_run_id(), "console", params, plan)
    errs = validate(m, SCHEMA)
    assert not errs, "seed_manifest wrote a manifest its own schema rejects:\n" + "\n".join(errs)
    assert set(m["models"]) <= set(SCHEMA["properties"]["models"]["properties"]), m["models"]


def test_every_model_role_start_pipeline_resolves_is_in_the_schema():
    roles = set(start_pipeline.ROLE_ALIASES) | set(start_pipeline.DEFAULT_MODELS)
    described = set(SCHEMA["properties"]["models"]["properties"])
    assert roles == described, (
        f"_resolve_models can write roles {sorted(roles - described)} the schema forbids "
        f"(models.additionalProperties is false), or the schema names roles "
        f"{sorted(described - roles)} nothing writes")


def test_the_driver_records_validate_after_a_stage_and_an_escalation(env):
    """The driver-written blocks -- stage_history, attestations, escalations -- validated on
    the manifest the driver actually puts back, not on dicts built here."""
    s3 = _S3()
    run_id = start_pipeline.new_run_id()
    key = f"runs/{run_id}/manifest.json"
    s3.put_object("b", key, json.dumps(
        start_pipeline.seed_manifest(run_id, "conductor", {}, SIGNED_PLAN)).encode())
    s3.put_object("b", f"runs/{run_id}/eval/report.json", b"{}")
    extract = b'{"messages":[]}\n{"messages":[]}\n'
    s3.put_object("b", "lakehouse/extracts/seed7-demo-x/42/train.jsonl", extract)
    c = {"s3": s3, "ddb": _DDB(), "events": _Events(), "sfn": None}
    event = {"run_id": run_id, "stage": "eval", "task": "score", "iteration": 0,
             "manifest_uri": f"s3://b/{key}"}
    # judge_prompt_sha256 makes the driver attest; the canonical prompt is absent from this
    # S3, so the attestation takes its could-not-check branch, `error` included.
    res = driver.handle_stage_complete(c, event, {
        "stage": "eval", "outputs": [f"s3://b/runs/{run_id}/eval/report.json"],
        "metrics": {"judge_win_rate": 0.6, "judge_prompt_sha256": "0" * 64,
                    "extract_uri": "s3://b/lakehouse/extracts/seed7-demo-x/42/train.jsonl",
                    "extract_sha256": __import__("hashlib").sha256(extract).hexdigest(),
                    "extract_snapshot_id": 42},
        "evidence": "scored 40 pairs"})
    assert res.get("ok"), res
    assert driver._record_manifest_escalation(c, event, {"reason": "gate missed twice"})

    saved = json.loads(s3.objects[key])
    for block in ("stage_history", "attestations", "escalations"):
        assert saved.get(block), f"the driver wrote no {block}: {sorted(saved)}"
    assert saved["attestations"][0].get("error"), saved["attestations"]
    gold = next(a for a in saved["attestations"] if a["kind"] == "gold_extract")
    assert gold["verified"] is True and gold["rows"] == 2 and gold["snapshot_id"] == "42", gold
    errs = validate(saved, SCHEMA)
    assert not errs, "the driver wrote records the schema rejects:\n" + "\n".join(errs)
