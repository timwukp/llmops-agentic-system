# Intent: the Architecture page shows the system as it is now

- **Slug:** architecture-refresh
- **Author:** Claude (AI agent)
- **Date:** 2026-10-27
- **Accepted-by:** Tim WU
- **Status:** accepted

Accepted on the owner's chat decisions of 2026-10-27: update the Get started → Introduction and
Architecture pages ("i don't think so, our app/solution design has been evolved a lot ... make sure
update our repo for the contents first, and then sync with the App"), and "好，先從 Architecture 開始".

## Problem

Measured against the repo on 2026-10-27:
- **The high-level diagram** names no lakehouse, Glue, S3 Tables, Bronze/Silver/Gold, Unified Studio,
  Model Registry, provenance card, `PublishProvenance` or canonical scorer: every one of those labels
  occurs 0 times in `docs/gen_architecture_svg.py`.
- **The page text** calls the system "an autonomous LLMOps distillation pipeline", and its low-level
  caption says the skills are "mounted from git", although all 19 mounts are `s3`.
- **`docs/ARCHITECTURE.md`** mentions the lakehouse and the card in passing, with no overview of
  the planes and nothing on the canonical identifier scorer.

## Outcome wanted

- The diagram, the page and the docs show four planes: pipeline, audit and operations, data,
  governance. Every new label is either derived from the file that makes it true or pinned by a
  test to it.
- Repo first, then the app: the console is deployed from the merged content.

## Acceptance

- The layout suite stays clean; new guards pin the planes, the Silver rules, the state count, the
  skill source kind in the caption, and the "never approves" claim to their sources.
- The deployed console's Architecture page, read by the owner.
