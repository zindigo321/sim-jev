"""Preregistered environment and paired-reference uncertainty, not long-horizon Q."""
from collections import Counter
import hashlib
import math
from pathlib import Path
import numpy as np
from scripts.phase2eb import read, jsonlines, check, immutable, METHODS, canonical_sha256
from scripts.phase2c_experiment import write_csv


def paired(values, scene_index):
    a = np.asarray(values, dtype=float)
    if a.size == 0:
        return dict(n=0, mean=None, environment_ci95=None, environment_one_sided_upper95=None,
                    reference_mc_ci95=None, reference_mc_se=None)
    if a.ndim != 2 or a.shape[1] != 32 or not np.isfinite(a).all():
        raise ValueError('expected paired environment by 32 matrix')
    n = len(a)
    env = np.random.default_rng(2026111001 + scene_index * 100)
    idx = env.integers(0, n, (5000, n))
    draws = a.mean(axis=1)[idx].mean(axis=1)
    rng = np.random.default_rng(2026111002 + scene_index * 100)
    mc = np.zeros(5000)
    digest = hashlib.sha256()
    for row in a:
        indices = rng.integers(0, 32, (5000, 32))
        digest.update(indices.astype('<i8').tobytes())
        mc += row[indices].mean(axis=1) / n
    return dict(n=n, mean=float(a.mean()), environment_ci95=np.quantile(draws, [.025, .975]).tolist(),
        environment_one_sided_upper95=float(np.quantile(draws, .95)),
        reference_mc_ci95=np.quantile(mc, [.025, .975]).tolist(),
        reference_mc_se=float(np.sqrt(np.sum(a.var(axis=1, ddof=1)/32))/n),
        environment_resampling_sha256=hashlib.sha256(idx.astype('<i8').tobytes()).hexdigest(),
        reference_resampling_sha256=digest.hexdigest(), bootstrap_n=5000,
        scope='environments and fixed-environment CRN32 are separate uncertainty sources')


