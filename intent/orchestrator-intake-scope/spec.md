# Spec: orchestrator intake scope

- **Intent:** ./intent.md
- **Author:** Claude (AI agent)
- **Signed-off-by:** Tim WU
- **Accepted-by:** Tim WU
- **Status:** signed-off

## Requirements

1. Step 0 (DATA DISCOVERY): data already in S3 is usable in place ONLY under `s3://<bucket>/customer-data/`
   (data-prep refuses a `source_uri` anywhere else; no stage reads another bucket). Anything else comes in
   through the drop zone; the agent says so instead of asking for the URI.
2. Intake paragraph: the console checks S3 existence and row counts only under `customer-data/` and
   `runs/`. A fact whose detail starts "not checked: outside" was not examined, is not evidence the object
   is missing, and is answered with "the pipeline cannot read data there; drop the file into this thread".
3. `tests/test_console_s3_scope.py` gains three guards, each derived from its source:
   - the intake paragraph names every `_INTAKE_S3_PREFIXES` entry and not "broader IAM";
   - it quotes the head of `_verify_s3_uri`'s "not checked" f-string;
   - step 0 names every prefix data-prep's prompt requires, names the drop zone's prefix, and no longer
     asks for a URI elsewhere.
4. Deploy with `deploy/05_harnesses.py --region us-east-1 --agent orchestrator`. Gate it on: no RUNNING
   pipeline execution, no consult task `thinking` or `accepting`, and `tools/audit_drift.py` showing
   `systemPrompt` as the orchestrator's only drift. Verify with a live consult turn. READY is not
   evidence.
