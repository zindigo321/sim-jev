"""Deterministic scientific outputs and a noninteractive overview figure."""

from __future__ import annotations

from dataclasses import asdict
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pandas as pd

from .provenance import runtime_metadata

if TYPE_CHECKING:
    from .engine import Simulation


EVENT_COLUMNS = [
    "event_id", "time", "event_type", "entity_id", "old_state", "new_state", "details"
]
MISSION_COLUMNS = [
    "id", "start_time", "duration_hours", "aircraft_required", "value", "status",
    "assigned_aircraft", "actual_start", "actual_end", "actual_duration_hours",
    "failed_aircraft", "flight_hours", "earned_value",
]
STATE_COLUMNS = [
    "time", "idle", "flying", "waiting_repair", "repairing", "on_hand",
    "in_transit", "unmet_demand",
]
REPAIR_COLUMNS = [
    "id", "aircraft_id", "requested_at", "status", "started_at", "completed_at",
    "due_at", "spare_wait_hours", "bay_wait_hours",
]
ORDER_COLUMNS = [
    "id", "quantity", "ordered_at", "expected_arrival", "status", "arrived_at",
]


def check_output_directory(output_dir: str | Path) -> Path:
    """Validate the destination without modifying any existing result files."""
    destination = Path(output_dir)
    if destination.exists():
        if not destination.is_dir():
            raise ValueError(f"Output path is not a directory: {destination}")
        if next(destination.iterdir(), None) is not None:
            raise ValueError(
                f"Output directory is not empty: {destination}. "
                "Choose a new directory to preserve the existing run."
            )
    return destination


def _write_json(path: Path, data: Any) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")


def _write_csv(path: Path, rows: list[dict[str, Any]], columns: list[str]) -> None:
    # JSON cells retain list membership and detail structure without relying on repr.
    normalized = [
        {
            key: json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
            if isinstance(value, (dict, list, tuple)) else value
            for key, value in row.items()
        }
        for row in rows
    ]
    pd.DataFrame(normalized, columns=columns).to_csv(
        path, index=False, encoding="utf-8", lineterminator="\n"
    )


def _mission_rows(simulation: Simulation) -> list[dict[str, Any]]:
    rows = []
    for mission in sorted(simulation.missions.values(), key=lambda item: (item.spec.start_time, item.spec.id)):
        row = asdict(mission.spec)
        elapsed = 0.0
        if mission.actual_start is not None:
            end = simulation.now if mission.actual_end is None else mission.actual_end
            elapsed = end - mission.actual_start
        row.update(
            status=mission.status,
            assigned_aircraft=mission.assigned_aircraft,
            actual_start=mission.actual_start,
            actual_end=mission.actual_end,
            actual_duration_hours=elapsed,
            failed_aircraft=mission.failed_aircraft,
            flight_hours=mission.flight_hours,
            earned_value=mission.spec.value if mission.status == "success" else 0.0,
        )
        rows.append(row)
    return rows


def _write_overview(simulation: Simulation, destination: Path) -> None:
    # Set the default before importing Matplotlib; never write into the Conda env.
    if "MPLCONFIGDIR" not in os.environ:
        cache = destination.parent / ".mplconfig"
        cache.mkdir(parents=True, exist_ok=True)
        os.environ["MPLCONFIGDIR"] = str(cache.resolve())
    import matplotlib

    matplotlib.use("Agg")
    from matplotlib import pyplot as plt

    states = pd.DataFrame(simulation.history, columns=STATE_COLUMNS)
    figure, axes = plt.subplots(2, 1, figsize=(11, 7), sharex=True, constrained_layout=True)
    try:
        for name, color in (
            ("idle", "#277DA1"), ("flying", "#43AA8B"),
            ("waiting_repair", "#F8961E"), ("repairing", "#F94144"),
        ):
            axes[0].step(states["time"], states[name], where="post", label=name, color=color)
        axes[0].set_ylabel("Aircraft count")
        axes[0].set_ylim(bottom=0)
        axes[0].legend(ncols=4, loc="upper right")
        axes[0].set_title("Synthetic fleet simulation: aircraft states")
        for name, color in (
            ("on_hand", "#277DA1"), ("in_transit", "#43AA8B"),
            ("unmet_demand", "#F94144"),
        ):
            axes[1].step(states["time"], states[name], where="post", label=name, color=color)
        axes[1].set_ylabel("Spare quantity")
        axes[1].set_xlabel("Simulation time (hours)")
        axes[1].set_ylim(bottom=0)
        axes[1].set_xlim(0, simulation.now if simulation.now > 0 else 1)
        axes[1].legend(ncols=3, loc="upper right")
        for axis in axes:
            axis.grid(alpha=0.2)
        figure.savefig(destination / "overview.png", dpi=150)
    finally:
        plt.close(figure)


def write_run(simulation: Simulation, output_dir: str | Path, *, run_context: dict | None = None) -> Path:
    """Export a completed or paused run, refusing to overwrite existing outputs.

    ``state.json`` is an internal audit export, not a policy observation or a
    supported checkpoint-resume format. It can include hidden simulation state.
    """
    destination = check_output_directory(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    _write_json(destination / "config.json", simulation.config.to_dict())
    _write_csv(destination / "events.csv", simulation.events, EVENT_COLUMNS)
    _write_csv(destination / "missions.csv", _mission_rows(simulation), MISSION_COLUMNS)
    _write_csv(destination / "states.csv", simulation.history, STATE_COLUMNS)
    _write_csv(
        destination / "repairs.csv",
        [asdict(value) for _, value in sorted(simulation.repairs.items())], REPAIR_COLUMNS,
    )
    _write_csv(
        destination / "orders.csv",
        [asdict(value) for _, value in sorted(simulation.orders.items())], ORDER_COLUMNS,
    )
    _write_json(destination / "summary.json", simulation.summary())
    _write_json(destination / "state.json", simulation.export_state())
    _write_json(destination / "runtime.json", runtime_metadata(
        simulation.config.to_dict(), run_context=run_context))
    _write_overview(simulation, destination)
    return destination
