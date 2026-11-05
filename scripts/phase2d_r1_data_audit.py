"""Read-only independent R1 scalar, identity and durable-ledger reconciliation.

This performs no model request and no candidate DES evaluation. Public pools and
mandatory-first execution histories are reconstructed only for audit. Raw records
remain unchanged; a successful receipt is written only after every check passes.
"""
from __future__ import annotations
import argparse
from collections import Counter
import csv
from dataclasses import asdict
import json
import math
from pathlib import Path
import statistics
import sys
import time
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from sim_jev.branch_audit_store import BranchAuditStore
from sim_jev.candidates import generate_candidates
from sim_jev.decision import DecisionRequest,DecisionResult
from sim_jev.decision_audit import validate_record as validate_decision
from sim_jev.local_provider import GenerationBudget,validate_record as validate_local
from sim_jev.provenance import canonical_sha256,code_fingerprint
from sim_jev.checkpoint import _loads
from sim_jev.weekly import WeeklySimulation
from sim_jev.weekly_policy import PolicySpec,choose_command
from scripts.phase2d_local import read,save,unpack,METHODS
from scripts.phase2d_r1 import build_case,policy,business_projection,check_freeze
from scripts.phase2c_experiment import decision_rows
from scripts.planning_verify import audit_planning_branch
from scripts.weekly_verify import audit_business_log


def read(path):
    """Audit evidence rejects duplicate object keys and non-finite JSON."""
    return _loads(Path(path).read_text(encoding='utf-8'))


def require(condition,message):
    if not condition:raise AssertionError(message)


def equal(actual,expected,label):
    require(actual==expected,label+' differs')


def verify_csv(path,expected):
    with Path(path).open(encoding='utf-8-sig',newline='') as stream:
        reader=csv.DictReader(stream);actual=list(reader)
    require(len(actual)==len(expected),str(path)+': row count')
    keys=list(dict.fromkeys(key for row in expected for key in row))
    equal(set(reader.fieldnames),set(keys),str(path)+': columns')
    for index,(left,right) in enumerate(zip(actual,expected)):
        encoded={key:(json.dumps(right[key],ensure_ascii=False,separators=(',',':'))
                     if isinstance(right.get(key),(dict,list)) else '' if right.get(key) is None else str(right[key]))
                 for key in keys}
        equal(left,encoded,str(path)+': row '+str(index))


def candidate_reference(scores,state_id):
    """Recompute estimates and paired tie rules directly from 64 scalar samples."""
    require(all(len(values)==64 for values in scores.values()),'reference requires exactly 64 samples per candidate')
    means={key:math.fsum(values)/64 for key,values in scores.items()}
    winner=min(means,key=lambda key:(-means[key],key)) if means else None
    rows=[]
    for key,values in scores.items():
        differences=[a-b for a,b in zip(scores[winner],values)]
        difference_mean=math.fsum(differences)/64
        paired_se=statistics.stdev(differences)/math.sqrt(64)
        rows.append({'state_id':state_id,'candidate_id':key,'q_ref':means[key],
            'standard_error':statistics.stdev(values)/math.sqrt(64),'N':64,'scores':values,
            'numerical_tie':abs(means[winner]-means[key])<=1e-12,
            'mc_approximate_tie':difference_mean-1.96*paired_se<=0<=difference_mean+1.96*paired_se,
            'paired_difference_mean':difference_mean,'paired_difference_se':paired_se,'valid':True})
    return means,winner,rows


def recompute_metrics(state,action,scores,means,winner):
    best=means[winner] if winner is not None else None
    top=action.get('top_k');selected=action.get('selected_candidate_id')
    require(top is None or len(top)==len(set(top)) and set(top)<=set(scores),'invalid preselection IDs')
    retained=min(top,key=lambda key:(-means[key],key)) if top and means else None
    def paired_se(key):
        return statistics.stdev([a-b for a,b in zip(scores[winner],scores[key])])/8 if key in scores else None
    return {**{key:state[key] for key in ('state_id','scenario','seed','window_start','window_end','actual_M',
             'independent_identity','cross_split_duplicate','within_holdout_duplicate')},
            **action,'best_reference_score':best,'selected_reference_score':means.get(selected),
            'action_regret':best-means[selected] if selected in means else None,
            'regret_at_k':best-max(means[key] for key in top) if top and means else None,
            'action_regret_paired_se':paired_se(selected),'regret_at_k_paired_se':paired_se(retained),
            'best_candidate_recall':any(abs(best-means[key])<=1e-12 for key in top) if top and means else None,
            'reference_des':len(scores)*64}


