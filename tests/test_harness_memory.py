"""Every harness create says what memory it wants, because silence means MANAGED.

Live, 2026-10-13: CreateHarness without a `memory` field provisions managed memory, and a
probe reached READY only with `{"disabled": {}}`. Every agents/*/harness.json said "Created
WITHOUT memory" -- and the read-only audit the same day found that each of the seven llmops
harnesses had been given a managed memory at create, which 04_wire_memory.py replaced with
the shared BYO memory and UpdateHarness never deleted. The claim was false for its whole
life, and nothing checked it, because a `_memory_note` is a comment no code reads.

So the guards here derive from the configs, the role file and the call sites -- not from a
copy of the claim -- and the create path is driven end to end against a fake control plane.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import pathlib
import re
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
CONFIGS = sorted((REPO / "agents").glob("*/harness.json"))
#: The dummy account every test in this suite uses; never a real one.
ACCOUNT = "123456789012"
ROLE = f"arn:aws:iam::{ACCOUNT}:role/x"


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ch = _load("create_harness_mem", "deploy/create_harness.py")
h5 = _load("harnesses_mem", "deploy/05_harnesses.py")


def _cfg(path: pathlib.Path) -> dict:
    return json.loads(path.read_text())


def _shared_memory_name() -> str:
    """MEMORY_NAME as 04_wire_memory.py defines it, read from its AST (it imports boto3)."""
    tree = ast.parse((REPO / "deploy/04_wire_memory.py").read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "MEMORY_NAME" for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError("deploy/04_wire_memory.py no longer defines MEMORY_NAME")


# --- the configs ------------------------------------------------------------------------

def test_every_harness_config_declares_a_create_time_memory():
    assert len(CONFIGS) >= 7, f"expected the 7 agent configs, found {len(CONFIGS)}"
    for path in CONFIGS:
        mem = ch.create_time_memory(_cfg(path), where=str(path))
        # Managed is legal for the API and wrong for this fleet: every agent shares the BYO
        # memory, so a managed one would be an orphan from the moment 04 attaches.
        assert "managedMemoryConfiguration" not in mem, f"{path} creates MANAGED memory"


def test_each_memory_note_quotes_the_memory_its_config_declares():
    """The note is prose about the config; make it quote the config instead of paraphrase it.

    "Created WITHOUT memory" paraphrased the absence of a key, which is exactly where the
    API's default hid. Quoting the declared value verbatim means a change to the value
    fails here until the note is changed with it. The memory's NAME is checked the same way:
    six notes called it `llmops-shared-memory`, a spelling no memory ever had.
    """
    shared = _shared_memory_name()
    for path in CONFIGS:
        cfg = _cfg(path)
        note = cfg.get("_memory_note", "")
        declared = json.dumps(cfg["memory"])
        assert declared in note, f"{path}: _memory_note does not quote memory={declared}"
        if cfg["memory"] == ch.MEMORY_DISABLED:
            assert "deploy/04_wire_memory.py" in note, \
                f"{path}: memory is disabled at create but the note names no attach step"
            assert shared in note, f"{path}: the note does not name the shared memory {shared!r}"
        assert "WITHOUT memory" not in note, f"{path}: the note still says WITHOUT memory"


# --- every call site --------------------------------------------------------------------

def _create_harness_calls():
    """(file, enclosing function, call) for every `.create_harness(` call outside tests/."""
    found = []
    for top in ("deploy", "tools", "orchestration", "pipeline"):
        for path in sorted((REPO / top).rglob("*.py")):
            tree = ast.parse(path.read_text(), filename=str(path))
            for fn in ast.walk(tree):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for node in ast.walk(fn):
                    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                            and node.func.attr == "create_harness"):
                        found.append((path.relative_to(REPO), fn, node))
    return found


def _calls_named(fn, name: str) -> bool:
    return any(isinstance(n, ast.Call) and getattr(n.func, "id", getattr(n.func, "attr", None))
               == name for n in ast.walk(fn))


def test_every_create_harness_call_sends_memory():
    """A call site either passes `memory=` or validates the config it spreads in.

    Three call sites existed when this was written -- the generic creator, the fleet
    deployer and the S6 VPC spike -- and NONE sent memory. The spike's probe harness was
    the third, and the one nobody would have thought to check: it is torn down, but a
    managed memory is not a resource its teardown list knows about.
    """
    calls = _create_harness_calls()
    files = {str(f) for f, _, _ in calls}
    assert {"deploy/05_harnesses.py", "deploy/create_harness.py"} <= files, \
        f"the scan found no create in a file known to create harnesses: {sorted(files)}"
    missing = [f"{f}:{call.lineno} in {fn.name}()" for f, fn, call in calls
               if not any(k.arg == "memory" for k in call.keywords)
               and not _calls_named(fn, "create_time_memory")]
    assert not missing, (
        "create_harness called without memory: " + ", ".join(missing) + ". An absent "
        "`memory` provisions MANAGED memory; pass memory= or call create_time_memory().")


# --- create_time_memory -----------------------------------------------------------------

def test_an_absent_memory_is_refused_and_says_why():
    with pytest.raises(SystemExit) as exc:
        ch.create_time_memory({"harnessName": "x"}, where="x")
    assert "MANAGED" in str(exc.value)


@pytest.mark.parametrize("mem", [
    {}, {"disabled": {}, "managedMemoryConfiguration": {}}, {"disabled": {}, "extra": 1},
    {"notAKind": {}}, "disabled"])
def test_a_memory_that_is_not_exactly_one_union_member_is_refused(mem):
    with pytest.raises(SystemExit):
        ch.create_time_memory({"memory": mem}, where="x")


@pytest.mark.parametrize("kind", ["disabled", "managedMemoryConfiguration",
                                  "agentCoreMemoryConfiguration"])
def test_each_union_member_is_accepted(kind):
    assert ch.create_time_memory({"memory": {kind: {}}}, where="x") == {kind: {}}


def test_the_generic_creator_refuses_a_config_with_no_memory(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "h.json"
    cfg.write_text(json.dumps({"name": "probe", "_memory_note": "dropped"}))
    monkeypatch.setattr(sys, "argv", ["create_harness.py", "--config", str(cfg),
                                      "--role-arn", ROLE,
                                      "--dry-run"])
    assert ch.main() == 1
    assert "MANAGED" in capsys.readouterr().out


def test_the_generic_creator_sends_the_declared_memory(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "h.json"
    cfg.write_text(json.dumps({"name": "probe", "memory": {"disabled": {}}}))
    monkeypatch.setattr(sys, "argv", ["create_harness.py", "--config", str(cfg),
                                      "--role-arn", ROLE,
                                      "--dry-run"])
    assert ch.main() == 0
    out = capsys.readouterr().out
    sent = json.loads(out[out.index("{"):])
    assert sent["memory"] == {"disabled": {}}


# --- 05_harnesses.py create_or_update ----------------------------------------------------

class _Ctl:
    """A control plane with no harnesses until one is created; then it serves that one."""

    def __init__(self, existing=False, live_memory=None):
        self.created, self.updated = [], []
        self.existing = existing
        self.live_memory = live_memory
        self.live = {}

    def list_harnesses(self):
        if self.existing:
            return {"harnesses": [{"harnessId": "llmops_eval-abcdefghij",
                                   "harnessName": "llmops_eval"}]}
        return {"harnesses": []}

    def create_harness(self, **kw):
        self.created.append(kw)
        self.live = {k: v for k, v in kw.items() if k != "memory"}
        return {"harness": {"harnessId": "llmops_eval-abcdefghij"}}

    def update_harness(self, **kw):
        self.updated.append(kw)
        self.live = dict(kw)

    def tag_resource(self, **kw):
        pass

    def get_harness(self, harnessId):
        h = dict(self.live, harnessId=harnessId, status="READY", harnessVersion="1",
                 arn=f"arn:aws:bedrock-agentcore:us-east-1:{ACCOUNT}:harness/llmops_eval")
        if self.live_memory is not None:
            h["memory"] = self.live_memory
        return {"harness": h}


def _fleet_cfg(agent="eval"):
    """A real agent config as 05 would load it; placeholders resolved with a dummy account."""
    mapping = h5.config_subst.mapping_for(ACCOUNT, "us-east-1", None)
    return h5.load_config(agent, False, mapping)


def test_a_fresh_create_sends_the_configs_explicit_memory():
    ctl = _Ctl()
    out = h5.create_or_update(ctl, _fleet_cfg(), ROLE, dry=False)
    assert out["action"] == "created"
    assert ctl.created and ctl.created[0]["memory"] == {"disabled": {}}


def test_a_config_without_memory_is_refused_before_anything_is_created():
    cfg = _fleet_cfg()
    cfg.pop("memory")
    ctl = _Ctl()
    with pytest.raises(SystemExit) as exc:
        h5.create_or_update(ctl, cfg, ROLE, dry=False)
    assert not ctl.created, "the create went out before the missing memory was noticed"
    assert "MANAGED" in str(exc.value)


def test_a_dry_run_checks_memory_too():
    cfg = _fleet_cfg()
    assert h5.create_or_update(None, dict(cfg), None, dry=True)["memory"] == {"disabled": {}}
    cfg.pop("memory")
    with pytest.raises(SystemExit):
        h5.create_or_update(None, cfg, None, dry=True)


def test_a_create_that_comes_back_managed_fails_the_deploy():
    arn = f"arn:aws:bedrock-agentcore:us-east-1:{ACCOUNT}:memory/llmops_eval-abcdefghij"
    ctl = _Ctl(live_memory={"managedMemoryConfiguration": {"arn": arn}})
    with pytest.raises(SystemExit) as exc:
        h5.create_or_update(ctl, _fleet_cfg(), ROLE, dry=False)
    assert ctl.created and arn in str(exc.value)


def test_an_update_never_sends_memory():
    """The opt-out is create-time only. Sent on update it would DETACH the shared memory
    04_wire_memory.py attached -- every redeploy would silently wipe the fleet's memory."""
    ctl = _Ctl(existing=True, live_memory={"agentCoreMemoryConfiguration": {"arn": "m"}})
    h5.create_or_update(ctl, _fleet_cfg(), ROLE, dry=False)
    assert ctl.updated and "memory" not in ctl.updated[0]
    assert "memory" not in h5.UPDATED_FIELDS


