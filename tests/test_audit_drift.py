"""Guards for the read-only drift audit.

Two kinds of test live here, and the split is the point.

The first kind drives the tool against fakes built FROM its own sent side. Those tests
answer "does a perfectly deployed system read clean, and does a real difference read as
drift" -- the false-alarm question, which is the one that decides whether anybody keeps
running this tool. They cannot, by construction, catch a sent side that is reconstructed
WRONG: both sides would be wrong together and the test would pass.

So the second kind never touches the fakes. It parses deploy/01_iam.py, deploy/05_harnesses.py
and deploy/07_lambdas.py and asserts the reconstruction still matches what those scripts
actually send. That is where a mis-built sent side gets caught, and a mis-built sent side is
the standard failure of this class of tool: it reports drift on a correctly deployed system,
forever, until somebody switches it off.

Nothing here reaches AWS. conftest.py's autouse fixture would turn any slip into an
AssertionError naming the call.
"""

import json
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))

import audit_drift as ad  # noqa: E402

REGION, ACCOUNT = "us-east-1", "123456789012"
DERIVED_BUCKET = f"llmops-agentic-{ACCOUNT}-{REGION}"


# ── fakes ───────────────────────────────────────────────────────────────────
class ApiError(Exception):
    """A botocore-shaped error: the code lives in response["Error"]["Code"]."""

    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


def specs(bucket=DERIVED_BUCKET):
    return ad.iam_deploy.build_role_specs(
        ad.config_subst.mapping_for(ACCOUNT, REGION, bucket), None)


class FakeIAM:
    def __init__(self, live=None, absent=(), boom=(), no_inline=()):
        self.live = live if live is not None else specs()
        self.absent, self.boom, self.no_inline = set(absent), set(boom), set(no_inline)

    def get_role(self, RoleName):  # noqa: N803 — boto3's own casing
        if RoleName in self.absent:
            raise ApiError("NoSuchEntity")
        if RoleName in self.boom:
            raise ApiError("AccessDenied")
        return {"Role": {"AssumeRolePolicyDocument": self.live[RoleName]["trust"],
                         "Tags": []}}

    def get_role_policy(self, RoleName, PolicyName):  # noqa: N803
        if RoleName in self.no_inline:
            raise ApiError("NoSuchEntity")
        return {"PolicyDocument": self.live[RoleName]["policy"]}


class FakeSFN:
    def __init__(self, definition=None, error=None):
        self.definition = (definition if definition is not None
                           else ad.asl_sent(REGION, ACCOUNT))
        self.error = error

    def describe_state_machine(self, stateMachineArn):  # noqa: N803
        if self.error:
            raise self.error
        return {"definition": self.definition, "revisionId": "r1"}


class FakeSSM:
    def __init__(self, bucket=DERIVED_BUCKET, error=None):
        self.bucket, self.error = bucket, error

    def get_parameter(self, Name):  # noqa: N803
        if Name == "/llmops/storage/bucket":
            if self.error:
                raise self.error
            return {"Parameter": {"Value": self.bucket}}
        if Name.startswith("/llmops/iam/"):
            key = Name.split("/")[-1].replace("_arn", "")
            return {"Parameter": {"Value": f"arn:aws:iam::{ACCOUNT}:role/llmops-{key}"}}
        raise ApiError("ParameterNotFound")


def harness_live(bucket=DERIVED_BUCKET, mutate=None):
    """{harnessName: the config the service would report on a clean deploy}.

    The service adds agentRuntime* keys the deploy never sent; they are included because
    a live harness always has them and `harness_config_drift` is containment for exactly
    that reason.

    Built from `harness_sent`, which makes it a fair double for what the SERVICE reports and
    a blind one for the sent side: both sides move together, so no test using this fixture
    can notice the sent builder changing. Measured -- deleting `ensure_env(cfg)` from
    `harness_sent` leaves every test here green, while the real audit would then report
    environmentVariables drift on all seven harnesses forever. Hence the one literal below:
    the sampler value is restated here rather than derived, so a double that has stopped
    carrying what the deploy actually sets says so instead of agreeing with the mistake.
    """
    mapping = ad.config_subst.mapping_for(ACCOUNT, REGION, bucket)
    out = {}
    for agent in ad.harness_deploy.AGENTS:
        name, sent = ad.harness_sent(agent, mapping)
        live = json.loads(json.dumps(sent))
        assert live.get("environmentVariables", {}).get("OTEL_TRACES_SAMPLER") == "always_on", (
            f"the double for live {name} does not carry the sampler the deploy sets; it is "
            "derived from harness_sent, so this is the sent side having changed")
        env = live.setdefault("environment", {})
        env["agentRuntimeArn"] = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:x/{name}"
        env["agentRuntimeId"] = f"{name}-abc"
        out[name] = live
    if mutate:
        mutate(out)
    return out


