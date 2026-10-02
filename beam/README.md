# Vitrial BEAM services

A BEAM/Erlang reimplementation of the Vitrial Connected Operations API, living
alongside the FastAPI service in the repository root. The Python service is
retained unmodified as the **reference implementation and parity oracle** — it is
not being retired by this tree.

## Why this exists

The Python service is 7,884 lines of application code with 11,487 lines of tests
guarding it. It is well run, but it is single-process CPython: one core, no
in-process memory ceiling, and GC pauses on the tail. This tree moves the
request-serving and state-machine work onto the BEAM while keeping the same
observable contract.

The acceptance criterion is **behavioural parity**, measured against the Python
implementation. See `../vitrial-phase0/BEHAVIOUR-SPEC.md` for the rule set both
sides are checked against.

## Layout

Eight independently supervised applications under `apps/`:

| App | Owns |
|---|---|
| `vitrial_sync` | V1 + V2 sync engine, revision guard, idempotency, transient retry |
| `vitrial_ownership` | 13 entity types, canonical parent chain, capability enforcement |
| `vitrial_delivery` | 8-state delivery machine, plan freezing, append-only history |
| `vitrial_lifecycle` | 62-state quotation closure validator, blockers, provenance |
| `vitrial_evidence` | Streamed SHA-256 verification, S3 multipart staging, GC queue |
| `vitrial_auth` | Device pairing, sessions, admin boundary |
| `vitrial_reference` | Versioned reference-data publications |
| `vitrial_web` | HTTP boundary only — no business logic |

No application holds mutable state belonging to another. Each has its own
supervisor and restart policy, so one failing service cannot take down its
siblings.

## Dependency doctrine

Erlang/OTP and the Elixir standard library are the baseline. External packages
are adopted only where a native rewrite is genuinely worse, and every adoption
carries a written verdict in `../vitrial-phase0/DEPENDENCY-DOCTRINE.md`.

The umbrella root redirects `build_path`, `deps_path` and `lockfile` to the
repository root so that **one resolved dependency set and one lockfile govern
all eight applications**. Eight independent dependency trees would be eight
independent CVE surfaces to audit.

## Building

```bash
cd beam
mix compile
mix test
```

> **Note on `mix` on this machine.** `/usr/local/bin/mix` is a wrapper around
> `.ci_only_guard`, which blocks `mix compile` locally and exits 0 — so
> `mix new` silently does nothing. Use the real Elixir at
> `/home/nez/elixir/bin/mix` until that guard is updated for this repository.

## Latency budgets

Per-operation budgets and the measured envelopes behind them are in
`../vitrial-phase0/VERSIONS-AND-BUDGETS.md`. Targets are stated per layer
because a single end-to-end figure cannot be met or defended.

## Relationship to native-shared

`native-shared` was read as a **reference for design decisions only**. No code
is copied and no dependency is taken. Its BEAM packages are reimplemented here
from the decision, not from the source, per project doctrine.
