"""Preregistered paired environment and conditional-reference statistics."""
import csv
from fractions import Fraction
import itertools
import json
import math
from pathlib import Path
import numpy as np
from scripts.phase2ec2_common import *


def csv_write(path,rows):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('x',encoding='utf-8-sig',newline='') as f:
        if not rows: return
        keys=list(dict.fromkeys(k for row in rows for k in row))
        w=csv.DictWriter(f,fieldnames=keys); w.writeheader()
        for row in rows:
            w.writerow({k:json.dumps(v,ensure_ascii=False) if isinstance(v,(list,dict)) else v for k,v in row.items()})


def paired_stats(matrix,reg,exact_means=None):
    a=np.asarray(matrix,dtype=float)
    if len(a)==0: return dict(n=0,mean=None,evidence='insufficient')
    if a.ndim==1: a=a[:,None]
    n,b=a.shape; means=a.mean(axis=1); count=reg['bootstrap_n']
    ei=np.random.default_rng(reg['environment_bootstrap_seed']).integers(0,n,(count,n))
    env=means[ei].mean(axis=1)
    rng=np.random.default_rng(reg['mc_bootstrap_seed']); mc=np.zeros(count)
    for row in a:
        idx=rng.integers(0,b,(count,b)); mc+=row[idx].mean(axis=1)/n
    signs=np.sign(means) if exact_means is None else np.array([int(x>0)-int(x<0) for x in exact_means])
    return dict(n=n,reference_scenarios_per_environment=b,mean=float(means.mean()),
        environment_ci95=np.quantile(env,[.025,.975]).tolist(),environment_lower95=float(np.quantile(env,.05)),
        environment_upper95=float(np.quantile(env,.95)),reference_mc_ci95=np.quantile(mc,[.025,.975]).tolist(),
        reference_mc_lower95=float(np.quantile(mc,.05)),reference_mc_upper95=float(np.quantile(mc,.95)),
        exact_wins=int((signs>0).sum()),exact_ties=int((signs==0).sum()),exact_losses=int((signs<0).sum()))


def per_environment_mc(values,seed,count=5000):
    a=np.asarray(values,dtype=float); rng=np.random.default_rng(seed)
    means=a[rng.integers(0,len(a),(count,len(a)))].mean(axis=1)
    return dict(ci95=np.quantile(means,[.025,.975]).tolist(),lower95=float(np.quantile(means,.05)),upper95=float(np.quantile(means,.95)))


def sign(value): return int(value>0)-int(value<0)


