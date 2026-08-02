#!/usr/bin/env python3
"""Update an AgentCore Harness with the correct payload rules.

The hard part of UpdateHarness is that the `optionalValue` wrapper applies ONLY to the fields whose
live shape has an `optionalValue` member — memory / environmentArtifact / authorizerConfiguration.
Everything else (including the structures model / environment / truncation) passes directly.
This script introspects the LIVE input shape and wraps correctly, so you don't have to memorize
the table. It also:
  - routes `tags` to a separate TagResource call (UpdateHarness rejects tags)
  - generates a valid clientToken (>= 33 chars)

Usage:
    python update_harness.py --harness-id <ID> --config harness.json
    python update_harness.py --harness-id <ID> --config harness.json --fields memory model --dry-run
    python update_harness.py --harness-id <ID> --harness-arn <ARN> --config harness.json   # to also set tags
"""
import argparse
import json
import pathlib
import secrets
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import config_subst  # noqa: E402 — deploy/ is not a package; path is set just above

# 'name'/'harnessName' are not UpdateHarness inputs (it uses harnessId from the CLI).
NON_API_KEYS = {"name", "harnessName"}


def _strip_comments(obj):
    """Remove any dict key starting with '_' (templates use _comment / _*_note keys)."""
    if isinstance(obj, dict):
        return {k: _strip_comments(v) for k, v in obj.items() if not k.startswith("_")}
    if isinstance(obj, list):
        return [_strip_comments(v) for v in obj]
    return obj


def get_structure_fields(client) -> set:
    """Return UpdateHarness members that actually use the `optionalValue` wrapper.

    The wrapper is NOT applied to every structure field — only to fields whose shape literally has an
    `optionalValue` member. Verified in boto3 1.43.29: memory / environmentArtifact / authorizerConfiguration
    wrap; model / environment / truncation are passed DIRECTLY. Detect this precisely instead of assuming.
    """
    shape = client.meta.service_model.operation_model("UpdateHarness").input_shape
    wrap = set()
    for name, member in shape.members.items():
        if member.type_name == "structure" and "optionalValue" in getattr(member, "members", {}):
            wrap.add(name)
    return wrap


def wrap_payload(cfg: dict, structure_fields: set, only_fields: list | None) -> dict:
    payload = {}
    for k, v in cfg.items():
        if k in NON_API_KEYS or k == "tags":
            continue
        if only_fields and k not in only_fields:
            continue
        if k in structure_fields:
            # already wrapped? leave as-is
            payload[k] = v if (isinstance(v, dict) and "optionalValue" in v) else {"optionalValue": v}
        else:
            payload[k] = v
    return payload


def main() -> int:
    ap = argparse.ArgumentParser(description="Update an AgentCore Harness (correct payload rules)")
    ap.add_argument("--harness-id", required=True)
    ap.add_argument("--harness-arn", help="Needed only to also apply tags via TagResource")
    ap.add_argument("--config", required=True)
    ap.add_argument("--fields", nargs="*", help="Restrict update to these top-level fields")
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--account-id", help="Resolve <ACCOUNT_ID>/<DATA_BUCKET> offline (dry-run)")
    ap.add_argument("--bucket", help="Override the data bucket used for <DATA_BUCKET>")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    print(f"Region: {args.region}")
    with open(args.config, encoding="utf-8") as f:
        cfg = _strip_comments(json.load(f))

    # live-verified fallback: only these three wrap in optionalValue
    fallback_fields = {"memory", "environmentArtifact", "authorizerConfiguration"}
    tags = cfg.get("tags")

    if args.dry_run:
        # no boto3 needed in dry-run — use the known optionalValue set
        if config_subst.unresolved(cfg):
            if not args.account_id:
                print("FAIL  config carries placeholders "
                      f"{config_subst.unresolved(cfg)} and no --account-id was given to "
                      "resolve them offline. Printing the unresolved config would show a "
                      "payload nobody will ever send.")
                return 1
            cfg = config_subst.resolve(
                cfg, config_subst.mapping_for(args.account_id, args.region, args.bucket),
                where=args.config)
        payload = wrap_payload(cfg, fallback_fields, args.fields)
        payload["harnessId"] = args.harness_id
        payload["clientToken"] = secrets.token_hex(20)
        print(f"DRY RUN — optionalValue-wrapped fields: {sorted(fallback_fields)}")
        print("update_harness(**payload) with:")
        print(json.dumps(payload, indent=2, default=str))
        if tags:
            print(f"\nthen tag_resource(resourceArn={args.harness_arn or '<ARN>'}, tags={tags})")
        return 0

    import boto3
    client = boto3.client("bedrock-agentcore-control", region_name=args.region)

    # Resolve placeholders BEFORE any API call. An unresolved <DATA_BUCKET> in a skill
    # URI is accepted by UpdateHarness, mints a version and reports READY -- and then
    # every session fails at start. config_subst.resolve raises rather than send one.
    if config_subst.unresolved(cfg):
        account_id = args.account_id or boto3.client(
            "sts", region_name=args.region).get_caller_identity()["Account"]
        bucket = args.bucket
        if not bucket:
            ssm = boto3.client("ssm", region_name=args.region)
            try:
                bucket = ssm.get_parameter(
                    Name="/llmops/storage/bucket")["Parameter"]["Value"]
            except ssm.exceptions.ParameterNotFound:
                pass  # mapping_for derives the default name
        cfg = config_subst.resolve(
            cfg, config_subst.mapping_for(account_id, args.region, bucket),
            where=args.config)
        print(f"OK    resolved placeholders (bucket={bucket or 'derived'})")

    try:
        structure_fields = get_structure_fields(client)
    except Exception as e:  # noqa: BLE001
        print(f"WARN  could not introspect UpdateHarness shape ({e}); falling back to known optionalValue set.")
        structure_fields = fallback_fields

    payload = wrap_payload(cfg, structure_fields, args.fields)
    payload["harnessId"] = args.harness_id
    payload["clientToken"] = secrets.token_hex(20)

    try:
        client.update_harness(**payload)
        print(f"OK    updated harness {args.harness_id}")
    except Exception as e:  # noqa: BLE001
        print(f"FAIL  update_harness rejected: {e}")
        print("      Tip: preflight.py --show-shape UpdateHarness to inspect the live field shapes.")
        return 1

    if tags:
        if not args.harness_arn:
            print("WARN  tags present in config but --harness-arn not given; skipping TagResource. "
                  "Re-run with --harness-arn to apply tags.")
        else:
            client.tag_resource(resourceArn=args.harness_arn, tags=tags)
            print(f"OK    applied {len(tags)} tag(s) via TagResource")
    return 0


if __name__ == "__main__":
    sys.exit(main())