def diagnostics_aggregates(metrics):
    result=[]
    for scope in ('unique_holdout','all_planned_trajectories','cross_split_duplicates'):
        for scenario in ('balanced','scarce'):
            for method in METHODS:
                group=[row for row in metrics if row['scenario']==scenario and row['method']==method and
                    row['status']=='completed' and (scope=='all_planned_trajectories' or
                    scope=='cross_split_duplicates' and row['cross_split_duplicate'] or
                    scope=='unique_holdout' and not row['cross_split_duplicate'] and not row['within_holdout_duplicate'])]
                row={'scope':scope,'scenario':scenario,'method':method,'states':len(group),
                     'trajectories':len({entry['seed'] for entry in group})}
                for key in ('action_regret','regret_at_k','best_candidate_recall'):
                    values=[entry[key] for entry in group if entry[key] is not None]
                    row[key]=math.fsum(values)/len(values) if values else None
                result.append(row)
    return result


def _state_identity(row):
    request,pool=unpack(row)
    require(request.protocol_version=='legal-weekly-request-v2','R1 request is not v2')
    regenerated=[item.to_dict() for item in sorted(generate_candidates(request,16,20261102),key=lambda item:item.id)]
    equal(row['pool'],regenerated,'public pool regeneration')
    equal(canonical_sha256(row['pool']),canonical_sha256(regenerated),'public pool regeneration canonical identity')
    equal(row['actual_M'],len(pool),'actual pool size')
    equal(row['canonical_request_sha256'],canonical_sha256(request.to_dict()),'canonical request hash')
    equal(row['pool_sha256'],canonical_sha256(row['pool']),'pool hash')
    equal(row['independent_identity'],canonical_sha256([row['canonical_request_sha256'],row['pool_sha256']]),'request+pool identity')
    equal(row['business_request_sha256'],canonical_sha256(business_projection(row['request'])),'exact business projection hash')
    equal(row['window_start'],request.context.window_start,'state start')
    equal(row['window_end'],request.context.window_end,'state endpoint')
    equal(row['reference_command'],choose_command(request.observation,request.context,PolicySpec()).to_dict(),'mandatory reference command')
    return request,pool


def _scalar(row,request,candidate,seed):
    counts=row['counts'];offered=[m for m in request.observation.missions
        if request.context.window_start<=m['start_time']<request.context.window_end]
    equal(counts['offered'],len(offered),'offered denominator')
    equal(counts['offered_value'],sum(m['value'] for m in offered),'offered value denominator')
    equal(counts['accepted'],len(candidate.command.accepted_ids),'accepted denominator')
    equal(counts['rejected'],counts['offered']-counts['accepted'],'rejected count')
    equal(counts['accepted'],sum(counts[key] for key in ('success','failed','accepted_not_executed','pending_execution','running')),'cohort partition')
    for key in ('offered','accepted','rejected','success','failed','accepted_not_executed','pending_execution','running'):
        require(type(counts[key]) is int and counts[key]>=0,'invalid scalar count '+key)
    require(0<=counts['earned_value']<=counts['offered_value'],'earned value outside offered cohort')
    for ratio,numerator,denominator in (('DSR','success','offered'),('RCR','earned_value','offered_value'),('ASR','success','accepted')):
        valid=counts[denominator]!=0;expected=counts[numerator]/counts[denominator] if valid else None
        equal(counts['validity'][ratio],valid,'ratio validity');equal(counts[ratio],expected,'ratio '+ratio)
    score=(counts['DSR'] or 0.)+(counts['RCR'] or 0.)
    equal(row['score'],score,'current-offer DSR+RCR scalar')
    equal(row['command'],candidate.command.to_dict(),'branch command')
    equal(row['duration_hours'],request.context.window_end-request.context.window_start,'branch duration')
    equal(row['origin'],request.context.window_start,'branch origin')
    equal(row['window_end'],request.context.window_end,'branch endpoint')
    equal(row['legal_input_hash'],canonical_sha256(request.to_dict()),'branch legal input')
    sid='planning-'+canonical_sha256({'legal_input_hash':canonical_sha256(request.to_dict()),
                                    'planning_seed':seed,'scenario_index':row['scenario_index']})
    equal(row['scenario_id'],sid,'branch scenario identity')


