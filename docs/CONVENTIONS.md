---
id: VITR-GOV-001
title: "Documentation & Work Conventions"
owner: vitrial-api
category: GOV
status: active
date: 2026-10-02
---

# Documentation & Work Conventions

> **Scope:** everything under `/home/nez/Projects/vitrial-api/`
> **Enforcement:** agents working this repo follow these rules. Deviations need a stated reason.

---

## 1. Single Source of Truth

Every topic lives in **exactly one** place. Duplicating a tracker or a research
finding into two files creates a third, worse, unstated source.

| Topic | Lives in |
|---|---|
| Defects reproduced + severity | `trackers/vitrial-api-TRACKER.md` (defects section) and GitHub issues |
| What shipped, checkbox state | `trackers/vitrial-api-TRACKER.md` |
| Research tangents / findings | `docs/research/` |
| How-to-run evidence | `deploy/README.md` |
| Agent-facing code rules | `~/.agents/skills/vitrial-api-guardrails/SKILL.md` |

The guardrails skill is **agent orientation**, not the authority for repo state.
Repo state is owned by files in this repo. When the skill and a tracker disagree,
the tracker is wrong only after it no longer reflects `git log`.

## 2. Defect namespacing

Defects are verified, reproduced, and ranked. Never cite a bare finding number.

| Form | Use |
|---|---|
| `VITR-V###` | a verified defect in vitrial-api (e.g. `VITR-V001`) |
| `VITR-S###` | a spec / requirement gap (e.g. on issue #28) |

Cross-repo IDs (loom `Harness-V-`, session-context `SC-V-`) are never this repo's
namespace.

## 3. Directory layout

```
docs/                    human-written reference, no per-session prose
  CONVENTIONS.md         this file
  INDEX.md               one-line map of every doc
  research/              tangent findings, each dated, each citable
trackers/
  vitrial-api-TRACKER.md reconciled checkbox state + defect table + open issues
sessions/                one note per working session
  session-{N}.md         or session-{YYYY-MM-DD}-{slug}.md
memory-seeds/            seeds in the memory-seed/v1 record format, harvested by
                         ~/.agents memory_seed_service. Do NOT hand-edit index files.
```

## 4. File naming

- `UPPER-CASE.md` — canonical, top-level, important
- `Category-SUBJECT.md` — categorized doc in a subdir
- `session-{N}.md` — per-session log, never in repo root
- `TEMPLATE-{name}.md` — reusable template
- No spaces in filenames. No `*.bak*` files. Delete 0-byte files.

## 5. Session notes

One file per session under `sessions/`. Capture: what was attempted, what was found
(especially dead ends — they cost the next agent a cycle), what changed, and what
remains. A session note that records only successes is incomplete.

## 6. Research tangents

A tangent belongs in `docs/research/` when it informed a decision or disproved an
assumption. Format: date, the question, the evidence (command + output excerpt),
and the conclusion. `docs/research/` is the only place findings may hold a claim
that is *not* yet a defect — once reproduced as a defect, move it to the tracker.

## 7. Memory seeds

`memory-seeds/` holds one seed per significant work stream, in the
`memory-seed/v1` record format that `memory_seed_service` harvests. Seeds are for
**cross-session continuity**, not for spec. See `memory-seeds/TEMPLATE-seed.md`.
A seed that records what *was done* goes stale — record what to *do next* and what
to *not touch*.
