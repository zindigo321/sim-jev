"""Validated synthetic scenario parameters; all time quantities are hours."""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
import json
import math
from pathlib import Path
from typing import Any


def require_number(name: str, value: object, minimum: float = 0, *, positive: bool = False) -> None:
    """Reject booleans, non-finite values, and numbers outside their domain."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite:
        raise ValueError(f"{name} must be finite")
    if value < minimum or (positive and value <= minimum):
        relation = ">" if positive else ">="
        raise ValueError(f"{name} must be {relation} {minimum}")


def require_integer(name: str, value: object, minimum: int = 0) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")


@dataclass(frozen=True)
class Config:
    """First-stage assumptions, not calibrated aircraft or paper parameters."""

    horizon_hours: float = 720.0
    seed: int = 42
    fleet_size: int = 12
    mission_rate_per_hour: float = 0.12
    mission_duration_min_hours: float = 2.0
    mission_duration_max_hours: float = 6.0
    mission_aircraft_min: int = 1
    mission_aircraft_max: int = 3
    mission_value_min: float = 50.0
    mission_value_max: float = 150.0
    failure_rate_per_flight_hour: float = 0.008
    repair_bays: int = 2
    repair_duration_min_hours: float = 4.0
    repair_duration_max_hours: float = 12.0
    initial_spares: int = 4
    reorder_target: int = 4
    reorder_interval_hours: float = 24.0
    lead_time_min_hours: float = 24.0
    lead_time_max_hours: float = 48.0
    parameter_note: str = "All values are synthetic examples; no real-fleet calibration or strict paper replication."

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        require_integer("seed", self.seed)
        require_integer("fleet_size", self.fleet_size, 1)
        require_integer("repair_bays", self.repair_bays)
        require_integer("initial_spares", self.initial_spares)
        require_integer("reorder_target", self.reorder_target)
        require_integer("mission_aircraft_min", self.mission_aircraft_min, 1)
        require_integer("mission_aircraft_max", self.mission_aircraft_max, 1)
        require_number("horizon_hours", self.horizon_hours, positive=True)
        require_number("reorder_interval_hours", self.reorder_interval_hours, positive=True)
        require_number("mission_rate_per_hour", self.mission_rate_per_hour)
        require_number("failure_rate_per_flight_hour", self.failure_rate_per_flight_hour)
        for prefix, suffix, positive in (
            ("mission_duration", "_hours", True),
            ("mission_value", "", False),
            ("repair_duration", "_hours", False),
            ("lead_time", "_hours", False),
        ):
            lower_name = f"{prefix}_min{suffix}"
            upper_name = f"{prefix}_max{suffix}"
            lower, upper = getattr(self, lower_name), getattr(self, upper_name)
            require_number(lower_name, lower, positive=positive)
            require_number(upper_name, upper, positive=positive)
            if upper < lower:
                raise ValueError(f"{upper_name} must be >= {lower_name}")
        if self.mission_aircraft_max < self.mission_aircraft_min:
            raise ValueError("mission_aircraft_max must be >= mission_aircraft_min")
        if not isinstance(self.parameter_note, str) or not self.parameter_note.strip():
            raise ValueError("parameter_note must be a nonempty string")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, values: dict[str, Any]) -> Config:
        if not isinstance(values, dict):
            raise ValueError("Configuration must be a JSON object")
        allowed = {field.name for field in fields(cls)}
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(f"Unknown configuration fields: {', '.join(sorted(map(str, unknown)))}")
        return cls(**values)


def load_config(path: str | Path) -> Config:
    """Load UTF-8 JSON; missing fields use the documented synthetic defaults."""
    with Path(path).open("r", encoding="utf-8-sig") as stream:
        return Config.from_dict(json.load(stream))
