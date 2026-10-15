"""The two lakehouse pipeline modes (Phase 1 PR-09): what the state machine runs, who may run it,
and what dispatch refuses.

The owner chose Step Functions running the Glue jobs (`glue:startJobRun.sync`), with one data-prep
turn reviewing the export, over agent-launched jobs and an EventBridge wake chain. So the guards
here are about the seams that choice creates:
- the ASL builds the jobs' arguments itself, and must build what the deploy smoke proved;
- the state machine's role must be able to run exactly those jobs;
- an ingest must reach no harness, and a training run must reach finetune only through the review;
- dispatch must refuse what would otherwise fail a Glue job one stage later.
"""
from __future__ import annotations

import fnmatch
import importlib.util
import json
import pathlib
import re
import runpy
import sys
import tempfile
import types

import pytest

from test_orchestration import _harnesses_of, _reachable_in_mode

REPO = pathlib.Path(__file__).resolve().parents[1]
ASL = json.loads((REPO / "orchestration" / "state_machine.asl.json").read_text())
STATES = ASL["States"]


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


lh = _load("lh_deploy_for_mode", "deploy/10_lakehouse.py")
sp = _load("start_pipeline_for_lakehouse", "orchestration/start_pipeline/handler.py")
driver = _load("driver_for_lakehouse", "orchestration/harness_driver/handler.py")
gen = _load("lh_generate_for_mode", "pipeline/lakehouse/generate.py")

GLUE_STATES = {n: s for n, s in STATES.items() if s.get("Resource", "").startswith("arn:aws:states:::glue:")}
STEP_OF_JOB = {name: step for step, (name, _) in lh.JOBS.items()}


def _path(expr, doc):
    assert expr.startswith("$."), expr
    for key in expr[2:].split("."):
        doc = doc[key]
    return doc


def _render(expr, doc):
    """Evaluate the two intrinsic shapes the lakehouse states use: a JSONPath and States.Format."""
    m = re.fullmatch(r"States\.Format\('([^']*)', (.+)\)", expr)
    if not m:
        return _path(expr, doc)
    out = m.group(1)
    for arg in (a.strip() for a in m.group(2).split(",")):
        out = out.replace("{}", str(_path(arg, doc)), 1)
    assert "{}" not in out, f"more placeholders than arguments in {expr}"
    return out


def _arguments(state, execution_input):
    return {k[:-2] if k.endswith(".$") else k: _render(v, execution_input) if k.endswith(".$") else v
            for k, v in state["Parameters"]["Arguments"].items()}


# ── the state machine builds what the smoke proved ───────────────────────────
@pytest.mark.parametrize("mode,params", [
    ("lakehouse_ingest", {"pipeline_mode": "lakehouse_ingest", "generator_seed": 7, "generator_scale": "demo"}),
    ("lakehouse", {"pipeline_mode": "lakehouse", "lakehouse_dataset": "seed7-demo-20261015-034233"}),
])
def test_every_glue_state_passes_exactly_the_layout_the_deploy_smoke_uses(mode, params):
    """run_arguments() is what --smoke passed when the jobs went green live. The ASL spells the
    same URIs with States.Format; a drift between them would be a job reading a landing prefix the
    generator never wrote, and it would fail only in a pipeline run."""
    run_id = "run-20261015T050000Z-0a1b2c3d"
    inp = {"run_id": run_id, "lakehouse": sp.lakehouse_input(params, run_id, "data-bucket")}
    reached = _reachable_in_mode(ASL, mode) & set(GLUE_STATES)
    assert reached, f"{mode} reaches no Glue state"
    for name in sorted(reached):
        st = GLUE_STATES[name]
        step = STEP_OF_JOB[st["Parameters"]["JobName"]]
        lake = inp["lakehouse"]
        want = lh.run_arguments(step, lake["data_bucket"], lake["dataset"], run_id=run_id,
                                seed=lake.get("seed"), scale=lake.get("scale"))
        assert _arguments(st, inp) == want, name


