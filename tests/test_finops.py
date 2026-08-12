"""Unit tests for the FinOps orchestration layer — no AWS, all clients injected.

Covers the finops harness config, the driver's handling of the three audit terminal
tools, and the scheduled reconcile Lambda. The cost arithmetic itself is tested in
tests/test_cost_model.py; this file is about the wiring around it.

Run: .venv/bin/python -m pytest tests/test_finops.py -q
"""
from __future__ import annotations

import datetime
import importlib.util
import json
import pathlib
import re
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


driver = _load("harness_driver_fin", "orchestration/harness_driver/handler.py")
reconcile = _load("finops_reconcile", "orchestration/finops_reconcile/handler.py")

HARNESS = json.loads((REPO / "agents/finops/harness.json").read_text())

ENV = {
    "RUNS_TABLE": "llmops-pipeline-runs",
    "EVENTS_TABLE": "llmops-stage-events",
    "ESTIMATES_TABLE": "llmops-cost-estimates",
    "ACTUALS_TABLE": "llmops-cost-actuals",
    "EVENT_BUS": "llmops-pipeline",
    "LLMOPS_SNS_TOPIC": "arn:aws:sns:us-east-1:123456789012:llmops-escalations",
    "DATA_BUCKET": "llmops-data-test",
    "DRIVER_FN": "llmops-harness-driver",
    "PROJECT": "llmops-agentic-system",
    "AWS_REGION": "us-east-1",
    # Without this, _resolve_harness_arn falls through to a live ssm:GetParameter for
    # /llmops/harness/finops. Any test that drives the handler LOOP (rather than
    # calling handle_finops_tool directly) reaches it -- and it passed on a laptop
    # with credentials while failing in CI with NoCredentialsError. tests/conftest.py
    # now makes that impossible to miss again.
    "HARNESS_ARN_LLMOPS_FINOPS":
        "arn:aws:bedrock-agentcore:us-east-1:123456789012:harness/llmops_finops-TESTSUFFIX",
}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)


# ── fakes ─────────────────────────────────────────────────────────────────────
class FakeTable:
    def __init__(self, items=None):
        self.items = list(items or [])
        self.puts, self.updates = [], []

    def put_item(self, Item):
        self.puts.append(Item)
        self.items.append(Item)

    def update_item(self, **kw):
        self.updates.append(kw)

    def query(self, **kw):
        return {"Items": self.items}

    def scan(self, **kw):
        return {"Items": self.items}


class FakeDDB:
    def __init__(self, tables=None):
        self.tables = tables or {}

    def Table(self, name):
        return self.tables.setdefault(name, FakeTable())


class FakeS3:
    def __init__(self, present=()):
        self.present = set(present)

    def head_object(self, Bucket, Key):
        if f"s3://{Bucket}/{Key}" not in self.present:
            raise RuntimeError("404")
        return {}


class FakeSNS:
    def __init__(self):
        self.published = []

    def publish(self, **kw):
        self.published.append(kw)


class FakeLambda:
    def __init__(self, payload=None):
        self.calls = []
        self.payload = payload or {"status": "completed"}

    def invoke(self, **kw):
        self.calls.append(kw)
        return {"StatusCode": 202,
                "Payload": json.dumps(self.payload).encode()}


REPORT_URI = "s3://llmops-data-test/finops/cost/report-2026-07-29.json"
RATES_URI = "s3://llmops-data-test/finops/rates/rate_card_latest.json"

FINOPS_EVENT = {"run_id": "finops-2026-07-29", "stage": "finops", "task": "reconcile",
                "harness_id": "llmops_finops",
                "manifest_uri": "s3://llmops-data-test/finops/manifests/2026-07-29.json",
                "params": {"task": "reconcile", "project": "llmops-agentic-system",
                           "period": "2026-07-29"}}


def _clients(present=(REPORT_URI, RATES_URI)):
    return {"s3": FakeS3(present), "ddb": FakeDDB(), "sns": FakeSNS(),
            "sfn": None, "events": None, "lambda": FakeLambda()}


# ── the harness config ────────────────────────────────────────────────────────
def test_harness_declares_the_three_audit_tools_and_the_driver_knows_them():
    """A tool the harness offers but the driver does not service falls into the
    unknown-tool branch and the agent loops until its re-asks run out — the audit
    would silently produce nothing. Asserted as equality, not subset: a shrinking
    driver list is exactly the failure this test exists to catch, and a subset
    assertion is satisfied by it."""
    declared = {t["name"] for t in HARNESS["tools"] if t["type"] == "inline_function"}
    audit = {"publish_cost_report", "update_rate_card", "flag_variance"}
    assert audit <= declared
    assert set(driver.FINOPS_TERMINAL_TOOLS) == audit
    assert audit <= set(HARNESS["allowedTools"])


def test_harness_is_read_only_by_construction_no_browser():
    """The auditor must not be able to change what it audits. It also gets no browser:
    its evidence is billing API text, and a browser widens a read-only role's reach."""
    types = {t["type"] for t in HARNESS["tools"]}
    assert "agentcore_browser" not in types
    assert "agentcore_code_interpreter" in types


def test_harness_prompt_names_every_measured_hazard():
    """These are not decorative warnings — each corresponds to a probe that failed on
    the live account. A prompt that loses one loses the reason the mechanism exists."""
    prompt = HARNESS["systemPrompt"][0]["text"]
    for phrase in ("NEVER BY SERVICE", "Inactive", "LAGS", "provisional",
                   "REALIZED RATES", "NEVER INVENT A RATE", "DO NOT STOP RUNS"):
        assert phrase in prompt, phrase


def _finops_prompt():
    return HARNESS["systemPrompt"][0]["text"]


def test_harness_points_at_the_canonical_cost_model_rather_than_prose_math():
    """The reference must be FETCHABLE, not merely present. A repo path satisfies "the
    prompt names the module" while giving a container with no checkout nothing to open —
    see test_the_canonical_cost_module_is_uploaded_where_the_prompt_says_to_fetch_it."""
    prompt = _finops_prompt()
    assert "cost_model.py" in prompt
    assert "pipeline/contracts/cost_model.py" not in prompt, \
        "a repo-relative path is unresolvable inside the harness container"


def test_harness_name_and_tags_match_the_seventh_runtime():
    assert HARNESS["harnessName"] == "llmops_finops"
    assert HARNESS["tags"]["agent-type"] == "finops"
    assert HARNESS["tags"]["project"] == "llmops-agentic-system"


# ── IAM: the auditor must not be able to change what it audits ───────────────
def _statements(path, *keys):
    doc = json.loads((REPO / path).read_text())
    for k in keys:
        doc = doc[k]
    return doc["Statement"]


def _actions(statements):
    """Every Action string in a statement list, or in a whole policy document."""
    if isinstance(statements, dict):
        statements = statements["Statement"]
    out = set()
    for s in statements:
        a = s.get("Action", [])
        out.update([a] if isinstance(a, str) else a)
    return out


HARNESS_ROLE = "deploy/iam/harness_execution_role.json"
LAMBDA_ROLES = "deploy/iam/lambda_roles.json"


def test_finops_can_read_the_billing_apis_it_needs():
    """Cost Explorer resource-level is the only path to per-run attribution, and the
    Price List call is what proves the rate feed stale rather than guessing."""
    acts = _actions(_statements(HARNESS_ROLE, "permissionsPolicy"))
    for needed in ("ce:GetCostAndUsage", "ce:GetCostAndUsageWithResources",
                   "pricing:GetProducts", "budgets:DescribeBudgets"):
        assert needed in acts, needed


# The Budgets service is the one place in this repo where the boto3 call name and the
# IAM action name diverge: describe_budgets authorizes against budgets:ViewBudget, so a
# policy that grants only budgets:DescribeBudgets denies the call. That is exactly what
# shipped, and the console degraded quietly -- an except/print, so the Cost tab rendered
# with an empty budgets list and no error anywhere the user could see. This map is the
# fixed part; the next test derives the demand side from the source that makes the calls.
BUDGETS_CALL_TO_IAM = {"describe_budgets": "budgets:ViewBudget"}

BILLING_CALLERS = ("deploy/console/lambda_function.py",
                   "orchestration/finops_reconcile/handler.py",
                   "pipeline/contracts/cost_model.py")