@pytest.mark.parametrize("sent,live,leak", [
    ({"disabled": {}}, {}, None),
    ({"disabled": {}}, {"memory": None}, None),
    ({"disabled": {}}, {"memory": {"managedMemoryConfiguration": {"arn": "a"}}}, "a"),
    ({"managedMemoryConfiguration": {}}, {"memory": {"managedMemoryConfiguration": {"arn": "a"}}},
     None),
])
def test_managed_memory_leak_flags_only_unrequested_managed_memory(sent, live, leak):
    assert h5.managed_memory_leak(sent, live) == leak


# --- validate_config.py ------------------------------------------------------------------

def _validate_memory(cfg):
    vc = _load("validate_config_mem", "deploy/validate_config.py")
    vc.validate_memory(cfg)
    return vc.ERRORS


def test_the_linter_rejects_an_absent_memory_and_accepts_the_opt_out():
    assert any("MANAGED" in e for e in _validate_memory({}))
    assert _validate_memory({"memory": {"disabled": {}}}) == []
    assert _validate_memory({"memory": {"managedMemoryConfiguration": {}}}) == []
    assert _validate_memory({"memory": {"disabled": {}, "managedMemoryConfiguration": {}}})


# --- the doc claim -----------------------------------------------------------------------

def _agents_md_memory_gotcha() -> str:
    text = (REPO / "AGENTS.md").read_text()
    m = re.search(r"^- \*\*A create that omits `memory`.*?(?=^- \*\*)", text, re.M | re.S)
    assert m, "AGENTS.md lost its create-time memory gotcha"
    return m.group(0)


