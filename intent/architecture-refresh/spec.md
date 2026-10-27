# Spec: the Architecture page and docs, refreshed

- **Intent:** ./intent.md
- **Author:** Claude (AI agent)
- **Signed-off-by:** Tim WU
- **Accepted-by:** Tim WU
- **Status:** signed-off

## Requirements

1. **High-level diagram** (`docs/gen_architecture_svg.py` → `docs/architecture-high-level.svg`):
   - title and subtitle name LLMOps Governance and the raw-data-to-approval path;
   - the GOVERNANCE / OPERATIONS row is renamed OPERATIONS (it is the idle-spend sweep);
   - a DATA PLANE row: generate · land → Bronze → Silver → Gold → Gold extract, with the Silver
     rules parsed from `pipeline/lakehouse/transform.py`;
   - a GOVERNANCE PLANE row: gates + scorers → PublishProvenance λ → Model Registry → Unified
     Studio → approver, with the state count read from the ASL;
   - both rows below every existing row, so no existing wire moves; the canvas grows to 1300;
   - the data-prep and eval cards say what they do now (lakehouse review; gates, Wilson, scorers).
2. **Architecture page text** (`deploy/console/frontend.html`): the summary, the high-level caption
   (four planes), the low-level caption (skills from a pinned S3 mirror), and a "how to read" bullet
   for the governance plane.
3. **Docs** (`docs/ARCHITECTURE.md`, `docs/ARCHITECTURE.zh-TW.md`): §0 the four planes; §7d the
   canonical scorers and identifier grounding.
4. **Guards:** `tests/test_architecture_page.py`; negative controls that mutate the SVG and the
   page (the runner does not regenerate the SVG).
5. **Evidence** (`deploy/evidence/VERIFICATION_architecture_refresh.md`): the audit above, and the
   governance loop closed live on 2026-10-27 (v1 rejected, v2 approved in Unified Studio).
