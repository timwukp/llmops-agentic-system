#!/usr/bin/env python3
"""A secure EC2 build/test runner for long jobs that must outlive the operator's laptop.

Why: spikes and the full suite run for minutes to hours (Glue jobs, VPC endpoint creation,
AgentCore ENI release). Run from a laptop, closing the lid kills them halfway — and a spike
killed before teardown leaves billable resources. On the runner they run under SSM RunCommand,
which does not depend on the caller staying connected (owner decision 2026-10-13: a long-job
runner only; the agent itself still runs on the laptop).

Security posture (each point is asserted offline in tests/test_runner.py):
  * No inbound path at all. The security group has zero ingress rules; there is no SSH key and
    no public IP. Management is SSM only.
  * Private subnet, egress through a NAT gateway (pip and the public GitHub repo need it).
  * IMDSv2 required (hop limit 1), encrypted gp3 root volume, termination by this script only.
  * Least privilege. The instance role may create and delete only resources named llmops-spike-*
    or tagged project=llmops-agentic-system, and may create IAM roles only if they carry the
    llmops-spike-boundary permissions boundary — so nothing it creates can hold more than the
    boundary allows, and it cannot loosen the boundary itself. No *FullAccess policy anywhere.
  * Code arrives as a tarball of the operator's tree in S3 (no GitHub credentials on the box);
    results go back to S3. Nothing secret is in user data.

    python3 tools/runner/runner.py plan
    python3 tools/runner/runner.py up
    python3 tools/runner/runner.py push                   # upload the working tree, print its URI
    python3 tools/runner/runner.py exec --tree <s3-uri> --cmd "python3 tools/spikes/s6_vpc_harness.py run"
    python3 tools/runner/runner.py fetch --command-id <id>
    python3 tools/runner/runner.py down
"""
from __future__ import annotations

import argparse
import io
import json
import pathlib
import re
import subprocess
import sys
import tarfile
import time

REPO = pathlib.Path(__file__).resolve().parents[2]
NAME = "llmops-build-runner"
TAGS = {"project": "llmops-agentic-system", "component": "build-runner"}
VPC_CIDR = "10.44.0.0/16"
PUBLIC_CIDR, PRIVATE_CIDR = "10.44.0.0/24", "10.44.1.0/24"
AZ_ID = "use1-az1"
INSTANCE_TYPE = "m7i.large"
AMI_PARAM = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
ROOT_GB = 60
ROLE = f"{NAME}-role"
PROFILE = f"{NAME}-profile"
BOUNDARY = "llmops-spike-boundary"
#: numpy is a real test dependency (pipeline/v2/verify_sandbox.py imports it), as in ci.yml.
PINS = ["boto3==1.43.57", "botocore==1.43.57", "pytest==9.1.1", "numpy==2.5.1"]
MAX_COMMAND_S = 48 * 3600
TREE_EXCLUDE = re.compile(r"(^|/)(\.venv|\.git|__pycache__|node_modules)(/|$)|\.local\.|\.pdf$")

USER_DATA = """#!/bin/bash
set -euo pipefail
dnf install -y git python3.12 python3.12-pip tar gzip jq nodejs
python3.12 -m venv /opt/llmops/venv
/opt/llmops/venv/bin/pip install --upgrade pip
/opt/llmops/venv/bin/pip install {pins}
mkdir -p /opt/llmops/runs
echo ready > /opt/llmops/READY
""".format(pins=" ".join(PINS))


# ── IAM documents ─────────────────────────────────────────────────────────────
def _arns(region, acct):
    return {"ec2": f"arn:aws:ec2:{region}:{acct}", "iam": f"arn:aws:iam::{acct}",
            "glue": f"arn:aws:glue:{region}:{acct}", "ac": f"arn:aws:bedrock-agentcore:{region}:{acct}",
            "logs": f"arn:aws:logs:{region}:{acct}", "s3t": f"arn:aws:s3tables:{region}:{acct}",
            "athena": f"arn:aws:athena:{region}:{acct}", "bedrock": f"arn:aws:bedrock:{region}:{acct}",
            "r53r": f"arn:aws:route53resolver:{region}:{acct}", "ssm": f"arn:aws:ssm:{region}:{acct}"}


TAG_COND = {"StringEquals": {"aws:ResourceTag/project": "llmops-agentic-system"}}
REQ_TAG_COND = {"StringEquals": {"aws:RequestTag/project": "llmops-agentic-system"}}


def boundary_policy(region: str, acct: str, data_bucket: str) -> dict:
    """The ceiling for every role the runner creates (the spikes' Glue roles). Only what a Glue
    job writing spike data needs; nothing that can create or modify IAM."""
    a = _arns(region, acct)
    return {"Version": "2012-10-17", "Statement": [
        {"Sid": "SpikeData", "Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject", "s3:ListBucket"],
         "Resource": [f"arn:aws:s3:::{data_bucket}", f"arn:aws:s3:::{data_bucket}/spikes/*"]},
        {"Sid": "SpikeTables", "Effect": "Allow", "Action": ["s3tables:Get*", "s3tables:PutTableData",
                                                              "s3tables:UpdateTableMetadataLocation"],
         "Resource": [f"{a['s3t']}:bucket/llmops-spike-*"]},
        {"Sid": "SpikeCatalog", "Effect": "Allow", "Action": ["glue:GetCatalog", "glue:GetDatabase",
                                                               "glue:GetTable", "glue:UpdateTable"],
         "Resource": [f"{a['glue']}:catalog", f"{a['glue']}:catalog/*", f"{a['glue']}:database/*",
                      f"{a['glue']}:table/*"]},
        {"Sid": "GlueLogs", "Effect": "Allow", "Action": ["logs:CreateLogGroup", "logs:CreateLogStream",
                                                           "logs:PutLogEvents"],
         "Resource": f"{a['logs']}:log-group:/aws-glue/*"},
    ]}