class FakeCtl:
    def __init__(self, live=None, error=None, status="READY"):
        self.live = harness_live() if live is None else live
        self.error, self.status = error, status

    def list_harnesses(self):
        if self.error:
            raise self.error
        return {"harnesses": [{"harnessName": n, "harnessId": f"{n}-0123456789"}
                              for n in self.live]}

    def get_harness(self, harnessId):  # noqa: N803
        name = harnessId.rsplit("-", 1)[0]
        return {"harness": {"status": self.status, **self.live[name]}}


def lambda_live(ssm, mutate=None):
    out = {}
    for cfg in ad.lambda_deploy.LAMBDAS.values():
        sent = ad.lambda_sent(ssm, REGION, ACCOUNT, cfg)
        out[cfg["fn"]] = {
            "Runtime": sent["Runtime"], "Handler": sent["Handler"],
            "Timeout": sent["Timeout"], "MemorySize": sent["MemorySize"],
            "Role": sent["Role"],
            "Environment": {"Variables": dict(sent["Environment"])}}
    if mutate:
        mutate(out)
    return out


class FakeLam:
    def __init__(self, ssm=None, live=None, invoke=None, absent=(), mutate=None):
        self.live = lambda_live(ssm or FakeSSM(), mutate) if live is None else live
        self.invoke = invoke if invoke is not None else {
            "MaximumRetryAttempts": ad.ASYNC_RETRIES,
            "MaximumEventAgeInSeconds": ad.ASYNC_MAX_AGE_S}
        self.absent = set(absent)
        self.asked = []

    def get_function_configuration(self, FunctionName):  # noqa: N803
        self.asked.append(("config", FunctionName))
        if FunctionName in self.absent:
            raise ApiError("ResourceNotFoundException")
        return dict(self.live[FunctionName])

    def get_function_event_invoke_config(self, FunctionName):  # noqa: N803
        if FunctionName in self.absent:
            raise ApiError("ResourceNotFoundException")
        return dict(self.invoke)


def clean_members(fn=None):
    """The member map a byte-perfect deploy of any function would serve."""
    return {"handler.py": (ad.lambda_deploy.LAMBDAS[fn]["src"].read_bytes()
                           if fn else b"x"),
            **ad.bundle_members_sent()}


def fetch_clean(lam, fn):
    key = next(k for k, c in ad.lambda_deploy.LAMBDAS.items() if c["fn"] == fn)
    return clean_members(key)


def clients(iam=None, sfn=None, ctl=None, lam=None, ssm=None):
    ssm = ssm or FakeSSM()
    return {"iam": iam or FakeIAM(), "sfn": sfn or FakeSFN(), "ctl": ctl or FakeCtl(),
            "lam": lam or FakeLam(ssm), "ssm": ssm}


def run(monkeypatch, fetch=fetch_clean, **kw):
    monkeypatch.setattr(ad, "_fetch_members", fetch)
    return ad.audit(REGION, ACCOUNT, clients(**kw))


def findings(legs, name=None):
    return [f for lg in legs if name in (None, lg["leg"]) for f in lg["drift"]]


def unanswered(legs, name=None):
    return [u for lg in legs if name in (None, lg["leg"]) for u in lg["unknown"]]


def rc(legs):
    return ad.report(legs, {"sha": "deadbeef", "branch": "main"}, out=lambda *a: None)


