#!/usr/bin/env python3
"""02_network.py — provision the production VPC for llmops-agentic-system (idempotent).

Enterprise baseline: harnesses and Lambdas run inside a dedicated VPC with NO internet
egress; every AWS dependency is reached through VPC endpoints. Dev-mode harnesses
(PUBLIC network) skip all of this — this script is only needed for *.prod.json configs.

Creates (all tagged project=llmops-agentic-system, all idempotent by tag lookup):
  - VPC 10.42.0.0/16 with DNS support+hostnames
  - 2 private subnets (10.42.1.0/24 az-a, 10.42.2.0/24 az-b) — no IGW, no NAT
  - Security group llmops-endpoints-sg: HTTPS(443) from the VPC CIDR only
  - Security group llmops-workload-sg:  all egress to endpoints SG; no ingress
  - Gateway endpoints (free): s3, dynamodb — attached to the main route table
  - Interface endpoints (billed ~$0.01/hr each, teardown with --destroy):
      bedrock-runtime, sagemaker.api, sagemaker.runtime, sts, logs, events, states,
      ecr.api, ecr.dkr, secretsmanager, ssm
  - SSM parameters under /llmops/network/* (vpc_id, subnet_ids, sg ids) for other scripts

Usage:
  python deploy/02_network.py --region us-east-1 [--dry-run]
  python deploy/02_network.py --region us-east-1 --destroy   # tears down interface endpoints only
"""
import argparse
import json
import sys

import boto3

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


def ensure_endpoints(ec2, vpc_id, subnet_ids, sg_id, region, dry):
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


def destroy_interface_endpoints(ec2, dry):
    eps = find_by_tag(ec2, ec2.describe_vpc_endpoints)["VpcEndpoints"]
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
    args = ap.parse_args()
    ec2 = boto3.client("ec2", region_name=args.region)
    ssm = boto3.client("ssm", region_name=args.region)

    if args.destroy:
        victims = destroy_interface_endpoints(ec2, args.dry_run)
        print(f"deleted {len(victims)} interface endpoints: {victims}")
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
    created = ensure_endpoints(ec2, vpc_id, subnet_ids, ep_sg, args.region, args.dry_run)

    params = {"vpc_id": vpc_id, "subnet_ids": ",".join(subnet_ids),
              "endpoints_sg": ep_sg, "workload_sg": wl_sg}
    if not args.dry_run:
        for k, v in params.items():
            ssm.put_parameter(Name=f"/llmops/network/{k}", Value=v, Type="String", Overwrite=True)

    print(json.dumps({"vpc": vpc_id, "subnets": subnet_ids, "endpoints_sg": ep_sg,
                      "workload_sg": wl_sg, "new_endpoints": created,
                      "dry_run": args.dry_run}, indent=2))
    print("\nNOTE: interface endpoints bill hourly (~$0.01/hr each × "
          f"{len(INTERFACE_SERVICES)} = ~${0.01*len(INTERFACE_SERVICES)*24:.2f}/day). "
          "Run with --destroy when idle.")


if __name__ == "__main__":
    sys.exit(main())