def _managed_memory_grant() -> str:
    role = json.loads((REPO / "deploy/iam/harness_execution_role.json").read_text())
    for st in _statements(role):
        if st.get("Sid") == "ManagedMemoryEvents":
            return st["Resource"].split(":memory/", 1)[1]
    raise AssertionError("harness_execution_role.json has no ManagedMemoryEvents statement")


def _statements(obj):
    if isinstance(obj, dict):
        if "Sid" in obj and "Effect" in obj:
            yield obj
        for v in obj.values():
            yield from _statements(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _statements(v)


def test_the_agents_md_memory_gotcha_matches_the_configs_and_the_role():
    """Count, opt-out shape and IAM pattern are each read from where they actually live."""
    para = _agents_md_memory_gotcha()
    disabled = sum(_cfg(p).get("memory") == ch.MEMORY_DISABLED for p in CONFIGS)
    m = re.search(r"All (\d+) harness configs declare `(\{[^`]*\})`", para)
    assert m, "the gotcha no longer states how many configs declare which memory"
    assert int(m.group(1)) == disabled == len(CONFIGS), \
        f"says {m.group(1)}, {disabled} of {len(CONFIGS)} configs declare {ch.MEMORY_DISABLED}"
    assert json.loads(m.group(2)) == ch.MEMORY_DISABLED
    grant = _managed_memory_grant()
    assert f"memory/{grant}" in para, \
        f"the role grants managed memory on memory/{grant}; the gotcha names a different one"
    # Both live name patterns, because the grant matches only one of them.
    assert "harness_<name>_" in para and "<harnessName>-" in para


# --- the pipeline finds the next orphan itself -------------------------------------------

def _sweep_clause() -> str:
    text = _cfg(REPO / "agents/monitor/harness.json")["systemPrompt"][0]["text"]
    lines = [ln for ln in text.splitlines() if ln.startswith('- "sweep"')]
    assert len(lines) == 1, "the sweep clause moved; re-anchor this guard"
    return lines[0]


def _role_statements() -> dict:
    role = json.loads((REPO / "deploy/iam/harness_execution_role.json").read_text())
    return {st["Sid"]: st for st in role["permissionsPolicy"]["Statement"]}


def _as_list(v):
    return [v] if isinstance(v, str) else list(v)


def test_the_sweep_is_told_to_find_memory_orphans():
    """The 7 orphans were found by a person calling AWS by hand, which is the work the
    pipeline exists to do. A grant nothing instructs the agent to use is the failure
    test_the_sweep_can_characterise_an_orphan_it_may_not_touch already records: the live
    sweep never took an AccessDenied, because it was never told to make the call."""
    clause = _sweep_clause()
    for cmd in ("aws bedrock-agentcore-control list-memories",
                "aws bedrock-agentcore-control list-harnesses",
                "aws bedrock-agentcore-control get-harness"):
        assert cmd in clause, f"the sweep clause never runs '{cmd}'"
    for token in ("harness_llmops_", "llmops_", "memory_orphans", "permission_gaps",
                  "do NOT delete any memory", "not assessed"):
        assert token in clause, f"the sweep clause lost {token!r}"


def test_the_memory_inventory_grant_reads_no_contents_and_no_foreign_config():
    """Enumerate account-wide, read config of OUR harnesses only, read no memory contents.

    The width is the argument, as for SageMakerList: an orphan is by definition something no
    harness of ours points at, so enumeration has to be account-wide. But GetHarness returns
    a whole config, env vars included, so it stays on harness/llmops_*; and nothing that
    reads inside a memory, or deletes one, is granted on a wildcard.
    """
    st = _role_statements()
    inv = st["AgentCoreInventoryForSweep"]
    assert set(_as_list(inv["Action"])) == {"bedrock-agentcore:ListMemories",
                                           "bedrock-agentcore:ListHarnesses"}
    own = st["AgentCoreHarnessReadOwn"]
    assert _as_list(own["Action"]) == ["bedrock-agentcore:GetHarness"]
    assert _as_list(own["Resource"]) == [
        "arn:aws:bedrock-agentcore:<REGION>:<ACCOUNT_ID>:harness/llmops_*"]

    content_or_mutation = {
        "bedrock-agentcore:GetMemory", "bedrock-agentcore:ListActors",
        "bedrock-agentcore:ListMemoryRecords", "bedrock-agentcore:GetMemoryRecord",
        "bedrock-agentcore:RetrieveMemoryRecords", "bedrock-agentcore:ListEvents",
        "bedrock-agentcore:GetEvent", "bedrock-agentcore:DeleteMemory",
        "bedrock-agentcore:UpdateMemory", "bedrock-agentcore:UpdateHarness",
        "bedrock-agentcore:DeleteHarness", "bedrock-agentcore:GetHarness"}
    wide = [sid for sid, s in st.items() if s.get("Effect") == "Allow"
            and any(r == "*" or r.endswith(":memory/*") or r.endswith(":harness/*")
                    for r in _as_list(s.get("Resource", [])))
            and (set(_as_list(s["Action"])) & content_or_mutation
                 or any(a in ("bedrock-agentcore:*", "*") for a in _as_list(s["Action"])))]
    assert not wide, f"statements grant memory contents, harness config or mutation wide: {wide}"
