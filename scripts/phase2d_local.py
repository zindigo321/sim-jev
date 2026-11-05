"""Explicit phase-2D development, frozen diagnostics, pilot and zero-inference replay."""
from __future__ import annotations
import argparse
from copy import deepcopy
from dataclasses import asdict, replace
import hashlib
import json
import math
from pathlib import Path
import statistics
import subprocess
import sys
import time
import uuid
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
from sim_jev.candidates import Candidate,generate_candidates
from sim_jev.decision import DecisionRequest,DecisionResult,DecisionPolicySpec
from sim_jev.decision_resolver import DecisionRuntime,select_from_pool,logical_decision_record
from sim_jev.local_policy import LocalDecisionPolicySpec
from sim_jev.local_provider import (LocalProvider,LocalError,GenerationBudget,identity,render,schema,
                                    validate_ranking,MODEL,compact)
from sim_jev.jev import _atomic_json
from sim_jev.branch_audit_store import BranchAuditStore, save_checkpoint
from sim_jev.planning import PlanningInput,PlanningScenario
from sim_jev.provenance import code_fingerprint,canonical_sha256,git_state
from sim_jev.run_identity import requested_run_identity, preflight_run_identity
from sim_jev.weekly import WeeklySimulation,WEEK_POINTS
from sim_jev.weekly_policy import PolicySpec,WeeklyCommand,choose_command
from sim_jev.weekly_reporting import write_weekly_run
from scripts.phase2c_experiment import build_case,write_csv,decision_rows
from scripts.planning_verify import audit_planning_branch
from scripts.weekly_verify import audit_business_log,compare_values,normalized_history
from scripts.phase2d_runtime import api,sha

METHODS=('mandatory-first','random-des','heuristic-des','local-direct','local-des','direct-des')
TOKENIZER_PYTHON=Path('G:/sim-jev-runtime/tokenizer-env/Scripts/python.exe')
TOKENIZER_PATH=Path('G:/sim-jev-models/tokenizer/cdbee75f17c01a7cc42f958dc650907174af0554/tokenizer.json')
TOKENIZER_CODE='import json,sys; from tokenizers import Tokenizer; t=Tokenizer.from_file(sys.argv[1]); print(len(t.encode(json.load(sys.stdin),add_special_tokens=False).ids))'

def read(path): return json.loads(Path(path).read_text(encoding='utf-8'))
def save(path,value):
    if isinstance(value,dict) and value.get('checkpoint_type')=='InternalCheckpoint':
        save_checkpoint(path,value)
    else:
        _atomic_json(Path(path),value)

def compare_replay(expected,actual):
    if expected.events!=actual.events or normalized_history(expected)!=normalized_history(actual):
        raise AssertionError('complete ordered execution log/history differs')
    left,right=expected.to_checkpoint(),actual.to_checkpoint()
    for value,sim in ((left,expected),(right,actual)):
        value['history']=normalized_history(sim)
        state=value.get('weekly_state')
        if state is not None and 'decision_records' in state:
            state['decision_records']=[logical_decision_record(x) for x in state['decision_records']]
    return compare_values(left,right,'checkpoint')+compare_values(expected.summary(),actual.summary(),'summary')

def count_tokens(prompt):
    r=subprocess.run([str(TOKENIZER_PYTHON),'-c',TOKENIZER_CODE,str(TOKENIZER_PATH)],input=json.dumps(prompt),
                     capture_output=True,text=True,encoding='utf-8',timeout=30,check=True)
    return int(r.stdout.strip())

def capture_states(seeds,times):
    rows=[]
    for name in ('balanced','scarce'):
        for seed in seeds:
            captured=[]
            def capture(req,policy,runtime=None):
                if req.context.window_start in times: captured.append(req)
                return DecisionResult(choose_command(req.observation,req.context,policy))
            with patch('sim_jev.weekly.resolve_decision',side_effect=capture):
                build_case(name,seed,PolicySpec()).run(max(times))
            for req in captured:
                pool=generate_candidates(req,16,20261102)
                row={'state_id':f'{name}-{seed}-{int(req.context.window_start)}','scenario':name,'seed':seed,
                     'window_start':req.context.window_start,'window_end':req.context.window_end,
                     'request':req.to_dict(),'pool':[c.to_dict() for c in sorted(pool,key=lambda x:x.id)],
                     'canonical_request_sha256':canonical_sha256(req.to_dict()),'actual_M':len(pool),
                     'reference_command':choose_command(req.observation,req.context,PolicySpec()).to_dict()}
                row['pool_sha256']=canonical_sha256(row['pool'])
                row['independent_identity']=canonical_sha256([row['canonical_request_sha256'],row['pool_sha256']])
                rows.append(row)
    return rows

def unpack(row):
    return (DecisionRequest.from_dict(row['request']),
            [Candidate(c['id'],WeeklyCommand.from_dict(c['command']),c['summary']) for c in row['pool']])