# ── the false-alarm question ────────────────────────────────────────────────
def test_a_perfectly_deployed_system_reports_clean(monkeypatch):
    """The test that decides whether anyone keeps running this tool.

    A drift audit that cries wolf on a correct deploy gets switched off by the third
    person it wakes, and then the real drift arrives inside a list of false alarms. Every
    leg here is fed exactly what the deploy would have sent.
    """
    legs = run(monkeypatch)
    assert findings(legs) == [], findings(legs)
    assert unanswered(legs) == [], unanswered(legs)
    assert rc(legs) == 0
    assert [lg["leg"] for lg in legs] == ["iam", "state_machine", "harnesses",
                                          "lambda_config", "lambda_code"]
    assert all(lg["compared"] for lg in legs), "a leg reported clean having compared nothing"


def test_the_keys_the_service_adds_to_a_harness_are_not_drift(monkeypatch):
    """agentRuntimeArn/Id are on every healthy harness and were sent by nobody. Strict
    equality would report drift on all seven forever -- the measured reason
    harness_config_drift is containment (05_harnesses.py:134)."""
    legs = run(monkeypatch, ctl=FakeCtl())
    assert findings(legs, "harnesses") == []


def test_a_stale_live_prompt_is_drift(monkeypatch):
    """The 2026-08-03 failure: live llmops_finops quoted a cost figure main had replaced
    weeks earlier, status READY, version 5, every surface healthy."""
    def stale(live):
        live["llmops_finops"]["systemPrompt"] = "an older prompt entirely"

    legs = run(monkeypatch, ctl=FakeCtl(harness_live(mutate=stale)))
    found = findings(legs, "harnesses")
    assert [f["field"] for f in found] == ["systemPrompt"], found
    assert "llmops_finops" in found[0]["resource"]
    assert rc(legs) == 1


def test_a_harness_that_is_not_ready_is_drift(monkeypatch):
    legs = run(monkeypatch, ctl=FakeCtl(status="UPDATING"))
    assert all("not READY" in f["problem"] for f in findings(legs, "harnesses"))
    assert len(findings(legs, "harnesses")) == len(ad.harness_deploy.AGENTS)


def test_a_state_absent_from_the_live_definition_is_named(monkeypatch):
    """EvalGenerate was merged, green, and simply never deployed; a human found it a day
    later reading the live definition by hand. The finding names the state, not the diff."""
    live = json.loads(ad.asl_sent(REGION, ACCOUNT))
    victim = sorted(live["States"])[0]
    live["States"].pop(victim)
    legs = run(monkeypatch, sfn=FakeSFN(json.dumps(live)))
    found = findings(legs, "state_machine")
    assert [f.get("state") for f in found] == [victim], found
    assert rc(legs) == 1


# ── the exit-code contract ──────────────────────────────────────────────────
def test_an_unreachable_check_never_exits_zero(monkeypatch):
    """AccessDenied on one role is not a pass. audit_landed.py's rule: a check that
    cannot answer must not report clean."""
    legs = run(monkeypatch, iam=FakeIAM(boom=["llmops-lambda-driver"]))
    assert findings(legs) == []
    assert [u["resource"] for u in unanswered(legs)] == ["role llmops-lambda-driver"]
    assert rc(legs) == 2


def test_a_role_that_does_not_exist_is_drift_not_unanswered(monkeypatch):
    """get_existing() in 01_iam.py returns (None, None) for BOTH absent and unreadable.
    That conflation is harmless on the deploy path -- either way it creates the role --
    and fatal here, because this tool's exit code turns on telling them apart."""
    legs = run(monkeypatch, iam=FakeIAM(absent=["llmops-sfn-execution"]))
    assert unanswered(legs) == []
    assert [f["problem"] for f in findings(legs, "iam")] == [
        "declared in deploy/iam/, ABSENT live"]
    assert rc(legs) == 1


def test_a_role_with_no_inline_policy_is_drift(monkeypatch):
    legs = run(monkeypatch, iam=FakeIAM(no_inline=["llmops-lambda-webhook"]))
    found = findings(legs, "iam")
    assert len(found) == 1 and "none of its permissions" in found[0]["problem"]


def test_drift_outranks_an_unanswered_leg(monkeypatch):
    legs = run(monkeypatch, iam=FakeIAM(absent=["llmops-lambda-start"],
                                        boom=["llmops-lambda-driver"]))
    assert findings(legs) and unanswered(legs)
    assert rc(legs) == 1, "drift must be reported as drift even when a leg went unanswered"


