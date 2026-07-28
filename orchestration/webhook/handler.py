"""webhook Lambda — HMAC-verified external trigger behind API Gateway.

External systems POST a JSON body with an `X-Signature-256` header
(`sha256=<hmac-sha256-hex of the raw body>`), keyed by a shared secret in
Secrets Manager. On a valid signature the body's params are forwarded to the
start-pipeline Lambda. Constant-time comparison; no information leaks on
rejection (403 either way).

Env: WEBHOOK_SECRET_ID, START_PIPELINE_FN.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os

import boto3

_secret_cache: dict = {}


def _get_secret(sm) -> str:
    sid = os.environ["WEBHOOK_SECRET_ID"]
    if sid not in _secret_cache:
        _secret_cache[sid] = sm.get_secret_value(SecretId=sid)["SecretString"]
    return _secret_cache[sid]


def verify_signature(secret: str, body: str, header: str) -> bool:
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(header[len("sha256="):], expected)


def _clients():
    region = os.environ.get("AWS_REGION", "us-east-1")
    return {
        "sm": boto3.client("secretsmanager", region_name=region),
        "lambda": boto3.client("lambda", region_name=region),
    }


def handler(event, context=None, clients=None):
    """event: API Gateway (HTTP API v2) proxy envelope."""
    c = clients or _clients()
    body = event.get("body") or ""
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    signature = headers.get("x-signature-256", "")

    if not verify_signature(_get_secret(c["sm"]), body, signature):
        return {"statusCode": 403, "body": json.dumps({"error": "forbidden"})}

    try:
        payload = json.loads(body) if body else {}
    except json.JSONDecodeError:
        return {"statusCode": 400, "body": json.dumps({"error": "invalid JSON"})}

    resp = c["lambda"].invoke(
        FunctionName=os.environ["START_PIPELINE_FN"],
        InvocationType="RequestResponse",
        Payload=json.dumps({"trigger_source": "webhook",
                            "params": payload.get("params") or {}}))
    started = json.loads(resp["Payload"].read())

    return {"statusCode": 202,
            "body": json.dumps({"run_id": started.get("run_id"),
                                "manifest_uri": started.get("manifest_uri")})}
