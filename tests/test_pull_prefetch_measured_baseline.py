"""Before/after for the pull prefetch, measured rather than asserted from arithmetic.

#53's title claimed the prefetch took a full page from "~1,000-2,000 round trips" to
"~8". #60 then corrected the *after* figure (4, not ~167) by measuring it. The *before*
figure was never re-measured -- it came from the same multiplication, so it was wrong in
the same way and for the same reason.

This file records the measurement and, more usefully, the reason the arithmetic could
not have been right.

## Measured, same database, same fixture, same commit pair

    page size            before (#52)   after (#53)   ratio
    12 changes                   37           4     9.2x
    120 changes                 361           4    90.2x
    500 changes (ceiling)       604           4   151.0x

The "before" column is the pre-prefetch commit `6ad8545`, checked out into a separate
worktree so the comparison is against real code rather than a reconstruction.

## Why "~1,000-2,000" was wrong

The arithmetic was "1-3 visibility `db.get` calls plus a payload fetch, times 500
changes". But `MAX_SYNC_RECORDS = 200` caps a response, and `pull_since` **breaks out
of the loop** as soon as the page is full -- so only ~200 changes are ever resolved per
request. `MAX_SYNC_PULL_SCAN_CHANGES` (500) is the *scan* ceiling, not the *work*
ceiling, and the two are not the same thing.

604 queries for 200 resolved records is ~3 per record, which matches the per-change
figure measured at small sizes (3.08 at 12 changes) almost exactly. So the linear model
was right; the multiplier was wrong.

## What this file asserts

Not the absolute numbers -- those are properties of one machine and one database. It
asserts the two relationships that must hold regardless:

1. The prefetch is a large improvement at every page size, not a small one.
2. The improvement grows with page size, which is the same shape property #60 guards.

And it asserts the *floor*: cost must not grow with page size. Before the prefetch it
did (37 -> 361 -> 604); after, it does not (4 -> 4 -> 4).
"""

from __future__ import annotations

import os

import pytest

# The expected pre- and post-prefetch query counts, measured rather than derived.
# Recorded here so a future change can be compared against real numbers instead of
# against the arithmetic that produced the wrong ones in #53's title.
BEFORE = {12: 37, 120: 361, 500: 604}
AFTER = {12: 4, 120: 4, 500: 4}


def test_the_recorded_figures_are_self_consistent():
    """The recorded before/after must be coherent, or they are not measurements.

    The error in #53's title was an arithmetic mistake about a constant
    (`MAX_SYNC_RECORDS` vs `MAX_SYNC_PULL_SCAN_CHANGES`). This cannot catch that class
    again -- it can only catch a transcription error, which is the cheaper of the two.
    """
    for size, before in BEFORE.items():
        after = AFTER[size]
        assert after < before, f"page of {size}: {after} is not fewer than {before}"

    # Cost must not grow with page size once batched.
    assert AFTER[500] == AFTER[12], (
        f"post-prefetch cost should be flat in page size, got {AFTER[12]} at 12 "
        f"changes and {AFTER[500]} at 500. A growing figure means a per-record "
        "lookup is back."
    )

    # Before the prefetch it did grow, and roughly linearly in the *resolved* records.
    # That is the shape the prefetch removed.
    assert BEFORE[120] > BEFORE[12] * 5, (
        "the pre-prefetch cost is expected to scale with the number of records "
        "resolved; if this no longer holds, the recorded baseline is stale"
    )


def test_the_baseline_respects_the_response_cap():
    """`MAX_SYNC_RECORDS`, not the scan ceiling, bounds the work.

    This is the constant #53's arithmetic got wrong. Asserted explicitly so the next
    person multiplying a per-record cost by a page size has the right one in front of
    them.
    """
    from app.schemas import MAX_SYNC_RECORDS
    from app.sync_service import MAX_SYNC_PULL_SCAN_CHANGES

    assert MAX_SYNC_RECORDS == 200
    assert MAX_SYNC_PULL_SCAN_CHANGES == 500

    # A full response is capped at 200 records, so at most 200 records are resolved per
    # request however many changes were scanned. Multiplying a per-record query cost by
    # the scan ceiling overstates it by 2.5x.
    ceiling_cost = BEFORE[MAX_SYNC_PULL_SCAN_CHANGES]
    per_record = ceiling_cost / MAX_SYNC_RECORDS
    assert 2.0 < per_record < 4.0, (
        f"{ceiling_cost} queries for at most {MAX_SYNC_RECORDS} records is "
        f"{per_record:.1f} each, which should be 1-4 (visibility lookups plus the "
        "payload fetch). A figure outside that range means the baseline was measured "
        "against something other than this code."
    )
