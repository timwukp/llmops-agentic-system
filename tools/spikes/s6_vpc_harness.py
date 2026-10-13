#!/usr/bin/env python3
"""S6 — VPC-mode AgentCore harness without NAT.

What does a harness need to run in VPC mode with no internet route? A probe harness — the
data-prep harness's model, S3 skill sources and execution role, in VPC network mode, with a
probe-only system prompt — is invoked once and asked to run a fixed list of commands from its
shell. The spike then classifies each probe from the exit code and output: OK, REACHABLE (the
endpoint answered but IAM said no — the network works), or NETWORK (no route / timeout / DNS).

Gates: Phase 1 PR-03 (VPC foundation).

The network is self-contained (its own /16, private subnets in the three AZ IDs AgentCore
supports in us-east-1, interface endpoints with private DNS, an S3 gateway endpoint, no IGW,
no NAT) so the spike cannot disturb anything else.

Teardown caveat, from the AgentCore VPC docs (2026-10-13): the ENIs AgentCore creates "may
persist in your VPC for up to 8 hours" after the agent is deleted. Subnets, the security group
and the VPC cannot be deleted while those ENIs exist, and they cost nothing, so teardown retries
for a few minutes and then records them; `cleanup` deletes them later. The interface endpoints,
which do bill, are deleted unconditionally.

    python3 tools/spikes/s6_vpc_harness.py plan
    python3 tools/spikes/s6_vpc_harness.py run
    python3 tools/spikes/s6_vpc_harness.py cleanup     # deferred VPC/subnet/SG deletion
"""
from __future__ import annotations

import json
import pathlib
import re
import secrets
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _spike import REPO, Spike, main  # noqa: E402

VPC_CIDR = "10.43.0.0/16"  # deliberately not deploy/02_network.py's 10.42.0.0/16
#: (cidr, AZ ID). AgentCore VPC connectivity in us-east-1 supports use1-az1, use1-az2, use1-az4.
SUBNETS = [("10.43.1.0/24", "use1-az1"), ("10.43.2.0/24", "use1-az2"), ("10.43.3.0/24", "use1-az4")]
#: ecr.api + ecr.dkr were absent in run 1 (2026-10-13) and the session never initialized:
#: InvokeHarness failed "Runtime initialization time exceeded ... 120s" while the harness was READY.
#: The AgentCore VPC docs list both as REQUIRED without internet access, with the S3 gateway
#: endpoint (ECR stores image layers in S3) and logs.
INTERFACE_SERVICES = ["bedrock-runtime", "bedrock-agentcore", "sts", "logs", "ssm", "s3tables",
                      "glue", "athena", "secretsmanager", "ecr.api", "ecr.dkr"]
HARNESS_PREFIX = "llmops_spike_vpc"
EXECUTION_ROLE = "llmops-harness-execution"
EXPECTED_SKILLS = ("llm-data-preparation", "llm-prompt-engineering", "llm-guardrails")

#: The probes. Each is run with a timeout so an unreachable endpoint cannot hang the turn.
PROBES = [
    ("sts", "aws sts get-caller-identity --region us-east-1"),
    ("s3_gateway", "aws s3 ls s3://{bucket}/skills/ --region us-east-1"),
    ("ssm", "aws ssm get-parameter --name /llmops/storage/bucket --region us-east-1"),
    ("logs", "aws logs describe-log-groups --limit 1 --region us-east-1"),
    ("secretsmanager", "aws secretsmanager list-secrets --max-results 1 --region us-east-1"),
    ("s3tables", "aws s3tables list-table-buckets --region us-east-1"),
    ("athena", "aws athena list-work-groups --region us-east-1"),
    ("glue", "aws glue get-databases --max-items 1 --region us-east-1"),
    ("internet_hf", "curl -sS -m 10 -o /dev/null -w '%{{http_code}}' https://huggingface.co"),
    ("internet_pypi", "curl -sS -m 10 -o /dev/null -w '%{{http_code}}' https://pypi.org/simple/"),
]
LAKEHOUSE_PROBES = ("s3tables", "athena", "glue")

