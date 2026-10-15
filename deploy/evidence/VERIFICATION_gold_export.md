# VERIFICATION — Gold export, Phase 1 PR-08 (2026-10-15)

**What was deployed.** `deploy/10_lakehouse.py` created the Glue Python-shell job `llmops-lakehouse-export`
(Glue 3.0, 0.0625 DPU, `export.py` shipped with `--extra-py-files`) and the Glue role's new statements.
It also reconciled the Athena workgroup `llmops-lakehouse`; see the finding below. `--verify`: **48/48 PASS**,
the previous 46 plus two new checks: the workgroup sets no result encryption, and the data bucket has
default encryption.

## The smoke (`--smoke --seed 7 --scale demo`): 22/22 PASS

Dataset `seed7-demo-20261015-034233`: generate, land, Bronze → Silver → Gold, then the export job **twice**
on the same Gold snapshots.

| Check | Result |
|---|---|
| land / silver / gold jobs | SUCCEEDED |
| export run 1, run 2 | SUCCEEDED, SUCCEEDED |
| same sha256 on both runs | train `4dffee596fd0f5ed…` both times, eval `c891df2be9e47598…` both times |
| driver `_verify_gold_extract`, train and eval | `verified: true`: the driver read the file and hashed it, rather than accepting the digest the job wrote |
| export rows = Gold rows | 251 / 32 |
| 8 rules vs the generator's plants | 8/8 exact |
| Silver + Gold DQ | passed |
| Athena counts | silver 283, gold 251 / 32 |

**Reproducibility across separate runs as well.** An earlier smoke (`…-030456`, the run that found the
defect below) computed the same train digest `4dffee59…` for a different dataset id and a different Gold
snapshot before its INSERT failed. The digest is a property of the rows, not of the run, the snapshot id or
how Athena split its UNLOAD files. That is the claim the test
`test_the_extract_bytes_depend_on_the_rows_not_their_order_or_file_split` makes offline.

Read back after the run:
- `lakehouse/extracts/<dataset>/<train snapshot>/` holds `train.jsonl` (56,395 B), `val.jsonl` (7,204 B),
  `extract.json` and the UNLOAD parts under `unload/`. Every object is `AES256`, from the bucket default.
- `gold.dataset_manifest` has one row per split per export run, here four rows for this dataset (and two
  from the earlier smoke `…-033032`). Each row carries the URI, sha256, row count and the Silver report
  as `pii_report_uri`.
- **In the live portal** (`/?tab=data`): the Exports panel shows the extract prefix, train 251 `4dffee59…`
  and eval 32 `c891df2b…`, each with its snapshot, and the stage list includes `export`. An older dataset
  that was never exported shows "not exported yet", and switching back restores the panel.
  The console was deployed only after the
  console `S3Manifests` narrowing (#163) reached `main`: a concurrent deploy from that change had replaced
  the first one, and redeploying from a tree without #163 would have rolled its grant back. The narrowed
  grant already covers `lakehouse/reports/*/*`, where the export reports live, and the tab reads nothing
  else for this panel.

## Found: Athena cannot write a CMK-encrypted S3 Table when the workgroup sets result encryption

The first smoke's export failed at the `INSERT INTO "gold"."dataset_manifest"`:
`INTERNAL_ERROR_QUERY_ENGINE`, `Retryable: False`, with an empty message. The UNLOAD before it had succeeded.
Isolated on the live account, with each scratch table and workgroup deleted afterwards:

| Workgroup result encryption | Target table encryption | INSERT |
|---|---|---|
| `SSE_S3` (as deployed) | CMK (`llmops-curated`) | **FAILED**: also with a cast timestamp, a cast NULL, and an all-optional schema |
| `SSE_S3` | SSE-S3 (scratch table) | SUCCEEDED |
| none | CMK | SUCCEEDED |
| `SSE_KMS` naming the table's own CMK | CMK | **FAILED** |

`SELECT … FOR VERSION AS OF` and `UNLOAD` against the same CMK table succeeded under `SSE_S3`, so reads
reveal nothing about this. The workgroup now sets no `EncryptionConfiguration`. Query results and UNLOAD
output are still encrypted at rest by the data bucket's default (`AES256`, SSE-C blocked). `--verify` now
asserts that default, so the change cannot silently become "unencrypted" if the bucket's configuration
changes. `ensure_workgroup` removes the override from an existing workgroup instead of reporting `exists`.
Recorded in AGENTS.md's gotchas bank.

## Found: a run that has just SUCCEEDED can still hold the job's concurrency slot

The second smoke's export run 1 succeeded. Run 2 was then refused at `StartJobRun` with
`ConcurrentRunsExceededException`, because Glue had not yet released run 1's slot. `_run_job` retries
that one exception with backoff (15 s, 30 s, …, 6 attempts). Any other error propagates (tests:
`test_a_back_to_back_run_waits_out_the_previous_runs_concurrency_slot`,
`test_only_the_concurrency_refusal_is_retried`).

## Least privilege

- The export job runs as `llmops-lakehouse-glue`. Its new grants are Athena on workgroup `llmops-lakehouse`
  only, and Get/Put on `lakehouse/extracts/*` and `athena/lakehouse/*`. `--verify` asks IAM as the role
  (`simulate_principal_policy`): StartQueryExecution on `primary` is **denied**, and DeleteObject on an
  extract is **denied**. An export can add a file but cannot remove one.
- The driver reads extracts only under `s3://<DATA_BUCKET>/lakehouse/extracts/`. Another bucket,
  `customer-data/`, `runs/`, a `..` path or a non-S3 URI is refused before any read (tests).
- Macie: `03_storage.py` now reports PII-scan coverage of `lakehouse/extracts/` as its own job,
  `llmops-lakehouse-extracts-pii`. The `customer-data/` job does not count as coverage. Today it reports
  the gap; creating the job is a billed recurring scan and waits on the owner's `--enable-pii-scan`.

## Open

- `unload/` parts stay beside the extract, because the Glue role deliberately has no DeleteObject. A
  lifecycle rule on `lakehouse/extracts/*/unload/` is the clean way to expire them.
- `dataset_manifest` appends a row per export run, so a re-export of the same snapshot adds rows with
  identical digests. That is an honest event log; a "latest per (dataset, table)" view is PR-09's to
  define when training starts reading it.
