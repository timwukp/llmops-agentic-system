"""The build runner's security posture, asserted offline (tools/runner/runner.py).

The runner holds the power to create VPCs, harnesses, Glue jobs and IAM roles, so the properties
that keep that power contained are pinned here rather than trusted: no inbound path, IMDSv2,
encryption, least privilege with a permissions boundary, and nothing local-only in the tree.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("runner", REPO / "tools" / "runner" / "runner.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)

ACCOUNT = "123456789012"  # documentation account
POL = runner._clean(runner.runner_policy("us-east-1", ACCOUNT, "data-bucket"))
BOUNDARY = runner.boundary_policy("us-east-1", ACCOUNT, "data-bucket")


def _actions(st):
    return st["Action"] if isinstance(st["Action"], list) else [st["Action"]]


def _resources(st):
    return st["Resource"] if isinstance(st["Resource"], list) else [st["Resource"]]


def test_launch_has_no_inbound_path_and_requires_imdsv2():
    p = runner.launch_params("ami-1", "subnet-1", "sg-1")
    assert "KeyName" not in p, "no SSH key: management is SSM only"
    nic = p["NetworkInterfaces"][0]
    assert nic["AssociatePublicIpAddress"] is False
    assert p["MetadataOptions"]["HttpTokens"] == "required" and p["MetadataOptions"]["HttpPutResponseHopLimit"] == 1
    ebs = p["BlockDeviceMappings"][0]["Ebs"]
    assert ebs["Encrypted"] is True and ebs["VolumeType"] == "gp3"


def test_user_data_holds_no_secret_and_pins_the_suite_versions():
    ud = runner.USER_DATA
    for needle in ("AKIA", "SECRET", "TOKEN", "PASSWORD", "ghp_"):
        assert needle not in ud.upper().replace("SECRETS", "")
    for pin in runner.PINS:
        assert pin in ud


def test_no_full_access_and_no_wildcard_admin():
    text = json.dumps(POL)
    assert "FullAccess" not in text
    for st in POL["Statement"]:
        assert not ("*" in _actions(st) and "*" in _resources(st)), st["Sid"]
        assert all(a != "iam:*" and not a.startswith("iam:Attach") for a in _actions(st)), st["Sid"]


def test_iam_role_creation_requires_the_permissions_boundary():
    creators = [st for st in POL["Statement"] if "iam:CreateRole" in _actions(st)]
    assert creators, "the spikes need to create roles"
    for st in creators:
        cond = st.get("Condition", {}).get("StringEquals", {})
        assert cond.get("iam:PermissionsBoundary", "").endswith(f":policy/{runner.BOUNDARY}"), st["Sid"]
        assert all(r.endswith("role/llmops-spike-*") for r in _resources(st))


def test_service_linked_role_creation_is_limited_to_route53_resolver():
    slr = [st for st in POL["Statement"] if "iam:CreateServiceLinkedRole" in _actions(st)]
    assert slr, "Resolver query logging needs its service-linked role; the grant is gone"
    for st in slr:
        assert st.get("Condition", {}).get("StringEquals", {}).get("iam:AWSServiceName") == \
            "route53resolver.amazonaws.com", st
        assert all("/aws-service-role/route53resolver.amazonaws.com/" in r for r in _resources(st)), st


def test_the_boundary_cannot_create_or_change_iam():
    for st in BOUNDARY["Statement"]:
        assert not any(a.startswith(("iam:", "sts:AssumeRole")) for a in _actions(st)), st["Sid"]


def test_every_mutating_ec2_statement_is_tag_or_creation_scoped():
    for st in POL["Statement"]:
        acts = [a for a in _actions(st) if a.startswith("ec2:") and not a.startswith("ec2:Describe")]
        if not acts or st["Sid"] in ("Ec2EndpointServices", "Ec2SecurityGroupRules"):
            continue
        assert "Condition" in st, f"{st['Sid']} mutates EC2 without a tag/create-action condition"


def test_harness_and_glue_and_athena_access_is_prefix_scoped():
    for st in POL["Statement"]:
        if st["Sid"] in ("HarnessLifecycle", "SpikeGlueJobs", "SpikeAthena", "SpikeTables"):
            assert all("llmops_spike_" in r or "llmops-spike-" in r for r in _resources(st)), st["Sid"]


def test_the_tree_excludes_local_only_and_heavy_paths():
    for bad in (".venv/lib/x.py", ".git/config", "deploy/evidence/SPIKE_06.local.json",
                "lakehouse llm architecture.pdf", "tools/spikes/__pycache__/a.pyc"):
        assert runner.TREE_EXCLUDE.search(bad), bad
    assert not runner.TREE_EXCLUDE.search("tools/spikes/s6_vpc_harness.py")


def test_run_script_exports_the_boundary_and_refuses_quoted_uris():
    lines = runner.run_script("s3://b/t.tgz", "pytest -q", "r1", "s3://b/res/", "arn:aws:iam::x:policy/b")
    assert any("LLMOPS_SPIKE_BOUNDARY_ARN='arn:aws:iam::x:policy/b'" in ln for ln in lines)
    import pytest
    with pytest.raises(ValueError):
        runner.run_script("s3://b/t'.tgz", "x", "r", "s3://b/")


# ── identity guards (the hourly Quick Setup swap, 2026-10-13) ────────────────
def _assoc(name, state="associated"):
    return {"State": state, "AssociationId": "iip-assoc-1",
            "IamInstanceProfile": {"Arn": f"arn:aws:iam::{ACCOUNT}:instance-profile/{name}"}}


def test_profile_problem_names_a_swapped_or_missing_profile():
    assert runner.profile_problem([_assoc(runner.PROFILE)]) == ""
    assert "AmazonSSMRoleForInstancesQuickSetup" in runner.profile_problem(
        [_assoc("AmazonSSMRoleForInstancesQuickSetup")])
    assert runner.profile_problem([]) == "no instance profile is associated"
    assert runner.profile_problem([_assoc(runner.PROFILE, "disassociated")])


def _qs(force="true", targets=None, role="AmazonSSMRoleForInstancesQuickSetup", name=runner.CLOBBERING_DOCUMENT):
    return {"Name": name, "AssociationName": "SystemAssociationForManagingInstances",
            "ScheduleExpression": "rate(1 hour)",
            "Targets": targets if targets is not None else [{"Key": "InstanceIds", "Values": ["*"]}],
            "Parameters": {"ForceReplace": [force], "RoleName": [role]}}


def test_a_force_replace_association_over_every_instance_is_reported():
    """The live shape: InstanceIds=*, ForceReplace=true, hourly."""
    found = runner.clobbering_associations([_qs()], "i-1")
    assert len(found) == 1 and "ForceReplace=true" in found[0] and "targets=*" in found[0]


def test_only_associations_that_can_replace_this_runner_are_reported():
    i = "i-1"
    assert not runner.clobbering_associations([_qs(force="false")], i), "ForceReplace=false only fills gaps"
    assert not runner.clobbering_associations([_qs(role=runner.ROLE)], i), "it would attach our own role"
    assert not runner.clobbering_associations([_qs(targets=[{"Key": "InstanceIds", "Values": ["i-2"]}])], i)
    assert not runner.clobbering_associations([_qs(name="AWS-RunPatchBaseline")], i)
    assert runner.clobbering_associations([_qs(targets=[{"Key": "InstanceIds", "Values": [i]}])], i)
    # Undecidable from the definition, so reported rather than assumed harmless.
    assert runner.clobbering_associations([_qs(targets=[{"Key": "tag:env", "Values": ["dev"]}])], i)


def _script():
    return runner.run_script("s3://b/t.tgz", "pytest", "r1", "s3://b/res/", "arn:aws:iam::x:policy/b")


def test_a_job_refuses_to_start_under_another_identity_before_touching_anything():
    lines = _script()
    guard = next(i for i, ln in enumerate(lines) if "REFUSED" in ln)
    first_aws_write = next(i for i, ln in enumerate(lines) if "aws s3 cp" in ln)
    assert guard < first_aws_write, "the identity check must precede the tree download"
    assert f"'{runner.ROLE}'" in lines[guard] and "exit 5" in lines[guard]
    assert "assumed-role" in "\n".join(lines) and "--query Arn" in "\n".join(lines)
    # only the role name is printed: the ARN carries the account id
    assert "sed 's#.*:assumed-role/##" in "\n".join(lines)


def test_an_identity_change_during_the_job_is_recorded_and_reported():
    lines = _script()
    start = next(i for i, ln in enumerate(lines) if "IDENTITY CHANGED" in ln)
    job = next(i for i, ln in enumerate(lines) if "> job.log 2>&1" in ln)
    stop = next(i for i, ln in enumerate(lines) if ln.startswith("{ kill $W"))
    assert start < job < stop, "the watchdog must run for exactly the job's duration"
    assert lines[start].rstrip().endswith("& W=$!")
    assert "identity.log" in lines[-1], "a swap must reach the SSM output even if S3 uploads fail"
    assert "exit 6" in lines[-1] and lines[-1].endswith("exit $RC"), (
        "the command must exit with the job's code (6 on a swap), or SSM reports Success for a failure")


# ── every AWS call a spike makes is one the runner may make ──────────────────
import ast  # noqa: E402
import fnmatch  # noqa: E402

#: boto3 client name -> IAM service prefix, where they differ.
_IAM_PREFIX = {"bedrock-agentcore-control": "bedrock-agentcore", "bedrock-runtime": "bedrock",
               "resourcegroupstaggingapi": "tag"}
#: boto3 operations whose IAM action is not their CamelCase name.
_IAM_ACTION = {("s3", "list_objects_v2"): "ListBucket", ("s3", "head_object"): "GetObject",
               ("s3", "delete_objects"): "DeleteObject"}
_NOT_API = {"get_paginator", "get_waiter", "exceptions", "paginate", "wait", "meta"}


def spike_aws_calls(path: pathlib.Path) -> set[tuple[str, str]]:
    """(boto3 service, operation) for every call on a client the module obtains with
    self.client("<service>"). Names are mapped module-wide, so the module's helpers that take
    the same client as a parameter (`_wait_glue(glue, ...)`) are covered too."""
    tree = ast.parse(path.read_text())
    names: dict[str, str] = {}

    def service_of(node):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "client" and node.args
                and isinstance(node.args[0], ast.Constant)):
            return node.args[0].value
        return None

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            pairs = []
            for tgt in node.targets:
                if isinstance(tgt, ast.Tuple) and isinstance(node.value, ast.Tuple):
                    pairs += list(zip(tgt.elts, node.value.elts))
                else:
                    pairs.append((tgt, node.value))
            for tgt, val in pairs:
                svc = service_of(val)
                if svc and isinstance(tgt, ast.Name):
                    names[tgt.id] = svc
    calls = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        base, op = node.func.value, node.func.attr
        svc = service_of(base) or (names.get(base.id) if isinstance(base, ast.Name) else None)
        if svc and op not in _NOT_API and not op.startswith("_"):
            calls.add((svc, op))
        if svc and op == "get_paginator" and node.args and isinstance(node.args[0], ast.Constant):
            calls.add((svc, node.args[0].value))
    return calls


def iam_action(svc: str, op: str) -> str:
    return f"{_IAM_PREFIX.get(svc, svc)}:" + _IAM_ACTION.get(
        (svc, op), "".join(w.capitalize() for w in op.split("_")))


def _allowed(action: str) -> bool:
    return any(st["Effect"] == "Allow" and any(fnmatch.fnmatchcase(action.lower(), a.lower())
                                               for a in _actions(st))
               for st in POL["Statement"])


SPIKE_FILES = sorted((REPO / "tools" / "spikes").glob("s[0-9]_*.py"))


def test_the_call_scanner_sees_the_calls_that_have_failed_live():
    """A floor, so a scanner that stops matching cannot pass by finding nothing. Each of these
    was a live AccessDenied on the runner on 2026-10-13."""
    found = {iam_action(*c) for f in SPIKE_FILES for c in spike_aws_calls(f)}
    for a in ("route53resolver:CreateResolverQueryLogConfig", "ec2:CreateVpcEndpoint",
              "route53resolver:GetResolverQueryLogConfigAssociation", "logs:CreateLogGroup",
              "s3:ListBucket", "glue:CreateJob"):
        assert a in found, (a, sorted(found))


def test_every_aws_call_a_spike_makes_is_granted_to_the_runner():
    """Three runner runs in a row died on a call the spike made and the role did not grant --
    each found only after creating a VPC. This finds them before."""
    missing = sorted({f"{iam_action(*c)}  ({f.name})" for f in SPIKE_FILES
                      for c in spike_aws_calls(f) if not _allowed(iam_action(*c))})
    assert not missing, "the runner role cannot make these spike calls:\n  " + "\n  ".join(missing)


def test_the_jobs_own_files_are_excluded_from_the_rebuilt_index():
    """Live: the full suite on the runner failed test_an_unstaged_new_file_is_a_lying_census
    because job.log and pip.log are written into the tree after `git add -A`."""
    lines = _script()
    init = next(i for i, ln in enumerate(lines) if ln.startswith("git init"))
    excl = next(i for i, ln in enumerate(lines) if ".git/info/exclude" in ln)
    first_write = next(i for i, ln in enumerate(lines) if ">> pip.log" in ln or "> job.log" in ln)
    assert init < excl < first_write
    for name in ("job.log", "pip.log", "identity.log"):
        assert name in lines[excl]
