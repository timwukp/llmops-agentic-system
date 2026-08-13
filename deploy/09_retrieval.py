#!/usr/bin/env python3
"""09_retrieval.py — provision the Bedrock Knowledge Base the RAFT runs retrieve from (idempotent).

Why this exists at all: r6c's diagnosis (deploy/evidence/SCALING_DIAGNOSIS_r6c_8B.md) showed the
acceptance set demands exactly the org-specific facts that a correct decontamination deletes from
the training rows — 41% of surviving rows dropped, and "no student size fixes the missing facts."
The exit with the strongest external evidence (deploy/evidence/RESEARCH_r6_direction.md) is to move
the org facts OUT of the weights and INTO a retrieval index that is legitimately available at
inference. This script builds that index. It is a DEPLOY concern, not a pipeline stage: a vector
collection bills by the hour whether or not a run is using it, and a standing cost is a human's
decision — the same argument as 03_storage.py --enable-pii-scan.

Creates (all tagged project=llmops-agentic-system):
  - IAM service role llmops-kb-service (trusted by bedrock.amazonaws.com, scoped to this
      account's knowledge bases): aoss data access on the one collection, s3 read on the
      corpus prefix ONLY, InvokeModel on the one embedding model
  - OpenSearch Serverless: encryption/network/data-access policies + VECTORSEARCH
      collection llmops-retrieval + one knn_vector index (1024-dim, titan-embed-v2)
  - Corpus objects: the customer's JSONL exploded to ONE OBJECT PER ROW under
      customer-data/kb-corpus/ — a Bedrock KB S3 data source treats each object as one
      document, so a single 300-row JSONL would ingest as ONE arbitrarily-chunked document
      and wreck oracle recall. Read-back verified; stale objects under the prefix removed.
  - Bedrock Knowledge Base llmops-org-facts + S3 data source whose inclusionPrefixes is
      exactly the corpus prefix — the acceptance sets (customer_eval_uri / ood_eval_uri)
      are structurally excluded from the index, which is the gate-integrity property this
      whole design leans on. The script REFUSES to proceed if either eval key sits under
      the inclusion prefix; a warned-and-continued contamination is not an exclusion.
  - --ingest: StartIngestionJob, polled to COMPLETED, and the document count RECONCILED
      against the corpus row count — a partial index looks exactly like a low-recall one.
  - SSM parameters /llmops/retrieval/{kb_id,data_source_id,collection_arn}

STANDING COST: an OpenSearch Serverless vector collection bills a minimum of 2 OCUs
(indexing + search, ~$0.24/OCU-hr) ≈ $11.52/day ≈ $175/month while it EXISTS, up to ~$350/month
at 4-OCU HA — whether or not anything queries it. This is the project's one deliberate exception
to the zero-standing-resources posture, per-run and human-authorized. Every create/exists result
prints this number and the teardown command; when the run's eval is done:
  python deploy/09_retrieval.py --region <r> --teardown

--dry-run prints the would-create plan without any AWS write. Pass --account-id for a fully
offline dry-run (no STS).

Usage:
  python deploy/09_retrieval.py --region us-east-1 \
      --source-uri s3://<bucket>/customer-data/<task>/corpus.jsonl \
      --customer-eval-key customer-data/<task>/id_acceptance.jsonl \
      --ood-eval-key customer-data/<task>/ood_acceptance.jsonl \
      [--ingest] [--dry-run]
  python deploy/09_retrieval.py --region us-east-1 --teardown
"""
import argparse
import json
import sys
import time
import urllib.request

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.exceptions import ClientError