def test_every_budgets_api_call_is_granted_the_action_it_authorizes_against():
    """Derived from the callers, not from a hand-kept list: grep the source for the
    boto3 Budgets calls that actually exist, then require the IAM action each one is
    authorized against in every policy whose role runs that code. Anchored on the
    call site (`b.describe_budgets(`) rather than the substring, so a mention of the
    name in a comment cannot satisfy it."""
    policies = {
        "deploy/console/iam-policy.json": _actions(
            json.loads((REPO / "deploy/console/iam-policy.json").read_text())),
        HARNESS_ROLE: _actions(_statements(HARNESS_ROLE, "permissionsPolicy")),
    }
    found = {}
    for rel in BILLING_CALLERS:
        p = REPO / rel
        if not p.exists():
            continue
        src = p.read_text()
        for call, action in BUDGETS_CALL_TO_IAM.items():
            if re.search(r"\.\s*" + call + r"\s*\(", src):
                found.setdefault(action, []).append(rel)
    assert found, ("no Budgets call found in any of " + str(BILLING_CALLERS)
                   + " -- if the call moved, point BILLING_CALLERS at its new home "
                     "rather than deleting this guard")
    for action, callers in found.items():
        for path, acts in policies.items():
            assert action in acts, (
                f"{path} is missing {action}, which {callers} needs: the Budgets API "
                f"authorizes describe_budgets against it, and the caller swallows the "
                f"AccessDeniedException into a log line")


def test_no_role_can_mutate_billing_configuration():
    """An auditor with write access to cost categories, allocation-tag status, or
    budgets can reshape the numbers it reports on. Every billing verb granted anywhere
    in this repo must be a read."""
    everything = _actions(_statements(HARNESS_ROLE, "permissionsPolicy"))
    lam = json.loads((REPO / LAMBDA_ROLES).read_text())
    for role in lam["roles"].values():
        everything |= _actions(role["permissionsPolicy"])
    console = json.loads((REPO / "deploy/console/iam-policy.json").read_text())
    everything |= _actions(console["Statement"])

    billing = {a for a in everything
               if a.split(":")[0] in ("ce", "pricing", "budgets", "cur", "billing")}
    assert billing, "expected some billing permissions to exist"
    # "View" joins the read verbs for Budgets specifically: budgets:ViewBudget is the
    # read side of that namespace (its writes are Create/Modify/Update/Delete/Execute).
    read_verbs = ("Get", "List", "Describe", "View")
    bad = [a for a in billing if not a.split(":", 1)[1].startswith(read_verbs)]
    assert bad == [], f"mutating billing permission granted: {bad}"
    # Deliberately redundant with the prefix check above, and only for the namespace
    # whose read verb we just widened: the named writes are what a future edit to
    # read_verbs (or a "budgets:*" grant reduced to a prefix) could let through.
    for w in ("budgets:ModifyBudget", "budgets:CreateBudget", "budgets:UpdateBudget",
              "budgets:DeleteBudget", "budgets:ExecuteBudgetAction", "budgets:*"):
        assert w not in billing, w


def test_finops_cannot_rewrite_an_approval_decision():
    """The gate is only a gate if the audited party cannot flip its own verdict. The
    agent reads estimates to compare against and must never write them."""
    for s in _statements(HARNESS_ROLE, "permissionsPolicy"):
        res = s.get("Resource", "")
        res = [res] if isinstance(res, str) else res
        if any("llmops-cost-estimates" in r for r in res):
            acts = _actions([s])
            assert not {a for a in acts if a.split(":", 1)[1].startswith(
                ("Put", "Update", "Delete", "BatchWrite"))}, acts


def test_the_driver_can_write_the_findings_it_records():
    """handle_finops_tool PutItems into the actuals table; without this grant the
    audit's own findings fail with AccessDenied while the rest of the turn succeeds."""
    lam = json.loads((REPO / LAMBDA_ROLES).read_text())
    acts, tables = set(), set()
    for s in lam["roles"]["driver"]["permissionsPolicy"]["Statement"]:
        res = s.get("Resource", "")
        res = [res] if isinstance(res, str) else res
        if any("llmops-cost-actuals" in r for r in res):
            acts |= _actions([s])
            tables |= {r for r in res if "llmops-cost-actuals" in r}
    assert "dynamodb:PutItem" in acts
    assert tables


def test_the_reconcile_lambda_has_a_role_that_can_invoke_the_driver():
    lam = json.loads((REPO / LAMBDA_ROLES).read_text())
    assert "finops_reconcile" in lam["roles"]
    acts = _actions(lam["roles"]["finops_reconcile"]["permissionsPolicy"])
    assert "lambda:InvokeFunction" in acts


# ── deployability: a config nothing deploys is a component that does not exist ──

def _deploy_src(name):
    return (REPO / "deploy" / name).read_text()