def runner_policy(region: str, acct: str, data_bucket: str) -> dict:
    """What spikes S1-S7 and the offline suite need, scoped by name prefix or project tag."""
    a = _arns(region, acct)
    boundary_arn = f"{a['iam']}:policy/{BOUNDARY}"
    ec2_tagged_types = ["vpc", "subnet", "security-group", "vpc-endpoint", "internet-gateway",
                        "route-table", "natgateway", "elastic-ip"]
    return {"Version": "2012-10-17", "Statement": [
        {"Sid": "RunnerArtifacts", "Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
         "Resource": [f"arn:aws:s3:::{data_bucket}/runner/*", f"arn:aws:s3:::{data_bucket}/spikes/*"]},
        {"Sid": "RunnerList", "Effect": "Allow", "Action": "s3:ListBucket", "Resource": f"arn:aws:s3:::{data_bucket}",
         "Condition": {"StringLike": {"s3:prefix": ["runner/*", "spikes/*", "skills/*"]}}},
        {"Sid": "SkillsRead", "Effect": "Allow", "Action": "s3:GetObject",
         "Resource": f"arn:aws:s3:::{data_bucket}/skills/*"},
        {"Sid": "ReadParams", "Effect": "Allow", "Action": ["ssm:GetParameter", "ssm:GetParameters"],
         "Resource": f"{a['ssm']}:parameter/llmops/*"},
        {"Sid": "Identity", "Effect": "Allow", "Action": ["sts:GetCallerIdentity", "tag:GetResources"], "Resource": "*"},
        {"Sid": "Ec2Read", "Effect": "Allow", "Action": ["ec2:Describe*"], "Resource": "*"},
        {"Sid": "Ec2CreateTagged", "Effect": "Allow",
         "Action": ["ec2:CreateVpc", "ec2:CreateSubnet", "ec2:CreateSecurityGroup", "ec2:CreateVpcEndpoint",
                    "ec2:CreateInternetGateway", "ec2:CreateRouteTable", "ec2:CreateNatGateway",
                    "ec2:AllocateAddress"],
         "Resource": [f"{a['ec2']}:{t}/*" for t in ec2_tagged_types], "Condition": REQ_TAG_COND},
        {"Sid": "Ec2TagOnCreate", "Effect": "Allow", "Action": "ec2:CreateTags",
         "Resource": [f"{a['ec2']}:{t}/*" for t in ec2_tagged_types],
         "Condition": {"StringEquals": {"ec2:CreateAction": [
             "CreateVpc", "CreateSubnet", "CreateSecurityGroup", "CreateVpcEndpoint", "CreateInternetGateway",
             "CreateRouteTable", "CreateNatGateway", "AllocateAddress"]}}},
        {"Sid": "Ec2InsideTaggedVpc", "Effect": "Allow",
         "Action": ["ec2:CreateSubnet", "ec2:CreateSecurityGroup", "ec2:CreateVpcEndpoint", "ec2:CreateRouteTable",
                    "ec2:CreateNatGateway", "ec2:ModifyVpcAttribute", "ec2:AttachInternetGateway",
                    "ec2:DetachInternetGateway", "ec2:CreateRoute", "ec2:DeleteRoute", "ec2:AssociateRouteTable",
                    "ec2:DisassociateRouteTable", "ec2:AuthorizeSecurityGroupIngress",
                    "ec2:RevokeSecurityGroupEgress", "ec2:DeleteVpc", "ec2:DeleteSubnet",
                    "ec2:DeleteSecurityGroup", "ec2:DeleteVpcEndpoints", "ec2:DeleteInternetGateway",
                    "ec2:DeleteRouteTable", "ec2:DeleteNatGateway", "ec2:ReleaseAddress"],
         "Resource": [f"{a['ec2']}:{t}/*" for t in ec2_tagged_types + ["route-table"]],
         "Condition": TAG_COND},
        {"Sid": "Ec2EndpointServices", "Effect": "Allow", "Action": "ec2:CreateVpcEndpoint",
         "Resource": f"arn:aws:ec2:{region}::prefix-list/*"},
        {"Sid": "Ec2SecurityGroupRules", "Effect": "Allow", "Action": "ec2:AuthorizeSecurityGroupIngress",
         "Resource": f"{a['ec2']}:security-group-rule/*"},
        {"Sid": "HarnessLifecycle", "Effect": "Allow",
         "Action": ["bedrock-agentcore:CreateHarness", "bedrock-agentcore:GetHarness",
                    "bedrock-agentcore:DeleteHarness", "bedrock-agentcore:InvokeHarness",
                    "bedrock-agentcore:InvokeAgentRuntime", "bedrock-agentcore:TagResource",
                    # A harness API authorizes against its underlying runtime too (AGENTS.md).
                    "bedrock-agentcore:CreateAgentRuntime", "bedrock-agentcore:GetAgentRuntime",
                    "bedrock-agentcore:UpdateAgentRuntime", "bedrock-agentcore:DeleteAgentRuntime",
                    "bedrock-agentcore:GetAgentRuntimeEndpoint", "bedrock-agentcore:DeleteAgentRuntimeEndpoint"],
         "Resource": [f"{a['ac']}:harness/llmops_spike_*", f"{a['ac']}:runtime/harness_llmops_spike_*"]},
        # CreateHarness authorizes against harness/* -- the ARN (harness/<name>-<suffix>) does not
        # exist yet -- so the name prefix above cannot scope it (live AccessDenied, run 3). The tag
        # every spike harness is created with scopes it instead (aws:RequestTag is honoured: the
        # probe got past it to the implicit TagResource, which authorizes against harness/* too).
        # Tagging a harness that is not ours grants nothing: every later call is name-scoped above.
        # Its runtime, runtime endpoint and workload identity are created the same way, each
        # against its own `*` ARN (found one at a time by probe, 2026-10-13).
        {"Sid": "HarnessCreateTagged", "Effect": "Allow",
         "Action": ["bedrock-agentcore:CreateHarness", "bedrock-agentcore:CreateAgentRuntime",
                    "bedrock-agentcore:CreateAgentRuntimeEndpoint", "bedrock-agentcore:CreateWorkloadIdentity",
                    "bedrock-agentcore:TagResource"],
         "Resource": [f"{a['ac']}:harness/*", f"{a['ac']}:runtime/*",
                      f"{a['ac']}:workload-identity-directory/default",
                      f"{a['ac']}:workload-identity-directory/default/workload-identity/*"],
         "Condition": REQ_TAG_COND},
        {"Sid": "HarnessWorkloadIdentity", "Effect": "Allow",
         "Action": ["bedrock-agentcore:CreateWorkloadIdentity", "bedrock-agentcore:DeleteWorkloadIdentity",
                    "bedrock-agentcore:GetWorkloadIdentity"],
         "Resource": [f"{a['ac']}:workload-identity-directory/default",
                      f"{a['ac']}:workload-identity-directory/default/workload-identity/harness_llmops_spike_*"]},
        {"Sid": "HarnessList", "Effect": "Allow", "Action": "bedrock-agentcore:ListHarnesses", "Resource": "*"},
        {"Sid": "PassHarnessRole", "Effect": "Allow", "Action": ["iam:PassRole", "iam:GetRole"],
         "Resource": f"{a['iam']}:role/llmops-harness-execution",
         "Condition": {"StringEqualsIfExists": {"iam:PassedToService": "bedrock-agentcore.amazonaws.com"}}},
        {"Sid": "SpikeRolesOnlyWithBoundary", "Effect": "Allow", "Action": ["iam:CreateRole", "iam:PutRolePolicy"],
         "Resource": f"{a['iam']}:role/llmops-spike-*",
         "Condition": {"StringEquals": {"iam:PermissionsBoundary": boundary_arn}}},
        {"Sid": "SpikeRolesManage", "Effect": "Allow",
         "Action": ["iam:GetRole", "iam:TagRole", "iam:DeleteRolePolicy", "iam:DeleteRole"],
         "Resource": f"{a['iam']}:role/llmops-spike-*"},
        {"Sid": "PassSpikeRolesToGlue", "Effect": "Allow", "Action": "iam:PassRole",
         "Resource": f"{a['iam']}:role/llmops-spike-*",
         "Condition": {"StringEquals": {"iam:PassedToService": "glue.amazonaws.com"}}},
        {"Sid": "SpikeTables", "Effect": "Allow", "Action": ["s3tables:*"],
         "Resource": [f"{a['s3t']}:bucket/llmops-spike-*", f"{a['s3t']}:bucket/llmops-spike-*/table/*"]},
        {"Sid": "SpikeTablesList", "Effect": "Allow", "Action": "s3tables:ListTableBuckets", "Resource": "*"},
        {"Sid": "TableData", "Effect": "Allow", "Action": ["s3:GetObject", "s3:ListBucket"],
         "Resource": ["arn:aws:s3:::*--table-s3", "arn:aws:s3:::*--table-s3/*"]},
        {"Sid": "SpikeGlueJobs", "Effect": "Allow",
         "Action": ["glue:CreateJob", "glue:DeleteJob", "glue:GetJob", "glue:StartJobRun", "glue:GetJobRun",
                    "glue:TagResource"],
         "Resource": f"{a['glue']}:job/llmops-spike-*"},
        {"Sid": "CatalogRead", "Effect": "Allow", "Action": ["glue:GetCatalog", "glue:GetCatalogs",
                                                             "glue:GetDatabase", "glue:GetDatabases", "glue:GetTable"],
         "Resource": [f"{a['glue']}:catalog", f"{a['glue']}:catalog/*", f"{a['glue']}:database/*",
                      f"{a['glue']}:table/*"]},
        {"Sid": "SpikeAthena", "Effect": "Allow",
         "Action": ["athena:CreateWorkGroup", "athena:DeleteWorkGroup", "athena:GetWorkGroup",
                    "athena:StartQueryExecution", "athena:GetQueryExecution", "athena:GetQueryResults",
                    "athena:TagResource"],
         "Resource": f"{a['athena']}:workgroup/llmops-spike-*"},
        {"Sid": "SpikeGuardrails", "Effect": "Allow",
         "Action": ["bedrock:CreateGuardrail", "bedrock:TagResource"], "Resource": "*", "Condition": REQ_TAG_COND},
        {"Sid": "SpikeGuardrailsManage", "Effect": "Allow",
         "Action": ["bedrock:DeleteGuardrail", "bedrock:GetGuardrail", "bedrock:ApplyGuardrail"],
         "Resource": f"{a['bedrock']}:guardrail/*", "Condition": TAG_COND},
        {"Sid": "GuardrailList", "Effect": "Allow", "Action": "bedrock:ListGuardrails", "Resource": "*"},
        {"Sid": "SpikeLogs", "Effect": "Allow",
         "Action": ["logs:CreateLogGroup", "logs:DeleteLogGroup", "logs:PutRetentionPolicy", "logs:TagResource",
                    "logs:FilterLogEvents", "logs:DescribeLogGroups"],
         "Resource": [f"{a['logs']}:log-group:/llmops/spike/*", f"{a['logs']}:log-group:/llmops/spike/*:*",
                      f"{a['logs']}:log-group:/aws/bedrock-agentcore/runtimes/harness_llmops_spike_*"]},
        {"Sid": "ResolverLogDelivery", "Effect": "Allow",
         "Action": ["logs:CreateLogDelivery", "logs:GetLogDelivery", "logs:UpdateLogDelivery",
                    "logs:DeleteLogDelivery", "logs:ListLogDeliveries", "logs:PutResourcePolicy",
                    # Resolver checks the destination with an account-level DescribeLogGroups; scoped
                    # to /llmops/spike/* it fails RSLVR-01605 "Missing permission to log destination"
                    "logs:DescribeResourcePolicies", "logs:DescribeLogGroups"], "Resource": "*"},
        {"Sid": "SpikeResolverLogs", "Effect": "Allow",
         "Action": ["route53resolver:CreateResolverQueryLogConfig", "route53resolver:DeleteResolverQueryLogConfig",
                    "route53resolver:AssociateResolverQueryLogConfig",
                    "route53resolver:DisassociateResolverQueryLogConfig",
                    "route53resolver:ListResolverQueryLogConfigAssociations",
                    "route53resolver:GetResolverQueryLogConfigAssociation", "route53resolver:TagResource"],
         "Resource": f"{a['r53r']}:resolver-query-log-config/*"},
        # The first query-log config in an account creates Resolver's service-linked role
        # (RSLVR-01604 without it). Persistent and account-level, like every SLR; this grant
        # can create that one role and no other.
        {"Sid": "ResolverServiceLinkedRole", "Effect": "Allow", "Action": "iam:CreateServiceLinkedRole",
         "Resource": f"{a['iam']}:role/aws-service-role/route53resolver.amazonaws.com/*",
         "Condition": {"StringEquals": {"iam:AWSServiceName": "route53resolver.amazonaws.com"}}},
    ]}