def test_the_static_arguments_are_not_in_the_state_machine():
    """They are the jobs' DefaultArguments; the ASL carrying them too would be a second copy, and
    07_lambdas substitutes only two placeholders, so it could not carry the account id anyway."""
    for name, st in GLUE_STATES.items():
        script = lh.JOBS[STEP_OF_JOB[st["Parameters"]["JobName"]]][1]
        static = lh.static_arguments(script, "1", "b")
        assert not set(static) & {k.removesuffix(".$") for k in st["Parameters"]["Arguments"]}, name


# ── routing ──────────────────────────────────────────────────────────────────
def test_an_ingest_builds_the_lakehouse_and_reaches_no_harness():
    reached = _reachable_in_mode(ASL, "lakehouse_ingest")
    assert {"LakehouseGenerate", "LakehouseLand", "LakehouseSilver", "LakehouseGold", "Complete"} <= reached
    assert not _harnesses_of(STATES, reached), "an ingest must not spend an agent turn or a GPU"
    assert "LakehouseExport" not in reached and "FinetuneLaunch" not in reached


def test_a_lakehouse_run_trains_only_through_the_review_and_never_generates():
    reached = _reachable_in_mode(ASL, "lakehouse")
    assert {"LakehouseExport", "LakehouseReview", "FinetuneLaunch", "QualityGateChoice"} <= reached
    assert not reached & {"LakehouseGenerate", "DataPrepGenerate", "DataPrepCurate"}
    entrants = {n for n, s in STATES.items() if s.get("Next") == "FinetuneLaunch"
                or any(c.get("Next") == "FinetuneLaunch" for c in s.get("Choices", []))}
    assert entrants == {"DataPrepCurate", "LakehouseReview"}
    assert STATES["LakehouseExport"]["Next"] == "LakehouseReview"


def test_both_modes_branch_only_at_the_top():
    routed = {}
    for c in STATES["PipelineModeChoice"]["Choices"]:
        mode = next((t["StringEquals"] for t in c.get("And", []) if "StringEquals" in t), None)
        routed[mode] = c["Next"]
    assert routed["lakehouse_ingest"] == "LakehouseGenerate" and routed["lakehouse"] == "LakehouseExport"
    assert STATES["PipelineModeChoice"]["Choices"][0]["Next"] == "DataAudit"


def test_a_failed_glue_job_escalates_and_only_a_concurrency_refusal_is_retried():
    """A failed DQ rule fails its job (the Silver and Gold jobs raise after writing the report).
    That is a fact about the data, so no remediation loop and no blanket retry may absorb it."""
    for name, st in GLUE_STATES.items():
        assert st["Catch"] == [{"ErrorEquals": ["States.ALL"], "ResultPath": "$.error", "Next": "EscalateFail"}], name
        assert [r["ErrorEquals"] for r in st.get("Retry", [])] == [["Glue.ConcurrentRunsExceededException"]], name
        assert st["ResultPath"] is None, f"{name} must not replace the state with the job run"
    review = STATES["LakehouseReview"]
    assert review["Catch"][0]["Next"] == "EscalateFail"
    pay = review["Parameters"]["Payload"]
    assert (pay["stage"], pay["task"], pay["harness_id"]) == ("data-prep", "lakehouse_review", "llmops_data_prep")


def test_the_pass_states_hand_the_lakehouse_object_back():
    for name in ("IncrementIteration", "IncrementIterationEval"):
        assert STATES[name]["Parameters"]["lakehouse.$"] == "$.lakehouse", name


# ── who may run it ───────────────────────────────────────────────────────────
def _statements(rel):
    doc = json.loads((REPO / rel).read_text())
    return doc.get("permissionsPolicy", doc)["Statement"]


def _actions(st):
    return st["Action"] if isinstance(st["Action"], list) else [st["Action"]]


