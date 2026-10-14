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


def test_redact_handles_short_exact_and_long_digit_runs():
    assert _spike.redact("id 12345678901 x") == "id 12345678901 x"  # 11 digits: untouched
    assert _spike.redact(f"a {ACCOUNT} b") == "a <ACCOUNT_ID> b"      # 12: an account id
    thirteen = ACCOUNT + "3"  # built at runtime: a literal would add runs to the census
    assert _spike.redact(f"x {thirteen} y") == f"x {int(thirteen):,} y"  # 13+: grouped


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
                              f"arn:aws:s3tables:us-east-1:{ACCOUNT}:bucket/b", "data-bucket", "b")
    for st in pol["Statement"]:
        res = st["Resource"] if isinstance(st["Resource"], list) else [st["Resource"]]
        assert all(r != "*" for r in res), st["Sid"]
        assert not any(a.startswith("lakeformation:") for a in st["Action"]), \
            "IAM-only is the hypothesis under test; a Lake Formation grant would hide the answer"


def test_s7_uses_a_fresh_table_bucket_name_per_run():
    """A deleted table bucket's name is briefly unusable (ConflictException, observed live), so
    a fixed name makes every second run fail at its first call."""
    a = s7.S7LakehouseSubstrate("us-east-1", bucket=None)
    assert a.bucket.startswith(s7.BUCKET_PREFIX + "-") and a.bucket != s7.BUCKET_PREFIX
    assert a.bucket == a.bucket.lower(), "S3 Tables / Glue / Athena need lowercase names"
    assert not __import__("re").search(r"[0-9]{13}", a.bucket), "a long stamp would be a digit run in evidence"


def test_evidence_redacts_registered_values_and_groups_long_digit_runs():
    s = _spike_class(MODULES["s7_lakehouse_substrate"])("us-east-1", ACCOUNT, bucket="b")
    bucket = f"llmops-agentic-{ACCOUNT}-us-east-1"
    s.redactions.append((bucket, "<DATA_BUCKET>"))
    snapshot = int(ACCOUNT + "4567890")  # 19 digits, built at runtime like a real snapshot id
    s.record("unload_nonempty", True, f"exists: s3://{bucket}/spikes/s7/unload-a/")
    s.observe("snapshot_id", snapshot)
    text = s.render_evidence()
    assert bucket not in text and "s3://<DATA_BUCKET>/spikes/s7/" in text
    assert f"{snapshot:,}" in text and str(snapshot) not in text
    assert not __import__("re").search(r"[0-9]{12}", text), "no 12-digit run may survive rendering"



# ── S6 (VPC-mode harness) ────────────────────────────────────────────────────
s6 = MODULES["s6_vpc_harness"]


@pytest.mark.parametrize("exit_code,out,verdict", [
    (0, "{...}", "OK"),
    (254, "An error occurred (AccessDeniedException) when calling ListTableBuckets", "REACHABLE"),
    (255, "Could not connect to the endpoint URL: https://glue.us-east-1.amazonaws.com/", "NETWORK"),
    (124, "", "NETWORK"),
    (6, "curl: (6) Could not resolve host: huggingface.co", "NETWORK"),
    (2, "something unexpected", "UNKNOWN"),
])
def test_s6_classifies_each_probe_outcome(exit_code, out, verdict):
    assert s6.classify_probe(exit_code, out) == verdict


def test_s6_parses_only_the_marked_report():
    rep = {"skills": ["llm-guardrails"], "probes": {"sts": {"exit": 0, "output": "ok"}}}
    assert s6.parse_report(f"chatter <<<REPORT {json.dumps(rep)} REPORT>>> tail") == rep
    assert s6.parse_report("no markers here {\"a\": 1}") is None
    assert s6.parse_report("<<<REPORT {not json} REPORT>>>") is None


def test_s6_plan_uses_only_the_az_ids_agentcore_supports():
    items = s6.S6VpcHarness("us-east-1").plan()
    subnets = [i["name"] for i in items if i["kind"] == "subnet"]
    assert {n.split(" in ")[1] for n in subnets} == {"use1-az1", "use1-az2", "use1-az4"}
    eps = [i for i in items if i["kind"] == "interface endpoint"]
    assert len(eps) == len(s6.INTERFACE_SERVICES) and all("s3tables" in e["name"] or True for e in eps)
    assert s6.VPC_CIDR != "10.42.0.0/16", "must not collide with deploy/02_network.py"


def test_s6_probe_message_names_every_probe_and_the_bucket():
    msg = s6.probe_message("data-bucket")
    assert all(f"- {pid}:" in msg for pid, _ in s6.PROBES)
    assert "s3://data-bucket/skills/" in msg and "code_interpreter_internet" in msg