def _account(directory,records):
    directory=Path(directory)
    require(all((directory/'branch-audit-store'/name).is_dir()
                for name in ('attempts','audits','artifacts')),'missing durable branch audit store topology')
    store=BranchAuditStore(directory,audit_planning_branch,read_only=True)
    # Inputs must already be finalized: this verifier may not repair a pending
    # writer state as a side effect of reporting a successful formal gate.
    require(all(row['status']=='completed' for row in store._attempts()),'unfinished compute attempt')
    result=store.reconcile({'weekly_state':{'decision_records':records}})
    equal(read(directory/'branch-accounting.json'),result,'saved branch accounting')
    audits=store._audits()
    if (directory/'branch-audits.json').exists():
        equal(read(directory/'branch-audits.json'),[audits[key] for key in sorted(audits)],'saved durable audit export')
    return result


def factory_inputs(state,fresh_request):
    """Prove exact public content before interpreting legacy reference numbering.

    Legacy factoring numbered repeated objects in factory insertion order. This
    transient view changes object traversal only after values, array order and
    canonical hashes have all been proved equal. Persisted evidence is untouched.
    """
    fresh_pool=sorted(generate_candidates(fresh_request,16,20261102),key=lambda item:item.id)
    for key,value in (('request',fresh_request.to_dict()),('pool',[c.to_dict() for c in fresh_pool])):
        equal(state[key],value,'factory exact '+key)
        equal(canonical_sha256(state[key]),canonical_sha256(value),'factory canonical '+key)
    return fresh_request,fresh_pool


def factory_record(record,request,pool):
    fresh_request=request.to_dict();fresh_pool=[c.to_dict() for c in pool]
    for key,value in (('request',fresh_request),('pool',fresh_pool)):
        equal(record[key],value,'decision factory exact '+key)
        equal(canonical_sha256(record[key]),canonical_sha256(value),'decision factory canonical '+key)
    view={**record,'request':fresh_request,'pool':fresh_pool}
    equal(view,record,'audit-only object reconstruction')
    equal(canonical_sha256(view),canonical_sha256(record),'unchanged complete record identity')
    return view


