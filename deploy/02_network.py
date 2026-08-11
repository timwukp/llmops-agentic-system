#!/usr/bin/env python3
"""02_network.py — provision the production VPC for llmops-agentic-system (idempotent).

Enterprise baseline: harnesses and Lambdas run inside a dedicated VPC with NO internet
egress; every AWS dependency is reached through VPC endpoints. Dev-mode harnesses
(PUBLIC network) skip all of this — this script is only needed for *.prod.json configs.

Creates (all tagged project=llmops-agentic-system, all idempotent by tag lookup):
  - VPC 10.42.0.0/16 with DNS support+hostnames                              [free]
  - 2 private subnets (10.42.1.0/24 az-a, 10.42.2.0/24 az-b) — no IGW, no NAT [free]
  - Security group llmops-endpoints-sg: HTTPS(443) from the VPC CIDR only     [free]
  - Security group llmops-workload-sg:  all egress to endpoints SG; no ingress[free]
  - Gateway endpoints: s3, dynamodb — attached to the main route table        [free]
  - SSM parameters under /llmops/network/* (vpc_id, subnet_ids, sg ids)       [free]
  - Interface endpoints — BILLED PER AZ, and the only part that costs anything:
      bedrock-runtime, sagemaker.api, sagemaker.runtime, sts, logs, events, states,
      ecr.api, ecr.dkr, secretsmanager, ssm
    Skipped unless something in this repo would actually route through them; see
    `find_endpoint_consumers`. Tear them down with --destroy.

Usage:
  python deploy/02_network.py --region us-east-1 [--dry-run]
  python deploy/02_network.py --region us-east-1 --force-unused-endpoints
  python deploy/02_network.py --region us-east-1 --destroy   # tears down interface endpoints only
"""
import argparse
import json
import sys
from pathlib import Path

import boto3

REPO = Path(__file__).resolve().parent.parent
TAG_KEY, TAG_VAL = "project", "llmops-agentic-system"
VPC_CIDR = "10.42.0.0/16"
SUBNETS = [("10.42.1.0/24", 0), ("10.42.2.0/24", 1)]  # (cidr, az offset)
GATEWAY_SERVICES = ["s3", "dynamodb"]
INTERFACE_SERVICES = [
    "bedrock-runtime",      # teacher (DeepSeek-R1) + harness model calls stay in-VPC
    "sagemaker.api",        # create/describe training jobs, endpoints
    "sagemaker.runtime",    # invoke student endpoint
    "sts", "logs", "events", "states",
    "ecr.api", "ecr.dkr",   # training image pulls
    "secretsmanager", "ssm",
]

#: us-east-1 `USE1-VpcEndpoint-Hours`, from the AWS Pricing API (2026-08-10). Charged
#: per endpoint per hour PER AVAILABILITY ZONE -- see endpoint_cost_per_day.
ENDPOINT_USD_PER_AZ_HOUR = 0.01


def endpoint_cost_per_day(n_services, n_azs):
    """Daily bill for `n_services` interface endpoints spread across `n_azs` AZs.

    Both dimensions are arguments because the printed note used to be
    `0.01 * len(INTERFACE_SERVICES) * 24` -- which is exactly half the real figure, and
    the missing factor is the one this function's name exists to make unmissable. AWS
    bills an interface endpoint "for each hour that your VPC endpoint remains provisioned
    in each Availability Zone", because `SubnetIds` creates one endpoint network interface
    per subnet ("If you add a subnet, we create an endpoint network interface in the
    subnet") and the ENI is the billed unit. `ensure_endpoints` passes SubnetIds=both
    subnets, so every one of the 11 endpoints is billed twice: $5.28/day, not $2.64/day.

    Deriving it from len() of the two lists rather than a literal is the point. A
    hardcoded total drifts silently the moment a twelfth service or a third AZ is added,
    and a cost note that is wrong is worse than absent: it is the number someone budgets
    against before deciding to leave this up over a weekend.
    """
    return ENDPOINT_USD_PER_AZ_HOUR * n_services * n_azs * 24


