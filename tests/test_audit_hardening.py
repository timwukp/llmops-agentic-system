"""Batch 5 of the 2026-10-28 security audit: process isolation for untrusted transform code,
and the storage deploy's data-at-rest / in-transit / accidental-deletion hardening.

Two findings:

1. **The ARC verification sandbox is not a sandbox.** `pipeline/v2/verify_sandbox.py` runs
   model-written `transform(grid)` under a restricted-builtins namespace. A restricted builtins
   dict is correctness scaffolding, not a boundary: `().__class__.__bases__[0].__subclasses__()`
   and `np.__builtins__['__import__']('os')` are plain attribute walks no builtin filter can stop,
   and from `os` the code reaches the process's AWS credentials and the IMDS token. The fix runs
   the code in a FORKED child whose environment has every credential-bearing variable scrubbed and
   whose address space is rlimited, then kills it if it overruns. The restricted namespace stays —
   it is cheap and it catches honest mistakes — but the child is what makes running the code safe.
   These tests prove the boundary holds (a known escape returns scrubbed values) and that the
   isolate path still runs legitimate transforms and still enforces the timeout.

2. **The data bucket and the DynamoDB tables had no in-transit deny, no point-in-time recovery,
   and no deletion protection.** `ensure_bucket` now attaches a `DenyInsecureTransport` policy;
   `ensure_table` applies PITR (where asked) and `DeletionProtectionEnabled` idempotently, to a
   table it created now AND to one that already existed — which is where a pitr-on-create-only
   path silently missed the stage-events audit trail.

Run: .venv/bin/python -m pytest tests/test_audit_hardening.py -q
"""
from __future__ import annotations

import importlib.util
import json
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


vs = _load("verify_sandbox_audit", "pipeline/v2/verify_sandbox.py")
storage = _load("storage_audit", "deploy/03_storage.py")


# ── 1. process isolation ─────────────────────────────────────────────────────
# A transform that escapes the restricted namespace, reaches os, and reports whether an AWS
# credential is visible in its environment: [[1,1]] if the secret leaked, [[0,0]] if it did not.
# The escape itself is deliberately real — `np.__builtins__` is the module's true builtins dict,
# whose `__import__` is unrestricted — because a test that cannot escape proves nothing about a
# boundary that must hold against code that can.
_ESCAPE = """
def transform(grid):
    b = np.__builtins__
    _import = b['__import__'] if isinstance(b, dict) else b.__import__
    os = _import('os')
    leaked = os.environ.get('AWS_SECRET_ACCESS_KEY')
    v = 1 if leaked else 0
    return [[v, v]]
"""

_SENTINEL = "AKIA_this_is_a_test_only_sentinel_secret"


def test_the_escape_reads_a_credential_in_process_so_the_test_is_not_a_tautology(monkeypatch):
    """Control: with no isolation, the escape works and the credential leaks. If this returned
    [[0,0]] the isolated case below would pass for the wrong reason (a broken escape)."""
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", _SENTINEL)
    res = vs.execute_transform(_ESCAPE, [[0]], timeout_sec=5, isolate=False)
    assert res["status"] == "success", res
    assert res["output"] == [[1, 1]], "the escape should see the credential when run in-process"


def test_the_isolated_child_cannot_see_the_credential(monkeypatch):
    """The boundary: the same escape, run isolated, sees a scrubbed environment and reports
    [[0,0]]. The credential is set in THIS process; the forked child inherits it and the worker
    scrubs it before the untrusted code runs."""
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", _SENTINEL)
    res = vs.execute_transform(_ESCAPE, [[0]], timeout_sec=5, isolate=True)
    assert res["status"] == "success", res
    assert res["output"] == [[0, 0]], "the credential must not be visible in the isolated child"


def test_the_worker_scrubs_every_credential_shaped_variable(monkeypatch):
    """Not just the one well-known name: a token or secret under any key is gone. The container's
    role is delivered through AWS_CONTAINER_CREDENTIALS_* and the IMDS hop through a token, so the
    scrub is by shape (AWS_*, *TOKEN*, *SECRET*), not an allow-list of names."""
    for k in ("AWS_SESSION_TOKEN", "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
              "SOME_API_TOKEN", "DB_SECRET"):
        monkeypatch.setenv(k, _SENTINEL)
    code = """
def transform(grid):
    b = np.__builtins__
    _import = b['__import__'] if isinstance(b, dict) else b.__import__
    os = _import('os')
    hit = any(v == %r for v in os.environ.values())
    return [[1 if hit else 0]]
""" % _SENTINEL
    res = vs.execute_transform(code, [[0]], timeout_sec=5, isolate=True)
    assert res["status"] == "success" and res["output"] == [[0]], res


def test_a_legitimate_transform_still_runs_isolated():
    code = "def transform(grid):\n    return [[c + 1 for c in row] for row in grid]\n"
    res = vs.execute_transform(code, [[1, 2], [3, 4]], timeout_sec=5, isolate=True)
    assert res["status"] == "success" and res["output"] == [[2, 3], [4, 5]], res