def verify_diagnostics(root,frozen,*,online_only=False):
    folder=root/'diagnostics';states=read(folder/'states.json');seen=read(folder/'seen-public-states.json')
    expected_states=[(scenario,seed,point) for scenario in ('balanced','scarce') for seed in (40,41,42,43) for point in (168,336,672)]
    equal([(r['scenario'],r['seed'],r['window_start']) for r in states],expected_states,'planned heldout states')
    equal(Counter((r['scenario'],r['seed'],r['window_start']) for r in seen),
          Counter((scenario,seed,point) for scenario in ('balanced','scarce') for seed,points in
                  [(10,(168,672)),(11,(168,672)),*[(s,(168,336,672)) for s in (20,21,22,23)]] for point in points),'both seen partitions')
    for row in seen:_state_identity(row)
    exact={r['independent_identity'] for r in seen};business={r['business_request_sha256'] for r in seen};within=set()
    trajectories={};captured={}
    for scenario,seed in dict.fromkeys((r['scenario'],r['seed']) for r in states):
        def capture(request,spec,runtime=None):
            captured[(scenario,seed,request.context.window_start)]=request
            return DecisionResult(choose_command(request.observation,request.context,spec))
        sim=build_case(scenario,seed,PolicySpec())
        with patch('sim_jev.weekly.resolve_decision',side_effect=capture):sim.run(672)
        audit_business_log(sim);trajectories[(scenario,seed)]=sim
    actions=read(folder/'online-actions.json');online=read(folder/'online-complete.json')
    equal(online['sha256'],canonical_sha256(actions),'online seal')
    equal([(r['state_id'],r['method']) for r in actions],[(s['state_id'],m) for s in states for m in METHODS],'all online actions')
    all_candidates=[];all_metrics=[];online_count=reference_count=physical_count=0;reference_hours=0.;accounted=[]
    for state in states:
        _state_identity(state);directory=folder/state['state_id']
        request,pool=factory_inputs(state,captured[(state['scenario'],state['seed'],state['window_start'])])
        by_id={c.id:c for c in pool}
        equal(state['cross_split_exact_duplicate'],state['independent_identity'] in exact,'cross-split exact flag')
        equal(state['cross_split_business_duplicate'],state['business_request_sha256'] in business,'cross-split business flag')
        equal(state['cross_split_duplicate'],state['independent_identity'] in exact or state['business_request_sha256'] in business,'cross-split exclusion')
        equal(state['within_holdout_duplicate'],state['independent_identity'] in within,'within-holdout duplicate');within.add(state['independent_identity'])
        shared=read(directory/'shared-ranking.json') if len(pool)>1 else None
        if shared:
            validate_local(shared,request,pool,frozen['identity'])
            require(shared['evidence'] in ('local_inference','local_replay') and shared['valid_local_model'],'non-live formal ranking')
        current_actions=[action for action in actions if action['state_id']==state['state_id']]
        for action in current_actions:
            method=action['method'];record=read(directory/(method+'.json'))
            equal(action['status'],'completed','online method status')
            if method=='mandatory-first':
                equal(record['command'],state['reference_command'],'rule command')
                selected=next((c.id for c in pool if c.command.to_dict()==record['command']),None)
                equal(record['selected_candidate_id'],selected,'rule selected ID');equal(record['budget']['actual_des'],0,'rule zero DES')
            else:
                record=factory_record(record,request,pool)
                validate_decision(record,policy(method,frozen['identity']),trajectories[(state['scenario'],state['seed'])])
                equal(record['request'],state['request'],'shared legal state');equal(record['pool'],state['pool'],'shared candidate pool')
                for branch in record['branches']:_scalar(branch,request,by_id[branch['candidate_id']],20261102)
                if method.startswith('local-') and shared:
                    for key in ('cache_key','ranked_ids','preference_scores','ties','identity'):
                        equal(record['local'][key],shared[key],'shared scoring '+key)
            equal(action['selected_candidate_id'],record['selected_candidate_id'],'online selected ID')
            top=record['ranking_ids'][:4] if method in ('random-des','heuristic-des','local-des','local-direct') else None
            equal(action['top_k'],top,'preselected top K')
            equal(action['actual_des'],record['budget']['actual_des'],'online DES count')
            equal(action['simulated_hours'],record['budget']['simulated_hours'],'online simulated hours')
            equal(action['shared_ranking_seconds'],shared['generation_seconds'] if shared and method.startswith('local-') else None,'shared generation timing')
            equal(action['cache_seconds'],(record.get('local') or {}).get('cache_seconds'),'cache read timing')
            accounting=_account(directory/method,[] if method=='mandatory-first' else [record]);accounted.append(accounting)
            online_count+=accounting['logical_branches']
        if online_only:continue
        reference_dir=folder/'evaluation'/state['state_id'];scores={c.id:[] for c in pool};branches=[]
        with (reference_dir/'branches.jsonl').open(encoding='utf-8') as stream:
            for line in stream:
                row=json.loads(line);candidate=by_id[row['candidate_id']]
                equal(row['scenario_index'],len(scores[candidate.id]),'reference scenario sequence')
                require(row['scenario_index']<64,'reference extra scenario')
                equal(row['planning_seed'],20261103,'reference seed');equal(row['purpose_namespace'],'reference-v1','reference namespace')
                _scalar(row,request,candidate,20261103)
                require(row['audit']['verified'] and row['audit']['score']==row['score'],'reference auditor scalar')
                scores[candidate.id].append(row['score']);reference_count+=1;reference_hours+=row['duration_hours']
                branches.append({key:value for key,value in row.items() if key not in ('audit','planning_seed','purpose_namespace')})
        accounted.append(_account(reference_dir,[{'request':request.to_dict(),'branches':branches}]))
        means,winner,rows=candidate_reference(scores,state['state_id']);all_candidates.extend(rows)
        all_metrics.extend(recompute_metrics(state,action,scores,means,winner) for action in current_actions)
        if pool:
            physical=read(reference_dir/'physical-reference-0.json');audit_planning_branch(physical,request)
            retained=next(row for row in branches if row['candidate_id']==min(by_id) and row['scenario_index']==0)
            equal(canonical_sha256(physical['final_state']),retained['physical_artifact_sha256'],'retained minimum candidate physical hash')
            physical_count+=1
    expected_audits=[{'state_id':a['state_id'],'method':a['method'],'des':a['actual_des'],
                     'simulated_hours':a['simulated_hours']} for a in actions]
    equal(read(folder/'online-audits.json'),expected_audits,'online scalar accounting export')
    equal(online['actual_des'],online_count,'online sealed DES total')
    equal(online['states'],24,'online sealed state count');equal(online['actions'],144,'online sealed method count')
    if online_only:
        require(online_count<=3072,'online DES quota')
        require(sum(a['logical_branches'] for a in accounted)==sum(a['actual_compute_attempts'] for a in accounted)==
                sum(a['persistent_audit_identities'] for a in accounted)==online_count,'online three-way accounting')
        return {'states':24,'actions':144,'online_branches':online_count,'trajectories':len(trajectories),
                'actual_compute_attempts':online_count,'persistent_audit_identities':online_count,
                'extra_selection_des':0,'extra_audit_des':0,'reference_labels_read':False,
                'audit_execution_simulated_hours':len(trajectories)*672}
    equal(read(folder/'evaluation/per-state.json'),all_metrics,'per-state metrics')
    verify_csv(folder/'evaluation/per-state.csv',all_metrics)
    verify_csv(folder/'evaluation/per-candidate-reference.csv',all_candidates)
    aggregate=diagnostics_aggregates(all_metrics)
    equal(read(folder/'evaluation/by-scenario.json'),aggregate,'diagnostic aggregates')
    verify_csv(folder/'evaluation/by-scenario.csv',aggregate)
    status=read(folder/'status.json')
    require(status['status']=='passed' and status['local_live_diagnostics_completed'],'diagnostic completion marker')
    equal(status['actual_reference_des'],reference_count,'reference total');equal(status['reference_simulated_hours'],reference_hours,'reference hours')
    equal(status['independent_branch_audits'],reference_count,'reference audit total')
    equal(status['online_des'],online_count,'online total');equal(online['actual_des'],online_count,'online sealed total')
    require(online_count<=3072 and reference_count<=24576,'formal diagnostic budget exceeded')
    require(sum(a['logical_branches'] for a in accounted)==sum(a['actual_compute_attempts'] for a in accounted)==
            sum(a['persistent_audit_identities'] for a in accounted)==online_count+reference_count,'diagnostic three-way accounting')
    return {'states':len(states),'trajectories':len(trajectories),'online_branches':online_count,
            'reference_branches':reference_count,'reference_simulated_hours':reference_hours,
            'retained_physical_artifacts_audited':physical_count,
            'cross_split_duplicates':sum(r['cross_split_duplicate'] for r in states),
            'unique_holdout_states':sum(not r['cross_split_duplicate'] and not r['within_holdout_duplicate'] for r in states),
            'extra_selection_des':0,'audit_execution_simulated_hours':len(trajectories)*672}