@pytest.mark.parametrize("drift,unknown,expected", [
    (0, 0, 0),
    (1, 0, 1),
    (0, 1, 2),
    (1, 1, 1),
])
def test_the_exit_code_table_is_the_documented_one(drift, unknown, expected):
    legs = [ad.leg("iam",
                   drift=[{"resource": "r", "problem": "p"}] * drift,
                   unknown=[{"resource": "r", "why": "w"}] * unknown,
                   compared=["r"])]
    assert rc(legs) == expected
    for code in ("    0 ", "    1 ", "    2 ", "    3 "):
        assert code in ad.__doc__, f"exit {code.strip()} is not documented"


def test_an_unreadable_bucket_parameter_leaves_the_harness_leg_unanswered(monkeypatch):
    """Every skill source URI embeds the bucket. Guessing the derived name when the
    published one is unreadable would report drift on all seven harnesses in the one case
    where a deploy passed --bucket -- so the honest answer is 'unanswered'."""
    legs = run(monkeypatch, ssm=FakeSSM(error=ApiError("AccessDeniedException")),
               lam=FakeLam(FakeSSM()))
    harnesses = next(lg for lg in legs if lg["leg"] == "harnesses")
    assert harnesses["drift"] == [] and harnesses["compared"] == []
    assert "/llmops/storage/bucket" in harnesses["unknown"][0]["resource"]
    # The same parameter feeds DATA_BUCKET into every Lambda's env, so that leg cannot be
    # answered either -- and says so rather than comparing against a derived guess.
    lam_leg = next(lg for lg in legs if lg["leg"] == "lambda_config")
    assert lam_leg["drift"] == []
    assert all("sent side unbuildable" in u["why"] for u in lam_leg["unknown"])
    assert rc(legs) == 2


def test_the_harness_bucket_is_published_and_the_iam_bucket_is_derived(monkeypatch):
    """01_iam.py DERIVES the bucket from the account id; 05_harnesses.py prefers the
    PUBLISHED /llmops/storage/bucket. Two scripts, two answers -- the sent side is
    whatever each script sends, so this asymmetry is mirrored, not smoothed over."""
    seen = {}

    def spy_harnesses(ctl, mapping, agents=None):
        seen["harness"] = mapping["<DATA_BUCKET>"]
        return ad.leg("harnesses")

    real = ad.iam_deploy.build_role_specs

    def spy_specs(mapping, memory_id):
        seen["iam"] = mapping["<DATA_BUCKET>"]
        return real(mapping, memory_id)

    monkeypatch.setattr(ad, "audit_harnesses", spy_harnesses)
    monkeypatch.setattr(ad.iam_deploy, "build_role_specs", spy_specs)
    run(monkeypatch, ssm=FakeSSM(bucket="a-bucket-03-storage-published"))
    assert seen == {"harness": "a-bucket-03-storage-published", "iam": DERIVED_BUCKET}


# ── the Lambda legs ─────────────────────────────────────────────────────────
def test_lambda_code_is_compared_per_member_not_by_sha():
    """`bundle()` uses zipfile.write, which stamps each entry with the source file's
    mtime, so the zip bytes -- and CodeSha256 with them -- are NOT reproducible from a
    fresh checkout. Comparing shas would report permanent drift on a perfectly deployed
    function, which is worse than not checking at all.

    Asserted mechanically rather than by reading the docstring: the two comparing
    functions must not mention the field, with docstrings and comments stripped first so
    the prose above cannot satisfy the test.
    """
    src = Path(ad.__file__).read_text()
    for name in ("audit_lambda_code", "audit_lambda_config"):
        start = src.index(f"def {name}(")
        body = src[start:src.index("\ndef ", start + 1)]
        code = "\n".join(line.split("#")[0]
                         for i, chunk in enumerate(body.split('"""')) if i % 2 == 0
                         for line in chunk.splitlines())
        assert "CodeSha256" not in code, f"{name} compares a sha that cannot be reproduced"
    assert "CodeSha256" in " ".join(ad.NOT_CHECKED), (
        "the tool must SAY it does not check the sha, or a reader assumes it does")


