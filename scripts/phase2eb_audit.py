"""Independent read-only evidence accounting; never calls PlanningScenario.evaluate.

Checks raw business scalars, CRN identities, ledger reservations, actions, model
normal equations, bootstrap contrasts and all seals. Physical log auditing is
performed on retained artifacts without advancing any simulator.
"""
from __future__ import annotations
from collections import Counter
import hashlib
import math
from pathlib import Path
import time
import numpy as np

from scripts.phase2eb import (read, jsonlines, file_sha, check, manifest, selection_files,
    LIMITS, METHODS, get_scores, unpack, canonical_sha256, immutable)
from scripts.phase2d_r1_data_audit import _scalar
from scripts.phase2e_diagnostic import request_pool_identity
from scripts.planning_verify import audit_planning_branch
from scripts.weekly_verify import audit_business_log
from sim_jev.candidates import generate_candidates, heuristic_ranking
from sim_jev.local_provider import validate_record
from sim_jev.residual_ranker import features, predict, ranked_ids
from sim_jev.weekly import WeeklySimulation
from sim_jev.weekly_policy import validate_command


def require(condition, message):
    if not condition:
        raise ValueError(message)


def audit_states(root, frozen):
    prior = read(root / 'history/seen-public.json')
    require(canonical_sha256(prior) == frozen['history_sha256'], 'history changed')
    exact = {r['exact_input_sha256'] for r in prior}
    business = {r['business_input_sha256'] for r in prior}
    states = []
    for split in ('train', 'validation', 'test'):
        for plan in [p for p in frozen['planned'] if p['split'] == split]:
            s = read(root / 'states' / plan['state_id'] / 'state.json')
            require(all(s[k] == v for k, v in plan.items()), 'planned state changed')
            a, b = request_pool_identity(s['request'], s['pool'])
            require((a, b) == (s['exact_input_sha256'], s['business_input_sha256']), 'public hash mismatch')
            require(s['exact_duplicate'] == (a in exact) and s['business_duplicate'] == (b in business), 'dedup differs')
            require(s['primary_eligible'] == (a not in exact and b not in business), 'eligibility differs')
            exact.add(a)
            business.add(b)
            req, pool = unpack(s)
            regenerated = sorted(generate_candidates(req, 16, frozen['generation_seed']), key=lambda c: c.id)
            require([c.to_dict() for c in regenerated] == s['pool'], 'candidate pool differs')
            cp = read(root / 'states' / s['state_id'] / 'mandatory-trajectory-checkpoint.json')
            sim = WeeklySimulation.from_checkpoint(cp)
            require(sim.now == 336 and sim.config.seed == s['seed'], 'snapshot boundary/seed differs')
            audit_business_log(sim)
            construction = read(root / 'states' / s['state_id'] / 'construction.json')
            require(construction['simulated_hours'] == 336 and construction['candidate_des'] == 0, 'trajectory cost differs')
            states.append(s)
    return states