def analyze(root):
    root = Path(root)
    f = check(root)
    # Require completed independent reference generation; audit is a separate entry point.
    if read(root / 'test-reference-status.json')['status'] != 'completed':
        raise ValueError('test reference incomplete')
    states = [read(root / 'states' / p['state_id'] / 'state.json') for p in f['planned']]
    actions = read(root / 'test/actions.json')
    candidates, metrics, references = [], [], {}
    for state in states:
        rows = jsonlines(root / 'reference' / state['state_id'] / 'branches.jsonl')
        scores = {c['id']: np.asarray([r['score'] for r in sorted(rows, key=lambda r: r['scenario_index'])
                                      if r['candidate_id'] == c['id']]) for c in state['pool']}
        require_lengths = all(len(a) == 32 for a in scores.values())
        if not require_lengths:
            raise ValueError('reference indices incomplete')
        means = {cid: float(a.mean()) for cid, a in scores.items()}
        best = min(means, key=lambda cid: (-means[cid], cid)) if means else None
        references[state['state_id']] = scores
        for cid, a in scores.items():
            delta = scores[best]-a
            candidates.append(dict(state_id=state['state_id'], split=state['split'], scenario=state['scenario'],
                candidate_id=cid, n=32, q_ref=means[cid], mc_se=float(a.std(ddof=1)/np.sqrt(32)),
                branch_scores=a.tolist(), paired_max_difference=delta.tolist(),
                paired_max_difference_se=float(delta.std(ddof=1)/np.sqrt(32)), best_finite_pool_id=best))
        for action in [a for a in actions if a['state_id'] == state['state_id']]:
            selected = action['selected_candidate_id']
            kept = action['retained_ids']
            top = max(means[c] for c in kept) if kept else None
            counts = [r['counts'] for r in rows if r['candidate_id'] == selected]
            m = dict(state_id=state['state_id'], scenario=state['scenario'], method=action['method'],
                primary_eligible=state['primary_eligible'], selected=selected,
                best_finite_pool_id=best, finite_pool_max=means[best] if best else None,
                screening_loss=means[best]-top if top is not None else None,
                within_selection_loss=top-means[selected] if top is not None else None,
                final_regret=means[best]-means[selected] if selected else None,
                selected_q_ref=means.get(selected),
                actual_des=action['actual_des'], rule_violations=action['rule_violations'],
                failure=action['failure'], fallback=action['fallback'],
                wall_seconds=action['continuous_wall_seconds'],
                reused_score_path_seconds=action['reused_score_path_seconds'])
            for key in ('offered', 'offered_value', 'accepted', 'accepted_value', 'success', 'earned_value',
                        'failed', 'accepted_not_executed', 'unfinished', 'mandatory_shortfall_as_of'):
                m['mean_' + key] = float(np.mean([c[key] for c in counts])) if counts else None
            if selected:
                m['regret_fixed_max_mc_se'] = float((scores[best]-scores[selected]).std(ddof=1)/np.sqrt(32))
            metrics.append(m)
    summaries, comparisons = {}, {}
    model_gate = read(root / 'models/gate.json')
    for si, scene in enumerate(('balanced', 'scarce')):
        eligible = [s for s in states if s['split'] == 'test' and s['scenario'] == scene and s['primary_eligible']]
        ids = [s['state_id'] for s in eligible]
        summary = dict(n=len(ids), planned_test=16, excluded=16-len(ids), methods={})
        for method in METHODS:
            values = [m for m in metrics if m['state_id'] in ids and m['method'] == method]
            timing = [v['wall_seconds'] for v in values if v['wall_seconds'] is not None]
            common = [m for m in values if m['final_regret'] is not None]
            record = dict(n=len(values), actual_des=sum(v['actual_des'] for v in values),
                rules=sum(v['rule_violations'] for v in values), failures=sum(v['failure'] is not None for v in values),
                fallback_rate=sum(v['fallback'] for v in values)/len(values) if values else None,
                wall_p50=float(np.quantile(timing, .5)) if timing else None,
                wall_p95=float(np.quantile(timing, .95)) if timing else None,
                quality_only=method=='q8')
            for field in ('screening_loss', 'within_selection_loss', 'final_regret', 'selected_q_ref'):
                valid = [v[field] for v in common if v[field] is not None]
                record[field] = float(np.mean(valid)) if valid else None
            for field in ('offered', 'offered_value', 'accepted', 'accepted_value', 'success', 'earned_value',
                          'failed', 'accepted_not_executed', 'unfinished', 'mandatory_shortfall_as_of'):
                valid = [v['mean_'+field] for v in common if v['mean_'+field] is not None]
                record['total_reference_mean_'+field] = math.fsum(valid) if valid else None
            den = record['total_reference_mean_offered']
            denv = record['total_reference_mean_offered_value']
            record['aggregate_DSR'] = record['total_reference_mean_success']/den if den else None
            record['aggregate_RCR'] = record['total_reference_mean_earned_value']/denv if denv else None
            summary['methods'][method] = record
        contrasts = {}
        for first, second, label in ([('h32', m, m+'_regret_increment_vs_h32') for m in ('h0','h8','A8','B8','q8')]
                + [('B8','h8','B8_improvement_vs_h8'), ('B8','A8','B8_improvement_vs_A8'), ('B8','q8','B8_improvement_vs_q8')]):
            differences = []
            for sid in ids:
                chosen = {a['method']: a['selected_candidate_id'] for a in actions if a['state_id'] == sid}
                if chosen[first] is not None and chosen[second] is not None:
                    differences.append(references[sid][chosen[first]] - references[sid][chosen[second]])
            contrasts[label] = paired(differences, si)
        anchor = summary['methods']['h32']
        gates = {}
        for method in ('h0', 'h8', 'A8', 'B8', 'q8'):
            m = summary['methods'][method]
            contrast = contrasts[method+'_regret_increment_vs_h32']
            route_ok = method[0] not in ('A','B') or method[0] in model_gate['models']
            quality = bool(route_ok and len(ids) >= 12 and contrast['environment_one_sided_upper95'] is not None
                and contrast['environment_one_sided_upper95'] <= .005 and m['rules'] <= anchor['rules'])
            speed = bool(method != 'q8' and m['wall_p50'] is not None and
                m['wall_p50'] <= .8*anchor['wall_p50'] and m['wall_p95'] <= anchor['wall_p95'])
            gates[method] = dict(route_available=route_ok, quality_gate=quality,
                speed_gate=speed if method != 'q8' else None,
                p50_ratio=m['wall_p50']/anchor['wall_p50'] if m['wall_p50'] is not None else None,
                p95_ratio=m['wall_p95']/anchor['wall_p95'] if m['wall_p95'] is not None else None,
                deployment_gate=quality and speed)
        signal = [contrasts['B8_improvement_vs_'+m] for m in ('h8','A8')]
        summary['qwen_signal'] = dict(
            means_positive=all(x['mean'] is not None and x['mean'] > 0 for x in signal),
            intervals_strictly_positive=all(x['environment_ci95'] and x['environment_ci95'][0] > 0 for x in signal),
            sufficient_samples=len(ids) >= 12, learning_route=model_gate['status'])
        summary['gates'] = gates
        summaries[scene] = summary
        comparisons[scene] = contrasts
    budget = read(root / 'generation-budget.json')
    http = read(root / 'http-budget.json')
    journal = jsonlines(root / 'des-journal.jsonl')
    counts = Counter(e['phase'] for e in journal if e['status'] == 'pending')
    result = dict(schema='phase2eb-analysis-v1', scenarios=summaries, comparisons=comparisons,
        resource=dict(formal_des=dict(counts), total_formal_des=sum(counts.values()),
            real_generation_attempts=len(budget['attempts']), real_logical_requests=len({a['slot'] for a in budget['attempts']}),
            generation_seconds=budget['seconds'], metadata=sum(r['kind']=='metadata' for r in http['requests']),
            trajectory_calls=len(states), trajectory_hours=336*len(states), audit_replay_des=0,
            hardware_energy_joules=None, monetary_cost=None),
        cross_scene_deployment=[m for m in ('h0','h8','A8','B8') if all(s['gates'][m]['deployment_gate'] for s in summaries.values())],
        cross_scene_qwen_signal=all(s['qwen_signal']['means_positive'] and s['qwen_signal']['intervals_strictly_positive']
            and s['qwen_signal']['sufficient_samples'] and s['qwen_signal']['learning_route']=='passed' for s in summaries.values()),
        limitations=['fixed states and finite candidate pool only', 'MC sample maximum selection bias remains',
            '32 paired scenarios are one environment', 'no long-horizon noninferiority or official Jev validation',
            'Qwen weights remain frozen; trained models are NumPy residual rankers'])
    immutable(root / 'analysis/reference-candidates.json', candidates)
    immutable(root / 'analysis/test-metrics.json', metrics)
    write_csv(root / 'analysis/test-metrics.csv', metrics)
    immutable(root / 'analysis/summary.json', result)
    return result