def test_the_state_machine_role_can_run_exactly_the_lakehouse_jobs():
    st = next(s for s in _statements("deploy/iam/sfn_execution_role.json") if s.get("Sid") == "RunLakehouseJobs")
    assert set(_actions(st)) == {"glue:StartJobRun", "glue:GetJobRun", "glue:GetJobRuns", "glue:BatchStopJobRun"}
    pattern = st["Resource"].replace("<REGION>", "us-east-1").replace("<ACCOUNT_ID>", "1")
    for name in {s["Parameters"]["JobName"] for s in GLUE_STATES.values()}:
        assert fnmatch.fnmatch(f"arn:aws:glue:us-east-1:1:job/{name}", pattern), name
    assert not any(a.startswith("iam:") for s in _statements("deploy/iam/sfn_execution_role.json") for a in _actions(s))


def test_agents_read_reports_and_extracts_and_can_neither_write_them_nor_run_a_job():
    """All seven harnesses share this role, so Gold read is fleet-wide; the tables, the jobs and the
    keys are not. A writer of lakehouse/extracts/ could swap the bytes after the driver hashed them."""
    sts = _statements("deploy/iam/harness_execution_role.json")
    ro = next(s for s in sts if s.get("Sid") == "S3LakehouseReadOnly")
    assert _actions(ro) == ["s3:GetObject"]
    for s in sts:
        res = s.get("Resource", [])
        res = [res] if isinstance(res, str) else res
        if any("/lakehouse/" in r for r in res):
            assert set(_actions(s)) <= {"s3:GetObject"}, s.get("Sid")
        assert not any(a.split(":")[0] in ("glue", "athena", "s3tables") for a in _actions(s)), s.get("Sid")


def test_the_training_job_reads_the_extract_and_cannot_write_it():
    sts = _statements("deploy/iam/sagemaker_execution_role.json")
    read = next(s for s in sts if s.get("Sid") == "S3GoldExtractsRead")
    assert _actions(read) == ["s3:GetObject"] and read["Resource"].endswith("/lakehouse/extracts/*")
    lst = next(s for s in sts if s.get("Sid") == "S3RunsList")
    assert "lakehouse/extracts/*" in lst["Condition"]["StringLike"]["s3:prefix"]


def test_the_generator_writes_landing_and_nothing_downstream_of_it():
    pol = lh.generator_role_policy("us-east-1", "1", "data")
    writes = [r for s in pol["Statement"] if "s3:PutObject" in _actions(s)
              for r in ([s["Resource"]] if isinstance(s["Resource"], str) else s["Resource"])]
    assert writes == [f"arn:aws:s3:::data/{lh.LANDING_PREFIX}/*"]
    assert not any(a.split(":")[0] in ("s3tables", "kms", "glue", "athena", "bedrock")
                   for s in pol["Statement"] for a in _actions(s))
    transforms = lh.glue_role_policy("us-east-1", "1", "data", {lh.RAW: "k1", lh.CURATED: "k2"})
    assert not any("s3:PutObject" in _actions(s) and f"/{lh.LANDING_PREFIX}/" in json.dumps(s["Resource"])
                   for s in transforms["Statement"]), "the transforms must still not write their own input"
    cases = lh.generator_role_cases("us-east-1", "1", "data", {lh.RAW: "arn:raw", lh.CURATED: "arn:cur"},
                                    {lh.RAW: "k1", lh.CURATED: "k2"})
    assert sum(1 for *_, want in cases if not want) >= 6, "the denials are what prove the scoping"


# ── dispatch ─────────────────────────────────────────────────────────────────
def test_the_dispatcher_knows_the_generators_scales():
    assert tuple(sorted(sp.LAKEHOUSE_SCALES)) == tuple(sorted(gen.SCALES))