SYSTEM_PROMPT = (
    "You are a network probe inside a test harness. Do exactly what the user message says: run "
    "each command with your shell tool, wrapped as `timeout 25 <command>`, and capture its exit "
    "code and the first 300 characters of combined stdout and stderr. Do not retry, do not fix, "
    "do not explain. Also list the names of the skills available to you. Then reply with ONLY a "
    "JSON object between the markers <<<REPORT and REPORT>>> with keys: \"skills\" (list of "
    "names), and \"probes\" (object: probe id -> {\"exit\": int, \"output\": string}). If a tool "
    "itself is unavailable, set exit to -1 and output to the reason.")


def probe_message(bucket: str) -> str:
    lines = [f"- {pid}: `{cmd.format(bucket=bucket)}`" for pid, cmd in PROBES]
    lines.append("- code_interpreter_internet: in the code_interpreter tool (not the shell), run "
                 "Python that fetches https://huggingface.co with a 10 s timeout; report exit 0 "
                 "on success, 1 on failure, and the error text as output.")
    return "Run these probes:\n" + "\n".join(lines)


_NETWORK = re.compile(r"could not connect|connect timeout|timed out|name or service not known|"
                      r"temporary failure in name resolution|connection refused|"
                      r"failed to connect|couldn't resolve|could not resolve|network is unreachable|"
                      r"endpoint url",
                      re.I)
_DENIED = re.compile(r"accessdenied|not authorized|unauthorizedoperation|access denied", re.I)


def classify_probe(exit_code, output: str) -> str:
    """OK, REACHABLE (answered but denied by IAM), NETWORK, or UNKNOWN."""
    out = output or ""
    if exit_code == 0:
        return "OK"
    if _DENIED.search(out):
        return "REACHABLE"
    # 124: `timeout` fired. 6/7/28: curl could not resolve / connect / finish in time.
    if exit_code in (6, 7, 28, 124) or _NETWORK.search(out):
        return "NETWORK"
    return "UNKNOWN"