def audit_qwen(root, frozen, states):
    budget = read(root / 'generation-budget.json')
    http = read(root / 'http-budget.json')
    require(len(budget['attempts']) <= 208 and budget['seconds'] <= 5400, 'generation cap exceeded')
    require(len({a['slot'] for a in budget['attempts']}) <= 104, 'logical cap exceeded')
    require(math.isclose(budget['seconds'], math.fsum(a.get('seconds', 0) for a in budget['attempts']), abs_tol=1e-8), 'generation seconds differ')
    attempts = {a['attempt_id']: a for a in budget['attempts']}
    require(len(attempts) == len(budget['attempts']), 'duplicate attempt ID')
    require(sum(r['kind'] == 'metadata' for r in http['requests']) <= 450, 'metadata cap exceeded')
    linked = set()
    for i, r in enumerate(http['requests']):
        request = read(root / 'http' / f'{i:04d}-request.json')
        require(request['base_url'].startswith('http://127.0.0.1:'), 'non-loopback request')
        require(r['index'] == i and r['route'] == request['route'] and
                canonical_sha256(request['data']) == r['request_sha256'], 'raw HTTP request mismatch')
        if r['status'] != 'pending':
            require(file_sha(root / r['artifact']) == r['artifact_sha256'], 'HTTP response changed')
        if r['kind'] == 'generation':
            aid = r['generation_attempt_id']
            require(aid in attempts and aid not in linked, 'HTTP generation reservation missing/reused')
            linked.add(aid)
    valid = failures = 0
    raw_attempts = set()
    requests = [(s['state_id'], s, False) for s in states]
    for i, s in enumerate(read(root / 'history/order-states.json')):
        requests.extend([(f'order-{i*2}', s, False), (f'order-{i*2+1}', s, True)])
    for slot, state, reverse in requests:
        value = read(root / 'qwen' / (slot + '.json'))
        req, pool = unpack(state)
        if reverse:
            pool.reverse()
        record = value['record']
        if record is None:
            failures += 1
            require(value['status'] == 'failed' and value['error'], 'missing Qwen failure')
            continue
        validate_record(record, req, pool, frozen['identity'])
        require(record['evidence'] in ('local_inference', 'deterministic'), 'cached/fixture score counted as fresh')
        receipt = value['receipt']
        for index, a in enumerate(record['attempts']):
            aid = a['attempt_id']
            require(aid in attempts and aid not in raw_attempts and aid in linked, 'attempt missing/reused')
            raw_attempts.add(aid)
            reserved = attempts[aid]
            require(reserved['slot'] == slot and reserved['attempt_index'] == index and index < 2, 'attempt slot mismatch')
            require(a['seconds'] <= reserved['reserved_deadline_seconds'] + 1., 'generation exceeded deadline tolerance')
            require(reserved['reserved_deadline_seconds'] <= 120, 'deadline exceeds 120')
            require(a['independent_prompt_tokens'] == a['raw']['prompt_eval_count'], 'token mismatch')
            token = receipt['tokenizer'][index]
            require(token['tokens'] == a['independent_prompt_tokens'] and
                    token['prompt_sha256'] == hashlib.sha256(a['body']['prompt'].encode()).hexdigest(), 'independent token receipt mismatch')
            artifact = read(root / 'responses' / slot / reserved['artifact']['path'])
            require(canonical_sha256(artifact) == reserved['artifact']['sha256'], 'attempt artifact changed')
        if record['evidence'] == 'local_inference':
            gpu = next((m for m in receipt['gpu']['models'] if m['name'] == frozen['identity']['model']), None)
            require(gpu is not None and gpu['size_vram'] > 0 and gpu['context_length'] == 16384, 'GPU context missing')
            require(gpu['digest'].removeprefix('sha256:') == frozen['identity']['model_digest'].removeprefix('sha256:'), 'GPU model differs')
            valid += 1
    for aid, a in attempts.items():
        require(a['slot'] in budget['slots'], 'unregistered request slot')
        same = [r for r in attempts.values() if r['slot'] == a['slot']]
        require(len(same) <= 2 and len({r['logical_request_id'] for r in same}) == 1, 'logical retry replaced')
        if a['status'] == 'completed':
            require(aid in linked, 'completed generation lacks raw HTTP')
    return dict(valid_logical_requests=valid, failed_slots=failures, attempts=len(attempts),
        generation_seconds=budget['seconds'], metadata=sum(r['kind'] == 'metadata' for r in http['requests']),
        pending_uncertain=sum(a['status'] != 'completed' for a in attempts.values()),
        fixture_generations=0, remote_requests=0)


def audit_online(root, require_seal=True):
    root = Path(root)
    frozen = check(root)
    if require_seal:
        seal = read(root / 'test-action-seal.json')
        require(selection_files(root) == seal['files'], 'test actions/request seal changed')
        require(canonical_sha256(seal['files']) == seal['files_sha256'], 'seal hash mismatch')
    require(manifest(root / 'models') == read(root / 'model-seal.json')['files'], 'model seal differs')
    states = audit_states(root, frozen)
    qcost = audit_qwen(root, frozen, states)
    actions = read(root / 'test/actions.json')
    seen, des = set(), 0
    models = {name: read(root / 'models' / (name + '.json')) for name in ('A', 'B') if (root / 'models' / (name + '.json')).exists()}
    tests = [s for s in states if s['split'] == 'test']
    for index, state in enumerate(tests):
        req, pool = unpack(state)
        q = get_scores(read(root / 'qwen' / (state['state_id'] + '.json'))['record'])
        base = list(METHODS[:5])
        expected_order = base[index % 5:] + base[:index % 5] + ['q8']
        actual = [a for a in actions if a['state_id'] == state['state_id']]
        require([a['method'] for a in actual] == expected_order, 'method interleave differs')
        for a in actual:
            method = a['method']
            key = state['state_id'], method
            require(key not in seen, 'duplicate action')
            seen.add(key)
            directory = root / 'test' / state['state_id'] / method
            require(read(directory / 'action.json') == a, 'action export differs')
            order = [c.id for c in heuristic_ranking(pool)]
            if method in ('A8', 'B8') and not a['fallback'] and a['predictions'] is not None:
                prediction = predict(models[method[0]], pool, q)
                require(prediction == a['predictions'], 'prediction mismatch')
                order = ranked_ids(prediction)
            elif method == 'q8' and q is not None:
                order = ranked_ids(q)
            require(order == a['ranking_ids'], 'ranking differs')
            k, b = (4, 8) if method == 'h32' else (1, 0) if method == 'h0' else (2, 4)
            if len(pool) < 2:
                b = 0
            kept = order[:k]
            require(a['retained_ids'] == (kept if b else None), 'retained set differs')
            rows = jsonlines(directory / 'branches.jsonl') if (directory / 'branches.jsonl').exists() else []
            require([(r['candidate_id'], r['scenario_index']) for r in rows] ==
                    [(cid, j) for j in range(b) for cid in kept], 'online branch allocation differs')
            values = {cid: math.fsum(r['score'] for r in rows if r['candidate_id'] == cid) / b for cid in kept} if b else {}
            chosen = min(values, key=lambda cid: (-values[cid], cid)) if b else order[0] if order else None
            require(chosen == a['selected_candidate_id'] and a['actual_des'] == len(rows), 'selected action or budget differs')
            if chosen:
                c = next(c for c in pool if c.id == chosen)
                require(a['command'] == c.command.to_dict(), 'selected command differs')
                require(validate_command(c.command, req.observation, req.context) == c.command, 'constraint violation')
            require(a['rule_violations'] == 0, 'violation count differs')
            require((a['continuous_wall_seconds'] is None) == (method == 'q8'), 'continuous timing misclassified')
            des += len(rows)
    require(len(actions) == len(tests) * 6 == len(seen), 'missing test methods')
    require(des <= 2048, 'test DES exceeds cap')
    return dict(status='passed', states=len(states), test_actions=len(actions), test_des=des, qwen=qcost)