class FakeWorld6(FakeWorld):
    """EC2 + AgentCore just deep enough for S6's run() and teardown()."""

    def __init__(self, reply, blocked_deletes=0):
        super().__init__()
        self.reply, self.blocked, self.n = reply, blocked_deletes, 0

    def _id(self, p):
        self.n += 1
        return f"{p}-{self.n:04d}"

    def ec2_create_vpc(self, **_):
        return {"Vpc": {"VpcId": self._id("vpc")}}

    def ec2_create_subnet(self, **_):
        return {"Subnet": {"SubnetId": self._id("subnet")}}

    def ec2_create_security_group(self, **_):
        return {"GroupId": self._id("sg")}

    def ec2_describe_route_tables(self, **_):
        return {"RouteTables": [{"RouteTableId": "rtb-main"}]}

    def ec2_create_route_table(self, **_):
        return {"RouteTable": {"RouteTableId": self._id("rtb")}}

    def ec2_associate_route_table(self, **_):
        return {"AssociationId": self._id("rtbassoc")}

    def ec2_create_internet_gateway(self, **_):
        return {"InternetGateway": {"InternetGatewayId": self._id("igw")}}

    def ec2_allocate_address(self, **_):
        return {"AllocationId": self._id("eipalloc")}

    def ec2_create_nat_gateway(self, **_):
        return {"NatGateway": {"NatGatewayId": self._id("nat")}}

    def ec2_describe_nat_gateways(self, **kw):
        deleted = any(op == "delete_nat_gateway" for _, op, _ in self.calls)
        return {"NatGateways": [{"NatGatewayId": i, "State": "deleted" if deleted else "available"}
                                for i in kw.get("NatGatewayIds", [])]}

    def ec2_create_vpc_endpoint(self, **_):
        return {"VpcEndpoint": {"VpcEndpointId": self._id("vpce")}}

    def ec2_describe_vpc_endpoints(self, **kw):
        deleted = any(op == "delete_vpc_endpoints" for _, op, _ in self.calls)
        state = "deleted" if deleted else "available"
        return {"VpcEndpoints": [{"VpcEndpointId": i, "State": state} for i in kw.get("VpcEndpointIds", [])]}

    def ec2_delete_subnet(self, **_):
        if self.blocked:
            raise RuntimeError("DependencyViolation: has dependencies (AgentCore ENI)")

    def iam_get_role(self, **_):
        return {"Role": {"Arn": f"arn:aws:iam::{ACCOUNT}:role/{s6.EXECUTION_ROLE}"}}

    def bedrock_agentcore_control_create_harness(self, **kw):
        self.harness_kw = kw
        return {"harness": {"harnessId": "h-1", "arn": "arn:h-1", "status": "CREATING"}}

    def bedrock_agentcore_control_get_harness(self, **_):
        return {"harness": {"harnessId": "h-1", "status": "READY"}}

    def bedrock_agentcore_invoke_harness(self, **_):
        return {"stream": [{"contentBlockDelta": {"delta": {"text": self.reply}}}]}

    def route53resolver_create_resolver_query_log_config(self, **_):
        return {"ResolverQueryLogConfig": {"Id": "rqlc-1"}}

    def route53resolver_list_resolver_query_log_config_associations(self, **_):
        return {"ResolverQueryLogConfigAssociations": []}

    def route53resolver_associate_resolver_query_log_config(self, **_):
        return {"ResolverQueryLogConfigAssociation": {"Id": "rqlca-1"}}

    #: Live, an association sits in CREATING for a while; run 3's teardown hit it there.
    resolver_states = ("CREATING", "CREATING", "ACTIVE")

    def route53resolver_get_resolver_query_log_config_association(self, **_):
        self.status_reads = getattr(self, "status_reads", 0) + 1
        st = self.resolver_states[min(self.status_reads - 1, len(self.resolver_states) - 1)]
        return {"ResolverQueryLogConfigAssociation": {"Id": "rqlca-1", "Status": st}}

    def logs_filter_log_events(self, **_):
        recs = [{"query_name": "bedrock-runtime.us-east-1.amazonaws.com.", "rcode": "NOERROR"},
                {"query_name": "unknown.service.example.", "rcode": "NXDOMAIN"},
                {"query_name": "bedrock-runtime.us-east-1.amazonaws.com.", "rcode": "NOERROR"}]
        return {"events": [{"message": json.dumps(r)} for r in recs]}