def find_endpoint_consumers(repo=REPO):
    """Everything in this repo that would actually route through an interface endpoint.

    Returns a list of human-readable reasons, empty when nothing would. Interface
    endpoints are the only billed resource here and they are pure plumbing: an endpoint
    with no consumer is not "provisioned ahead of need", it is a charge with no
    corresponding traffic. This script provisioned all 11 and printed a warm success
    line, so the failure mode was invisible by construction -- the exit code, the JSON
    and the note all looked like a normal deploy.

    Two things could make them load-bearing, and neither exists today (measured
    2026-08-10):

      * `agents/*/harness.prod.json` with a non-PUBLIC networkMode. `05_harnesses.py
        --prod` reads these; all 7 live configs are `networkMode: PUBLIC`, and no
        `.prod.json` has ever existed in this repo (deploy/README.md:25 already says so).
      * a Lambda deployed with `VpcConfig`. `07_lambdas.py` contains the string zero
        times, which is why ARCHITECTURE §11's "the Lambdas can run VPC-isolated" was a
        capability claim with no deploy path behind it.

    So the check reads the same files a deploy reads, rather than a flag someone sets by
    hand: a flag would be the same optimism the missing check already cost us.
    """
    reasons = []
    for cfg in sorted((repo / "agents").glob("*/harness.prod.json")):
        try:
            doc = json.loads(cfg.read_text())
        except (OSError, ValueError) as e:
            reasons.append(f"{cfg.relative_to(repo)} exists but is unreadable ({e}) — "
                           "treating it as a consumer rather than skipping it")
            continue
        mode = (((doc.get("environment") or {}).get("agentCoreRuntimeEnvironment") or {})
                .get("networkConfiguration") or {}).get("networkMode")
        if mode and mode != "PUBLIC":
            reasons.append(f"{cfg.relative_to(repo)} runs networkMode={mode}")
    lambdas = repo / "deploy" / "07_lambdas.py"
    if lambdas.exists() and "VpcConfig" in lambdas.read_text():
        reasons.append("deploy/07_lambdas.py deploys at least one Lambda with VpcConfig")
    return reasons


def tag_spec(rtype, name):
    return [{
        "ResourceType": rtype,
        "Tags": [{"Key": TAG_KEY, "Value": TAG_VAL}, {"Key": "Name", "Value": name}],
    }]


def find_by_tag(ec2, resource, filters=None):
    f = [{"Name": f"tag:{TAG_KEY}", "Values": [TAG_VAL]}] + (filters or [])
    return resource(Filters=f)


def ensure_vpc(ec2, dry):
    vpcs = find_by_tag(ec2, ec2.describe_vpcs)["Vpcs"]
    if vpcs:
        return vpcs[0]["VpcId"], False
    if dry:
        return "vpc-DRYRUN", True
    vpc = ec2.create_vpc(CidrBlock=VPC_CIDR, TagSpecifications=tag_spec("vpc", "llmops-vpc"))["Vpc"]
    vid = vpc["VpcId"]
    ec2.get_waiter("vpc_available").wait(VpcIds=[vid])
    ec2.modify_vpc_attribute(VpcId=vid, EnableDnsSupport={"Value": True})
    ec2.modify_vpc_attribute(VpcId=vid, EnableDnsHostnames={"Value": True})
    return vid, True


def ensure_subnets(ec2, vpc_id, region, dry):
    existing = find_by_tag(ec2, ec2.describe_subnets,
                           [{"Name": "vpc-id", "Values": [vpc_id]}])["Subnets"]
    if len(existing) >= 2:
        return [s["SubnetId"] for s in existing[:2]]
    if dry:
        return ["subnet-DRYRUN-a", "subnet-DRYRUN-b"]
    azs = [z["ZoneName"] for z in ec2.describe_availability_zones()["AvailabilityZones"]]
    out = []
    for cidr, az_i in SUBNETS:
        sn = ec2.create_subnet(VpcId=vpc_id, CidrBlock=cidr, AvailabilityZone=azs[az_i],
                               TagSpecifications=tag_spec("subnet", f"llmops-private-{az_i}"))["Subnet"]
        out.append(sn["SubnetId"])
    return out


def ensure_sg(ec2, vpc_id, name, desc, dry):
    got = ec2.describe_security_groups(Filters=[
        {"Name": "vpc-id", "Values": [vpc_id]},
        {"Name": "group-name", "Values": [name]}])["SecurityGroups"]
    if got:
        return got[0]["GroupId"], False
    if dry:
        return f"sg-DRYRUN-{name}", True
    sg = ec2.create_security_group(VpcId=vpc_id, GroupName=name, Description=desc,
                                   TagSpecifications=tag_spec("security-group", name))
    return sg["GroupId"], True


