"""Explicit contracts; change these when scientific or random semantics change."""

MODEL_VERSION = "synthetic-single-lru-v1"
RNG_PROTOCOL_VERSION = "numpy-seedsequence-four-stream-v1"
CHECKPOINT_SCHEMA_VERSION = 1
AUDIT_STATE_SCHEMA_VERSION = 1
PROVENANCE_SCHEMA_VERSION = 1

# Separate identities: legacy snapshots remain schema 1 under MODEL_VERSION.
WEEKLY_MODEL_VERSION = "synthetic-weekly-single-lru-v1"
KEYED_RNG_PROTOCOL_VERSION = "sha256-entity-counter-v1"
WEEKLY_CHECKPOINT_SCHEMA_VERSION = 1
WEEKLY_POLICY_VERSION = "mandatory-first-nominal-v1"
DAILY_POLICY_VERSION = "id-dispatch-fifo-daily-stock-v1"

# Arithmetic is independent of the physical model and random draw protocols.
ARITHMETIC_PROTOCOL_LEGACY = "call-interval-binary64-v1"
ARITHMETIC_PROTOCOL_V2 = "event-anchor-binary64-v2"
WEEKLY_ANCHORED_CHECKPOINT_SCHEMA_VERSION = 2
