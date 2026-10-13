"""Phase 0 spike harness (tools/spikes/): the contract every spike must keep, checked offline.

conftest.py refuses sockets and strips credentials, so everything here runs against fake
clients. The point is not to test AWS; it is to make the properties the evidence files rely on
impossible to lose: criteria are declared up front, plan() is offline, every resource a spike
creates is deleted again even when run() crashes, and nothing committed carries an account id.
"""
from __future__ import annotations

import gzip
import importlib.util
import json
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
SPIKES = REPO / "tools" / "spikes"
sys.path.insert(0, str(SPIKES))
import _spike  # noqa: E402

ACCOUNT = "123456789012"  # documentation account — never a real one, per the repo rule


def _load(path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SPIKE_FILES = sorted(SPIKES.glob("s[0-9]_*.py"))
MODULES = {p.stem: _load(p) for p in SPIKE_FILES}


def _spike_class(mod):
    return next(v for v in vars(mod).values()
                if isinstance(v, type) and issubclass(v, _spike.Spike) and v is not _spike.Spike)


# ── definitions ──────────────────────────────────────────────────────────────
def test_there_are_exactly_the_seven_planned_spikes():
    assert [p.stem.split("_")[0] for p in SPIKE_FILES] == [f"s{i}" for i in range(1, 8)]


@pytest.mark.parametrize("stem", sorted(MODULES))
def test_every_spike_declares_its_question_gate_and_criteria(stem):
    cls = _spike_class(MODULES[stem])
    assert cls.NAME == stem, "NAME must match the file name: evidence files are named from it"
    assert cls.NUMBER == int(stem[1])
    assert cls.QUESTION and cls.GATES
    ids = [cid for cid, _ in cls.PASS_CRITERIA]
    assert ids and len(ids) == len(set(ids)), "criteria ids must be present and unique"
    assert all(desc for _, desc in cls.PASS_CRITERIA)


@pytest.mark.parametrize("stem", sorted(MODULES))
def test_plan_is_offline_and_lists_resources(stem):
    """conftest refuses sockets, so any network call in plan() fails this test."""
    items = _spike_class(MODULES[stem])("us-east-1", ACCOUNT).plan()
    assert items and all({"kind", "name"} <= set(i) for i in items)


def test_an_unimplemented_spike_refuses_to_run_without_touching_aws(capsys):
    cls = _spike_class(MODULES["s1_flink_s3tables"])
    assert cls.IMPLEMENTED is False
    assert _spike.main(cls, ["run"]) == 2
    assert "not implemented" in capsys.readouterr().err


def test_recording_an_undeclared_criterion_is_refused():
    s = _spike_class(MODULES["s7_lakehouse_substrate"])("us-east-1", ACCOUNT)
    with pytest.raises(KeyError):
        s.record("made_up_after_the_fact", True)


# ── evidence ─────────────────────────────────────────────────────────────────
def test_evidence_is_redacted_and_names_every_criterion():
    s = _spike_class(MODULES["s7_lakehouse_substrate"])("us-east-1", ACCOUNT)
    s.record("v2", True, f"arn:aws:s3tables:us-east-1:{ACCOUNT}:bucket/x")
    s.observe("role", f"arn:aws:iam::{ACCOUNT}:role/r")
    text = s.render_evidence()
    assert ACCOUNT not in text and "<ACCOUNT_ID>" in text
    assert all(cid in text for cid, _ in s.PASS_CRITERIA)
    assert "NOT RUN" in text and "INCOMPLETE" in text


def test_the_verdict_is_pass_only_when_every_criterion_passed():
    s = _spike_class(MODULES["s6_vpc_harness"])("us-east-1")
    for cid, _ in s.PASS_CRITERIA:
        s.record(cid, True)
    assert "**Verdict:** PASS" in s.render_evidence()
    s.record(s.PASS_CRITERIA[0][0], False, "no")
    assert "**Verdict:** FAIL" in s.render_evidence()


def test_redact_leaves_shorter_and_longer_digit_runs_alone():
    assert _spike.redact("id 12345678901 x") == "id 12345678901 x"
    thirteen = ACCOUNT + "3"  # built at runtime: a literal would add runs to the census
    assert _spike.redact(f"x {thirteen} y") == f"x {thirteen} y"
    assert _spike.redact(f"a {ACCOUNT} b") == "a <ACCOUNT_ID> b"


# ── teardown symmetry ────────────────────────────────────────────────────────
def test_teardown_deletes_newest_first_and_survives_a_failing_deleter():
    s = _spike.Spike("us-east-1")
    order = []
    s.track("a", "1", lambda: order.append("a"))
    s.track("b", "2", lambda: (_ for _ in ()).throw(RuntimeError("stuck")))
    s.track("c", "3", lambda: order.append("c"))
    problems = s.teardown()
    assert order == ["c", "a"] and len(problems) == 1 and not s.created


# ── S7 against fake AWS ──────────────────────────────────────────────────────
s7 = MODULES["s7_lakehouse_substrate"]


class _NotFound(Exception):
    pass


class _Fake:
    def __init__(self, world, service):
        self.w, self.svc = world, service
        self.exceptions = type("E", (), {"EntityNotFoundException": _NotFound})

    def __getattr__(self, op):
        def call(**kw):
            self.w.calls.append((self.svc, op, kw))
            if self.w.crash_on == (self.svc, op):
                raise RuntimeError("injected crash")
            return getattr(self.w, f"{self.svc}_{op}", lambda **_: {})(**kw)
        return call

    def get_paginator(self, op):
        w = self.w
        return type("P", (), {"paginate": lambda _self, **kw: [w.s3_list(**kw)]})()


class FakeWorld:
    """Just enough of S3 Tables / S3 / Glue / Athena / IAM for S7's run() to traverse."""

    def __init__(self, integration=True, crash_on=None):
        self.calls, self.integration, self.crash_on = [], integration, crash_on
        self.objects, self.written, self.queries = {}, False, 0

    def client(self, service, region_name=None):
        return _Fake(self, service)

    def sts_get_caller_identity(self, **_):
        return {"Account": ACCOUNT}

    def ssm_get_parameter(self, **_):
        return {"Parameter": {"Value": "data-bucket"}}

    def s3tables_create_table_bucket(self, **kw):
        return {"arn": f"arn:aws:s3tables:us-east-1:{ACCOUNT}:bucket/{kw['name']}"}

    def s3tables_get_table_metadata_location(self, **_):
        return {"metadataLocation": "s3://wh--table-s3/metadata/m.json"}

    def s3_get_object(self, Bucket, Key):
        if Bucket == "wh--table-s3":
            meta = {"format-version": 2}
            if self.written:
                meta["current-snapshot-id"] = 4242
            body = json.dumps(meta).encode()
        else:
            body = self.objects[Key]
        return {"Body": type("B", (), {"read": lambda _s: body})()}

    def s3_list(self, Bucket, Prefix):
        return {"Contents": [{"Key": k} for k in sorted(self.objects) if k.startswith(Prefix)]}

    def glue_get_catalog(self, **_):
        if not self.integration:
            raise _NotFound("no s3tablescatalog")
        return {}

    def glue_start_job_run(self, **_):
        self.written = True
        return {"JobRunId": "jr_1"}

    def glue_get_job_run(self, **_):
        return {"JobRun": {"JobRunState": "SUCCEEDED"}}

    def iam_create_role(self, **kw):
        return {"Role": {"Arn": f"arn:aws:iam::{ACCOUNT}:role/{kw['RoleName']}"}}

    def athena_start_query_execution(self, **kw):
        self.queries += 1
        dest = kw["QueryString"].split("TO 's3://data-bucket/")[1].split("'")[0]
        if not any(k.startswith(dest) for k in self.objects):
            rows = [json.dumps({"id": i}).encode() for i in range(s7.ROWS)]
            half = len(rows) // 2  # two files, different split per leg: the digest must not care
            cut = half if "unload-a" in dest else half + 7
            self.objects[dest + "p1.gz"] = gzip.compress(b"\n".join(rows[:cut]))
            self.objects[dest + "p2.gz"] = gzip.compress(b"\n".join(rows[cut:]))
            self._state = "SUCCEEDED"
        else:
            self._state = "FAILED"
        return {"QueryExecutionId": f"q{self.queries}"}

    def athena_get_query_execution(self, **_):
        return {"QueryExecution": {"Status": {"State": self._state,
                                              "StateChangeReason": "prefix not empty"}}}


def _deletes(world):
    return [(svc, op) for svc, op, _ in world.calls if op.startswith("delete")]


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(s7.time, "sleep", lambda _s: None)


def test_s7_passes_every_substrate_criterion_and_deletes_everything(no_sleep):
    world = FakeWorld()
    spike = s7.S7LakehouseSubstrate("us-east-1", session=world)
    spike.execute()
    done = {c: v.status for c, v in spike.criteria.items()}
    for cid in ("v2", "integration", "glue_write", "snapshot", "unload_det",
                "unload_count", "unload_nonempty"):
        assert done[cid] == _spike.PASS, (cid, spike.criteria[cid].detail, spike.errors)
    assert all(done[c] == _spike.NOT_RUN for c in ("smus_visible", "smus_lineage", "smus_price"))
    assert not spike.created, "every tracked resource must have been torn down"
    deleted = _deletes(world)
    for expected in (("athena", "delete_work_group"), ("glue", "delete_job"),
                     ("iam", "delete_role"), ("s3tables", "delete_table"),
                     ("s3tables", "delete_namespace"), ("s3tables", "delete_table_bucket"),
                     ("s3", "delete_objects")):
        assert expected in deleted, expected
    assert deleted.index(("s3tables", "delete_table")) < deleted.index(("s3tables", "delete_table_bucket"))


def test_s7_crash_mid_run_still_tears_down_what_exists(no_sleep):
    world = FakeWorld(crash_on=("glue", "create_job"))
    spike = s7.S7LakehouseSubstrate("us-east-1", session=world)
    spike.execute()
    assert any("injected crash" in e for e in spike.errors)
    assert not spike.created
    assert ("s3tables", "delete_table_bucket") in _deletes(world)
    assert ("iam", "delete_role") in _deletes(world)


def test_s7_stops_without_enabling_the_account_level_integration(no_sleep):
    world = FakeWorld(integration=False)
    spike = s7.S7LakehouseSubstrate("us-east-1", session=world)
    spike.execute()
    assert spike.criteria["integration"].status == _spike.FAIL
    assert not any(svc == "glue" and op == "create_job" for svc, op, _ in world.calls)
    assert not spike.created


# ── S7 pure helpers ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("bad", ["42", 4.2, True, None])
def test_unload_sql_refuses_a_non_int_snapshot(bad):
    with pytest.raises(TypeError):
        s7.unload_sql(bad, "s3://b/p/")


@pytest.mark.parametrize("dest", ["b/p/", "s3://b/p'; DROP TABLE x; --"])
def test_unload_sql_refuses_a_bad_destination(dest):
    with pytest.raises(ValueError):
        s7.unload_sql(1, dest)


def test_unload_sql_pins_the_snapshot_and_orders_rows():
    sql = s7.unload_sql(4242, "s3://b/p/")
    assert "FOR VERSION AS OF 4242" in sql and "ORDER BY id" in sql and "format = 'JSON'" in sql


def test_content_digest_ignores_file_splits_and_gzip_headers():
    rows = [f'{{"id": {i}}}'.encode() for i in range(10)]
    a = s7.content_digest([gzip.compress(b"\n".join(rows[:3]), mtime=1),
                           gzip.compress(b"\n".join(rows[3:]), mtime=1)])
    b = s7.content_digest([b"\n".join(reversed(rows[5:])),
                           gzip.compress(b"\n".join(rows[:5]), mtime=99)])
    assert a == b and a[1] == 10


def test_glue_role_policy_is_resource_scoped_and_iam_only():
    pol = s7.glue_role_policy("us-east-1", ACCOUNT,
                              f"arn:aws:s3tables:us-east-1:{ACCOUNT}:bucket/{s7.BUCKET}", "data-bucket")
    for st in pol["Statement"]:
        res = st["Resource"] if isinstance(st["Resource"], list) else [st["Resource"]]
        assert all(r != "*" for r in res), st["Sid"]
        assert not any(a.startswith("lakeformation:") for a in st["Action"]), \
            "IAM-only is the hypothesis under test; a Lake Formation grant would hide the answer"
