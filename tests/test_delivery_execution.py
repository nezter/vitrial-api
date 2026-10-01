"""Delivery execution contract vocabulary and batch ordering.

Lifecycle and operational authority live in app.delivery_execution and are covered by
its policy, PostgreSQL, materials, procurement, production, installation and closeout
suites. This file pins only the public sync vocabulary and dependency ordering seams.
"""

from __future__ import annotations

import base64
import json
from typing import get_args

from app.ownership import ENTITY_ORDER, mutation_sort_key
from app.schemas import Capability, EntityType, SyncRecord


def test_capability_and_entity_type_are_part_of_the_vocabulary():
    assert "delivery.manage" in get_args(Capability)
    assert "delivery_execution" in get_args(EntityType)


def test_sync_record_accepts_a_delivery_execution():
    body = base64.b64encode(json.dumps({"status": "engineeringReview"}).encode()).decode()
    SyncRecord.model_validate(
        {
            "id": "record-1",
            "clientMutationID": "mutation-1",
            "entityType": "delivery_execution",
            "entityID": "execution-1",
            "updatedAt": "2026-09-28T00:00:00Z",
            "payload": body,
        }
    )


def test_delivery_execution_is_authorized_after_its_quotation():
    assert ENTITY_ORDER["delivery_execution"] > ENTITY_ORDER["quotation"]
    assert mutation_sort_key("delivery_execution", 0) > mutation_sort_key("quotation", 99)
