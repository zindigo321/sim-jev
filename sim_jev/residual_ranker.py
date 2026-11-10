"""Frozen public numeric features and state-weighted NumPy ridge residuals.

No simulator, checkpoint, seed, IDs, outcomes or environment labels are inputs.
Candidate IDs only align output and break prediction ties outside this module.
"""
from __future__ import annotations
import math
import numpy as np

FEATURES = (
    'nominal_dsr', 'nominal_rcr', 'accepted_fraction', 'planned_afh_fraction',
    'peak_fraction', 'reserve_slack_fraction', 'afh_slack_fraction',
    'mean_duration_fraction', 'max_duration_fraction', 'early_afh_fraction',
    'idle_fraction', 'flying_fraction', 'active_repairs_fraction',
    'on_hand_per_aircraft', 'in_transit_per_aircraft', 'unmet_per_aircraft',
    'nominal_times_unavailable', 'load_times_unavailable',
)
QWEN_FEATURES = ('qwen_score_div100', 'qwen_centered', 'qwen_average_rank_fraction')
GRID = (0.01, 0.1, 1.0, 10.0)


def features(pool, scores=None):
    """Only listed public summary fields are read; equal Qwen scores share rank."""
    out = []
    qvalues = sorted(scores.values()) if scores is not None else None
    if scores is not None and (set(scores) != {c.id for c in pool} or
            any(type(v) is not int or not 0 <= v <= 100 for v in scores.values())):
        raise ValueError('complete strict frozen Qwen scores required')
    center = math.fsum(qvalues) / len(qvalues) / 100 if qvalues else 0.
    for c in pool:
        s = c.summary
        h, u, r = s['heuristic'], s['plan_usage'], s['resources']
        fleet = r['fleet_size']
        window = s['window_end'] - s['window_start']
        scale = fleet * window
        missions = s['missions']
        durations = [m['duration_hours'] for m in missions]
        idle = sum(a['state'] == 'idle' for a in r['aircraft']) / fleet
        flying = sum(a['state'] == 'flying' for a in r['aircraft']) / fleet
        unavailable = 1 - idle - flying
        cap = s['active_rule']['max_aircraft_hours']
        x = [h['DSR_proxy'], h['RCR_proxy'],
             s['accepted_count'] / s['offered_count'] if s['offered_count'] else 0.,
             u['aircraft_hours'] / scale, u['peak_aircraft'] / fleet,
             (u['minimum_reserve'] - s['active_rule']['min_reserve']) / fleet,
             (cap - u['aircraft_hours']) / scale if cap is not None else float('nan'),
             math.fsum(durations) / len(durations) / window if durations else 0.,
             max(durations, default=0.) / window,
             math.fsum(m['duration_hours'] * m['aircraft_required'] for m in missions
                       if m['start_time'] < s['window_start'] + 24) / scale,
             idle, flying, sum(v['status'] != 'completed' for v in r['repairs']) / fleet,
             r['inventory']['on_hand'] / fleet, r['inventory']['in_transit'] / fleet,
             r['inventory']['unmet_demand'] / fleet,
             h['score'] * unavailable, u['aircraft_hours'] / scale * unavailable]
        if scores is not None:
            q = scores[c.id]
            # Average descending rank; tied values have identical features.
            rank = sum(v > q for v in qvalues) + (sum(v == q for v in qvalues) - 1) / 2
            x += [q / 100, q / 100 - center, rank / max(1, len(qvalues) - 1)]
        out.append(x)
    return np.asarray(out, dtype=float).reshape(len(pool), len(FEATURES) + (3 if scores is not None else 0))


def fit(groups, alpha, *, qwen=False):
    """groups: (features, reference_minus_nominal); each state has unit weight."""
    if alpha not in GRID or not groups or any(len(x) == 0 for x, y in groups):
        raise ValueError('nonempty training states and registered alpha required')
    x = np.concatenate([g[0] for g in groups])
    y = np.concatenate([g[1] for g in groups])
    w = np.concatenate([np.full(len(a), 1 / len(a)) for a, _ in groups])
    expected = len(FEATURES) + (3 if qwen else 0)
    if x.shape != (len(y), expected) or not np.all(np.isfinite(y)):
        raise ValueError('training data shape or labels invalid')
    # Weighted median imputation uses only training observations, then weighted scaling.
    impute = []
    for j in range(expected):
        valid = np.isfinite(x[:, j])
        order = np.argsort(x[valid, j], kind='stable')
        vals, weights = x[valid, j][order], w[valid][order]
        impute.append(float(vals[np.searchsorted(np.cumsum(weights), weights.sum() / 2)]) if len(vals) else 0.)
    x = np.where(np.isfinite(x), x, impute)
    mean = np.average(x, weights=w, axis=0)
    scale = np.sqrt(np.average((x - mean) ** 2, weights=w, axis=0))
    scale = np.where(scale > 1e-12, scale, 1.)
    z = np.column_stack([np.ones(len(x)), (x - mean) / scale])
    penalty = np.eye(z.shape[1]) * alpha
    penalty[0, 0] = 0
    coef = np.linalg.solve(z.T @ (w[:, None] * z) + penalty, z.T @ (w * y))
    return dict(schema='phase2eb-ridge-residual-v1', qwen=qwen, alpha=alpha,
                feature_names=list(FEATURES + (QWEN_FEATURES if qwen else ())),
                impute=impute, mean=mean.tolist(), scale=scale.tolist(), coef=coef.tolist(),
                training_states=len(groups), training_candidates=len(y),
                objective='sum_state(mean_candidate_squared_residual_error) + alpha * squared_slopes')


def predict(model, pool, scores=None):
    if model['feature_names'] != list(FEATURES + (QWEN_FEATURES if model['qwen'] else ())):
        raise ValueError('feature contract differs')
    if model['qwen'] and scores is None:
        raise ValueError('B requires frozen Qwen scores')
    x = features(pool, scores if model['qwen'] else None)
    x = np.where(np.isfinite(x), x, model['impute'])
    z = np.column_stack([np.ones(len(x)), (x - model['mean']) / model['scale']])
    values = np.asarray([c.summary['heuristic']['score'] for c in pool]) + z @ model['coef']
    if not np.all(np.isfinite(values)):
        raise ValueError('non-finite prediction')
    return {c.id: float(v) for c, v in zip(pool, values)}


def ranked_ids(predictions):
    return sorted(predictions, key=lambda key: (-predictions[key], key))
