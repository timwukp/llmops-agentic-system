# Security Policy

## Secrets & cloud metadata

This is a public repository. The following must NEVER appear in any file, doc,
evidence log, or commit diff (including deletion lines of redaction commits):

- AWS account IDs (bare 12-digit numbers), account-bearing ARNs, bucket names
  embedding account IDs
- Credentials of any kind: access keys (`AKIA*`/`ASIA*`), secret keys, session
  tokens, API keys, passwords, JWTs
- Internal hostnames, VPC/subnet/SG IDs from real deployments

Use `<ACCOUNT_ID>` / `<REGION>` / `<DATA_BUCKET>` placeholders; deploy scripts
substitute real values at run time and publish them only to SSM Parameter Store.

**Allowlisted exceptions**: AWS public ECR gallery accounts `683313688378` and
`763104351884` (official image URIs), and `123456789012` (AWS's canonical
documentation example ID, used only in offline dry-run usage examples).

## Enforcement layers

1. `hooks/pre-commit` — blocks staged content matching account-ID / credential /
   account-bearing-ARN patterns; also enforces bilingual-doc pairing.
   Install: `ln -sf ../../hooks/pre-commit .git/hooks/pre-commit`
2. `.github/workflows/redaction-check.yml` — the same scan in CI on every push/PR.
3. Pre-push review discipline: scan the full diff (including deletions) before
   any push, per [AGENTS.md](AGENTS.md).

## Infrastructure posture

- **Least-privilege IAM only** — every role in `deploy/iam/` is resource-scoped;
  no `*FullAccess` managed policies anywhere.
- **VPC isolation** — `deploy/02_network.py` builds a dedicated VPC with no internet
  gateway, and can reach every AWS dependency through VPC endpoints. The 11 billed
  *interface* endpoints are skipped by default because nothing routes through them yet
  (`--force-unused-endpoints` overrides); the free S3 and DynamoDB gateway endpoints are
  always built. VPC-mode *harness* variants are
  **not built yet**; their skill-mirror prerequisite is now met — all 19 skill sources
  are `s3`, a pinned snapshot under `skills/` that a VPC-mode harness can reach over the
  S3 endpoint, fetched with `GetObject` + `ListBucket` and no write. Nothing reads GitHub
  at session start any more, so the skill repo's default branch can no longer change a
  deployed agent's behaviour without a version.
- `InvokeAgentRuntime` on `runtime/harness_*` executes shell in the session VM and
  bypasses `allowedTools` — it is granted only to the harness-driver Lambda role.
- S3: public access block ON, SSE, versioning; DynamoDB: PITR on.
- Webhook trigger requires HMAC (`X-Hub-Signature-256`, constant-time compare);
  admin API requires Cognito auth.

## Incident response

If a secret or account identifier lands in git history:

1. Rotate/invalidate the credential immediately (if applicable).
2. Rewrite history to a redacted tree and force-push; delete stale branches.
3. **Verify the redaction commit itself** — its diff deletion lines still expose
   the value; check every dangling SHA via the commit API, not just file contents.
4. Request dangling-commit purge via GitHub Support.
5. Record the incident and the lesson in the session log.

## Reporting

Open a GitHub issue titled "SECURITY" (without the sensitive value) or contact the
maintainer directly.
