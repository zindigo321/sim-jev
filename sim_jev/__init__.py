"""Synthetic aircraft fleet discrete-event research prototype."""

from .config import Config, load_config
from .models import MissionSpec

__all__ = ["Config", "MissionSpec", "Simulation", "WeeklySimulation", "load_config"]
__version__ = "0.1.0"


def __getattr__(name: str):
    # Keep configuration and state records importable without loading the engine.
    if name == "Simulation":
        from .engine import Simulation

        return Simulation
    if name == "WeeklySimulation":
        from .weekly import WeeklySimulation

        return WeeklySimulation
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
