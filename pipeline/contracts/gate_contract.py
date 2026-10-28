"""The eval gate's output contract: gate.schema.json plus the rules a schema cannot say.

gate.json was never specified -- the eval prompt named neither the file nor its shape -- and four
live runs wrote four shapes (see the schema's $comment). Two readers depend on it, the provenance
card on the registered model and the console's Outcome tab, and both had to guess. The driver now
refuses an eval gate stage_complete whose file fails `check`, and tells the agent why.

The cross-field rules are the gate's own: every gate the signed plan names is decided at the bar it
signed, no gate is invented, each verdict is the one its rule gives (a Wilson interval is
recomputed from k and n), a borderline is never handed in unresolved, and gate_passed is what the
verdicts say. A gate.json that passes this has a verdict a reviewer can re-derive from its own
numbers.

Dependency-free, like sweep_contract: it is vendored flat into the driver's Lambda bundle.
"""
from __future__ import annotations

import json
import math
import pathlib
from statistics import NormalDist

try:
    from pipeline.contracts.sweep_contract import validate
except ImportError:  # Lambda bundle layout
    from sweep_contract import validate  # type: ignore

SCHEMA_VERSION = "gate/1"
SCHEMA = json.loads(pathlib.Path(__file__).with_name("gate.schema.json").read_text())
#: The two-sided 95% normal quantile, derived rather than typed.
Z95 = NormalDist().inv_cdf(0.975)
#: A scalar with no interval is borderline AT or within this of its threshold (the eval prompt's
#: fixed band).
BAND = 0.05
#: How far a reported number may sit from the one it is derived from: agents round to 4-6 places.
TOL = 0.0015
#: Proportions with a count behind them. Their gates are decided on the Wilson interval of k of
#: n (or are `missing`, which fails) and nothing else: an agent free to label one `band` or
#: `interval` could pass it on numbers the driver cannot recompute.
WILSON_METRICS = ("format_validity", "answer_match", "identifier_grounding", "judge_score")
#: gate.json is a few KB. A larger file is refused unread rather than loaded into the driver's
#: 512 MB: the agent writes it, and a file of any size would otherwise be read whole.
MAX_BYTES = 256 * 1024
#: `check`'s claimed_passed when there is no stage_complete to compare against.
UNSET = object()


def _no_constant(name):
    raise ValueError(f"{name} is not a number JSON allows")


def loads(data: bytes):
    """json.loads that refuses NaN and Infinity, which Python accepts by default. A NaN bar
    compares false against everything, so the bar check passes and every verdict reads as
    borderline."""
    return json.loads(data, parse_constant=_no_constant)


def _non_finite(value, path="$") -> list:
    if isinstance(value, float) and not math.isfinite(value):
        return [f"{path}: {value} is not a finite number"]
    if isinstance(value, dict):
        return [e for k, v in value.items() for e in _non_finite(v, f"{path}.{k}")]
    if isinstance(value, list):
        return [e for i, v in enumerate(value) for e in _non_finite(v, f"{path}[{i}]")]
    return []


def wilson(k: int, n: int) -> tuple:
    p = k / n
    centre = (p + Z95 ** 2 / (2 * n)) / (1 + Z95 ** 2 / n)
    half = Z95 * math.sqrt(p * (1 - p) / n + Z95 ** 2 / (4 * n * n)) / (1 + Z95 ** 2 / n)
    return max(0.0, centre - half), min(1.0, centre + half)


def _verdict(g: dict) -> tuple:
    """(the verdict the gate's rule gives, [errors in the numbers it was given])."""
    rule, thr, errs = g["rule"], g["threshold"], []
    if rule == "missing":
        if g["value"] is not None:
            errs.append("rule missing but a value is given")
        return "fail", errs
    if g["value"] is None:
        return None, [f"rule {rule} needs a value"]
    if rule == "band":
        v = g["value"]
        return ("pass" if v > thr + BAND else "fail" if v < thr - BAND else "borderline"), errs
    lo, hi = g["ci_low"], g["ci_high"]
    if lo is None or hi is None:
        return None, [f"rule {rule} needs ci_low and ci_high"]
    if rule == "wilson95":
        k, n = g["k"], g["n"]
        if k is None or not n or k > n:
            return None, ["rule wilson95 needs k <= n and n > 0"]
        if abs(g["value"] - k / n) > TOL:
            errs.append(f"value {g['value']} is not k/n = {k}/{n} = {k / n:.4f}")
        wl, wh = wilson(k, n)
        if abs(lo - wl) > TOL or abs(hi - wh) > TOL:
            errs.append(f"[ci_low, ci_high] = [{lo}, {hi}] is not the Wilson 95% interval of "
                        f"{k}/{n} = [{wl:.4f}, {wh:.4f}]")
    if lo > hi:
        errs.append(f"ci_low {lo} is above ci_high {hi}")
    return ("pass" if lo >= thr else "fail" if hi < thr else "borderline"), errs


