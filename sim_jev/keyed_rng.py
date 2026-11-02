"""Versioned entity-keyed exogenous samples for the synthetic weekly model.

The manifest is internal audit material, never policy input.  Calling order and
policy names are absent from every key.  This is not a prediction scenario API.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math

from .config import require_integer, require_number


KEYED_RNG_PROTOCOL_VERSION = "sha256-entity-counter-v1"
DISTRIBUTION_TRANSFORM_VERSION = "inverse-cdf-v1"
_KEY_FIELDS = {"protocol", "root_seed", "domain", "scenario", "entity_type",
               "entity_id", "generation", "purpose", "draw_index"}
_ENTRY_FIELDS = {"key", "key_sha256", "transform_version", "distribution", "value"}


def _shape(value, fields, name):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ValueError(f"{name}: expected exactly {sorted(fields)}")


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name}: expected nonempty text")


def canonical_key_bytes(key: dict) -> bytes:
    """UTF-8 canonical JSON, integer counters, explicit domain and version."""
    _shape(key, _KEY_FIELDS, "random key")
    if key["protocol"] != KEYED_RNG_PROTOCOL_VERSION:
        raise ValueError("unsupported keyed RNG protocol")
    for name in ("root_seed", "generation", "draw_index"):
        require_integer(name, key[name])
    for name in ("domain", "scenario", "entity_type", "entity_id", "purpose"):
        _text(key[name], name)
    if key["domain"] not in {"execution", "planning"}:
        raise ValueError("random domain must be execution or planning")
    return json.dumps(key, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _decode_value(value):
    if value == {"$float": "+inf"}:
        return math.inf
    require_number("manifest value", value)
    return value


class KeyedRNG:
    """Stateless SHA256 variates with a deduplicated, serializable audit ledger.

    Uniform/exponential transforms use an open 52-bit midpoint uniform. Integer
    samples use rejection on the full 256-bit digest, avoiding modulo bias.
    Degenerate distributions are still recorded under their semantic key.
    """

    def __init__(self, seed: int, scenario_id: str, domain: str = "execution",
                 manifest: list[dict] | None = None):
        require_integer("root seed", seed)
        _text(scenario_id, "scenario_id")
        _text(domain, "domain")
        if domain not in {"execution", "planning"}:
            raise ValueError("random domain must be execution or planning")
        self.seed, self.scenario_id, self.domain = seed, scenario_id, domain
        self.manifest: list[dict] = []
        self._entries: dict[bytes, dict] = {}
        if manifest is not None:
            self._restore_manifest(manifest)

    def _key(self, *, entity_type: str, entity_id: str, generation: int = 0,
             purpose: str, draw_index: int = 0) -> dict:
        key = {"protocol": KEYED_RNG_PROTOCOL_VERSION, "root_seed": self.seed,
               "domain": self.domain, "scenario": self.scenario_id,
               "entity_type": entity_type, "entity_id": entity_id,
               "generation": generation, "purpose": purpose, "draw_index": draw_index}
        canonical_key_bytes(key)
        return key

    @staticmethod
    def _validate_distribution(distribution, value):
        if not isinstance(distribution, dict):
            raise ValueError("manifest distribution must be an object")
        kind = distribution.get("kind")
        if kind in {"uniform", "integer"}:
            _shape(distribution, {"kind", "low", "high"}, "distribution")
            low, high = distribution["low"], distribution["high"]
            if kind == "integer":
                require_integer("integer low", low)
                require_integer("integer high", high)
                require_integer("integer result", value)
            else:
                require_number("uniform low", low)
                require_number("uniform high", high)
                require_number("uniform result", value)
            if high < low or not low <= value <= high:
                raise ValueError("manifest value outside distribution support")
        elif kind == "exponential":
            _shape(distribution, {"kind", "rate"}, "distribution")
            rate = distribution["rate"]
            require_number("exponential rate", rate)
            if rate == 0:
                if value != math.inf:
                    raise ValueError("zero-rate exponential must have infinite lifetime")
            else:
                require_number("exponential result", value)
        else:
            raise ValueError("unsupported manifest distribution")

    def _restore_manifest(self, manifest):
        if not isinstance(manifest, list):
            raise ValueError("manifest must be an array")
        for source in manifest:
            _shape(source, _ENTRY_FIELDS, "manifest entry")
            entry = copy.deepcopy(source)
            key = entry["key"]
            encoded = canonical_key_bytes(key)
            if (key["root_seed"], key["scenario"], key["domain"]) != (
                    self.seed, self.scenario_id, self.domain):
                raise ValueError("manifest key belongs to a different execution identity")
            if encoded in self._entries:
                raise ValueError("duplicate manifest key")
            if entry["key_sha256"] != hashlib.sha256(encoded).hexdigest():
                raise ValueError("manifest canonical key digest mismatch")
            if entry["transform_version"] != DISTRIBUTION_TRANSFORM_VERSION:
                raise ValueError("unsupported distribution transform version")
            value = _decode_value(entry["value"])
            self._validate_distribution(entry["distribution"], value)
            self.manifest.append(entry)
            self._entries[encoded] = entry

    def _draw(self, distribution: dict, key: dict):
        encoded = canonical_key_bytes(key)
        if encoded in self._entries:
            entry = self._entries[encoded]
            if entry["distribution"] != distribution:
                raise ValueError("one random purpose/key cannot use different distributions")
            return _decode_value(entry["value"])
        digest = hashlib.sha256(encoded).digest()
        # A midpoint over 52 bits is exactly inside (0, 1) in binary64.
        u = ((int.from_bytes(digest[:8], "big") >> 12) + 0.5) / 2**52
        kind = distribution["kind"]
        if kind == "uniform":
            low, high = distribution["low"], distribution["high"]
            value = float(low + (high - low) * u)
        elif kind == "exponential":
            rate = distribution["rate"]
            value = -math.log1p(-u) / rate if rate else math.inf
        else:
            low, high = distribution["low"], distribution["high"]
            width = high - low + 1
            if width > 2**256:
                raise ValueError("integer distribution width exceeds 256-bit protocol")
            limit = 2**256 - (2**256 % width)
            integer = int.from_bytes(digest, "big")
            rejection_index = 0
            while integer >= limit:
                rejection_index += 1
                integer = int.from_bytes(hashlib.sha256(
                    encoded + b"\x00integer-rejection\x00" +
                    str(rejection_index).encode("ascii")).digest(), "big")
            value = low + integer % width
        self._validate_distribution(distribution, value)
        entry = {"key": key, "key_sha256": hashlib.sha256(encoded).hexdigest(),
                 "transform_version": DISTRIBUTION_TRANSFORM_VERSION,
                 "distribution": distribution,
                 "value": {"$float": "+inf"} if value == math.inf else value}
        self.manifest.append(entry)
        self._entries[encoded] = entry
        return value

    def uniform(self, low: float, high: float, *, entity_type: str, entity_id: str,
                generation: int = 0, purpose: str, draw_index: int = 0) -> float:
        require_number("uniform low", low)
        require_number("uniform high", high, low)
        return self._draw({"kind": "uniform", "low": low, "high": high}, self._key(
            entity_type=entity_type, entity_id=entity_id, generation=generation,
            purpose=purpose, draw_index=draw_index))

    def exponential(self, rate: float, *, entity_type: str, entity_id: str,
                    generation: int = 0, purpose: str, draw_index: int = 0) -> float:
        require_number("exponential rate", rate)
        return self._draw({"kind": "exponential", "rate": rate}, self._key(
            entity_type=entity_type, entity_id=entity_id, generation=generation,
            purpose=purpose, draw_index=draw_index))

    def integer(self, low: int, high: int, *, entity_type: str, entity_id: str,
                generation: int = 0, purpose: str, draw_index: int = 0) -> int:
        require_integer("integer low", low)
        require_integer("integer high", high, low)
        return self._draw({"kind": "integer", "low": low, "high": high}, self._key(
            entity_type=entity_type, entity_id=entity_id, generation=generation,
            purpose=purpose, draw_index=draw_index))

    def to_dict(self) -> dict:
        return copy.deepcopy({"protocol_version": KEYED_RNG_PROTOCOL_VERSION,
                              "transform_version": DISTRIBUTION_TRANSFORM_VERSION,
                              "seed": self.seed, "scenario_id": self.scenario_id,
                              "domain": self.domain, "manifest": self.manifest})

    @classmethod
    def from_dict(cls, data: dict) -> "KeyedRNG":
        _shape(data, {"protocol_version", "transform_version", "seed", "scenario_id",
                      "domain", "manifest"}, "keyed RNG state")
        if data["protocol_version"] != KEYED_RNG_PROTOCOL_VERSION:
            raise ValueError("unsupported keyed RNG protocol")
        if data["transform_version"] != DISTRIBUTION_TRANSFORM_VERSION:
            raise ValueError("unsupported distribution transform version")
        # Restore the saved values/cache only; no variates are generated here.
        return cls(data["seed"], data["scenario_id"], data["domain"], data["manifest"])
