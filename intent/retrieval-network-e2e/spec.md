# Spec: end-to-end retrieve and ingest under the seal

- **Intent:** ./intent.md
- **Author:** Claude (AI agent)
- **Signed-off-by:** Tim WU
- **Accepted-by:** Tim WU
- **Status:** signed-off

Signed off with the intent, on the owner's request of 2026-11-23 (chat): "pls run an end to end test".

## Requirements

1. **Harness turn.**
   - Call `llmops_orchestrator` on `bedrock-agentcore` `invoke_harness` with a fresh 33+ character
     `runtimeSessionId` and `actorId` `probe-retrieval-e2e-<hex>`. This mirrors
     `tools/probe_orchestrator_intake.py`'s isolation.
   - The message asks for two shell commands and a raw report. No plan, and no inline function. Any
     inline-function call is recorded, never serviced.
   - Read the stream as the probe's `drain()` does.
   - Record: the caller's role name (account id masked), the result count, each result's score and
     key under `customer-data/kb-corpus/`, whether the query ticket's own document came back, and
     the turn's rounds and tokens.
2. **Ingest.**
   - Run `python deploy/09_retrieval.py --region us-east-1 --source-uri
     s3://<bucket>/customer-data/helpdesk-demo/helpdesk_tickets.jsonl --customer-eval-key
     customer-data/helpdesk-demo/helpdesk_id_acceptance.jsonl --ood-eval-key
     customer-data/helpdesk-demo/helpdesk_acceptance.jsonl --ingest`, after the same command with
     `--dry-run`.
   - Record `network_open`, `network` (seal), `network_window_s`, `index`, `corpus`, and `ingestion`
     (scanned, failed, reconciled_against).
   - Afterwards: an independent `get-security-policy`, plus one outside probe.
3. **Evidence.** Append a §6 to `deploy/evidence/VERIFICATION_retrieval_network_private.md`, and
   strike the two gaps in §5 only for what actually passed. Redacted: no account id, bucket name, or
   personal data from tickets.
4. **Docs.** TEST_RESULTS EN + zh-TW row; CHANGELOG; `intent/retrieval-network-private-live/*` →
   `shipped`; redaction census if it moves.