def test_a_byte_perfect_bundle_is_clean():
    assert ad.audit_lambda_code(FakeLam(), fetch=fetch_clean)["drift"] == []


def test_a_stale_member_is_named_with_the_first_line_that_differs():
    """The 26 KB-diff rule from state_machine_drift: the useful sentence is 'handler.py
    differs from line 40', not a dump of both files."""
    def fetch(lam, fn):
        got = fetch_clean(lam, fn)
        got["events.py"] = got["events.py"].replace(b"\n", b"\n", 1)[:200] + b"# stale\n"
        return got

    found = ad.audit_lambda_code(FakeLam(), fetch=fetch)["drift"]
    assert found, "a live bundle serving different bytes read clean"
    for f in found:
        assert f["member"] == "events.py"
        assert re.search(r"first at line \d+ \(byte \d+\)", f["problem"]), f


def test_a_member_missing_from_the_live_zip_is_drift():
    """task_tokens.py was added, imported by two handlers, and a hand-maintained write
    list had no way to notice: the deploy succeeds and the driver dies at cold start."""
    def fetch(lam, fn):
        got = fetch_clean(lam, fn)
        got.pop("task_tokens.py")
        return got

    found = ad.audit_lambda_code(FakeLam(), fetch=fetch)["drift"]
    assert found and all(f["member"] == "task_tokens.py" for f in found)
    assert "cold start" in found[0]["problem"]


def test_a_leftover_member_in_the_live_zip_is_drift():
    def fetch(lam, fn):
        return {**fetch_clean(lam, fn), "old_contract.py": b"# deleted from the repo\n"}

    found = ad.audit_lambda_code(FakeLam(), fetch=fetch)["drift"]
    assert found and all(f["member"] == "old_contract.py" for f in found)


def test_a_function_that_cannot_be_fetched_is_unanswered():
    def fetch(lam, fn):
        raise ApiError("AccessDeniedException")

    got = ad.audit_lambda_code(FakeLam(), fetch=fetch)
    assert got["drift"] == [] and len(got["unknown"]) == len(ad.lambda_deploy.LAMBDAS)


def test_a_live_env_variable_this_deploy_never_sends_is_drift(monkeypatch):
    """update_function_configuration REPLACES the whole Variables map, so a leftover key
    is one an `os.environ.get()` may still be reading -- the asymmetry with harnesses
    (containment) is deliberate."""
    def mutate(live):
        live["llmops-webhook"]["Environment"]["Variables"]["OLD_TABLE"] = "gone"

    ssm = FakeSSM()
    legs = run(monkeypatch, ssm=ssm, lam=FakeLam(ssm, mutate=mutate))
    found = findings(legs, "lambda_config")
    assert [f["field"] for f in found] == ["env.OLD_TABLE"], found
    assert "sends no such variable" in found[0]["problem"]


def test_a_missing_async_invoke_config_is_drift(monkeypatch):
    """Without it the ACCOUNT defaults apply: 2 retries over up to 6 HOURS. Lambda
    dropped one async self-invoke on 2026-08-08 and a run sat dead nine hours; a
    continuation redelivered hours later would resume a turn whose session is long gone."""
    ssm = FakeSSM()
    lam = FakeLam(ssm)
    lam.get_function_event_invoke_config = lambda FunctionName: (_ for _ in ()).throw(
        ApiError("ResourceNotFoundException"))
    legs = run(monkeypatch, ssm=ssm, lam=lam)
    found = findings(legs, "lambda_config")
    assert len(found) == len(ad.lambda_deploy.LAMBDAS)
    assert all("6 hours" in f["problem"] for f in found)


def test_an_absent_lambda_is_drift_on_both_of_its_legs(monkeypatch):
    ssm = FakeSSM()
    lam = FakeLam(ssm, absent=["llmops-resurrector"])

    def fetch(lam_, fn):
        if fn == "llmops-resurrector":
            raise ApiError("ResourceNotFoundException")
        return fetch_clean(lam_, fn)

    legs = run(monkeypatch, fetch=fetch, ssm=ssm, lam=lam)
    assert [f["problem"] for f in findings(legs, "lambda_config")] == [
        "deployed by 07_lambdas.py, ABSENT live"]
    assert [f["problem"] for f in findings(legs, "lambda_code")] == [
        "deployed by 07_lambdas.py, ABSENT live"]