def parse_report(text: str) -> dict | None:
    """The JSON object between <<<REPORT and REPORT>>>, or None."""
    m = re.search(r"<<<REPORT\s*(\{.*?\})\s*REPORT>>>", text or "", re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


class S6VpcHarness(Spike):
    NAME = "s6_vpc_harness"
    NUMBER = 6
    QUESTION = "VPC-mode AgentCore harness without NAT"
    GATES = "Phase 1 PR-03: VPC foundation"
    PASS_CRITERIA = (
        ("session", "A session starts and the S3 skill sources resolve"),
        ("model", "The model call succeeds through the bedrock-runtime endpoint"),
        ("tools", "aws s3tables list-table-buckets, an Athena query and glue get-job succeed from the shell"),
        ("egress", "Which tools need egress is recorded"),
        ("failures", "What fails without NAT is recorded"),
    )
    IMPLEMENTED = True

    def __init__(self, region, account_id=None, session=None, stamp=None, dns_logging=True, nat=False):
        super().__init__(region, account_id, session)
        self.stamp = stamp or time.strftime("%Y%m%d_%H%M%S", time.gmtime())
        self.harness_name = f"{HARNESS_PREFIX}_{self.stamp}"
        #: Run 3 diagnostics. dns_logging records the hostnames the runtime resolves while it
        #: initializes (Route 53 Resolver query logs); nat=True is the positive control that
        #: separates "an endpoint is missing" from "VPC mode needs internet at all".
        import os
        self.dns_logging = dns_logging
        self.nat = nat or os.environ.get("LLMOPS_S6_NAT") == "1"  # CLI switch for the control run
        #: Run 3c. With the gateway endpoint, S3 traffic takes the prefix-list route even when a
        #: NAT exists, so run 3b (NAT) still reached the service's S3 log bucket through OUR
        #: endpoint -- 39 lookups of it in both 3a and 3b. Off, S3 goes out through the NAT.
        self.s3_gateway = os.environ.get("LLMOPS_S6_S3_GATEWAY", "1") != "0"
        if not self.s3_gateway and not self.nat:
            raise ValueError("without the S3 gateway endpoint S3 is unreachable unless nat=True")
        #: Run 3d. The same harness config in PUBLIC network mode, no VPC at all: after three
        #: VPC topologies failed identically, this separates "VPC mode" from "this config".
        self.public = os.environ.get("LLMOPS_S6_PUBLIC") == "1"
        #: Run 3e. The one variable every VPC run held fixed: the interface endpoints and their
        #: private DNS (ECR, bedrock-agentcore, ... resolved to OUR endpoints in 3a-3c). Off, the
        #: runtime resolves public names and leaves through the NAT.
        self.interface_endpoints = os.environ.get("LLMOPS_S6_INTERFACE_ENDPOINTS", "1") != "0"
        if not self.interface_endpoints and not self.nat:
            raise ValueError("without interface endpoints nothing is reachable unless nat=True")

    def plan(self) -> list[dict]:
        return ([{"kind": "vpc", "name": f"{VPC_CIDR} (DNS support + hostnames, no IGW, no NAT)"}]
                + [{"kind": "subnet", "name": f"{c} in {az}"} for c, az in SUBNETS]
                + [{"kind": "security group", "name": "443 from the VPC CIDR"}]
                + [{"kind": "interface endpoint", "name": f"com.amazonaws.{self.region}.{s} (private DNS)"}
                   for s in INTERFACE_SERVICES]
                + [{"kind": "gateway endpoint", "name": f"com.amazonaws.{self.region}.s3"},
                   {"kind": "agentcore harness", "name": f"{self.harness_name} (VPC network mode)"}])

    # ── run ──────────────────────────────────────────────────────────────────
    def run(self) -> None:
        sts, ssm = self.client("sts"), self.client("ssm")
        self.account_id = self.account_id or sts.get_caller_identity()["Account"]
        bucket = ssm.get_parameter(Name="/llmops/storage/bucket")["Parameter"]["Value"]
        self.redactions.append((bucket, "<DATA_BUCKET>"))
        if self.public:
            self.observe("variant", "PUBLIC network control (identical config, no VPC)")
            self.observe("memory", "disabled (runs 1-2: managed memory, the API default)")
            self._probe(bucket, self._harness(bucket, None, None), None)
            return
        ec2 = self.client("ec2")
        vpc_id, subnet_ids, sg_id = self._network(ec2)
        self.private_rt = self._private_route_table(ec2, vpc_id, subnet_ids)
        self.observe("variant", ("NAT positive control" if self.nat else "no NAT")
                     + ("" if self.s3_gateway else ", no S3 gateway endpoint (S3 via NAT)")
                     + ("" if self.interface_endpoints else ", no interface endpoints (public DNS via NAT)"))
        self.observe("memory", "disabled (runs 1-2: managed memory, the API default)")
        log_group = self._dns_logging(vpc_id) if self.dns_logging else None
        if self.nat:
            self._nat(ec2, vpc_id)
        self._endpoints(ec2, vpc_id, subnet_ids, sg_id)
        self._probe(bucket, self._harness(bucket, subnet_ids, sg_id), log_group)

    def _probe(self, bucket, harness_arn, log_group) -> None:
        if not harness_arn:
            return
        started = int(time.time() * 1000)
        text, err = self._invoke(harness_arn, probe_message(bucket))
        self.observe("invoke_error", err or "none")
        if log_group:
            time.sleep(60)  # resolver query logs are delivered with a short delay
            self.observe("dns_queries_during_invoke", self._dns_names(log_group, started))
        self.record("model", bool(text), "streamed text received" if text else f"no text: {err}")
        report = parse_report(text)
        if report is None:
            self.observe("raw_reply_tail", (text or "")[-1500:])
            self.record("session", False, "no parseable <<<REPORT>>> in the reply")
            return
        skills = [str(s) for s in report.get("skills", [])]
        missing = [s for s in EXPECTED_SKILLS if not any(s in k for k in skills)]
        self.observe("skills_seen", skills)
        self.record("session", not missing, "all three S3 skills listed" if not missing
                    else f"missing skills: {missing}")
        verdicts = {pid: classify_probe(p.get("exit"), str(p.get("output", "")))
                    for pid, p in (report.get("probes") or {}).items()}
        self.observe("probe_verdicts", verdicts)
        self.observe("probe_outputs", {pid: str(p.get("output", ""))[:200]
                                       for pid, p in (report.get("probes") or {}).items()})
        lake = {p: verdicts.get(p, "MISSING") for p in LAKEHOUSE_PROBES}
        self.record("tools", all(v in ("OK", "REACHABLE") for v in lake.values()), json.dumps(lake))
        self.record("egress", len(verdicts) >= len(PROBES), f"{len(verdicts)} probes classified")
        fails = sorted(p for p, v in verdicts.items() if v in ("NETWORK", "UNKNOWN"))
        self.observe("fails_without_nat", fails)
        self.record("failures", True, ", ".join(fails) or "nothing failed")

    # ── building blocks ──────────────────────────────────────────────────────
    def _ec2_tags(self, kind, name):
        return [{"ResourceType": kind, "Tags": [{"Key": k, "Value": v} for k, v in
                                                 {**self.tags, "Name": name}.items()]}]

    def _network(self, ec2):
        vpc_id = ec2.create_vpc(CidrBlock=VPC_CIDR,
                                TagSpecifications=self._ec2_tags("vpc", "llmops-spike-s6"))["Vpc"]["VpcId"]
        self.track("vpc", vpc_id, lambda: _retry(lambda: ec2.delete_vpc(VpcId=vpc_id)))
        ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsSupport={"Value": True})
        ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsHostnames={"Value": True})
        subnet_ids = []
        for cidr, az_id in SUBNETS:
            sid = ec2.create_subnet(VpcId=vpc_id, CidrBlock=cidr, AvailabilityZoneId=az_id,
                                    TagSpecifications=self._ec2_tags("subnet", f"llmops-spike-s6-{az_id}"))["Subnet"]["SubnetId"]
            self.track("subnet", sid, lambda sid=sid: _retry(lambda: ec2.delete_subnet(SubnetId=sid)))
            subnet_ids.append(sid)
        sg_id = ec2.create_security_group(
            VpcId=vpc_id, GroupName="llmops-spike-s6", Description="spike s6: 443 inside the VPC",
            TagSpecifications=self._ec2_tags("security-group", "llmops-spike-s6"))["GroupId"]
        self.track("security group", sg_id, lambda: _retry(lambda: ec2.delete_security_group(GroupId=sg_id)))
        ec2.authorize_security_group_ingress(GroupId=sg_id, IpPermissions=[{
            "IpProtocol": "tcp", "FromPort": 443, "ToPort": 443, "IpRanges": [{"CidrIp": VPC_CIDR}]}])
        self.observe("subnets", dict(zip([az for _, az in SUBNETS], subnet_ids)))
        return vpc_id, subnet_ids, sg_id

    def _private_route_table(self, ec2, vpc_id, subnet_ids) -> str:
        """A tagged route table for the private subnets, instead of the VPC's main one.

        The main route table is created by EC2 without tags, so a caller whose grants are
        tag-scoped (the build runner, tools/runner/runner.py) cannot attach a gateway endpoint
        to it or route it to a NAT: run 3 failed UnauthorizedOperation on CreateVpcEndpoint
        against exactly that table. Routing is unchanged -- local plus whatever the endpoints
        and the NAT control add."""
        rt = ec2.create_route_table(VpcId=vpc_id, TagSpecifications=self._ec2_tags(
            "route-table", "llmops-spike-s6-private"))["RouteTable"]["RouteTableId"]
        self.track("route table", rt, lambda: _retry_fresh(
            lambda: _retry(lambda: ec2.delete_route_table(RouteTableId=rt))))
        for sn in subnet_ids:
            assoc = _retry_fresh(lambda: ec2.associate_route_table(RouteTableId=rt, SubnetId=sn))["AssociationId"]
            self.track("route table association", assoc,
                       lambda a=assoc: ec2.disassociate_route_table(AssociationId=a))
        return rt

    def _dns_logging(self, vpc_id) -> str:
        logs, r53r = self.client("logs"), self.client("route53resolver")
        group = f"/llmops/spike/s6/dns-{self.stamp}"
        logs.create_log_group(logGroupName=group, tags=self.tags)
        self.track("log group", group, lambda: logs.delete_log_group(logGroupName=group))
        logs.put_retention_policy(logGroupName=group, retentionInDays=1)
        arn = f"arn:aws:logs:{self.region}:{self.account_id}:log-group:{group}"
        cfg = r53r.create_resolver_query_log_config(
            Name=f"llmops-spike-s6-{self.stamp}", DestinationArn=arn,
            CreatorRequestId=secrets.token_hex(16),
            Tags=[{"Key": k, "Value": v} for k, v in self.tags.items()])["ResolverQueryLogConfig"]["Id"]
        self.track("resolver query log config", cfg,
                   lambda: r53r.delete_resolver_query_log_config(ResolverQueryLogConfigId=cfg))
        assoc = r53r.associate_resolver_query_log_config(
            ResolverQueryLogConfigId=cfg, ResourceId=vpc_id)["ResolverQueryLogConfigAssociation"]["Id"]

        def _status():
            return r53r.get_resolver_query_log_config_association(
                ResolverQueryLogConfigAssociationId=assoc)["ResolverQueryLogConfigAssociation"]["Status"]

        def _disassociate():
            # Disassociating while CREATING/DELETING fails RSLVR-01400 and strands both the
            # association and the config (run 3, 2026-10-13): wait out the transition first.
            _wait(lambda: _status() not in ("CREATING", "DELETING"), timeout_s=300,
                  what="query log association settled")
            r53r.disassociate_resolver_query_log_config(ResolverQueryLogConfigId=cfg, ResourceId=vpc_id)
            _wait(lambda: not r53r.list_resolver_query_log_config_associations(Filters=[
                {"Name": "ResolverQueryLogConfigId", "Values": [cfg]}])["ResolverQueryLogConfigAssociations"],
                timeout_s=300, what="query log disassociation")
        self.track("resolver query log association", vpc_id, _disassociate)
        # Queries made before ACTIVE are not logged, so the invoke must not start earlier.
        _wait(lambda: _status() != "CREATING", timeout_s=300, what="query log association active")
        if _status() != "ACTIVE":
            raise RuntimeError(f"query log association {assoc} is {_status()}, not ACTIVE")
        return group

    def _dns_names(self, group, start_ms) -> dict:
        """{query_name: [count, sorted set of rcodes]} for every query logged since start_ms."""
        logs, names, token = self.client("logs"), {}, None
        while True:
            kw = {"logGroupName": group, "startTime": start_ms}
            if token:
                kw["nextToken"] = token
            page = logs.filter_log_events(**kw)
            for ev in page.get("events", []):
                summarize_dns_event(ev.get("message", ""), names)
            token = page.get("nextToken")
            if not token:
                break
        return {k: [v[0], sorted(v[1])] for k, v in sorted(names.items())}

    def _nat(self, ec2, vpc_id):
        """IGW + public subnet + NAT gateway; the private route table default-routes to it."""
        igw = ec2.create_internet_gateway(TagSpecifications=self._ec2_tags("internet-gateway", "llmops-spike-s6"))[
            "InternetGateway"]["InternetGatewayId"]
        ec2.attach_internet_gateway(InternetGatewayId=igw, VpcId=vpc_id)

        def _del_igw():
            ec2.detach_internet_gateway(InternetGatewayId=igw, VpcId=vpc_id)
            ec2.delete_internet_gateway(InternetGatewayId=igw)
        self.track("internet gateway", igw, _del_igw)
        pub = ec2.create_subnet(VpcId=vpc_id, CidrBlock="10.43.10.0/24", AvailabilityZoneId="use1-az1",
                                TagSpecifications=self._ec2_tags("subnet", "llmops-spike-s6-public"))["Subnet"]["SubnetId"]
        self.track("subnet", pub, lambda: _retry(lambda: ec2.delete_subnet(SubnetId=pub)))
        rt = ec2.create_route_table(VpcId=vpc_id, TagSpecifications=self._ec2_tags("route-table", "llmops-spike-s6-public"))[
            "RouteTable"]["RouteTableId"]
        self.track("route table", rt, lambda: ec2.delete_route_table(RouteTableId=rt))
        ec2.create_route(RouteTableId=rt, DestinationCidrBlock="0.0.0.0/0", GatewayId=igw)
        assoc = ec2.associate_route_table(RouteTableId=rt, SubnetId=pub)["AssociationId"]
        self.track("route table association", assoc, lambda: ec2.disassociate_route_table(AssociationId=assoc))
        eip = ec2.allocate_address(Domain="vpc", TagSpecifications=self._ec2_tags("elastic-ip", "llmops-spike-s6"))["AllocationId"]
        self.track("elastic ip", eip, lambda: _retry(lambda: ec2.release_address(AllocationId=eip)))
        nat = ec2.create_nat_gateway(SubnetId=pub, AllocationId=eip,
                                     TagSpecifications=self._ec2_tags("natgateway", "llmops-spike-s6"))["NatGateway"]["NatGatewayId"]

        def _del_nat():
            ec2.delete_nat_gateway(NatGatewayId=nat)
            _wait(lambda: ec2.describe_nat_gateways(NatGatewayIds=[nat])["NatGateways"][0]["State"] == "deleted",
                  timeout_s=600, what="nat gateway deleted")
        self.track("nat gateway", nat, _del_nat)
        _wait(lambda: ec2.describe_nat_gateways(NatGatewayIds=[nat])["NatGateways"][0]["State"] == "available",
              timeout_s=600, what="nat gateway available")
        rt_priv = self.private_rt
        _retry_fresh(lambda: ec2.create_route(RouteTableId=rt_priv, DestinationCidrBlock="0.0.0.0/0",
                                              NatGatewayId=nat))
        self.track("default route", rt_priv, lambda: ec2.delete_route(RouteTableId=rt_priv, DestinationCidrBlock="0.0.0.0/0"))

    def _endpoints(self, ec2, vpc_id, subnet_ids, sg_id):
        rt_id = self.private_rt
        ids = []
        gw = None if not self.s3_gateway else _retry_fresh(lambda: ec2.create_vpc_endpoint(
            VpcId=vpc_id, VpcEndpointType="Gateway", ServiceName=f"com.amazonaws.{self.region}.s3",
            RouteTableIds=[rt_id], TagSpecifications=self._ec2_tags("vpc-endpoint", "llmops-spike-s6-s3")))
        if gw:
            ids.append(gw["VpcEndpoint"]["VpcEndpointId"])
        for svc in (INTERFACE_SERVICES if self.interface_endpoints else ()):
            ep = ec2.create_vpc_endpoint(
                VpcId=vpc_id, VpcEndpointType="Interface", ServiceName=f"com.amazonaws.{self.region}.{svc}",
                SubnetIds=subnet_ids, SecurityGroupIds=[sg_id], PrivateDnsEnabled=True,
                TagSpecifications=self._ec2_tags("vpc-endpoint", f"llmops-spike-s6-{svc}"))
            ids.append(ep["VpcEndpoint"]["VpcEndpointId"])
        self.track("vpc endpoints", f"{len(ids)} endpoints", lambda: _delete_endpoints(ec2, ids))
        _wait(lambda: all(e["State"].lower() == "available" for e in
                          ec2.describe_vpc_endpoints(VpcEndpointIds=ids)["VpcEndpoints"]),
              timeout_s=900, what="vpc endpoints available")

    def _harness(self, bucket, subnet_ids, sg_id) -> str | None:
        iam, ctl = self.client("iam"), self.client("bedrock-agentcore-control")
        role_arn = iam.get_role(RoleName=EXECUTION_ROLE)["Role"]["Arn"]
        base = json.loads((REPO / "agents" / "data-prep" / "harness.json").read_text())
        skills = json.loads(json.dumps(base["skills"]).replace("<DATA_BUCKET>", bucket))
        try:
            resp = ctl.create_harness(
                harnessName=self.harness_name, clientToken=secrets.token_hex(20),
                executionRoleArn=role_arn, model=base["model"],
                systemPrompt=[{"text": SYSTEM_PROMPT}],
                tools=[t for t in base["tools"] if t.get("type") == "agentcore_code_interpreter"],
                allowedTools=["shell", "file_read", "file_write", "code_interpreter", "skills"],
                skills=skills, maxIterations=30, timeoutSeconds=600,
                # Omitting `memory` now provisions MANAGED memory (probe, 2026-10-13: CreateHarness
                # failed CreateMemory under the runner role). Runs 1-2 therefore had memory, run 3
                # does not: a deliberate change of one variable, recorded in the evidence, because
                # a runtime that must reach memory at start-up is itself a candidate for the
                # init timeout, and a network spike should not carry it unasked.
                memory={"disabled": {}},
                environment={"agentCoreRuntimeEnvironment": {
                    "networkConfiguration": {"networkMode": "PUBLIC"} if self.public else {
                        "networkMode": "VPC", "networkModeConfig": {
                            "subnets": subnet_ids, "securityGroups": [sg_id]}},
                    "lifecycleConfiguration": {"idleRuntimeSessionTimeout": 300, "maxLifetime": 1800}}},
                tags=self.tags)
        except Exception as e:  # noqa: BLE001 -- a rejected VPC config is itself the answer
            self.record("session", False, f"CreateHarness rejected: {type(e).__name__}: {e}"[:400])
            return None
        h = resp["harness"]
        hid, arn = h["harnessId"], h["arn"]
        self.track("agentcore harness", self.harness_name,
                   lambda: (ctl.delete_harness(harnessId=hid), time.sleep(30)))
        status = _wait(lambda: ctl.get_harness(harnessId=hid)["harness"]["status"] in ("READY", "FAILED"),
                       timeout_s=900, what="harness READY",
                       value=lambda: ctl.get_harness(harnessId=hid)["harness"])
        self.observe("harness_status", status.get("status") if isinstance(status, dict) else status)
        if isinstance(status, dict) and status.get("status") != "READY":
            self.record("session", False, f"harness {status.get('status')}: {status.get('failureReason', '')}"[:400])
            return None
        return arn

    def _invoke(self, harness_arn: str, prompt: str) -> tuple[str, str]:
        import boto3
        from botocore.config import Config
        data = (self._session or boto3.Session(region_name=self.region)).client(
            "bedrock-agentcore", region_name=self.region,
            config=Config(read_timeout=870, connect_timeout=30, retries={"max_attempts": 0}))
        text = []
        try:
            resp = data.invoke_harness(harnessArn=harness_arn, runtimeSessionId=f"spike-s6-{secrets.token_hex(16)}",
                                       messages=[{"role": "user", "content": [{"text": prompt}]}])
            kinds, errors = {}, []
            for event in resp["stream"]:
                for k in event:
                    kinds[k] = kinds.get(k, 0) + 1
                    # An error can arrive as a stream MEMBER rather than as a raise; run 3d (PUBLIC)
                    # ended with no text and no error because those members were dropped.
                    if "exception" in k.lower() or "error" in k.lower():
                        errors.append(f"{k}: {json.dumps(event[k], default=str)[:300]}")
                delta = event.get("contentBlockDelta", {}).get("delta", {})
                if "text" in delta:
                    text.append(delta["text"])
            self.observe("stream_event_kinds", kinds)
            if errors:
                self.observe("stream_errors", errors[:5])
                if not text:
                    return "", errors[0][:400]
        except Exception as e:  # noqa: BLE001
            return "".join(text), f"{type(e).__name__}: {e}"[:400]
        return "".join(text), ""

    # ── deferred cleanup ─────────────────────────────────────────────────────
    def cleanup(self) -> list[str]:
        """Delete tagged spike VPCs (endpoints, route tables, subnets, SGs) whose AgentCore ENIs
        are gone; report the rest and move on. It used to raise on the first VPC still held,
        so one young VPC kept every older, already-free one alive too."""
        ec2, done = self.client("ec2"), []
        vpcs = ec2.describe_vpcs(Filters=[{"Name": "tag:spike", "Values": [self.NAME]}])["Vpcs"]
        for v in vpcs:
            vid = v["VpcId"]
            enis = ec2.describe_network_interfaces(Filters=[{"Name": "vpc-id", "Values": [vid]}])["NetworkInterfaces"]
            if enis:
                done.append(f"kept {vid}: {len(enis)} network interface(s) still attached "
                            "(AgentCore releases them up to 8 h after the harness is deleted)")
                continue
            eps = [e["VpcEndpointId"] for e in ec2.describe_vpc_endpoints(
                Filters=[{"Name": "vpc-id", "Values": [vid]}])["VpcEndpoints"]]
            if eps:
                _delete_endpoints(ec2, eps)
            for rt in ec2.describe_route_tables(Filters=[{"Name": "vpc-id", "Values": [vid]}])["RouteTables"]:
                if not any(a.get("Main") for a in rt.get("Associations", [])):
                    for a in rt.get("Associations", []):
                        ec2.disassociate_route_table(AssociationId=a["RouteTableAssociationId"])
                    ec2.delete_route_table(RouteTableId=rt["RouteTableId"])
            for s in ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vid]}])["Subnets"]:
                ec2.delete_subnet(SubnetId=s["SubnetId"])
            for g in ec2.describe_security_groups(Filters=[{"Name": "vpc-id", "Values": [vid]}])["SecurityGroups"]:
                if g["GroupName"] != "default":
                    ec2.delete_security_group(GroupId=g["GroupId"])
            ec2.delete_vpc(VpcId=vid)
            done.append(f"deleted {vid}")
        return done


