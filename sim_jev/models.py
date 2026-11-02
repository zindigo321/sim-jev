"""Explicit state records for the single heap-based simulation engine."""

from __future__ import annotations

from dataclasses import dataclass, field
import math

from .config import require_integer, require_number


@dataclass(frozen=True)
class MissionSpec:
    id: str
    start_time: float
    duration_hours: float
    aircraft_required: int
    value: float

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise ValueError("mission id must be a nonempty string")
        require_number("mission start_time", self.start_time)
        require_number("mission duration_hours", self.duration_hours, positive=True)
        require_integer("mission aircraft_required", self.aircraft_required, 1)
        require_number("mission value", self.value)


@dataclass
class Aircraft:
    id: str
    state: str = "idle"
    remaining_life_hours: float = math.inf
    flight_hours: float = 0.0
    mission_id: str | None = None
    version: int = 0
    failure_due_at: float | None = None


@dataclass
class Mission:
    spec: MissionSpec
    status: str = "pending"
    assigned_aircraft: list[str] = field(default_factory=list)
    actual_start: float | None = None
    actual_end: float | None = None
    failed_aircraft: list[str] = field(default_factory=list)
    flight_hours: float = 0.0


@dataclass
class Repair:
    id: str
    aircraft_id: str
    requested_at: float
    status: str = "waiting"
    started_at: float | None = None
    completed_at: float | None = None
    due_at: float | None = None
    spare_wait_hours: float = 0.0
    bay_wait_hours: float = 0.0


@dataclass
class Order:
    id: str
    quantity: int
    ordered_at: float
    expected_arrival: float
    status: str = "in_transit"
    arrived_at: float | None = None


@dataclass(order=True)
class Event:
    time: float
    priority: int
    sequence: int
    kind: str = field(compare=False)
    entity_id: str = field(compare=False)
    version: int = field(default=0, compare=False)