def audit_branches(root, frozen, states):
    events = jsonlines(root / 'des-journal.jsonl')
    ledger = {}
    for e in events:
        require(e['freeze_sha256'] == canonical_sha256(frozen), 'journal freeze differs')
        if e['status'] == 'pending':
            require(e['key'] not in ledger, 'duplicate reserve')
        else:
            require(e['status'] == 'completed' and ledger.get(e['key'], {}).get('status') == 'pending', 'invalid completion')
        ledger[e['key']] = e
    require(all(e['status'] == 'completed' for e in ledger.values()), 'uncertain DES')
    counts, matched, physical = Counter(), set(), 0
    for state in states:
        req, pool = unpack(state)
        by = {c.id: c for c in pool}
        paths = [('reference', 32, frozen['reference_seed'], 'reference')]
        if state['split'] != 'test':
            paths.append(('online-library', 8, frozen['online_seed'], 'train_online'))
        else:
            for method in METHODS:
                paths.append(('test', None, frozen['online_seed'], 'test_online', method))
        for spec in paths:
            namespace = f'{spec[0]}/{state["state_id"]}' + ('/' + spec[4] if len(spec) == 5 else '')
            directory = root / namespace
            rows = jsonlines(directory / 'branches.jsonl') if (directory / 'branches.jsonl').exists() else []
            if spec[1] is not None:
                require([(r['candidate_id'], r['scenario_index']) for r in rows] ==
                        [(c.id, j) for j in range(spec[1]) for c in pool], 'reference/library coverage differs')
            costs = read(directory / 'cost.json')
            require(costs['actual_des'] == len(rows), 'branch cost count differs')
            for r in rows:
                _scalar(r, req, by[r['candidate_id']], spec[2])
                require(r['audit']['verified'] and r['audit']['score'] == r['score'] and r['audit']['counts'] == r['counts'], 'branch audit scalar differs')
                key = f'{namespace}/{r["candidate_id"]}/{r["scenario_index"]}'
                require(key in ledger and key not in matched, 'branch not uniquely reserved')
                require(ledger[key]['scalar_sha256'] == canonical_sha256(r), 'journal scalar hash differs')
                require(ledger[key]['phase'] == spec[3], 'wrong budget class')
                matched.add(key)
                counts[spec[3]] += 1
                if r['scenario_index'] == 0:
                    artifact = read(directory / f'physical-{r["candidate_id"]}-0.json')
                    audited = audit_planning_branch(artifact, req)
                    require(audited == r['audit'], 'retained physical audit differs')
                    require(canonical_sha256(artifact['final_state']) == r['physical_artifact_sha256'], 'physical SHA differs')
                    physical += 1
            require(math.isclose(costs['des_seconds'], math.fsum(r['compute_seconds'] for r in rows), abs_tol=1e-7), 'DES duration differs')
            require(math.isclose(costs['audit_seconds'], math.fsum(r['audit_seconds'] for r in rows), abs_tol=1e-7), 'audit duration differs')
    require(matched == set(ledger), 'unaccounted DES reservations')
    require(all(counts[k] <= limit for k, limit in LIMITS.items()), 'formal budget exceeded')
    return dict(counts=dict(counts), actual_des=sum(counts.values()), physical_artifacts=physical,
                simulated_hours=168 * sum(counts.values()), pending_uncertain=0)