def _clean(doc: dict) -> dict:
    for st in doc["Statement"]:
        if st.get("Condition") is None:
            st.pop("Condition", None)
    return doc


def trust_policy() -> dict:
    return {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": "ec2.amazonaws.com"},
                                                     "Action": "sts:AssumeRole"}]}


def launch_params(ami: str, subnet: str, sg: str) -> dict:
    """RunInstances arguments. IMDSv2, encrypted root, no key pair, no public IP."""
    return {"ImageId": ami, "InstanceType": INSTANCE_TYPE, "MinCount": 1, "MaxCount": 1,
            "IamInstanceProfile": {"Name": PROFILE},
            "NetworkInterfaces": [{"DeviceIndex": 0, "SubnetId": subnet, "Groups": [sg],
                                   "AssociatePublicIpAddress": False}],
            "MetadataOptions": {"HttpTokens": "required", "HttpPutResponseHopLimit": 1, "HttpEndpoint": "enabled"},
            "BlockDeviceMappings": [{"DeviceName": "/dev/xvda", "Ebs": {
                "VolumeSize": ROOT_GB, "VolumeType": "gp3", "Encrypted": True, "DeleteOnTermination": True}}],
            "UserData": USER_DATA,
            "TagSpecifications": [{"ResourceType": t, "Tags": _tags(NAME)} for t in ("instance", "volume")]}