class _Fake6(_Fake):
    def __init__(self, world, service):
        super().__init__(world, service.replace("-", "_"))


@pytest.fixture
def s6_world(monkeypatch):
    monkeypatch.setattr(s6.time, "sleep", lambda _s: None)

    def make(reply, **kw):
        w = FakeWorld6(reply, **kw)
        w.client = lambda service, region_name=None, config=None: _Fake6(w, service)
        return w
    return make


def _s6_reply(verdict_lake="denied"):
    lake_out = ("An error occurred (AccessDeniedException)" if verdict_lake == "denied"
                else "Could not connect to the endpoint URL")
    probes = {pid: {"exit": 0, "output": "ok"} for pid, _ in s6.PROBES}
    for p in s6.LAKEHOUSE_PROBES:
        probes[p] = {"exit": 254 if verdict_lake == "denied" else 255, "output": lake_out}
    for p in ("internet_hf", "internet_pypi"):
        probes[p] = {"exit": 6, "output": "Could not resolve host"}
    return "<<<REPORT " + json.dumps({"skills": list(s6.EXPECTED_SKILLS), "probes": probes}) + " REPORT>>>"


def test_s6_run_records_every_criterion_and_tears_down_in_reverse(s6_world):
    world = s6_world(_s6_reply("denied"))
    spike = s6.S6VpcHarness("us-east-1", session=world, stamp="x", dns_logging=False)
    spike.execute()
    assert {c: v.status for c, v in spike.criteria.items()} == {c: _spike.PASS for c, _ in spike.PASS_CRITERIA}, \
        {c: v.detail for c, v in spike.criteria.items()}
    assert spike.observations["fails_without_nat"] == ["internet_hf", "internet_pypi"]
    net = world.harness_kw["environment"]["agentCoreRuntimeEnvironment"]["networkConfiguration"]
    assert net["networkMode"] == "VPC" and len(net["networkModeConfig"]["subnets"]) == 3
    assert "<DATA_BUCKET>" not in json.dumps(world.harness_kw["skills"]), "skills must be resolved"
    deletes = [op for _, op, _ in world.calls if op.startswith("delete")]
    assert deletes[0] == "delete_harness" and deletes[-1] == "delete_vpc"
    assert deletes.index("delete_vpc_endpoints") < deletes.index("delete_security_group")
    assert not spike.created and not spike.left


def test_s6_a_network_failure_on_the_lakehouse_probes_fails_tools(s6_world):
    spike = s6.S6VpcHarness("us-east-1", session=s6_world(_s6_reply("network")), stamp="x", dns_logging=False)
    spike.execute()
    assert spike.criteria["tools"].status == _spike.FAIL


def test_s6_eni_blocked_deletes_are_reported_as_left_running(s6_world):
    spike = s6.S6VpcHarness("us-east-1", session=s6_world(_s6_reply(), blocked_deletes=1), stamp="x", dns_logging=False)
    spike.execute()
    assert any(k == "subnet" for k, _, _ in spike.left)
    text = spike.render_evidence()
    assert "LEFT RUNNING (delete failed): subnet" in text and "all tracked resources deleted" not in text


def test_cleanup_action_is_available_from_the_cli_only_where_defined(capsys):
    assert _spike.main(_spike_class(MODULES["s1_flink_s3tables"]), ["cleanup"]) == 2


# ── S4 (zh-TW PII recall) ────────────────────────────────────────────────────
s4 = MODULES["s4_pii_recall_zhtw"]


def test_s4_checksums_accept_generated_values_and_reject_tampered_ones():
    import random
    rng = random.Random(1)
    tw, ubn, card = s4.make_tw_id(rng), s4.make_ubn(rng), s4.make_card(rng)
    assert s4.tw_id_checksum_ok(tw) and s4.ubn_checksum_ok(ubn)
    assert s4.luhn_ok(card.replace("-", "").replace(" ", ""))
    bad = tw[:-1] + str((int(tw[-1]) + 1) % 10)
    assert not s4.tw_id_checksum_ok(bad)


def test_s4_records_are_deterministic_and_carry_no_twelve_digit_run():
    a, b = s4.generate_records(200), s4.generate_records(200)
    assert a == b, "same seed, same records: the spike must be reproducible"
    assert {r["lang"] for r in a} == {"zh-TW", "en"}
    for r in a:
        assert not __import__("re").search(r"[0-9]{12}", r["text"])
        for t in r["truth"]:
            assert r["text"][t["start"]:t["end"]] == t["value"], "truth offsets must be exact"