def verify_pilot_run(directory,frozen):
    directory=Path(directory);row=read(directory/'result.json');checkpoint=read(directory/'checkpoint.json')
    require((directory/'branch-audits.json').is_file(),'missing completed-run durable audit export')
    sim=WeeklySimulation.from_checkpoint(checkpoint)
    require(sim.now==720 and sim.arithmetic_protocol=='event-anchor-binary64-v2','pilot endpoint/protocol')
    equal(sim.weekly_state['policy_spec'],policy(row['method'],frozen['identity']).to_dict(),'pilot policy')
    requested=build_case(row['scenario'],row['seed'],policy(row['method'],frozen['identity']))
    for actual,wanted,label in (
            (sim.config.to_dict(),requested.config.to_dict(),'config'),
            (sim.weekly_state['scenario_id'],requested.weekly_state['scenario_id'],'scenario identity'),
            ([asdict(value) for value in sim._input_missions],[asdict(value) for value in requested._input_missions],'exogenous missions'),
            (sim.weekly_state['rules_input'],requested.weekly_state['rules_input'],'exogenous rules'),
            (sim._initial_lifetimes,requested._initial_lifetimes,'initial lifetime overrides')):
        equal(actual,wanted,'pilot requested '+label)
        equal(canonical_sha256(actual),canonical_sha256(wanted),'pilot requested canonical '+label)
    audit_business_log(sim);summary=sim.summary()
    # Historical result.failures denotes model/protocol failures; the physical
    # failure count remains independently checked in checkpoint metrics/logs.
    for key in (row.keys() & summary.keys())-{'failures'}:equal(row[key],summary[key],'pilot summary '+key)
    equal(row['failures'],sum(d.get('failures',0) for d in read(directory/'per-decision.json')),'model protocol failures')
    require(row['seed']==sim.config.seed and row['status']=='completed','pilot seed/status')
    expected_exogenous={'missions':[asdict(x) for x in sim._input_missions],'rules':sim.weekly_state['rules_input']}
    equal(read(directory/'exogenous.json'),expected_exogenous,'exogenous records')
    equal(row['paired_task_rule_sha256'],canonical_sha256(expected_exogenous),'exogenous pair hash')
    details=read(directory/'per-decision.json');expected=decision_rows(sim,row['scenario'],row['seed'],row['method'])
    equal(len(details),5,'pilot decisions')
    for actual,wanted in zip(details,expected):
        for key,value in wanted.items():equal(actual[key],value,'decision field '+key)
    records=sim.weekly_state.get('decision_records',[])
    equal(read(directory/'decisions.json'),records,'checkpoint/decision export')
    for record in records:
        request,pool=unpack(record);candidates={c.id:c for c in pool}
        validate_decision(record,policy(row['method'],frozen['identity']),sim)
        for branch in record['branches']:_scalar(branch,request,candidates[branch['candidate_id']],20261102)
        local=record.get('local')
        if local and len(pool)>1:require(local['evidence'] in ('local_inference','local_replay') and local['valid_local_model'],'pilot fixture masquerades as model')
        selected=[b for b in record['branches'] if b['candidate_id']==record['selected_candidate_id'] and b['scenario_index']==0]
        if selected:
            artifact=read(directory/('window-'+str(int(request.context.window_start)))/'branch-0.json')
            audit_planning_branch(artifact,request)
            equal(canonical_sha256(artifact['final_state']),selected[0]['physical_artifact_sha256'],'selected branch physical hash')
    accounting=_account(directory,records)
    equal(row['actual_des'],accounting['logical_branches'],'pilot committed DES count')
    equal(row['decisions'],len(details),'pilot detail count')
    for total,field in (('actual_des','actual_des'),('simulated_hours','simulated_hours'),('local_generations','local_generations'),
                        ('input_tokens','input_tokens'),('output_tokens','output_tokens'),('local_seconds','local_seconds'),('corrections','corrections')):
        equal(row[total],sum(d[field] for d in details),'pilot total '+total)
    for detail in details:
        record=next((r for r in records if r['command']['window_start']==detail['window_start']),{})
        local=record.get('local') or {}
        for field,key in (('local_generations','local_generation_attempts'),('input_tokens','actual_input_tokens'),('output_tokens','actual_output_tokens')):
            equal(detail[field],local.get(key,0),'local actual accounting '+field)
    invocations=[read(path) for path in (directory/'execution-attempts').glob('*.json')]
    forward=[entry for entry in invocations if entry['role']=='forward_execution']
    unknown=sum(entry['seconds'] is None for entry in forward)
    known=sum(entry['seconds'] for entry in forward if entry['seconds'] is not None)
    equal(row['end_to_end_seconds'],None if unknown else known,'resume-aware total execution time')
    equal(row['known_end_to_end_seconds'],known,'known execution time')
    equal(row['uncertain_timing_invocations'],unknown,'unknown execution time count')
    equal(row['des_seconds'],accounting['compute_seconds'],'DES timing ledger')
    equal(row['independent_audit_seconds'],accounting['audit_seconds'],'independent audit timing ledger')
    return row,details,accounting


