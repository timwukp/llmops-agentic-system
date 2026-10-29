"""IAM scope-down, Batch 2a (2026-10-28 security audit).

Three classes of over-grant the audit found, each closed by a change this file pins:
  1. the harness role could forge pipeline events (`events:PutEvents`, no condition);
  2. it could overwrite the instruments it is judged by or executes (`contracts/*`,
     `pipeline/contracts/*`, `reports/*` were Get+Put);
  3. it could register a model as Approved, and the console could rewrite any harness's prompt.

The model-approval guarantee is enforced two ways: an IAM `NeverApproveAtRegistration` Deny (whose
condition key must be confirmed against the live account with simulate_principal_policy at deploy),
and — the part that does not depend on that key — start-pipeline forcing the status regardless of
input. This file tests the deterministic half; the Deny's presence is pinned, its runtime efficacy
is a deploy-time check.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
HARNESS = json.loads((REPO / "deploy/iam/harness_execution_role.json").read_text())["permissionsPolicy"]["Statement"]
CONSOLE = json.loads((REPO / "deploy/console/iam-policy.json").read_text())["Statement"]


def _sid(stmts, sid):
    return next((s for s in stmts if s.get("Sid") == sid), None)


def _acts(s):
    a = s.get("Action", [])
    return [a] if isinstance(a, str) else a


def _res(s):
    r = s.get("Resource", [])
    return [r] if isinstance(r, str) else r


# ── 1. no event forgery ────────────────────────────────────────────────────────
def test_the_harness_role_cannot_put_pipeline_events():
    """The driver is the only emitter; no agent prompt calls put-events. An unconditioned
    grant let a hijacked agent forge EscalatedToHuman and drive a conductor triage."""
    for s in HARNESS:
        if s.get("Effect") == "Allow":
            assert "events:PutEvents" not in _acts(s), f"{s.get('Sid')} still grants events:PutEvents"


# ── 2. instruments are read-only ───────────────────────────────────────────────
def test_the_instruments_are_not_writable():
    """contracts/* (cost_model.py, the gate schema+checker) and reports/* (the driver's
    canonical report) are Get-only; an explicit Deny backs it."""
    write = _sid(HARNESS, "S3PipelineObjects")
    for p in ("contracts", "pipeline/contracts", "reports"):
        assert not any(f"/{p}/" in r for r in _res(write)), f"S3PipelineObjects still writes {p}/"
    ro = _sid(HARNESS, "S3InstrumentsReadOnly")
    assert ro and _acts(ro) == ["s3:GetObject"]
    assert all(any(f"/{p}/" in r for r in _res(ro)) for p in ("contracts", "pipeline/contracts", "reports"))


def test_an_explicit_deny_backs_the_instrument_and_code_trees():
    d = _sid(HARNESS, "S3DenyInstrumentWrites")
    assert d and d["Effect"] == "Deny"
    assert set(_acts(d)) == {"s3:PutObject", "s3:DeleteObject"}
    for p in ("contracts", "pipeline/contracts", "reports", "code", "skills"):
        assert any(f"/{p}/" in r for r in _res(d)), f"the Deny does not cover {p}/"


# ── 3a. model approval — the IAM Deny (presence; runtime efficacy is a deploy check) ──
def test_the_harness_role_denies_registering_an_approved_model():
    d = _sid(HARNESS, "NeverApproveAtRegistration")
    assert d and d["Effect"] == "Deny"
    assert "sagemaker:CreateModelPackage" in _acts(d) and "sagemaker:UpdateModelPackage" in _acts(d)
    assert d["Condition"]["StringEquals"]["sagemaker:ModelApprovalStatus"] == "Approved"


# ── 3b. model approval — the guarantee that does not depend on the condition key ──
def _start_pipeline():
    spec = importlib.util.spec_from_file_location(
        "start_pipeline_iam", REPO / "orchestration/start_pipeline/handler.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("source", ["params", "plan"])
def test_no_input_can_register_an_approved_model(source):
    sp = _start_pipeline()
    want = sp.APPROVAL_STATUS_DEFAULT
    attempt = {"model_package_approval_status": "Approved"}
    merged = sp._merge_params(attempt if source == "params" else {},
                              {"params": attempt} if source == "plan" else {})
    assert merged["model_package_approval_status"] == want, (
        f"a {source} set approval to Approved and it survived the merge")


def test_the_pipeline_still_registers_as_pending_by_default():
    sp = _start_pipeline()
    assert sp._merge_params({}, {})["model_package_approval_status"] == "PendingManualApproval"


# ── 3c. the console cannot rewrite any harness's prompt ────────────────────────
def test_the_console_can_only_touch_this_projects_harnesses():
    assert _sid(CONSOLE, "AgentCoreControl") is None, "the wildcard UpdateHarness grant is gone"
    own = _sid(CONSOLE, "AgentCoreOwnHarness")
    assert own and set(_acts(own)) == {"bedrock-agentcore:GetHarness", "bedrock-agentcore:UpdateHarness"}
    assert own["Resource"].endswith("harness/llmops_*"), own["Resource"]
    lst = _sid(CONSOLE, "AgentCoreList")
    assert lst and "bedrock-agentcore:UpdateHarness" not in _acts(lst)


def test_the_console_role_drops_the_unused_dangerous_grants():
    """Neither is called by the console code; both were abuse levers (anti-forensics,
    the admin's plaintext password)."""
    assert _sid(CONSOLE, "Secrets") is None, "the Secrets Manager grant is still present"
    fas = _sid(CONSOLE, "LogsDescribeAndFas")
    assert fas and "logs:PutRetentionPolicy" not in _acts(fas)
