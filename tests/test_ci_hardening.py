"""The redaction scanner catches more secret shapes, and the documented push path scans first.

The 2026-10-30 audit found: the scanner had no rule for ECR-URI account ids, GitHub/HuggingFace/
Anthropic/Slack tokens or private keys; `--diff-filter=ACM` skipped renamed-and-edited files; and
`tools/push_via_api.py` (the documented push path, used because commits are hook-blocked) never ran
the scan, so a secret could reach the public remote unscanned. Each is pinned here with a synthetic
string — no real secret in this file.
"""
from __future__ import annotations

import importlib.util
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
rs = importlib.util.module_from_spec(
    importlib.util.spec_from_file_location("redaction_scan_ci", REPO / "tests/redaction_scan.py"))
importlib.util.spec_from_file_location("redaction_scan_ci", REPO / "tests/redaction_scan.py").loader.exec_module(rs)


# ── new secret shapes are caught ─────────────────────────────────────────────
# Fixtures are built by concatenation so THIS file's bytes never contain a real secret shape
# (and so it needs no whole-file exemption); each runtime value does contain one.
@pytest.mark.parametrize("label, blob", [
    ("ECR registry account id", b"image: " + b"1234" + b"56785678" + b".dkr.ecr.us-east-1.amazonaws.com/x"),
    ("GitHub token", b"token = ghp_" + b"A" * 36),
    ("Hugging Face token", b"HF_TOKEN=hf_" + b"B" * 34),
    ("Anthropic API key", b"key: sk-ant-" + b"api03" + b"-" + b"C" * 24),
    ("Slack token", b"xoxb" + b"-" + b"a1b2c3d4e5f6g7h8i9j0"),
    ("private key block", b"-----BEGIN RSA " + b"PRIVATE KEY-----\nMIIabc\n"),
    ("AWS secret key assignment", b"aws_secret_access_key" + b" = " + b"a" * 40),
], ids=["ecr", "github", "hf", "anthropic", "slack", "privkey", "awssecret"])
def test_the_scanner_flags_the_new_secret_shapes(label, blob):
    hits = [f for f in rs.scan_blob("x.txt", blob)]
    assert any(label == name for _, name, _ in hits), f"{label} not flagged in {hits}"


def test_a_plain_12_digit_run_before_a_dot_is_still_caught_as_ecr():
    """GENERIC_ACCOUNT_ID skips a 12-digit run followed by '.', which is exactly the ECR host
    shape, so the dedicated rule is what catches an account id leaking through an image URI."""
    hits = rs.scan_blob("deploy.yaml", b"4444" + b"55556666" + b".dkr.ecr.eu-west-1.amazonaws.com/repo")
    assert any(name == "ECR registry account id" for _, name, _ in hits)


def test_an_ordinary_sentence_is_not_flagged():
    assert rs.scan_blob("notes.md", b"the model trained for about forty-five cents of compute") == []


# ── renamed-and-edited files are scanned (ACMR) ──────────────────────────────
def test_the_staged_scan_includes_renamed_files():
    src = (REPO / "tests/redaction_scan.py").read_text()
    assert '"--diff-filter=ACMR"' in src, "the staged scan still uses ACM; a renamed+edited file is skipped"
    assert '--diff-filter=ACMR' in (REPO / "hooks/pre-commit").read_text()


# ── the documented push path scans before it pushes ─────────────────────────
def _push_mod():
    spec = importlib.util.spec_from_file_location("push_via_api_ci", REPO / "tools/push_via_api.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_push_via_api_refuses_before_any_api_call_when_the_scan_finds_a_secret(monkeypatch):
    """Functional, not source-level: a finding (exit 1) must stop the push before it reads a
    token or touches a ref. If the guard is disabled, main proceeds past this point (and would
    hit git/gh), so it would NOT raise 'push blocked'."""
    pv = _push_mod()
    monkeypatch.setattr(pv, "_preflight_redaction_scan", lambda: 1)
    # Make anything after the guard fail loudly, so a disabled guard cannot look like success.
    monkeypatch.setattr(pv, "git", lambda *a, **k: (_ for _ in ()).throw(AssertionError("reached git — the scan did not gate")))
    with pytest.raises(SystemExit, match="push blocked"):
        pv.main(["--branch", "nope"])


def test_the_preflight_passes_on_a_clean_tree():
    # The real scan over this repo (clean) returns 0 — the guard does not false-positive.
    assert _push_mod()._preflight_redaction_scan() == 0


# ── the workflow passes inputs through env, never into the script body ───────
def test_the_workflow_passes_inputs_through_env_not_the_script():
    """A ${{ inputs.X }} expansion happens before bash runs, so interpolating it into the
    run: body is command injection in the step that holds the assumed AWS role."""
    wf = (REPO / ".github/workflows/run-pipeline.yml").read_text()
    body = wf[wf.index("- name: Start pipeline"):]
    run = body[body.index("run: |"):]
    assert "${{ inputs." not in run, "an input is interpolated into the run: script"
    assert "NOTE: ${{ inputs.note }}" in body and "--arg note \"$NOTE\"" in run
    assert 'case "$TASK_COUNT" in' in run, "the counts are not validated as integers"


def test_the_ci_workflows_take_least_privilege_tokens():
    for wf in ("ci.yml", "redaction-check.yml"):
        s = (REPO / ".github/workflows" / wf).read_text()
        assert "permissions:\n  contents: read" in s, f"{wf} has no least-privilege permissions block"