def verify_pilot(root,frozen,*,require_complete=False):
    folder=root/'pilot';rows=[];details=[];accounts=[];pairings={}
    if not folder.exists():return {'pilot_runs':0,'pilot_decisions':0,'pilot_des':0,'pilot_completed':False}
    # Saved rows preserve the preregistered interleaved execution order.
    planned=[]
    for seed in (50,51,52,53,54):
        for index,scenario in enumerate(('balanced','scarce')):
            offset=(seed-50+index)%6
            planned.extend((scenario,seed,method) for method in METHODS[offset:]+METHODS[:offset])
    for scenario,seed,method in planned:
        directory=folder/f'{scenario}-{seed}-{method}'
        if not (directory/'result.json').exists():continue
        row,detail,account=verify_pilot_run(directory,frozen)
        equal((row['scenario'],row['seed'],row['method']),(scenario,seed,method),'pilot directory identity')
        pair=(scenario,seed)
        equal(pairings.setdefault(pair,row['paired_task_rule_sha256']),row['paired_task_rule_sha256'],'paired exogenous CRN inputs')
        rows.append(row);details.extend(detail);accounts.append(account)
    equal([(r['scenario'],r['seed'],r['method']) for r in rows],planned[:len(rows)],'pilot order and missing runs')
    if (folder/'per-seed.json').exists():equal(read(folder/'per-seed.json'),rows,'per-seed export')
    if (folder/'per-seed.csv').exists():verify_csv(folder/'per-seed.csv',rows)
    if (folder/'per-decision.csv').exists():verify_csv(folder/'per-decision.csv',details)
    total=sum(row['actual_des'] for row in rows)
    require(total<=6400,'pilot DES limit')
    equal(sum(a['actual_compute_attempts'] for a in accounts),total,'actual pilot compute attempts')
    equal(sum(a['persistent_audit_identities'] for a in accounts),total,'pilot persistent coverage')
    if (folder/'by-scenario.json').exists():
        for aggregate in read(folder/'by-scenario.json'):
            group=[r for r in rows if r['scenario']==aggregate['scenario'] and r['method']==aggregate['method']]
            equal(aggregate['runs'],len(group),'aggregate run count')
            for key in aggregate.keys()-{'scenario','method','runs','DSR','RCR','ASR','availability'}:
                expected=math.fsum(r[key] for r in group) if all(r[key] is not None for r in group) else None
                equal(aggregate[key],expected,'pilot accumulated '+key)
            for key,num,den in (('DSR','success','offered'),('RCR','earned_value','offered_value'),('ASR','success','accepted'),('availability','available_aircraft_hours','fleet_horizon_hours')):
                numerator=math.fsum(r[num] for r in group);denominator=math.fsum(r[den] for r in group)
                equal(aggregate[key],numerator/denominator if denominator else None,'ratio of accumulated counts '+key)
    completed=len(rows)==60
    if require_complete:require(completed and len(details)==300,'pilot incomplete')
    if (folder/'status.json').exists() and read(folder/'status.json').get('status')=='passed':
        status=read(folder/'status.json')
        require(completed and status['runs']==60 and status['decisions']==300 and status['actual_des']==total,'false pilot completion gate')
    return {'pilot_runs':len(rows),'pilot_decisions':len(details),'pilot_des':total,'pilot_completed':completed,
            'pilot_actual_compute_attempts':sum(a['actual_compute_attempts'] for a in accounts),
            'pilot_persistent_audit_identities':sum(a['persistent_audit_identities'] for a in accounts)}


