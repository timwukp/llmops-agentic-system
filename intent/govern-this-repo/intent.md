# Intent: govern this repository with the ai-native-sdlc gate

- **Slug:** govern-this-repo
- **Author:** Claude (AI agent)
- **Date:** 2026-09-22
- **Status:** draft

## Problem

This repository already runs its own evidence culture (D1 drift audit, D2-D5 controls,
probe ledger), but changes are not carried by a committed artifact chain: intent, spec and
plan live in session transcripts and memory, not in the tree. The owner is dogfooding his
ai-native-sdlc skill, and this repo is the chosen first candidate — small scale inside the
skill's verified envelope, an existing gate culture, GitHub-hosted.

This installation is also the first live-runtime exposure of the skill's Claude Code
PreToolUse surface, which its COMPATIBILITY.md labels "contract tested, live runtime
unproven". Refusals and misfires observed here are dogfood data, not merely friction.

## Desired outcome

Tier-2 enforcement, Monitor mode: the write-time hook active on both Kiro and Claude Code
surfaces (fail-open), the CI gate running on every PR but NOT a required check. After a
2-4 week observation window, the owner decides from the refusal record whether to promote
the gate to a required branch-protection check (Semgrep Monitor→Comment→Block pattern) or
back off.

## Acceptance

Owner-directed (2026-09-22 session: 「我點頭。從我自己做起」). Merging the installation
PR is the recorded confirmation.