def test_an_ingest_names_its_dataset_after_itself():
    run_id = sp.new_run_id()
    lake = sp.lakehouse_input({"pipeline_mode": "lakehouse_ingest", "generator_seed": 7,
                               "generator_scale": "small"}, run_id, "b")
    assert lake == {"dataset": run_id.lower(), "data_bucket": "b", "seed": "7", "scale": "small"}
    assert sp.LAKEHOUSE_DATASET_ID.match(lake["dataset"])
    assert sp.lakehouse_input({}, run_id, "b") is None
    assert sp.lakehouse_input({"pipeline_mode": "eval_only"}, run_id, "b") is None


def _seed(params, plan=None):
    return sp.seed_manifest("run-x", "test", params, plan or {})


GATES = {"gates": {"answer_match": 0.8}, "dataset": "store-copilot"}


@pytest.mark.parametrize("params,why", [
    ({"pipeline_mode": "lakehouse_ingest", "generator_scale": "demo"}, "generator_seed"),
    ({"pipeline_mode": "lakehouse_ingest", "generator_seed": 7}, "generator_scale"),
    ({"pipeline_mode": "lakehouse_ingest", "generator_seed": True, "generator_scale": "demo"}, "integer"),
    ({"pipeline_mode": "lakehouse_ingest", "generator_seed": "7; rm", "generator_scale": "demo"}, "integer"),
    ({"pipeline_mode": "lakehouse_ingest", "generator_seed": -1, "generator_scale": "demo"}, "integer"),
    ({"pipeline_mode": "lakehouse_ingest", "generator_seed": 7, "generator_scale": "huge"}, "scale"),
    ({"pipeline_mode": "lakehouse", "models": {"student": "Qwen/Qwen3-1.7B"}, **GATES}, "lakehouse_dataset"),
    ({"pipeline_mode": "lakehouse", "lakehouse_dataset": "../customer-data",
      "models": {"student": "Qwen/Qwen3-1.7B"}, **GATES}, "lakehouse_dataset"),
    ({"pipeline_mode": "lakehouse", "lakehouse_dataset": "Seed7", "models": {"student": "Qwen/Qwen3-1.7B"},
      **GATES}, "lakehouse_dataset"),
    ({"pipeline_mode": "lakehouse", "lakehouse_dataset": "seed7-demo-x", **GATES}, "student"),
    ({"pipeline_mode": "lakehouse", "lakehouse_dataset": "seed7-demo-x",
      "models": {"student": "Qwen/Qwen3-1.7B"}}, "gates"),
])
def test_dispatch_refuses_what_a_glue_job_would_fail_on_a_stage_later(params, why):
    with pytest.raises(ValueError, match=why):
        _seed(params)


def test_a_complete_lakehouse_dispatch_seeds():
    m = _seed({"pipeline_mode": "lakehouse", "lakehouse_dataset": "seed7-demo-x",
               "models": {"student": "Qwen/Qwen3-1.7B"}, **GATES})
    assert m["params"]["lakehouse_dataset"] == "seed7-demo-x"
    m = _seed({"pipeline_mode": "lakehouse_ingest", "generator_seed": 7, "generator_scale": "demo"})
    assert m["params"]["generator_seed"] == 7


class _Rec:
    def __init__(self):
        self.calls = []

    def __getattr__(self, op):
        return lambda **kw: self.calls.append((op, kw)) or {"executionArn": "arn:x"}

    def Table(self, name):  # noqa: N802 (boto3's spelling)
        return self


def test_the_execution_input_carries_the_lakehouse_object(monkeypatch):
    for k, v in {"DATA_BUCKET": "data-bucket", "RUNS_TABLE": "runs", "EVENT_BUS": "bus",
                 "STATE_MACHINE_ARN": "arn:sm"}.items():
        monkeypatch.setenv(k, v)
    clients = {"s3": _Rec(), "ddb": _Rec(), "events": _Rec(), "sfn": _Rec()}
    out = sp.handler({"trigger_source": "test", "params": {
        "pipeline_mode": "lakehouse_ingest", "generator_seed": 7, "generator_scale": "demo"}}, clients=clients)
    (op, kw), = [c for c in clients["sfn"].calls if c[0] == "start_execution"]
    inp = json.loads(kw["input"])
    assert inp["lakehouse"] == {"dataset": out["run_id"].lower(), "data_bucket": "data-bucket",
                                "seed": "7", "scale": "demo"}
    sp.handler({"trigger_source": "test", "params": {}}, clients=clients)
    assert json.loads(clients["sfn"].calls[-1][1]["input"])["lakehouse"] is None


