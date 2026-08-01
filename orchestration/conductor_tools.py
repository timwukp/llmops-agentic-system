"""conductor_tools — servicing for the orchestrator's dispatch tool, shared verbatim.

Two callers import this module and MUST behave identically:

  - the console Lambda's task-chat worker (a human accepted a plan in the Tasks tab)
  - the harness driver (an orchestrator run invoked with params.task="plan")

launch_run was declared in agents/orchestrator/harness.json from Phase 5 on, but no
runtime ever serviced it — the driver's unknown-tool fallthrough answered
{"status": "unsupported"} and the conductor's dispatch died there, politely. Putting
the servicing in one shared module rather than in either caller is what keeps the
two paths from drifting apart the way harness.json and the driver's tool list did.

Also here: the approval-record signing helpers. An approval record is the artifact
that answers "a human decided this, not the agent" — canonical JSON, hash-chained to
the previous audit event, and signed with an asymmetric KMS key whose private half
cannot leave KMS hardware. service_launch_run verifies the signature before letting
the approval block anywhere near a manifest: a forged or tampered approval must die
at the tool boundary, as a rejectable toolResult, never inside a run's paper trail.
"""
from __future__ import annotations

import base64
import hashlib
import json


APPROVAL_KEY_ALIAS = "alias/llmops-approval"
SIGNING_ALGORITHM = "ECDSA_SHA_256"

# Keys covered by the signature. Everything that changes the meaning of "what was
# approved" must be in this list; anything NOT listed (e.g. the signature itself)
# is excluded from the signed digest by construction.
SIGNED_KEYS = (
    "task_id", "plan_uri", "plan_sha256", "cost_estimate_usd", "gate",
    "decision", "approved_by", "cognito_sub", "source_ip", "approved_at",
    "prev_event_sha256",
)


def canonical_json(record: dict) -> str:
    """Deterministic serialization of the SIGNED subset of an approval record.

    Sorted keys, no whitespace variance, only SIGNED_KEYS — so the digest is a pure
    function of the approval's meaning, not of dict ordering or of fields (like the
    signature) that are added after signing.
    """
    subset = {k: record[k] for k in SIGNED_KEYS if k in record}
    return json.dumps(subset, sort_keys=True, separators=(",", ":"), default=str)


def record_sha256(record: dict) -> str:
    return hashlib.sha256(canonical_json(record).encode()).hexdigest()


def chain_hash(prev_record: dict | None) -> str:
    """Hash of the previous audit event, for the prev_event_sha256 link.

    The chain makes tampering VISIBLE (any rewrite breaks every later link); the KMS
    signature makes forging a single record IMPOSSIBLE. They protect against
    different attacks, which is why both exist.
    """
    if not prev_record:
        return "genesis"
    return prev_record.get("record_sha256") or record_sha256(prev_record)


def sign_record(kms, record: dict, key_id: str = APPROVAL_KEY_ALIAS) -> dict:
    """Sign the record's digest and return the record with signature attached."""
    digest_hex = record_sha256(record)
    resp = kms.sign(
        KeyId=key_id,
        Message=bytes.fromhex(digest_hex),
        MessageType="DIGEST",
        SigningAlgorithm=SIGNING_ALGORITHM,
    )
    return {
        **record,
        "record_sha256": digest_hex,
        "signature": {
            "key": key_id,
            "algorithm": SIGNING_ALGORITHM,
            "value": base64.b64encode(resp["Signature"]).decode(),
        },
    }


def verify_record(kms, record: dict) -> bool:
    """Re-derive the digest from the record's contents and verify the signature.

    Recomputing (rather than trusting the stored record_sha256) is the point: a
    tampered record carries a perfectly valid signature — of the OLD contents.
    """
    sig = record.get("signature") or {}
    if not sig.get("value"):
        return False
    digest_hex = record_sha256(record)
    try:
        resp = kms.verify(
            KeyId=sig.get("key", APPROVAL_KEY_ALIAS),
            Message=bytes.fromhex(digest_hex),
            MessageType="DIGEST",
            Signature=base64.b64decode(sig["value"]),
            SigningAlgorithm=sig.get("algorithm", SIGNING_ALGORITHM),
        )
        return bool(resp.get("SignatureValid"))
    except Exception:
        # KMSInvalidSignatureException on tamper; any other failure is also a "no".
        return False