def _tags(name):
    return [{"Key": k, "Value": v} for k, v in {**TAGS, "Name": name}.items()]


# ── tree packaging ───────────────────────────────────────────────────────────
def tree_files(repo: pathlib.Path = REPO) -> list[str]:
    """Tracked + untracked-but-not-ignored files, minus the venv, git dir and local-only output."""
    out = subprocess.check_output(["git", "ls-files", "--cached", "--others", "--exclude-standard"],
                                  cwd=repo, text=True)
    return sorted(f for f in out.splitlines() if f and not TREE_EXCLUDE.search(f) and (repo / f).is_file())


def pack_tree(repo: pathlib.Path = REPO) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for f in tree_files(repo):
            tar.add(repo / f, arcname=f)
    return buf.getvalue()


#: An SSM association that replaces instance profiles. Live on 2026-10-13 the account's legacy
#: Quick Setup ran AWS-AttachIAMToInstance hourly over InstanceIds=* with ForceReplace=true: at
#: :01 it swapped this runner's role for AmazonSSMRoleForInstancesQuickSetup (and did the same to
#: six unrelated instances every hour). Jobs dispatched after that failed AccessDenied before
#: creating anything -- but a long spike that crossed :01 would have lost its identity mid-run,
#: and with it the permissions its own teardown needs.
CLOBBERING_DOCUMENT = "AWS-AttachIAMToInstance"