# ── the review ───────────────────────────────────────────────────────────────
def _prompt(agent):
    return json.loads((REPO / "agents" / agent / "harness.json").read_text())["systemPrompt"][0]["text"]


def test_the_review_is_declared_and_says_what_it_may_never_do():
    text = _prompt("data-prep")
    body = re.search(r'\n- "lakehouse_review":(.*?)(?=\n- "[a-z_]+":|\nRules:)', text, re.S).group(1)
    for must in ("escalate_human", "never edit, re-split, re-dedupe or re-filter", "extract_uri",
                 "extract_sha256", "extract_snapshot_id", 'source: "lakehouse"', "plant_check"):
        assert must in body, must
    assert "job_launched" not in body, "a review launches nothing; launching is the state machine's"
    assert driver.STAGE_EVENT_MAP[("data-prep", "lakehouse_review")] == driver.ev.DATASET_CURATED
    assert set(driver.GOLD_EXTRACT_CLAIM_KEYS) <= set(re.findall(r"extract_[a-z0-9_]+", body))


def test_finetune_and_eval_use_the_extract_verbatim():
    ft, ev_ = _prompt("finetune"), _prompt("eval")
    assert 'says source \\"lakehouse\\"' in json.dumps(ft) and "never re-split, re-dedupe or re-filter" in ft
    assert "val_uri" in ev_ and "gold.qlora_eval" in ev_


# ── the generator job, run with Glue and S3 stubbed ──────────────────────────
class _GenS3:
    def __init__(self, existing=()):
        self.objects = {k: b"x" for k in existing}

    def list_objects_v2(self, Bucket, Prefix, MaxKeys=1000):
        return {"KeyCount": sum(1 for k in self.objects if k.startswith(Prefix))}

    def put_object(self, Bucket, Key, Body, **_):
        self.objects[Key] = Body


def _run_generator(monkeypatch, s3, dataset="run-x", seed="7", scale="small"):
    argv = ["generate_landing.py", "--data_bucket", "b", "--dataset", dataset, "--seed", seed, "--scale", scale]
    utils = types.ModuleType("awsglue.utils")
    utils.getResolvedOptions = lambda a, names: {n: a[a.index(f"--{n}") + 1] for n in names}
    monkeypatch.setitem(sys.modules, "awsglue", types.ModuleType("awsglue"))
    monkeypatch.setitem(sys.modules, "awsglue.utils", utils)
    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=lambda svc: s3))
    monkeypatch.setitem(sys.modules, "generate", gen)
    monkeypatch.setattr(sys, "argv", argv)
    runpy.run_path(str(REPO / "pipeline" / "lakehouse" / "glue" / "generate_landing.py"), run_name="__main__")


def test_the_generator_job_writes_what_the_local_generator_writes_manifest_last(monkeypatch, capsys):
    s3 = _GenS3()
    _run_generator(monkeypatch, s3)
    with tempfile.TemporaryDirectory() as tmp:
        local = gen.write(gen.generate(7, "small"), pathlib.Path(tmp))
        files = {p.name: p.read_bytes() for p in pathlib.Path(tmp).iterdir()}
    landed = {k.rsplit("/", 1)[-1]: v for k, v in s3.objects.items()}
    assert landed == files and list(s3.objects)[-1].endswith("manifest.json")
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["dataset_sha256"] == local["dataset_sha256"]
    assert all(k.startswith(f"{lh.LANDING_PREFIX}/run-x/") for k in s3.objects)