def check(doc, run_id: str | None = None, iteration: int | None = None,
          signed_gates: dict | None = None, claimed_passed=UNSET,
          directives: list | None = None) -> list:
    """Schema errors, then the cross-field rules. [] means the gate may be accepted.

    `signed_gates` is the signed plan's {metric: threshold}; `claimed_passed` is the
    stage_complete's metrics.gate_passed, which is what the state machine branches on. A null
    or absent claim is refused rather than read as false: the machine would fail closed on it,
    and gate.json would then record a verdict the run did not act on.

    `directives` is what was DELIVERED to this run, as [{"decision", "actor"}], read by the
    driver from a table the agent cannot write. When given, a borderline's directive must be
    one of them, and a borderline counted as passed must rest on a person's directive, not
    the conductor agent's.
    """
    errs = _non_finite(doc)
    if errs:
        return errs
    errs = validate(doc, SCHEMA)
    if errs or not isinstance(doc, dict):
        return errs  # the rules below assume the shape; report the shape first
    if run_id and doc["run_id"] != run_id:
        errs.append(f"$.run_id: {doc['run_id']!r} but this gate was dispatched as {run_id!r}")
    if iteration is not None and doc["iteration"] != iteration:
        errs.append(f"$.iteration: {doc['iteration']} but this is iteration {iteration}")
    gates = doc["gates"]
    if signed_gates is None:
        errs.append("the signed plan names no gates, so there is no bar to decide against: "
                    "escalate_human instead of deciding")
    else:
        for name in sorted(set(signed_gates) - set(gates)):
            errs.append(f"$.gates: the signed plan gates {name!r} and gate.json does not decide it "
                        "(a metric the report lacks is rule missing, verdict fail)")
        for name in sorted(set(gates) - set(signed_gates)):
            errs.append(f"$.gates.{name}: no signed gate is named {name!r}; a bar the plan did not "
                        "sign is not a gate")
    for name, g in gates.items():
        want = signed_gates.get(name) if signed_gates else None
        try:
            if want is not None and abs(float(want) - g["threshold"]) > 1e-9:
                errs.append(f"$.gates.{name}.threshold: {g['threshold']} but the signed bar is {want}")
        except (TypeError, ValueError):
            errs.append(f"$.gates.{name}: the signed bar {want!r} is not a number")
        if name in WILSON_METRICS and g["rule"] not in ("wilson95", "missing"):
            errs.append(f"$.gates.{name}.rule: {name} is a proportion and is decided by rule "
                        f"wilson95 on its k and n, not {g['rule']!r}")
        verdict, bad = _verdict(g)
        errs += [f"$.gates.{name}: {b}" for b in bad]
        if verdict is not None and g["verdict"] != verdict:
            errs.append(f"$.gates.{name}.verdict: {g['verdict']!r} but rule {g['rule']} on these "
                        f"numbers gives {verdict!r}")
        if g["verdict"] == "borderline" and not g.get("directive", "").strip():
            errs.append(f"$.gates.{name}: borderline with no directive. A borderline is not "
                        "handed in: page_human, then checkpoint until a directive arrives, and "
                        "record it here as directive")
        elif g["verdict"] == "borderline" and directives is not None:
            said = g["directive"].strip()
            match = [d for d in directives if str(d.get("decision", "")).strip() == said]
            if not match:
                errs.append(f"$.gates.{name}.directive: {said[:80]!r} is not a directive this "
                            "run was given; copy the decision a checkpoint delivered, word for word")
            elif doc["gate_passed"] and all(d.get("actor") == "conductor" for d in match):
                errs.append(f"$.gates.{name}: a borderline counted as passed needs a person's "
                            "directive; this one came from the conductor agent")
    resolved = all(g["verdict"] == "pass" or (g["verdict"] == "borderline" and g.get("directive"))
                   for g in gates.values())
    checks_ok = all(c["passed"] for c in doc.get("checks", {}).values())
    if doc["gate_passed"] and not (resolved and checks_ok and gates):
        errs.append("$.gate_passed: true, but a gate failed, is unresolved, or a check failed")
    if not doc["gate_passed"] and all(g["verdict"] == "pass" for g in gates.values()) \
            and checks_ok and gates:
        errs.append("$.gate_passed: false, but every gate passed and every check passed")
    if claimed_passed is not UNSET and claimed_passed is not doc["gate_passed"]:
        errs.append(f"$.gate_passed: {doc['gate_passed']} but stage_complete reports "
                    f"metrics.gate_passed={claimed_passed!r}; the state machine branches on that, "
                    "so the two must be the same boolean")
    return errs
