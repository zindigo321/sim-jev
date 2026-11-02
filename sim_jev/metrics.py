"""Time integrals and scientific summaries, independent of wall-clock time."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .engine import Simulation


@dataclass
class Metrics:
    available_aircraft_hours: float = 0.0
    idle_aircraft_hours: float = 0.0
    flight_hours: float = 0.0
    spare_wait_hours: float = 0.0
    bay_wait_hours: float = 0.0
    failures: int = 0
    repairs_started: int = 0
    repairs_completed: int = 0
    ordered_quantity: int = 0
    arrived_quantity: int = 0

    def integrate(self, dt: float, idle: int, flying: int, spare_wait: int, bay_wait: int) -> None:
        self.available_aircraft_hours += dt * (idle + flying)
        self.idle_aircraft_hours += dt * idle
        self.flight_hours += dt * flying
        self.spare_wait_hours += dt * spare_wait
        self.bay_wait_hours += dt * bay_wait


def summarize(sim: Simulation) -> dict:
    """Counts use offered missions; rates use elapsed time, including final interval."""
    offered = [m for m in sim.missions.values() if m.status != "pending"]
    counts = {status: sum(m.status == status for m in offered)
              for status in ("success", "failed", "not_executed", "running")}
    values = {status: sum(m.spec.value for m in offered if m.status == status)
              for status in counts}
    denominator = sim.config.fleet_size * sim.now
    metrics = sim.metrics
    total_value = sum(m.spec.value for m in offered)
    return {
        "parameter_basis": "synthetic; not a strict paper replication or real-fleet validation",
        "seed": sim.config.seed,
        "elapsed_hours": sim.now,
        "configured_horizon_hours": sim.config.horizon_hours,
        "missions_offered": len(offered),
        "missions_success": counts["success"],
        "missions_failed": counts["failed"],
        "missions_not_executed": counts["not_executed"],
        "missions_unfinished": counts["running"],
        "missions_future": sum(m.status == "pending" for m in sim.missions.values()),
        "mission_success_rate": counts["success"] / len(offered) if offered else None,
        "mission_value_offered": total_value,
        "mission_value_earned": values["success"],
        "mission_value_failed": values["failed"],
        "mission_value_not_executed": values["not_executed"],
        "mission_value_unfinished": values["running"],
        "mission_value_capture_rate": values["success"] / total_value if total_value else None,
        "flight_hours": metrics.flight_hours,
        "failures": metrics.failures,
        "repairs_started": metrics.repairs_started,
        "repairs_completed": metrics.repairs_completed,
        "spare_wait_hours": metrics.spare_wait_hours,
        "bay_wait_hours": metrics.bay_wait_hours,
        "total_repair_wait_hours": metrics.spare_wait_hours + metrics.bay_wait_hours,
        "available_aircraft_hours": metrics.available_aircraft_hours,
        "idle_aircraft_hours": metrics.idle_aircraft_hours,
        "availability": metrics.available_aircraft_hours / denominator if denominator else None,
        "idle_fraction": metrics.idle_aircraft_hours / denominator if denominator else None,
        "ordered_quantity": metrics.ordered_quantity,
        "arrived_quantity": metrics.arrived_quantity,
        "on_hand": sim.on_hand,
        "in_transit": sim.in_transit,
        "unmet_demand": sim.unmet_demand,
        "inventory_position": sim.on_hand + sim.in_transit - sim.unmet_demand,
        "aircraft_idle": sum(a.state == "idle" for a in sim.aircraft.values()),
        "aircraft_flying": sum(a.state == "flying" for a in sim.aircraft.values()),
        "aircraft_waiting_repair": sum(a.state == "waiting_repair" for a in sim.aircraft.values()),
        "aircraft_repairing": sum(a.state == "repairing" for a in sim.aircraft.values()),
        "orders_in_transit": sum(o.status == "in_transit" for o in sim.orders.values()),
    }
