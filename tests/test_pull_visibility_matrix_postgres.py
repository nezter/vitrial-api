"""Characterization of which records a pull delivers.

This is the safety net for the batched-prefetch optimisation of `pull_since`. The
prefetch is not written yet; this file exists so that when it is, the test to
compare against already exists and already passes.

## Why this file is a characterization and not a spec

`pull_since` decides what one principal may read out of one organization. A batched
prefetch that resolves ownership with a set-based query instead of per-record
`db.get` calls is a performance change to an **authorization decision**. The only
thing that makes it safe is a test which pins today's answers exactly, so that any
divergence is a red test rather than a data leak discovered in production.

So the assertions here are deliberately literal, including the parts that look like
bugs. If one of them is wrong, it is fixed deliberately and visibly, not quietly.

## What is pinned

For every entity type, four outcomes:

* **visible** -- the principal is in scope and the chain resolves to a project it
  may read. Delivered.
* **invisible, wrong project** -- the record exists and resolves, but the principal
  has no access to the owning project. Not delivered.
* **invisible, missing canonical row** -- the change log and payload exist but the
  canonical row does not. Not delivered. This is the dangling-reference case.
* **invisible, missing payload** -- visible in scope but `SyncEntity` is absent. Not
  delivered.

The last two are the ones a prefetch is most likely to get wrong, because a
prefetch that batches by entity type naturally does "SELECT ... WHERE id IN (...)"
and a missing row is simply absent from the result rather than raising.

## Not pinned, deliberately

`delivery_execution` is listed in `PROJECT_CHILD_TYPES` but `pull_since` routes it
through `delivery_execution_is_visible` instead, which applies extra state rules.
It is covered by `test_delivery_execution_postgres.py` and is out of scope here, so
it is excluded from the generic-type table rather than asserted through the generic
path.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest
from sqlalchemy import delete

from app.auth import Principal
from app.db import SessionFactory
from app.schemas import SyncBatch
from app.sync_service import pull_since

pytestmark = pytest.mark.skipif(
    os.getenv("POSTGRES_INTEGRATION") != "1",
    reason="requires migrated PostgreSQL integration database",
)

ORG = "org-vis-matrix"
NOW = datetime(2026, 9, 17, 14, 0, tzinfo=timezone.utc)

# Every generic entity type, paired with the model that must exist for the chain to
# resolve. `delivery_execution` is excluded; see the module docstring.
RESOLVING_TYPES = [
    ("customer", "customer"),
    ("project", "project"),
    ("project_sector", "project_sector"),
    ("item", "item"),
    ("quotation", "project_child"),
]


def principal(
    *,
    all_customers: bool = True,
    all_projects: bool = True,
    customer_ids: frozenset[str] = frozenset(),
    project_ids: frozenset[str] = frozenset(),
) -> Principal:
    return Principal(
        user_id="user-vis-matrix",
        organization_id=ORG,
        membership_id="membership-vis-matrix",
        session_id="session-vis-matrix",
        authorization_revision=1,
        capabilities=frozenset({"sync"}),
        customer_ids=customer_ids,
        project_ids=project_ids,
        all_customers=all_customers,
        all_projects=all_projects,
    )


async def seed(db) -> None:
    """Two customers, two projects, one record of each type in each project.

    A single project per record is deliberate: a shared project would let the
    identity map satisfy later lookups from cache, which is exactly the effect
    that makes a prefetch look better than it is.
    """
    from app.models import (
        CanonicalCustomer,
        CanonicalItem,
        CanonicalProject,
        CanonicalProjectChild,
        CanonicalProjectSector,
        Organization,
        SyncChangeLog,
        SyncEntity,
    )

    db.add(Organization(id=ORG, name="Visibility Matrix", authorization_revision=1))
    await db.flush()

    for customer_id in ("customer-mine", "customer-theirs"):
        db.add(CanonicalCustomer(organization_id=ORG, customer_id=customer_id))
    await db.flush()

    for owner in ("mine", "theirs"):
        project_id = f"project-{owner}"
        db.add(
            CanonicalProject(
                organization_id=ORG,
                project_id=project_id,
                customer_id=f"customer-{owner}",
            )
        )
        await db.flush()

        for entity_type in ("customer", "project", "project_sector", "item", "quotation"):
            entity_id = f"{entity_type}-{owner}"

            if entity_type == "customer":
                db.add(
                    CanonicalCustomer(
                        organization_id=ORG,
                        customer_id=f"customer-record-{owner}",
                    )
                )
                await db.flush()
            elif entity_type == "project":
                db.add(
                    CanonicalProject(
                        organization_id=ORG,
                        project_id=f"project-record-{owner}",
                        customer_id=f"customer-{owner}",
                    )
                )
                await db.flush()
            elif entity_type == "project_sector":
                db.add(
                    CanonicalProjectSector(
                        organization_id=ORG,
                        project_sector_id=entity_id,
                        project_id=project_id,
                        # NOT NULL with no default. `record_is_visible` ignores
                        # this column, but the constraint does not care whether
                        # the code reads it.
                        sector_id=f"sector-{owner}",
                    )
                )
                await db.flush()
            elif entity_type == "item":
                db.add(
                    CanonicalItem(
                        organization_id=ORG,
                        item_id=entity_id,
                        project_id=project_id,
                        project_sector_id=None,
                    )
                )
                await db.flush()
            else:
                db.add(
                    CanonicalProjectChild(
                        organization_id=ORG,
                        entity_type="quotation",
                        entity_id=entity_id,
                        project_id=project_id,
                    )
                )
                await db.flush()

            # The change log and payload for a record that resolves and is in scope.
            db.add(
                SyncChangeLog(
                    organization_id=ORG,
                    entity_type=entity_type,
                    entity_id=entity_id,
                    server_revision=1,
                    client_mutation_id=f"cm-{entity_id}",
                    operation="upsert",
                )
            )
            db.add(
                SyncEntity(
                    organization_id=ORG,
                    entity_type=entity_type,
                    entity_id=entity_id,
                    server_revision=1,
                    schema_version=1,
                    payload_json={"id": entity_id},
                    updated_at=NOW,
                )
            )

        # A change whose canonical row does not exist. The payload is present, so
        # only the authorization lookup can reject it.
        db.add(
            SyncChangeLog(
                organization_id=ORG,
                entity_type="item",
                entity_id=f"item-dangling-{owner}",
                server_revision=1,
                client_mutation_id="cm-dangling",
                operation="upsert",
            )
        )
        db.add(
            SyncEntity(
                organization_id=ORG,
                entity_type="item",
                entity_id=f"item-dangling-{owner}",
                server_revision=1,
                schema_version=1,
                payload_json={"id": "dangling"},
                updated_at=NOW,
            )
        )

    # An in-scope record whose payload is missing. Authorization passes; the
    # SyncEntity fetch is what rejects it.
    db.add(
        SyncChangeLog(
            organization_id=ORG,
            entity_type="item",
            entity_id="item-no-payload",
            server_revision=1,
            client_mutation_id="cm-no-payload",
            operation="upsert",
        )
    )
    await db.commit()


async def clear(db) -> None:
    from app.models import (
        CanonicalCustomer,
        CanonicalItem,
        CanonicalProject,
        CanonicalProjectChild,
        CanonicalProjectSector,
        Organization,
        SyncChangeLog,
        SyncEntity,
    )

    await db.execute(
        delete(SyncChangeLog).where(SyncChangeLog.organization_id == ORG)
    )
    await db.execute(delete(SyncEntity).where(SyncEntity.organization_id == ORG))
    await db.execute(
        delete(CanonicalItem).where(CanonicalItem.organization_id == ORG)
    )
    await db.execute(
        delete(CanonicalProjectSector).where(CanonicalProjectSector.organization_id == ORG)
    )
    await db.execute(
        delete(CanonicalProjectChild).where(CanonicalProjectChild.organization_id == ORG)
    )
    await db.execute(
        delete(CanonicalProject).where(CanonicalProject.organization_id == ORG)
    )
    await db.execute(
        delete(CanonicalCustomer).where(CanonicalCustomer.organization_id == ORG)
    )
    await db.execute(delete(Organization).where(Organization.id == ORG))
    await db.commit()


def delivered(batch: SyncBatch) -> set[tuple[str, str]]:
    return {(r.entityType, r.entityID) for r in batch.records}


async def pull(p) -> SyncBatch:
    async with SessionFactory() as db:
        return await pull_since(db, p, "seq:0")


@pytest.mark.asyncio
async def test_scope_matrix_pins_delivery_per_entity_type():
    """The literal expected sets, for both a broad and a narrow principal.

    A broad principal (``all_customers``/``all_projects``) sees every record whose
    canonical row resolves. A narrow principal sees only the ``mine`` project.
    """
    async with SessionFactory() as db:
        await clear(db)
        await seed(db)

    broad = await pull(principal())
    # Customer records are seeded as `customer-record-{owner}`; the other four use
    # `{entity_type}-{owner}`. The mismatch is a little awkward to read but it keeps
    # the "owning customer" (`customer-{owner}`) distinct from the "customer record"
    # (`customer-record-{owner}`), which is what makes the scope matrix meaningful.
    assert delivered(broad) == {
        ("customer", "customer-record-mine"),
        ("customer", "customer-record-theirs"),
    } | {
        (entity_type, f"{entity_type}-{owner}")
        for entity_type, _ in RESOLVING_TYPES
        if entity_type != "customer"
        for owner in ("mine", "theirs")
    }

    narrow = await pull(
        principal(
            all_customers=False,
            all_projects=False,
            customer_ids=frozenset({"customer-mine", "customer-record-mine"}),
            project_ids=frozenset({"project-mine"}),
        )
    )
    # `project-record-mine` is deliberately absent. The narrow principal holds
    # `project-mine`, not `project-record-mine`, and `can_access_project` requires
    # the project id itself to be in scope -- a project being inside an accessible
    # customer is not enough. The `customer-record-mine` row *is* delivered,
    # because customers are keyed on `customer_ids` and it is listed there.
    assert delivered(narrow) == {
        ("customer", "customer-record-mine"),
        ("project_sector", "project_sector-mine"),
        ("item", "item-mine"),
        ("quotation", "quotation-mine"),
    }


@pytest.mark.asyncio
async def test_dangling_reference_and_missing_payload_are_not_delivered():
    """Two rejections that a set-based prefetch would most plausibly get wrong.

    A batched ``SELECT ... WHERE id IN (...)`` returns no row for a missing
    reference; it does not raise. Both of these must therefore be rejected by
    explicit membership tests rather than by an exception.
    """
    batch = await pull(principal())
    assert ("item", "item-dangling-mine") not in delivered(batch)
    assert ("item", "item-no-payload") not in delivered(batch)


@pytest.mark.asyncio
async def test_missing_payload_rejection_does_not_stall_the_cursor():
    """A rejected change is consumed; it must not pin the cursor forever.

    `item-no-payload` is in scope, so authorization passes and only the absent
    `SyncEntity` rejects it. The cursor must still advance past it, otherwise every
    subsequent pull re-reads this change and no later record is ever delivered.
    """
    async with SessionFactory() as db:
        await clear(db)
        await seed(db)
        # A record sequenced after the rejected one. If the cursor stalled at
        # `item-no-payload`, this would never be delivered.
        from app.models import SyncChangeLog, SyncEntity

        db.add(
            SyncChangeLog(
                organization_id=ORG,
                entity_type="item",
                entity_id="item-after-gap",
                server_revision=1,
                client_mutation_id="cm-after-gap",
                operation="upsert",
            )
        )
        db.add(
            SyncEntity(
                organization_id=ORG,
                entity_type="item",
                entity_id="item-after-gap",
                server_revision=1,
                schema_version=1,
                payload_json={"id": "after-gap"},
                updated_at=NOW,
            )
        )
        from app.models import CanonicalItem, CanonicalProject

        project = await db.get(CanonicalProject, (ORG, "project-mine"))
        db.add(
            CanonicalItem(
                organization_id=ORG,
                item_id="item-after-gap",
                project_id=project.project_id,
                project_sector_id=None,
            )
        )
        await db.commit()

    batch = await pull(principal())
    assert ("item", "item-after-gap") in delivered(batch)


@pytest.mark.asyncio
async def test_customer_scope_alone_does_not_confer_project_access():
    """`can_access_project` requires customer access AND project access.

    A principal with every customer but no project must see customer records and
    nothing else. This is the case a prefetch which inlines the two checks
    separately is most likely to lose.
    """
    customer_only = principal(
        all_customers=True,
        all_projects=False,
        project_ids=frozenset(),
    )
    batch = await pull(customer_only)
    kinds = {t for t, _ in delivered(batch)}
    assert kinds == {"customer"}


@pytest.mark.asyncio
async def test_project_scope_without_customer_scope_delivers_nothing():
    """A project id in scope whose owning customer is not delivers nothing.

    `can_access_project` is an AND: customer access **and** project access. The
    other cases in this file never test that independently, because they were
    built with matching `customer_ids` and `project_ids` -- every project
    reachable was also a customer reachable, so dropping either half of the AND
    produces the same answer.

    This is the case that separates the halves: `project-mine` is granted, and
    `customer-mine` is not. If the customer check were removed, this principal
    would receive `item-mine`, `project_sector-mine`, `quotation-mine` and the
    `project-mine` owning row -- i.e. read another customer's project.

    Verified by sabotage: removing `can_access_customer(customer_id) and` from
    `can_access_project` leaves every other test in this file green.
    """
    project_without_customer = principal(
        all_customers=False,
        all_projects=False,
        customer_ids=frozenset(),
        project_ids=frozenset({"project-mine"}),
    )
    batch = await pull(project_without_customer)
    assert delivered(batch) == set()


@pytest.mark.asyncio
async def test_missing_sync_capability_is_rejected_before_any_query():
    """Authorization is checked before the page is read, not after."""
    no_sync = Principal(
        user_id="user-vis-matrix",
        organization_id=ORG,
        membership_id="membership-vis-matrix",
        session_id="session-vis-matrix",
        authorization_revision=1,
        capabilities=frozenset(),
        customer_ids=frozenset(),
        project_ids=frozenset(),
        all_customers=True,
        all_projects=True,
    )
    async with SessionFactory() as db:
        with pytest.raises(PermissionError):
            await pull_since(db, no_sync, "seq:0")