TAG_KEY, TAG_VAL = "project", "llmops-agentic-system"
#: One collection, one index, one KB — spelled once each. The dry-run branch and the real
#: branch of every ensure_* below derive from these constants, for the same reason
#: 03_storage.py has EVAL_PREFIX: a mutation test there proved two hand-spelled copies of
#: one prefix could disagree while every guard stayed green.
COLLECTION = "llmops-retrieval"
INDEX = "llmops-kb-index"
KB_NAME = "llmops-org-facts"
KB_ROLE = "llmops-kb-service"
#: The ONLY prefix the data source ingests. Everything under it is index content; nothing
#: outside it can enter the index. The acceptance sets live outside it by refusal below.
CORPUS_PREFIX = "customer-data/kb-corpus/"
EMBED_MODEL = "amazon.titan-embed-text-v2:0"
VECTOR_DIM = 1024
#: Field names the Bedrock KB writes/reads in the index. Fixed by the KB, not chosen here.
VECTOR_FIELD = "bedrock-knowledge-base-default-vector"
TEXT_FIELD = "AMAZON_BEDROCK_TEXT_CHUNK"
METADATA_FIELD = "AMAZON_BEDROCK_METADATA"
SSM_PREFIX = "/llmops/retrieval/"
#: AOSS bills per OCU-hour with a 2-OCU floor (1 indexing + 1 search) while the collection
#: exists. Disclosed on every create/exists line; priced per run via the plan's
#: kb_ocu_hours (pipeline/contracts/cost_model.py, category retrieval_index).
OCU_HOURLY_USD = 0.24
MIN_OCUS = 2

STANDING_COST = (
    f"AOSS bills min {MIN_OCUS} OCU x ${OCU_HOURLY_USD}/OCU-hr ~= "
    f"${MIN_OCUS * OCU_HOURLY_USD * 24:.2f}/day (~$175/mo, up to ~$350/mo at 4-OCU HA) "
    f"while collection '{COLLECTION}' exists, queried or not. Stop it with: "
    f"python deploy/09_retrieval.py --region <region> --teardown"
)


def safe_client(service, region, dry):
    """boto3 client, but degrade to None under --dry-run when the environment has
    no usable AWS config at all (fully offline dry-run with --account-id)."""
    try:
        return boto3.client(service, region_name=region)
    except Exception:
        if dry:
            return None
        raise


def refuse_eval_keys_under_prefix(customer_eval_key, ood_eval_key):
    """The gate-integrity check, and it REFUSES rather than warns.

    The whole reason retrieval is allowed near this gate is that the acceptance files can
    never enter the index: the data source's inclusionPrefixes is CORPUS_PREFIX and nothing
    else. That property is structural only while the eval keys sit OUTSIDE the prefix — an
    operator who uploads id_acceptance.jsonl under kb-corpus/ has silently converted the
    open-book exam into an exam with the answer sheet stapled to it, and every downstream
    judge_score would still look legitimate. A warning that scrolls past in a deploy log
    does not stop that; an exit code does.
    """
    for name, key in (("customer_eval_key", customer_eval_key),
                      ("ood_eval_key", ood_eval_key)):
        if key.startswith(CORPUS_PREFIX):
            raise SystemExit(
                f"{name} '{key}' sits under the KB inclusion prefix '{CORPUS_PREFIX}' -- "
                f"the acceptance set would be INGESTED INTO THE INDEX the student retrieves "
                f"from, which staples the answer sheet to the exam. Move the eval file "
                f"outside {CORPUS_PREFIX} and re-run. Refusing.")
    return f"both eval keys verified outside {CORPUS_PREFIX}"


def parse_s3_uri(uri):
    if not uri.startswith("s3://"):
        raise SystemExit(f"--source-uri must be s3://... (got '{uri}')")
    rest = uri[len("s3://"):]
    bucket, _, key = rest.partition("/")
    if not bucket or not key:
        raise SystemExit(f"--source-uri must name a bucket and key (got '{uri}')")
    return bucket, key


def render_row(row, idx):
    """One KB document per corpus row: every string field, labeled, in a stable order.

    The corpus format is the CUSTOMER'S (the data-prep prompt refuses to guess it, and so
    does this script in spirit): rather than betting on field names like ticket/resolution,
    the document carries every scalar field under its own label. Oracle recall only needs
    the resolution text to be IN the document; a labeled dump guarantees that without a
    schema argument. Non-dict rows and rows with no usable text are refusals, not skips —
    a silently thinner index reads as a retrieval-quality problem later.
    """
    if not isinstance(row, dict):
        raise SystemExit(f"corpus row {idx} is not a JSON object -- refusing to build a "
                         f"document from it (a guessed rendering poisons recall silently)")
    parts = []
    for k in sorted(row):
        v = row[k]
        if isinstance(v, (dict, list)):
            v = json.dumps(v, ensure_ascii=False)
        text = str(v).strip()
        if text:
            parts.append(f"{k}: {text}")
    if not parts:
        raise SystemExit(f"corpus row {idx} has no non-empty fields -- refusing")
    doc_id = str(row.get("id", "")).strip() or f"row-{idx:05d}"
    # Keys must be S3-safe and stable across re-runs (same row -> same key, so re-explodes
    # are idempotent overwrites, not duplicate documents).
    doc_id = "".join(c if c.isalnum() or c in "-_." else "-" for c in doc_id)
    return doc_id, "\n\n".join(parts)