# ── the sent side, checked against the deploy scripts themselves ────────────
def _deploy_src(rel):
    return (REPO / rel).read_text()


def _function_src(text, name):
    start = text.index(f"def {name}(")
    return text[start:text.index("\ndef ", start + 1)]


def test_the_asl_sent_side_does_exactly_what_the_deploy_does():
    """Two str.replace calls and nothing else. A third one added to the deploy without
    being added here would make every audit report state-machine drift forever."""
    body = _function_src(_deploy_src("deploy/07_lambdas.py"), "deploy_state_machine")
    assert body.count(".replace(") == 2, "the deploy's substitutions changed"
    assert '"${HarnessDriverArn}", driver_arn' in body
    assert '"${EventBusName}", "llmops-pipeline"' in body
    arn = re.search(r'driver_arn = f"([^"]+)"', body).group(1)
    expected = arn.replace("{region}", REGION).replace("{account}", ACCOUNT)
    sent = ad.asl_sent(REGION, ACCOUNT)
    assert expected in sent and "${" not in sent
    raw = (REPO / "orchestration/state_machine.asl.json").read_text()
    assert sent == raw.replace("${HarnessDriverArn}", expected) \
                      .replace("${EventBusName}", "llmops-pipeline")


def test_the_harness_sent_side_pops_and_ensures_what_the_deploy_does():
    """ensure_env is the load-bearing one: it sets OTEL_TRACES_SAMPLER=always_on, which is
    in nobody's harness.json, so a sent side that skipped it would report
    environmentVariables drift on all seven harnesses."""
    body = _function_src(_deploy_src("deploy/05_harnesses.py"), "create_or_update")
    assert body.count("cfg.pop(") == 2, "the deploy pops a different set of keys now"
    assert 'cfg.pop("harnessName")' in body and 'cfg.pop("tags", None)' in body
    assert "ensure_env(cfg)" in body
    assert "k in UPDATED_FIELDS" in body

    mapping = ad.config_subst.mapping_for(ACCOUNT, REGION, DERIVED_BUCKET)
    for agent in ad.harness_deploy.AGENTS:
        name, sent = ad.harness_sent(agent, mapping)
        assert name and "harnessName" not in sent and "tags" not in sent
        assert set(sent) <= set(ad.harness_deploy.UPDATED_FIELDS)
        assert sent["environmentVariables"]["OTEL_TRACES_SAMPLER"] == "always_on"
        assert ad.config_subst.unresolved(sent) == [], f"{agent} kept a placeholder"


def test_the_sent_lambda_shape_matches_the_deploy_script():
    """Runtime, handler and the async policy are literals inside the deploy's write path,
    so they are restated here -- and a restated fact is how the env_keys list drifted for
    eight days. This is the check that keeps the copy honest."""
    body = _deploy_src("deploy/07_lambdas.py")
    assert f'Runtime="{ad.RUNTIME}"' in body
    assert f'Handler="{ad.HANDLER}"' in body
    assert f"MaximumRetryAttempts={ad.ASYNC_RETRIES}" in body
    assert f"MaximumEventAgeInSeconds={ad.ASYNC_MAX_AGE_S}" in body
    sent = ad.lambda_sent(FakeSSM(), REGION, ACCOUNT,
                          ad.lambda_deploy.LAMBDAS["driver"])
    assert sent["Timeout"] == 900 and sent["MemorySize"] == 512
    # Derived from the handler's own os.environ[...] reads, never hand-listed.
    assert "ACTUALS_TABLE" in sent["Environment"], (
        "the requirement the deploy missed for eight days is not in the sent side")


def test_the_sent_bundle_is_the_union_the_deploy_writes():
    body = _function_src(_deploy_src("deploy/07_lambdas.py"), "bundle")
    assert 'z.write(src, "handler.py")' in body
    assert "vendored_modules().items()" in body
    members = set(ad.bundle_members_sent())
    assert members == {p.name for p in ad.lambda_deploy.vendored_modules().values()}
    assert "handler.py" not in members, "handler.py is per-function, not vendored"


