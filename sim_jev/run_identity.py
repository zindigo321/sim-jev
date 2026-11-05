"""Fail-closed, content-bound preflight for resumable phase-2D runs.

This module only reads existing evidence.  It never repairs a journal, advances a
simulation, reserves a model request, or writes an execution-attempt receipt.
"""
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path

from .branch_audit_store import read_json
from .checkpoint import _encode_life
from .provenance import canonical_sha256, code_fingerprint
from .versions import DAILY_POLICY_VERSION
from .weekly import WeeklySimulation

RUN_IDENTITY_VERSION = 'phase2d-immutable-run-v1'


def immutable_simulation_inputs(sim):
    """Include every reset input and protocol, retaining array and numeric values."""
    return {'scenario_id': sim.weekly_state['scenario_id'],
        'config': sim.config.to_dict(),
        'input_missions': None if sim._input_missions is None else [asdict(x) for x in sim._input_missions],
        'initial_lifetimes': None if sim._initial_lifetimes is None else [
            _encode_life(x, 'run_identity.initial_lifetimes') for x in sim._initial_lifetimes],
        'rules_input': deepcopy(sim.weekly_state['rules_input']),
        'policy_spec': deepcopy(sim.weekly_state['policy_spec']),
        'model_version': sim.model_version, 'rng_protocol_version': sim.rng_protocol_version,
        'checkpoint_schema_version': sim.checkpoint_schema_version,
        'arithmetic_protocol': sim.arithmetic_protocol,
        'decision_protocol_version': sim.decision_protocol_version,
        'daily_policy_version': DAILY_POLICY_VERSION}


def requested_run_identity(name, seed, method, ident, *, build_case_fn, policy_fn,
                           source_root, experiment_context=None, local_provider=None):
    policy = policy_fn(method, ident)
    if local_provider is not None and not _same(getattr(policy, 'identity', None), local_provider.identity):
        raise ValueError('run identity mismatch: local provider and policy/model/renderer differ')
    # Construction materializes frozen reset inputs, without run(0), a decision,
    # candidate DES evaluation, backend metadata, or network access.
    sim = build_case_fn(name, seed, policy)
    inputs = {'requested_scenario': name, 'requested_seed': seed, 'requested_method': method,
        'simulation': immutable_simulation_inputs(sim),
        'experiment_context': deepcopy(experiment_context) if experiment_context is not None else {
            'version': 'unfrozen-offline-engineering-recovery-v1'},
        'execution_source_sha256': code_fingerprint(source_root)['sha256']}
    return {'version': RUN_IDENTITY_VERSION, 'inputs': inputs,
            'identity_sha256': canonical_sha256(inputs)}


def _same(actual, expected):
    # Python treats 0, 0.0, and False as equal; the strict JSON identity also
    # prevents those substitutions while allowing JSON object key permutations.
    return actual == expected and canonical_sha256(actual) == canonical_sha256(expected)


def preflight_run_identity(run, expected):
    """Return (existing identity, completed) before any persistent mutation."""
    run = Path(run)
    path = run / 'run-identity.json'
    checkpoint_path = run / 'checkpoint.json'
    result_path = run / 'result.json'
    if path.exists():
        actual = read_json(path)
        if (type(actual) is not dict or set(actual) != {'version', 'inputs', 'identity_sha256'} or
                actual['version'] != RUN_IDENTITY_VERSION or
                type(actual['inputs']) is not dict or
                actual['identity_sha256'] != canonical_sha256(actual['inputs'])):
            raise ValueError('invalid or unknown immutable run identity')
        if not _same(actual, expected):
            changed = [key for key in expected['inputs']
                       if not _same(actual['inputs'].get(key), expected['inputs'][key])]
            raise ValueError('run identity mismatch: ' + ', '.join(changed))
    else:
        historical = any(p.is_file() or p.is_symlink() for p in run.rglob('*'))
        if historical:
            raise ValueError('historical partial/completed run lacks immutable source/freeze identity; '
                'automatic continuation refused. Independently verify its checkpoint, public inputs, '
                'responses and durable ledgers with the read-only strict audit entry; do not relabel it.')
        actual = None
    exogenous = run / 'exogenous.json'
    expected_exogenous = {'missions': expected['inputs']['simulation']['input_missions'],
                          'rules': expected['inputs']['simulation']['rules_input']}
    if exogenous.exists() and not _same(read_json(exogenous), expected_exogenous):
        raise ValueError('immutable exogenous tasks/rules differ')
    if checkpoint_path.exists():
        restored = WeeklySimulation.from_checkpoint(read_json(checkpoint_path))
        if not _same(immutable_simulation_inputs(restored), expected['inputs']['simulation']):
            raise ValueError('checkpoint immutable run identity mismatch (scenario/config/seed/policy/model/renderer/rules)')
        if not exogenous.exists():
            raise ValueError('immutable exogenous tasks/rules are missing')
        if result_path.exists():
            row = read_json(result_path)
            if restored.now != 720 or any(row.get(key) != value for key, value in (
                    ('scenario', expected['inputs']['requested_scenario']),
                    ('seed', expected['inputs']['requested_seed']),
                    ('method', expected['inputs']['requested_method']))):
                raise ValueError('completed result immutable run identity mismatch')
    elif result_path.exists():
        raise ValueError('completed result has no checkpoint')
    return actual, result_path.exists()
