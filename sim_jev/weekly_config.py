"""Explicit weekly-mode input file; legacy Config JSON remains unchanged."""
from dataclasses import replace
import json
from pathlib import Path

from .config import Config
from .weekly import WeeklySimulation
from .weekly_policy import PolicySpec, RuleSpec
from .decision import policy_from_dict
from .versions import WEEKLY_MODEL_VERSION


def load_weekly_simulation(path, seed=None):
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    required = {"model_version", "scenario_id", "config", "rules", "policy"}
    if not isinstance(data, dict) or set(data) != required:
        raise ValueError("weekly input requires model_version/scenario_id/config/rules/policy")
    if data["model_version"] != WEEKLY_MODEL_VERSION:
        raise ValueError("unsupported weekly input model_version")
    config = Config.from_dict(data["config"])
    if seed is not None:
        config = replace(config, seed=seed)
    if not isinstance(data["rules"], list):
        raise ValueError("weekly rules must be an array")
    return WeeklySimulation(config, scenario_id=data["scenario_id"],
                            rules=[RuleSpec.from_dict(rule) for rule in data["rules"]],
                            policy=policy_from_dict(data["policy"]))
