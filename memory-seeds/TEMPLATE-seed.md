---
format: memory-seed/v1
record_kind: continuity
category: seed-template
seed_type: template
name: memory-seed-template
date: 2026-10-02
repos: []
---

# Memory Seed Template

One seed per significant work stream. Record what to **do next** and what **not to touch**;
a seed that records only what was done goes stale. Keep it under ~2 MiB.

```markdown
---
kind: candidate            # candidate: prose capture; active: frontmatter-backed
recordKind: continuity
title: "<one-line title>"
tags: [vitrial-api, <area>]
entities: [task-####, VITR-V###, <repo>]
---

## State
- Repo @ HEAD, branch, clean/dirty, test result (real numbers, not "green").

## Good next step — <defect or task>
- What to change, where, and why.

## Do not touch without a deliberate decision
- The invariants that will look like bugs but are not (soft-delete visibility,
  delivery_execution deleted_at check, evidence PUT no middleware ceiling, admin
  include_in_schema=False, prefetch twins).

## Burned lessons (cost a cycle)
- Only ones that generalized, not one-off trivia.
```