def profile_problem(associations: list[dict]) -> str:
    """'' when the instance holds exactly this runner's profile; otherwise why it does not.
    `associations` is DescribeIamInstanceProfileAssociations' list for one instance."""
    live = [a for a in associations if a.get("State") in ("associated", "associating")]
    if not live:
        return "no instance profile is associated"
    name = live[0].get("IamInstanceProfile", {}).get("Arn", "").rsplit("/", 1)[-1]
    return "" if name == PROFILE else f"instance profile is {name}, not {PROFILE}"


def clobbering_associations(described: list[dict], instance_id: str) -> list[str]:
    """Associations that can replace this runner's profile, from DescribeAssociation results.
    Tag- or resource-group-targeted ones are reported too: whether they match is not decidable
    from the definition, and a guard that guesses "no" is the failure it exists to prevent."""
    found = []
    for d in described:
        if d.get("Name") != CLOBBERING_DOCUMENT:
            continue
        params = d.get("Parameters", {})
        if [v.lower() for v in params.get("ForceReplace", ["false"])] != ["true"]:
            continue
        if params.get("RoleName", [""])[0] in (ROLE, PROFILE):
            continue
        targets = d.get("Targets", [])
        ids = [v for t in targets if t.get("Key") == "InstanceIds" for v in t.get("Values", [])]
        other = [t for t in targets if t.get("Key") != "InstanceIds"]
        if "*" in ids or instance_id in ids or other:
            found.append(f"{d.get('AssociationName') or d.get('AssociationId')} "
                         f"({CLOBBERING_DOCUMENT}, ForceReplace=true, "
                         f"targets={'*' if '*' in ids else instance_id if instance_id in ids else 'tags/other'}, "
                         f"{d.get('ScheduleExpression', '')})")
    return found


def run_script(tree_uri: str, cmd: str, run_id: str, results_uri: str, boundary_arn: str = "") -> list[str]:
    """Shell the runner executes for one job: unpack the tree, run, upload results.
    LLMOPS_SPIKE_BOUNDARY_ARN tells the spikes to attach the boundary to every role they create;
    without it the runner role's iam:CreateRole is denied, by design."""
    if "'" in tree_uri or "'" in results_uri or "'" in boundary_arn:
        raise ValueError("URIs must not contain quotes")
    return [
        "set -uo pipefail",
        f"export AWS_REGION=us-east-1 AWS_DEFAULT_REGION=us-east-1 LLMOPS_SPIKE_BOUNDARY_ARN='{boundary_arn}'",
        f"D=/opt/llmops/runs/{run_id}; mkdir -p $D && cd $D",
        # Refuse before touching anything if the instance is not running as this runner's role.
        # Only the role name is printed: the ARN carries the account id.
        "who() { aws sts get-caller-identity --query Arn --output text 2>&1 | sed 's#.*:assumed-role/##; s#/.*##'; }",
        f"NOW=$(who); [ \"$NOW\" = '{ROLE}' ] || {{ echo \"REFUSED: running as $NOW, not {ROLE}\"; exit 5; }}",
        f"aws s3 cp '{tree_uri}' tree.tgz --only-show-errors && tar xzf tree.tgz && rm tree.tgz",
        # The tree ships without .git; an index rebuilt from it is what CI's checkout sees, and the
        # census/redaction tests read `git ls-files`, so without one they see an empty repo.
        "git init -q && git add -A",
        # The job's own files land in the tree after the index is built; excluded locally, or the
        # untracked-file guard (tests/test_redaction_scan.py) reports the runner's logs as unstaged.
        "printf 'job.log\\npip.log\\nidentity.log\\ntree.tgz\\n' >> .git/info/exclude",
        "source /opt/llmops/venv/bin/activate && ln -sfn /opt/llmops/venv .venv",
        # user data runs once per instance; re-asserting the pins per job keeps a live runner in step
        f"pip install -q --disable-pip-version-check {' '.join(PINS)} >> pip.log 2>&1 || {{ tail -20 pip.log; exit 3; }}",
        # A new `aws` process re-reads IMDS, so this sees a swap within 30 s; the job's own SDK may
        # keep cached credentials a while longer, which is not something to rely on either way.
        f"( while sleep 30; do N=$(who); [ \"$N\" = '{ROLE}' ] || echo \"$(date -u +%FT%TZ) IDENTITY CHANGED: $N\" >> identity.log; done ) & W=$!",
        f"( {cmd} ) > job.log 2>&1; RC=$?; echo \"exit=$RC\" >> job.log",
        "{ kill $W; wait $W; } 2>/dev/null; if [ -s identity.log ]; then { echo 'identity changed during the job:'; cat identity.log; } >> job.log; fi",
        f"aws s3 cp job.log '{results_uri}job.log' --only-show-errors",
        f"aws s3 cp deploy/evidence/ '{results_uri}evidence/' --recursive --exclude '*' --include 'SPIKE_*' --only-show-errors || true",
        # The job's own exit code, so SSM's status means something (it said Success for exit=1);
        # 6 when the identity changed, whatever the job reported, because its results are suspect.
        "tail -5 job.log; cat identity.log 2>/dev/null; [ -s identity.log ] && exit 6; exit $RC",
    ]