def summarize_dns_event(message: str, names: dict) -> None:
    """Fold one Route 53 Resolver query-log record into {name: [count, {rcodes}]}."""
    try:
        rec = json.loads(message)
    except json.JSONDecodeError:
        return
    name = str(rec.get("query_name", "")).rstrip(".").lower()
    if not name:
        return
    slot = names.setdefault(name, [0, set()])
    slot[0] += 1
    slot[1].add(str(rec.get("rcode", "?")))


def _delete_endpoints(ec2, ids) -> None:
    ec2.delete_vpc_endpoints(VpcEndpointIds=list(ids))
    _wait(lambda: not [e for e in ec2.describe_vpc_endpoints(VpcEndpointIds=list(ids))["VpcEndpoints"]
                       if e["State"].lower() not in ("deleted",)],
          timeout_s=600, what="vpc endpoints deleted", tolerate=Exception)


def _retry_fresh(fn, attempts: int = 6, delay_s: int = 10):
    """For a call on a resource created seconds ago, under tag-scoped grants (the build runner).

    Live, run 3 (2026-10-13): AssociateRouteTable and DeleteRouteTable on a route table created
    moments earlier, tagged at creation, failed UnauthorizedOperation; the decoded message showed
    `ec2:RouteTableID = "*"` -- authorization could not yet see the table, so it had no tags to
    match. The same call from the same role succeeded (dry run) minutes later. Only
    UnauthorizedOperation is retried, and only for a minute: this waits out consistency, it does
    not paper over a missing grant, which still fails after the last attempt."""
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            if "UnauthorizedOperation" not in str(e) or i == attempts - 1:
                raise
            time.sleep(delay_s)


def _retry(fn, attempts: int = 4, delay_s: int = 30):
    """For VPC/subnet/SG deletes, which fail while AgentCore's ENIs linger (up to 8 h)."""
    last = None
    for _ in range(attempts):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            last = e
            if "DependencyViolation" not in str(e) and "InvalidParameterValue" not in str(e):
                raise
            time.sleep(delay_s)
    raise RuntimeError(f"still blocked after {attempts} attempts (AgentCore ENIs persist up to 8 h; "
                       f"run `cleanup` later): {last}")


def _wait(pred, timeout_s, what, value=None, tolerate=()):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            if pred():
                return value() if value else True
        except tolerate:  # noqa: PERF203 -- e.g. NotFound once an endpoint is gone
            return True
        time.sleep(10)
    raise TimeoutError(f"timed out waiting for {what}")


if __name__ == "__main__":
    sys.exit(main(S6VpcHarness))
