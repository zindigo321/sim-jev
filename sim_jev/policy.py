"""Stable, inspectable rules without random draws or mutation."""

from __future__ import annotations

from collections.abc import Iterable

from .models import Aircraft, Repair


def dispatch_aircraft(aircraft: Iterable[Aircraft], required: int) -> list[Aircraft]:
    idle = sorted((item for item in aircraft if item.state == "idle"), key=lambda item: item.id)
    return idle[:required] if len(idle) >= required else []


def prioritize_repairs(repairs: Iterable[Repair]) -> list[Repair]:
    return sorted(
        (item for item in repairs if item.status == "waiting"),
        key=lambda item: (item.requested_at, item.id),
    )


def reorder_quantity(on_hand: int, in_transit: int, unmet_demand: int, target: int) -> int:
    """Order up to target inventory position, accounting for all waiting repairs."""
    return max(0, target - (on_hand + in_transit - unmet_demand))