def ensure_corpus_objects(s3, bucket, source_uri, dry):
    """Explode the customer's JSONL into one object per row under CORPUS_PREFIX.

    A Bedrock KB S3 data source treats each OBJECT as one document. The customer corpus is
    one JSONL file; ingested directly it becomes ONE document that the chunker splits at
    arbitrary boundaries, so a retrieve for "password lockout" returns a window that starts
    mid-ticket-#12 and ends mid-ticket-#13 — oracle recall dies before the student ever
    answers. One object per ticket makes chunking NONE honest: document boundaries ARE
    ticket boundaries.

    Runs under the DEPLOYER's credentials on purpose: the pipeline role is read-only on
    customer-data/* (S3CustomerDataReadOnly) and must stay that way — an agent that can
    rewrite the corpus can rewrite the evidence its own answers are retrieved from.

    Every object is read back byte-for-byte (the ensure_code lesson), and stale objects
    under the prefix from a previous, larger corpus are DELETED — the --ingest step
    reconciles document counts against this function's row count, and leftovers would make
    that reconciliation lie in the direction nobody checks.
    """
    src_bucket, src_key = parse_s3_uri(source_uri)
    if dry:
        return {"would": f"explode s3://{src_bucket}/{src_key} into per-row objects",
                "to": f"s3://{bucket}/{CORPUS_PREFIX}"}
    body = s3.get_object(Bucket=src_bucket, Key=src_key)["Body"].read().decode("utf-8")
    rows = [json.loads(line) for line in body.splitlines() if line.strip()]
    if not rows:
        raise SystemExit(f"{source_uri} parsed to zero rows -- an empty index would make "
                         f"every retrieve miss and the run would fail looking like a "
                         f"model problem. Refusing.")
    written = {}
    for idx, row in enumerate(rows):
        doc_id, text = render_row(row, idx)
        if doc_id in written:
            raise SystemExit(f"corpus rows {written[doc_id]} and {idx} both render to "
                             f"doc id '{doc_id}' -- the second would silently overwrite "
                             f"the first in the index. Refusing.")
        key = f"{CORPUS_PREFIX}{doc_id}.txt"
        data = text.encode("utf-8")
        s3.put_object(Bucket=bucket, Key=key, Body=data)
        got = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        if got != data:
            raise SystemExit(f"read-back mismatch for s3://{bucket}/{key}")
        written[doc_id] = idx
    # Remove leftovers from any previous explode so ingest-count reconciliation is exact.
    keep = {f"{CORPUS_PREFIX}{d}.txt" for d in written}
    stale = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=CORPUS_PREFIX):
        for obj in page.get("Contents", []):
            if obj["Key"] not in keep:
                stale.append(obj["Key"])
    for key in stale:
        s3.delete_object(Bucket=bucket, Key=key)
    return {"rows": len(written), "to": f"s3://{bucket}/{CORPUS_PREFIX}",
            "read_back": "verified", "stale_removed": len(stale)}