@pytest.mark.parametrize("kw,match", [
    ({"dataset": "../customer-data"}, "lowercase id"), ({"scale": "huge"}, "scale"),
])
def test_the_generator_job_refuses_bad_arguments(monkeypatch, kw, match):
    with pytest.raises(SystemExit, match=match):
        _run_generator(monkeypatch, _GenS3(), **kw)


def test_the_generator_job_never_overwrites_a_dataset(monkeypatch):
    s3 = _GenS3(existing=[f"{lh.LANDING_PREFIX}/run-x/manifest.json"])
    with pytest.raises(SystemExit, match="refusing to overwrite"):
        _run_generator(monkeypatch, s3)
    assert list(s3.objects) == [f"{lh.LANDING_PREFIX}/run-x/manifest.json"]



def test_the_manifest_records_the_dataset_and_the_schema_accepts_it(monkeypatch):
    from test_manifest_schema import validate
    for k, v in {"DATA_BUCKET": "data-bucket", "RUNS_TABLE": "runs", "EVENT_BUS": "bus",
                 "STATE_MACHINE_ARN": "arn:sm"}.items():
        monkeypatch.setenv(k, v)
    clients = {"s3": _Rec(), "ddb": _Rec(), "events": _Rec(), "sfn": _Rec()}
    sp.handler({"trigger_source": "test", "params": {
        "pipeline_mode": "lakehouse_ingest", "generator_seed": 7, "generator_scale": "demo"}}, clients=clients)
    (_, put), = [c for c in clients["s3"].calls if c[0] == "put_object"]
    manifest = json.loads(put["Body"])
    inp = json.loads(next(kw for op, kw in clients["sfn"].calls if op == "start_execution")["input"])
    assert manifest["lakehouse"] == inp["lakehouse"] and manifest["lakehouse"]["dataset"] == manifest["run_id"].lower()
    schema = json.loads((REPO / "pipeline" / "contracts" / "manifest.schema.json").read_text())
    assert validate(manifest, schema) == []
    assert validate({**manifest, "lakehouse": {**manifest["lakehouse"], "dataset": "../x"}}, schema)
    assert validate({**manifest, "lakehouse": None}, schema) == []


def test_the_architecture_docs_count_the_modes_the_machine_routes():
    """The heading said "Four entry modes" while the machine routed six; a count in prose with no
    guard is a count that goes stale on the next mode."""
    routed = {next(t["StringEquals"] for t in c["And"] if "StringEquals" in t)
              for c in STATES["PipelineModeChoice"]["Choices"]} | {"full"}
    words = {"en": ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"],
             "zh": ["零", "一", "二", "三", "四", "五", "六", "七", "八", "九", "十"]}
    en = (REPO / "docs" / "ARCHITECTURE.md").read_text()
    zh = (REPO / "docs" / "ARCHITECTURE.zh-TW.md").read_text()
    assert f"**{words['en'][len(routed)].capitalize()} entry modes," in en
    assert f"**{words['zh'][len(routed)]}種入口模式" in zh
    for mode in routed:
        assert f"`{mode}`" in en and f"`{mode}`" in zh, mode


def test_the_review_claims_the_acceptance_set_and_eval_anchors_the_gate_to_it():
    """PR-11: a lakehouse gate is anchored to the held-out acceptance set, not to a val split drawn
    from the training phrasings, and the review hands the driver its digest to re-measure."""
    body = re.search(r'\n- "lakehouse_review":(.*?)(?=\n- "[a-z_]+":|\nRules:)', _prompt("data-prep"), re.S).group(1)
    assert set(driver.ACCEPTANCE_CLAIM_KEYS) <= set(re.findall(r"acceptance_[a-z0-9_]+", body))
    assert "never build one yourself" in body and "appears as a user turn of train.jsonl" in body
    ev_ = _prompt("eval")
    i = ev_.index("in a lakehouse run")
    clause = ev_[i:i + 600]
    assert clause.index("acceptance_uri") < clause.index("val_uri"), "acceptance first, val split only without it"