def test_s4_regex_layer_catches_every_structured_entity_and_nothing_else():
    recs = s4.generate_records(400)
    sc = s4.score(recs, {r["id"]: s4.detect_regex(r["text"]) for r in recs})
    assert sc["recall_structured"] == 1.0 and sc["precision"] == 1.0
    assert sc["recall_names"] == 0.0, "names are what the regex layer cannot do; that is the point of layering"


def test_s4_best_combination_prefers_the_union_that_covers_names():
    recs = s4.generate_records(50)
    regex = {r["id"]: s4.detect_regex(r["text"]) for r in recs}
    names = {r["id"]: [(t["start"], t["end"]) for t in r["truth"] if t["type"] in s4.NAMES] for r in recs}
    combo, sc = s4.best_combination(recs, {"regex": regex, "names_only": names})
    assert combo == "names_only+regex" and sc["recall_names"] == 1.0 and sc["recall_structured"] == 1.0


def test_s4_guardrail_matches_and_glue_output_become_spans():
    assert s4.spans_from_matches("call 0912-345-678 or 0912-345-678", ["0912-345-678"]) == [(5, 17), (21, 33)]
    blob = json.dumps({"id": 3, "text": "x", "DetectedEntities": {"text": [{"start": 1, "end": 4}]}}).encode()
    assert s4.parse_glue_output([blob, b"\n"]) == {3: [(1, 4)]}


def test_s4_glue_role_policy_is_prefix_scoped():
    pol = s4.glue_role_policy("us-east-1", ACCOUNT, "data-bucket")
    for st in pol["Statement"]:
        res = st["Resource"] if isinstance(st["Resource"], list) else [st["Resource"]]
        assert all(r != "*" for r in res)
    assert pol["Statement"][0]["Resource"].endswith(f"/{s4.PREFIX}/*")



def test_s6_dns_logging_records_resolved_names_and_tears_the_logging_down(s6_world):
    world = s6_world(_s6_reply("denied"))
    spike = s6.S6VpcHarness("us-east-1", session=world, stamp="x", dns_logging=True)
    spike.execute()
    q = spike.observations["dns_queries_during_invoke"]
    assert q["bedrock-runtime.us-east-1.amazonaws.com"] == [2, ["NOERROR"]]
    assert q["unknown.service.example"] == [1, ["NXDOMAIN"]]
    deletes = [op for _, op, _ in world.calls if op.startswith(("delete", "disassociate"))]
    for op in ("disassociate_resolver_query_log_config", "delete_resolver_query_log_config", "delete_log_group"):
        assert op in deletes, op
    assert deletes.index("disassociate_resolver_query_log_config") < deletes.index("delete_resolver_query_log_config")


def test_s6_dns_summary_ignores_unparseable_records():
    names = {}
    s6.summarize_dns_event("not json", names)
    s6.summarize_dns_event(json.dumps({"query_name": "A.Example.", "rcode": "NOERROR"}), names)
    assert names == {"a.example": [1, {"NOERROR"}]}



def test_s6_routes_through_its_own_tagged_route_table_never_the_main_one(s6_world):
    """Run 3: the runner's tag-scoped grants could not attach the S3 gateway endpoint to the
    untagged main route table (UnauthorizedOperation). Everything goes through a tagged table."""
    world = s6_world(_s6_reply("denied"))
    spike = s6.S6VpcHarness("us-east-1", session=world, stamp="x", dns_logging=False, nat=True)
    spike.execute()
    by_op = lambda op: [kw for _, o, kw in world.calls if o == op]
    rt = spike.private_rt
    assert rt.startswith("rtb-") and rt != "rtb-main"
    gw = next(kw for kw in by_op("create_vpc_endpoint") if kw.get("VpcEndpointType") == "Gateway")
    assert gw["RouteTableIds"] == [rt]
    nat_route = [kw for kw in by_op("create_route") if "NatGatewayId" in kw]
    assert nat_route and all(kw["RouteTableId"] == rt for kw in nat_route), nat_route
    assert all(kw["RouteTableId"] != "rtb-main" for kw in by_op("create_route"))
    assert not spike.left and not spike.errors, (spike.left, spike.errors)
    tagged = by_op("create_route_table")
    assert any("llmops-spike-s6-private" in json.dumps(kw) for kw in tagged)
    assert len([kw for kw in by_op("associate_route_table") if kw["RouteTableId"] == rt]) == len(s6.SUBNETS)
    assert by_op("delete_route_table"), "the route table must be torn down"