def policy(method,ident):
    if method=='mandatory-first': return PolicySpec()
    if method.startswith('local-'): return LocalDecisionPolicySpec(method=method,identity=ident)
    return DecisionPolicySpec(method=method)

def provider(root,store,phase,*,mode='live'):
    ident=read(root/'freeze.json')['identity'] if (root/'freeze.json').exists() else read(root/'development-identity.json')
    return LocalProvider(ident,store,mode=mode,budget=GenerationBudget(root/'generation-budget.json'),phase=phase,
                         token_counter=count_tokens if mode=='live' else None)

def check_freeze(root):
    freeze=read(root/'freeze.json')
    if freeze['source']!=code_fingerprint(ROOT): raise ValueError('frozen source changed: create a new batch')
    return freeze

def development(root):
    if (root/'freeze.json').exists():raise ValueError('development is closed after freeze')
    folder=root/'development';folder.mkdir(parents=True,exist_ok=True)
    rows=capture_states((10,11),(168,672))
    if (folder/'states.json').exists():
        prior=read(folder/'states.json')
        if [x['independent_identity'] for x in prior]!=[x['independent_identity'] for x in rows]:
            raise ValueError('development state identity changed')
    save(folder/'states.json',rows)
    tokens=[]
    for row in rows:
        req,pool=unpack(row); rendered=render(req,pool)
        n=count_tokens(rendered['prompt']);tokens.append(n)
        save(folder/(row['state_id']+'-input.json'),rendered)
        row['prompt_tokens']=n
    save(folder/'states.json',rows)
    ctx=8192 if max(tokens)+1024<=8192 else 16384
    ident=identity(read(root/'hardware/model-manifest.json'),num_ctx=ctx)
    save(root/'development-identity.json',ident)
    result={'status':'prepared','states':len(rows),'prompt_token_counts':tokens,'num_ctx':ctx,
            'fits_context':max(tokens)+1024<=ctx,'templates_compared':2,'batch_size':1,'concurrency':1}
    save(folder/'preflight.json',result)
    if not result['fits_context']: raise LocalError('development_input_exceeds_16384',result)
    return result

