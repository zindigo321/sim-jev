"""Frozen paired-environment and paired-CRN analysis for phase 2E-A.

The reference maximum is confined to the actual bounded candidate pool. The
primary paired contrast cancels that maximum: q(selected_local)-q(selected_H).
"""
from __future__ import annotations

import hashlib
import math
from pathlib import Path
import statistics

import numpy as np

from scripts.phase2e_diagnostic import (ANALYSIS_SEED, METHODS, SCREENED, check_registration,
    immutable, read, verify_reference)
from scripts.phase2c_experiment import write_csv
from sim_jev.checkpoint import _loads
from sim_jev.provenance import canonical_sha256


def paired_se(values):
    return statistics.stdev(values) / math.sqrt(len(values)) if len(values) > 1 else None


def decompose(scores, selected, retained):
    """No after-the-fact retained set is fabricated for unscreened methods."""
    if any(len(values) != 64 for values in scores.values()):
        raise ValueError('each reference candidate requires 64 paired scenarios')
    means = {key: math.fsum(values) / 64 for key, values in scores.items()}
    if selected is not None and selected not in scores:
        raise ValueError('selected candidate absent from actual reference pool')
    if retained is not None and (not retained or len(retained) != len(set(retained)) or not set(retained) <= set(scores) or selected not in retained):
        raise ValueError('retained set must be the actual valid preselection containing selected candidate')
    best_id = min(means, key=lambda key: (-means[key], key)) if means else None
    retained_id = min(retained, key=lambda key: (-means[key], key)) if retained else None
    best = means.get(best_id)
    regret = best - means[selected] if selected in means else None
    screening = best - means[retained_id] if retained_id else None
    choice_loss = means[retained_id] - means[selected] if retained_id else None
    if screening is not None and not math.isclose(regret, screening + choice_loss, rel_tol=1e-12, abs_tol=1e-12):
        raise ValueError('screening plus within-set selection loss does not equal final regret')
    def difference_se(first, second):
        return paired_se([a-b for a, b in zip(scores[first], scores[second])]) if first is not None and second is not None else None
    return {'best_finite_pool_candidate_id': best_id, 'best_reference_score': best,
            'selected_reference_score': means.get(selected), 'final_action_regret': regret,
            'screening_loss': screening, 'within_retained_selection_loss': choice_loss,
            'retained_set_best_id': retained_id, 'retained_set_size': len(retained) if retained else None,
            'regret_paired_mc_se': difference_se(best_id, selected),
            'screening_paired_mc_se': difference_se(best_id, retained_id),
            'selection_paired_mc_se': difference_se(retained_id, selected),
            'actual_M': len(scores), 'candidate_reference_range': max(means.values()) - min(means.values()) if means else None}


def paired_intervals(differences, *, scene_index, repetitions=5000, seed=ANALYSIS_SEED):
    """differences has one row per independent environment and 64 CRN columns."""
    values = np.asarray(differences, dtype=np.float64)
    if values.size == 0:
        return {'n': 0, 'mean_improvement': None, 'environment_paired_95_interval': None,
                'conditional_reference_mc_95_interval': None, 'paired_reference_mc_se': None}
    if values.ndim != 2 or values.shape[1] != 64 or not np.isfinite(values).all():
        raise ValueError('paired differences must be finite environment-by-64 matrix')
    n = values.shape[0]
    means = values.mean(axis=1)
    env_rng = np.random.default_rng(seed + scene_index * 2)
    env_indices = env_rng.integers(0, n, size=(repetitions, n))
    env_samples = means[env_indices].mean(axis=1)
    mc_rng = np.random.default_rng(seed + scene_index * 2 + 1)
    mc_samples = np.zeros(repetitions)
    digest = hashlib.sha256()
    for row in values:
        # This SAME index array applies to every candidate/method in the input.
        # Taking the paired score difference first is algebraically equivalent.
        indices = mc_rng.integers(0, 64, size=(repetitions, 64))
        digest.update(indices.astype('<i8', copy=False).tobytes())
        mc_samples += row[indices].mean(axis=1) / n
    return {'n': n, 'mean_improvement': float(means.mean()),
            'environment_paired_95_interval': np.quantile(env_samples, [.025, .975]).tolist(),
            'conditional_reference_mc_95_interval': np.quantile(mc_samples, [.025, .975]).tolist(),
            'paired_reference_mc_se': float(np.sqrt(np.sum(values.var(axis=1, ddof=1) / 64)) / n),
            'bootstrap_repetitions': repetitions, 'environment_rng_seed': seed + scene_index * 2,
            'mc_rng_seed': seed + scene_index * 2 + 1,
            'environment_resample_indices_sha256': hashlib.sha256(env_indices.astype('<i8', copy=False).tobytes()).hexdigest(),
            'paired_mc_resample_indices_sha256': digest.hexdigest(),
            'environment_interval_scope': 'environment resampling conditional on estimated reference means',
            'mc_interval_scope': '64-scenario resampling conditional on retained environments; all candidates/methods paired'}