def verify_generation_budget(root,*,require_complete_binding=False):
    budget=GenerationBudget(root/'generation-budget.json').read()
    require(all(a['status']=='completed' for a in budget['attempts']),'uncertain model generation prevents successful audit')
    counts=Counter(a['phase'] for a in budget['attempts'])
    require(all(counts[phase]<=limit for phase,limit in {'development':80,'diagnostics':48,'pilot':200}.items()),'generation quota')
    require(len(budget['attempts'])<=328 and budget['seconds']<=7200,'campaign quota')
    require(len({a['attempt_id'] for a in budget['attempts']})==len(budget['attempts']),'duplicate generation attempt identity')
    paths=[path for path in root.rglob('attempt-*.json') if 'responses' in path.parts]
    artifacts={path.name:path for path in paths}
    equal(len(paths),len(artifacts),'duplicate raw attempt files')
    by_id={attempt['attempt_id']:attempt for attempt in budget['attempts']}
    raw_by_id={}
    for attempt in budget['attempts']:
        artifact=attempt.get('artifact');require(artifact is not None and artifact['path'] in artifacts,'generation lacks durable raw evidence')
        value=read(artifacts[artifact['path']]);equal(canonical_sha256(value),artifact['sha256'],'raw generation artifact hash')
        equal(value['attempt']['attempt_id'],attempt['attempt_id'],'raw attempt binding')
        equal(value['cache_key'],attempt['key'],'raw attempt cache binding')
        raw_by_id[attempt['attempt_id']]=value
    records=[]
    for path in (root/'development').glob('*/completion-*.json'):records.append(('development',read(path)))
    for path in (root/'diagnostics').glob('*/shared-ranking.json'):records.append(('diagnostics',read(path)))
    for path in (root/'pilot').glob('*/decisions.json'):
        records.extend(('pilot',record['local']) for record in read(path) if record.get('local'))
    bound_attempts=set()
    for phase,record in records:
        if phase=='development':
            canonical=record['rendered']['canonical']
            request,pool=unpack({'request':canonical['decision'],'pool':canonical['candidates']})
            validate_local(record,request,pool,record['identity'])
        for attempt in record['attempts']:
            aid=attempt['attempt_id'];require(aid in by_id,'selected response has no campaign attempt')
            bound_attempts.add(aid)
            equal(by_id[aid]['phase'],phase,'cross-phase model response reuse')
            equal(by_id[aid]['key'],record['cache_key'],'response campaign cache identity')
            raw=raw_by_id[aid]
            equal(raw['identity'],record['identity'],'raw model protocol identity')
            equal(canonical_sha256(raw['identity']),canonical_sha256(record['identity']),'raw model protocol identity canonical')
            equal(raw['rendered'],record['rendered'],'raw public input identity')
            equal(canonical_sha256(raw['rendered']),canonical_sha256(record['rendered']),'raw public input identity canonical')
            accepted_attempt={key:value for key,value in attempt.items() if key!='format_error'}
            equal(raw['attempt'],accepted_attempt,'response/raw attempt contents')
            equal(canonical_sha256(raw['attempt']),canonical_sha256(accepted_attempt),'response/raw attempt contents canonical')
    if require_complete_binding:
        equal(bound_attempts,set(by_id),'all raw generations bound to accepted strict records')
    return {'generation_counts':dict(counts),'actual_generations':len(budget['attempts']),
            'generation_seconds':budget['seconds'],'remote_typesafe_requests':0}