def ensure_kb_role(iam, account_id, region, bucket, dry):
    """The service role the Knowledge Base assumes to read the corpus and write the index.

    Scoped to what a KB needs and nothing an agent could abuse: s3 read on the corpus
    prefix ONLY (not customer-data/* — the acceptance files live there, and this role not
    being able to read them is a second fence behind the inclusionPrefixes one),
    InvokeModel on the ONE embedding model, aoss access on the ONE collection. Trust is
    conditioned on this account's knowledge bases so another account's KB cannot ride it.
    """
    role_arn = f"arn:aws:iam::{account_id}:role/{KB_ROLE}"
    if dry:
        return {"role": KB_ROLE, "status": "would create/verify", "arn": role_arn}
    trust = {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": "bedrock.amazonaws.com"},
            "Action": "sts:AssumeRole",
            "Condition": {
                "StringEquals": {"aws:SourceAccount": account_id},
                "ArnLike": {"aws:SourceArn":
                            f"arn:aws:bedrock:{region}:{account_id}:knowledge-base/*"},
            },
        }],
    }
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "CorpusReadOnly",
                "Effect": "Allow",
                "Action": ["s3:GetObject"],
                "Resource": f"arn:aws:s3:::{bucket}/{CORPUS_PREFIX}*",
            },
            {
                "Sid": "CorpusList",
                "Effect": "Allow",
                "Action": ["s3:ListBucket"],
                "Resource": f"arn:aws:s3:::{bucket}",
                "Condition": {"StringLike": {"s3:prefix": [f"{CORPUS_PREFIX}*"]}},
            },
            {
                "Sid": "EmbedOnly",
                "Effect": "Allow",
                "Action": ["bedrock:InvokeModel"],
                "Resource": f"arn:aws:bedrock:{region}::foundation-model/{EMBED_MODEL}",
            },
            {
                "Sid": "CollectionAccess",
                "Effect": "Allow",
                "Action": ["aoss:APIAccessAll"],
                "Resource": f"arn:aws:aoss:{region}:{account_id}:collection/*",
            },
        ],
    }
    status = "exists"
    try:
        iam.get_role(RoleName=KB_ROLE)
    except ClientError as e:
        if e.response["Error"]["Code"] != "NoSuchEntity":
            raise
        iam.create_role(RoleName=KB_ROLE,
                        AssumeRolePolicyDocument=json.dumps(trust),
                        Tags=[{"Key": TAG_KEY, "Value": TAG_VAL}])
        iam.get_waiter("role_exists").wait(RoleName=KB_ROLE)
        status = "created"
    # Idempotent puts — trust and inline policy reapplied every run so drift heals.
    iam.update_assume_role_policy(RoleName=KB_ROLE, PolicyDocument=json.dumps(trust))
    iam.put_role_policy(RoleName=KB_ROLE, PolicyName=f"{KB_ROLE}-inline",
                        PolicyDocument=json.dumps(policy))
    return {"role": KB_ROLE, "status": status, "arn": role_arn}


def ensure_aoss_policies(aoss, account_id, region, deployer_arn, dry):
    """Encryption, network and data-access policies the collection cannot exist without.

    Data access lists TWO principals: the KB role (Bedrock's ingest/query path) and the
    DEPLOYER (this script must create the index — boto3 has no index API for AOSS, the
    index is a signed HTTP PUT against the collection endpoint under whoever runs this).
    """
    kb_role_arn = f"arn:aws:iam::{account_id}:role/{KB_ROLE}"
    if dry:
        return {"status": "would create/verify 3 policies "
                          f"(encryption/network/data) for collection {COLLECTION}"}
    made = []
    enc = json.dumps({
        "Rules": [{"ResourceType": "collection",
                   "Resource": [f"collection/{COLLECTION}"]}],
        "AWSOwnedKey": True,
    })
    net = json.dumps([{
        "Rules": [{"ResourceType": "collection",
                   "Resource": [f"collection/{COLLECTION}"]}],
        "AllowFromPublic": True,
    }])
    data = json.dumps([{
        "Rules": [
            {"ResourceType": "collection",
             "Resource": [f"collection/{COLLECTION}"],
             "Permission": ["aoss:DescribeCollectionItems",
                            "aoss:CreateCollectionItems",
                            "aoss:UpdateCollectionItems"]},
            {"ResourceType": "index",
             "Resource": [f"index/{COLLECTION}/*"],
             "Permission": ["aoss:CreateIndex", "aoss:DescribeIndex",
                            "aoss:UpdateIndex", "aoss:ReadDocument",
                            "aoss:WriteDocument"]},
        ],
        "Principal": [kb_role_arn, deployer_arn],
    }])
    for name, ptype, doc in ((f"{COLLECTION}-enc", "encryption", enc),
                             (f"{COLLECTION}-net", "network", net),
                             (f"{COLLECTION}-data", "data", data)):
        api = "create_access_policy" if ptype == "data" else "create_security_policy"
        try:
            getattr(aoss, api)(name=name, type=ptype, policy=doc)
            made.append(f"{name}: created")
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConflictException":
                raise
            made.append(f"{name}: exists")
    return {"status": made}


