"""Phase 2D-R1: gated local-score development and preregistered new experiments."""
from __future__ import annotations

import argparse
import hashlib
from copy import deepcopy
from dataclasses import asdict
import json
import math
from pathlib import Path
import statistics
import subprocess
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sim_jev.branch_audit_store import BranchAuditStore
from sim_jev.planning import PlanningInput, PlanningScenario
from sim_jev.candidates import generate_candidates
from sim_jev.decision import DecisionRequest, DecisionResult, DecisionPolicySpec
from sim_jev.decision_resolver import DecisionRuntime, select_from_pool
from sim_jev.local_policy import LocalDecisionPolicySpec, LOCAL_SCORE_POLICY_VERSION
from sim_jev.local_provider import (LocalProvider, LocalError, GenerationBudget, identity_v2,
    render_v2, SCORE_TEMPLATES, SCORE_CORRECTION, SCORE_SORT_RULE, validate_record, compact)
from sim_jev.provenance import canonical_sha256, code_fingerprint, git_state
from sim_jev.weekly import WeeklySimulation
from sim_jev.weekly_policy import PolicySpec, RuleSpec, choose_command
from scripts.phase2c_experiment import build_case as build_legacy_case, write_csv
from scripts.phase2d_local import read, save, count_tokens, unpack, METHODS, TOKENIZER_PATH
from scripts.phase2d_runtime import sha, api
from scripts.planning_verify import audit_planning_branch

ARITHMETIC = 'event-anchor-binary64-v2'
REQUEST = 'legal-weekly-request-v2'
ENGINEERING_GATES = ('recovery_fixes_verified', 'legacy_compatibility_verified',
    'pause_identity_verified', 'audit_ledger_resume_verified', 'local_score_v2_offline_verified')
ALL_GATES = (*ENGINEERING_GATES, 'all_development_states_valid', 'new_live_stability_verified',
    'formal_diagnostics_completed', 'pilot_completed', 'cache_only_replay_verified')