def test_the_iam_sent_side_is_the_repo_document():
    """policy_diff is 01_iam.py's own comparator, called with the printing removed."""
    body = _function_src(_deploy_src("deploy/01_iam.py"), "show_diff")
    assert "policy_diff(label, current, desired)" in body, (
        "show_diff no longer delegates; the audit and the deploy now compare differently")
    live = specs()
    for name, spec in live.items():
        assert ad.iam_deploy.policy_diff(name, spec["policy"], spec["policy"]) == []
    assert ad.iam_deploy.policy_diff("x", None, {"a": 1}), (
        "an absent live document must read as a difference, not as no change"
    )


# ── offline mode ────────────────────────────────────────────────────────────
def test_offline_without_an_account_id_is_a_usage_error(capsys):
    assert ad.main(["--region", REGION, "--offline"]) == 3


def test_offline_builds_every_sent_side_and_calls_no_aws(monkeypatch, capsys):
    """This is the mode CI runs. It must not need credentials at all -- so boto3 is
    replaced by something that raises, and offline still has to work."""
    def boom(*a, **kw):
        raise AssertionError("offline mode made an AWS call")

    monkeypatch.setattr(ad.boto3, "client", boom)
    assert ad.main(["--region", REGION, "--account-id", ACCOUNT, "--offline"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["unresolved"] == []
    would = out["would_compare"]
    assert len(would["roles"]) == len(ad.iam_deploy.ROLE_NAMES)
    assert len(would["harnesses"]) == len(ad.harness_deploy.AGENTS)
    assert len(would["lambdas"]) == len(ad.lambda_deploy.LAMBDAS)
    assert "handler.py" in would["bundle_members"]


def test_offline_refuses_a_sent_side_that_still_carries_a_placeholder(monkeypatch):
    """A `<PLACEHOLDER>` or `${Token}` that survives substitution is a config AgentCore
    ACCEPTS, versions and reports READY -- and then fails at every session start."""
    monkeypatch.setattr(ad, "asl_sent",
                        lambda region, account: '{"StartAt": "${EventBusName}"}')
    assert ad.main(["--region", REGION, "--account-id", ACCOUNT, "--offline"]) == 1


def test_offline_runs_env_values_for_real(monkeypatch):
    """The eight-day ACTUALS_TABLE bug: a handler required a variable the deploy had no
    value for, and the crash landed inside an agent turn a day later. env_values raises on
    that, so running it offline is most of what this mode buys."""
    monkeypatch.setattr(ad.lambda_deploy, "required_env_keys",
                        lambda src: {"A_NEW_REQUIREMENT"})
    with pytest.raises(KeyError, match="A_NEW_REQUIREMENT"):
        ad.build_sent_side(ACCOUNT, REGION)


def test_no_credentials_is_exit_two_not_a_pass(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("NoCredentialsError")

    monkeypatch.setattr(ad.boto3, "client", boom)
    assert ad.main(["--region", REGION]) == 2


# ── what it says it cannot answer ───────────────────────────────────────────
def test_the_report_always_prints_what_it_did_not_check():
    lines = []
    rcode = ad.report([ad.leg("iam", compared=["role x"])],
                      {"sha": "abc", "branch": "main"}, out=lines.append)
    assert rcode == 0
    text = "\n".join(lines)
    assert "NOT checked" in text
    for item in ad.NOT_CHECKED:
        assert item in text
    assert "WORKING TREE" in text, (
        "a clean report must still say it compared the working tree, or 'clean' reads as "
        "'this branch is deployed'")


def test_a_dirty_tree_is_called_out():
    lines = []
    ad.report([], {"sha": "abc", "branch": "main", "dirty": True}, out=lines.append)
    assert "DIRTY" in lines[0]


def test_the_ci_offline_step_runs_this_tool():
    wf = (REPO / ".github/workflows/ci.yml").read_text()
    assert "tools/audit_drift.py" in wf and "--offline" in wf


def test_the_json_output_is_gitignored():
    """The report contains live ARNs, and therefore this account's id. The redaction
    scanner covers tracked files; this keeps the file from becoming one."""
    assert "/drift-audit*.json" in (REPO / ".gitignore").read_text()
