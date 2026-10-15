# Intent: scope the console role's S3 reads to what the console reads

- **Slug:** console-s3-scope
- **Author:** Claude (AI agent)
- **Date:** 2026-10-15
- **Accepted-by:** Tim WU
- **Status:** accepted

Accepted on the owner's request of 2026-10-15 (chat): narrow `S3Manifests` to the prefixes the
console actually reads, derive them from the source, guard it with an offline test, verify live.

## Problem

`deploy/console/iam-policy.json` statement `S3Manifests` grants `s3:GetObject` + `s3:ListBucket` on
`Resource: "*"` to `LlmopsAdminLambdaRole`: every object in every bucket of the account. The console
reads five things from one bucket. The one code path that does use the breadth is intake verification:
it checks any `s3://` URI a chat message names, in any bucket, and reports it `verified`. The harness
execution role that consumes `source_uri` can read only data-bucket prefixes. So "verified" could
describe an object the pipeline would get AccessDenied on.

## Desired outcome

The console role reads only the data-bucket prefixes the console's code reads. An intake URI outside
those prefixes is reported as not checked, with the reason, and is not reported as verified. A test
derives the read set from the source and fails when the policy and the code drift apart in either
direction.

## Acceptance

- No statement in the console policy grants `s3:GetObject` or `s3:ListBucket` on `*`.
- `simulate_principal_policy` on the live role: allowed for each derived prefix, denied for a
  non-derived prefix of the same bucket and for another bucket.