def audit_training(root, states):
    result = {}
    table = read(root / 'models/training-feature-table.json')
    for name in ('A', 'B'):
        path = root / 'models' / (name + '.json')
        if not path.exists():
            result[name] = 'partial'
            continue
        m = read(path)
        groups = []
        for s in states:
            if s['split'] != 'train' or not s['primary_eligible']:
                continue
            req, pool = unpack(s)
            q = get_scores(read(root / 'qwen' / (s['state_id'] + '.json'))['record'])
            if name == 'B' and q is None:
                continue
            x = features(pool, q if name == 'B' else None)
            labels = jsonlines(root / 'reference' / s['state_id'] / 'branches.jsonl')
            y = [np.mean([r['score'] for r in labels if r['candidate_id'] == c.id]) - c.summary['heuristic']['score'] for c in pool]
            groups.append((x, np.asarray(y)))
        x, y = np.concatenate([x for x, _ in groups]), np.concatenate([y for _, y in groups])
        w = np.concatenate([np.full(len(x), 1 / len(x)) for x, _ in groups])
        impute = []
        for j in range(x.shape[1]):
            valid = np.isfinite(x[:, j])
            seq = sorted(zip(x[valid, j].tolist(), w[valid].tolist()))
            total = math.fsum(b for _, b in seq)
            running, value = 0., 0.
            for value, weight in seq:
                running += weight
                if running >= total / 2:
                    break
            impute.append(value)
        require(np.allclose(impute, m['impute'], atol=1e-12, rtol=1e-12), 'training-only imputation differs')
        x = np.where(np.isfinite(x), x, m['impute'])
        mean = np.average(x, weights=w, axis=0)
        sd = np.sqrt(np.average((x-mean)**2, weights=w, axis=0))
        sd[sd <= 1e-12] = 1.
        require(np.array_equal(mean, m['mean']) and np.array_equal(sd, m['scale']), 'training-only scaling differs')
        z = np.c_[np.ones(len(x)), (x - mean) / sd]
        penalty = np.diag([0.] + [math.sqrt(m['alpha'])] * (z.shape[1]-1))
        design = np.vstack([z * np.sqrt(w[:, None]), penalty])
        target = np.r_[y * np.sqrt(w), np.zeros(z.shape[1])]
        coef = np.linalg.lstsq(design, target, rcond=None)[0]
        require(np.allclose(coef, m['coef'], atol=1e-10, rtol=1e-10), 'independent ridge solution differs')
        vals = read(root / 'models' / (name + '-validation.json'))
        grid = read(root / 'models' / (name + '-grid.json'))
        for alpha, candidate_model in grid.items():
            penalty = np.diag([0.] + [math.sqrt(float(alpha))] * (z.shape[1]-1))
            solved = np.linalg.lstsq(np.vstack([z*np.sqrt(w[:,None]),penalty]),target,rcond=None)[0]
            require(np.allclose(solved,candidate_model['coef'],atol=1e-10,rtol=1e-10), 'grid ridge differs')
        state_by_id = {s['state_id']:s for s in states}
        for v in vals:
            state = state_by_id[v['state_id']]
            require(state['split']=='validation' and state['primary_eligible'], 'validation contains ineligible state')
            _, pool = unpack(state)
            q = get_scores(read(root/'qwen'/(state['state_id']+'.json'))['record'])
            prediction = predict(grid[str(v['alpha'])],pool,q)
            require(prediction==v['predictions'],'validation prediction differs')
            kept=ranked_ids(prediction)[:2]
            raw=jsonlines(root/'online-library'/state['state_id']/'branches.jsonl')
            values={c:math.fsum(r['score'] for r in raw if r['candidate_id']==c and r['scenario_index']<4)/4 for c in kept}
            chosen=min(values,key=lambda c:(-values[c],c))
            ref=jsonlines(root/'reference'/state['state_id']/'branches.jsonl')
            means={c.id:np.mean([r['score'] for r in ref if r['candidate_id']==c.id]) for c in pool}
            require(chosen==v['selected'] and kept==v['retained'],'validation action differs')
            require(abs(v['regret']-(max(means.values())-means[chosen]))<1e-12,'validation regret differs')
        means = {alpha: np.mean([v['regret'] for v in vals if v['alpha'] == alpha]) for alpha in (.01, .1, 1., 10.)}
        expected = max(a for a, v in means.items() if abs(v-min(means.values())) <= 1e-12)
        require(m['alpha'] == expected, 'validation selection/stronger tie break differs')
        result[name] = dict(independent_lstsq_equal=True, training_states=len(groups), alpha=m['alpha'])
    return result


