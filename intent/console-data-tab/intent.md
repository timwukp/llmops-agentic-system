# Intent: see the lakehouse in the console

- **Slug:** console-data-tab
- **Author:** Claude (AI agent)
- **Date:** 2026-10-14
- **Accepted-by:** Tim WU
- **Status:** accepted

## Problem

The owner asked when the new features would be visible in the UI (2026-10-14) and chose a read-only
Data tab right after PR-07, ahead of the full tab planned for PR-10.

## Desired outcome

The console shows each lakehouse dataset's tables, rule results against the plants and DQ outcomes,
without gaining any access to the table data.

## Acceptance

- Live in the portal after a smoke run; a bad dataset id refused; older datasets degrade cleanly.
- The tab's code makes no table-data or decrypt call; its only new grant is metadata-only.
