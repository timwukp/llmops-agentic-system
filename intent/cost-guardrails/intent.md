# Intent: make Cost enforcement visible, switchable, and alarm on running jobs

- **Slug:** cost-guardrails
- **Author:** Claude (AI agent)
- **Date:** 2026-10-12
- **Accepted-by:** Tim WU
- **Status:** accepted

## Problem

After Cost v2 (#147), enforcement was a per-budget `block` mode chosen in a form: invisible
until someone opened a budget, and not demonstrable live. A budget breach was only noticed at
launch time — nothing watched runs that were already spending. Stopping a running job was
listed as "phase 2" with no way to opt in.

## Desired outcome (owner's direction, 2026-10-12)

1. Block mode becomes a visible ON/OFF button in the portal, OFF by default, that an admin
   switches ON when a budget must be enforced — people must see the feature and the option.
2. The default action on a budget breach is alarm + notification. Stopping a running job is a
   second ON/OFF switch, OFF by default — no hard-stop unless someone chooses it.
3. Running jobs are watched: a 15-minute schedule (owner chose this over button-only).
4. "Stop" stops the run and its training job only; endpoints are left to Teardown (owner chose
   this over also deleting endpoints).
5. README and every related doc updated, especially the Cost design.

## Acceptance

- Both switches render OFF on the live portal; flipping one is approver-only and logged.
- With both OFF a breach alarms and nothing is refused or stopped.
- The schedule exists and its invocation reaches the sweep.