def test_every_harness_config_on_disk_is_named_by_the_deploy_script():
    """05_harnesses.py's AGENTS list is what a bare run creates and what --agent
    validates against, so a config it does not name is a harness that silently never
    exists. This was live: agents/finops/harness.json shipped complete, `--agent
    finops` was rejected as an invalid choice, and the fleet stayed at six while
    every doc said seven."""
    on_disk = {p.parent.name for p in (REPO / "agents").glob("*/harness.json")}
    # Read the list the script itself defines rather than re-parsing the source: a
    # string-split parser has to model Python's comment and quoting rules, and the
    # first version of this test silently dropped the very entry it was written to
    # catch because a `#` comment sits inside the literal.
    spec = importlib.util.spec_from_file_location(
        "harnesses_deploy", REPO / "deploy/05_harnesses.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    missing = on_disk - set(mod.AGENTS)
    assert not missing, f"harness configs with no deploy path: {missing}"


def test_every_scheduled_function_is_one_the_lambda_deployer_creates():
    """08_triggers.py schedules llmops-finops-daily against a function name. If
    07_lambdas.py does not create that function the schedule fires daily into a
    ResourceNotFound — visible only in the scheduler's own metrics, never in the
    dashboard, so the auditor appears to be running while nothing runs."""
    triggers, lambdas = _deploy_src("08_triggers.py"), _deploy_src("07_lambdas.py")
    assert "llmops-finops-reconcile" in triggers, "test guards the wrong name"
    assert '"fn": "llmops-finops-reconcile"' in lambdas, \
        "scheduled function has no entry in LAMBDAS"


def test_a_role_change_reaches_an_existing_function_not_only_a_new_one():
    """update_function_configuration silently ignores a role it is not passed, so
    without Role= a tightened role applies only to functions that do not exist yet:
    every re-run reports "updated" while the live function keeps the role it was born
    with. Measured live — llmops-finops-reconcile stayed on llmops-lambda-driver
    across a successful "updated" run."""
    src = _deploy_src("07_lambdas.py")
    upd = src.split("update_function_configuration(", 1)[1].split(")", 1)[0]
    assert "Role=role_arn" in upd, "role change would not reach an existing function"


def test_the_auditor_lambda_runs_under_its_own_role_not_the_drivers():
    """Reusing the driver role works and is what a first pass reaches for. It also
    hands the auditor every permission the thing it audits has."""
    src = _deploy_src("07_lambdas.py")
    finops = src.split('"finops": {', 1)[1].split("},", 1)[0]
    assert "/llmops/iam/lambda_finops_reconcile_arn" in finops
    assert "/llmops/iam/lambda_driver_arn" not in finops


# The harness deploy has to prove it landed too (#81). Same defect as the Role above and
# as the ASL read-back in #80, one resource over: a call that returned is not a change that
# landed, and for a harness READY is the reassuring surface that hides it.

def _h5():
    return _load("deploy_harnesses_readback", "deploy/05_harnesses.py")


def _sent(prompt="new", **over):
    """A config in the shape create_or_update sends: only UPDATED_FIELDS, resolved."""
    cfg = {"systemPrompt": [{"text": prompt}],
           "model": {"bedrockModelConfig": {"modelId": "global.anthropic.claude-fable-5"}},
           "maxTokens": 65536, "timeoutSeconds": 840,
           "environment": {"agentCoreRuntimeEnvironment": {
               "networkConfiguration": {"networkMode": "PUBLIC"}}}}
    cfg.update(over)
    return cfg


class _CtlReads:
    """get_harness returns each queued config in turn, the last one repeating."""
    def __init__(self, *harnesses):
        self.queue, self.reads = list(harnesses), 0

    def get_harness(self, **kw):
        i = min(self.reads, len(self.queue) - 1)
        self.reads += 1
        return {"harness": self.queue[i]}


def test_the_service_adding_its_own_keys_is_not_reported_as_drift():
    """Containment, not equality -- and this is the measurement that forced it.

    On a harness that is perfectly in sync, ``environment`` still differs: the deploy sends
    networkConfiguration + lifecycleConfiguration and the service returns those plus the
    agentRuntimeArn/Name/Id it assigned. Strict equality would therefore report drift on
    every correct deploy of all seven harnesses, forever -- and a check that cries wolf on a
    correct deploy is a check the third person it wakes switches off. Verified live against
    all seven harnesses: five reported CLEAN under containment.
    """
    m = _h5()
    sent = _sent()
    live = {"systemPrompt": [{"text": "new"}],
            "model": {"bedrockModelConfig": {"modelId": "global.anthropic.claude-fable-5"}},
            "maxTokens": 65536, "timeoutSeconds": 840,
            "environment": {"agentCoreRuntimeEnvironment": {
                "networkConfiguration": {"networkMode": "PUBLIC"},
                "agentRuntimeArn": "arn:aws:bedrock-agentcore:us-east-1:123456789012:"
                                   "runtime/harness_x-abc",
                "agentRuntimeId": "harness_x-abc"}},
            "harnessVersion": "6", "status": "READY", "createdAt": "whenever"}
    assert m.harness_config_drift(sent, live) == []


def test_a_prompt_that_did_not_land_is_named_as_drift():
    """The live #77 shape: main says one thing, the harness serves another, status READY."""
    m = _h5()
    drift = m.harness_config_drift(_sent(prompt="the $36.36/day figure"),
                                   {"systemPrompt": [{"text": "the ~$18/day figure"}],
                                    "status": "READY"})
    assert any(d["field"] == "systemPrompt" for d in drift), \
        f"a stale prompt was not reported: {drift}"


def test_a_field_absent_live_is_distinguished_from_one_that_differs():
    """"sent, but ABSENT live" and "differs" have different causes and different fixes.

    A field the service dropped means the update did not carry it; a field with another
    value means something else deployed. Collapsing both into "differs" makes the log ask
    the reader to guess which.
    """
    m = _h5()
    sent = _sent(environment={"agentCoreRuntimeEnvironment": {
        "lifecycleConfiguration": {"maxLifetime": 28800}}})
    drift = m.harness_config_drift(sent, {"systemPrompt": [{"text": "new"}],
                                          "model": sent["model"],
                                          "maxTokens": 65536, "timeoutSeconds": 840,
                                          "environment": {"agentCoreRuntimeEnvironment": {}}})
    paths = {d["field"]: d["problem"] for d in drift}
    assert "environment.agentCoreRuntimeEnvironment.lifecycleConfiguration" in paths, \
        f"the absent nested field was not named by its path: {drift}"
    assert "ABSENT live" in \
        paths["environment.agentCoreRuntimeEnvironment.lifecycleConfiguration"]


def test_a_field_the_deployer_never_sends_is_not_its_drift_to_report():
    """``memory`` belongs to 04_wire_memory.py. Reporting it here would make every run of
    this script look broken over a field it deliberately does not touch -- and a report full
    of another owner's differences is one nobody finishes reading."""
    m = _h5()
    assert "memory" not in m.UPDATED_FIELDS
    drift = m.harness_config_drift(_sent(), {"systemPrompt": [{"text": "new"}],
                                             "model": _sent()["model"],
                                             "maxTokens": 65536, "timeoutSeconds": 840,
                                             "environment": _sent()["environment"],
                                             "memory": {"whatever": "the wirer set"}})
    assert drift == [], f"a field this script never sends was reported as its drift: {drift}"


def test_the_read_back_checks_exactly_the_fields_the_update_sends():
    """Two lists that must not drift apart. If the update call sends a field the read-back
    does not check, that field can fail to land silently -- which is this whole defect,
    reintroduced one field at a time. So the deployer builds both from one name."""
    m = _h5()
    src = (REPO / "deploy/05_harnesses.py").read_text()
    assert "if k in UPDATED_FIELDS}" in src, \
        "the update payload no longer derives from UPDATED_FIELDS; the two lists can drift"
    assert "systemPrompt" in m.UPDATED_FIELDS


def test_a_long_prompt_is_reported_by_length_and_divergence_not_pasted_twice():
    """The finops prompt is 6539 characters. Dumping both sides scrolls the one line that
    mattered off the screen, so the report gives lengths and the first divergence."""
    m = _h5()
    a, b = "x" * 3000 + "OLD" + "y" * 500, "x" * 3000 + "NEW" + "y" * 500
    problem = m.harness_config_drift({"systemPrompt": [{"text": b}]},
                                     {"systemPrompt": [{"text": a}]})[0]["problem"]
    assert "chars" in problem and "first differ at" in problem
    assert len(problem) < 400, f"the drift report pasted the whole prompt: {len(problem)}"


def test_a_config_that_never_lands_fails_the_deploy_loudly():
    """A warning is read by nobody: the deploy prints a success report either way, and the
    next person reads "updated"/READY. Same reason a blocking bus gap raises in 07."""
    m = _h5()
    ctl = _CtlReads({"systemPrompt": [{"text": "stale"}], "status": "READY",
                     "harnessVersion": "5"})
    with pytest.raises(SystemExit) as exc:
        m.confirm_harness_landed(ctl, "llmops_finops-abc", _sent(prompt="fresh"),
                                 sleep=lambda s: None)
    assert "llmops_finops-abc" in str(exc.value)
    assert "READY" in str(exc.value), \
        "the message does not tell the reader why READY was not evidence"


def test_a_version_still_publishing_is_waited_out_rather_than_called_drift():
    """A version publish is not instantaneous. A read-back that gives up on the first look
    turns a settling deploy into a failed one, and a guard that fails correct deploys gets
    deleted -- the lesson from the ASL read-back and from warm()'s two-fast-turns rule."""
    m = _h5()
    ctl = _CtlReads({"systemPrompt": [{"text": "stale"}], "harnessVersion": "5"},
                    {"systemPrompt": [{"text": "fresh"}],
                     "model": _sent()["model"], "maxTokens": 65536,
                     "timeoutSeconds": 840, "environment": _sent()["environment"],
                     "harnessVersion": "6"})
    slept = []
    out = m.confirm_harness_landed(ctl, "h-abc", _sent(prompt="fresh"),
                                   sleep=slept.append)
    assert out["config_confirmed"] is True
    assert out["harness_version"] == "6"
    assert ctl.reads == 2 and slept == [1], \
        f"did not wait out a settling publish: reads={ctl.reads} slept={slept}"


def test_a_harness_read_back_that_cannot_run_says_so_instead_of_claiming_confirmed():
    """Not being able to check is not the same as having checked. A throttled or denied
    get_harness must not resolve to a confirmation.

    Named for the harness deliberately: the ASL version of this test (#80) already exists in
    this file, and the first draft of this one reused its exact name. Python keeps the later
    definition, so the ASL test silently vanished from the suite and m93's control would
    have been verified by the wrong test entirely -- a collision no count noticed, because
    the total still went up.
    """
    m = _h5()

    class _Denied:
        def get_harness(self, **kw):
            raise RuntimeError("AccessDeniedException: bedrock-agentcore:GetHarness")

    out = m.confirm_harness_landed(_Denied(), "h-abc", _sent(), sleep=lambda s: None)
    assert out["config_confirmed"] is False
    assert "AccessDenied" in out["read_back_unreachable"]


def test_the_harness_deploy_path_actually_calls_the_read_back():
    """A verifier nothing invokes is the same defect one level up.

    Drives the real create_or_update against a fake control plane whose live config is the
    stale one, and asserts it both fails and had really called update_harness -- so the test
    cannot pass by never reaching the deploy at all.
    """
    m = _h5()

    class _Ctl:
        def __init__(self):
            self.updated = []

        def list_harnesses(self):
            return {"harnesses": [{"harnessId": "llmops_finops-abc",
                                   "harnessName": "llmops_finops"}]}

        def get_harness(self, **kw):
            return {"harness": {"harnessId": "llmops_finops-abc", "status": "READY",
                                "arn": "arn:aws:bedrock-agentcore:us-east-1:123456789012:"
                                       "harness/llmops_finops",
                                "systemPrompt": [{"text": "the stale ~$18/day prompt"}],
                                "harnessVersion": "5"}}

        def update_harness(self, **kw):
            self.updated.append(kw)
            return {}

    ctl = _Ctl()
    cfg = dict(_sent(prompt="the $36.36/day prompt"), harnessName="llmops_finops")
    with pytest.raises(SystemExit) as exc:
        m.create_or_update(ctl, cfg, "arn:aws:iam::123456789012:role/x", dry=False)
    assert ctl.updated, "the deploy never even called update_harness"
    assert "systemPrompt" in str(exc.value)


def test_the_config_is_confirmed_before_any_turn_is_spent_warming_it():
    """Order matters, and not only for tidiness: warm() spends up to six real model turns.
    Warming first means paying to make a harness that serves the WRONG prompt fast to
    reach, and it puts the reassuring "warmed" line above the failure in the log."""
    src = (REPO / "deploy/05_harnesses.py").read_text()
    body = src.split("def create_or_update(", 1)[1]
    assert body.index("confirm_harness_landed(") < body.index("out.update(warm("), \
        "the harness is warmed before its config is confirmed"


def _run_deployer_dry(monkeypatch, capsys, only):
    """Run 07_lambdas.py's main() with --dry-run and stub clients, and return the
    parsed report. Dry-run reaches every target-selection decision -- which is the
    property under test -- while the only AWS call left on the path is the read-only
    ValidateStateMachineDefinition, stubbed here."""
    mod = _load("deploy_lambdas", "deploy/07_lambdas.py")

    class _Sfn:
        exceptions = type("E", (), {"StateMachineDoesNotExist": Exception})

        def validate_state_machine_definition(self, **kw):
            return {"result": "OK", "diagnostics": []}

    monkeypatch.setattr(mod.boto3, "client",
                        lambda svc, **kw: _Sfn() if svc == "stepfunctions" else object())
    argv = ["07_lambdas.py", "--region", "us-east-1", "--dry-run"]
    for t in only:
        argv += ["--only", t]
    monkeypatch.setattr(sys, "argv", argv)
    mod.main()
    return json.loads(capsys.readouterr().out)


def test_a_targeted_deploy_ships_only_what_was_asked_for(monkeypatch, capsys):
    """The state machine and the resume rule used to deploy unconditionally, so
    `--only driver` shipped the driver AND whatever the working tree's ASL said. A
    flag named --only that widens past its argument is worse than no flag: the whole
    reason to reach for it is blast radius."""
    report = _run_deployer_dry(monkeypatch, capsys, ["driver"])
    assert report["targets"] == ["driver"]
    assert [r.get("lambda") for r in report["results"]] == ["llmops-harness-driver"]
    assert not [r for r in report["results"] if "state_machine" in r or "rule" in r], \
        "a driver-only deploy also published the state machine or the resume rule"


def test_the_state_machine_can_be_deployed_without_redeploying_any_lambda(monkeypatch,
                                                                         capsys):
    """Found by the MarkRunDone fix: the ASL change had to reach production while the
    driver's own redeploy was deliberately held back pending an IAM widen, and --only
    could not express that -- it selected among LAMBDAS alone, so every route to the
    ASL dragged a Lambda along."""
    report = _run_deployer_dry(monkeypatch, capsys, ["state_machine"])
    assert report["targets"] == ["state_machine"]
    assert [r.get("state_machine") for r in report["results"]] == ["llmops-pipeline"]
    assert not [r for r in report["results"] if "lambda" in r], \
        "deploying the state machine alone still redeployed a Lambda"


def test_a_bare_run_still_deploys_everything(monkeypatch, capsys):
    """Making the non-Lambda targets selectable must not make them opt-in: a bare
    `07_lambdas.py --region ...` is the full-deploy path, and dropping the state
    machine from it would leave the ASL silently un-deployed forever."""
    report = _run_deployer_dry(monkeypatch, capsys, [])
    src = _deploy_src("07_lambdas.py")
    every = re.findall(r'^\s{4}"(\w+)": \{$', src, re.M)  # the LAMBDAS keys
    assert set(every) <= set(report["targets"]), "a bare run skips a Lambda"
    assert {"state_machine", "resume_rule"} <= set(report["targets"])
    assert [r.get("state_machine") for r in report["results"]].count("llmops-pipeline") == 1
    assert [r.get("rule") for r in report["results"]].count(
        "llmops-sagemaker-job-state") == 1


def test_the_documented_spine_lambda_count_matches_LAMBDAS():
    """Both the script's own docstring and deploy/README.md said "4 spine Lambdas" long
    after finops-reconcile made it 5 — the auditor was undocumented in the very table
    someone reads to find out what runs. Derived from LAMBDAS so the next addition
    fails here instead of quietly making the docs wrong again."""
    src = _deploy_src("07_lambdas.py")
    n = len(re.findall(r'^\s{4}"(\w+)": \{$', src, re.M))
    assert n >= 5, "guard reads the wrong thing: found no LAMBDAS keys"
    for doc, text in (("07_lambdas.py", src),
                      ("deploy/README.md", (REPO / "deploy/README.md").read_text())):
        claims = re.findall(r"(\w+) spine Lambdas", text)
        assert claims, f"{doc}: no spine-Lambda count claim to check"
        words = {"four": 4, "five": 5, "six": 6, "seven": 7}
        for c in claims:
            got = words.get(c.lower(), int(c) if c.isdigit() else None)
            assert got == n, f"{doc} claims {c!r} spine Lambdas, LAMBDAS has {n}"


# ---------------------------------------------------------------------------
# The ASL deploy has to prove it landed (#80)
# ---------------------------------------------------------------------------
def _lambdas():
    return _load("deploy_lambdas_readback", "deploy/07_lambdas.py")


_ASL = json.dumps({
    "StartAt": "A",
    "States": {"A": {"Type": "Task", "TimeoutSeconds": 86400, "Next": "B"},
               "B": {"Type": "Pass", "End": True}},
})


def _asl_without(state):
    """The same ASL with one state removed — the shape the live machine was actually in."""
    d = json.loads(_ASL)
    d["States"].pop(state)
    return json.dumps(d)


class _SfnReads:
    """describe_state_machine returns each queued definition in turn, last one repeating."""

    exceptions = type("E", (), {"StateMachineDoesNotExist": Exception})

    def __init__(self, *definitions):
        self.queue, self.reads, self.updated = list(definitions), 0, []

    def describe_state_machine(self, **kw):
        i = min(self.reads, len(self.queue) - 1)
        self.reads += 1
        return {"definition": self.queue[i], "revisionId": f"rev-{i}"}

    def update_state_machine(self, **kw):
        self.updated.append(kw)
        return {}


def test_a_state_the_asl_declares_but_the_live_machine_lacks_is_named():
    """The exact defect, reduced. On 2026-08-03 the live definition was missing
    EvalGenerate entirely: merged 2026-08-02 as the whole point of #57 (the quality gate
    read evaluation/report.json and nothing wrote it), the suite green, the ASL never
    deployed. `action: "updated"` was the only thing the deployer had ever said about it.

    The assertion is on the state NAME, not on "something differs": a report that says
    "definitions differ" sends the reader to a 26 KB diff, and the whole value here is
    the one-line answer.
    """
    mod = _lambdas()
    drift = mod.state_machine_drift(_ASL, _asl_without("B"))
    assert drift, "a live machine missing a state was reported as matching"
    assert any(d.get("state") == "B" and "ABSENT live" in d["problem"] for d in drift), \
        f"the drift report does not name the missing state: {drift}"


def test_drift_reports_a_live_state_the_asl_no_longer_has():
    """The other direction, and it is not symmetric noise: a state left behind live is
    reachable by any execution whose path still names it, so a rename deployed as
    add-plus-remove can leave the old one running forever."""
    mod = _lambdas()
    drift = mod.state_machine_drift(_asl_without("B"), _ASL)
    assert any(d.get("state") == "B" and "absent from the ASL" in d["problem"]
               for d in drift), drift


def test_drift_names_the_fields_that_differ_on_a_shared_state():
    """A timeout that never landed is the case that started this: the field has to be
    named, because "A differs" does not tell you whether it was the timeout, the retry
    policy or the next state."""
    mod = _lambdas()
    stale = json.loads(_ASL)
    stale["States"]["A"]["TimeoutSeconds"] = 7200
    drift = mod.state_machine_drift(_ASL, json.dumps(stale))
    assert any(d.get("state") == "A" and "TimeoutSeconds" in d["problem"]
               for d in drift), drift


def test_identical_definitions_are_not_reported_as_drift():
    """The check has to be silent when things are fine, or it gets switched off — and
    then the next undeployed state is invisible again. Byte-for-byte equal, and also
    equal-but-reformatted: Step Functions returns the definition verbatim today
    (measured), but a whitespace difference is not a deploy failure."""
    mod = _lambdas()
    assert mod.state_machine_drift(_ASL, _ASL) == []
    reformatted = json.dumps(json.loads(_ASL), indent=4, sort_keys=True)
    assert mod.state_machine_drift(_ASL, reformatted) == [], \
        "a formatting-only difference was reported as drift; this check cries wolf"


def test_an_unexplained_difference_is_never_reported_as_clean():
    """If equality fails and neither walk localises it, the honest answer is "differs,
    cannot localise" -- not []. Returning clean would turn the one case this function
    cannot explain into a pass, which is the failure it exists to prevent.

    The input is the reachable version of that, not a contrived one: a key PRESENT with a
    null value on one side and ABSENT on the other. The dicts are unequal (different key
    sets) while every `.get()` comparison agrees, because both sides yield None. Both
    walks are built on `.get`, so both are blind to exactly this shape.
    """
    mod = _lambdas()
    with_null = json.dumps({**json.loads(_ASL), "Comment": None})
    drift = mod.state_machine_drift(with_null, _ASL)
    assert drift, "an unexplained difference was reported as no drift"
    assert "cannot localise" in json.dumps(drift), \
        f"the difference was localised after all -- this test's subject is wrong: {drift}"


def test_a_deploy_whose_definition_never_lands_fails_loudly():
    """The point of the task. update_state_machine returning 200 is not evidence the
    machine changed — the sibling defect (update_function_configuration called without
    Role) reported "updated" for months while the live function kept its birth role. So
    a definition that never matches must stop the deploy, not annotate it.
    """
    mod = _lambdas()
    sfn = _SfnReads(_asl_without("B"))
    with pytest.raises(SystemExit) as exc:
        mod.confirm_state_machine_landed(sfn, "arn:sm", _ASL, attempts=2,
                                        sleep=lambda s: None)
    msg = str(exc.value)
    assert "B" in msg and "ABSENT live" in msg, f"the failure does not say what drifted: {msg}"
    assert "not the one in this tree" in msg, \
        "the failure does not say the live definition is what will execute"


def test_a_confirmed_deploy_reports_the_revision_it_verified():
    """"updated" is the deployer's own claim; the revision id and the live state count are
    read back from the resource. That difference is the entire task."""
    mod = _lambdas()
    sfn = _SfnReads(_ASL)
    out = mod.confirm_state_machine_landed(sfn, "arn:sm", _ASL, sleep=lambda s: None)
    assert out["definition_confirmed"] is True
    assert out["revision_id"] == "rev-0"
    assert out["states_live"] == 2
    assert sfn.reads == 1, "a matching definition should be confirmed on the first read"


def test_eventual_consistency_is_waited_out_rather_than_reported_as_drift():
    """UpdateStateMachine is eventually consistent — AWS documents that executions
    started immediately after may still use the PREVIOUS definition. A single read would
    fail on deploys that were in fact fine, and a check that cries wolf is a check that
    gets deleted. Same eventual consistency that bit the push tool's ref read.
    """
    mod = _lambdas()
    sfn = _SfnReads(_asl_without("B"), _ASL)   # stale first, correct on the retry
    slept = []
    out = mod.confirm_state_machine_landed(sfn, "arn:sm", _ASL, sleep=slept.append)
    assert out["definition_confirmed"] is True, \
        "a stale first read was reported as drift instead of being retried"
    assert sfn.reads == 2 and slept == [1], f"reads={sfn.reads} slept={slept}"


def test_a_read_back_that_cannot_run_says_so_instead_of_claiming_confirmed():
    """No credentials, throttling, a deleted machine: all of them mean "unknown". The one
    answer that must never come out of an unreachable read is "confirmed" -- the same
    rule the dry-run's ASL validation already follows when the validator is unreachable.
    """
    mod = _lambdas()

    class _Denied:
        exceptions = type("E", (), {"StateMachineDoesNotExist": Exception})

        def describe_state_machine(self, **kw):
            raise RuntimeError("AccessDeniedException")

    out = mod.confirm_state_machine_landed(_Denied(), "arn:sm", _ASL, sleep=lambda s: None)
    assert out["definition_confirmed"] is False
    assert "AccessDenied" in out["read_back_unreachable"]


def test_the_asl_deploy_path_actually_calls_the_read_back():
    """A verifier nothing invokes is the same defect one level up. Runs the real
    deploy_state_machine against a live machine stuck on a stale definition and requires
    the deploy to fail -- which is only possible if it read the definition back.
    """
    mod = _lambdas()
    # Substituted exactly as the deployer will substitute it, so the ONLY drift in the
    # report is the missing state. A fixture that also differs on every Parameters block
    # would pass this test while proving nothing about which difference was detected.
    asl = (REPO / "orchestration/state_machine.asl.json").read_text() \
        .replace("${HarnessDriverArn}",
                 "arn:aws:lambda:us-east-1:123456789012:function:llmops-harness-driver") \
        .replace("${EventBusName}", "llmops-pipeline")
    stale = json.loads(asl)
    stale["States"].pop("EvalGenerate")   # the real state that was really missing

    class _Ssm:
        exceptions = type("E", (), {"ParameterNotFound": Exception})

        def get_parameter(self, Name):
            return {"Parameter": {"Value": "arn:aws:iam::123456789012:role/sfn"}}

    sfn = _SfnReads(json.dumps(stale))
    with pytest.raises(SystemExit) as exc:
        mod.deploy_state_machine(sfn, _Ssm(), "us-east-1", "123456789012", False,
                                 sleep=lambda s: None)
    assert "EvalGenerate" in str(exc.value)
    assert sfn.updated, "the deploy never even called update_state_machine"


def _harness_s3(sid):
    doc = json.loads((REPO / "deploy/iam/harness_execution_role.json").read_text())
    for st in doc["permissionsPolicy"]["Statement"]:
        if st.get("Sid") == sid:
            return st
    raise AssertionError(f"no statement {sid}")


def _prefixes(sid):
    st = _harness_s3(sid)
    res = st["Resource"]
    return {r.rsplit("/", 1)[0].split("/", 1)[-1]
            for r in ([res] if isinstance(res, str) else res)}


def _readable_prefixes():
    """Prefixes the role can GET: the read/write set plus every read-only statement.

    Each read-only Sid is named rather than globbed, so adding a new one is a deliberate
    edit here. All three exist because their prefix must NOT be writable: customer-data/
    holds the held-out set the gates are judged on, skills/ holds the instructions the agent
    itself is judged against, and code/ holds the canonical trainer and the canonical judge
    prompt -- an agent that could overwrite its own measuring instrument is not measured.

    code/ was MISSING from this set until the eval prompt named it, and the omission was
    invisible for a different reason than it looks: the finetune prompt has cited
    `code/distill/train_qlora.py` since the canonical-trainer fix, but in the bare spelling,
    which the caller's `s3://<bucket>/...` regex does not match -- and `code` was absent from
    this set, so the bare-form branch (`known`) did not look for it either. Two independent
    misses cancelling out is why this returned green while granting nothing.
    """
    return (_prefixes("S3PipelineObjects")
            | _prefixes("S3CustomerDataReadOnly")
            | _prefixes("S3SkillsReadOnly")
            | _prefixes("S3CanonicalCodeReadOnly"))


def test_every_s3_prefix_the_auditor_writes_is_one_its_role_can_write():
    """Measured live on the first successful pricing_refresh: the agent derived a
    complete 37-SKU rate card, then got AccessDenied writing
    finops/rates/rate_card_latest.json, so it published nothing and stamped its own
    output non-canonical. The role granted runs/, reports/ and skills-mirror/ only —
    the whole feature ran to completion and threw its work away.

    Asserted against the prompt's own URIs rather than a hardcoded list, so a future
    prefix added to the prompt fails here instead of at 09:00 UTC in production.
    """
    prompt = _finops_prompt()
    granted = {r.rsplit("/", 1)[0].split("/", 1)[-1]
               for r in _harness_s3("S3PipelineObjects")["Resource"]}
    for prefix in {u.split("/")[3] for u in re.findall(r"s3://<bucket>/\S+", prompt)}:
        assert prefix in granted, f"prompt writes {prefix}/ but the role cannot"


def test_every_s3_prefix_any_agent_prompt_uses_is_one_the_role_can_reach():
    """The same defect as the test above, widened past finops -- because it recurred.

    Live, the orchestrator's accepted plan pointed data-prep at
    customer-data/arc-demo/ (the prefix the consult prompt tells it to use) and the
    data-prep specialist was denied both s3:ListBucket and s3:GetObject there. The run
    dispatched, reached DataPrepGenerate, and failed without reading a single task; the
    customer's data was never the problem. models-mirror/ has the identical gap: the
    prompts and train_qlora.py both treat it as the only trustworthy source of
    open-weight models, and nothing granted it.

    Driven off every agent prompt rather than a hardcoded list, so a prefix added to
    any prompt fails here rather than in the middle of a paid run."""
    granted = _readable_prefixes()
    # Every prefix these prompts name -- as an s3:// URI or bare, which is how
    # customer-data/ and models-mirror/ are written and how both were missed.
    known = granted | {"customer-data", "models-mirror"}
    for harness in sorted((REPO / "agents").glob("*/harness.json")):
        text = harness.read_text()
        used = {u.split("/")[3] for u in re.findall(r"s3://<bucket>/\S+", text)}
        used |= {m for m in known if f"{m}/" in text}
        for prefix in used:
            assert prefix in granted, \
                f"{harness.parent.name} prompt uses {prefix}/ but the role cannot reach it"


def test_the_list_prefixes_match_the_object_prefixes():
    """ListBucket is condition-scoped, so a prefix granted for Get/Put but absent from
    the condition can be read object-by-object and never enumerated. That reads as
    "the rate card history is not there" when it is."""
    objects = _readable_prefixes()
    listed = {p.rstrip("/*") for p in
              _harness_s3("S3PipelineList")["Condition"]["StringLike"]["s3:prefix"]}
    assert objects == listed, f"asymmetric: objects={objects} list={listed}"


def test_the_agents_cannot_rewrite_the_instrument_they_are_measured_by():
    """`code/` holds the canonical trainer and the canonical pairwise judge prompt. Both
    are artifacts an agent is told to READ instead of composing its own, which only means
    something if it cannot write them: an eval agent that can PutObject over
    code/eval/judge_prompt_pairwise.md can move its own bar and every run after it silently
    measures something else. Same argument as skills/ and customer-data/ one test down;
    stated separately because `code/` arrived later and was granted GetObject with no
    guard saying the Put half must stay absent.
    """
    ro = _harness_s3("S3CanonicalCodeReadOnly")
    assert set(ro["Action"]) == {"s3:GetObject"}, "the canonical code prefix must not be writable"
    assert "code" in _prefixes("S3CanonicalCodeReadOnly")
    assert "code" not in _prefixes("S3PipelineObjects"), \
        "code/ leaked into the read/write statement -- the instrument is now self-editable"


def test_the_customers_own_data_is_readable_but_never_writable():
    """A pipeline that can rewrite the customer's data can destroy the held-out set
    its own quality gates are judged against -- the one prefix where least-privilege
    has to mean read-only, not "read plus the write we happened to need"."""
    ro = _harness_s3("S3CustomerDataReadOnly")
    assert set(ro["Action"]) == {"s3:GetObject"}, "customer data must not be writable"
    assert "customer-data" in _prefixes("S3CustomerDataReadOnly")
    # and it must NOT have leaked into the read/write statement
    assert "customer-data" not in _prefixes("S3PipelineObjects")


def test_the_canonical_cost_module_is_uploaded_where_the_prompt_says_to_fetch_it():
    """The prompt used to say "read pipeline/contracts/cost_model.py" — a repo-relative
    path that means nothing inside an AgentCore container, which has no checkout. Live
    result: the agent searched its filesystem, the installed packages, and S3, found the
    module in none of them, and hand-applied the merge precedence instead, stamping its
    card v1-DRAFT-noncanonical because the fallback_static tier lives inside the module.
    A canonical implementation nothing distributes is not canonical."""
    prompt = _finops_prompt()
    assert "s3://<bucket>/contracts/cost_model.py" in prompt, \
        "prompt must name a fetchable URI, not a repo path"
    storage = _deploy_src("03_storage.py")
    assert "def ensure_contracts" in storage and 'f"contracts/{p.name}"' in storage, \
        "nothing uploads the contracts to the prefix the prompt reads"
    assert "results[\"contracts\"]" in storage, "ensure_contracts is defined but never called"


def test_the_price_list_coverage_rule_states_what_it_actually_cannot_price():
    """The rule existed because Price List silently zero-prices models it does not
    carry. Its DeepSeek-R1 example was wrong — the model attribute value is bare 'R1'
    (provider=DeepSeek), so scanning for a name containing "DeepSeek-R1" finds nothing
    and concludes absence. Verified 2026-07-31: R1 IS priced and matches our realized
    rate to <0.001%; Claude Fable 5 / Opus 5 are NOT (newest Anthropic entries are
    Claude 3). A hazard rule justified by a false example gets deleted by the next
    reader who checks it."""
    prompt = _finops_prompt()
    rule = [p for p in prompt.split("\n\n") if "PRICE LIST" in p][0]
    assert "Fable 5" in rule and "Opus 5" in rule, "must name what is unpriceable"
    assert "cannot price DeepSeek-R1" not in rule, "falsified claim still in the prompt"


# ── driver: publish_cost_report ───────────────────────────────────────────────
def test_report_with_a_missing_s3_artifact_is_rejected_not_recorded():
    """Trust-but-verify, same as stage_complete: a report the agent claims but never
    wrote leaves a dashboard panel pointing at a 404."""
    c = _clients(present=())
    out = driver.handle_finops_tool(c, FINOPS_EVENT, "publish_cost_report", {
        "report_uri": REPORT_URI, "period": "2026-07-29", "total_usd": 10.77,
        "settlement": "settled", "headline": "x"})
    assert out["ok"] is False
    assert "not in S3" in out["reason"]
    assert c["ddb"].Table(ENV["ACTUALS_TABLE"]).puts == []


def test_report_without_a_settlement_state_is_rejected():
    """A cost number with no settlement state cannot be read safely: the reader cannot
    tell whether it will still move."""
    c = _clients()
    out = driver.handle_finops_tool(c, FINOPS_EVENT, "publish_cost_report", {
        "report_uri": REPORT_URI, "period": "2026-07-29", "total_usd": 10.77,
        "headline": "x"})
    assert out["ok"] is False
    assert "provisional" in out["reason"]


def test_report_with_an_invented_settlement_value_is_rejected():
    c = _clients()
    out = driver.handle_finops_tool(c, FINOPS_EVENT, "publish_cost_report", {
        "report_uri": REPORT_URI, "period": "2026-07-29", "total_usd": 1.0,
        "settlement": "final", "headline": "x"})
    assert out["ok"] is False


def test_a_valid_provisional_report_is_accepted_and_recorded():
    c = _clients()
    out = driver.handle_finops_tool(c, FINOPS_EVENT, "publish_cost_report", {
        "report_uri": REPORT_URI, "period": "2026-07-29", "total_usd": 10.77,
        "settlement": "provisional", "excluded_usd": 314.18, "headline": "ok"})
    assert out["ok"] is True
    row = c["ddb"].Table(ENV["ACTUALS_TABLE"]).puts[0]
    assert row["project"] == "llmops-agentic-system"
    assert row["sk"].startswith("2026-07-29#finding#publish_cost_report")



# ── driver: the audit loop ────────────────────────────────────────────────────
class _Stream:
    """Minimal invoke_harness response: a tool call, then messageStop."""

    def __init__(self, name, args, stop="tool_use"):
        self.events = [
            {"contentBlockStart": {"start": {"toolUse": {
                "toolUseId": f"tu-{name}", "name": name}}}},
            {"contentBlockDelta": {"delta": {"toolUse": {
                "input": json.dumps(args)}}}},
            {"messageStop": {"stopReason": stop}},
        ]


class _FakeAgentCore:
    def __init__(self, streams):
        self.streams, self.calls = list(streams), []

    def invoke_harness(self, **kw):
        self.calls.append(kw)
        if not self.streams:      # terminal tools send a fire-and-forget ack
            return {"stream": [{"messageStop": {"stopReason": "end_turn"}}]}
        return {"stream": self.streams.pop(0).events}


def test_flag_variance_continues_the_loop_with_its_own_acknowledgement():
    """flag_variance is a FINDING, not the end of the audit -- one turn can flag
    several runs, so the driver acknowledges it and keeps going. That continue-branch
    assigned the ack to a variable the loop no longer reads (a leftover from renaming
    the loop's content -> messages for the two-message resume contract), so the next
    turn would re-send the PREVIOUS message: the same finding flagged forever, or a
    toolResult answering a call that was already answered. The existing tests call
    handle_finops_tool directly and so never see the loop; pyflakes flagged it as an
    unused local, and only a loop-level test shows what it costs.

    It also asserts the RETURN VALUE, because the first version of this test did not.
    Its fake publish_cost_report omitted the mandatory ``settlement`` field, so the
    report was rejected, the loop ran out of re-asks, and the run ended in
    ``missing stage_complete`` -- yet the turn-2 assertions below all passed, because
    turn 2 happens before any of that. A loop test that never checks where the loop
    ENDED will keep passing while the loop dies two turns later."""
    ac = _FakeAgentCore([
        _Stream("flag_variance", {
            "run_id": "run-a", "estimate_usd": 10.0, "actual_usd": 30.0,
            "variance_pct": 200.0, "driver": "sagemaker_training",
            "recommendation": "raise the throughput constant"}),
        _Stream("publish_cost_report", {
            "report_uri": REPORT_URI, "period": "2026-07-29",
            "total_usd": 30.0, "settlement": "provisional",
            "headline": "spend up 200% on training"}),
    ])
    c = _clients()
    c["agentcore"] = ac
    out = driver.handler(dict(FINOPS_EVENT, harness_id="llmops_finops",
                              task_token=None), clients=c)

    # turn 2 must carry flag_variance's OWN acknowledgement, matched by toolUseId
    second = ac.calls[1]["messages"]
    assert [m["role"] for m in second] == ["assistant", "user"]
    echo = second[0]["content"][0]["toolUse"]
    tr = second[1]["content"][0]["toolResult"]
    assert echo["name"] == "flag_variance"
    assert tr["toolUseId"] == echo["toolUseId"]
    assert json.loads(tr["content"][0]["text"])["status"] == "recorded"

    # ...and the loop must then finish on the terminal tool, not exhaust its re-asks.
    assert out["status"] == "completed", out
    assert out["tool"] == "publish_cost_report"
    assert len(ac.calls) == 3, "one turn per stream plus the terminal ack"


# ── driver: flag_variance ─────────────────────────────────────────────────────
def test_variance_without_a_driver_category_is_rejected():
    """One aggregate percentage says the estimate was wrong without saying what to
    fix, and improving the next estimate is the entire point of reconciling."""
    c = _clients()
    out = driver.handle_finops_tool(c, FINOPS_EVENT, "flag_variance", {
        "run_id": "run-a", "actual_usd": 30.0, "variance_pct": 200.0})
    assert out["ok"] is False
    assert "driver" in out["reason"]


def test_a_flagged_variance_notifies_a_human_and_is_recorded():
    c = _clients()
    out = driver.handle_finops_tool(c, FINOPS_EVENT, "flag_variance", {
        "run_id": "run-a", "estimate_usd": 10.0, "actual_usd": 30.0,
        "variance_pct": 200.0, "driver": "sagemaker_training",
        "recommendation": "raise the throughput constant"})
    assert out["ok"] is True
    assert len(c["sns"].published) == 1
    assert "run-a" in c["sns"].published[0]["Subject"]
    assert c["ddb"].Table(ENV["ACTUALS_TABLE"]).puts[0]["sk"].endswith("#run-a")


def test_a_notify_failure_still_keeps_the_variance_row():
    """The finding is the durable artifact; SNS is the convenience. Losing the row
    because a topic was misconfigured would lose the audit."""
    class DeadSNS:
        def publish(self, **kw):
            raise RuntimeError("no topic")
    c = _clients()
    c["sns"] = DeadSNS()
    out = driver.handle_finops_tool(c, FINOPS_EVENT, "flag_variance", {
        "run_id": "run-a", "actual_usd": 30.0, "variance_pct": 200.0,
        "driver": "sagemaker_training"})
    assert out["ok"] is True
    assert len(c["ddb"].Table(ENV["ACTUALS_TABLE"]).puts) == 1


def test_two_runs_flagged_in_one_turn_produce_two_distinct_rows():
    """An audit covers many runs, so flag_variance must not be terminal — the sort key
    carries run_id precisely so a second finding does not overwrite the first."""
    c = _clients()
    for rid in ("run-a", "run-b"):
        driver.handle_finops_tool(c, FINOPS_EVENT, "flag_variance", {
            "run_id": rid, "actual_usd": 1.0, "variance_pct": 50.0,
            "driver": "sagemaker_training"})
    sks = [p["sk"] for p in c["ddb"].Table(ENV["ACTUALS_TABLE"]).puts]
    assert len(set(sks)) == 2


# ── driver: update_rate_card ───────────────────────────────────────────────────
def test_rate_card_must_exist_in_s3_to_be_accepted():
    c = _clients(present=())
    out = driver.handle_finops_tool(c, FINOPS_EVENT, "update_rate_card", {
        "rates_uri": RATES_URI, "n_rates": 9})
    assert out["ok"] is False


def test_rate_card_records_the_skus_it_could_not_price():
    """The Price List API cannot price Fable 5 or Opus 5 on this account — the harness
    fleet's own models — so a refresh reporting zero missing SKUs is the suspicious
    outcome, not the good one."""
    c = _clients()
    out = driver.handle_finops_tool(c, FINOPS_EVENT, "update_rate_card", {
        "rates_uri": RATES_URI, "n_rates": 7, "n_stale": 1,
        "missing_skus": ["bedrock:tokens:us.deepseek.r1-v1:0:input"],
        "sources": {"ce_realized": 5, "price_list": 2}})
    assert out["ok"] is True
    detail = json.loads(c["ddb"].Table(ENV["ACTUALS_TABLE"]).puts[0]["detail"])
    assert detail["missing_skus"]


# ── driver: the audit invocation has no run and no token ──────────────────────
def test_finops_escalation_does_not_mint_a_phantom_run_row():
    """A scheduled audit has no row in the runs table. Writing one would surface a
    fake run in the console beside real pipeline runs."""
    c = _clients()
    driver.handle_escalate(c, FINOPS_EVENT, {"reason": "r", "details": "d"})
    assert c["ddb"].Table(ENV["RUNS_TABLE"]).updates == []
    assert len(c["sns"].published) == 1


def test_is_finops_only_matches_the_audit_stage():
    assert driver._is_finops(FINOPS_EVENT) is True
    assert driver._is_finops({"stage": "finetune"}) is False
    assert driver._is_finops({}) is False


# ── reconcile Lambda: period selection ────────────────────────────────────────
def test_default_period_is_behind_the_cost_explorer_lag():
    """CE lags ~24 h; a same-day resource-level query returned Estimated=true with zero
    groups. Reconciling today would read an empty period and report $0."""
    d = datetime.date(2026, 7, 31)
    assert reconcile.default_period(d) == "2026-07-29"
    assert reconcile.DEFAULT_LAG_DAYS >= 2


def test_provisional_periods_are_rechecked_not_left_frozen():
    """Without this the first read of a period wins forever: the settled figure exists
    in Cost Explorer but nothing ever goes back for it."""
    ddb = FakeDDB({ENV["ACTUALS_TABLE"]: FakeTable([
        {"project": "p", "sk": "2026-07-28#run-a#sagemaker_training",
         "settlement": "provisional"},
        {"project": "p", "sk": "2026-07-27#run-b#sagemaker_training",
         "settlement": "settled"},
    ])})
    got = reconcile.unsettled_periods(ddb, "p", today=datetime.date(2026, 7, 31))
    assert got == ["2026-07-28"]


def test_scheduled_run_covers_the_new_day_plus_any_still_provisional_day():
    ddb = FakeDDB({ENV["ACTUALS_TABLE"]: FakeTable([
        {"project": "llmops-agentic-system", "sk": "2026-07-28#run-a#x",
         "settlement": "provisional"}]),
        ENV["ESTIMATES_TABLE"]: FakeTable([])})
    c = {"ddb": ddb, "lambda": FakeLambda(), "sns": FakeSNS()}
    out = reconcile.handler({}, clients=c)
    assert len(out["periods"]) >= 2
    assert "2026-07-28" in out["periods"]


def test_an_explicit_period_overrides_the_schedule_default():
    c = {"ddb": FakeDDB(), "lambda": FakeLambda(), "sns": FakeSNS()}
    out = reconcile.handler({"period": "2026-06-01", "runs": []}, clients=c)
    assert out["periods"] == ["2026-06-01"]


def test_an_unknown_task_is_refused_before_any_invocation():
    c = {"ddb": FakeDDB(), "lambda": FakeLambda(), "sns": FakeSNS()}
    out = reconcile.handler({"task": "delete_everything"}, clients=c)
    assert "error" in out
    assert c["lambda"].calls == []


def test_the_three_supported_tasks_are_exactly_the_documented_ones():
    assert reconcile.TASKS == ("reconcile", "pricing_refresh", "report")


# ── reconcile Lambda: payload ─────────────────────────────────────────────────
def test_payload_carries_region_and_bucket_so_nothing_is_hardcoded():
    """The prompt forbids hardcoding account-specific values, so they must arrive."""
    p = reconcile.build_payload("reconcile", "proj", "2026-07-29", ["run-a"],
                                "bkt", "us-east-1")
    assert p["params"]["bucket"] == "bkt"
    assert p["params"]["region"] == "us-east-1"
    assert p["params"]["rates_uri"].startswith("s3://bkt/finops/rates/")
    assert p["harness_id"] == "llmops_finops"
    assert p["stage"] == "finops"


def test_payload_stage_is_finops_so_the_driver_takes_the_audit_path():
    p = reconcile.build_payload("report", "proj", "2026-07", [], "b", "us-east-1")
    assert driver._is_finops(p) is True


def test_a_failed_run_lookup_degrades_the_comparison_not_the_actuals():
    """Attribution is by resource pattern, so actuals survive an empty run list; only
    the estimate-vs-actual comparison narrows. Skipping the whole period instead would
    lose the day's cost data over a metadata read."""
    class AngryDDB(FakeDDB):
        def Table(self, name):
            if name == ENV["ESTIMATES_TABLE"]:
                raise RuntimeError("throttled")
            return super().Table(name)
    c = {"ddb": AngryDDB(), "lambda": FakeLambda(), "sns": FakeSNS()}
    out = reconcile.handler({"period": "2026-07-29"}, clients=c)
    assert out["results"][0]["status"] == "invoked"
    assert out["results"][0]["n_runs"] == 0


def test_outcome_is_recorded_so_a_missed_daily_reconcile_is_visible():
    ddb = FakeDDB()
    c = {"ddb": ddb, "lambda": FakeLambda(), "sns": FakeSNS()}
    reconcile.handler({"period": "2026-07-29", "runs": []}, clients=c)
    puts = ddb.Table(ENV["ACTUALS_TABLE"]).puts
    assert puts and puts[0]["sk"] == "2026-07-29#audit#reconcile"


def test_audit_rows_cannot_collide_with_cost_rows():
    """Both live in the actuals table under (project, period#...). The reserved
    '#audit#'/'#finding#' segments are what keep them apart, since no run_id can
    contain '#'."""
    ddb = FakeDDB()
    c = {"ddb": ddb, "lambda": FakeLambda(), "sns": FakeSNS()}
    reconcile.handler({"period": "2026-07-29", "runs": []}, clients=c)
    audit_sk = ddb.Table(ENV["ACTUALS_TABLE"]).puts[0]["sk"]
    cost_sk = "2026-07-29#run-a#sagemaker_training"
    assert audit_sk != cost_sk
    assert "#audit#" in audit_sk and "#audit#" not in cost_sk


def test_sync_mode_surfaces_the_drivers_own_result():
    """The console's on-demand button needs the outcome, not just an ack."""
    c = {"ddb": FakeDDB(), "lambda": FakeLambda({"status": "completed", "tool": "x"}),
         "sns": FakeSNS()}
    out = reconcile.handler({"period": "2026-07-29", "runs": [], "sync": True},
                            clients=c)
    assert out["results"][0]["status"] == "completed"
    assert c["lambda"].calls[0]["InvocationType"] == "RequestResponse"


def test_scheduled_mode_is_async_so_the_scheduler_is_not_held_open():
    c = {"ddb": FakeDDB(), "lambda": FakeLambda(), "sns": FakeSNS()}
    reconcile.handler({"period": "2026-07-29", "runs": []}, clients=c)
    assert c["lambda"].calls[0]["InvocationType"] == "Event"


def test_only_launched_or_reconciled_estimates_become_reconcile_targets():
    """A draft or rejected estimate never spent anything; comparing against one would
    manufacture a variance for a run that does not exist."""
    ddb = FakeDDB({ENV["ESTIMATES_TABLE"]: FakeTable([
        {"run_id": "run-live", "status": "launched", "project": "p"},
        {"run_id": "run-done", "status": "reconciled", "project": "p"},
        {"run_id": "run-draft", "status": "draft", "project": "p"},
        {"run_id": "run-no", "status": "rejected", "project": "p"},
        {"run_id": "run-wait", "status": "pending_approval", "project": "p"},
    ])})
    assert reconcile.runs_in_period(ddb, "p", "2026-07-29") == ["run-done", "run-live"]


def test_iam_documents_survive_comment_stripping_as_printable_ascii():
    """PutRolePolicy enforces printable ASCII and rejects the WHOLE document with
    "Syntax errors in policy", naming no key. Two em dashes cost a deploy cycle once
    (4d71d76) and masked a second defect while doing it.

    Checked AFTER stripping _comment, because that is the document AWS actually sees:
    the rationale keys are free to use whatever punctuation reads best, and this test
    would be wrong to forbid it. What must be ASCII is everything that survives the
    strip.
    """
    spec = importlib.util.spec_from_file_location("iam_deploy", REPO / "deploy/01_iam.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for path in sorted((REPO / "deploy/iam").glob("*.json")):
        doc = mod.substitute(json.loads(path.read_text()),
                             {"<REGION>": "us-east-1", "<ACCOUNT_ID>": "123456789012",
                              "<DATA_BUCKET>": "b", "<MEMORY_ID>": "m"})
        # ensure_ascii=False is load-bearing: the default escapes every non-ASCII char
        # to \uXXXX, so the check would be satisfied by construction and pass against
        # the very document it exists to reject. The first version of this test did
        # exactly that -- an em dash injected into a Sid was caught only by unrelated
        # tests, while this one reported green.
        bad = [c for c in json.dumps(doc, ensure_ascii=False) if not (32 <= ord(c) < 127)]
        assert not bad, f"{path.name}: non-ASCII survives stripping: {set(bad)}"


# ── the observability cutoff belonged to one account ──────────────────────────────
# The console filters batch-eval and insights to runs created after SPANS_SINCE, because a
# session sampled before OTEL_TRACES_SAMPLER=always_on has no spans and scoring it yields a
# "failed session" that is really a missing measurement. That timestamp was a literal in
# TWO files -- deploy/console/deploy.sh and lambda_function.py -- naming the hour tracing
# came up in THIS account. Wrong for every other deployment, and wrong in the hiding
# direction: a fresh deployment's runs are all newer than 2026-07-28, so they pass the
# filter, get scored against spans nobody sampled, and return as failures with nothing
# naming the cutoff. It is now stamped by the deploy step that makes it true.

class _FakeSsm:
    """SSM that enforces the one semantic this fix rests on: Overwrite=False refuses.

    A double that accepted every put would report the idempotent implementation and the
    cutoff-moving one as identical -- and moving the cutoff forward is the actual damage,
    since it silently excludes runs that DO have spans.
    """

    class ParameterAlreadyExists(Exception):
        pass

    def __init__(self, params=None):
        self.params = dict(params or {})
        self.puts = []

    def put_parameter(self, Name, Value, Overwrite=False, **kw):
        self.puts.append((Name, Value, Overwrite))
        if Name in self.params and not Overwrite:
            raise self.ParameterAlreadyExists(
                f"ParameterAlreadyExists: {Name}")
        self.params[Name] = Value

    def get_parameter(self, Name):
        if Name not in self.params:
            raise KeyError(f"ParameterNotFound: {Name}")
        return {"Parameter": {"Value": self.params[Name]}}


def test_the_spans_cutoff_is_stamped_on_the_first_always_on_deploy():
    """The step that ENABLES always_on is the step that records when it started.

    Written by the deploy rather than transcribed by a human, because a human's copy is
    right for exactly the deployment they copied it from.
    """
    mod, ssm = _h5(), _FakeSsm()
    got = mod.ensure_spans_since(ssm, now="2026-08-10T04:00:00Z")
    assert got == "2026-08-10T04:00:00Z"
    assert ssm.params[mod.SPANS_SINCE_PARAM] == "2026-08-10T04:00:00Z"
    assert ssm.puts[0][2] is False, (
        "the cutoff was written with Overwrite=True, so every redeploy moves it forward")


def test_a_redeploy_never_moves_the_spans_cutoff_forward():
    """Idempotent in the only direction that matters.

    Spans start once. If a redeploy restamped the parameter with today's date, every run
    between the first deploy and now -- all of which DO have spans -- would drop out of
    batch eval and insights, and drop out silently: a missing row prompts no questions.
    """
    mod = _h5()
    ssm = _FakeSsm({"/llmops/observability/spans_since": "2026-07-28T12:00:00Z"})
    got = mod.ensure_spans_since(ssm, now="2026-08-10T04:00:00Z")
    assert got == "2026-07-28T12:00:00Z", "a redeploy moved the cutoff to today"
    assert ssm.params["/llmops/observability/spans_since"] == "2026-07-28T12:00:00Z"


def test_no_deployment_specific_spans_timestamp_is_hardcoded_anywhere():
    """The literal must be gone from BOTH files, not moved between them.

    Checked as "no ISO timestamp near SPANS_SINCE" rather than as the old string, because
    the defect is a hardcoded date for one account -- any date, not that date. A guard
    pinned to 2026-07-28T12:00:00Z would pass the moment somebody updated it to the hour
    THEIR account came up, which is the same bug with a fresher number.
    """
    for rel in ("deploy/console/lambda_function.py", "deploy/console/deploy.sh"):
        for line in (REPO / rel).read_text().splitlines():
            if "SPANS_SINCE" not in line or line.lstrip().startswith("#"):
                continue
            assert not re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", line), (
                f"{rel} hardcodes a spans cutoff: {line.strip()!r}. It is a fact about "
                "one deployment; read it from SSM, where the always_on deploy stamps it.")


def test_the_console_falls_back_to_no_filter_rather_than_a_guessed_date():
    """Empty is the safe end of this switch, and the code has to say so.

    Filtering nothing leaves an unscoreable session visibly unscoreable. A guessed date
    removes real sessions from the list entirely, and nobody asks why a row they never saw
    is missing -- so the fallback direction is the whole design decision here.
    """
    src = (REPO / "deploy/console/lambda_function.py").read_text()
    fn = src.split("def spans_since(", 1)[1].split("\ndef ", 1)[0]
    assert "/llmops/observability/spans_since" in fn, (
        "spans_since() does not read the parameter the deploy stamps")
    assert re.search(r'val\s*=\s*""', fn), (
        "spans_since() has no empty fallback: on a deployment where the parameter is "
        "absent it must filter nothing rather than invent a cutoff")
