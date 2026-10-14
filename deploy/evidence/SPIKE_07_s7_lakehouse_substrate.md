# SPIKE_07_s7_lakehouse_substrate — S3 Tables substrate: Iceberg v2, Glue 5 IAM-mode writes, deterministic UNLOAD FOR VERSION AS OF, Unified Studio visibility

- **Verdict:** FAIL — substrate 7/7 PASS; Unified Studio: lineage and price PASS, visibility FAIL
- **Gates:** Phase 1 PR-04 (lakehouse storage) and PR-05 (Unified Studio)
- **Region:** us-east-1
- **Ran:** substrate 2026-10-13T06:34:09Z → 06:37:14Z; domain part 2026-10-14, 7 runs

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
| smus_visible | The table is visible as an asset in a Unified Studio project | **FAIL** | domain part, 7 runs (2026-10-14): the project role cannot read the IAM-mode `s3tablescatalog` — see "Domain part" |
| smus_lineage | The Glue write's OpenLineage event reaches the domain and is processed | **PASS** | 36 events, all `SUCCESS`; the output dataset's `symlinks` facet names `table/s3tablescatalog/<bucket>/spike/events` — read from the domain after run 7 |
| smus_price | The domain's price is read from the AWS Price List API | **PASS** | 11 price dimensions (global): no per-user charge; $0.4/GiB-month storage, $1.776 per compute unit, $10 per 100k requests, each with a free tier |

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

## Domain part (2026-10-14, `LLMOPS_S7_PART=domain`)

Run from the owner's credentials, not the build runner: creating a project in the shared domain needs
a DataZone domain member (`RUNNER_EXEMPT_SERVICES` in the spike says so, and `tests/test_runner.py`
reads it). The account's only Unified Studio domain is `demo`: a **classic V2 domain** (2024-12,
SSO disabled, project profiles and blueprints) holding seven projects of other work. It is not the newer
"IAM-based domain" type. Owner decision: add one spike project with the "Data analytics and AI-ML
model development" profile, delete it afterwards, and touch nothing else.

| Run | Got to | Stopped by | Change for the next run |
|---|---|---|---|
| 1 | `CreateProject` | the profile sets `allowCustomProjectResourceTags=false`: no `spike=` tag allowed | retry without tags; a spike project is found by its name prefix |
| 2 | project ACTIVE, Glue write SUCCEEDED | Tooling environment `VALIDATION_FAILED`: DataZone adds `AmazonSageMakerManageAccess-…` as a **Lake Formation admin**, and the account's LF admin list held a **deleted role** (`cdk-datazone-cfn-exec-role-<ACCOUNT_ID>-us-east-1`): "invalid principal". Project deletion then failed the same way | owner removed the stale LF admin (2026-10-14); the leftover project was deleted |
| 3 | all three environments ACTIVE | `CreateDataSource`: "For DataSources created for V2 domains, environment Id cannot be passed" | use the project's connection |
| 4 | connection `project.default_lakehouse` (type `LAKEHOUSE`) picked | a connection-based data source refuses `dataAccessRole`; then `StartDataSourceRun` before the data source was READY | run as the project role; wait for READY |
| 5 | data source created | `FAILED_CREATION`: "Not able to access Glue Catalog …s3tablescatalog/<bucket> … Project Role has IAM or Lake Formation permissions" | grant the project role read on the one catalog (removed before the project) |
| 6 | grant applied (`PutRolePolicy` OK) | same `FAILED_CREATION`; CloudTrail: `GetCatalog` **AccessDenied** for `datazone_usr_role_…`, 15 s after the grant | hypothesis: IAM propagation — wait 60 s, retry once |
| 7 | two attempts, 60 s apart | same `FAILED_CREATION` both times | propagation **rejected** |

**Price** came from the Price List API on the first run (global prices: `regionCode` is "" and `location`
is "Any", so a Region filter returns nothing — the spike's first attempt did exactly that).

**Lineage** was measured from the domain after run 7. Every run's Glue 5 write carried the documented
Spark configuration (OpenLineage listener, `amazon_datazone_api` transport, the domain id) and a
`datazone:PostLineageEvent` grant. 36 events arrived from the runs, all processed `SUCCESS`. On the output
dataset, `namespace` is the table's warehouse location and `name` is `/`, but the `symlinks` facet carries
the Glue identity `table/s3tablescatalog/<bucket>/spike/events` and the `schema` facet has the right
columns. A Glue-catalog symlink is what lets a lineage node be linked to an imported asset.

**Visibility is not established.** What is known:
- The project role carries the AWS managed boundary `SageMakerStudioProjectUserRolePermissionsBoundary`
  (v19). It does allow `glue:GetCatalog/GetTable(s)`, and its other Glue read paths are conditioned on
  `glue:LakeFormationPermissions = Enabled`.
- An inline IAM grant on that exact catalog did not stop the denial, while the spike's own Glue job role
  read the same catalog with IAM grants alone.
- The leading hypothesis is that a Unified Studio data source expects the S3 Tables integration in
  **Lake Formation mode** with grants to the project role. This account's integration is in **IAM mode**
  (`IAM_ALLOWED_PRINCIPALS`, created for S7 on 2026-10-13). The documentation page that would settle it
  was not reachable.

Teardown removed everything every run created. After run 2, only the project record was left (no
stacks, no roles); it was deleted once the LF admin was fixed.

## Consequences for Phase 1 PR-05 (Unified Studio)

- **Creating any project changes Lake Formation admins.** DataZone adds its ManageAccess role as an LF
  admin, which is an account-level change by design. A stale LF admin breaks project creation and
  deletion for every project in the account, not only ours.
- **PR-05 must choose the integration mode first.** Either move the S3 Tables integration to Lake
  Formation mode and grant the project role through LF (an account-level change, the owner's call), or
  keep IAM mode and accept that the tables are not catalog assets in Unified Studio. The lakehouse
  itself (PR-04/06/07) works in IAM mode either way.
- **Lineage needs no new mechanism.** The Spark configuration above is what PR-07's jobs would carry.
- **Domain choice is still open.** The shared `demo` domain is a classic V2 domain with other work in
  it; a new IAM-based domain would be a separate, account-level setup.