def ensure_collection(aoss, dry):
    if dry:
        return {"name": COLLECTION, "status": "would create (VECTORSEARCH)",
                "standing_cost": STANDING_COST}
    status = "exists"
    existing = aoss.batch_get_collection(names=[COLLECTION]).get("collectionDetails", [])
    if not existing:
        aoss.create_collection(name=COLLECTION, type="VECTORSEARCH",
                               tags=[{"key": TAG_KEY, "value": TAG_VAL}])
        status = "created"
    # Wait ACTIVE either way: a just-created collection is CREATING for minutes, and every
    # step after this needs its endpoint.
    for _ in range(120):
        got = aoss.batch_get_collection(names=[COLLECTION]).get("collectionDetails", [])
        if got and got[0].get("status") == "ACTIVE":
            return {"name": COLLECTION, "status": status,
                    "arn": got[0]["arn"], "endpoint": got[0]["collectionEndpoint"],
                    "standing_cost": STANDING_COST}
        time.sleep(10)
    raise SystemExit(f"collection {COLLECTION} never reached ACTIVE")


def _signed_request(method, url, region, body=None):
    """SigV4-signed HTTP against the collection data plane (service name 'aoss').

    boto3 has no API for AOSS indexes; this is the documented path. Kept to stdlib +
    botocore so the deploy has no dependency the rest of the repo lacks.
    """
    session = boto3.Session()
    creds = session.get_credentials().get_frozen_credentials()
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = AWSRequest(method=method, url=url, data=data,
                     headers={"Content-Type": "application/json"})
    SigV4Auth(creds, "aoss", region).add_auth(req)
    http_req = urllib.request.Request(url, data=data, method=method,
                                      headers=dict(req.headers))
    try:
        with urllib.request.urlopen(http_req) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8") or "{}")


def ensure_index(endpoint, region, dry):
    """The knn_vector index the KB stores embeddings in. Bedrock does NOT create it.

    Retried on 403: the data-access policy grants the deployer principal above, and AOSS
    propagates that grant asynchronously — the first PUT after a fresh collection reliably
    403s for a minute or two, which is propagation, not a permissions bug.
    """
    if dry:
        return {"index": INDEX, "status": "would create "
                f"(knn_vector {VECTOR_DIM}-dim for {EMBED_MODEL})"}
    mapping = {
        "settings": {"index": {"knn": True}},
        "mappings": {"properties": {
            VECTOR_FIELD: {
                "type": "knn_vector",
                "dimension": VECTOR_DIM,
                "method": {"name": "hnsw", "engine": "faiss",
                           "space_type": "innerproduct"},
            },
            TEXT_FIELD: {"type": "text"},
            METADATA_FIELD: {"type": "text", "index": False},
        }},
    }
    url = f"{endpoint}/{INDEX}"
    last = None
    for _ in range(30):
        code, resp = _signed_request("PUT", url, region, mapping)
        if code in (200, 201):
            # AOSS acknowledges the PUT before the index is queryable; the KB's first
            # ingest fails on a not-yet-visible index, so settle briefly.
            time.sleep(30)
            return {"index": INDEX, "status": "created"}
        err_type = (resp.get("error") or {}).get("type", "")
        if err_type == "resource_already_exists_exception":
            return {"index": INDEX, "status": "exists"}
        last = (code, resp)
        if code == 403:
            time.sleep(10)
            continue
        break
    raise SystemExit(f"index PUT failed: {last}")


