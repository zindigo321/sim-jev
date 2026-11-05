"""Phase 2E-A immutable fixed-state inputs, sealed selection and isolated reference.

No command starts a model service. Live selection requires an injected, already
budgeted LocalProvider. Audit and replay use the saved public input and cache.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sim_jev.branch_audit_store import BranchAuditStore, branch_key
from sim_jev.candidates import generate_candidates
from sim_jev.checkpoint import _loads
from sim_jev.decision import DecisionRequest, DecisionResult, ModelParameters
from sim_jev.decision_audit import validate_record as validate_decision, historical_observation
from sim_jev.decision_resolver import DecisionRuntime, select_from_pool, logical_decision_record
from sim_jev.local_provider import LocalProvider, validate_record as validate_local, render_for_identity
from sim_jev.planning import PlanningInput, PlanningScenario
from sim_jev.provenance import canonical_sha256, code_fingerprint
from sim_jev.weekly import WeeklySimulation
from sim_jev.weekly_policy import PolicyObservation, PolicySpec, RuleContext, RuleSpec, choose_command
from scripts.phase2d_local import METHODS, save, unpack
from scripts.phase2d_r1 import build_case, immutable, policy
from scripts.phase2d_r1_data_audit import _scalar, _account
from scripts.planning_verify import audit_planning_branch
from scripts.weekly_verify import audit_business_log
from scripts.phase2c_experiment import write_csv

VERSION = 'phase2e-a-fixed-state-diagnostic-v1'
ANALYSIS_SEED = 2026110401
DES_LIMITS = {'online': 5120, 'reference': 40960}
SCREENED = ('random-des', 'heuristic-des', 'local-des')


def read(path):
    return _loads(Path(path).read_text(encoding='utf-8'))


def file_sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def request_pool_identity(request, pool):
    """Identity hashes exclude only documented top-level protocol/ID wrappers.

    Business mission IDs, array order, numeric values and candidate summaries
    remain. Candidate IDs and schema/version labels are transport wrappers.
    """
    exact = canonical_sha256({'request': request, 'pool': pool})
    business_request = {key: deepcopy(request[key]) for key in ('observation', 'context', 'parameters')}
    business_pool = []
    for candidate in pool:
        summary = {key: deepcopy(value) for key, value in candidate['summary'].items()
                   if key not in ('schema_version', 'generator_version')}
        # heuristic.version is a named estimator contract, not a sample ID.
        business_pool.append({'command': deepcopy(candidate['command']), 'summary': summary})
    business = canonical_sha256({'request': business_request, 'pool': business_pool})
    return exact, business


def collect_seen_public(evidence):
    """All saved R1 public inputs, including pilot decisions and old captures."""
    evidence = Path(evidence)
    rows = []
    for relative in ('development/states.json', 'diagnostics/states.json',
                     'diagnostics/seen-public-states.json'):
        path = evidence / relative
        if path.exists():
            for index, row in enumerate(read(path)):
                if 'request' in row and 'pool' in row:
                    rows.append({'origin': relative + '#' + str(index),
                                 'request': row['request'], 'pool': row['pool']})
    for path in sorted((evidence / 'pilot').glob('*/checkpoint.json')):
        value = read(path)
        records = value['weekly_state'].get('decision_records', [])
        for index, row in enumerate(records):
            rows.append({'origin': path.relative_to(evidence).as_posix() + '#' + str(index),
                         'request': row['request'], 'pool': row['pool']})
        if not records:
            # Mandatory-first stores commands and physical logs rather than a
            # candidate-resolver ledger. Rebuild its public offer barriers from
            # those existing facts; never advance execution or evaluate DES.
            sim = WeeklySimulation.from_checkpoint(value)
            for point in value['weekly_state']['processed_week_points']:
                request = request_from_historical_facts(sim, point)
                pool = sorted(generate_candidates(request, 16, 20261102), key=lambda candidate: candidate.id)
                rows.append({'origin': path.relative_to(evidence).as_posix() + '#historical-public-' + str(int(point)),
                             'request': request.to_dict(), 'pool': [candidate.to_dict() for candidate in pool]})
    for folder in ('legacy-v1', 'development'):
        for path in sorted((evidence / folder).rglob('completion-*.json')):
            canonical = read(path)['rendered']['canonical']
            rows.append({'origin': path.relative_to(evidence).as_posix(),
                         'request': canonical['decision'], 'pool': canonical['candidates']})
    if not rows:
        raise ValueError('R1 public input evidence is empty')
    for row in rows:
        request, pool = unpack(row)
        row['request'] = request.to_dict()
        row['pool'] = [candidate.to_dict() for candidate in pool]
    return rows


def request_from_historical_facts(sim, point):
    observation, carry = historical_observation(sim, point)
    rule = next(RuleSpec.from_dict(row) for row in sim.weekly_state['rules_input'] if row['window_start'] == point)
    context = RuleContext(point, min(point + 168., 720.), sim.config.fleet_size, rule, tuple(carry))
    return DecisionRequest.create(PolicyObservation.from_dict(observation), context,
        ModelParameters.from_config(sim.config), protocol_version=sim.decision_protocol_version)


def seed_usage(run_roots):
    """Inspect saved environment metadata; never open a new environment."""
    usage = []
    filenames = {'checkpoint.json', 'runtime.json', 'result.json', 'states.json',
                 'registration.json', 'freeze.json', 'manifest.json', 'online-actions.json'}
    for run_root in run_roots:
        base = Path(run_root)
        for current, directories, names in os.walk(base):
            directories[:] = [name for name in directories if name not in (
                'branch-audit-store', 'responses', 'evaluation', '__pycache__', '.git')]
            for name in names:
                if name not in filenames:
                    continue
                path = Path(current) / name
                try:
                    value = read(path)
                except (ValueError, OSError):
                    continue
                def visit(item, scenario=None):
                    if isinstance(item, dict):
                        own = item.get('scenario', item.get('scenario_name', item.get('scenario_id', scenario)))
                        scene = next((s for s in ('balanced', 'scarce') if isinstance(own, str) and s in own), scenario)
                        for key in ('seed', 'environment_seed'):
                            if type(item.get(key)) is int:
                                usage.append({'scenario': scene, 'seed': item[key],
                                              'path': str(path), 'field': key})
                        for key, child in item.items():
                            if key not in ('random_state', 'rng', 'events', 'history', 'branches', 'scenarios',
                                           'initial_state', 'final_state', 'random_manifest'):
                                visit(child, scene)
                    elif isinstance(item, list):
                        for child in item:
                            visit(child, scenario)
                visit(value)
    return usage


def preregister(root, identity, seen_rows, prior_usage, execution_source=None):
    root = Path(root)
    source = execution_source or code_fingerprint(ROOT)
    planned, conflicts = [], []
    for scenario, begin, replacement_begin in (('balanced', 1000, 3000), ('scarce', 2000, 4000)):
        # Unknown-scenario historical seeds are treated conservatively as used.
        used = {row['seed'] for row in prior_usage if row.get('scenario') in (None, scenario)}
        replacements = iter(number for number in range(replacement_begin, replacement_begin + 100000) if number not in used)
        chosen = set()
        for original in range(begin, begin + 20):
            seed = next(replacements) if original in used else original
            while seed in chosen:
                seed = next(replacements)
            chosen.add(seed)
            if seed != original:
                conflicts.append({'scenario': scenario, 'requested_seed': original, 'replacement_seed': seed,
                                  'prior_evidence': [row for row in prior_usage if row['seed'] == original and
                                                     row.get('scenario') in (None, scenario)]})
            planned.append({'order': len(planned), 'scenario': scenario, 'environment_seed': seed,
                            'requested_seed': original, 'window_start': 336, 'window_end': 504,
                            'state_id': f'{scenario}-{seed}-336'})
    seen = []
    for row in seen_rows:
        exact, business = request_pool_identity(row['request'], row['pool'])
        seen.append({**row, 'exact_input_sha256': exact, 'business_input_sha256': business})
    value = {'schema': VERSION, 'source': source, 'identity': identity,
             'identity_sha256': canonical_sha256(identity), 'planned_inputs': planned,
             'seed_conflicts': conflicts, 'seed_usage_sha256': canonical_sha256(prior_usage),
             'seen_public_inputs_sha256': canonical_sha256(seen), 'methods': list(METHODS),
             'candidate_generation': {'M': 16, 'K': 4, 'B': 8, 'planning_seed': 20261102,
                                      'direct_des': 'min(4,M)*8 total; stable-ID round robin'},
             'reference': {'seed': 20261104, 'indices': list(range(64)),
                           'score': 'current offer cohort DSR+RCR; existing cross-window settlement'},
             'formal_des_limits': DES_LIMITS,
             'analysis': {'bootstrap_repetitions': 5000, 'seed': ANALYSIS_SEED,
                          'environment_interval': 'per-scene percentile 2.5/97.5 paired environment bootstrap',
                          'mc_interval': 'per-scene fixed-environment paired-CRN 64-scenario bootstrap; same indices for all candidates and methods within input',
                          'representative': 'first planned occurrence of exact business input across both scenes; exclude cross-seen inputs',
                          'primary': 'regret_heuristic-des minus regret_local-des = q_local-selected minus q_heuristic-selected',
                          'numeric_tolerance': 1e-12,
                          'continuation': {'each_scene_min_n': 10, 'each_scene_min_mean': .005,
                                           'each_scene_interval_lower_strictly_above': 0}},
             'business_projection': 'request observation/context/parameters + pool commands/summaries; omit candidate IDs and summary schema_version/generator_version; preserve business IDs/order/types/no rounding',
             'cache': 'one shared physical score per exact input; first occurrence live; subsequent exact duplicates explicitly declared replay before scoring',
             'timing': 'trajectory build, candidate build, generation, cache, DES, independent audit and serialization separate; shared score counted physically once',
             'scope': 'fixed state equal DES; no common deadline/anytime; no pilot/PPO/value training/official Jev acceptance'}
    immutable(root / 'diagnostic-registration.json', value)
    immutable(root / 'diagnostic-registration-sha256.json', {'sha256': canonical_sha256(value)})
    immutable(root / 'diagnostic-seed-usage.json', prior_usage)
    immutable(root / 'diagnostic-seen-public-inputs.json', seen)
    return {'status': 'preregistered', 'planned_inputs': len(planned), 'seed_conflicts': len(conflicts),
            'registration_sha256': canonical_sha256(value), 'new_states_built': 0}


def check_registration(root, *, allow_source_difference=False):
    root = Path(root)
    registration = read(root / 'diagnostic-registration.json')
    if read(root / 'diagnostic-registration-sha256.json') != {'sha256': canonical_sha256(registration)}:
        raise ValueError('diagnostic registration changed')
    if not allow_source_difference and registration['source'] != code_fingerprint(ROOT):
        raise ValueError('frozen scientific source changed; new batch required')
    if canonical_sha256(read(root / 'diagnostic-seen-public-inputs.json')) != registration['seen_public_inputs_sha256']:
        raise ValueError('seen public input evidence changed')
    if canonical_sha256(read(root / 'diagnostic-seed-usage.json')) != registration['seed_usage_sha256']:
        raise ValueError('seed usage evidence changed')
    return registration


def require_g1(root, registration):
    gate = read(Path(root) / 'development/status.json')
    if not (gate.get('status') == 'passed' and gate.get('valid_completions') == 20 and
            gate.get('unique_states') == 8 and gate.get('pending_uncertain') == 0 and
            gate.get('source_sha256') == registration['source']['sha256'] and
            gate.get('identity_sha256') == registration['identity_sha256']):
        raise ValueError('new renderer real development G1 gate incomplete')
    return gate


def deduplicate(rows, seen):
    seen_exact = {row['exact_input_sha256'] for row in seen}
    seen_business = {row['business_input_sha256'] for row in seen}
    exact_first, business_first = {}, {}
    result = []
    for row in rows:
        row = deepcopy(row)
        exact, business = request_pool_identity(row['request'], row['pool'])
        row.update(exact_input_sha256=exact, business_input_sha256=business,
                   cross_seen_exact_duplicate=exact in seen_exact,
                   cross_seen_business_duplicate=business in seen_business,
                   within_batch_exact_duplicate=exact in exact_first,
                   within_batch_business_duplicate=business in business_first,
                   exact_representative_state_id=exact_first.get(exact, row['state_id']),
                   business_representative_state_id=business_first.get(business, row['state_id']))
        row['primary_eligible'] = not (row['cross_seen_exact_duplicate'] or row['cross_seen_business_duplicate'] or
                                           row['within_batch_business_duplicate'])
        exact_first.setdefault(exact, row['state_id'])
        business_first.setdefault(business, row['state_id'])
        result.append(row)
    return result


def capture(root):
    root = Path(root)
    registration = check_registration(root)
    require_g1(root, registration)
    folder = root / 'diagnostics'
    folder.mkdir(exist_ok=False)
    rows, costs = [], []
    for planned in registration['planned_inputs']:
        captured = []
        def intercept(request, spec, runtime=None):
            if request.context.window_start == 336:
                captured.append(request)
            return DecisionResult(choose_command(request.observation, request.context, spec))
        started = time.perf_counter()
        with patch('sim_jev.weekly.resolve_decision', side_effect=intercept):
            sim = build_case(planned['scenario'], planned['environment_seed'], PolicySpec())
            sim.run(336)
        trajectory_seconds = time.perf_counter() - started
        if len(captured) != 1:
            raise ValueError('expected exactly one public legal request at hour 336')
        request = captured[0]
        started = time.perf_counter()
        pool = sorted(generate_candidates(request, 16, 20261102), key=lambda candidate: candidate.id)
        candidate_seconds = time.perf_counter() - started
        started = time.perf_counter()
        audit_business_log(sim)
        audit_seconds = time.perf_counter() - started
        directory = folder / planned['state_id']
        directory.mkdir()
        started = time.perf_counter()
        save(directory / 'mandatory-trajectory-checkpoint.json', sim.to_checkpoint())
        serialization_seconds = time.perf_counter() - started
        rows.append({**planned, 'seed': planned['environment_seed'], 'request': request.to_dict(),
                     'pool': [candidate.to_dict() for candidate in pool], 'actual_M': len(pool),
                     'reference_command': choose_command(request.observation, request.context, PolicySpec()).to_dict()})
        costs.append({'state_id': planned['state_id'], 'execution_simulated_hours': 336,
                      'candidate_des': 0, 'trajectory_build_seconds': trajectory_seconds,
                      'candidate_build_seconds': candidate_seconds, 'trajectory_audit_seconds': audit_seconds,
                      'serialization_seconds': serialization_seconds})
        save(folder / 'construction-cost.json', costs)
    rows = deduplicate(rows, read(root / 'diagnostic-seen-public-inputs.json'))
    immutable(folder / 'states.json', rows)
    dedup = [{key: value for key, value in row.items() if key not in ('request', 'pool', 'reference_command')} for row in rows]
    immutable(folder / 'deduplication.json', dedup)
    write_csv(folder / 'deduplication.csv', dedup)
    immutable(folder / 'cache-replay-plan.json', [{'state_id': row['state_id'],
        'exact_input_sha256': row['exact_input_sha256'],
        'score_mode': 'replay' if row['within_batch_exact_duplicate'] else 'live' if row['actual_M'] > 1 else 'deterministic_skip',
        'source_state_id': row['exact_representative_state_id']} for row in rows])
    return {'status': 'inputs_frozen', 'planned_inputs': 40, 'actual_inputs': len(rows),
            'unseen_business_unique': sum(row['primary_eligible'] for row in rows),
            'construction_simulated_hours': 336 * len(rows), 'construction_candidate_des': 0}


class DESJournal:
    """Append-only global pre-computation reservations; uncertain work blocks."""
    def __init__(self, path, registration_sha256):
        self.path = Path(path)
        self.binding = registration_sha256
        self.entries = {}
        if self.path.exists():
            for line in self.path.read_text(encoding='utf-8').splitlines():
                event = _loads(line)
                if event['registration_sha256'] != self.binding:
                    raise ValueError('formal DES campaign identity differs')
                key = event['key']
                if event['status'] == 'pending':
                    if key in self.entries:
                        raise ValueError('duplicate formal DES reservation')
                    self.entries[key] = event
                elif event['status'] == 'completed':
                    if key not in self.entries or self.entries[key]['status'] != 'pending':
                        raise ValueError('invalid formal DES completion')
                    self.entries[key] = event
                else:
                    raise ValueError('unknown formal DES state')
        self.pending = {key for key, row in self.entries.items() if row['status'] != 'completed'}
        self.counts = {phase: sum(row['phase'] == phase for row in self.entries.values()) for phase in DES_LIMITS}
        for phase, limit in DES_LIMITS.items():
            if self.counts[phase] > limit:
                raise ValueError('formal DES hard limit exceeded')

    def append(self, event):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n')
            stream.flush()
            os.fsync(stream.fileno())

    def reserve(self, phase, namespace, request, candidate, scenario):
        if phase not in DES_LIMITS:
            raise ValueError('unknown formal DES phase')
        if self.pending:
            raise ValueError('pending/uncertain formal DES attempt prevents automatic retry')
        key = namespace + '/' + branch_key(request, candidate.id, scenario.scenario_id)
        if key in self.entries:
            raise ValueError('formal DES branch already reserved')
        if self.counts[phase] >= DES_LIMITS[phase]:
            raise ValueError('formal DES hard limit exhausted')
        event = {'registration_sha256': self.binding, 'key': key, 'phase': phase,
                 'namespace': namespace, 'branch_key': branch_key(request, candidate.id, scenario.scenario_id),
                 'status': 'pending', 'reserved_unix': time.time()}
        self.append(event)
        self.entries[key] = event
        self.pending.add(key)
        self.counts[phase] += 1
        return key

    def finish(self, key, audit_identity):
        if self.entries[key]['status'] != 'pending':
            raise ValueError('invalid formal DES transition')
        event = {**self.entries[key], 'status': 'completed', 'audit_identity': audit_identity}
        self.append(event)
        self.entries[key] = event
        self.pending.remove(key)

    def summary(self):
        return {'actual_compute_attempts': len(self.entries),
                'pending_uncertain': len(self.pending),
                **{phase + '_attempts': self.counts[phase] for phase in DES_LIMITS}}


class BudgetedBranchStore:
    def __init__(self, directory, journal, phase, namespace):
        self.store = BranchAuditStore(directory, audit_planning_branch)
        self.journal, self.phase, self.namespace = journal, phase, namespace
        self.active = None
        self.timings = {'branches': 0, 'des_compute_seconds': 0., 'independent_branch_audit_seconds': 0.,
                        'reservation_hashing_serialization_persistence_seconds': 0.,
                        'result_hashing_serialization_persistence_seconds': 0.,
                        'completion_journal_serialization_persistence_seconds': 0.,
                        'audit_receipt_read_seconds': 0.}

    def before_branch(self, request, candidate, index, scenario):
        started = time.perf_counter()
        self.active = self.journal.reserve(self.phase, self.namespace, request, candidate, scenario)
        self.store.before_branch(request, candidate, index, scenario)
        self.timings['reservation_hashing_serialization_persistence_seconds'] += time.perf_counter() - started

    def __call__(self, result, request):
        started = time.perf_counter()
        checked = self.store(result, request)
        store_seconds = time.perf_counter() - started
        key = self.active
        started = time.perf_counter()
        audit = read(self.store.root / 'audits' / (key.rsplit('/', 1)[-1] + '.json'))
        self.timings['audit_receipt_read_seconds'] += time.perf_counter() - started
        self.timings['des_compute_seconds'] += audit['compute_seconds']
        self.timings['independent_branch_audit_seconds'] += audit['audit_seconds']
        self.timings['result_hashing_serialization_persistence_seconds'] += max(0., store_seconds - audit['audit_seconds'])
        started = time.perf_counter()
        self.journal.finish(key, audit['audit_identity'])
        self.timings['completion_journal_serialization_persistence_seconds'] += time.perf_counter() - started
        self.timings['branches'] += 1
        self.active = None
        return checked


def _action(row, method, record, shared):
    return {'state_id': row['state_id'], 'scenario': row['scenario'], 'seed': row['seed'],
            'method': method, 'status': 'completed', 'selected_candidate_id': record['selected_candidate_id'],
            'retained_ids': record['ranking_ids'][:min(4, row['actual_M'])] if method in SCREENED and row['actual_M'] > 1 else None,
            'skip_reason': record.get('deterministic_reason'), 'actual_des': record['budget']['actual_des'],
            'simulated_hours': record['budget']['simulated_hours'],
            'shared_score_key': shared['cache_key'] if shared and method.startswith('local-') else None,
            'shared_physical_generation_seconds': shared['generation_seconds'] if shared and method.startswith('local-') else None,
            'score_cost_allocation': 'shared once per exact input; local method standalone latency is not inferred' if method.startswith('local-') else None,
            'method_end_to_end_seconds': (record.get('execution_audit') or {}).get('end_to_end_seconds')}


def online(root, live_provider):
    root = Path(root)
    registration = check_registration(root)
    require_g1(root, registration)
    folder = root / 'diagnostics'
    if (folder / 'online-started.json').exists():
        raise ValueError('online batch already started; preserve partial and do not retry automatically')
    if live_provider.identity != registration['identity'] or live_provider.mode != 'live':
        raise ValueError('diagnostic live provider identity/mode differs')
    if Path(live_provider.store).resolve() != (folder / 'responses').resolve():
        raise ValueError('shared diagnostic provider must use diagnostics/responses')
    immutable(folder / 'online-started.json', {'registration_sha256': canonical_sha256(registration), 'started_unix': time.time()})
    journal = DESJournal(root / 'formal-des-journal.jsonl', canonical_sha256(registration))
    actions, accounting_rows = [], []
    replay_plan = {row['state_id']: row for row in read(folder / 'cache-replay-plan.json')}
    try:
        for row in read(folder / 'states.json'):
            request, pool = unpack(row)
            directory = folder / row['state_id']
            plan = replay_plan[row['state_id']]
            shared = live_provider.rank(request, pool)
            validate_local(shared, request, pool, registration['identity'])
            expected = 'local_replay' if plan['score_mode'] == 'replay' and len(pool) > 1 else 'local_inference' if len(pool) > 1 else 'deterministic'
            if shared['evidence'] != expected:
                raise ValueError('shared score mode differs from preregistered exact-cache replay plan')
            immutable(directory / 'shared-ranking.json', shared)
            for method in METHODS:
                store = BudgetedBranchStore(directory / method, journal, 'online', row['state_id'] + '/' + method)
                if method == 'mandatory-first':
                    selected = next((candidate.id for candidate in pool if candidate.command.to_dict() == row['reference_command']), None)
                    record = {'method': method, 'command': row['reference_command'], 'selected_candidate_id': selected,
                              'ranking_ids': [], 'budget': {'actual_des': 0, 'simulated_hours': 0.}, 'branches': [],
                              'deterministic_reason': 'rule_only_no_prefilter'}
                else:
                    cached = LocalProvider(registration['identity'], folder / 'responses', mode='cache_only') if method.startswith('local-') else None
                    record = select_from_pool(request, policy(method, registration['identity']), pool,
                        DecisionRuntime(branch_auditor=store, local_provider=cached)).record
                serialize_started = time.perf_counter()
                immutable(directory / (method + '.json'), record)
                record_serialization_seconds = time.perf_counter() - serialize_started
                reconcile_started = time.perf_counter()
                accounting = store.store.reconcile({'weekly_state': {'decision_records': [] if method == 'mandatory-first' else [record]}})
                reconciliation_seconds = time.perf_counter() - reconcile_started
                serialize_started = time.perf_counter()
                immutable(directory / method / 'branch-accounting.json', accounting)
                accounting_serialization_seconds = time.perf_counter() - serialize_started
                immutable(directory / method / 'execution-cost.json', {**store.timings,
                    'record_hashing_serialization_persistence_seconds': record_serialization_seconds,
                    'accounting_hashing_serialization_persistence_seconds': accounting_serialization_seconds,
                    'independent_ledger_reconciliation_seconds': reconciliation_seconds,
                    'timing_scope': 'persistence includes JSON encoding, content hashing and file I/O; independent branch audit subtracted; audit receipt reads recorded separately',
                    'method_selection_end_to_end_seconds': (record.get('execution_audit') or {}).get('end_to_end_seconds')})
                accounting_rows.append({'state_id': row['state_id'], 'method': method, **accounting})
                actions.append(_action(row, method, record, shared))
                save(folder / 'online-actions.json', actions)
                save(folder / 'online-accounting.json', accounting_rows)
            print('phase2e online', row['state_id'], 'actions', len(actions), flush=True)
        result = verify_online(root, require_seal=False)
        manifest = online_manifest(root)
        immutable(folder / 'online-seal.json', {'registration_sha256': canonical_sha256(registration),
            'actions_sha256': canonical_sha256(actions), 'files': manifest,
            'files_sha256': canonical_sha256(manifest), 'independent_pre_reference_verification': result})
        immutable(folder / 'online-status.json', {'status': 'passed', **result})
        return result
    except Exception as error:
        save(folder / 'online-status.json', {'status': 'partial', 'error': type(error).__name__ + ': ' + str(error),
             'actions_completed': len(actions), 'reference_opened': False, 'budget': journal.summary()})
        raise


def online_manifest(root):
    folder = Path(root) / 'diagnostics'
    result = []
    for path in sorted(folder.rglob('*')):
        if path.is_file() and 'evaluation' not in path.relative_to(folder).parts and path.name not in ('online-seal.json', 'online-status.json'):
            result.append({'path': path.relative_to(folder).as_posix(), 'size': path.stat().st_size, 'sha256': file_sha(path)})
    return result


def verify_online(root, *, require_seal=True):
    """Read-only independently validate all online choices, hashes and ledgers."""
    root = Path(root)
    registration = check_registration(root)
    require_g1(root, registration)
    folder = root / 'diagnostics'
    states = read(folder / 'states.json')
    planned = registration['planned_inputs']
    if [(row['scenario'], row['seed'], row['window_start']) for row in states] != [
            (row['scenario'], row['environment_seed'], 336) for row in planned]:
        raise ValueError('all 40 preregistered states required in frozen order')
    recomputed_dedup = deduplicate(states, read(root / 'diagnostic-seen-public-inputs.json'))
    if states != recomputed_dedup:
        raise ValueError('saved business/exact deduplication differs')
    actions = read(folder / 'online-actions.json')
    if [(row['state_id'], row['method']) for row in actions] != [(state['state_id'], method) for state in states for method in METHODS]:
        raise ValueError('all six methods for all 40 inputs required before reference')
    ledger_rows = []
    budget = DESJournal(root / 'formal-des-journal.jsonl', canonical_sha256(registration))
    physical_scores = replay_scores = 0
    shared_records = []
    for state in states:
        request, pool = unpack(state)
        if [candidate.to_dict() for candidate in sorted(generate_candidates(request, 16, 20261102), key=lambda candidate: candidate.id)] != state['pool']:
            raise ValueError('candidate pool differs from frozen generation')
        directory = folder / state['state_id']
        sim = WeeklySimulation.from_checkpoint(read(directory / 'mandatory-trajectory-checkpoint.json'))
        if sim.config.seed != state['seed'] or sim.now != 336:
            raise ValueError('construction trajectory seed/time differs')
        audit_business_log(sim)
        shared = read(directory / 'shared-ranking.json')
        validate_local(shared, request, pool, registration['identity'])
        shared_records.append(shared)
        expected = 'local_replay' if state['within_batch_exact_duplicate'] and len(pool) > 1 else 'local_inference' if len(pool) > 1 else 'deterministic'
        if shared['evidence'] != expected:
            raise ValueError('shared scoring source differs from preregistered plan')
        physical_scores += shared['evidence'] == 'local_inference'
        replay_scores += shared['evidence'] == 'local_replay'
        for method in METHODS:
            record = read(directory / (method + '.json'))
            if method == 'mandatory-first':
                chosen = next((candidate.id for candidate in pool if candidate.command.to_dict() == state['reference_command']), None)
                if record['command'] != state['reference_command'] or record['selected_candidate_id'] != chosen or record['branches']:
                    raise ValueError('mandatory-first action differs')
            else:
                validate_decision(record, policy(method, registration['identity']), sim)
                if record['request'] != state['request'] or record['pool'] != state['pool']:
                    raise ValueError('selector did not use the shared legal request/pool')
                by_id = {candidate.id: candidate for candidate in pool}
                for branch in record['branches']:
                    _scalar(branch, request, by_id[branch['candidate_id']], 20261102)
                if method.startswith('local-'):
                    for key in ('identity', 'cache_key', 'preference_scores', 'ranked_ids', 'rendered'):
                        if record['local'][key] != shared[key]:
                            raise ValueError('two local selectors did not share the exact physical score')
            actual_action = next(row for row in actions if row['state_id'] == state['state_id'] and row['method'] == method)
            if actual_action != _action(state, method, record, shared):
                raise ValueError('online action summary differs from raw record')
            accounting = _account(directory / method, [] if method == 'mandatory-first' else [record])
            execution_cost = read(directory / method / 'execution-cost.json')
            if execution_cost['branches'] != accounting['logical_branches'] or any(
                    type(value) not in (int, float) or not math.isfinite(value) or value < 0
                    for key, value in execution_cost.items() if key.endswith('_seconds') and value is not None):
                raise ValueError('invalid independently recorded execution cost')
            for key, source_key in (('des_compute_seconds', 'compute_seconds'), ('independent_branch_audit_seconds', 'audit_seconds')):
                if not math.isclose(execution_cost[key], accounting[source_key], rel_tol=1e-12, abs_tol=1e-12):
                    raise ValueError('execution timing differs from persistent branch receipts')
            ledger_rows.append({'state_id': state['state_id'], 'method': method, **accounting})
            audits = BranchAuditStore(directory / method, audit_planning_branch, read_only=True)._audits()
            for key, audit in audits.items():
                global_key = state['state_id'] + '/' + method + '/' + key
                entry = budget.entries.get(global_key)
                if entry is None or entry['status'] != 'completed' or entry['audit_identity'] != audit['audit_identity']:
                    raise ValueError('global reservation differs from persistent branch audit')
    if ledger_rows != read(folder / 'online-accounting.json'):
        raise ValueError('online three-way accounting export differs')
    count = sum(row['logical_branches'] for row in ledger_rows)
    summary = budget.summary()
    if summary['pending_uncertain'] or summary['online_attempts'] != count or count > DES_LIMITS['online']:
        raise ValueError('online global DES budget reconciliation failed')
    # Raw local attempts, persistent quotas, HTTP bodies and GPU receipts must
    # independently bind these exact shared records before reference is opened.
    from scripts.phase2e_live import audit_diagnostic_generations
    generation_check = audit_diagnostic_generations(root, shared_records)
    if require_seal:
        seal = read(folder / 'online-seal.json')
        manifest = online_manifest(root)
        if seal['registration_sha256'] != canonical_sha256(registration) or seal['actions_sha256'] != canonical_sha256(actions) or seal['files'] != manifest or seal['files_sha256'] != canonical_sha256(manifest):
            raise ValueError('sealed online artifacts changed; reference is locked')
    return {'status': 'passed', 'states': len(states), 'actions': len(actions), 'online_des': count,
            'actual_compute_attempts': count, 'persistent_audit_identities': count,
            'physical_shared_scores': physical_scores, 'declared_exact_cache_replays': replay_scores,
            'pending_uncertain': 0, 'reference_labels_read': False, 'extra_candidate_des': 0,
            'generation_http_gpu_verification': generation_check}


def reference(root):
    root = Path(root)
    registration = check_registration(root)
    # Every online method and state is independently closed before ANY new label.
    precheck = verify_online(root)
    folder = root / 'diagnostics'
    evaluation = folder / 'evaluation'
    evaluation.mkdir(exist_ok=False)
    immutable(evaluation / 'pre-reference-verification.json', precheck)
    journal = DESJournal(root / 'formal-des-journal.jsonl', canonical_sha256(registration))
    actions = read(folder / 'online-actions.json')
    completed = []
    try:
        for state in read(folder / 'states.json'):
            request, pool = unpack(state)
            directory = evaluation / state['state_id']
            directory.mkdir()
            store = BudgetedBranchStore(directory, journal, 'reference', state['state_id'] + '/reference')
            all_branches = []
            scalar_serialization_seconds = physical_serialization_seconds = 0.
            retained_ids = {min(candidate.id for candidate in pool)} if pool else set()
            retained_ids |= {row['selected_candidate_id'] for row in actions if row['state_id'] == state['state_id'] and row['method'].endswith('-des') and row['selected_candidate_id'] is not None}
            with (directory / 'branches.jsonl').open('x', encoding='utf-8') as stream:
                for index in range(64):
                    scenario = PlanningScenario(PlanningInput(request, 20261104, index))
                    for candidate in sorted(pool, key=lambda candidate: candidate.id):
                        store.before_branch(request, candidate, index, scenario)
                        result = scenario.evaluate(candidate.command)
                        checked = store(result, request)
                        scalar = {key: deepcopy(value) for key, value in result.items() if key != 'final_state'}
                        scalar.update(candidate_id=candidate.id, physical_artifact_sha256=canonical_sha256(result['final_state']))
                        _scalar(scalar, request, candidate, 20261104)
                        all_branches.append(scalar)
                        serialize_started = time.perf_counter()
                        stream.write(json.dumps({**scalar, 'audit': checked}, ensure_ascii=False, separators=(',', ':'), allow_nan=False) + '\n')
                        scalar_serialization_seconds += time.perf_counter() - serialize_started
                        if index == 0 and candidate.id in retained_ids:
                            serialize_started = time.perf_counter()
                            immutable(directory / ('physical-' + candidate.id + '-0.json'), result)
                            physical_serialization_seconds += time.perf_counter() - serialize_started
            reconcile_started = time.perf_counter()
            accounting = store.store.reconcile({'weekly_state': {'decision_records': [{'request': request.to_dict(), 'branches': all_branches}]}})
            reconciliation_seconds = time.perf_counter() - reconcile_started
            serialize_started = time.perf_counter()
            immutable(directory / 'branch-accounting.json', accounting)
            accounting_serialization_seconds = time.perf_counter() - serialize_started
            immutable(directory / 'execution-cost.json', {**store.timings,
                'scalar_jsonl_serialization_write_seconds': scalar_serialization_seconds,
                'retained_physical_serialization_persistence_seconds': physical_serialization_seconds,
                'accounting_hashing_serialization_persistence_seconds': accounting_serialization_seconds,
                'independent_ledger_reconciliation_seconds': reconciliation_seconds,
                'timing_scope': 'persistence includes JSON encoding, content hashing and file I/O; independent branch audit subtracted; scalar stream close/OS flush overhead not separately measured'})
            completed.append({'state_id': state['state_id'], 'actual_M': len(pool), 'reference_des': len(all_branches),
                              'required_retained_ids': sorted(retained_ids), **accounting})
            save(evaluation / 'reference-accounting.json', completed)
            print('phase2e reference', state['state_id'], 'DES', sum(row['reference_des'] for row in completed), flush=True)
        verified = verify_reference(root)
        immutable(evaluation / 'status.json', {'status': 'passed', **verified})
        return verified
    except Exception as error:
        save(evaluation / 'status.json', {'status': 'partial', 'error': type(error).__name__ + ': ' + str(error),
                                         'completed_states': len(completed), 'budget': journal.summary()})
        raise


def verify_reference(root):
    root = Path(root)
    registration = check_registration(root)
    verify_online(root)
    folder = root / 'diagnostics'
    evaluation = folder / 'evaluation'
    journal = DESJournal(root / 'formal-des-journal.jsonl', canonical_sha256(registration))
    all_counts = []
    required_physical = 0
    for state in read(folder / 'states.json'):
        request, pool = unpack(state)
        candidates = {candidate.id: candidate for candidate in pool}
        directory = evaluation / state['state_id']
        rows = [_loads(line) for line in (directory / 'branches.jsonl').read_text(encoding='utf-8').splitlines()]
        if [(row['scenario_index'], row['candidate_id']) for row in rows] != [(index, key) for index in range(64) for key in sorted(candidates)]:
            raise ValueError('reference must cover every actual candidate and all 64 paired scenarios')
        for row in rows:
            _scalar(row, request, candidates[row['candidate_id']], 20261104)
        logical = [{key: value for key, value in row.items() if key != 'audit'} for row in rows]
        accounting = _account(directory, [{'request': request.to_dict(), 'branches': logical}])
        execution_cost = read(directory / 'execution-cost.json')
        if execution_cost['branches'] != len(rows) or any(
                type(value) not in (int, float) or not math.isfinite(value) or value < 0
                for key, value in execution_cost.items() if key.endswith('_seconds') and value is not None):
            raise ValueError('invalid reference execution timing')
        for key, source_key in (('des_compute_seconds', 'compute_seconds'), ('independent_branch_audit_seconds', 'audit_seconds')):
            if not math.isclose(execution_cost[key], accounting[source_key], rel_tol=1e-12, abs_tol=1e-12):
                raise ValueError('reference timing differs from persistent branch receipts')
        audits = BranchAuditStore(directory, audit_planning_branch, read_only=True)._audits()
        for key, audit in audits.items():
            entry = journal.entries.get(state['state_id'] + '/reference/' + key)
            if entry is None or entry['status'] != 'completed' or entry['audit_identity'] != audit['audit_identity']:
                raise ValueError('reference global DES reservation/audit differs')
        expected_ids = {min(candidates)} if candidates else set()
        expected_ids |= {row['selected_candidate_id'] for row in read(folder / 'online-actions.json') if row['state_id'] == state['state_id'] and row['method'].endswith('-des') and row['selected_candidate_id'] is not None}
        for candidate_id in sorted(expected_ids):
            artifact = read(directory / ('physical-' + candidate_id + '-0.json'))
            audit_planning_branch(artifact, request)
            scalar = next(row for row in rows if row['candidate_id'] == candidate_id and row['scenario_index'] == 0)
            if canonical_sha256(artifact['final_state']) != scalar['physical_artifact_sha256']:
                raise ValueError('retained reference physical artifact differs from scalar')
            required_physical += 1
        all_counts.append({'state_id': state['state_id'], 'actual_M': len(pool), 'reference_des': len(rows),
                           'required_retained_ids': sorted(expected_ids), **accounting})
    if all_counts != read(evaluation / 'reference-accounting.json'):
        raise ValueError('reference accounting export differs')
    total = sum(row['logical_branches'] for row in all_counts)
    budget = journal.summary()
    if budget['pending_uncertain'] or budget['reference_attempts'] != total or total > DES_LIMITS['reference']:
        raise ValueError('reference DES hard-budget reconciliation failed')
    return {'states': len(all_counts), 'reference_des': total, 'actual_compute_attempts': total,
            'persistent_audit_identities': total, 'retained_physical_artifacts_verified': required_physical,
            'pending_uncertain': 0, 'new_model_generations': 0, 'extra_candidate_des': 0}


def replay(root, output):
    """Actual cache-only replay of all new actions and online branch scalars."""
    root, output = Path(root), Path(output)
    registration = check_registration(root)
    verify_online(root)
    output.mkdir(exist_ok=False)
    journal = DESJournal(output / 'audit-replay-des-journal.jsonl', canonical_sha256(registration))
    count = 0
    results = []
    for state in read(root / 'diagnostics/states.json'):
        request, pool = unpack(state)
        original_dir = root / 'diagnostics' / state['state_id']
        for method in METHODS:
            original = read(original_dir / (method + '.json'))
            if method == 'mandatory-first':
                chosen = choose_command(request.observation, request.context, PolicySpec()).to_dict()
                if chosen != original['command']:
                    raise ValueError('mandatory cache replay command differs')
                continue
            cache = LocalProvider(registration['identity'], root / 'diagnostics/responses', mode='cache_only') if method.startswith('local-') else None
            store = BudgetedBranchStore(output / state['state_id'] / method, journal, 'online', state['state_id'] + '/' + method)
            actual = select_from_pool(request, policy(method, registration['identity']), pool,
                                      DecisionRuntime(branch_auditor=store, local_provider=cache)).record
            if logical_decision_record(actual) != logical_decision_record(original):
                raise ValueError('cache replay exact prompt/cache/action/branch scalar differs: ' + state['state_id'] + '/' + method)
            accounting = store.store.reconcile({'weekly_state': {'decision_records': [actual]}})
            save(output / state['state_id'] / method / 'branch-accounting.json', accounting)
            save(output / state['state_id'] / (method + '.json'), actual)
            count += len(actual['branches'])
            results.append({'state_id': state['state_id'], 'method': method, 'passed': True,
                            'extra_audit_des': len(actual['branches']), 'new_generations': cache.generations if cache else 0,
                            'metadata_requests': cache.metadata_queries if cache else 0})
    result = {'status': 'passed', 'states': len(read(root / 'diagnostics/states.json')),
              'decisions': len(results) + len(read(root / 'diagnostics/states.json')),
              'extra_audit_des': count, 'new_generations': 0, 'metadata_requests': 0, 'results': results,
              'claim': 'offline exact cache and DES replay; not new real inference'}
    immutable(output / 'status.json', result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('preregister', 'capture', 'reference', 'audit-online', 'audit-reference', 'replay', 'analyze'))
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--seen-evidence', type=Path, help='fixed R1 public evidence, required for preregister')
    parser.add_argument('--history-root', type=Path, action='append', help='existing research run root to scan; repeatable, default project runs')
    args = parser.parse_args(argv)
    if args.action == 'preregister':
        if args.seen_evidence is None:
            parser.error('preregister requires --seen-evidence for all fixed R1 public inputs')
        frozen = read(args.root / 'freeze.json')
        if frozen['source'] != code_fingerprint(ROOT):
            raise ValueError('execution freeze source changed before preregistration')
        result = preregister(args.root, frozen['identity'], collect_seen_public(args.seen_evidence),
                             seed_usage(args.history_root or [ROOT / 'runs']), frozen['source'])
    elif args.action == 'capture':
        result = capture(args.root)
    elif args.action == 'reference':
        result = reference(args.root)
    elif args.action == 'replay':
        if args.output is None:
            parser.error('replay requires --output in a new directory')
        result = replay(args.root, args.output)
    elif args.action == 'analyze':
        from scripts.phase2e_analysis import analyze
        result = analyze(args.root)
    else:
        result = verify_online(args.root) if args.action == 'audit-online' else verify_reference(args.root)
        if args.output is not None:
            immutable(args.output, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