def smoke(root):
    if (root/'freeze.json').exists():raise ValueError('smoke configuration closed after freeze')
    folder=root/'smoke';folder.mkdir(parents=True,exist_ok=True)
    ident=read(root/'development-identity.json');budget=GenerationBudget(root/'generation-budget.json')
    tiny=read(folder/'tiny.json') if (folder/'tiny.json').exists() else []
    for m in (2,4,8,16):
        if any(x['M']==m for x in tiny):continue
        ids=[f'C{i:02d}' for i in range(m)]
        prompt='<|im_start|>user\nRank these candidate IDs in ascending order. Return only JSON ranked_ids: '+compact(ids)+'<|im_end|>\n<|im_start|>assistant\n'
        body={'model':MODEL,'prompt':prompt,'raw':True,'stream':False,'format':schema(ids),
              'options':ident['decoding'],'keep_alive':'24h','truncate':False,'shift':False}
        reservation,timeout=budget.reserve('development',f'tiny-{m}');timer=time.perf_counter()
        try: raw=api('http://127.0.0.1:11439','/api/generate',body,timeout)
        except Exception:
            budget.finish(reservation,time.perf_counter()-timer,'uncertain');raise
        elapsed=time.perf_counter()-timer;budget.finish(reservation,elapsed,'completed')
        row={'M':m,'body':body,'raw':raw,'seconds':elapsed,'independent_prompt_tokens':count_tokens(prompt)}
        tiny.append(row);save(folder/'tiny.json',tiny)
        validate_ranking(raw['response'],ids)
        if raw['prompt_eval_count']!=row['independent_prompt_tokens']: raise LocalError('tiny_token_count_mismatch',row)
        print(f'tiny M={m}: {elapsed:.2f}s',flush=True)
    rows=read(root/'development/states.json');completed=read(folder/'completions.json') if (folder/'completions.json').exists() else []
    for c in completed:
        if read(folder/f'completion-{c["index"]:02d}.json')['identity']!=ident:raise ValueError('smoke identity changed; new batch required')
    p=provider(root,folder/'responses','development')
    failures=read(folder/'failed-logical-requests.json') if (folder/'failed-logical-requests.json').exists() else []
    logical=len(completed)+len(failures)
    while len(completed)<20:
        row=rows[logical%len(rows)]; req,pool=unpack(row);logical+=1
        try:result=p.rank(req,pool,force=True)
        except LocalError as exc:
            failures.append({'logical_index':logical-1,'state_id':row['state_id'],'code':exc.code,'audit':exc.audit})
            save(folder/'failed-logical-requests.json',failures)
            if exc.code!='invalid_local_ranking':raise
            print('format failed after fixed correction',row['state_id'],flush=True)
            continue
        i=len(completed);save(folder/f'completion-{i:02d}.json',result)
        meta=api('http://127.0.0.1:11439','/api/ps')
        loaded=next(x for x in meta['models'] if x['name']==MODEL)
        if loaded['size_vram']<=0 or loaded['context_length']!=ident['decoding']['num_ctx']:
            raise LocalError('gpu_or_context_gate_failed')
        completed.append({'index':i,'state_id':row['state_id'],'request_identity':row['independent_identity'],
                          'repeat_input':any(x['state_id']==row['state_id'] for x in completed),'actual_M':len(pool),'seconds':result['generation_seconds'],
                          'generations':result['local_generation_attempts'],'first_round_valid':result['first_round_valid'],
                          'ps':meta,'prompt_tokens':result['attempts'][-1]['raw']['prompt_eval_count']})
        save(folder/'completions.json',completed)
        print(f'stability {i+1}/20 {row["state_id"]}: {result["generation_seconds"]:.2f}s',flush=True)
    status={'status':'passed','model_load_verified':True,'tiny_inference_verified':True,'full_request_verified':True,
            'valid_completions':20,'actual_generations':sum(x['generations'] for x in completed),
            'first_round_format_success_rate':sum(x['first_round_valid'] for x in completed)/20,
            'corrections':sum(x['generations']-1 for x in completed)+len(failures),'failures':len(failures),
            'unique_business_inputs':len({x['request_identity'] for x in completed}),
            'max_actual_M':max(x['actual_M'] for x in completed),'tiny_generations':4,
            'local_metadata_queries':p.metadata_queries+20,'remote_typesafe_requests':0}
    status['all_development_states_valid']=len({x['state_id'] for x in completed})==8
    # Include earlier failed CLI invocations in this same final configuration.
    # Each failed format request uses both permitted attempts; no transport
    # uncertainty is allowed to reach this completed gate calculation.
    keys=set()
    for row in rows:
        req,pool=unpack(row);rendered=render(req,pool)
        keys.add(canonical_sha256({'identity':ident,'rendered':rendered}))
    actual=[x for x in budget.read()['attempts'] if x['key'] in keys]
    failed_attempts=len(actual)-status['actual_generations']
    if failed_attempts%2 or any(x['status']!='completed' for x in actual):raise ValueError('smoke attempt reconciliation failed')
    status.update(actual_generations_including_failures=len(actual),failures=failed_attempts//2,
                  corrections=sum(x['generations']-1 for x in completed)+failed_attempts//2,
                  correction_successes=sum(not x['first_round_valid'] for x in completed),
                  first_round_format_success_rate=sum(x['first_round_valid'] for x in completed)/(20+failed_attempts//2),
                  local_metadata_queries=(20+failed_attempts//2)*2+20)
    status['status']='passed' if status['all_development_states_valid'] else 'partial'
    save(folder/'status.json',status);return status

def freeze(root,*,offline_only=False):
    if (root/'freeze.json').exists(): raise ValueError('freeze is immutable')
    gate=read(root/'smoke/status.json')['status']=='passed'
    if not gate and not offline_only: raise ValueError('hardware/protocol coverage gate not passed')
    value={'version':'phase2d-contract-v1','identity':read(root/'development-identity.json'),
           'formal_live_allowed':gate and not offline_only,'offline_only':offline_only,
           'source':code_fingerprint(ROOT),'git':git_state(ROOT),'created_unix':time.time(),
           'development':{'seeds':[10,11],'times':[168,672]},'diagnostics':{'seeds':[20,21,22,23],'times':[168,336,672]},
           'pilot':{'seeds':[30,31,32,33,34],'method_order':'left rotate METHODS by (seed-30 + scenario index) modulo 6'},
           'methods':METHODS,'M':16,'K':4,'B':8,'planning_seed':20261102,'reference_seed':20261103,'reference_B':64,
           'random_namespaces':{'online':'planning/seed20261102','reference':'planning/seed20261103','execution':'execution/entity-CRN'},
           'budgets':{'development_generations':80,'diagnostic_generations':48,'pilot_generations':200,
                      'generation_seconds':7200,'diagnostic_des':3072,'reference_des':24576,'pilot_des':6400,'formal_des':34048},
           'ties':{'numeric_absolute_tolerance':1e-12,'mc':'paired difference mean +/- 1.96 sample SD/sqrt(64) includes zero'},
           'missing':'preserve missing rows; no replacement states or fallback; cross-split duplicates excluded from unique holdout',
           'physical_retention':'fixed state minimum stable candidate ID reference scenario 0; pilot branch 0 each decision',
           'replay_ignored_fields':['execution_audit','local evidence/actual counters/cache_seconds/generation_seconds/attempt timing'],
           'tokenizer':{'path':str(TOKENIZER_PATH),'sha256':sha(TOKENIZER_PATH),'package':'tokenizers==0.22.2'},
           'cloud_service_fee':0,'jev_live_smoke_verified':False,'jev_live_pilot_completed':False}
    save(root/'freeze.json',value);return {'status':'frozen','source':value['source']['sha256']}

def branch_auditor(folder,*,retain_first=False):
    counts={'des':0,'simulated_hours':0.,'audit_seconds':0.}
    def audit(result,req):
        t=time.perf_counter(); checked=audit_planning_branch(result,req)
        counts['audit_seconds']+=time.perf_counter()-t
        if retain_first and counts['des']==0: save(folder/'branch-0.json',result)
        counts['des']+=1;counts['simulated_hours']+=result['duration_hours']
        return checked
    return audit,counts

def diagnostics(root,*,baseline_only=False):
    frozen=check_freeze(root)
    if not baseline_only and not frozen['formal_live_allowed']:raise ValueError('formal local diagnostics gate not passed')
    folder=root/('baseline-diagnostics' if baseline_only else 'diagnostics');folder.mkdir(parents=True,exist_ok=False)
    rows=capture_states((20,21,22,23),(168,336,672))
    development_ids={r['independent_identity'] for r in read(root/'development/states.json')}
    seen=set()
    for row in rows:
        row['cross_split_duplicate']=row['independent_identity'] in development_ids
        row['within_holdout_duplicate']=row['independent_identity'] in seen;seen.add(row['independent_identity'])
    save(folder/'states.json',rows)
    online=[];audits=[]
    ident=read(root/'development-identity.json') if baseline_only else read(root/'freeze.json')['identity']
    p=None if baseline_only else provider(root,folder/'responses','diagnostics')
    for row in rows:
        req,pool=unpack(row); state_folder=folder/row['state_id'];state_folder.mkdir()
        # Persist the captured ranking before any selection/reference evaluation.
        ranking=None if p is None else p.rank(req,pool)
        if ranking: save(state_folder/'shared-ranking.json',ranking)
        for method in METHODS:
            if baseline_only and method.startswith('local-'):
                online.append({'state_id':row['state_id'],'method':method,'status':'not_run','reason':'hardware_gate_not_passed'});continue
            audit,count=branch_auditor(state_folder)
            if method=='mandatory-first':
                command=row['reference_command']
                selected=next((c.id for c in pool if c.command.to_dict()==command),None)
                result={'method':method,'command':command,'selected_candidate_id':selected,'ranking_ids':[],
                        'budget':{'actual_des':0,'simulated_hours':0.},'branches':[]}
            else:
                replay=None if p is None else LocalProvider(ident,folder/'responses',mode='cache_only')
                result=select_from_pool(req,policy(method,ident),pool,DecisionRuntime(branch_auditor=audit,local_provider=replay)).record
            save(state_folder/(method+'.json'),result)
            if count['des']!=result['budget']['actual_des']: raise AssertionError('online audit count mismatch')
            online.append({'state_id':row['state_id'],'method':method,'status':'completed',
                           'selected_candidate_id':result['selected_candidate_id'],
                           'top_k':result['ranking_ids'][:4] if method in {'random-des','heuristic-des','local-des'} else None,
                           'actual_des':count['des'],'simulated_hours':count['simulated_hours'],
                           'shared_ranking_seconds':ranking['generation_seconds'] if ranking and method.startswith('local-') else None,
                           'cache_seconds':(result.get('local') or {}).get('cache_seconds')})
            audits.append({'state_id':row['state_id'],'method':method,**count})
        save(folder/'online-actions.json',online);save(folder/'online-audits.json',audits)
        print('online persisted',row['state_id'],flush=True)
    save(folder/'online-complete.json',{'states':len(rows),'actions':len(online),'sha256':canonical_sha256(online),
                                     'actual_des':sum(x['des'] for x in audits)})
    return {'status':'online_completed','planned_states':24,'actual_states':len(rows)}

def reference(root,*,baseline_only=False):
    check_freeze(root)
    folder=root/('baseline-diagnostics' if baseline_only else 'diagnostics')
    ready=read(folder/'online-complete.json');online=read(folder/'online-actions.json')
    if ready['sha256']!=canonical_sha256(online): raise ValueError('online action seal mismatch')
    evaluation=folder/'evaluation';evaluation.mkdir(exist_ok=False)
    rows=read(folder/'states.json');metrics=[];candidate_rows=[];des=0;hours=0.;audited=0
    for row in rows:
        req,pool=unpack(row);scores={c.id:[] for c in pool};out=evaluation/row['state_id'];out.mkdir()
        minimum=min(scores) if scores else None
        with (out/'branches.jsonl').open('w',encoding='utf-8') as stream:
            for index in range(64):
                scenario=PlanningScenario(PlanningInput(req,20261103,index))
                for candidate in sorted(pool,key=lambda c:c.id):
                    result=scenario.evaluate(candidate.command)
                    check=audit_planning_branch(result,req);audited+=1
                    artifact=result.pop('final_state');digest=canonical_sha256(artifact)
                    if candidate.id==minimum and index==0: save(out/'physical-reference-0.json',{**result,'final_state':artifact})
                    record={**result,'candidate_id':candidate.id,'physical_artifact_sha256':digest,
                            'audit':check,'purpose_namespace':'reference-v1','planning_seed':20261103}
                    stream.write(compact(record)+'\n');scores[candidate.id].append(result['score'])
                    des+=1;hours+=result['duration_hours']
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

def run_pilot_case(run,name,seed,method,ident,**kwargs):
    """Persist invocation timing independently from logical branches/attempts."""
    run=Path(run)
    # This gate precedes mkdir, journal repair, execution-attempt accounting,
    # model reservation, and all forward execution/output writes.
    context=kwargs.pop('experiment_context',None)
    expected=requested_run_identity(name,seed,method,ident,
        build_case_fn=kwargs.get('build_case_fn',build_case),
        policy_fn=kwargs.get('policy_fn',policy),source_root=ROOT,
        experiment_context=context,local_provider=kwargs.get('local_provider'))
    existing,completed=preflight_run_identity(run,expected)
    if completed:
        return _run_pilot_case(run,name,seed,method,ident,**kwargs)
    run.mkdir(parents=True,exist_ok=True)
    if existing is None:
        save(run/'run-identity.json',expected)
    started=time.perf_counter();invocation_id=uuid.uuid4().hex
    path=run/'execution-attempts'/(invocation_id+'.json')
    invocation={'invocation_id':invocation_id,'status':'pending','seconds':None,
                'role':'forward_execution'}
    save(path,invocation)
    try:
        row,detail=_run_pilot_case(run,name,seed,method,ident,**kwargs)
    except BaseException as error:
        invocation.update(status='failed',seconds=time.perf_counter()-started,error_type=type(error).__name__)
        save(path,invocation)
        raise
    invocation.update(status='completed',seconds=time.perf_counter()-started)
    save(path,invocation)
    if row['status']=='completed':
        runs=[read(p) for p in (run/'execution-attempts').glob('*.json')]
        forward=[r for r in runs if r['role']=='forward_execution']
        unknown=sum(r['seconds'] is None for r in forward)
        known=sum(r['seconds'] for r in forward if r['seconds'] is not None)
        accounting=read(run/'branch-accounting.json')
        row.update(end_to_end_seconds=None if unknown else known,known_end_to_end_seconds=known,
                   uncertain_timing_invocations=unknown,des_seconds=accounting['compute_seconds'],
                   independent_audit_seconds=accounting['audit_seconds'])
        save(run/'result.json',row)
    return row,detail


def _run_pilot_case(run,name,seed,method,ident,*,local_provider=None,build_case_fn=build_case,
                   policy_fn=policy,until=720,fault_hook=None):
    """One resumable run; durable evidence precedes every checkpoint commit.

    An offline recovery CLI uses this exact writer/resolver/persistence path.
    The caller is responsible for experiment stage gates and seed registration.
    """
    run=Path(run);run.mkdir(parents=True,exist_ok=True)
    journal=BranchAuditStore(run,audit_planning_branch,fault_hook=fault_hook,
                           read_only=(run/'result.json').exists())
    if (run/'result.json').exists():
        checkpoint,accounting=journal.verify_checkpoint(run/'checkpoint.json')
        row=read(run/'result.json');detail=read(run/'per-decision.json')
        if checkpoint['time']!=720 or row['actual_des']!=accounting['logical_branches']:
            raise ValueError('existing result/checkpoint/audit reconciliation failed')
        sim=WeeklySimulation.from_checkpoint(checkpoint)
        audit_business_log(sim)
        metrics=sim.summary()
        scientific_keys=('DSR','RCR','ASR','offered','accepted','success','offered_value','earned_value',
                         'mandatory_shortfall_terminal','availability','spare_wait_hours','bay_wait_hours',
                         'available_aircraft_hours','on_hand','in_transit','flight_hours',
                         'repairs_started','repairs_completed')
        if any(row[key]!=metrics[key] for key in scientific_keys):
            raise ValueError('existing result/business summary mismatch')
        if row['scenario']!=name or row['seed']!=seed or row['method']!=method:
            raise ValueError('existing result belongs to a different requested run')
        if sim.config.seed!=seed or sim.weekly_state['policy_spec']!=policy_fn(method,ident).to_dict():
            raise ValueError('checkpoint belongs to a different seed or policy')
        expected=decision_rows(sim,name,seed,method)
        if len(expected)!=len(detail) or any(any(row[k]!=old[k] for k in row) for row,old in zip(expected,detail)):
            raise ValueError('existing per-decision rows differ from checkpoint')
        return row,detail
    timer=time.perf_counter()
    runtime=DecisionRuntime(branch_auditor=journal,local_provider=local_provider)
    if (run/'checkpoint.json').exists():
        checkpoint,accounting=journal.verify_checkpoint(run/'checkpoint.json')
        sim=WeeklySimulation.from_checkpoint(checkpoint);sim._decision_runtime=runtime
    else:
        # A crash before the first checkpoint cannot be resumed by recomputing.
        journal.reconcile({'weekly_state':{'decision_records':[]}})
        sim=build_case_fn(name,seed,policy_fn(method,ident),runtime)
    exogenous={'missions':[asdict(x) for x in sim._input_missions],'rules':sim.weekly_state['rules_input']}
    paired=canonical_sha256(exogenous)
    if (run/'exogenous.json').exists() and read(run/'exogenous.json')!=exogenous:
        raise ValueError('resumed exogenous inputs differ')
    save(run/'exogenous.json',exogenous)
    for point in WEEK_POINTS:
        if sim.now<=point<=until:
            sim.run(point);journal.commit_checkpoint(run/'checkpoint.json',sim.to_checkpoint())
            if point==0 and not (run/'resume-point.json').exists():
                save(run/'resume-point.json',sim.to_checkpoint())
    if sim.now<until:
        sim.run(until);journal.commit_checkpoint(run/'checkpoint.json',sim.to_checkpoint())
    if until<720:
        return {'status':'paused','time':sim.now,**journal.reconcile(sim.to_checkpoint())},[]
    business=audit_business_log(sim);save(run/'business-audit.json',business)
    write_weekly_run(sim,run/'science')
    records=sim.weekly_state.get('decision_records',[]);save(run/'decisions.json',records)
    detail=decision_rows(sim,name,seed,method)
    for d in detail:
        record=next((r for r in records if r['command']['window_start']==d['window_start']),{})
        local=record.get('local') or {}
        d.update(local_evidence=local.get('evidence'),valid_local_model=local.get('valid_local_model',False),
                 local_generations=local.get('local_generation_attempts',0),local_metadata_queries=local.get('local_metadata_queries',0),
                 input_tokens=local.get('actual_input_tokens',0),output_tokens=local.get('actual_output_tokens',0),
                 local_seconds=local.get('generation_seconds',0),cache_seconds=local.get('cache_seconds',0),
                 corrections=max(0,local.get('local_generation_attempts',0)-1),failures=0)
    metrics=sim.summary()
    row={'scenario':name,'seed':seed,'method':method,'status':'completed','paired_task_rule_sha256':paired,
         **{k:metrics[k] for k in ('DSR','RCR','ASR','offered','accepted','success','offered_value','earned_value',
            'mandatory_shortfall_terminal','availability','spare_wait_hours','bay_wait_hours','available_aircraft_hours',
            'on_hand','in_transit','flight_hours','repairs_started','repairs_completed')},
         'fleet_horizon_hours':sim.config.fleet_size*720,'decisions':len(detail),
         'actual_des':sum(d['actual_des'] for d in detail),'simulated_hours':sum(d['simulated_hours'] for d in detail),
         'local_generations':sum(d['local_generations'] for d in detail),'input_tokens':sum(d['input_tokens'] for d in detail),
         'output_tokens':sum(d['output_tokens'] for d in detail),'local_seconds':sum(d['local_seconds'] for d in detail),
         'corrections':sum(d['corrections'] for d in detail),'failures':0,'remote_typesafe_requests':0,
         'end_to_end_seconds':time.perf_counter()-timer}
    save(run/'per-decision.json',detail);save(run/'result.json',row)
    return row,detail


def pilot(root,*,mode='live'):
    freeze_data=check_freeze(root)
    if not freeze_data['formal_live_allowed']:raise ValueError('formal local pilot gate not passed')
    if read(root/'diagnostics/status.json')['status']!='passed': raise ValueError('diagnostics gate not passed')
    if read(root/'data-audit.json')['status']!='passed': raise ValueError('independent diagnostic data audit not passed')
    folder=root/'pilot';folder.mkdir(exist_ok=True)
    rows=[];details=[];pairings={}
    for seed in (30,31,32,33,34):
        for si,name in enumerate(('balanced','scarce')):
            offset=(seed-30+si)%len(METHODS);ordered=METHODS[offset:]+METHODS[:offset]
            for method in ordered:
                run=folder/f'{name}-{seed}-{method}';run.mkdir(exist_ok=True)
                ident=freeze_data['identity']
                p=provider(root,run/'responses','pilot',mode=mode) if method.startswith('local-') and not (run/'result.json').exists() else None
                row,detail=run_pilot_case(run,name,seed,method,ident,local_provider=p,
                                        experiment_context=freeze_data)
                key=(name,seed)
                if pairings.setdefault(key,row['paired_task_rule_sha256'])!=row['paired_task_rule_sha256']: raise AssertionError('paired inputs differ')
                rows.append(row);details.extend(detail)
                write_csv(folder/'per-seed.csv',rows);write_csv(folder/'per-decision.csv',details)
                save(folder/'per-seed.json',rows)
                print('pilot',len(rows),'/60',name,seed,method,flush=True)
        if seed==30: save(folder/'first-12-gate.json',{'status':'passed','runs':len(rows),'decisions':len(details)})
    aggregates=[]
    for name in ('balanced','scarce'):
        for method in METHODS:
            group=[r for r in rows if r['scenario']==name and r['method']==method]
            totals={k:math.fsum(r[k] for r in group) for k in ('offered','accepted','success','offered_value','earned_value','mandatory_shortfall_terminal',
                    'flight_hours','available_aircraft_hours','fleet_horizon_hours','spare_wait_hours','bay_wait_hours','actual_des','simulated_hours',
                    'local_generations','input_tokens','output_tokens','local_seconds','end_to_end_seconds')}
            aggregates.append({'scenario':name,'method':method,'runs':len(group),**totals,
                               **{k:totals[n]/totals[d] if totals[d] else None for k,n,d in [('DSR','success','offered'),('RCR','earned_value','offered_value'),
                                  ('ASR','success','accepted'),('availability','available_aircraft_hours','fleet_horizon_hours')]}})
    write_csv(folder/'by-scenario.csv',aggregates);save(folder/'by-scenario.json',aggregates)
    status={'status':'passed','runs':len(rows),'decisions':len(details),'actual_des':sum(r['actual_des'] for r in rows),
            'local_generations':sum(r['local_generations'] for r in rows),'local_pilot_completed':len(rows)==60,'remote_typesafe_requests':0}
    if status['actual_des']>6400 or len(details)!=300: raise AssertionError('pilot budget/count mismatch')
    save(folder/'status.json',status);return status

def replay(root,*,label='replay'):
    # No model, tokenizer, server metadata or network is accessed by this entry.
    if label not in {'replay','extracted-replay'}:raise ValueError('unsupported replay output label')
    folder=root/label;folder.mkdir(exist_ok=False);results=[];extra_des=0;extra_hours=0.
    for run in sorted((root/'pilot').glob('*')):
        if not run.is_dir() or not (run/'result.json').exists(): continue
        expected=WeeklySimulation.from_checkpoint(read(run/'checkpoint.json'))
        start=read(run/'resume-point.json');resumed=WeeklySimulation.from_checkpoint(start)
        spec=resumed.weekly_state['policy_spec']
        local=LocalProvider(spec['identity'],run/'responses',mode='cache_only') if spec['kind']=='local-candidate-des' else None
        resumed._decision_runtime=DecisionRuntime(local_provider=local)
        resumed.run(720)
        compare_replay(expected,resumed)
        expected_records=expected.weekly_state.get('decision_records',[]);actual=resumed.weekly_state.get('decision_records',[])
        if [logical_decision_record(x) for x in expected_records]!=[logical_decision_record(x) for x in actual]:
            raise AssertionError('logical replay mismatch')
        old_count=len(start['weekly_state'].get('decision_records',[]))
        extra_des+=sum(r['budget']['actual_des'] for r in actual[old_count:])
        extra_hours+=sum(r['budget']['simulated_hours'] for r in actual[old_count:])
        results.append({'run':run.name,'passed':True,'new_local_generations':local.generations if local else 0,
                        'local_metadata_queries':local.metadata_queries if local else 0,'resume_time':resumed.now})
        save(folder/'results.json',results)
    status={'status':'passed' if results else 'not_run','runs':len(results),'cache_only_replay_verified':bool(results),'new_local_generations':0,
            'extra_replay_des':extra_des,'extra_replay_simulated_hours':extra_hours,'budget_role':'extra audit replay, excluded from formal selection/reference budget'}
    save(folder/'status.json',status);return status

def captured_replay(root,*,label='replay'):
    """Replay each successful captured response and its short-window decisions.

    This verifies real response replay, not a new GPU run or a full closed loop.
    All DES here is separately booked as audit replay, never formal selection.
    """
    from sim_jev.local_provider import validate_record
    folder=root/('captured-replay' if label=='replay' else 'extracted-captured-replay')
    folder.mkdir(exist_ok=False);reports=[];des=0;hours=0.
    for path in sorted((root/'smoke').glob('completion-*.json')):
        original=read(path);canonical=original['rendered']['canonical']
        req,pool=unpack({'request':canonical['decision'],'pool':canonical['candidates']})
        validate_record(original,req,pool,original['identity'])
        local_folder=folder/path.stem;store=local_folder/'responses'
        save(store/(original['cache_key']+'.json'),original)
        p=LocalProvider(original['identity'],store,mode='cache_only')
        with patch.object(p,'_http',side_effect=AssertionError('network forbidden during replay')):
            replayed=p.rank(req,pool)
            if replayed['ranked_ids']!=original['ranked_ids']:raise AssertionError('captured ranking differs')
            for method in ('local-direct','local-des'):
                audit,count=branch_auditor(local_folder,retain_first=True)
                result=select_from_pool(req,policy(method,original['identity']),pool,DecisionRuntime(branch_auditor=audit,local_provider=p))
                logical=logical_decision_record(result.record)
                if label=='extracted-replay':
                    expected=read(root/'captured-replay'/path.stem/(method+'.json'))
                    if expected!=logical:raise AssertionError('extracted captured decision/branch artifacts differ')
                save(local_folder/(method+'.json'),logical);des+=count['des'];hours+=count['simulated_hours']
        reports.append({'capture':path.name,'request_identity':original['rendered']['canonical_sha256'],
                        'passed':True,'new_local_generations':p.generations,'metadata_queries':p.metadata_queries})
    status={'status':'passed' if reports else 'not_run','captures':len(reports),'unique_inputs':len({x['request_identity'] for x in reports}),
            'cache_only_replay_verified':bool(reports),'new_local_generations':0,'local_metadata_queries':0,
            'extra_audit_des':des,'extra_audit_simulated_hours':hours,'scope':'captured rankings and current-window local-direct/local-des artifacts; no closed-loop or GPU revalidation',
            'reports':reports}
    save(folder/'status.json',status);return status

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['development','smoke','freeze','diagnostics','reference','pilot','replay','captured-replay','recovery-run'])
    p.add_argument('--output',required=True,type=Path);p.add_argument('--baseline-only',action='store_true')
    p.add_argument('--replay-label',choices=['replay','extracted-replay'],default='replay')
    p.add_argument('--offline-only',action='store_true')
    p.add_argument('--until',type=float,default=720)
    p.add_argument('--scenario',choices=['balanced','scarce'],default='balanced')
    p.add_argument('--seed',type=int,default=30)
    p.add_argument('--method',choices=['mandatory-first','heuristic-des','random-des','direct-des'],default='heuristic-des')
    p.add_argument('--planning-seed',type=int,default=20261102)
    p.add_argument('--top-k',type=int,default=4)
    p.add_argument('--scenarios-per-candidate',type=int,default=8)
    p.add_argument('--receipt-output',type=Path,
                   help='Independent recovery command receipts; defaults to a sibling directory')
    a=p.parse_args();root=a.output.resolve();t=time.perf_counter()
    receipt_root=((a.receipt_output if a.receipt_output else root.parent/(root.name+'-receipts'))
                  if a.action=='recovery-run' else root/'commands').resolve()
    if a.action=='recovery-run' and receipt_root.is_relative_to(root):
        p.error('recovery receipt output must be outside the immutable run directory')
    if a.action!='recovery-run':root.mkdir(parents=True,exist_ok=True)
    receipt={'argv':sys.orig_argv,'cwd':str(Path.cwd()),'source':code_fingerprint(ROOT)['sha256']}
    try:
        if a.action=='recovery-run':
            if a.seed not in (10,11,13,30,31,32,33,34) or not 0<=a.until<=720:
                raise ValueError('recovery CLI only permits preregistered development/regression seeds and 0..720h')
            def recovery_policy(method,ident):
                value=policy(method,ident)
                return value if method=='mandatory-first' else replace(value,
                    planning_seed=a.planning_seed,top_k=a.top_k,
                    scenarios_per_candidate=a.scenarios_per_candidate)
            import socket
            with patch.object(socket,'socket',side_effect=AssertionError('network forbidden during offline recovery')):
                result,unused=run_pilot_case(root,a.scenario,a.seed,a.method,None,
                    until=a.until,policy_fn=recovery_policy)
            result={**result,'evidence':'offline engineering recovery only','new_model_generations':0,'network_requests':0}
        elif a.action in {'diagnostics','reference'}:result=globals()[a.action](root,baseline_only=a.baseline_only)
        elif a.action=='replay':result=replay(root,label=a.replay_label)
        elif a.action=='captured-replay':result=captured_replay(root,label=a.replay_label)
        elif a.action=='freeze':result=freeze(root,offline_only=a.offline_only)
        else:result=globals()[a.action](root)
        receipt.update(exit_code=0,result=result)
    except Exception as e:
        import traceback
        receipt.update(exit_code=1,error=f'{type(e).__name__}: {e}',audit=getattr(e,'audit',None),traceback=traceback.format_exc())
        if a.action in {'smoke','diagnostics','reference','pilot','replay'}:
            stage=root/('diagnostics' if a.action=='reference' else a.action)
            save(stage/f'failure-{time.time_ns()}.json',{'status':'partial','action':a.action,'error':receipt['error'],
                 'reason_code':getattr(e,'code',type(e).__name__),'new_inference_budget_not_added':True})
        print(receipt['traceback'],file=sys.stderr)
    receipt['seconds']=time.perf_counter()-t
    save(receipt_root/f'{a.action}-{time.time_ns()}.json',receipt)
    print(json.dumps({k:v for k,v in receipt.items() if k not in ('traceback','audit')},indent=2))
    return receipt['exit_code']

if __name__=='__main__': raise SystemExit(main())