def analyze(root):
    root=Path(root); reg=check(root,action=True)
    if (root/'analysis/results.json').exists(): return read(root/'gate.json')
    summary=read(root/'reference-summary.json'); complete=set(summary['completed'])
    top1=read(root/'top1-actions.json'); extra=read(root/'reference-candidates.json')
    data={}; metrics=[]; branch_metrics=[]; excluded=[]; compare_rows=[]; permutations=[]; reranks=[]
    for plan in read(root/'pool-plan.json'):
        sid=plan['state_id']
        if not plan['eligible'] or sid not in complete:
            excluded.append(dict(state_id=sid,reason=plan['reason'] or 'incomplete_reference_pair')); continue
        state=read(root/'states'/sid/'state.json'); actions={m:read(root/'online'/sid/m/'action.json') for m in METHODS}
        r32=lines(root/'reference/r32'/sid/'branches.jsonl'); r64=lines(root/'reference/r64'/sid/'branches.jsonl')
        raw={(r['candidate_id'],r['scenario_index']):r for r in r32+r64}
        if len(raw)!=len(r32)+len(r64): raise ValueError('duplicate reference branch')
        ids=[c['id'] for c in state['pool']]
        if set(raw)!={(c,j) for c in ids for j in range(32)}|{(c,j) for c in extra[sid]['all'] for j in range(32,64)}:
            raise ValueError('reference matrix incomplete')
        q32={c:sum((q_exact(raw[c,j]) for j in range(32)),Fraction(0))/32 for c in ids}
        q64={c:sum((q_exact(raw[c,j]) for j in range(64)),Fraction(0))/64 for c in extra[sid]['all']}
        selected={m:a['selected_candidate_id'] for m,a in actions.items()}; selected.update(top1[sid])
        x={m:np.array([float(q_exact(raw[c,j])) for j in range(64)]) for m,c in selected.items()}
        data[sid]=dict(state=state,actions=actions,raw=raw,q32=q32,q64=q64,selected=selected,x=x)
        for method,cid in selected.items():
            a=actions.get(method); kept=a['retained_ids'] if a else [cid]
            best=max(q32.values()); retained_best=max(q32[c] for c in kept)
            rows=[raw[cid,j] for j in range(64)]; fleet=state['request']['parameters']['fleet_size']
            business={k:float(np.mean([r['counts'][k] for r in rows])) for k in ('offered','success','offered_value','earned_value',
                'accepted','rejected','failed','unfinished','mandatory_shortfall_as_of')}
            business['DSR']=float(np.mean([r['counts']['DSR'] or 0 for r in rows])); business['RCR']=float(np.mean([r['counts']['RCR'] or 0 for r in rows]))
            business['availability']=float(np.mean([r['metrics_delta']['available_aircraft_hours']/(fleet*168) for r in rows]))
            row=dict(state_id=sid,index=plan['index'],method=method,selected=cid,M=len(ids),Q=float(q64[cid]),Q_exact=rational(q64[cid]),
                R32_screening_loss=float(best-retained_best),R32_within_selection_loss=float(retained_best-q32[cid]),
                R32_total_regret=float(best-q32[cid]),R32_top2_covers_best=retained_best==best,
                continuous_seconds=a['continuous_wall_seconds'] if a else None,
                actual_des=a['actual_des'] if a else 0,forward_calls=a['forward_calls'] if a else 0,
                token_count=a['token_count'] if a else None,memory=a['memory'] if a else None,
                float_exact_selection_differs=a['float_exact_selection_differs'] if a else None,**business)
            metrics.append(row)
            for j,r in enumerate(rows):
                c=r['counts']
                branch_metrics.append(dict(state_id=sid,method=method,candidate_id=cid,scenario_index=j,scenario_id=r['scenario_id'],
                    **{k:c[k] for k in ('success','offered','earned_value','offered_value','DSR','RCR','mandatory_shortfall_as_of')},
                    validity=r['validity'],availability=r['metrics_delta']['available_aircraft_hours']/(fleet*168)))
    methods=METHODS+['P0','A0']; aggregate={}
    for m in methods:
        rows=[r for r in metrics if r['method']==m]
        if not rows: continue
        keys=('Q','R32_screening_loss','R32_within_selection_loss','R32_total_regret','R32_top2_covers_best','DSR','RCR','availability','mandatory_shortfall_as_of')
        aggregate[m]=dict(n=len(rows),**{k:float(np.mean([r[k] for r in rows])) for k in keys},
            p50=float(np.median([r['continuous_seconds'] for r in rows])) if m in METHODS else None,
            p95=float(np.quantile([r['continuous_seconds'] for r in rows],.95)) if m in METHODS else None,
            actual_des=sum(r['actual_des'] for r in rows),forward_calls=sum(r['forward_calls'] for r in rows))
    contrasts={}
    pairs=list(itertools.combinations(METHODS,2))+[('P0','A0')]+[(x,y) for x,y in
        [('P8-G','A8'),('P8-G','A8-G'),('P8-G','H42'),('P8-G','P8'),('P8','A8'),('A8-G','A8')]]
    for first,second in dict.fromkeys(pairs):
        exact=[v['q64'][v['selected'][first]]-v['q64'][v['selected'][second]] for v in data.values()]
        stat=paired_stats([v['x'][first]-v['x'][second] for v in data.values()],reg,exact)
        stat['different_actions']=sum(v['selected'][first]!=v['selected'][second] for v in data.values())
        contrasts[first+'__'+second]=stat
        for (sid,v),delta in zip(data.items(),exact):
            compare_rows.append(dict(state_id=sid,first=first,second=second,Q_difference=float(delta),exact_difference=rational(delta),
                action_differs=v['selected'][first]!=v['selected'][second]))
    permutation_stats={}; variant_matrices={}; variant_exact={}
    for tag in ('reverse','random'):
        mats={'P8':[],'P8-G':[]}; exacts={'P8':[],'P8-G':[]}; ids=[]; align=[]; rows=[]
        for sid,v in data.items():
            path=root/'permutation'/sid/tag/'action.json'
            if not path.exists(): continue
            a=read(path)
            if a['status']!='completed': continue
            alignment=read(path.parent/'alignment.json'); ids.append(sid); align.append(alignment['ID_alignment'])
            row=dict(state_id=sid,variant=tag,top2_equal=alignment['top2_equal'],ID_alignment=alignment['ID_alignment'],
                command_equal=alignment['command_equal'],score_changes={c:a['predictions'][c]-v['actions']['P8']['predictions'][c] for c in a['predictions']},
                logit_min_adjacent_gap=alignment['logit_min_adjacent_gap'])
            for method,cid in [('P8',a['selected_candidate_id']),('P8-G',a['guard_selected_id'])]:
                baseline=v['selected'][method]; exact=v['q64'][baseline]-v['q64'][cid]
                delta=np.array([float(q_exact(v['raw'][baseline,j])-q_exact(v['raw'][cid,j])) for j in range(64)])
                mats[method].append(delta); exacts[method].append(exact)
                row[method+'_penalty']=float(exact); row[method+'_reference_equal']=exact==0
                row[method+'_action_equal']=baseline==cid
                row[method+'_MC']=per_environment_mc(delta,reg['mc_bootstrap_seed'])
            rows.append(row); permutations.append(row)
        permutation_stats[tag]=dict(n=len(ids),top2_consistency=float(np.mean([r['top2_equal'] for r in rows])) if rows else None,
            ID_all_aligned=bool(ids) and all(align),
            penalties={m:paired_stats(mats[m],reg,exacts[m]) for m in mats})
        variant_matrices[tag]=dict(zip(ids,mats['P8-G'])); variant_exact[tag]=dict(zip(ids,exacts['P8-G']))
    both=sorted(set(variant_matrices['reverse'])&set(variant_matrices['random']))
    clustered=paired_stats([(variant_matrices['reverse'][s]+variant_matrices['random'][s])/2 for s in both],reg,
        [(variant_exact['reverse'][s]+variant_exact['random'][s])/2 for s in both])
    rerank_aggregate={}
    for sid,v in data.items():
        for method in ('P8','A8'):
            a=v['actions'][method]; first,second=a['retained_ids']
            online=lines(root/'online'/sid/method/'branches.jsonl'); on={(r['candidate_id'],r['scenario_index']):r for r in online}
            online_diff=sum((q_exact(on[second,j])-q_exact(on[first,j]) for j in range(4)),Fraction(0))/4
            ref_diff=v['q64'][second]-v['q64'][first]
            ref_vector=[float(q_exact(v['raw'][second,j])-q_exact(v['raw'][first,j])) for j in range(64)]
            switched=a['selected_candidate_id']!=first
            original_gain=v['q64'][a['selected_candidate_id']]-v['q64'][first]
            row=dict(state_id=sid,method=method,online_B4_difference=rational(online_diff),R64_difference=rational(ref_diff),
                online_sign=sign(online_diff),reference_sign=sign(ref_diff),R64_difference_MC=per_environment_mc(ref_vector,reg['mc_bootstrap_seed']),
                original_switched=switched,original_switch_effect='help' if original_gain>0 else 'harm' if original_gain<0 else 'tie',
                original_gain=float(original_gain),selections={})
            path=root/'rerank'/sid/'selection.json'
            if path.exists() and read(path)['status']=='completed':
                d=read(path)
                for name,s in d['selections'][method].items():
                    cid=s['selected']; best=max(v['q64'][c] for c in a['retained_ids'])
                    row['selections'][name]=dict(selected=cid,switched_from_top1=cid!=first,changed_from_original=cid!=a['selected_candidate_id'],
                        Q=float(v['q64'][cid]),within_retained_loss=float(best-v['q64'][cid]))
            reranks.append(row)
    for method in ('P8','A8'):
        rows=[r for r in reranks if r['method']==method]
        rerank_aggregate[method]=dict(n=len(rows),switched=sum(r['original_switched'] for r in rows),
            help=sum(r['original_switched'] and r['original_switch_effect']=='help' for r in rows),
            harm=sum(r['original_switched'] and r['original_switch_effect']=='harm' for r in rows),
            tie=sum(r['original_switched'] and r['original_switch_effect']=='tie' for r in rows),budgets={})
        for name in ('B4-0','B4-1','B4-2','B4-3','B8','B16'):
            usable=[r['selections'][name] for r in rows if name in r['selections']]
            rerank_aggregate[method]['budgets'][name]=dict(n=len(usable),
                switch_rate=float(np.mean([r['switched_from_top1'] for r in usable])) if usable else None,
                changed_from_original_rate=float(np.mean([r['changed_from_original'] for r in usable])) if usable else None,
                selection_loss=float(np.mean([r['within_retained_loss'] for r in usable])) if usable else None,
                Q=float(np.mean([r['Q'] for r in usable])) if usable else None)
    mandatory={}
    for baseline in ('A8','H32'):
        values=[]
        for v in data.values():
            p,b=v['selected']['P8-G'],v['selected'][baseline]
            values.append([v['raw'][p,j]['counts']['mandatory_shortfall_as_of']-v['raw'][b,j]['counts']['mandatory_shortfall_as_of'] for j in range(64)])
        mandatory[baseline]=paired_stats(values,reg)
    journal=Journal(root); entries=list(journal.entries.values())
    pending=[e['key'] for e in entries if e['status']=='pending' or (e['status']=='failed' and e['phase']!='engineering_des')]
    eligible=sum(p['eligible'] for p in read(root/'pool-plan.json'))
    foundation=dict(complete_pairs=len(data),eligible=eligible,minimum=112,
        no_eligible_failure=len(data)==eligible and not read(root/'online-validation.json')['failures'] and not summary['failures'],
        ledgers_complete=not pending,budget_within_cap=all(journal.counts[k]<=cap for k,cap in LIMITS.items()),
        single_forward_all=all(v['actions'][m]['forward_calls']==1 for v in data.values() for m in ('P8','P8-G')),
        legal_all=all(not a['fallback'] and a['rule_violations']==0 for v in data.values() for a in v['actions'].values()),
        continuous_timing_complete=all(math.isfinite(a['continuous_wall_seconds']) and a['continuous_wall_seconds']>0 for v in data.values() for a in v['actions'].values()),
        seals_verified=True,pending_or_failed_attempts=pending)
    foundation['passed']=len(data)>=112 and all(foundation[k] for k in ('no_eligible_failure','ledgers_complete','budget_within_cap','single_forward_all','legal_all','continuous_timing_complete','seals_verified'))
    checks=[]
    def gate(name,value,op,threshold,denominator,group):
        passed=value is not None and {'>':lambda:value>threshold,'>=':lambda:value>=threshold,'<=':lambda:value<=threshold,'==':lambda:value==threshold}[op]()
        checks.append(dict(name=name,value=value,operator=op,threshold=threshold,denominator=denominator,group=group,
            passed=bool(passed),failure_reason=None if passed else 'evidence insufficient' if value is None or denominator==0 else 'preregistered threshold not met'))
    gate('foundation',foundation['passed'],'==',True,len(data),'both')
    for baseline in ('A8','A8-G','H42'):
        c=contrasts.get('P8-G__'+baseline,{})
        group='both' if baseline!='H42' else 'pilot'
        gate('improvement_'+baseline,c.get('mean'),'>=',.002,c.get('n',0),group)
        for kind in ('environment','reference_mc'): gate(kind+'_lower_'+baseline,c.get(kind+'_lower95'),'>',0,c.get('n',0),group)
    for p,ratio in [('p50',.8),('p95',1.)]:
        value=aggregate.get('P8-G',{}).get(p); h=aggregate.get('H32',{}).get(p)
        gate(p+'_ratio',value/h if value is not None and h else None,'<=',ratio,len(data),'both')
    dev=0
    for s in read(root/'development/states.json'):
        c=read(root/'probe'/s['state_id']/'mechanism.json'); target=c['mixed']['prefix']['ids'].index(c['target_id'])
        dev+=int(max(abs(c['mixed'][name][side][target]) for name in ('prefix','suffix')
            for side in ('candidate_effect','tail_effect'))>c['same_input_score_jitter_max'])
    gate('development_mechanism_states',dev,'>=',2,4,'training')
    new_signal=any(not r['top2_equal'] or r['P8-G_penalty']>0 or r['P8_penalty']>0 for r in permutations)
    gate('new_state_order_signal',new_signal,'==',True,len(permutations),'training')
    for first,threshold,kinds in [('P8',.002,('environment',)),('H32',.005,('environment','reference_mc'))]:
        c=contrasts.get(first+'__P8-G',{})
        for kind in kinds: gate('extra_regret_'+first+'_'+kind,c.get(kind+'_upper95'),'<=',threshold,c.get('n',0),'pilot')
    for tag,s in permutation_stats.items():
        gate(tag+'_sample',s['n'],'>=',24,s['n'],'pilot'); gate(tag+'_top2',s['top2_consistency'],'>=',.9,s['n'],'pilot')
        gate(tag+'_alignment',s['ID_all_aligned'],'==',True,s['n'],'pilot')
        for kind in ('environment','reference_mc'): gate(tag+'_penalty_'+kind,s['penalties']['P8-G'].get(kind+'_upper95'),'<=',.002,s['n'],'pilot')
    for baseline,s in mandatory.items(): gate('mandatory_'+baseline,s.get('environment_upper95'),'<=',0,s['n'],'pilot')
    training=all(c['passed'] for c in checks if c['group'] in ('both','training'))
    pilot=all(c['passed'] for c in checks if c['group'] in ('both','pilot'))
    gate_result=dict(targeted_training_eligible=training,pilot_science_gate=pilot,
        evidence_status='sufficient_sample' if len(data)>=112 else 'insufficient_evidence',foundation=foundation,checks=checks,
        closed_loop_readiness=dict(status='pilot_pending' if pilot else 'not_science_eligible',verified=False,
            prerequisites=['strategy serialization','normal weekly entry','real checkpoint action/ledger replay','legal 672–720h settlement',
                           'new paired pilot seeds and execution CRN','frozen daily executor and initial conditions']),
        next_batch='single-factor head training preregistration permitted' if training else 'training not eligible; retain diagnosis and fixed-rule engineering evidence',
        holdout_reuse='C2 cannot be used for training, model selection, calibration, or next blinded evaluation')
    result=dict(n=len(data),excluded=excluded,aggregate=aggregate,contrasts=contrasts,permutation=permutation_stats,
        permutation_clustered=clustered,rerank=rerank_aggregate,mandatory=mandatory,
        latency_P8G_minus_A8={k:aggregate['P8-G'][k]-aggregate['A8'][k] for k in ('p50','p95')} if data else {},
        counts=dict(journal.counts),forward_counts=dict(journal.forward_counts),
        forward_seconds=sum(e.get('seconds',60.) for e in entries if e['phase']=='forward'),gate=gate_result)
    write(root/'analysis/results.json',result); write(root/'gate.json',gate_result)
    csv_write(root/'STATE_METRICS.csv',metrics); csv_write(root/'METHOD_BRANCH_METRICS.csv',branch_metrics)
    csv_write(root/'CONTRASTS_BY_ENVIRONMENT.csv',compare_rows); csv_write(root/'PERMUTATION_BY_ENVIRONMENT.csv',permutations)
    csv_write(root/'RERANK_BY_ENVIRONMENT.csv',reranks); csv_write(root/'METHODS.csv',[dict(method=m,**v) for m,v in aggregate.items()])
    csv_write(root/'EXCLUSIONS.csv',excluded)
    return gate_result