def audit_analysis(root, states):
    if not (root/'analysis/summary.json').exists():
        return dict(status='not_yet_generated')
    summary=read(root/'analysis/summary.json')
    actions=read(root/'test/actions.json')
    checked=0
    for si,scene in enumerate(('balanced','scarce')):
        valid=[s for s in states if s['split']=='test' and s['scenario']==scene and s['primary_eligible']]
        require(summary['scenarios'][scene]['n']==len(valid),'analysis n differs')
        lookup={}
        for state in valid:
            rows=jsonlines(root/'reference'/state['state_id']/'branches.jsonl')
            lookup[state['state_id']]={c['id']:np.array([r['score'] for r in sorted(rows,key=lambda r:r['scenario_index'])
                if r['candidate_id']==c['id']]) for c in state['pool']}
        for method in METHODS:
            chosen=[a for a in actions if a['method']==method and a['state_id'] in lookup]
            regrets=[]
            for a in chosen:
                scores=lookup[a['state_id']]
                regrets.append(max(np.mean(x) for x in scores.values())-np.mean(scores[a['selected_candidate_id']]))
            saved=summary['scenarios'][scene]['methods'][method]
            require(abs(saved['final_regret']-np.mean(regrets))<1e-12,'summary regret differs')
            require(saved['actual_des']==sum(a['actual_des'] for a in chosen),'summary DES differs')
            if method!='q8':
                wall=[a['continuous_wall_seconds'] for a in chosen]
                require(np.isclose(saved['wall_p50'],np.percentile(wall,50),atol=1e-12),'P50 differs')
                require(np.isclose(saved['wall_p95'],np.percentile(wall,95),atol=1e-12),'P95 differs')
        for label,saved in summary['comparisons'][scene].items():
            if '_regret_increment_vs_h32' in label:
                first,second='h32',label.split('_regret_')[0]
            else:first,second='B8',label.split('_vs_')[1]
            deltas=[]
            for s in valid:
                selected={a['method']:a['selected_candidate_id'] for a in actions if a['state_id']==s['state_id']}
                scores=lookup[s['state_id']]
                deltas.append(scores[selected[first]]-scores[selected[second]])
            a=np.array(deltas);n=len(a)
            rng=np.random.default_rng(2026111001+si*100)
            env=a.mean(1)[rng.integers(n,size=(5000,n))].mean(1)
            rng=np.random.default_rng(2026111002+si*100)
            mc=np.zeros(5000)
            for row in a:mc += row[rng.integers(32,size=(5000,32))].mean(1)/n
            require(abs(saved['mean']-a.mean())<1e-12,'paired mean differs')
            require(np.allclose(saved['environment_ci95'],np.percentile(env,[2.5,97.5]),atol=1e-12),'environment interval differs')
            require(abs(saved['environment_one_sided_upper95']-np.percentile(env,95))<1e-12,'upper95 differs')
            require(np.allclose(saved['reference_mc_ci95'],np.percentile(mc,[2.5,97.5]),atol=1e-12),'MC interval differs')
            checked+=1
    return dict(status='passed',independent_paired_contrasts=checked)


def audit(root):
    root = Path(root)
    started = time.perf_counter()
    online = audit_online(root)
    frozen = check(root)
    states = [read(root / 'states' / p['state_id'] / 'state.json') for p in frozen['planned']]
    start = read(root / 'test-reference-started.json')
    seal = read(root / 'test-action-seal.json')
    require(start['unix'] >= seal['sealed_unix'] and start['action_seal_sha256'] == file_sha(root / 'test-action-seal.json'), 'test labels precede action seal')
    require(read(root / 'model-seal.json')['sealed_unix'] <= seal['sealed_unix'], 'model sealed too late')
    branches = audit_branches(root, frozen, states)
    training = audit_training(root, states)
    analysis = audit_analysis(root,states)
    result = dict(status='passed', online=online, branches=branches, training=training, analysis=analysis,
        trajectory_build_calls=len(states), trajectory_hours=336 * len(states),
        new_des=0, new_qwen_generations=0, audit_seconds=time.perf_counter()-started,
        source_sha256=frozen['source']['sha256'])
    # Caller chooses a new output receipt; never rewrite sealed evidence.
    return result