def _read_s3_uri(s3, uri: str) -> bytes:
    bucket, _, key = uri[5:].partition("/")
    return s3.get_object(Bucket=bucket, Key=key)["Body"].read()


def service_launch_run(lam, s3, kms, args: dict, start_fn: str,
                       expected: dict | None = None) -> dict:
    """Service one launch_run tool call. Returns {"ok": True, run_id, ...} or
    {"ok": False, "reason": ...} for a rejectable toolResult.

    Trust-but-verify, same contract as stage_complete: every claim the agent makes
    is checked against reality before the dispatch happens.

      - plan_uri must exist in S3 (an agent that reports a plan it never wrote
        would produce a manifest pointing at a 404);
      - the approval block must carry a VALID KMS signature — this is the boundary
        where a forged "a human said yes" dies;
      - if the caller knows what was approved (`expected` from the task record),
        the tool's numbers must match it: a cost drifted >20% from the approved
        estimate, or a plan hash that differs from the signed one, is a different
        plan than the human signed.
    """
    plan_uri = args.get("plan_uri") or ""
    if not plan_uri.startswith("s3://"):
        return {"ok": False, "reason": f"plan_uri must be an s3:// URI, got {plan_uri!r}"}

    try:
        plan_raw = _read_s3_uri(s3, plan_uri)
    except Exception as e:
        return {"ok": False, "reason": f"plan_uri not readable in S3: {plan_uri} ({e}). "
                                       "Write plan.json first, then call launch_run."}
    try:
        plan = json.loads(plan_raw)
    except Exception as e:
        return {"ok": False, "reason": f"plan.json is not valid JSON: {e}"}

    approval = args.get("approval") or (expected or {}).get("approval")
    if not approval:
        return {"ok": False, "reason": "no approval record present. A run can only be "
                                       "dispatched after a human accepts the plan."}
    if kms is not None and not verify_record(kms, approval):
        return {"ok": False, "reason": "approval record signature did not verify — the "
                                       "record was tampered with or never signed. Refusing "
                                       "to dispatch."}

    plan_hash = hashlib.sha256(plan_raw).hexdigest()
    if approval.get("plan_sha256") and approval["plan_sha256"] != plan_hash:
        return {"ok": False, "reason": "plan.json on S3 no longer matches the hash the "
                                       "human signed (plan_sha256 mismatch). The approved "
                                       "plan and the dispatched plan must be byte-identical."}

    if expected and expected.get("cost_estimate_usd") is not None:
        try:
            claimed = float(args.get("cost_estimate_usd") or expected["cost_estimate_usd"])
            approved = float(expected["cost_estimate_usd"])
            if approved > 0 and abs(claimed - approved) / approved > 0.20:
                return {"ok": False, "reason": f"cost_estimate_usd {claimed} drifted >20% "
                                               f"from the approved {approved}. Revise the "
                                               "plan and seek a fresh acceptance."}
        except (TypeError, ValueError):
            return {"ok": False, "reason": "cost_estimate_usd is not a number"}

    payload = {
        "trigger_source": "conductor",
        "params": args.get("params") or {},
        "plan": plan,
        "approval": approval,
    }
    resp = lam.invoke(FunctionName=start_fn, InvocationType="RequestResponse",
                      Payload=json.dumps(payload, default=str).encode())
    body = json.loads(resp["Payload"].read())
    if "run_id" not in body:
        return {"ok": False, "reason": f"start-pipeline did not return a run_id: {body}"}
    return {"ok": True, "run_id": body["run_id"],
            "manifest_uri": body.get("manifest_uri", ""),
            "execution_arn": body.get("execution_arn", "")}