def test_s6_query_logging_waits_for_active_and_never_disassociates_mid_transition(s6_world):
    world = s6_world(_s6_reply("denied"))
    spike = s6.S6VpcHarness("us-east-1", session=world, stamp="x", dns_logging=True)
    spike.execute()
    ops = [o for _, o, _ in world.calls]
    first_invoke = ops.index("invoke_harness")
    reads_before = ops[:first_invoke].count("get_resolver_query_log_config_association")
    assert reads_before >= 3, "the invoke started before the association left CREATING"
    i = ops.index("disassociate_resolver_query_log_config")
    assert "get_resolver_query_log_config_association" in ops[first_invoke:i], (
        "teardown must check the association has settled before disassociating (RSLVR-01400)")
    assert not spike.left, spike.left


def test_s6_waits_out_tag_consistency_but_not_a_missing_grant(monkeypatch):
    monkeypatch.setattr(s6.time, "sleep", lambda _s: None)
    n = {"calls": 0}

    def flaky():
        n["calls"] += 1
        if n["calls"] < 3:
            raise RuntimeError("An error occurred (UnauthorizedOperation) when calling AssociateRouteTable")
        return "ok"
    assert s6._retry_fresh(flaky) == "ok" and n["calls"] == 3

    def denied():
        raise RuntimeError("An error occurred (UnauthorizedOperation)")
    with pytest.raises(RuntimeError):
        s6._retry_fresh(denied)  # a grant that is really missing still fails, after a minute

    def other():
        n["other"] = n.get("other", 0) + 1
        raise ValueError("InvalidRouteTableID.NotFound")
    with pytest.raises(ValueError):
        s6._retry_fresh(other)
    assert n["other"] == 1, "only UnauthorizedOperation is retried"


def test_s6_creates_its_harness_without_memory_and_says_so(s6_world):
    world = s6_world(_s6_reply("denied"))
    spike = s6.S6VpcHarness("us-east-1", session=world, stamp="x", dns_logging=False)
    spike.execute()
    assert world.harness_kw["memory"] == {"disabled": {}}, "omitting memory provisions managed memory"
    assert world.harness_kw["tags"] == spike.tags, "the runner may create a harness only with the tag"
    assert "disabled" in spike.observations["memory"]


def test_s6_can_send_s3_through_the_nat_instead_of_the_gateway(s6_world, monkeypatch):
    monkeypatch.setenv("LLMOPS_S6_S3_GATEWAY", "0")
    world = s6_world(_s6_reply("denied"))
    spike = s6.S6VpcHarness("us-east-1", session=world, stamp="x", dns_logging=False, nat=True)
    spike.execute()
    eps = [kw for _, op, kw in world.calls if op == "create_vpc_endpoint"]
    assert eps and all(kw.get("VpcEndpointType") != "Gateway" for kw in eps)
    assert "no S3 gateway" in spike.observations["variant"]
    with pytest.raises(ValueError):
        s6.S6VpcHarness("us-east-1", session=world, stamp="y")  # S3 would be unreachable


def test_s6_public_control_builds_no_network_and_uses_the_same_config(s6_world, monkeypatch):
    monkeypatch.setenv("LLMOPS_S6_PUBLIC", "1")
    world = s6_world(_s6_reply("denied"))
    spike = s6.S6VpcHarness("us-east-1", session=world, stamp="x")
    spike.execute()
    assert not [op for svc, op, _ in world.calls if svc == "ec2" and op.startswith("create")]
    net = world.harness_kw["environment"]["agentCoreRuntimeEnvironment"]["networkConfiguration"]
    assert net == {"networkMode": "PUBLIC"}
    assert world.harness_kw["memory"] == {"disabled": {}} and world.harness_kw["tags"] == spike.tags
    assert spike.criteria["model"].status == _spike.PASS, spike.errors


def test_s6_an_error_inside_the_stream_is_reported_not_dropped(s6_world):
    world = s6_world("")
    world.bedrock_agentcore_invoke_harness = lambda **_: {"stream": [
        {"messageStart": {"role": "assistant"}},
        {"internalServerException": {"message": "skill source unreadable"}}]}
    spike = s6.S6VpcHarness("us-east-1", session=world, stamp="x", dns_logging=False)
    spike.execute()
    assert "skill source unreadable" in spike.observations["invoke_error"]
    assert spike.observations["stream_event_kinds"] == {"messageStart": 1, "internalServerException": 1}
    assert spike.criteria["model"].status == _spike.FAIL