# ── AWS operations ───────────────────────────────────────────────────────────
class Runner:
    def __init__(self, region="us-east-1"):
        import boto3
        self.region, self.s = region, boto3.Session(region_name=region)
        self.acct = self.s.client("sts").get_caller_identity()["Account"]
        self.bucket = self.s.client("ssm").get_parameter(Name="/llmops/storage/bucket")["Parameter"]["Value"]

    def c(self, svc):
        return self.s.client(svc, region_name=self.region)

    def _find(self, describe, key, extra=None):
        flt = [{"Name": "tag:Name", "Values": [NAME]}, {"Name": "tag:component", "Values": ["build-runner"]}] + (extra or [])
        return describe(Filters=flt)[key]

    def up(self) -> str:
        ec2, iam = self.c("ec2"), self.c("iam")
        vpcs = self._find(ec2.describe_vpcs, "Vpcs")
        vpc = vpcs[0]["VpcId"] if vpcs else ec2.create_vpc(CidrBlock=VPC_CIDR, TagSpecifications=[
            {"ResourceType": "vpc", "Tags": _tags(NAME)}])["Vpc"]["VpcId"]
        ec2.modify_vpc_attribute(VpcId=vpc, EnableDnsSupport={"Value": True})
        ec2.modify_vpc_attribute(VpcId=vpc, EnableDnsHostnames={"Value": True})
        subnets = {s["CidrBlock"]: s["SubnetId"] for s in ec2.describe_subnets(
            Filters=[{"Name": "vpc-id", "Values": [vpc]}])["Subnets"]}
        for cidr in (PUBLIC_CIDR, PRIVATE_CIDR):
            if cidr not in subnets:
                subnets[cidr] = ec2.create_subnet(VpcId=vpc, CidrBlock=cidr, AvailabilityZoneId=AZ_ID,
                                                  TagSpecifications=[{"ResourceType": "subnet", "Tags": _tags(NAME)}])["Subnet"]["SubnetId"]
        igws = ec2.describe_internet_gateways(Filters=[{"Name": "attachment.vpc-id", "Values": [vpc]}])["InternetGateways"]
        if not igws:
            igw = ec2.create_internet_gateway(TagSpecifications=[{"ResourceType": "internet-gateway", "Tags": _tags(NAME)}])["InternetGateway"]["InternetGatewayId"]
            ec2.attach_internet_gateway(InternetGatewayId=igw, VpcId=vpc)
        else:
            igw = igws[0]["InternetGatewayId"]
        nats = [n for n in ec2.describe_nat_gateways(Filters=[{"Name": "vpc-id", "Values": [vpc]}])["NatGateways"]
                if n["State"] in ("pending", "available")]
        if not nats:
            eip = ec2.allocate_address(Domain="vpc", TagSpecifications=[{"ResourceType": "elastic-ip", "Tags": _tags(NAME)}])["AllocationId"]
            nat = ec2.create_nat_gateway(SubnetId=subnets[PUBLIC_CIDR], AllocationId=eip,
                                         TagSpecifications=[{"ResourceType": "natgateway", "Tags": _tags(NAME)}])["NatGateway"]["NatGatewayId"]
            ec2.get_waiter("nat_gateway_available").wait(NatGatewayIds=[nat])
        else:
            nat = nats[0]["NatGatewayId"]
        rts = {r["Tags"][0]["Value"] if r.get("Tags") else "": r for r in ec2.describe_route_tables(
            Filters=[{"Name": "vpc-id", "Values": [vpc]}])["RouteTables"]}
        for label, cidr, target in (("public", PUBLIC_CIDR, {"GatewayId": igw}), ("private", PRIVATE_CIDR, {"NatGatewayId": nat})):
            rt_name = f"{NAME}-{label}"
            rt = next((r for r in rts.values() if any(t["Key"] == "Name" and t["Value"] == rt_name for t in r.get("Tags", []))), None)
            if rt is None:
                rt_id = ec2.create_route_table(VpcId=vpc, TagSpecifications=[{"ResourceType": "route-table", "Tags": _tags(rt_name)}])["RouteTable"]["RouteTableId"]
                ec2.create_route(RouteTableId=rt_id, DestinationCidrBlock="0.0.0.0/0", **target)
                ec2.associate_route_table(RouteTableId=rt_id, SubnetId=subnets[cidr])
        sgs = ec2.describe_security_groups(Filters=[{"Name": "vpc-id", "Values": [vpc]},
                                                    {"Name": "group-name", "Values": [NAME]}])["SecurityGroups"]
        sg = sgs[0]["GroupId"] if sgs else ec2.create_security_group(
            VpcId=vpc, GroupName=NAME, Description="build runner: no inbound; egress only",
            TagSpecifications=[{"ResourceType": "security-group", "Tags": _tags(NAME)}])["GroupId"]
        self._iam(iam)
        live = self._find(ec2.describe_instances, "Reservations",
                          [{"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]}])
        if live:
            iid = live[0]["Instances"][0]["InstanceId"]
        else:
            ami = self.c("ssm").get_parameter(Name=AMI_PARAM)["Parameter"]["Value"]
            for _ in range(10):  # a new instance profile takes a few seconds to become usable
                try:
                    iid = ec2.run_instances(**launch_params(ami, subnets[PRIVATE_CIDR], sg))["Instances"][0]["InstanceId"]
                    break
                except Exception as e:  # noqa: BLE001
                    if "Invalid IAM Instance Profile" not in str(e):
                        raise
                    time.sleep(10)
        self.c("ssm").put_parameter(Name="/llmops/runner/instance_id", Value=iid, Type="String", Overwrite=True)
        self._repair_profile(iid)
        for problem in self.doctor(iid):
            print(f"WARNING: {problem}", file=sys.stderr)
        return iid

    def _associations(self, iid: str) -> list[dict]:
        return self.c("ec2").describe_iam_instance_profile_associations(
            Filters=[{"Name": "instance-id", "Values": [iid]}])["IamInstanceProfileAssociations"]

    def _repair_profile(self, iid: str) -> None:
        """Put this runner's own profile back on its own instance (the only instance it touches)."""
        if not profile_problem(self._associations(iid)):
            return
        ec2 = self.c("ec2")
        live = [a for a in self._associations(iid) if a["State"] in ("associated", "associating")]
        if live:
            ec2.replace_iam_instance_profile_association(IamInstanceProfile={"Name": PROFILE},
                                                         AssociationId=live[0]["AssociationId"])
        else:
            ec2.associate_iam_instance_profile(IamInstanceProfile={"Name": PROFILE}, InstanceId=iid)
        for _ in range(30):
            if not profile_problem(self._associations(iid)):
                return
            time.sleep(5)
        raise RuntimeError(f"could not restore {PROFILE} on {iid}: {profile_problem(self._associations(iid))}")

    def doctor(self, iid: str | None = None) -> list[str]:
        """Read-only: everything that would make a job run as the wrong identity."""
        ssm = self.c("ssm")
        iid = iid or ssm.get_parameter(Name="/llmops/runner/instance_id")["Parameter"]["Value"]
        problems = [p for p in [profile_problem(self._associations(iid))] if p]
        described, token = [], None
        while True:
            page = ssm.list_associations(AssociationFilterList=[{"key": "Name", "value": CLOBBERING_DOCUMENT}],
                                         **({"NextToken": token} if token else {}))
            described += [ssm.describe_association(AssociationId=a["AssociationId"])["AssociationDescription"]
                          for a in page.get("Associations", [])]
            token = page.get("NextToken")
            if not token:
                break
        problems += [f"association can replace the runner's role: {c}"
                     for c in clobbering_associations(described, iid)]
        return problems

    def _iam(self, iam):
        boundary_arn = f"arn:aws:iam::{self.acct}:policy/{BOUNDARY}"
        doc = json.dumps(boundary_policy(self.region, self.acct, self.bucket))
        try:
            iam.create_policy(PolicyName=BOUNDARY, PolicyDocument=doc, Tags=_tags(BOUNDARY),
                              Description="Ceiling for roles the build runner creates")
        except iam.exceptions.EntityAlreadyExistsException:
            versions = iam.list_policy_versions(PolicyArn=boundary_arn)["Versions"]
            for v in versions:
                if not v["IsDefaultVersion"] and len(versions) >= 5:
                    iam.delete_policy_version(PolicyArn=boundary_arn, VersionId=v["VersionId"])
                    break
            iam.create_policy_version(PolicyArn=boundary_arn, PolicyDocument=doc, SetAsDefault=True)
        try:
            iam.create_role(RoleName=ROLE, AssumeRolePolicyDocument=json.dumps(trust_policy()), Tags=_tags(ROLE))
        except iam.exceptions.EntityAlreadyExistsException:
            pass
        iam.attach_role_policy(RoleName=ROLE, PolicyArn="arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore")
        iam.put_role_policy(RoleName=ROLE, PolicyName="runner", PolicyDocument=json.dumps(
            _clean(runner_policy(self.region, self.acct, self.bucket))))
        try:
            iam.create_instance_profile(InstanceProfileName=PROFILE, Tags=_tags(PROFILE))
            iam.add_role_to_instance_profile(InstanceProfileName=PROFILE, RoleName=ROLE)
        except iam.exceptions.EntityAlreadyExistsException:
            pass

    def push(self) -> str:
        key = f"runner/trees/{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}.tgz"
        self.c("s3").put_object(Bucket=self.bucket, Key=key, Body=pack_tree())
        return f"s3://{self.bucket}/{key}"

    def exec(self, tree_uri: str, cmd: str, timeout_s: int = MAX_COMMAND_S,
             accept_identity_risk: bool = False) -> str:
        iid = self.c("ssm").get_parameter(Name="/llmops/runner/instance_id")["Parameter"]["Value"]
        # A wrong profile is always refused (`up` repairs it). A clobbering association is
        # refused too unless explicitly accepted: it means any job may lose its identity at the
        # next scheduled run, and only the caller knows whether this job is short enough.
        problems = self.doctor(iid)
        blocking = [p for p in problems if accept_identity_risk is False or not p.startswith("association")]
        if blocking:
            raise RuntimeError("refusing to dispatch:\n  " + "\n  ".join(blocking)
                               + f"\n  run `{pathlib.Path(__file__).name} up` to restore the profile; "
                               "pass --accept-identity-risk only for a job shorter than the schedule")
        run_id = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        results = f"s3://{self.bucket}/runner/results/{run_id}/"
        r = self.c("ssm").send_command(
            InstanceIds=[iid], DocumentName="AWS-RunShellScript", TimeoutSeconds=600,
            Parameters={"commands": run_script(tree_uri, cmd, run_id, results,
                                               f"arn:aws:iam::{self.acct}:policy/{BOUNDARY}"),
                        "executionTimeout": [str(min(timeout_s, MAX_COMMAND_S))]},
            OutputS3BucketName=self.bucket, OutputS3KeyPrefix=f"runner/ssm/{run_id}",
            Comment=f"llmops runner {run_id}"[:100])
        return r["Command"]["CommandId"]

    def fetch(self, command_id: str) -> dict:
        iid = self.c("ssm").get_parameter(Name="/llmops/runner/instance_id")["Parameter"]["Value"]
        inv = self.c("ssm").get_command_invocation(CommandId=command_id, InstanceId=iid)
        return {"status": inv["Status"], "stdout_tail": inv.get("StandardOutputContent", "")[-2000:],
                "stderr_tail": inv.get("StandardErrorContent", "")[-1000:]}

    def down(self) -> None:
        ec2 = self.c("ec2")
        for res in self._find(ec2.describe_instances, "Reservations",
                              [{"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]}]):
            for i in res["Instances"]:
                ec2.terminate_instances(InstanceIds=[i["InstanceId"]])
                ec2.get_waiter("instance_terminated").wait(InstanceIds=[i["InstanceId"]])
        for vpc in self._find(ec2.describe_vpcs, "Vpcs"):
            vid = vpc["VpcId"]
            for n in ec2.describe_nat_gateways(Filters=[{"Name": "vpc-id", "Values": [vid]}])["NatGateways"]:
                if n["State"] not in ("deleted", "deleting"):
                    ec2.delete_nat_gateway(NatGatewayId=n["NatGatewayId"])
            deadline = time.time() + 900
            while time.time() < deadline and any(
                    n["State"] != "deleted" for n in ec2.describe_nat_gateways(
                        Filters=[{"Name": "vpc-id", "Values": [vid]}])["NatGateways"]):
                time.sleep(15)
            for a in ec2.describe_addresses(Filters=[{"Name": "tag:Name", "Values": [NAME]}])["Addresses"]:
                ec2.release_address(AllocationId=a["AllocationId"])
            for rt in ec2.describe_route_tables(Filters=[{"Name": "vpc-id", "Values": [vid]}])["RouteTables"]:
                if any(a.get("Main") for a in rt.get("Associations", [])):
                    continue
                for a in rt.get("Associations", []):
                    ec2.disassociate_route_table(AssociationId=a["RouteTableAssociationId"])
                ec2.delete_route_table(RouteTableId=rt["RouteTableId"])
            for g in ec2.describe_internet_gateways(Filters=[{"Name": "attachment.vpc-id", "Values": [vid]}])["InternetGateways"]:
                ec2.detach_internet_gateway(InternetGatewayId=g["InternetGatewayId"], VpcId=vid)
                ec2.delete_internet_gateway(InternetGatewayId=g["InternetGatewayId"])
            for s in ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vid]}])["Subnets"]:
                ec2.delete_subnet(SubnetId=s["SubnetId"])
            for g in ec2.describe_security_groups(Filters=[{"Name": "vpc-id", "Values": [vid]}])["SecurityGroups"]:
                if g["GroupName"] != "default":
                    ec2.delete_security_group(GroupId=g["GroupId"])
            ec2.delete_vpc(VpcId=vid)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Secure EC2 runner for long build/test jobs")
    ap.add_argument("action", choices=["plan", "up", "doctor", "push", "exec", "fetch", "down"])
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--tree")
    ap.add_argument("--cmd")
    ap.add_argument("--command-id")
    ap.add_argument("--accept-identity-risk", action="store_true",
                    help="dispatch although an association may replace the runner role")
    args = ap.parse_args(argv)
    if args.action == "plan":
        print(f"vpc {VPC_CIDR} (public {PUBLIC_CIDR} with IGW + NAT, private {PRIVATE_CIDR}, {AZ_ID})")
        print(f"security group {NAME}: 0 ingress rules")
        print(f"instance {INSTANCE_TYPE} AL2023, IMDSv2 required, {ROOT_GB} GB gp3 encrypted, no key, no public IP")
        print(f"role {ROLE}: AmazonSSMManagedInstanceCore + scoped inline 'runner'; boundary {BOUNDARY} for roles it creates")
        print(f"tree: {len(tree_files())} files")
        return 0
    r = Runner(args.region)
    if args.action == "up":
        print(f"runner instance: {r.up()}")
    elif args.action == "push":
        print(r.push())
    elif args.action == "doctor":
        problems = r.doctor()
        print("\n".join(problems) or "ok: runner profile in place, no association can replace it")
        return 1 if problems else 0
    elif args.action == "exec":
        print(f"command id: {r.exec(args.tree, args.cmd, accept_identity_risk=args.accept_identity_risk)}")
    elif args.action == "fetch":
        print(json.dumps(r.fetch(args.command_id), indent=2))
    elif args.action == "down":
        r.down()
        print("runner deleted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
