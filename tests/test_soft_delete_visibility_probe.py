"""Does a soft-deleted record reach a pull?

`delivery_execution_is_visible` rejects a deleted payload, child, or project.
`record_is_visible` does not read `deleted_at` on any branch, so for every other
entity type a soft-deleted canonical row is invisible to that check.

This is a measurement, not a fix. It reports what the pull actually delivers for a
soft-deleted record so the decision about whether that is correct can be made on a
number rather than on a reading of the code.

Runs without PostgreSQL: `record_is_visible` only calls `db.get`, so a stub session
drives the shipped function unmodified.
"""

from __future__ import annotations

import datetime
import inspect

import pytest

from app.auth import Principal
from app.ownership import record_is_visible
from app.pull_prefetch import resolve_visible

ORG = "org-soft-delete-probe"
STAMP = datetime.datetime(2026, 9, 17, 14, 0, tzinfo=datetime.timezone.utc)


class Row:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class StubDB:
    def __init__(self, rows):
        self.rows = rows
        self.queries = 0

    async def get(self, model, pk):
        self.queries += 1
        return self.rows.get((model.__name__, pk))


def principal() -> Principal:
    """Broad scope: every record below is in scope, so only `deleted_at` can deny it."""
    return Principal(
        user_id="user-soft-delete",
        organization_id=ORG,
        membership_id="membership-soft-delete",
        session_id="session-soft-delete",
        authorization_revision=1,
        capabilities=frozenset({"sync"}),
        customer_ids=frozenset(),
        project_ids=frozenset(),
        all_customers=True,
        all_projects=True,
    )


# The generic branches of record_is_visible, and the rows each one resolves through.
CASES = {
    "customer": (("CanonicalCustomer", (ORG, "cust-1")), "cust-1"),
    "project": (("CanonicalProject", (ORG, "proj-1")), "proj-1"),
    "project_sector": (("CanonicalProjectSector", (ORG, "sec-1")), "sec-1"),
    "item": (("CanonicalItem", (ORG, "item-1")), "item-1"),
    "quotation": (("CanonicalProjectChild", (ORG, "quotation", "q-1")), "q-1"),
    "measurement": (("CanonicalItemChild", (ORG, "measurement", "m-1")), "m-1"),
}


def rows_for(*, soft_delete: str | None) -> dict:
    """Every row present and in scope. `soft_delete` names the one to mark deleted.

    The *parent* of each chain stays live, so the only thing that can deny delivery is
    the `deleted_at` on the record itself. That isolates the behaviour under test from
    every other reason a record might be withheld.
    """
    return {
        ("CanonicalCustomer", (ORG, "cust-1")): Row(
            customer_id="cust-1",
            deleted_at=STAMP if soft_delete == "customer" else None,
        ),
        ("CanonicalProject", (ORG, "proj-1")): Row(
            project_id="proj-1",
            customer_id="cust-1",
            deleted_at=STAMP if soft_delete == "project" else None,
        ),
        ("CanonicalProjectSector", (ORG, "sec-1")): Row(
            project_sector_id="sec-1",
            project_id="proj-1",
            sector_id="s",
            deleted_at=STAMP if soft_delete == "project_sector" else None,
        ),
        ("CanonicalItem", (ORG, "item-1")): Row(
            item_id="item-1",
            project_id="proj-1",
            project_sector_id=None,
            deleted_at=STAMP if soft_delete == "item" else None,
        ),
        ("CanonicalProjectChild", (ORG, "quotation", "q-1")): Row(
            entity_type="quotation",
            entity_id="q-1",
            project_id="proj-1",
            deleted_at=STAMP if soft_delete == "quotation" else None,
        ),
        ("CanonicalItemChild", (ORG, "measurement", "m-1")): Row(
            entity_type="measurement",
            entity_id="m-1",
            item_id="item-1",
            deleted_at=STAMP if soft_delete == "measurement" else None,
        ),
    }


async def delivered(entity_type: str, entity_id: str, *, soft_delete: str | None) -> bool:
    return await record_is_visible(
        StubDB(rows_for(soft_delete=soft_delete)),
        principal(),
        entity_type=entity_type,
        entity_id=entity_id,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "entity_type,entity_id",
    [(entity_type, lookup[1]) for entity_type, lookup in sorted(CASES.items())],
)
async def test_soft_deleted_record_is_still_delivered(entity_type, entity_id):
    """A soft-deleted record is delivered to an in-scope principal.

    Asserted as current behaviour so that changing it is a deliberate act. If this
    test goes red, someone has added a `deleted_at` check to `record_is_visible` --
    which is a defensible fix, but an authorization change that should be reviewed
    as one rather than picked up incidentally by a test edit.
    """
    live = await delivered(entity_type, entity_id, soft_delete=None)
    deleted = await delivered(entity_type, entity_id, soft_delete=entity_type)

    assert live is True, "the live record must be delivered, or this test is measuring nothing"
    assert deleted is True, (
        f"{entity_type}: a soft-deleted record is delivered to an in-scope principal. "
        "This is pre-existing behaviour in record_is_visible, which reads no "
        "deleted_at on any branch. delivery_execution_is_visible does check it. "
        "If the assertion here is wrong, the fix is an authorization change."
    )


@pytest.mark.asyncio
async def test_the_write_path_does_treat_soft_deletes_as_absent():
    """The asymmetry, stated as a fact about the codebase rather than a judgement.

    Delete-safety guards filter `deleted_at.is_(None)` when deciding whether a
    customer may be deleted, so the write path reasons about soft deletes and the
    read path does not. Both can be correct -- a tombstone may be meant to stay
    visible so clients can reconcile the delete -- but they should not be correct by
    accident, so the difference is pinned here.
    """
    import pathlib

    ownership = pathlib.Path("app/ownership.py").read_text(encoding="utf-8")
    # The delete-safety guards filter soft deletes explicitly.
    assert "deleted_at.is_(None)" in ownership, (
        "expected the delete-safety guards to filter deleted_at; if that changed, "
        "the asymmetry described here no longer exists"
    )

    body = inspect.getsource(record_is_visible)
    assert "deleted_at" not in body, (
        "record_is_visible now reads deleted_at. If that was intentional, the "
        "assertions in this file are wrong and the behaviour change needs review; "
        "if it was not, this is a regression."
    )