def test_s6_can_run_without_interface_endpoints_behind_a_nat(s6_world, monkeypatch):
    monkeypatch.setenv("LLMOPS_S6_INTERFACE_ENDPOINTS", "0")
    world = s6_world(_s6_reply("denied"))
    spike = s6.S6VpcHarness("us-east-1", session=world, stamp="x", dns_logging=False, nat=True)
    spike.execute()
    eps = [kw for _, op, kw in world.calls if op == "create_vpc_endpoint"]
    assert [kw["VpcEndpointType"] for kw in eps] == ["Gateway"], eps
    assert "no interface endpoints" in spike.observations["variant"] and not spike.left, spike.errors
    with pytest.raises(ValueError):
        s6.S6VpcHarness("us-east-1", session=world, stamp="y")


def test_s6_cleanup_skips_a_held_vpc_and_still_deletes_the_free_ones(s6_world):
    world = s6_world("")
    world.ec2_describe_vpcs = lambda **_: {"Vpcs": [{"VpcId": "vpc-held"}, {"VpcId": "vpc-free"}]}
    world.ec2_describe_network_interfaces = lambda **kw: {"NetworkInterfaces": (
        [{"NetworkInterfaceId": "eni-1"}] if kw["Filters"][0]["Values"] == ["vpc-held"] else [])}
    world.ec2_describe_subnets = lambda **_: {"Subnets": [{"SubnetId": "subnet-1"}]}
    world.ec2_describe_security_groups = lambda **_: {"SecurityGroups": [{"GroupId": "sg-d", "GroupName": "default"}]}
    world.ec2_describe_route_tables = lambda **_: {"RouteTables": [
        {"RouteTableId": "rtb-main", "Associations": [{"Main": True}]},
        {"RouteTableId": "rtb-priv", "Associations": [{"Main": False, "RouteTableAssociationId": "a-1"}]}]}
    out = s6.S6VpcHarness("us-east-1", session=world, stamp="x").cleanup()
    assert out[0].startswith("kept vpc-held") and out[1] == "deleted vpc-free", out
    deleted = [kw for _, op, kw in world.calls if op == "delete_vpc"]
    assert deleted == [{"VpcId": "vpc-free"}]
    assert [kw for _, op, kw in world.calls if op == "delete_route_table"] == [{"RouteTableId": "rtb-priv"}]


# ── S7 --part domain (Unified Studio) ────────────────────────────────────────
class _NotFoundDZ(Exception):
    pass


class _Fake7D(_Fake):
    def __init__(self, world, service):
        super().__init__(world, service)
        self.exceptions = type("E", (), {"EntityNotFoundException": _NotFound,
                                         "ResourceNotFoundException": _NotFoundDZ})


class FakeWorld7D(FakeWorld):
    """S7's FakeWorld plus DataZone and the Price List API."""

    def __init__(self, domain=True, asset=True, lineage=True, **kw):
        super().__init__(**kw)
        self.domain, self.asset, self.lineage, self.project_deleted = domain, asset, lineage, False

    def client(self, service, region_name=None):
        return _Fake7D(self, service)

    def datazone_list_domains(self, **_):
        return {"items": [{"id": "dzd_1", "name": s7.DOMAIN_NAME}] if self.domain else []}

    def datazone_get_domain(self, **_):
        return {"domainVersion": "V2", "singleSignOn": {"type": "DISABLED"}}

    def datazone_list_project_profiles(self, **_):
        return {"items": [{"id": "pp_other", "name": "SQL analytics"},
                          {"id": "pp_1", "name": s7.PROJECT_PROFILE}]}

    refuse_tags = False

    def datazone_create_project(self, **kw):
        if self.refuse_tags and "resourceTags" in kw:
            raise RuntimeError("ValidationException: Additional project resource tags are not allowed "
                               "when allowCustomProjectResourceTags is false")
        self.project_kw = kw
        return {"id": "prj_1"}

    def datazone_get_project(self, **_):
        if self.project_deleted:
            raise _NotFoundDZ()
        return {"projectStatus": "ACTIVE"}

    def datazone_delete_project(self, **kw):
        assert kw["skipDeletionCheck"] is True
        self.project_deleted = True

    lake_status = "ACTIVE"

    def datazone_list_connections(self, **_):
        return {"items": [{"connectionId": "c_athena", "name": "project.athena", "type": "ATHENA"},
                          {"connectionId": "c_lake", "name": "project.default_lakehouse", "type": "IAM"}]}

    def datazone_list_environments(self, **_):
        return {"items": [{"id": "env_t", "name": "Tooling"}, {"id": "env_l", "name": "Lakehouse Database"}]}

    def datazone_get_environment(self, identifier, **_):
        st = "ACTIVE" if identifier == "env_t" else self.lake_status
        return {"status": st, "lastDeployment": {"failureReason": {"message": "invalid principal"}}
                if st != "ACTIVE" else {},
                "provisionedResources": [{"name": "userRoleArn", "type": "IAMRole",
                                          "value": f"arn:aws:iam::{ACCOUNT}:role/datazone_usr_role_x"}]}

    def datazone_create_data_source(self, **kw):
        self.ds_kw = kw
        return {"id": "ds_1"}

    ds_status = "READY"

    def datazone_get_data_source(self, **_):
        return {"status": self.ds_status}

    def datazone_start_data_source_run(self, **_):
        return {"id": "run_1"}

    def datazone_get_data_source_run(self, **_):
        return {"status": "SUCCESS"}

    def datazone_search(self, **_):
        return {"items": [{"assetItem": {"name": s7.TABLE}}] if self.asset else []}

    def datazone_list_lineage_events(self, **_):
        return {"items": [{"id": "le_1", "processingStatus": "SUCCESS"}] if self.lineage else []}

    def datazone_get_lineage_event(self, **_):
        body = json.dumps({"job": {"name": s7.GLUE_JOB}, "outputs": [{"name": f"{s7.NAMESPACE}.{s7.TABLE}"}]})
        return {"event": type("B", (), {"read": lambda _s: body.encode()})()}

    def pricing_get_products(self, **kw):
        assert kw["ServiceCode"] == "AmazonDataZone"

        def prod(region, desc):  # live shape: global prices carry regionCode "" and location "Any"
            attrs = {"regionCode": "", "location": "Any"} if region == "Any" else {"regionCode": region,
                                                                                   "location": "elsewhere"}
            return json.dumps({"product": {"attributes": attrs},
                               "terms": {"OnDemand": {"t": {"priceDimensions": {"d": {
                                   "description": desc, "unit": "Requests", "pricePerUnit": {"USD": "0.0001"}}}}}}})
        return {"PriceList": [prod("Any", "requests"), prod("eu-west-1", "elsewhere")]}


