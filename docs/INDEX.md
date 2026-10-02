---
id: VITR-GOV-002
title: "vitrial-api documentation index"
status: active
date: 2026-10-02
---

# Documentation Index

Entry point to the in-repo documentation. See `docs/CONVENTIONS.md` for the rules these follow.

| Doc | Purpose |
|---|---|
| `docs/CONVENTIONS.md` | Single-source-of-truth, defects namespacing, directory/naming/session rules |
| `trackers/vitrial-api-TRACKER.md` | Reconciled #28 checkbox state + defect table + verified defects |
| `docs/research/` | Findings that informed decisions or are not yet defects |
| `sessions/` | One note per working session; successes *and* dead ends |
| `memory-seeds/` | Cross-session continuity seeds harvested by memory_seed_service |
| `deploy/README.md` | Deploy, rollback, acceptance, preflight, GC runbook, evidence list |
| `docs/DEVICE_PAIRING_AND_SESSION_SECURITY.md` | Pairing operator flow + session security + resource bounds |
| `contracts/backend/v2/README.md` | V2 native-JSON protocol compatibility rules |
| `README.md` | Contract boundary, stack, V1 endpoints, observability, admin plane, invariants |

Agent-facing operational rules live in `~/.agents/skills/vitrial-api-guardrails/SKILL.md`;
repo state is owned by the files in this table, not the skill.

New file? Add it here. Edited an owner of an existing row? Bump the date in its frontmatter.