def test_an_overrunning_transform_is_killed_not_left_running():
    code = "def transform(grid):\n    while True:\n        pass\n"
    res = vs.execute_transform(code, [[0]], timeout_sec=1, isolate=True)
    assert res["status"] == "timeout", res


def test_isolate_defaults_to_on():
    """A caller who forgets the keyword gets the safe path. The signature's default is the
    control that makes every existing call site isolated without editing it."""
    import inspect
    assert inspect.signature(vs.execute_transform).parameters["isolate"].default is True


# ── 2. storage hardening ─────────────────────────────────────────────────────
def _client_error(code):
    from botocore.exceptions import ClientError
    return ClientError({"Error": {"Code": code, "Message": code}}, "DescribeTable")


class _FakeDDB:
    """Records the hardening calls. `present` names tables that already exist."""

    def __init__(self, present=()):
        self.present = set(present)
        self.created, self.pitr_on, self.deletion_protection = [], [], []

    def describe_table(self, TableName):
        if TableName in self.present:
            return {"Table": {"TableName": TableName}}
        raise _client_error("ResourceNotFoundException")

    def create_table(self, TableName, **kw):
        self.created.append(TableName)
        self.present.add(TableName)

    def get_waiter(self, _name):
        return type("W", (), {"wait": lambda _s, **_k: None})()

    def update_continuous_backups(self, TableName, PointInTimeRecoverySpecification):
        assert PointInTimeRecoverySpecification["PointInTimeRecoveryEnabled"] is True
        self.pitr_on.append(TableName)

    def update_table(self, TableName, DeletionProtectionEnabled):
        assert DeletionProtectionEnabled is True
        self.deletion_protection.append(TableName)


_SPEC = {"AttributeDefinitions": [{"AttributeName": "run_id", "AttributeType": "S"}],
         "KeySchema": [{"AttributeName": "run_id", "KeyType": "HASH"}]}


def test_a_new_table_is_created_then_gains_pitr_and_deletion_protection():
    ddb = _FakeDDB()
    assert storage.ensure_table(ddb, "llmops-x", _SPEC, dry=False, pitr=True) == "created"
    assert ddb.created == ["llmops-x"]
    assert ddb.pitr_on == ["llmops-x"] and ddb.deletion_protection == ["llmops-x"]


def test_an_existing_table_is_not_recreated_but_still_gains_the_protections():
    """The whole point of the rewrite: a table created before this deploy (the stage-events
    audit trail, created on day one with no pitr flag) picks up PITR and deletion protection on
    the next run instead of being skipped by an early `return "exists"`."""
    ddb = _FakeDDB(present=["llmops-x"])
    assert storage.ensure_table(ddb, "llmops-x", _SPEC, dry=False, pitr=True) == "exists"
    assert ddb.created == []
    assert ddb.pitr_on == ["llmops-x"] and ddb.deletion_protection == ["llmops-x"]


def test_deletion_protection_is_applied_even_without_pitr():
    """DeletionProtection is unconditional; PITR is the opt-in. A table asked for without pitr
    still cannot be dropped by a stray DeleteTable."""
    ddb = _FakeDDB()
    assert storage.ensure_table(ddb, "llmops-y", _SPEC, dry=False, pitr=False) == "created"
    assert ddb.pitr_on == [] and ddb.deletion_protection == ["llmops-y"]


def test_a_dry_run_touches_no_table():
    ddb = _FakeDDB()
    assert storage.ensure_table(ddb, "llmops-z", _SPEC, dry=True, pitr=True) == "would create"
    assert ddb.created == [] and ddb.pitr_on == [] and ddb.deletion_protection == []


def test_the_run_events_table_is_deployed_with_pitr():
    """The one table whose loss is unrecoverable from anywhere else — the stage-events audit
    trail — must be deployed with PITR. Derived from the call in main(), not hand-asserted."""
    src = (REPO / "deploy/03_storage.py").read_text()
    call = src[src.index("EVENTS_TABLE, {"):]
    call = call[:call.index("results[ESTIMATES_TABLE]")]
    assert "pitr=True" in call, "the stage-events table must be created with PITR"


class _FakeS3:
    def __init__(self):
        self.policies = {}

    def head_bucket(self, Bucket):
        return {}

    def __getattr__(self, name):
        if name == "put_bucket_policy":
            def put(Bucket, Policy):
                self.policies[Bucket] = json.loads(Policy)
            return put
        return lambda **kw: {}


def test_the_bucket_gets_a_tls_only_deny():
    s3 = _FakeS3()
    assert storage.ensure_bucket(s3, "llmops-data", "us-east-1", dry=False) == "exists"
    pol = s3.policies["llmops-data"]
    (stmt,) = pol["Statement"]
    assert stmt["Effect"] == "Deny" and stmt["Action"] == "s3:*"
    assert stmt["Condition"] == {"Bool": {"aws:SecureTransport": "false"}}
    assert set(stmt["Resource"]) == {"arn:aws:s3:::llmops-data", "arn:aws:s3:::llmops-data/*"}, \
        "the deny must cover both the bucket and its objects"


def test_a_dry_run_attaches_no_bucket_policy():
    s3 = _FakeS3()
    storage.ensure_bucket(s3, "llmops-data", "us-east-1", dry=True)
    assert s3.policies == {}