def ensure_kb(bedrock_agent, account_id, region, collection_arn, dry):
    if dry:
        return {"kb": KB_NAME, "status": "would create"}
    for kb in bedrock_agent.get_paginator("list_knowledge_bases").paginate():
        for item in kb.get("knowledgeBaseSummaries", []):
            if item["name"] == KB_NAME:
                return {"kb": KB_NAME, "status": "exists",
                        "kb_id": item["knowledgeBaseId"]}
    created = bedrock_agent.create_knowledge_base(
        name=KB_NAME,
        description="Org-facts retrieval index for RAFT training and open-book eval "
                    "(r6d). Corpus prefix only; acceptance sets structurally excluded.",
        roleArn=f"arn:aws:iam::{account_id}:role/{KB_ROLE}",
        knowledgeBaseConfiguration={
            "type": "VECTOR",
            "vectorKnowledgeBaseConfiguration": {
                "embeddingModelArn":
                    f"arn:aws:bedrock:{region}::foundation-model/{EMBED_MODEL}",
            },
        },
        storageConfiguration={
            "type": "OPENSEARCH_SERVERLESS",
            "opensearchServerlessConfiguration": {
                "collectionArn": collection_arn,
                "vectorIndexName": INDEX,
                "fieldMapping": {"vectorField": VECTOR_FIELD,
                                 "textField": TEXT_FIELD,
                                 "metadataField": METADATA_FIELD},
            },
        },
        tags={TAG_KEY: TAG_VAL},
    )["knowledgeBase"]
    for _ in range(60):
        got = bedrock_agent.get_knowledge_base(
            knowledgeBaseId=created["knowledgeBaseId"])["knowledgeBase"]
        if got["status"] == "ACTIVE":
            return {"kb": KB_NAME, "status": "created",
                    "kb_id": got["knowledgeBaseId"]}
        if got["status"] == "FAILED":
            raise SystemExit(f"knowledge base {KB_NAME} reached FAILED: "
                             f"{got.get('failureReasons')}")
        time.sleep(5)
    raise SystemExit(f"knowledge base {KB_NAME} never reached ACTIVE")


def ensure_data_source(bedrock_agent, kb_id, bucket, dry):
    """S3 data source pinned to CORPUS_PREFIX with chunking NONE.

    chunking NONE is only honest because ensure_corpus_objects made document boundaries
    equal ticket boundaries; inclusionPrefixes is the structural exclusion the refusal
    check above defends.
    """
    if dry:
        return {"data_source": f"{KB_NAME}-s3", "status": "would create",
                "inclusion": [CORPUS_PREFIX], "chunking": "NONE"}
    for page in bedrock_agent.get_paginator("list_data_sources").paginate(
            knowledgeBaseId=kb_id):
        for item in page.get("dataSourceSummaries", []):
            if item["name"] == f"{KB_NAME}-s3":
                return {"data_source": f"{KB_NAME}-s3", "status": "exists",
                        "data_source_id": item["dataSourceId"]}
    created = bedrock_agent.create_data_source(
        knowledgeBaseId=kb_id,
        name=f"{KB_NAME}-s3",
        dataSourceConfiguration={
            "type": "S3",
            "s3Configuration": {
                "bucketArn": f"arn:aws:s3:::{bucket}",
                "inclusionPrefixes": [CORPUS_PREFIX],
            },
        },
        vectorIngestionConfiguration={
            "chunkingConfiguration": {"chunkingStrategy": "NONE"},
        },
    )["dataSource"]
    return {"data_source": f"{KB_NAME}-s3", "status": "created",
            "data_source_id": created["dataSourceId"]}


def ingest(bedrock_agent, kb_id, ds_id, expected_rows, dry):
    """StartIngestionJob, polled to COMPLETE, with the count RECONCILED — not reported.

    scanned != corpus rows or failed > 0 is a refusal: a KB that indexed 214 of 300
    tickets answers retrieves confidently and the missing 86 read as "the student is bad
    at those categories" in the judge output, which is the least debuggable place this
    error could surface.
    """
    if dry:
        return {"status": f"would ingest and reconcile against {expected_rows} rows"}
    job = bedrock_agent.start_ingestion_job(
        knowledgeBaseId=kb_id, dataSourceId=ds_id)["ingestionJob"]
    for _ in range(360):
        got = bedrock_agent.get_ingestion_job(
            knowledgeBaseId=kb_id, dataSourceId=ds_id,
            ingestionJobId=job["ingestionJobId"])["ingestionJob"]
        if got["status"] == "COMPLETE":
            stats = got.get("statistics", {})
            scanned = stats.get("numberOfDocumentsScanned", 0)
            failed = stats.get("numberOfDocumentsFailed", 0)
            if scanned != expected_rows or failed:
                raise SystemExit(
                    f"ingestion count mismatch: scanned={scanned} failed={failed} "
                    f"expected={expected_rows}. A partially-indexed corpus surfaces as "
                    f"unexplained judge losses, not as an error. Refusing to report "
                    f"success.")
            return {"status": "COMPLETE", "scanned": scanned,
                    "indexed_new": stats.get("numberOfNewDocumentsIndexed", 0),
                    "indexed_modified": stats.get("numberOfModifiedDocumentsIndexed", 0),
                    "failed": failed, "reconciled_against": expected_rows}
        if got["status"] == "FAILED":
            raise SystemExit(f"ingestion job FAILED: {got.get('failureReasons')}")
        time.sleep(10)
    raise SystemExit("ingestion job never reached COMPLETE")


