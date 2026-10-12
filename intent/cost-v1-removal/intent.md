# Intent: remove the v1 cost routes

- **Slug:** cost-v1-removal
- **Author:** Claude (AI agent)
- **Date:** 2026-10-12
- **Accepted-by:** Tim WU
- **Status:** accepted

## Problem

After Cost v2 (#147) and the visible enforcement switches (#148), the v1 cost surface — five
routes, four panels, and the `BUDGET_MODE=blocking` path — duplicated what v2 does, and the
blocking path was a second, invisible way to refuse a launch.

## Desired outcome (owner, 2026-10-12: "請刪掉舊的 v1 cost routes 開新 PR")

The five v1 routes and everything only they used are removed; what is still load-bearing
(rate-card health, `/api/finops-run`, the advisory $20,000 record in the signed approval) stays.

## Acceptance

- No v1 route, handler or panel remains; the portal's Cost tab is unchanged for a user.
- Signed task approvals keep their schema; accepting a plan dispatches it unless the Block
  switch refuses it.