# ── a job that finished before its token was parked (found live in PR-11) ────
class _SM:
    def __init__(self, status, billed=0):
        self.status, self.billed = status, billed

    def describe_training_job(self, TrainingJobName):
        return {"TrainingJobStatus": self.status, "BillableTimeInSeconds": self.billed,
                "ModelArtifacts": {"S3ModelArtifacts": "s3://b/model.tar.gz"}, "FailureReason": "oom"}


class _Lam:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def invoke(self, **kw):
        if self.fail:
            raise RuntimeError("AccessDenied")
        self.calls.append(kw)


@pytest.mark.parametrize("status,billed", [("Completed", 900), ("Failed", 30), ("Stopped", 0)])
def test_a_job_already_terminal_when_parked_is_delivered_to_the_resume_lambda(status, billed):
    lam = _Lam()
    out = driver._deliver_if_already_terminal({"sagemaker": _SM(status, billed), "lambda": lam}, "run-x", "job-1")
    assert out == {"terminal_check": status, "late_delivery": True}
    (call,) = lam.calls
    assert call["FunctionName"] == "llmops-resume-pipeline" and call["InvocationType"] == "Event"
    detail = json.loads(call["Payload"])["detail"]
    assert detail["TrainingJobName"] == "job-1" and detail["TrainingJobStatus"] == status
    assert detail["BillingSecondsUsed"] == billed, "the capacity-stop rule reads billed seconds"


def test_the_delivered_event_is_one_the_resume_lambda_settles():
    """Shape parity: the resume handler must read what the driver sends exactly as it reads
    SageMaker's own event, or a late delivery would settle differently from a timely one."""
    resume = _load("resume_for_late", "orchestration/resume_pipeline/handler.py")
    lam = _Lam()
    driver._deliver_if_already_terminal({"sagemaker": _SM("Stopped", 0), "lambda": lam}, "run-x", "job-1")
    detail = json.loads(lam.calls[0]["Payload"])["detail"]
    assert resume._is_capacity_stop(detail) is True
    detail = json.loads(json.dumps({**detail, "BillingSecondsUsed": 120}))
    assert resume._is_capacity_stop(detail) is False


@pytest.mark.parametrize("sm,lam,want", [
    (_SM("InProgress"), _Lam(), {"terminal_check": "InProgress"}),
    (None, _Lam(), {"terminal_check": "skipped"}),
    (_SM("Completed"), _Lam(fail=True), {"terminal_check": "Completed", "late_delivery": False}),
])
def test_a_running_job_or_a_failed_check_changes_nothing(sm, lam, want):
    assert driver._deliver_if_already_terminal({"sagemaker": sm, "lambda": lam}, "run-x", "job-1") == want
    assert not lam.calls


def test_servicing_job_launched_checks_the_job_and_delivers_a_finished_one(monkeypatch):
    """Through handle_job_launched itself: the helper being correct is worth nothing if the
    handoff stops calling it, and the stranded run is what that looks like."""
    monkeypatch.setenv("RUNS_TABLE", "runs")
    monkeypatch.setenv("EVENT_BUS", "bus")

    class _Table:
        def __init__(self):
            self.updates = []

        def update_item(self, **kw):
            self.updates.append(kw)

    table, lam = _Table(), _Lam()
    c = {"ddb": types.SimpleNamespace(Table=lambda name: table), "events": types.SimpleNamespace(put_events=lambda **kw: {}),
         "sagemaker": _SM("Completed", 900), "lambda": lam}
    out = driver.handle_job_launched(c, {"run_id": "run-x", "stage": "finetune", "task_token": "tok"},
                                     {"job_name": "job-probe"})
    assert table.updates and table.updates[0]["ExpressionAttributeValues"][":t"] == "tok", "the token is parked first"
    assert out["released"] is True and out["late_delivery"] is True
    assert json.loads(lam.calls[0]["Payload"])["detail"]["TrainingJobName"] == "job-probe"