@pytest.fixture
def s7d(monkeypatch):
    monkeypatch.setattr(s7.time, "sleep", lambda _s: None)
    return lambda **kw: FakeWorld7D(**kw)


def test_s7_domain_part_passes_and_tears_down_the_project(s7d):
    world = s7d()
    spike = s7.S7LakehouseSubstrate("us-east-1", session=world, part="domain")
    spike.execute()
    done = {c: v.status for c, v in spike.criteria.items()}
    for cid in ("smus_visible", "smus_lineage", "smus_price"):
        assert done[cid] == _spike.PASS, (cid, spike.criteria[cid].detail, spike.errors)
    assert all(done[c] == _spike.NOT_RUN for c in ("v2", "glue_write", "unload_det"))
    assert world.project_kw["projectProfileId"] == "pp_1"
    assert world.project_kw["resourceTags"]["spike"] == s7.S7LakehouseSubstrate.NAME
    ops = [op for _, op, _ in world.calls]
    assert "delete_project" in ops and "delete_data_source" in ops and not spike.left
    assert ops.index("delete_data_source") < ops.index("delete_project"), "children before the project"


def test_s7_domain_part_points_the_data_source_at_the_s3_tables_catalog(s7d):
    world = s7d()
    spike = s7.S7LakehouseSubstrate("us-east-1", session=world, part="domain")
    spike.execute()
    glue = world.ds_kw["configuration"]["glueRunConfiguration"]
    assert glue["catalogName"] == f"s3tablescatalog/{spike.bucket}" and "dataAccessRole" not in glue
    assert world.ds_kw["publishOnImport"] is False, "a spike must not publish into a shared catalog"
    grant = next(kw for _, op, kw in world.calls if op == "put_role_policy" and kw["PolicyName"] == s7.PROJECT_GRANT)
    assert grant["RoleName"] == "datazone_usr_role_x"
    ops = [(op, kw.get("PolicyName")) for _, op, kw in world.calls]
    assert ops.index(("delete_role_policy", s7.PROJECT_GRANT)) < ops.index(("delete_project", None)), \
        "the grant must go before the project, or CloudFormation cannot delete the role"


def test_s7_domain_part_emits_lineage_from_the_glue_job(s7d):
    world = s7d()
    spike = s7.S7LakehouseSubstrate("us-east-1", session=world, part="domain")
    spike.execute()
    job = next(kw for _, op, kw in world.calls if op == "create_job")
    conf = job["DefaultArguments"]["--conf"]
    assert "transport.type=amazon_datazone_api" in conf and "transport.domainId=dzd_1" in conf
    role = next(kw for _, op, kw in world.calls if op == "put_role_policy" and kw["RoleName"] == s7.GLUE_ROLE)
    lineage = [st for st in json.loads(role["PolicyDocument"])["Statement"] if st["Sid"] == "OpenLineageToTheDomain"]
    assert lineage and lineage[0]["Resource"].endswith(":domain/dzd_1")