def ensure_ssm(ssm, values, dry):
    if dry:
        return {"would": [f"{SSM_PREFIX}{k}" for k in values]}
    for k, v in values.items():
        ssm.put_parameter(Name=f"{SSM_PREFIX}{k}", Value=v,
                          Type="String", Overwrite=True)
    return {"written": [f"{SSM_PREFIX}{k}" for k in values]}


def teardown(region, account_id, dry):
    """Delete everything ensure_* creates, in reverse order, tolerating partial state.

    Every ensure has its counterpart here — guarded by test, because a teardown that
    forgets the collection leaves the ONE billable resource standing while reporting the
    cleanup done, which is the worst possible asymmetry for the one script whose reason
    to exist is that the collection bills by the hour.
    """
    if dry:
        return {"would": ["delete data source", "delete knowledge base",
                          f"delete collection {COLLECTION} (stops the OCU bill)",
                          "delete 3 aoss policies", "delete ssm params",
                          f"delete role {KB_ROLE}"]}
    out = {}
    bedrock_agent = boto3.client("bedrock-agent", region_name=region)
    kb_id = None
    for page in bedrock_agent.get_paginator("list_knowledge_bases").paginate():
        for item in page.get("knowledgeBaseSummaries", []):
            if item["name"] == KB_NAME:
                kb_id = item["knowledgeBaseId"]
    if kb_id:
        for page in bedrock_agent.get_paginator("list_data_sources").paginate(
                knowledgeBaseId=kb_id):
            for item in page.get("dataSourceSummaries", []):
                bedrock_agent.delete_data_source(knowledgeBaseId=kb_id,
                                                 dataSourceId=item["dataSourceId"])
        out["data_source"] = "deleted"
        bedrock_agent.delete_knowledge_base(knowledgeBaseId=kb_id)
        out["knowledge_base"] = f"deleted ({kb_id})"
    else:
        out["knowledge_base"] = "absent"
    aoss = boto3.client("opensearchserverless", region_name=region)
    existing = aoss.batch_get_collection(names=[COLLECTION]).get("collectionDetails", [])
    if existing:
        aoss.delete_collection(id=existing[0]["id"])
        # Wait for it to be GONE: "delete requested" is not "bill stopped".
        for _ in range(60):
            if not aoss.batch_get_collection(
                    names=[COLLECTION]).get("collectionDetails", []):
                break
            time.sleep(10)
        out["collection"] = f"deleted ({COLLECTION}) -- standing cost stopped"
    else:
        out["collection"] = "absent -- no standing cost"
    for name, ptype in ((f"{COLLECTION}-data", "data"),
                        (f"{COLLECTION}-net", "network"),
                        (f"{COLLECTION}-enc", "encryption")):
        api = "delete_access_policy" if ptype == "data" else "delete_security_policy"
        try:
            getattr(aoss, api)(name=name, type=ptype)
            out[name] = "deleted"
        except ClientError as e:
            if e.response["Error"]["Code"] != "ResourceNotFoundException":
                raise
            out[name] = "absent"
    ssm = boto3.client("ssm", region_name=region)
    for k in ("kb_id", "data_source_id", "collection_arn"):
        try:
            ssm.delete_parameter(Name=f"{SSM_PREFIX}{k}")
            out[f"{SSM_PREFIX}{k}"] = "deleted"
        except ClientError as e:
            if e.response["Error"]["Code"] != "ParameterNotFound":
                raise
            out[f"{SSM_PREFIX}{k}"] = "absent"
    iam = boto3.client("iam", region_name=region)
    try:
        iam.delete_role_policy(RoleName=KB_ROLE, PolicyName=f"{KB_ROLE}-inline")
        iam.delete_role(RoleName=KB_ROLE)
        out["role"] = f"deleted ({KB_ROLE})"
    except ClientError as e:
        if e.response["Error"]["Code"] != "NoSuchEntity":
            raise
        out["role"] = "absent"
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", required=True)
    ap.add_argument("--account-id", help="skip STS (offline dry-run) and use this account id")
    ap.add_argument("--bucket", help="data bucket (default llmops-agentic-<acct>-<region>)")
    ap.add_argument("--source-uri",
                    help="s3://... JSONL corpus to explode into the index, one object per "
                         "row. Required unless --teardown.")
    ap.add_argument("--customer-eval-key",
                    help=f"bucket-relative key of the gated acceptance set. Required "
                         f"unless --teardown: the script must PROVE it sits outside "
                         f"{CORPUS_PREFIX} before any ingest can be allowed.")
    ap.add_argument("--ood-eval-key",
                    help="bucket-relative key of the report-only OOD set. Required "
                         "unless --teardown, same refusal.")
    ap.add_argument("--ingest", action="store_true",
                    help="StartIngestionJob and reconcile document counts. Billable "
                         "(embedding tokens); a deploy decision, never an agent's.")
    ap.add_argument("--teardown", action="store_true",
                    help=f"delete everything this script creates, in reverse order. "
                         f"This is what stops the OCU bill.")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    account_id = args.account_id
    if not account_id:
        account_id = boto3.client("sts", region_name=args.region) \
            .get_caller_identity()["Account"]
    bucket = args.bucket or f"llmops-agentic-{account_id}-{args.region}"

    if args.teardown:
        print(json.dumps({"teardown": teardown(args.region, account_id, args.dry_run),
                          "dry_run": args.dry_run}, indent=2))
        return 0

    for flag, val in (("--source-uri", args.source_uri),
                      ("--customer-eval-key", args.customer_eval_key),
                      ("--ood-eval-key", args.ood_eval_key)):
        if not val:
            raise SystemExit(f"{flag} is required (or pass --teardown). The eval keys are "
                             f"required BECAUSE they are how the script proves the "
                             f"acceptance sets cannot enter the index.")

    results = {}
    results["gate_integrity"] = refuse_eval_keys_under_prefix(
        args.customer_eval_key, args.ood_eval_key)

    s3 = safe_client("s3", args.region, args.dry_run)
    iam = safe_client("iam", args.region, args.dry_run)
    aoss = safe_client("opensearchserverless", args.region, args.dry_run)
    bedrock_agent = safe_client("bedrock-agent", args.region, args.dry_run)

    if args.dry_run:
        deployer_arn = f"arn:aws:iam::{account_id}:root"
    else:
        deployer_arn = boto3.client("sts", region_name=args.region) \
            .get_caller_identity()["Arn"]

    results["kb_role"] = ensure_kb_role(iam, account_id, args.region, bucket,
                                        args.dry_run)
    results["aoss_policies"] = ensure_aoss_policies(aoss, account_id, args.region,
                                                    deployer_arn, args.dry_run)
    coll = ensure_collection(aoss, args.dry_run)
    results["collection"] = coll
    results["index"] = ensure_index(coll.get("endpoint", ""), args.region, args.dry_run)
    corpus = ensure_corpus_objects(s3, bucket, args.source_uri, args.dry_run)
    results["corpus"] = corpus
    kb = ensure_kb(bedrock_agent, account_id, args.region,
                   coll.get("arn", ""), args.dry_run)
    results["knowledge_base"] = kb
    ds = ensure_data_source(bedrock_agent, kb.get("kb_id", ""), bucket, args.dry_run)
    results["data_source"] = ds
    if args.ingest:
        results["ingestion"] = ingest(bedrock_agent, kb.get("kb_id", ""),
                                      ds.get("data_source_id", ""),
                                      corpus.get("rows", 0), args.dry_run)
    else:
        results["ingestion"] = ("skipped (pass --ingest; embedding is billed work and a "
                                "deploy decision)")
    ssm = safe_client("ssm", args.region, args.dry_run)
    results["ssm"] = ensure_ssm(ssm, {
        "kb_id": kb.get("kb_id", ""),
        "data_source_id": ds.get("data_source_id", ""),
        "collection_arn": coll.get("arn", ""),
    }, args.dry_run)
    results["standing_cost"] = STANDING_COST
    results["dry_run"] = args.dry_run
    print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
