#!/usr/bin/env python3
"""Provision two delivery-capable operators using the established acceptance bootstrap.

The underlying two-user helper remains the canonical privacy-safe bootstrap implementation.
This wrapper extends its operator capability set for the approved-quotation -> delivery
execution acceptance path and strengthens profile verification so a handoff is never emitted
unless the server actually returned every required delivery authority.
"""

from __future__ import annotations

import bootstrap_two_user_acceptance as base

DELIVERY_ACCEPTANCE_CAPABILITIES = (
    "quotation.send",
    "quotation.approve",
    "delivery.manage",
)

for capability in DELIVERY_ACCEPTANCE_CAPABILITIES:
    if capability not in base.DEFAULT_CAPABILITIES:
        base.DEFAULT_CAPABILITIES.append(capability)

_base_profile_matches = base._profile_matches


def _delivery_profile_matches(
    profile: dict,
    *,
    organization_id: str,
    user_id: str,
    project_id: str,
) -> bool:
    capabilities = set(profile.get("capabilities", []))
    return (
        _base_profile_matches(
            profile,
            organization_id=organization_id,
            user_id=user_id,
            project_id=project_id,
        )
        and set(DELIVERY_ACCEPTANCE_CAPABILITIES).issubset(capabilities)
    )


# The base main resolves this function from its own module globals at runtime. Replace it only
# inside this delivery-specific entry point so generic acceptance semantics remain unchanged.
base._profile_matches = _delivery_profile_matches


def main(argv: list[str] | None = None) -> int:
    return base.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