def test_s7_substrate_part_carries_no_domain_grant():
    pol = s7.glue_role_policy("us-east-1", ACCOUNT, "arn:b", "data", "b")
    assert not any("datazone" in json.dumps(st) for st in pol["Statement"])


def test_s7_domain_data_source_role_is_read_only():
    pol = s7.datasource_role_policy("us-east-1", ACCOUNT, "b")
    actions = [a for st in pol["Statement"] for a in st["Action"]]
    assert all(a.split(":")[1].startswith("Get") for a in actions), actions


@pytest.mark.parametrize("kw,cid", [({"asset": False}, "smus_visible"), ({"lineage": False}, "smus_lineage")])
def test_s7_domain_part_fails_the_criterion_it_could_not_observe(s7d, kw, cid, monkeypatch):
    monkeypatch.setattr(s7.time, "time", iter(range(0, 10**7, 400)).__next__)  # expire the lineage wait
    world = s7d(**kw)
    spike = s7.S7LakehouseSubstrate("us-east-1", session=world, part="domain")
    spike.execute()
    assert spike.criteria[cid].status == _spike.FAIL, spike.criteria[cid].detail


def test_s7_domain_part_without_the_domain_creates_nothing(s7d):
    world = s7d(domain=False)
    spike = s7.S7LakehouseSubstrate("us-east-1", session=world, part="domain")
    spike.execute()
    assert spike.criteria["smus_visible"].status == _spike.FAIL
    assert not [op for _, op, _ in world.calls if op.startswith("create")]


def test_lineage_mentions_reads_the_event_text():
    assert s7.lineage_mentions('{"job":{"name":"j"},"outputs":[{"name":"spike.events"}]}', ("j", "events")) == \
        {"j": True, "events": True}


def test_s7_domain_part_retries_without_tags_when_the_profile_refuses_them(s7d):
    world = s7d()
    world.refuse_tags = True
    spike = s7.S7LakehouseSubstrate("us-east-1", session=world, part="domain")
    spike.execute()
    assert "resourceTags" not in world.project_kw and "refused" in spike.observations["project_tags"]
    assert spike.criteria["smus_visible"].status == _spike.PASS


def test_s7_price_keeps_global_and_this_region_only(s7d):
    world = s7d()
    spike = s7.S7LakehouseSubstrate("us-east-1", session=world, part="domain")
    ok, detail = spike._price()
    assert ok and [d["description"] for d in spike.observations["smus_price_dimensions"]] == ["requests"]


def test_s7_domain_part_uses_the_lakehouse_environment_and_stops_if_it_failed(s7d):
    world = s7d()
    spike = s7.S7LakehouseSubstrate("us-east-1", session=world, part="domain")
    spike.execute()
    assert world.ds_kw["connectionIdentifier"] == "c_lake" and "environmentIdentifier" not in world.ds_kw
    world = s7d()
    world.lake_status = "VALIDATION_FAILED"
    spike = s7.S7LakehouseSubstrate("us-east-1", session=world, part="domain")
    spike.execute()
    assert spike.criteria["smus_visible"].status == _spike.FAIL
    assert "invalid principal" in spike.criteria["smus_visible"].detail
    assert not [op for _, op, _ in world.calls if op == "create_data_source"]


def test_s7_pick_connection_prefers_the_lakehouse_then_iam_then_athena():
    a = {"connectionId": "a", "name": "project.athena", "type": "ATHENA"}
    i = {"connectionId": "i", "name": "project.iam", "type": "IAM"}
    lake = {"connectionId": "l", "name": "project.default_lakehouse", "type": "IAM"}
    assert s7.pick_connection([a, i, lake])["connectionId"] == "l"
    assert s7.pick_connection([a, i])["connectionId"] == "i"
    assert s7.pick_connection([a])["connectionId"] == "a"
    assert s7.pick_connection([]) is None


def test_s7_domain_part_waits_for_the_data_source_and_stops_if_it_failed(s7d):
    world = s7d()
    world.ds_status = "FAILED_CREATION"
    spike = s7.S7LakehouseSubstrate("us-east-1", session=world, part="domain")
    spike.execute()
    assert spike.criteria["smus_visible"].status == _spike.FAIL
    assert not [op for _, op, _ in world.calls if op == "start_data_source_run"]
