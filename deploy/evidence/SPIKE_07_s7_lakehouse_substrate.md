# SPIKE_07_s7_lakehouse_substrate — S3 Tables substrate: Iceberg v2, Glue 5 IAM-mode writes, deterministic UNLOAD FOR VERSION AS OF, Unified Studio visibility

- **Verdict:** INCOMPLETE
- **Gates:** Phase 1 PR-04 (lakehouse storage) and PR-05 (Unified Studio)
- **Region:** us-east-1
- **Ran:** 2026-10-13T06:34:09Z → 2026-10-13T06:37:14Z

## Pass criteria

| id | criterion | result | detail |
|---|---|---|---|
| v2 | A table created via the S3 Tables API has Iceberg format-version 2 | **PASS** | format-version=2 |
| integration | The account's S3 Tables ↔ Glue Data Catalog integration is enabled | **PASS** | s3tablescatalog federated catalog present |
| glue_write | A Glue 5 job writes 1000 rows with IAM-only grants (no Lake Formation) | **PASS** | SUCCEEDED |
| snapshot | The write produces a current snapshot id readable from table metadata | **PASS** | current-snapshot-id=1,453,185,831,670,343,737 |
| unload_det | Two UNLOADs of the same snapshot produce identical content hashes | **PASS** | sha256 a=173cace1d77e0355 b=173cace1d77e0355 |
| unload_count | The UNLOAD export contains exactly 1000 rows | **PASS** | rows=1000 |
| unload_nonempty | An UNLOAD into a non-empty prefix fails | **PASS** | FAILED: HIVE_PATH_ALREADY_EXISTS: Target directory already exists: s3://<DATA_BUCKET>/spikes/s7/unload-a/ |
| smus_visible | The table is visible in a Unified Studio IAM-based domain project | **NOT RUN** |  |
| smus_lineage | A Glue lineage node for the write appears in the domain | **NOT RUN** |  |
| smus_price | The domain's price is read from the AWS Price List API | **NOT RUN** |  |

## Observations

- **table_bucket:** `llmops-spike-s7-` + the run's 14-digit UTC start stamp (2026-10-13 06:34:09); later runs use `YYYYMMDD-HHMMSS`
- **format_version:** 2
- **snapshot_id:** 1,453,185,831,670,343,737
- **unload_digests:** ["173cace1d77e0355430bc22b985718f0243ef17246a792799aa65935a130f14c", "173cace1d77e0355430bc22b985718f0243ef17246a792799aa65935a130f14c"]

## Errors

- none

## Teardown

- all tracked resources deleted

## Run history (2026-10-13)

The table above is run 3. The first two runs are kept because each one is a finding.

| Run | Started (UTC) | Outcome | What it established |
|---|---|---|---|
| 1 | 06:24:18 | `v2` PASS; `integration` FAIL; rest NOT RUN | A table created through the S3 Tables API is Iceberg **format-version 2**. The account had **no S3 Tables ↔ Glue Data Catalog integration**; the spike stopped instead of enabling it (by design). The owner enabled it at 06:32 with `glue create-catalog` in IAM mode (`IAM_ALLOWED_PRINCIPALS` / `ALL`, no Lake Formation grants). |
| 2 | 06:32:46 | first call failed | `CreateTableBucket` with the name just deleted by run 1 returned **`ConflictException: The bucket is in a transitional state because of a previous deletion attempt`**. A deleted table bucket's name is not immediately reusable. Fix: a timestamped name per run (`BUCKET_PREFIX` + UTC stamp); Phase 1's teardown/re-create must do the same. |
| 3 | 06:34:09 | all 7 substrate criteria PASS | See the table above. Glue 5 wrote through the `s3tablescatalog` federation with **IAM-only grants** (no Lake Formation); two `UNLOAD … FOR VERSION AS OF` exports of one snapshot hashed **identically**; an `UNLOAD` into a non-empty prefix failed with `HIVE_PATH_ALREADY_EXISTS`. |

## Consequences for Phase 1

- **PR-04 can proceed on IAM mode.** No Lake Formation grant was needed for the Glue writer; the
  role in `glue_role_policy()` (resource-scoped `s3tables:*Table*`, Glue catalog get/update on the
  `s3tablescatalog/<bucket>` hierarchy, script/temp S3, logs) is the measured minimum shape.
- **The training export is reproducible** at the content level: the export step hashes sorted,
  decompressed lines, because file splits and gzip headers are not part of the data.
- **Every export needs a fresh, empty prefix** — `runs/<run_id>/distillation/` per run satisfies this.
- **Table bucket names must not be reused right after deletion.**
- **Still open (S7 `--part domain`):** Unified Studio IAM-based domain visibility, Glue lineage in the
  domain, and the domain's price.

Teardown re-checked live after run 3: no table bucket other than the AWS-managed `aws-s3`, no objects
under `spikes/s7/`, the Glue role, Glue job and Athena workgroup absent, and the tag sweep empty.
