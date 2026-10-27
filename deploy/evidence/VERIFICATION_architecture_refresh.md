# VERIFICATION — the Architecture page and docs, refreshed (2026-10-27)

## What the owner asked for

Check that Get started → Introduction and Architecture match the current design ("i don't think so,
our app/solution design has been evolved a lot"), update the repo first, then sync the app.
Architecture first.

## The audit, against the repo

- **`docs/gen_architecture_svg.py`, label counts before the change:** Lakehouse 0, Glue 0, Bronze 0,
  Silver 0, Gold 0, Unified Studio 0, SMUS 0, PublishProvenance 0, provenance 0, identifier 0,
  Outcome 0, S3 Tables 0, Model Registry 0, Guardrail 0, Athena 0.
- **The page:** "An autonomous LLMOps distillation pipeline, operated by agents"; the low-level
  caption said "skills mounted from git", while all 19 mounts are `s3` (the diagram's own STATE band,
  derived from the configs, said `s3`: the page's prose had decayed where the generated band had not).
- **`docs/ARCHITECTURE.md`:** the lakehouse 10 mentions, Unified Studio 1, `identifier_grounding` 0,
  no overview of the planes.

## The change

- **Two new rows** on the high-level diagram, below every existing row so no existing wire moved:
  DATA PLANE (generate · land → Bronze → Silver → Gold → extract) and GOVERNANCE PLANE (gates +
  scorers → PublishProvenance λ → Model Registry → Unified Studio → approver). The Silver rule list is
  parsed from `transform.py` and the state count from the ASL. The old GOVERNANCE / OPERATIONS row,
  which is the idle-spend sweep, is now OPERATIONS.
- **Layout:** `tests/test_svg_geometry.py` clean: 32 wires, 35 cards, no crossings, no wire
  through a card, no text over a wire or a card or off the canvas. Card subtitles were shortened to
  fit the 180px cards on a conservative width estimate (widest 162px).
- **Guards** in `tests/test_architecture_page.py`, each with a negative control that mutates the SVG
  or the page as the runner does (the runner does not regenerate the SVG, so a control mutating the
  generator would read an unchanged SVG and could never be caught; the first draft of these controls
  had exactly that hole).

## The governance loop, closed live (2026-10-27)

What the new GOVERNANCE PLANE draws happened end to end before it was drawn. In SageMaker Unified
Studio the owner rejected `llmops-student-store-copilot-lakehouse` version 1 (fabricates a case number
in every answer: identifier_grounding 0/120) at 11:20 and approved version 2 (120/120, all gates
pass) at 11:21. Read back with `DescribeModelPackage`: v1 `Rejected`, v2 `Approved`, both modified by
the project role `llmops-studio-execution` with the user profile in the session name. Two gaps, as
measured:
- `ApprovalDescription` is empty on both: the reason is on the model card, not on the decision.
- The approver is recorded as the shared project role, not a named person: the IAM-domain limitation
  (every project member shares one role); a named approver needs an Identity Center domain.

## Not yet verified live

- The console's Architecture page after `deploy/console/deploy.sh` (owner's terminal and reading).