def ensure_endpoints(ec2, vpc_id, subnet_ids, sg_id, region, dry, interface=True):
    """Gateway endpoints always; interface endpoints only when `interface`.

    Split on the billing line, not on the script: the VPC, both subnets, both security
    groups, the gateway endpoints and the SSM parameters are free, and they are exactly
    what a `harness.prod.json` has to be written *against*. Refusing to build the free
    substrate would make the missing consumer unfixable -- the gate would block the only
    route to satisfying it.
    """
    existing = {e["ServiceName"]: e for e in find_by_tag(
        ec2, ec2.describe_vpc_endpoints, [{"Name": "vpc-id", "Values": [vpc_id]}])["VpcEndpoints"]}
    rtbs = [r["RouteTableId"] for r in ec2.describe_route_tables(
        Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["RouteTables"]] if not dry else []
    created = []
    for svc in GATEWAY_SERVICES:
        sname = f"com.amazonaws.{region}.{svc}"
        if sname in existing:
            continue
        if not dry:
            ec2.create_vpc_endpoint(VpcId=vpc_id, ServiceName=sname, VpcEndpointType="Gateway",
                                    RouteTableIds=rtbs,
                                    TagSpecifications=tag_spec("vpc-endpoint", f"llmops-gw-{svc}"))
        created.append(sname)
    if not interface:
        return created
    for svc in INTERFACE_SERVICES:
        sname = f"com.amazonaws.{region}.{svc}"
        if sname in existing:
            continue
        if not dry:
            ec2.create_vpc_endpoint(VpcId=vpc_id, ServiceName=sname, VpcEndpointType="Interface",
                                    SubnetIds=subnet_ids, SecurityGroupIds=[sg_id],
                                    PrivateDnsEnabled=True,
                                    TagSpecifications=tag_spec("vpc-endpoint", f"llmops-if-{svc}"))
        created.append(sname)
    return created


def find_our_vpc(ec2):
    """The VPC this script provisions, or None. Never creates one.

    Separate from ensure_vpc because a teardown that can CREATE what it is about to
    delete is a teardown that always finds something to delete.
    """
    vpcs = find_by_tag(ec2, ec2.describe_vpcs)["Vpcs"]
    return vpcs[0]["VpcId"] if vpcs else None


def destroy_interface_endpoints(ec2, dry, vpc_id=None):
    """Delete the interface endpoints inside OUR VPC. Returns the ids deleted.

    `vpc-id` is not a refinement, it is the whole safety property. `project` is a tag
    anyone in the account can apply, and `ensure_endpoints` scopes its own read to
    `vpc-id` — so a destroy filtered on the tag alone was not the inverse of the
    create: it deleted every same-tagged interface endpoint in the region, including
    ones this script never made and another team's VPC depends on. An interface
    endpoint is load-bearing; deleting one is a silent outage for whatever was
    resolving through it, and the deletion is not reversible by re-running anything
    here (the replacement gets a new id and new DNS).

    A caller that cannot find our VPC gets an empty list, not an unscoped sweep: with
    no VPC there are no endpoints of ours to bill, so there is nothing to tear down.
    """
    vpc_id = vpc_id or find_our_vpc(ec2)
    if not vpc_id:
        return []
    eps = find_by_tag(ec2, ec2.describe_vpc_endpoints,
                      [{"Name": "vpc-id", "Values": [vpc_id]}])["VpcEndpoints"]
    victims = [e["VpcEndpointId"] for e in eps if e["VpcEndpointType"] == "Interface"]
    if victims and not dry:
        ec2.delete_vpc_endpoints(VpcEndpointIds=victims)
    return victims


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--destroy", action="store_true",
                    help="delete interface endpoints (the only hourly-billed part)")
    ap.add_argument("--force-unused-endpoints", action="store_true",
                    help="create the 11 billed interface endpoints even though nothing "
                         "in this repo routes through them — you are paying per AZ per "
                         "hour for plumbing with no traffic")
    args = ap.parse_args()
    ec2 = boto3.client("ec2", region_name=args.region)
    ssm = boto3.client("ssm", region_name=args.region)

    if args.destroy:
        vpc_id = find_our_vpc(ec2)
        if not vpc_id:
            print(f"no VPC tagged {TAG_KEY}={TAG_VAL} in {args.region}; "
                  "nothing of ours to tear down")
            return
        victims = destroy_interface_endpoints(ec2, args.dry_run, vpc_id)
        print(f"{'DRY-RUN — ' if args.dry_run else ''}deleted {len(victims)} "
              f"interface endpoints in {vpc_id}: {victims}")
        return

    vpc_id, vpc_new = ensure_vpc(ec2, args.dry_run)
    subnet_ids = ensure_subnets(ec2, vpc_id, args.region, args.dry_run)
    ep_sg, _ = ensure_sg(ec2, vpc_id, "llmops-endpoints-sg", "HTTPS from VPC to endpoints", args.dry_run)
    wl_sg, wl_new = ensure_sg(ec2, vpc_id, "llmops-workload-sg", "harness/lambda workloads", args.dry_run)
    if not args.dry_run:
        try:
            ec2.authorize_security_group_ingress(GroupId=ep_sg, IpPermissions=[{
                "IpProtocol": "tcp", "FromPort": 443, "ToPort": 443,
                "IpRanges": [{"CidrIp": VPC_CIDR, "Description": "HTTPS from VPC"}]}])
        except ec2.exceptions.ClientError as e:
            if "InvalidPermission.Duplicate" not in str(e):
                raise
    consumers = find_endpoint_consumers()
    daily = endpoint_cost_per_day(len(INTERFACE_SERVICES), len(subnet_ids))
    want_interface = bool(consumers) or args.force_unused_endpoints
    created = ensure_endpoints(ec2, vpc_id, subnet_ids, ep_sg, args.region, args.dry_run,
                               interface=want_interface)

    params = {"vpc_id": vpc_id, "subnet_ids": ",".join(subnet_ids),
              "endpoints_sg": ep_sg, "workload_sg": wl_sg}
    if not args.dry_run:
        for k, v in params.items():
            ssm.put_parameter(Name=f"/llmops/network/{k}", Value=v, Type="String", Overwrite=True)

    print(json.dumps({"vpc": vpc_id, "subnets": subnet_ids, "endpoints_sg": ep_sg,
                      "workload_sg": wl_sg, "new_endpoints": created,
                      "interface_endpoints": want_interface,
                      "endpoint_consumers": consumers,
                      "dry_run": args.dry_run}, indent=2))

    # Every free thing above is built either way. This is the only line that costs money,
    # so it is the only one with a gate in front of it -- and the note is derived from BOTH
    # list lengths because an interface endpoint is billed per AZ, not per endpoint.
    if not want_interface:
        print(f"\nSKIPPED the {len(INTERFACE_SERVICES)} interface endpoints: nothing in "
              "this repo routes through them. No agents/*/harness.prod.json declares a "
              "non-PUBLIC networkMode, and deploy/07_lambdas.py deploys no Lambda with "
              "VpcConfig, so all 11 would have carried traffic from nobody at "
              f"~${daily:.2f}/day. The VPC, subnets, security groups, gateway endpoints "
              "and /llmops/network/* ARE built — write a harness.prod.json against them, "
              "then re-run. --force-unused-endpoints creates them anyway.", file=sys.stderr)
        # Exit 0, not 01_iam.py's 2: nothing failed and nothing was half-applied. That
        # script refuses because PROCEEDING would strip a live region's permissions;
        # here skipping destroys nothing and the whole free substrate is in place. The
        # signal lives on stderr and in `interface_endpoints: false`, which is what a
        # caller should branch on -- an exit code cannot say "built 6 of 7 things".
        return 0
    why = "; ".join(consumers) if consumers else "--force-unused-endpoints (no consumer)"
    print(f"\nNOTE: the {len(INTERFACE_SERVICES)} interface endpoints bill "
          f"${ENDPOINT_USD_PER_AZ_HOUR}/hr EACH, PER AZ, and they are attached to "
          f"{len(subnet_ids)} subnets in {len(subnet_ids)} AZs — so "
          f"{len(INTERFACE_SERVICES)}x{len(subnet_ids)} = "
          f"{len(INTERFACE_SERVICES)*len(subnet_ids)} billed endpoint-hours per hour, "
          f"~${daily:.2f}/day. Built because: {why}. Run with --destroy when idle.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