def analyze(root):
    root = Path(root)
    registration = check_registration(root)
    verified = verify_reference(root)
    folder = root / 'diagnostics'
    evaluation = folder / 'evaluation'
    rows, candidate_rows, primary_rows, paired = [], [], [], []
    actions = read(folder / 'online-actions.json')
    states = read(folder / 'states.json')
    for state in states:
        raw = [_loads(line) for line in (evaluation / state['state_id'] / 'branches.jsonl').read_text(encoding='utf-8').splitlines()]
        scores = {candidate['id']: [] for candidate in state['pool']}
        for branch in raw:
            scores[branch['candidate_id']].append(branch['score'])
        means = {key: math.fsum(values) / 64 for key, values in scores.items()}
        best_id = min(means, key=lambda key: (-means[key], key)) if means else None
        for key in scores:
            differences = [a-b for a,b in zip(scores[best_id], scores[key])]
            mean_difference = math.fsum(differences) / 64
            se = paired_se(differences)
            candidate_rows.append({'state_id': state['state_id'], 'candidate_id': key, 'q_ref': means[key],
                'scores': scores[key], 'N': 64, 'reference_mc_se': paired_se(scores[key]),
                'paired_difference_from_finite_pool_max': differences, 'paired_difference_mean': mean_difference,
                'paired_difference_se': se, 'numeric_tie': abs(mean_difference) <= 1e-12,
                'mc_approximate_tie': mean_difference - 1.96 * se <= 0 <= mean_difference + 1.96 * se})
        state_actions = [action for action in actions if action['state_id'] == state['state_id']]
        for action in state_actions:
            selected = action['selected_candidate_id']
            retained = action['retained_ids']
            metric = decompose(scores, selected, retained)
            selected_branches = [branch for branch in raw if branch['candidate_id'] == selected]
            denominators = {}
            for key in ('offered', 'offered_value', 'accepted', 'accepted_value', 'success', 'earned_value',
                        'failed', 'accepted_not_executed', 'pending_execution', 'running'):
                values = [branch['counts'][key] for branch in selected_branches]
                denominators['reference_mean_' + key] = math.fsum(values) / len(values) if values else None
            rows.append({**{key: state[key] for key in ('state_id', 'scenario', 'seed', 'primary_eligible',
                         'exact_input_sha256', 'business_input_sha256')}, **action, **metric, **denominators,
                         'direct_local_selected_action': selected if action['method'] == 'local-direct' else None})
        local = next(action['selected_candidate_id'] for action in state_actions if action['method'] == 'local-des')
        heuristic = next(action['selected_candidate_id'] for action in state_actions if action['method'] == 'heuristic-des')
        differences = [a-b for a,b in zip(scores[local], scores[heuristic])] if local in scores and heuristic in scores else None
        primary_rows.append({'state_id': state['state_id'], 'scenario': state['scenario'], 'seed': state['seed'],
            'primary_eligible': state['primary_eligible'], 'business_input_sha256': state['business_input_sha256'],
            'local_selected_id': local, 'heuristic_selected_id': heuristic,
            'improvement': math.fsum(differences) / 64 if differences is not None else None,
            'paired_crn_differences': differences, 'paired_reference_mc_se': paired_se(differences) if differences is not None else None})
    for scene_index, scene in enumerate(('balanced', 'scarce')):
        group = [row for row in primary_rows if row['scenario'] == scene and row['primary_eligible'] and row['paired_crn_differences'] is not None]
        interval = paired_intervals([row['paired_crn_differences'] for row in group], scene_index=scene_index)
        eligible = [state for state in states if state['scenario'] == scene and state['primary_eligible']]
        lower = interval['environment_paired_95_interval'][0] if interval['n'] else None
        continuation = interval['n'] >= 10 and interval['mean_improvement'] >= .005 and lower > 0
        paired.append({'scenario': scene, 'planned_environments': 20,
                       'completed_environments': sum(state['scenario'] == scene for state in states),
                       'unseen_business_unique_environments': len(eligible), **interval,
                       'scene_resource_gate_passed': continuation,
                       'interpretation': 'exploratory only: fewer than 10 unseen distinct environments' if interval['n'] < 10 else 'preregistered finite-pool fixed-state paired contrast'})
    method_summaries = []
    for scope in ('unseen_business_unique', 'all_planned_descriptive'):
        for scene in ('balanced', 'scarce'):
            for method in METHODS:
                group = [row for row in rows if row['scenario'] == scene and row['method'] == method and
                         (scope == 'all_planned_descriptive' or row['primary_eligible'])]
                summary = {'scope': scope, 'scenario': scene, 'method': method, 'environments': len(group)}
                for key in ('final_action_regret', 'screening_loss', 'within_retained_selection_loss',
                            'candidate_reference_range', 'selected_reference_score'):
                    values = [row[key] for row in group if row[key] is not None]
                    summary['mean_' + key] = math.fsum(values) / len(values) if values else None
                summary['online_candidate_des'] = sum(row['actual_des'] for row in group)
                for key in ('offered', 'offered_value', 'accepted', 'accepted_value', 'success', 'earned_value'):
                    values = [row['reference_mean_' + key] for row in group if row['reference_mean_' + key] is not None]
                    summary['sum_reference_mean_' + key] = math.fsum(values) if values else None
                method_summaries.append(summary)
    result = {'schema': 'phase2e-a-paired-analysis-v1', 'planned_inputs': 40, 'actual_completed_inputs': len(states),
        'unseen_business_unique_inputs': sum(state['primary_eligible'] for state in states),
        'cross_seen_exact_duplicates': sum(state['cross_seen_exact_duplicate'] for state in states),
        'cross_seen_business_duplicates': sum(state['cross_seen_business_duplicate'] for state in states),
        'within_batch_business_duplicates': sum(state['within_batch_business_duplicate'] for state in states),
        'scenes': paired, 'method_summaries': method_summaries,
        'scientific_resource_criterion_passed': all(row['scene_resource_gate_passed'] for row in paired),
        'recommendation': 'consider a separately authorized fair closed-loop design' if all(row['scene_resource_gate_passed'] for row in paired) else 'do not automatically add pilot; negative result, conditional signal or insufficient independent evidence',
        'engineering_verification': verified,
        'limitations': ['environment uncertainty and reference MC error are separate intervals',
            'finite-pool reference max has selection bias; primary difference cancels that max',
            'within-retained-set loss is not wholly attributed to DES noise',
            'no pool-external coverage claim, long-term Q or global optimum',
            'equal DES fixed-state diagnostic; not equal wall-clock superiority or 720h mandatory noninferiority',
            'official Jev remains unaccepted; PPO/value training remain untrained'],
        'analysis_registration_sha256': canonical_sha256(registration['analysis'])}
    immutable(evaluation / 'per-method-losses.json', rows)
    write_csv(evaluation / 'per-method-losses.csv', rows)
    immutable(evaluation / 'candidate-reference.json', candidate_rows)
    immutable(evaluation / 'primary-paired-environments.json', primary_rows)
    immutable(evaluation / 'paired-summary.json', result)
    return result