def immutable(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if canonical_sha256(read(path)) != canonical_sha256(value):
            raise ValueError('immutable manifest differs: ' + str(path))
        return
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')


def require_engineering(root):
    gate = read(root / 'engineering-gates.json')
    if not all(gate.get(key) is True for key in ENGINEERING_GATES):
        raise ValueError('recovery/legacy/offline engineering gates incomplete')
    # The evidence itself is bound to this implementation, not a handwritten flag.
    if gate['source_sha256'] != code_fingerprint(ROOT)['sha256']:
        raise ValueError('engineering evidence source changed; rerun verification')
    if not gate.get('proofs'):
        raise ValueError('engineering evidence receipts missing')
    for proof in gate['proofs']:
        path = (root / proof['path']).resolve()
        if not path.is_relative_to(root.resolve()) or hashlib.sha256(path.read_bytes()).hexdigest() != proof['sha256']:
            raise ValueError('engineering proof missing or modified')
    return gate


def build_case(name, seed, policy, runtime=None):
    old = build_legacy_case(name, seed, policy, runtime)
    return WeeklySimulation(old.config, old._input_missions, old._initial_lifetimes,
        scenario_id=old.weekly_state['scenario_id'],
        rules=[RuleSpec.from_dict(value) for value in old.weekly_state['rules_input']],
        policy=policy, decision_runtime=runtime, arithmetic_protocol=ARITHMETIC)


def policy(method, ident):
    if method == 'mandatory-first':
        return PolicySpec()
    if method.startswith('local-'):
        return LocalDecisionPolicySpec(method=method, identity=ident,
                                       version=LOCAL_SCORE_POLICY_VERSION, name='phase2d-r1')
    return DecisionPolicySpec(method=method)


def business_projection(request):
    """Exact content with only request/protocol identity wrappers excluded; no rounding."""
    return {key: deepcopy(request[key]) for key in ('observation', 'context', 'parameters')}


def capture_states(seeds, times):
    rows = []
    for name in ('balanced', 'scarce'):
        for seed in seeds:
            captured = []
            def capture(req, spec, runtime=None):
                if req.context.window_start in times:
                    captured.append(req)
                return DecisionResult(choose_command(req.observation, req.context, spec))
            with patch('sim_jev.weekly.resolve_decision', side_effect=capture):
                build_case(name, seed, PolicySpec()).run(max(times))
            for req in captured:
                pool = sorted(generate_candidates(req, 16, 20261102), key=lambda c: c.id)
                row = {'state_id': f'{name}-{seed}-{int(req.context.window_start)}',
                    'scenario': name, 'seed': seed, 'window_start': req.context.window_start,
                    'window_end': req.context.window_end, 'request': req.to_dict(),
                    'pool': [c.to_dict() for c in pool], 'actual_M': len(pool),
                    'canonical_request_sha256': canonical_sha256(req.to_dict()),
                    'reference_command': choose_command(req.observation, req.context, PolicySpec()).to_dict()}
                row['pool_sha256'] = canonical_sha256(row['pool'])
                row['independent_identity'] = canonical_sha256([row['canonical_request_sha256'], row['pool_sha256']])
                row['business_request_sha256'] = canonical_sha256(business_projection(row['request']))
                row['business_projection_rule'] = 'exact observation/context/parameters; exclude only request_id and protocol_version'
                rows.append(row)
    return rows


def provider(root, store, phase, ident=None, mode='live'):
    ident = ident or read(root / 'freeze.json')['identity']
    base_url = read(root / 'hardware/server.json')['base_url'] if mode == 'live' and (root / 'hardware/server.json').exists() else 'http://127.0.0.1:11439'
    return LocalProvider(ident, store, mode=mode,
        budget=GenerationBudget(root / 'generation-budget.json'), phase=phase,
        token_counter=count_tokens if mode == 'live' else None, base_url=base_url)


def development(root):
    require_engineering(root)
    if (root / 'freeze.json').exists():
        raise ValueError('development is closed after freeze')
    folder = root / 'development'
    templates = list(SCORE_TEMPLATES)
    manifest = {'schema': 'phase2d-r1-development-registration-v1',
        'seeds': [10, 11], 'times': [168, 672], 'scenarios': ['balanced', 'scarce'],
        'templates': SCORE_TEMPLATES, 'correction': SCORE_CORRECTION, 'sort_rule': SCORE_SORT_RULE,
        'arithmetic_protocol': ARITHMETIC, 'request_protocol': REQUEST,
        'selection': 'first template whose first full pass covers all 8 states; no third template',
        'stability': 'first 8 successful coverage requests plus repeated fixed row order until 20 valid logical completions; stop on any stability failure',
        'generation_limit': 80, 'campaign_path': 'generation-budget.json',
        'source_sha256': code_fingerprint(ROOT)['sha256']}
    immutable(folder / 'registration.json', manifest)
    rows = capture_states((10, 11), (168, 672))
    immutable(folder / 'states.json', rows)
    identities = {}
    tokenizer_python = Path('G:/sim-jev-runtime/tokenizer-env/Scripts/python.exe')
    info = subprocess.run([str(tokenizer_python), '-c',
        'import json,platform,tokenizers; print(json.dumps({"python":platform.python_version(),"tokenizers":tokenizers.__version__}))'],
        capture_output=True, text=True, check=True)
    tokenizer_runtime = json.loads(info.stdout)
    tokenizer_runtime['python_executable_sha256'] = sha(tokenizer_python)
    if tokenizer_runtime['tokenizers'] != '0.22.2':
        raise ValueError('fixed tokenizer runtime changed')
    for template in templates:
        ident = identity_v2(read(root / 'hardware/model-manifest.json'), template=template,
            arithmetic_protocol=ARITHMETIC, request_protocol=REQUEST,
            tokenizer_sha256=sha(TOKENIZER_PATH), tokenizer_runtime=tokenizer_runtime)
        identities[template] = ident
        counts = []
        for row in rows:
            req, pool = unpack(row)
            rendered = render_v2(req, pool, template)
            n = count_tokens(rendered['prompt'])
            if n + 1024 > 16384:
                raise ValueError('complete input exceeds context')
            counts.append(n)
            immutable(folder / template / (row['state_id'] + '-input.json'), rendered)
        immutable(folder / template / 'identity.json', ident)
        immutable(folder / template / 'preflight.json', {'tokens': counts, 'num_ctx': 16384})
    immutable(folder / 'identities.json', identities)
    return {'status': 'prepared', 'states': len(rows), 'templates': len(templates)}


def smoke(root):
    require_engineering(root)
    folder = root / 'development'
    reg = read(folder / 'registration.json')
    if reg['source_sha256'] != code_fingerprint(ROOT)['sha256']:
        raise ValueError('development configuration changed; create a new campaign')
    rows = read(folder / 'states.json')
    identities = read(folder / 'identities.json')
    journal_path = folder / 'live-requests.json'
    journal = read(journal_path) if journal_path.exists() else []
    if any(r['status'] not in ('completed', 'failed') for r in journal):
        raise ValueError('incomplete logical request is uncertain; no automatic resubmission')

    def execute(template, row, index, purpose):
        existing = [r for r in journal if r['template'] == template and r['logical_index'] == index]
        if existing:
            return existing[0]
        record = {'template': template, 'state_id': row['state_id'], 'logical_index': index,
            'purpose': purpose, 'status': 'pending', 'independent_identity': row['independent_identity'],
            'repeat_input': any(r['template'] == template and r['state_id'] == row['state_id'] for r in journal)}
        journal.append(record)
        save(journal_path, journal)
        p = provider(root, folder / template / 'responses', 'development', identities[template])
        req, pool = unpack(row)
        try:
            result = p.rank(req, pool, force=True)
            validate_record(result, req, pool, identities[template])
            immutable(folder / template / f'completion-{index:02d}.json', result)
            record.update(status='response_completed_pending_hardware', cache_key=result['cache_key'],
                attempt_ids=[a['attempt_id'] for a in result['attempts']],
                first_round_valid=result['first_round_valid'], generations=result['local_generation_attempts'],
                seconds=result['generation_seconds'], ties=result['ties'],
                preference_scores=result['preference_scores'], ranked_ids=result['ranked_ids'])
            record['gpu_metadata_queries'] = 1
            meta = api(p.base_url, '/api/ps')
            immutable(folder / template / f'gpu-{index:02d}.json', meta)
            loaded = next(x for x in meta['models'] if x['name'] == identities[template]['model'])
            if loaded['size_vram'] <= 0 or loaded['context_length'] != 16384:
                raise ValueError('GPU/context verification failed')
            record['status'] = 'completed'
        except LocalError as error:
            record.update(status='failed', error=error.code, audit=error.audit)
            save(journal_path, journal)
            if error.code != 'invalid_local_scores':
                raise
        finally:
            record['local_metadata_queries'] = p.metadata_queries
            record['actual_generations'] = p.generations
            save(journal_path, journal)
        save(journal_path, journal)
        print('development', template, index, row['state_id'], record['status'], flush=True)
        return record

    selected = None
    for template in reg['templates']:
        coverage = [execute(template, row, i, 'coverage') for i, row in enumerate(rows)]
        if all(r['status'] == 'completed' for r in coverage):
            selected = template
            break
    if selected is None:
        status = {'status': 'partial', 'all_development_states_valid': False,
            'new_live_stability_verified': False, 'reason': 'both preregistered templates failed complete coverage'}
    else:
        stable = True
        for i in range(8, 20):
            if execute(selected, rows[i % 8], i, 'stability')['status'] != 'completed':
                stable = False
                break
        completions = [r for r in journal if r['template'] == selected and r['status'] == 'completed']
        ids = [aid for r in completions for aid in r['attempt_ids']]
        if len(ids) != len(set(ids)):
            raise AssertionError('reused generation attempt IDs')
        status = {'status': 'passed' if stable and len(completions) == 20 else 'partial',
            'all_development_states_valid': True, 'new_live_stability_verified': stable and len(completions) == 20,
            'selected_template': selected, 'valid_completions': len(completions),
            'unique_states': len({r['state_id'] for r in completions}),
            'first_round_success_rate': sum(r['first_round_valid'] for r in completions) / len(completions),
            'correction_successes': sum(not r['first_round_valid'] for r in completions),
            'all_tied_fraction': statistics.fmean(r['ties']['all_tied'] for r in completions),
            'has_ties_fraction': statistics.fmean(r['ties']['has_ties'] for r in completions)}
        save(root / 'development-identity.json', identities[selected])
    budget = GenerationBudget(root / 'generation-budget.json').read()
    status.update(actual_generations=len(budget['attempts']), failed_logical_requests=sum(r['status'] == 'failed' for r in journal),
        generation_seconds=budget['seconds'], remote_typesafe_requests=0, official_jev_accepted=False)
    save(folder / 'status.json', status)
    return status


def validate_development_gate(root):
    gate = read(root / 'development/status.json')
    if gate['status'] != 'passed' or not gate['all_development_states_valid'] or not gate['new_live_stability_verified']:
        raise ValueError('v2 true generation coverage/stability gates incomplete')
    ident = read(root / 'development-identity.json')
    if ident.get('protocol') != 'local-preference-scores-v2':
        raise ValueError('v1 cannot unlock v2 acceptance')
    rows = read(root / 'development/states.json')
    if len(rows) != 8 or {row['state_id'] for row in rows} != {
        f'{scenario}-{seed}-{hour}' for scenario in ('balanced', 'scarce') for seed in (10, 11) for hour in (168, 672)}:
        raise ValueError('all eight preregistered development states required')
    selected = gate['selected_template']
    registration = read(root / 'development/registration.json')
    if registration['source_sha256'] != code_fingerprint(ROOT)['sha256'] or selected not in registration['templates']:
        raise ValueError('development source/template registration differs')
    if read(root / 'development/identities.json')[selected] != ident:
        raise ValueError('selected development identity differs')
    budget = GenerationBudget(root / 'generation-budget.json').read()
    if any(a['status'] != 'completed' for a in budget['attempts']):
        raise ValueError('uncertain generation prevents acceptance')
    attempts = {a['attempt_id']: a for a in budget['attempts']}
    if len(attempts) != len(budget['attempts']):
        raise ValueError('duplicate attempt identity')
    seen = set()
    for index in range(20):
        req, pool = unpack(rows[index % 8])
        record = read(root / 'development' / selected / f'completion-{index:02d}.json')
        validate_record(record, req, pool, ident)
        if record['evidence'] != 'local_inference' or not record['valid_local_model']:
            raise ValueError('cache/fixture cannot count as new real completion')
        metadata = read(root / 'development' / selected / f'gpu-{index:02d}.json')
        loaded = next((x for x in metadata['models'] if x['name'] == ident['model']), None)
        if loaded is None or loaded['size_vram'] <= 0 or loaded['context_length'] != 16384:
            raise ValueError('completion lacks matching GPU/context evidence')
        for attempt in record['attempts']:
            aid = attempt['attempt_id']
            if aid in seen or aid not in attempts:
                raise ValueError('untracked or reused real generation')
            seen.add(aid)
            reservation = attempts[aid]
            if reservation['phase'] != 'development' or reservation['key'] != record['cache_key']:
                raise ValueError('campaign attempt does not belong to this request')
            evidence = read(root / 'development' / selected / 'responses' / reservation['artifact']['path'])
            if canonical_sha256(evidence) != reservation['artifact']['sha256']:
                raise ValueError('immutable raw generation evidence changed')
            if evidence['identity'] != ident or evidence['attempt']['raw'] != attempt['raw'] or evidence['attempt']['body'] != attempt['body']:
                raise ValueError('completion/raw attempt identity differs')
    return gate


def freeze(root):
    require_engineering(root)
    validate_development_gate(root)
    value = {'version': 'phase2d-r1-contract-v1', 'source': code_fingerprint(ROOT), 'git': git_state(ROOT),
        'identity': read(root / 'development-identity.json'), 'formal_live_allowed': True,
        'arithmetic_protocol': ARITHMETIC, 'request_protocol': REQUEST,
        'development': {'seeds': [10, 11], 'times': [168, 672]},
        'previously_seen': {'seeds': [20, 21, 22, 23], 'times': [168, 336, 672], 'labels': 'not supplied to selectors'},
        'diagnostics': {'seeds': [40, 41, 42, 43], 'times': [168, 336, 672], 'trajectories': 8},
        'pilot': {'seeds': [50, 51, 52, 53, 54], 'first_gate': 12,
            'method_order': 'left rotate METHODS by (seed-50 + scenario index) modulo 6'},
        'methods': METHODS, 'M': 16, 'K': 4, 'B': 8, 'planning_seed': 20261102,
        'reference_seed': 20261103, 'reference_B': 64,
        'budgets': {'development_generations': 80, 'diagnostic_generations': 48, 'pilot_generations': 200,
            'generation_seconds': 7200, 'per_generation_seconds': 120,
            'diagnostic_des': 3072, 'reference_des': 24576, 'pilot_des': 6400, 'formal_des': 34048},
        'ties': {'numeric_absolute_tolerance': 1e-12, 'mc': 'paired difference mean +/- 1.96 sample SD/8 includes zero'},
        'missing': 'preserve partial rows, no fallback/replacement/extra budget; no reference after online failure',
        'deduplication': 'exact request+pool and exact observation/context/parameters; no wrapper-only novelty',
        'statistics': 'unique identities and scenario/environment-seed trajectories; no formal p values',
        'physical_retention': 'minimum stable candidate ID reference scenario 0; selected candidate branch 0 for each pilot decision',
        'replay_ignored_fields': ['execution_audit', 'local evidence/actual counters/cache_seconds/generation_seconds/attempt timing'],
        'tokenizer': {'path': str(TOKENIZER_PATH), 'sha256': sha(TOKENIZER_PATH), 'package': 'tokenizers==0.22.2'},
        'valid_jev': False}
    immutable(root / 'freeze.json', value)
    immutable(root / 'freeze-sha256.json', {'canonical_sha256': canonical_sha256(value)})
    return {'status': 'frozen', 'source_sha256': value['source']['sha256']}


def check_freeze(root):
    frozen = read(root / 'freeze.json')
    if read(root / 'freeze-sha256.json')['canonical_sha256'] != canonical_sha256(frozen):
        raise ValueError('preregistration modified')
    if frozen['source'] != code_fingerprint(ROOT):
        raise ValueError('frozen source changed; new batch required')
    if not frozen['formal_live_allowed']:
        raise ValueError('formal stage is locked')
    return frozen


def diagnostics(root):
    frozen = check_freeze(root)
    folder = root / 'diagnostics'
    folder.mkdir(exist_ok=False)
    # Rebuild all seen PUBLIC states in the same arithmetic protocol. No labels read.
    seen_rows = read(root / 'development/states.json') + capture_states((20, 21, 22, 23), (168, 336, 672))
    save(folder / 'seen-public-states.json', seen_rows)
    ids = {r['independent_identity'] for r in seen_rows}
    businesses = {r['business_request_sha256'] for r in seen_rows}
    rows = capture_states((40, 41, 42, 43), (168, 336, 672))
    seen = set()
    for row in rows:
        row['cross_split_exact_duplicate'] = row['independent_identity'] in ids
        row['cross_split_business_duplicate'] = row['business_request_sha256'] in businesses
        row['cross_split_duplicate'] = row['cross_split_exact_duplicate'] or row['cross_split_business_duplicate']
        row['within_holdout_duplicate'] = row['independent_identity'] in seen
        seen.add(row['independent_identity'])
    save(folder / 'states.json', rows)
    online, audits = [], []
    ident = frozen['identity']
    p = provider(root, folder / 'responses', 'diagnostics')
    for row in rows:
        req, pool = unpack(row)
        state_folder = folder / row['state_id']
        state_folder.mkdir()
        ranking = p.rank(req, pool) if len(pool) > 1 else None
        if ranking:
            save(state_folder / 'shared-ranking.json', ranking)
        for method in METHODS:
            from sim_jev.branch_audit_store import BranchAuditStore
            store = BranchAuditStore(state_folder / method, audit_planning_branch)
            if method == 'mandatory-first':
                command = row['reference_command']
                selected = next((c.id for c in pool if c.command.to_dict() == command), None)
                result = {'method': method, 'command': command, 'selected_candidate_id': selected,
                    'ranking_ids': [], 'budget': {'actual_des': 0, 'simulated_hours': 0.}, 'branches': []}
            else:
                replay = provider(root, folder / 'responses', 'diagnostics', mode='cache_only') if method.startswith('local-') else None
                result = select_from_pool(req, policy(method, ident), pool,
                    DecisionRuntime(branch_auditor=store, local_provider=replay)).record
            save(state_folder / (method + '.json'), result)
            accounting = store.reconcile({'weekly_state': {'decision_records': [result] if method != 'mandatory-first' else []}})
            save(state_folder / method / 'branch-accounting.json', accounting)
            online.append({'state_id': row['state_id'], 'method': method, 'status': 'completed',
                'selected_candidate_id': result['selected_candidate_id'],
                'top_k': result['ranking_ids'][:4] if method in {'random-des', 'heuristic-des', 'local-des', 'local-direct'} else None,
                'actual_des': result['budget']['actual_des'], 'simulated_hours': result['budget']['simulated_hours'],
                'shared_ranking_seconds': ranking['generation_seconds'] if ranking and method.startswith('local-') else None,
                'cache_seconds': (result.get('local') or {}).get('cache_seconds')})
            audits.append({'state_id': row['state_id'], 'method': method,
                'des': result['budget']['actual_des'], 'simulated_hours': result['budget']['simulated_hours']})
            save(folder / 'online-actions.json', online)
            save(folder / 'online-audits.json', audits)
        print('online sealed', row['state_id'], flush=True)
    if len(online) != 144 or sum(r['des'] for r in audits) > 3072:
        raise AssertionError('online coverage/budget mismatch')
    save(folder / 'online-complete.json', {'states': len(rows), 'actions': len(online),
        'sha256': canonical_sha256(online), 'actual_des': sum(r['des'] for r in audits)})
    return {'status': 'online_completed', 'planned_states': 24, 'actual_states': len(rows)}


def reference(root):
    baseline_only=False
    check_freeze(root)
    from scripts.phase2d_r1_data_audit import verify_online
    verify_online(root)
    folder=root/('baseline-diagnostics' if baseline_only else 'diagnostics')
    ready=read(folder/'online-complete.json');online=read(folder/'online-actions.json')
    if ready['sha256']!=canonical_sha256(online): raise ValueError('online action seal mismatch')
    evaluation=folder/'evaluation';evaluation.mkdir(exist_ok=False)
    rows=read(folder/'states.json');metrics=[];candidate_rows=[];des=0;hours=0.;audited=0
    for row in rows:
        req,pool=unpack(row);scores={c.id:[] for c in pool};out=evaluation/row['state_id'];out.mkdir()
        minimum=min(scores) if scores else None
        store=BranchAuditStore(out,audit_planning_branch)
        all_branches=[]
        with (out/'branches.jsonl').open('w',encoding='utf-8') as stream:
            for index in range(64):
                scenario=PlanningScenario(PlanningInput(req,20261103,index))
                for candidate in sorted(pool,key=lambda c:c.id):
                    store.before_branch(req,candidate,index,scenario)
                    result=scenario.evaluate(candidate.command)
                    check=store(result,req);audited+=1
                    artifact=result.pop('final_state');digest=canonical_sha256(artifact)
                    all_branches.append({**deepcopy(result),'candidate_id':candidate.id,'physical_artifact_sha256':digest})
                    if candidate.id==minimum and index==0: save(out/'physical-reference-0.json',{**result,'final_state':artifact})
                    record={**result,'candidate_id':candidate.id,'physical_artifact_sha256':digest,
                            'audit':check,'purpose_namespace':'reference-v1','planning_seed':20261103}
                    stream.write(compact(record)+'\n');scores[candidate.id].append(result['score'])
                    des+=1;hours+=result['duration_hours']
        accounting=store.reconcile({'weekly_state':{'decision_records':[{'request':req.to_dict(),'branches':all_branches}]}})
        save(out/'branch-accounting.json',accounting)
        means={k:statistics.fmean(v) for k,v in scores.items()};best=max(means.values()) if means else None
        winner=min(means,key=lambda k:(-means[k],k)) if means else None
        for key,values in scores.items():
            diffs=[a-b for a,b in zip(scores[winner],values)];mean=statistics.fmean(diffs);se=statistics.stdev(diffs)/8
            candidate_rows.append({'state_id':row['state_id'],'candidate_id':key,'q_ref':means[key],
                'standard_error':statistics.stdev(values)/8,'N':64,'scores':values,
                'numerical_tie':abs(best-means[key])<=1e-12,'mc_approximate_tie':mean-1.96*se<=0<=mean+1.96*se,
                'paired_difference_mean':mean,'paired_difference_se':se,'valid':True})
        for action in [a for a in online if a['state_id']==row['state_id']]:
            top=action.get('top_k');selected=action.get('selected_candidate_id')
            retained_best=min(top,key=lambda k:(-means[k],k)) if top and means else None
            action_se=statistics.stdev([a-b for a,b in zip(scores[winner],scores[selected])])/8 if selected in scores else None
            retained_se=statistics.stdev([a-b for a,b in zip(scores[winner],scores[retained_best])])/8 if retained_best else None
            metrics.append({**{k:row[k] for k in ('state_id','scenario','seed','window_start','window_end','actual_M','independent_identity','cross_split_duplicate','within_holdout_duplicate')},
                **action,'best_reference_score':best,'selected_reference_score':means.get(selected),
                'action_regret':best-means[selected] if selected in means else None,
                'regret_at_k':best-max(means[k] for k in top) if top and means else None,
                'action_regret_paired_se':action_se,'regret_at_k_paired_se':retained_se,
                'best_candidate_recall':any(abs(best-means[k])<=1e-12 for k in top) if top and means else None,
                'reference_des':len(pool)*64})
        write_csv(evaluation/'per-state.csv',metrics);save(evaluation/'per-state.json',metrics)
        write_csv(evaluation/'per-candidate-reference.csv',candidate_rows)
        print('reference audited',row['state_id'],'cumulative DES',des,flush=True)
    summaries=[]
    for scope in ('unique_holdout','all_planned_trajectories','cross_split_duplicates'):
        for scenario in ('balanced','scarce'):
            for method in METHODS:
                group=[r for r in metrics if r['scenario']==scenario and r['method']==method and r['status']=='completed' and
                       (scope=='all_planned_trajectories' or (scope=='cross_split_duplicates' and r['cross_split_duplicate']) or
                        (scope=='unique_holdout' and not r['cross_split_duplicate'] and not r['within_holdout_duplicate']))]
                aggregate={'scope':scope,'scenario':scenario,'method':method,'states':len(group),'trajectories':len({x['seed'] for x in group})}
                for k in ('action_regret','regret_at_k','best_candidate_recall'):
                    values=[r[k] for r in group if r[k] is not None];aggregate[k]=statistics.fmean(values) if values else None
                summaries.append(aggregate)
    write_csv(evaluation/'by-scenario.csv',summaries);save(evaluation/'by-scenario.json',summaries)
    status={'status':'passed','actual_states':len(rows),'actual_reference_des':des,'reference_simulated_hours':hours,
            'independent_branch_audits':audited,'online_des':ready['actual_des'],
            'cross_split_duplicate_states':sum(r['cross_split_duplicate'] for r in rows),
            'unique_holdout_states':sum(not r['cross_split_duplicate'] and not r['within_holdout_duplicate'] for r in rows),
            'local_live_diagnostics_completed':not baseline_only,'MC_warning':'finite-pool short-window estimates; max selection bias; MC ties are descriptive, not simultaneous confidence sets'}
    if des>24576 or ready['actual_des']>3072 or des!=audited: raise AssertionError('diagnostic budget/audit failure')
    save(folder/'status.json',status);return status


def pilot(root):
    frozen = check_freeze(root)
    if read(root / 'diagnostics/status.json')['status'] != 'passed' or read(root / 'data-audit.json')['status'] != 'passed':
        raise ValueError('diagnostic completion and independent accounting required')
    from scripts.phase2d_local import run_pilot_case
    from scripts.phase2d_r1_data_audit import verify
    folder = root / 'pilot'
    folder.mkdir(exist_ok=True)
    rows, details, pairings = [], [], {}
    ident = frozen['identity']
    for seed in (50, 51, 52, 53, 54):
        for si, name in enumerate(('balanced', 'scarce')):
            offset = (seed - 50 + si) % len(METHODS)
            for method in METHODS[offset:] + METHODS[:offset]:
                run = folder / f'{name}-{seed}-{method}'
                local = provider(root, run / 'responses', 'pilot') if method.startswith('local-') and not (run / 'result.json').exists() else None
                row, detail = run_pilot_case(run, name, seed, method, ident,
                    local_provider=local, build_case_fn=build_case, policy_fn=policy,
                    experiment_context=frozen)
                if pairings.setdefault((name, seed), row['paired_task_rule_sha256']) != row['paired_task_rule_sha256']:
                    raise AssertionError('paired exogenous inputs differ')
                rows.append(row)
                details.extend(detail)
                save(folder / 'per-seed.json', rows)
                save(folder / 'per-decision.json', details)
                write_csv(folder / 'per-seed.csv', rows)
                write_csv(folder / 'per-decision.csv', details)
                # Independent full-log audit already ran in run_pilot_case; the
                # durable branch identities are additionally reconciled here.
                _, counts = BranchAuditStore(run, audit_planning_branch).verify_checkpoint(run / 'checkpoint.json')
                if counts['logical_branches'] != row['actual_des']:
                    raise AssertionError('run branch accounting differs')
                print('pilot', len(rows), '/60', name, seed, method, flush=True)
        if seed == 50:
            audit = verify(root)
            if audit['status'] != 'passed' or len(rows) != 12 or len(details) != 60:
                raise AssertionError('first 12 runs failed independent gate')
            save(folder / 'first-12-gate.json', {'status': 'passed', 'runs': len(rows), 'decisions': len(details),
                'audit_sha256': canonical_sha256(audit)})
    aggregates = []
    for name in ('balanced', 'scarce'):
        for method in METHODS:
            group = [r for r in rows if r['scenario'] == name and r['method'] == method]
            totals = {key: math.fsum(r[key] for r in group) if all(r[key] is not None for r in group) else None for key in (
                'offered', 'accepted', 'success', 'offered_value', 'earned_value', 'mandatory_shortfall_terminal',
                'flight_hours', 'available_aircraft_hours', 'fleet_horizon_hours', 'spare_wait_hours', 'bay_wait_hours',
                'actual_des', 'simulated_hours', 'local_generations', 'input_tokens', 'output_tokens',
                'local_seconds', 'end_to_end_seconds')}
            aggregates.append({'scenario': name, 'method': method, 'runs': len(group), **totals,
                **{key: totals[n] / totals[d] if totals[d] else None for key, n, d in (
                    ('DSR', 'success', 'offered'), ('RCR', 'earned_value', 'offered_value'),
                    ('ASR', 'success', 'accepted'), ('availability', 'available_aircraft_hours', 'fleet_horizon_hours'))}})
    save(folder / 'by-scenario.json', aggregates)
    write_csv(folder / 'by-scenario.csv', aggregates)
    status = {'status': 'passed', 'runs': len(rows), 'decisions': len(details),
        'actual_des': sum(r['actual_des'] for r in rows),
        'local_generations': sum(r['local_generations'] for r in rows),
        'local_pilot_completed': len(rows) == 60, 'remote_typesafe_requests': 0}
    if status['actual_des'] > 6400 or len(details) != 300:
        raise AssertionError('pilot count/budget mismatch')
    save(folder / 'status.json', status)
    verify(root)
    return status


def replay(root):
    """Fresh 720h runs with all five decisions, exact outputs, and forbidden sockets."""
    import socket
    from sim_jev.decision_resolver import logical_decision_record
    from sim_jev.weekly_reporting import write_weekly_run
    from scripts.weekly_verify import normalized_history, audit_business_log
    from scripts.phase2c_science_regression import SCIENCE_FILES
    frozen = read(root / 'freeze.json')
    if read(root / 'pilot/status.json')['status'] != 'passed':
        raise ValueError('live pilot is incomplete')
    folder = root / 'cache-only-replay'
    folder.mkdir(exist_ok=False)
    results, extra_des, extra_hours = [], 0, 0.
    with patch.object(socket, 'socket', side_effect=AssertionError('network forbidden in cache-only replay')):
        for run in sorted((root / 'pilot').iterdir()):
            if not run.is_dir() or not (run / 'result.json').exists():
                continue
            row = read(run / 'result.json')
            expected = WeeklySimulation.from_checkpoint(read(run / 'checkpoint.json'))
            destination = folder / run.name
            journal = BranchAuditStore(destination, audit_planning_branch)
            local = provider(root, run / 'responses', 'pilot', mode='cache_only') if row['method'].startswith('local-') else None
            actual = build_case(row['scenario'], row['seed'], policy(row['method'], frozen['identity']),
                DecisionRuntime(branch_auditor=journal, local_provider=local))
            actual.run(720)
            audit_business_log(actual)
            left, right = expected.to_checkpoint(), actual.to_checkpoint()
            for value, sim in ((left, expected), (right, actual)):
                value['history'] = normalized_history(sim)
                if 'decision_records' in value['weekly_state']:
                    value['weekly_state']['decision_records'] = [logical_decision_record(x) for x in value['weekly_state']['decision_records']]
            if left != right or expected.summary() != actual.summary() or expected.events != actual.events:
                raise AssertionError('exact full checkpoint/business replay differs: ' + run.name)
            counts = journal.reconcile(actual.to_checkpoint())
            save(destination / 'branch-accounting.json', counts)
            write_weekly_run(actual, destination / 'science')
            compared = []
            for name in SCIENCE_FILES:
                if (run / 'science' / name).read_bytes() != (destination / 'science' / name).read_bytes():
                    raise AssertionError('scientific bytes differ: ' + run.name + '/' + name)
                compared.append(name)
            extra_des += counts['logical_branches']
            records = actual.weekly_state.get('decision_records', [])
            extra_hours += sum(r['budget']['simulated_hours'] for r in records)
            results.append({'run': run.name, 'passed': True, 'new_generations': local.generations if local else 0,
                'metadata_queries': local.metadata_queries if local else 0, 'science_files_byte_identical': compared,
                'all_five_decisions_replayed': True, **counts})
            save(folder / 'results.json', results)
            print('cache-only replay', len(results), run.name, flush=True)
    status = {'status': 'passed' if len(results) == 60 else 'partial', 'runs': len(results),
        'cache_only_replay_verified': len(results) == 60, 'new_generations': 0, 'metadata_queries': 0,
        'extra_audit_des': extra_des, 'extra_audit_simulated_hours': extra_hours, 'network_disabled_in_process': True,
        'budget_role': 'additional audit replay; excluded from formal 34048 DES'}
    save(folder / 'status.json', status)
    return status


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['development', 'smoke', 'freeze', 'diagnostics', 'reference', 'pilot', 'replay'])
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    receipt = {'argv': sys.orig_argv, 'cwd': str(Path.cwd()), 'source_sha256': code_fingerprint(ROOT)['sha256']}
    try:
        result = globals()[args.action](root)
        receipt.update(exit_code=0, result=result)
    except Exception as error:
        import traceback
        receipt.update(exit_code=1, error=f'{type(error).__name__}: {error}', traceback=traceback.format_exc())
        save(root / args.action / f'failure-{time.time_ns()}.json', {'status': 'partial',
            'error': receipt['error'], 'new_budget_added': False})
        print(receipt['traceback'], file=sys.stderr)
    receipt['seconds'] = time.perf_counter() - started
    save(root / 'commands' / f'{args.action}-{time.time_ns()}.json', receipt)
    print(json.dumps({k: v for k, v in receipt.items() if k != 'traceback'}, indent=2))
    return receipt['exit_code']


if __name__ == '__main__':
    raise SystemExit(main())