def verify_online(root):
    """Mandatory pre-reference audit; never opens reference labels or runs DES."""
    root=Path(root);started=time.perf_counter();frozen=check_freeze(root)
    result=verify_diagnostics(root,frozen,online_only=True)
    result.update(verify_generation_budget(root))
    result.update(status='passed',source_sha256=frozen['source']['sha256'],
                  online_actions_sha256=canonical_sha256(read(root/'diagnostics/online-actions.json')),
                  audit_seconds=time.perf_counter()-started)
    save(root/'online-data-audit.json',result)
    return result


def verify(root,*,include_pilot=True,compatibility=None,output=None):
    root=Path(root);started=time.perf_counter()
    frozen=check_freeze(root) if compatibility is None else compatibility.frozen_for(root)
    if output is not None and Path(output).resolve().is_relative_to(root.resolve()):
        raise ValueError('derived audit output must be outside read-only evidence')
    result=verify_diagnostics(root,frozen)
    result.update(verify_pilot(root,frozen,require_complete=compatibility is not None) if include_pilot else {'pilot_runs':0,'pilot_des':0,'pilot_decisions':0,'pilot_completed':False})
    result.update(verify_generation_budget(root,require_complete_binding=compatibility is not None))
    result.update(status='passed',formal_des=result['online_branches']+result['reference_branches']+result['pilot_des'],
        raw_scalar_audit=True,local_methods_included=True,remote_typesafe_requests=0,
        evidence_source_sha256=frozen['source']['sha256'],auditor_source_sha256=code_fingerprint(ROOT)['sha256'],
        audit_seconds=time.perf_counter()-started,extra_audit_des=0)
    require(result['formal_des']<=34048,'formal total DES quota')
    if compatibility is not None:result.update(compatibility.identity())
    save(root/'data-audit.json' if output is None else Path(output),result)
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();print(json.dumps(verify(args.output.resolve()),indent=2));return 0


if __name__=='__main__':raise SystemExit(main())
