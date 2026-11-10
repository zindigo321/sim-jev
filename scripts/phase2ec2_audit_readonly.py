"""Independent C2 audit. Reads only JSON/NPZ; never imports the simulator or model.

Run with the separate sim-jev interpreter. Its stdout can be saved as a receipt.
"""
from collections import Counter
from fractions import Fraction
import hashlib
import json
import math
from pathlib import Path
import sys
import numpy as np


def read(p): return json.loads(Path(p).read_text(encoding='utf8'))
def lines(p): return [json.loads(s) for s in Path(p).read_text(encoding='utf8').splitlines() if s.strip()]
def digest(x): return hashlib.sha256(json.dumps(x,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()).hexdigest()
def filehash(p):
    with Path(p).open('rb') as f: return hashlib.file_digest(f,'sha256').hexdigest()
def demand(value,message):
    if not value: raise ValueError(message)
def quality(r):
    c=r['counts']
    return sum((Fraction(str(c[a]))/Fraction(str(c[b])) if c[b] else Fraction(0)
        for a,b in [('success','offered'),('earned_value','offered_value')]),Fraction(0))


def validate_seal(root,name):
    s=read(root/name); demand(digest(s['files'])==s['files_sha256'],'seal digest '+name)
    for r in s['files']: demand(filehash(root/r['path'])==r['sha256'],'sealed bytes '+r['path'])
    return len(s['files'])


def intervals(matrix,reg):
    a=np.array(matrix,dtype=float)
    if not len(a): return dict(n=0,mean=None)
    if a.ndim==1: a=a.reshape(-1,1)
    count=reg['bootstrap_n']; n,b=a.shape
    averages=np.mean(a,axis=1)
    erng=np.random.default_rng(reg['environment_bootstrap_seed'])
    env=averages[erng.integers(n,size=(count,n))].mean(axis=1)
    mrng=np.random.default_rng(reg['mc_bootstrap_seed']); monte=np.zeros(count)
    for row in a: monte+=np.mean(row[mrng.integers(b,size=(count,b))],axis=1)/n
    return dict(n=n,mean=float(np.mean(averages)),environment_ci95=np.quantile(env,[.025,.975]).tolist(),
        environment_lower95=float(np.quantile(env,.05)),environment_upper95=float(np.quantile(env,.95)),
        reference_mc_ci95=np.quantile(monte,[.025,.975]).tolist(),reference_mc_lower95=float(np.quantile(monte,.05)),
        reference_mc_upper95=float(np.quantile(monte,.95)))


def same_numbers(expected,actual,where):
    for key,value in expected.items():
        if isinstance(value,(float,list)):
            demand(np.allclose(value,actual[key],atol=1e-12,rtol=0),'statistics '+where+'/'+key)
        else: demand(value==actual[key],'statistics '+where+'/'+key)


def audit(root):
    root=Path(root); reg=read(root/'registration.json'); result=read(root/'analysis/results.json'); gates=read(root/'gate.json')
    seal_counts={n:validate_seal(root,n) for n in ('protocol-seal.json','pool-seal.json','action-seal.json')}
    demand(read(root/'reference-started.json')['action_seal_sha256']==filehash(root/'action-seal.json'),'reference barrier SHA')
    started=read(root/'reference-started.json')['unix']; seal_time=read(root/'action-seal.json')['sealed_unix']
    demand(started>=seal_time,'reference opened before action seal')
    entries={}; binding=filehash(root/'registration.json')
    for row in lines(root/'compute-journal.jsonl'):
        demand(row['registration_sha256']==binding,'journal registration binding')
        old=entries.get(row['key'])
        demand((row['status']=='pending' and old is None) or (row['status'] in ('completed','failed') and old is not None and old['status']=='pending'),'journal transition')
        if row['phase'] in ('r32','r64'): demand(row['unix']>=started,'reference branch reserved before barrier')
        entries[row['key']]=row
    counts=Counter(e['phase'] for e in entries.values()); fg=Counter(e.get('forward_group') for e in entries.values() if e['phase']=='forward')
    for k,v in reg['limits'].items(): demand(counts[k]<=v,'budget '+k)
    for k,v in reg['forward_limits'].items(): demand(fg[k]<=v,'forward budget '+k)
    research=sum(counts[p] for p in ('online','permutation','rerank','r32','r64'))
    demand(research<=105984,'research total'); demand(counts['trajectory']*336<=43008,'trajectory hours')
    seconds=sum(e.get('seconds',60) for e in entries.values() if e['phase']=='forward')
    demand(seconds<=3600,'forward seconds')
    demand(dict(counts)==result['counts'],'reported compute counts')
    reference_ids=read(root/'reference-candidates.json'); top1=read(root/'top1-actions.json')
    methods=reg['methods']; data={}; raw_total=0; action_count=0; supplement_count=0; unaligned=0
    def branches(path,state,seed,ids,indices):
        nonlocal raw_total
        rows=lines(path/'branches.jsonl'); by={}; req=state['request']; legal=digest(req)
        pool={c['id']:c for c in state['pool']}; offered=[m for m in req['observation']['missions'] if 336<=m['start_time']<504]
        for row in rows:
            c,j=row['candidate_id'],row['scenario_index']; key=f'{path.relative_to(root).as_posix()}/{c}/{j}'
            demand((c,j) not in by,'duplicate branch')
            demand(key in entries and entries[key]['status']=='completed' and entries[key]['scalar_sha256']==digest(row),'raw branch/journal match')
            demand(row['legal_input_hash']==legal,'legal public input hash')
            demand(row['scenario_id']=='planning-'+digest(dict(legal_input_hash=legal,planning_seed=seed,scenario_index=j)),'scenario identity')
            demand(row['command']==pool[c]['command'],'candidate command identity')
            a=row['counts']; demand(a['offered']==len(offered),'offered count')
            demand(a['offered_value']==math.fsum(m['value'] for m in offered),'offered value')
            demand(a['accepted']+a['rejected']==a['offered'],'accepted/rejected accounting')
            demand(sum(a[k] for k in ('success','failed','accepted_not_executed','pending_execution','running'))==a['accepted'],'outcome accounting')
            demand(a['unfinished']==a['pending_execution']+a['running'],'unfinished count')
            for ratio,n,d in [('DSR','success','offered'),('RCR','earned_value','offered_value')]:
                demand(a['validity'][ratio]==bool(a[d]) and row['validity'][ratio]==bool(a[d]),'denominator validity')
                demand(a[ratio]==(a[n]/a[d] if a[d] else None),'business ratio')
            demand(abs(float(quality(row))-row['score'])<1e-12,'business score')
            demand(row['audit']['verified'] and row['audit']['score']==row['score'],'branch audit')
            demand(row['origin']==336 and row['window_end']==504 and row['duration_hours']==168,'branch horizon')
            physical=path/f'physical-{c}-{j}.json'
            if physical.exists(): demand(digest(read(physical)['final_state'])==row['physical_artifact_sha256'],'physical evidence')
            by[c,j]=row; raw_total+=1
        demand(set(by)=={(c,j) for c in ids for j in indices},'branch coverage '+str(path))
        return by
    for plan in read(root/'pool-plan.json'):
        sid=plan['state_id']
        if not plan['eligible'] or sid not in reference_ids: continue
        state=read(root/'states'/sid/'state.json'); actions={}; shared={}
        for m in methods:
            a=read(root/'online'/sid/m/'action.json'); demand(a['status']=='completed','method incomplete')
            if a['predictions'] is not None:
                demand(a['ranking_ids']==sorted(a['predictions'],key=lambda c:(-a['predictions'][c],c)),'ranking/ID mapping')
            demand(a['retained_ids']==a['ranking_ids'][:a['K']],'retained set')
            b=branches(root/'online'/sid/m,state,reg['online_seed'],a['retained_ids'],range(a['B']))
            for key,row in b.items():
                scientific={k:v for k,v in row.items() if k not in ('compute_seconds','audit_seconds')}
                if key in shared: demand(shared[key]==scientific,'independent overlapping scientific results')
                shared[key]=scientific
            scores={c:math.fsum(b[c,j]['score'] for j in range(a['B']))/a['B'] for c in a['retained_ids']}
            chosen=min(scores,key=lambda c:(-scores[c],c))
            demand(chosen==a['original_selected_id'],'C1 float selection')
            if a['K']==2:
                one,two=a['retained_ids']; d=[quality(b[two,j])-quality(b[one,j]) for j in range(4)]
                choice=two if sum(v>0 for v in d)>=3 and sum(d)/4>=Fraction(1,500) else one
                demand(a['guard_selected_id']==choice,'exact guard')
                chosen=choice if m.endswith('-G') else chosen
            demand(chosen==a['selected_candidate_id'],'final choice')
            demand(a['command']==next(c['command'] for c in state['pool'] if c['id']==chosen),'final command')
            demand(a['continuous_wall_seconds']>0 and a['rule_violations']==0 and not a['fallback'],'legality/timing')
            if m.startswith('P8'):
                f=read(root/'forwards/online'/sid/(m+'.json'))
                demand(f['actual_forward_calls']==1 and a['forward_calls']==1,'one true forward')
                demand(f['encoded']['candidate_ids']==a['input_candidate_ids'],'forward ID mapping')
                demand(len(f['encoded']['input_ids'])<=2048 and f['identity']['dtype']=='bfloat16','frozen encoding/dtype')
                demand(not f['identity']['padding'] and not f['identity']['use_cache'] and f['identity']['batch']==1,'causal runtime shape')
            actions[m]=a; action_count+=1
        for m in ('A8','P8'):
            for key in ('predictions','input_candidate_ids','ranking_ids','forward_cache_key'):
                demand(actions[m][key]==actions[m+'-G'][key],'independent guard ranking drift')
        base=set(actions['P8']['retained_ids']+actions['A8']['retained_ids']+[actions[m]['selected_candidate_id'] for m in ('H42','H32')])
        extra=set(); variants={}
        for tag,perm in read(root/'states'/sid/'orders.json')['permutations'].items():
            a=read(root/'permutation'/sid/tag/'action.json')
            demand(a['status']=='completed','permutation incomplete')
            demand(a['input_candidate_ids']==[state['pool'][i]['id'] for i in perm],'real token order mapping')
            f=read(root/'forwards/permutation'/sid/(tag+'.json'))
            demand(f['encoded']['candidate_ids']==a['input_candidate_ids'] and f['actual_forward_calls']==1,'permutation forward identity')
            branches(root/'permutation'/sid/tag,state,reg['online_seed'],a['retained_ids'],range(4))
            extra.update(a['retained_ids']); variants[tag]=a
            demand(set(a['predictions'])==set(actions['P8']['predictions']),'permutation ID coverage')
        extra-=base
        expected=dict(base=sorted(base),extra=sorted(extra),all=sorted(base|extra))
        demand(expected==reference_ids[sid] and len(base)<=6 and len(extra)<=4,'R64 frozen supplemental list')
        demand(top1[sid]==dict(P0=actions['P8']['ranking_ids'][0],A0=actions['A8']['ranking_ids'][0]),'frozen Top1')
        supplement_count+=len(base|extra)
        diag=root/'rerank'/sid/'selection.json'
        if diag.exists():
            d=read(diag); demand(d['status']=='completed','rerank failure')
            b=branches(diag.parent,state,reg['rerank_seed'],d['union'],range(16))
            for m in ('A8','P8'):
                demand(d['retained'][m]==actions[m]['retained_ids'],'fixed rerank retained set')
                for name,s in d['selections'][m].items():
                    qs={c:math.fsum(b[c,j]['score'] for j in s['indices'])/len(s['indices']) for c in d['retained'][m]}
                    demand(s['selected']==min(qs,key=lambda c:(-qs[c],c)),'B4/B8/B16 original selector')
        ids=[c['id'] for c in state['pool']]
        raw=branches(root/'reference/r32'/sid,state,reg['reference_seed'],ids,range(32))
        raw.update(branches(root/'reference/r64'/sid,state,reg['reference_seed'],expected['all'],range(32,64)))
        selected={m:a['selected_candidate_id'] for m,a in actions.items()}; selected.update(top1[sid])
        x={m:[float(quality(raw[c,j])) for j in range(64)] for m,c in selected.items()}
        exact={c:sum((quality(raw[c,j]) for j in range(64)),Fraction(0))/64 for c in expected['all']}
        q32={c:sum((quality(raw[c,j]) for j in range(32)),Fraction(0))/32 for c in ids}
        data[sid]=dict(raw=raw,actions=actions,selected=selected,x=x,exact=exact,q32=q32,variants=variants)
    demand(len(data)==result['n'],'complete environment denominator')
    recomputed={}
    for name,s in result['contrasts'].items():
        first,second=name.split('__'); matrix=[np.subtract(v['x'][first],v['x'][second]) for v in data.values()]
        calc=intervals(matrix,reg); same_numbers(calc,s,name)
        signs=[v['exact'][v['selected'][first]]-v['exact'][v['selected'][second]] for v in data.values()]
        for k,value in [('exact_wins',sum(x>0 for x in signs)),('exact_ties',sum(x==0 for x in signs)),('exact_losses',sum(x<0 for x in signs)),
                        ('different_actions',sum(v['selected'][first]!=v['selected'][second] for v in data.values()))]: demand(s[k]==value,'exact contrast tally')
        recomputed[name]=calc
    agg={}
    for m in methods+['P0','A0']:
        q=[]; screen=[]; inside=[]; total=[]; latency=[]
        for v in data.values():
            cid=v['selected'][m]; kept=v['actions'][m]['retained_ids'] if m in methods else [cid]
            full=max(v['q32'].values()); top=max(v['q32'][c] for c in kept)
            q.append(float(v['exact'][cid])); screen.append(float(full-top)); inside.append(float(top-v['q32'][cid])); total.append(float(full-v['q32'][cid]))
            if m in methods: latency.append(v['actions'][m]['continuous_wall_seconds'])
        calc=dict(Q=float(np.mean(q)),R32_screening_loss=float(np.mean(screen)),R32_within_selection_loss=float(np.mean(inside)),R32_total_regret=float(np.mean(total)))
        if latency: calc.update(p50=float(np.median(latency)),p95=float(np.quantile(latency,.95)))
        same_numbers(calc,result['aggregate'][m],m); agg[m]=calc
    independent_values={'foundation':gates['foundation']['passed'],'development_mechanism_states':0,
        'p50_ratio':agg['P8-G']['p50']/agg['H32']['p50'],'p95_ratio':agg['P8-G']['p95']/agg['H32']['p95']}
    for state in read(root/'development/states.json'):
        r=read(root/'probe'/state['state_id']/'mechanism.json'); mixes=r['mixed']
        jitter=max(abs(v) for v in mixes['repeat']['candidate_effect']+mixes['repeat']['tail_effect']+
            (np.array(mixes['repeat']['both'])-np.array(mixes['repeat']['original'])).tolist())
        target=mixes['prefix']['ids'].index(r['target_id'])
        signal=max(abs(mixes[name][side][target]) for name in ('prefix','suffix') for side in ('candidate_effect','tail_effect'))>jitter
        independent_values['development_mechanism_states']+=int(signal)
    for baseline in ('A8','A8-G','H42'):
        c=recomputed['P8-G__'+baseline]; independent_values['improvement_'+baseline]=c['mean']
        for kind in ('environment','reference_mc'): independent_values[kind+'_lower_'+baseline]=c[kind+'_lower95']
    for baseline,kinds in [('P8',('environment',)),('H32',('environment','reference_mc'))]:
        for kind in kinds: independent_values['extra_regret_'+baseline+'_'+kind]=recomputed[baseline+'__P8-G'][kind+'_upper95']
    new_signal=False
    for tag in ('reverse','random'):
        ds=[]; same=[]
        for v in data.values():
            if tag not in v['variants']: continue
            a=v['variants'][tag]; same.append(set(a['retained_ids'])==set(v['actions']['P8']['retained_ids']))
            base=v['selected']['P8-G']; target=a['guard_selected_id']
            ds.append([float(quality(v['raw'][base,j])-quality(v['raw'][target,j])) for j in range(64)])
            new_signal |= (not same[-1] or v['exact'][base]>v['exact'][target] or v['exact'][v['selected']['P8']]>v['exact'][a['selected_candidate_id']])
        c=intervals(ds,reg); same_numbers(c,result['permutation'][tag]['penalties']['P8-G'],tag)
        independent_values.update({tag+'_sample':len(ds),tag+'_top2':float(np.mean(same)) if same else None,tag+'_alignment':bool(ds)})
        for kind in ('environment','reference_mc'): independent_values[tag+'_penalty_'+kind]=c.get(kind+'_upper95')
    independent_values['new_state_order_signal']=bool(new_signal)
    for baseline in ('A8','H32'):
        a=[]
        for v in data.values():
            p,b=v['selected']['P8-G'],v['selected'][baseline]
            a.append([v['raw'][p,j]['counts']['mandatory_shortfall_as_of']-v['raw'][b,j]['counts']['mandatory_shortfall_as_of'] for j in range(64)])
        c=intervals(a,reg); same_numbers(c,result['mandatory'][baseline],'mandatory '+baseline)
        independent_values['mandatory_'+baseline]=c['environment_upper95']
    # Foundation independently derived, including all eligible slots and unknown attempts.
    eligible=sum(p['eligible'] for p in read(root/'pool-plan.json'))
    independent_values['foundation']=len(data)>=112 and len(data)==eligible and all(e['status']=='completed' or (e['status']=='failed' and e['phase']=='engineering_des') for e in entries.values()) and not read(root/'online-validation.json')['failures'] and not read(root/'reference-summary.json')['failures']
    flags={}
    for c in gates['checks']:
        value=independent_values[c['name']]
        if isinstance(value,(int,float)) and not isinstance(value,bool): demand(abs(value-c['value'])<1e-12,'gate value '+c['name'])
        else: demand(value==c['value'],'gate value '+c['name'])
        t=c['threshold']; op=c['operator']
        ok=value is not None and (value==t if op=='==' else value>t if op=='>' else value>=t if op=='>=' else value<=t)
        demand(bool(ok)==c['passed'],'gate predicate '+c['name']); flags[c['name']]=bool(ok)
    training=all(flags[c['name']] for c in gates['checks'] if c['group'] in ('both','training'))
    pilot=all(flags[c['name']] for c in gates['checks'] if c['group'] in ('both','pilot'))
    demand(training==gates['targeted_training_eligible'] and pilot==gates['pilot_science_gate'],'final gate decisions')
    demand(raw_total==research,'research raw branch coverage versus reservations')
    for e in entries.values():
        if e['phase']=='forward' and e['status']=='completed':
            f=read(root/e['artifact']); demand(digest(f)==e['scalar_sha256'] and f['actual_forward_calls']==1,'true forward journal receipt')
    return dict(status='passed',interpreter=sys.executable,read_only=True,new_des=0,new_forwards=0,
        audited_raw_branches=raw_total,actions=action_count,complete_environments=len(data),
        supplement_candidate_count=supplement_count,verified_seals=seal_counts,counts=dict(counts),research_des=research,
        forward_groups=dict(fg),forward_seconds=seconds,targeted_training_eligible=training,pilot_science_gate=pilot,
        recomputed_contrasts=len(recomputed),gate_checks=len(flags),simulator_or_model_imported=False)


if __name__=='__main__':
    print(json.dumps(audit(Path(sys.argv[1])),ensure_ascii=False))
