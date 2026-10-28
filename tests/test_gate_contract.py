"""The eval gate's verdict has one shape, and the driver holds the agent to it.

The eval prompt never named evaluation/gate.json or its shape. Four live runs of it wrote four:
the metrics under gate_results, decisions, results or gates_applied; verdicts as decisive_pass,
pass_decisive or fail_decisive; counts as "87/120" under count or counts, or n alone. The
provenance card and the console's Outcome tab both read the file, through a reader that guessed
among the four (provenance.GATE_SHAPES), so a fifth spelling would have dropped the scores from both
with no error. The legacy keys below are those live shapes, so the contract is tested against the
drift it exists to stop. The numbers in VALID are the 2026-10-26 identifier-grounding run's.
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


gc = _load("gate_contract_t", "pipeline/contracts/gate_contract.py")
sc = _load("sweep_contract_for_gate_t", "pipeline/contracts/sweep_contract.py")
prov = _load("provenance_for_gate_t", "pipeline/contracts/provenance.py")
driver = _load("harness_driver_gate_contract", "orchestration/harness_driver/handler.py")

RUN = "run-20261026T133101Z-c266a595"
SIGNED = {"format_validity": 0.95, "answer_match": 0.6, "identifier_grounding": 0.95}


def _wilson_gate(k, n, thr):
    lo, hi = gc.wilson(k, n)
    verdict = "pass" if lo >= thr else "fail" if hi < thr else "borderline"
    return {"threshold": thr, "value": round(k / n, 6), "k": k, "n": n,
            "ci_low": round(lo, 6), "ci_high": round(hi, 6), "rule": "wilson95", "verdict": verdict}


VALID = {
    "schema_version": "gate/1", "run_id": RUN, "iteration": 0,
    "decided_at": "2026-10-26T14:40:02Z",
    "report_uri": f"s3://llmops-data-test/runs/{RUN}/evaluation/report.json",
    "gate_passed": False,
    "gates": {"format_validity": _wilson_gate(120, 120, 0.95),
              "answer_match": _wilson_gate(87, 120, 0.6),
              "identifier_grounding": _wilson_gate(0, 120, 0.95)},
    "checks": {"safety": {"passed": True, "detail": "20 spot-checks"}},
}


def _doc(**over):
    d = copy.deepcopy(VALID)
    d.update(over)
    return d


def _check(doc, **kw):
    kw.setdefault("signed_gates", SIGNED)
    return gc.check(doc, run_id=RUN, iteration=0, **kw)


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
    unknown = _keywords(gc.SCHEMA, set()) - sc.VALIDATED - sc.ANNOTATIONS
    assert not unknown, f"the validator would silently ignore {unknown}"


def test_the_wilson_interval_is_the_one_the_live_files_report():
    """The four live files agree with this to six places, so the recomputation is the same
    instrument the agent used, not a rival one."""
    assert [round(x, 6) for x in gc.wilson(87, 120)] == [0.63907, 0.796971]
    assert [round(x, 6) for x in gc.wilson(120, 120)] == [0.968981, 1.0]
    assert [round(x, 6) for x in gc.wilson(0, 120)] == [0.0, 0.031019]


# --- the shape ---------------------------------------------------------------------------

def test_a_conforming_verdict_is_accepted():
    assert _check(VALID) == []


@pytest.mark.parametrize("legacy", prov.GATE_SHAPES)
def test_each_live_legacy_shape_is_rejected_by_name(legacy):
    d = _doc()
    d[legacy] = d.pop("gates")
    assert any("'gates'" in e for e in _check(d)), legacy


@pytest.mark.parametrize("verdict", ["decisive_pass", "pass_decisive", "fail_decisive"])
def test_the_live_verdict_spellings_are_rejected(verdict):
    d = _doc()
    d["gates"]["answer_match"]["verdict"] = verdict
    assert any("verdict" in e for e in _check(d))


def test_a_count_string_is_not_k_and_n():
    d = _doc()
    g = d["gates"]["answer_match"]
    g["count"] = "87/120"
    del g["k"], g["n"]
    errs = _check(d)
    assert any("'k'" in e for e in errs) and any("'n'" in e for e in errs)


def test_the_dispatched_run_and_iteration_are_the_ones_that_count():
    assert any("$.run_id" in e for e in _check(_doc(run_id="run-other")))
    assert any("$.iteration" in e for e in _check(_doc(iteration=1)))


# --- the bars are the signed ones ---------------------------------------------------------

def test_a_signed_gate_left_undecided_is_rejected():
    d = _doc()
    del d["gates"]["identifier_grounding"]
    assert any("does not decide it" in e for e in _check(d))


def test_a_gate_the_plan_did_not_sign_is_rejected():
    d = _doc()
    d["gates"]["judge_score"] = _wilson_gate(60, 100, 0.5)
    assert any("no signed gate" in e for e in _check(d))


def test_a_bar_other_than_the_signed_one_is_rejected():
    """A gate.json cannot pass by lowering its own bar."""
    d = _doc()
    d["gates"]["identifier_grounding"] = _wilson_gate(0, 120, 0.0)
    assert any("signed bar is 0.95" in e for e in _check(d))


def test_with_no_signed_gates_there_is_nothing_to_decide():
    assert any("escalate_human" in e for e in _check(VALID, signed_gates=None))


# --- each verdict is the one its rule gives ----------------------------------------------

def test_a_pass_the_interval_does_not_support_is_rejected():
    d = _doc()
    d["gates"]["identifier_grounding"]["verdict"] = "pass"
    assert any("gives 'fail'" in e for e in _check(d))


def test_an_interval_that_is_not_the_wilson_interval_of_k_and_n_is_rejected():
    d = _doc()
    d["gates"]["answer_match"]["ci_low"] = 0.7
    assert any("is not the Wilson 95% interval" in e for e in _check(d))


def test_a_value_that_is_not_k_over_n_is_rejected():
    d = _doc()
    d["gates"]["answer_match"]["value"] = 0.8
    assert any("is not k/n" in e for e in _check(d))


def test_an_unresolved_borderline_is_never_handed_in():
    signed = {"answer_match": 0.7}
    d = _doc(gates={"answer_match": _wilson_gate(87, 120, 0.7)})
    assert d["gates"]["answer_match"]["verdict"] == "borderline"
    assert any("borderline with no directive" in e for e in _check(d, signed_gates=signed))
    d["gates"]["answer_match"]["directive"] = "accept at this score (owner, 2026-10-26)"
    d["gate_passed"] = True
    assert _check(d, signed_gates=signed) == []


@pytest.mark.parametrize("value, verdict", [(0.97, "pass"), (0.93, "borderline"), (0.84, "fail")])
def test_the_band_rule_for_a_scalar_with_no_interval(value, verdict):
    g = {"threshold": 0.9, "value": value, "k": None, "n": None, "ci_low": None, "ci_high": None,
         "rule": "band", "verdict": verdict}
    passed = verdict == "pass"
    d = _doc(gates={"relative_solve_rate": g}, gate_passed=passed)
    if verdict == "borderline":
        g["directive"] = "re-train"
    assert _check(d, signed_gates={"relative_solve_rate": 0.9}) == []


def test_a_metric_the_report_lacks_is_a_failed_gate():
    g = {"threshold": 0.95, "value": None, "k": None, "n": None, "ci_low": None, "ci_high": None,
         "rule": "missing", "verdict": "fail"}
    d = _doc(gates={**VALID["gates"], "identifier_grounding": g})
    assert _check(d) == []
    g["verdict"] = "pass"
    assert _check(d)


# --- gate_passed is what the verdicts say -------------------------------------------------

def _all_pass():
    signed = {"format_validity": 0.95}
    return _doc(gates={"format_validity": _wilson_gate(120, 120, 0.95)}, gate_passed=True), signed


def test_gate_passed_true_needs_every_gate_and_check_to_pass():
    assert any("$.gate_passed: true" in e for e in _check(_doc(gate_passed=True)))
    d, signed = _all_pass()
    assert _check(d, signed_gates=signed) == []
    d["checks"]["safety"]["passed"] = False
    assert any("$.gate_passed: true" in e for e in _check(d, signed_gates=signed))


def test_gate_passed_false_when_everything_passed_is_rejected():
    d, signed = _all_pass()
    d["gate_passed"] = False
    assert any("every gate passed" in e for e in _check(d, signed_gates=signed))


@pytest.mark.parametrize("claimed", [True, None])
def test_the_stage_complete_claim_must_be_the_same_boolean(claimed):
    """A null claim is refused too: the machine fails closed on it, and the file would then
    record a verdict the run did not act on."""
    assert any("metrics.gate_passed" in e for e in _check(VALID, claimed_passed=claimed))
    assert _check(VALID, claimed_passed=False) == []


# --- the prompt states what the driver enforces ------------------------------------------

def _required(schema, out):
    out.update(schema.get("required", []))
    for sub in schema.get("properties", {}).values():
        _required(sub, out)
    if isinstance(schema.get("additionalProperties"), dict):
        _required(schema["additionalProperties"], out)
    return out


def _enums(schema, out):
    out.update(v for v in schema.get("enum", []) if isinstance(v, str))
    for sub in schema.get("properties", {}).values():
        _enums(sub, out)
    if isinstance(schema.get("additionalProperties"), dict):
        _enums(schema["additionalProperties"], out)
    return out


def test_the_gate_clause_names_every_required_key_and_every_allowed_value():
    """Derived from the schema, so a key or a rule added there fails here until the prompt says
    it too. A contract the agent is not told is one it can only learn by being rejected."""
    text = json.loads((REPO / "agents/eval/harness.json").read_text())["systemPrompt"][0]["text"]
    clause = [ln for ln in text.splitlines() if ln.startswith('- "gate"')]
    assert len(clause) == 1, "the gate clause moved; re-anchor this guard"
    missing = sorted(k for k in _required(gc.SCHEMA, set()) if f'"{k}"' not in clause[0])
    assert not missing, f"the gate clause never names {missing}"
    values = sorted(v for v in _enums(gc.SCHEMA, set()) if f'"{v}"' not in clause[0])
    assert not values, f"the gate clause never offers {values}"
    assert "evaluation/gate.json" in clause[0] and '"checks"' in clause[0]


# --- the driver's check ------------------------------------------------------------------

class _S3:
    def __init__(self, objects=None, fail=None):
        self.objects, self.fail = dict(objects or {}), fail

    def get_object(self, Bucket, Key, Range=None):
        if self.fail:
            raise self.fail
        body = self.objects[f"s3://{Bucket}/{Key}"].encode()
        if Range:
            lo, hi = (int(x) for x in Range[len("bytes="):].split("-"))
            body = body[lo:hi + 1]
        return {"Body": io.BytesIO(body)}


class _Table:
    def __init__(self, items=()):
        self.items = list(items)

    def get_item(self, Key):
        hit = next((i for i in self.items if all(i.get(k) == v for k, v in Key.items())), None)
        return {"Item": hit} if hit else {}

    def query(self, **kw):
        return {"Items": [i for i in self.items if "sk" in i]}


class _DDB:
    def __init__(self, row=None, directives=()):
        self.tables = {"llmops-pipeline-runs": _Table([row] if row else []),
                       "llmops-stage-events": _Table(directives)}

    def Table(self, name):
        return self.tables[name]


@pytest.fixture(autouse=True)
def _tables(monkeypatch):
    monkeypatch.setenv("RUNS_TABLE", "llmops-pipeline-runs")
    monkeypatch.setenv("EVENTS_TABLE", "llmops-stage-events")


PREFIX = f"s3://llmops-data-test/runs/{RUN}"
EVENT = {"run_id": RUN, "iteration": 0, "manifest_uri": f"{PREFIX}/manifest.json"}
MANIFEST = json.dumps({"run_id": RUN, "params": {"gates": SIGNED}})
ROW = {"run_id": RUN, "status": "running", "signed_gates": json.dumps(SIGNED)}


def _store(gate, manifest=MANIFEST):
    return _S3({f"{PREFIX}/manifest.json": manifest, f"{PREFIX}/evaluation/gate.json": gate})


def _drive(gate, ddb=None, claimed=False, manifest=MANIFEST):
    return driver.check_gate_output(_store(gate, manifest), EVENT, {"gate_passed": claimed},
                                    ddb=ddb or _DDB(ROW))


def test_the_driver_accepts_a_conforming_file():
    assert _drive(json.dumps(VALID)) == []


def test_the_bars_come_from_the_run_row_not_the_agent_writable_manifest():
    """The harness role can PutObject on runs/*, so a manifest whose bars were lowered must not
    move the bar. The row is written by start-pipeline and is out of the agent's reach."""
    lowered = {"format_validity": 0.0, "answer_match": 0.0, "identifier_grounding": 0.0}
    d = _doc(gates={k: _wilson_gate(*kn, 0.0) for k, kn in
                    {"format_validity": (120, 120), "answer_match": (87, 120),
                     "identifier_grounding": (0, 120)}.items()}, gate_passed=True)
    errs = _drive(json.dumps(d), claimed=True,
                  manifest=json.dumps({"params": {"gates": lowered}}))
    assert any("signed bar is 0.95" in e for e in errs), errs


def test_a_run_from_before_the_row_carried_gates_falls_back_to_the_manifest():
    assert _drive(json.dumps(VALID), ddb=_DDB({"run_id": RUN, "status": "running"})) == []


def test_the_driver_rejects_a_legacy_file_with_the_reason():
    d = _doc()
    d["gate_results"] = d.pop("gates")
    assert any("'gates'" in e for e in _drive(json.dumps(d)))


@pytest.mark.parametrize("s3, why", [
    (_S3({f"{PREFIX}/manifest.json": MANIFEST, f"{PREFIX}/evaluation/gate.json": "not json"}),
     "not JSON"),
    (_S3(fail=RuntimeError("NoSuchKey")), "could not be read back"),
])
def test_an_absent_or_unreadable_file_is_a_rejection_never_a_pass(s3, why):
    errs = driver.check_gate_output(s3, EVENT, {"gate_passed": False}, ddb=_DDB(ROW))
    assert errs and why in errs[0]


def test_an_unreadable_run_row_is_a_rejection_never_a_pass():
    class _Down(_DDB):
        def Table(self, name):
            raise RuntimeError("ProvisionedThroughputExceeded")
    errs = _drive(json.dumps(VALID), ddb=_Down())
    assert errs and "could not be read" in errs[0]


def test_an_oversized_file_is_refused_unread():
    """The agent writes the file; read whole, a file of any size would land in the driver's
    512 MB."""
    d = _doc(padding="x" * (gc.MAX_BYTES + 10))
    errs = _drive(json.dumps(d))
    assert errs and "over" in errs[0] and "bytes" in errs[0]


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_nan_and_infinity_are_not_numbers_here(constant):
    """Python's json accepts them. A NaN bar compares false against everything: the bar check
    passes and every verdict reads as borderline."""
    text = json.dumps(VALID).replace('"threshold": 0.95', f'"threshold": {constant}', 1)
    errs = _drive(text)
    assert errs and "not JSON" in errs[0]
    d = _doc()
    d["gates"]["format_validity"]["threshold"] = float(constant.replace("Infinity", "inf"))
    assert any("not a finite number" in e for e in _check(d))


def test_a_proportion_is_decided_by_its_interval_and_nothing_else():
    d = _doc()
    d["gates"]["identifier_grounding"] = {
        "threshold": 0.95, "value": 0.99, "k": None, "n": None, "ci_low": None, "ci_high": None,
        "rule": "band", "verdict": "pass"}
    assert any("decided by rule wilson95" in e for e in _check(d))


BORDER = {"answer_match": 0.7}


def _border(passed, directive):
    d = _doc(gates={"answer_match": _wilson_gate(87, 120, 0.7)}, gate_passed=passed)
    d["gates"]["answer_match"]["directive"] = directive
    return d


def test_a_borderline_directive_must_be_one_the_run_was_given():
    given = [{"decision": "accept at this score", "actor": "owner"}]
    assert _check(_border(True, "accept at this score"), signed_gates=BORDER, directives=given) == []
    errs = _check(_border(True, "approved"), signed_gates=BORDER, directives=given)
    assert any("is not a directive this run was given" in e for e in errs)


def test_a_borderline_pass_needs_a_person_not_the_conductor():
    given = [{"decision": "accept at this score", "actor": "conductor"}]
    errs = _check(_border(True, "accept at this score"), signed_gates=BORDER, directives=given)
    assert any("person's directive" in e for e in errs)
    assert _check(_border(False, "accept at this score"), signed_gates=BORDER,
                  directives=given) == [], "a conductor may still send a borderline back"


def test_the_driver_reads_directives_from_the_table_the_agent_cannot_write():
    row = {"run_id": RUN, "status": "running", "signed_gates": json.dumps(BORDER)}
    given = [{"run_id": RUN, "sk": "directive#2026-10-28T00:00:00Z",
              "decision": "accept at this score", "actor": "owner", "delivered": True}]
    gate = json.dumps(_border(True, "accept at this score"))
    assert _drive(gate, ddb=_DDB(row, given), claimed=True) == []
    undelivered = [{**given[0], "delivered": False}]
    assert _drive(gate, ddb=_DDB(row, undelivered), claimed=True)


def test_only_the_gate_task_is_held_to_the_gate_contract():
    """evaluate and score write other files; holding them to this would reject every eval stage."""
    src = (REPO / "orchestration/harness_driver/handler.py").read_text()
    body = src[src.index("def handle_stage_complete("):]
    body = body[:body.index("\ndef ")]
    assert re.search(r'if stage == "eval" and task == "gate":\s*\n\s*invalid = check_gate_output',
                     body), "the gate check is no longer scoped to (eval, gate)"


def test_the_rejection_tells_the_agent_what_to_fix_and_caps_the_list():
    reason = driver._gate_contract_rejection([f"err{i}" for i in range(30)])
    assert "gate/1" in reason and "err0" in reason and "and 10 more" in reason
    assert "err25" not in reason


# --- the readers -------------------------------------------------------------------------

def test_the_shared_reader_reads_the_contract_shape():
    """The provenance card and the Outcome tab both read gates through gate_view."""
    assert prov.GATE_SCHEMA == gc.SCHEMA_VERSION
    v = prov.gate_view(VALID)
    assert v["shape"] == "gate/1" and v["gate_passed"] is False
    assert v["metrics"]["answer_match"]["k"] == "87" and v["metrics"]["answer_match"]["n"] == "120"
    assert v["metrics"]["identifier_grounding"]["verdict"] == "fail"


# --- the Lambda bundle -------------------------------------------------------------------

def test_the_driver_bundle_carries_the_contract_and_its_schema():
    """gate_contract reads its schema at import time, and the driver imports it at module load,
    so a bundle without either fails every driver invocation, not just gates."""
    l7 = _load("lambdas_gate_contract", "deploy/07_lambdas.py")
    names = zipfile.ZipFile(io.BytesIO(
        l7.bundle(REPO / "orchestration/harness_driver/handler.py"))).namelist()
    assert {"gate_contract.py", "gate.schema.json", "sweep_contract.py"} <= set(names), names


# --- where the bars are put ----------------------------------------------------------------

def test_start_pipeline_puts_the_gates_on_the_run_row(monkeypatch):
    """The driver's gate check reads them there, so a run that starts without them on its row
    would fall back to the agent-writable manifest."""
    lm = _load("lakehouse_mode_for_gate", "tests/test_lakehouse_mode.py")
    for k, v in {"DATA_BUCKET": "data-bucket", "RUNS_TABLE": "runs", "EVENT_BUS": "bus",
                 "STATE_MACHINE_ARN": "arn:sm"}.items():
        monkeypatch.setenv(k, v)
    clients = {"s3": lm._Rec(), "ddb": lm._Rec(), "events": lm._Rec(), "sfn": lm._Rec()}
    lm.sp.handler({"trigger_source": "test", "params": dict(lm.BUILD)}, clients=clients)
    (_, kw), = [c for c in clients["ddb"].calls if c[0] == "put_item"]
    (_, put), = [c for c in clients["s3"].calls if c[0] == "put_object"]
    signed = json.loads(put["Body"])["params"]["gates"]
    assert json.loads(kw["Item"]["signed_gates"]) == signed and signed
