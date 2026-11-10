"""Blinded, resumable C2 execution and the enforced action-to-reference barrier."""
import math
from pathlib import Path
import shutil
import time
import traceback
import numpy as np
from scripts.phase2ec2_common import *
from scripts.phase2ec2_runtime import evaluate,make_backbone,C2Runtime,mixed_readout,scientific
from scripts.phase2eb import capture
from scripts.phase2d_local import unpack
from sim_jev.qwen_pointer import CandidateHeadPolicySpec,encode,numeric
from sim_jev.decision_resolver import resolve_decision
from sim_jev.residual_ranker import predict
from sim_jev.candidates import heuristic_ranking


def failure(root,scope,sid,error):
    row=dict(scope=scope,state_id=sid,error=repr(error),traceback=traceback.format_exc(),unix=time.time())
    write(Path(root)/'failures'/f'{scope}-{sid}-{time.time_ns()}.json',row)
    return row


def build_pools(root,reg,journal,tokenizer):
    root=Path(root)
    if (root/'pool-seal.json').exists():
        verify_seal(root,'pool-seal.json'); return read(root/'pool-plan.json')
    prior=read(root/'history/public-inputs.json'); plans=[]
    for plan in reg['planned']:
        sid=plan['state_id']; key='trajectory/'+sid; path=root/'states'/sid/'state.json'
        eligibility=root/'states'/sid/'eligibility.json'
        if eligibility.exists():
            result=read(eligibility); plans.append(result)
            if path.exists(): prior.append(read(path))
            continue
        if key in journal.entries:
            # A fully written capture can be indexed without rerunning its trajectory.
            if journal.entries[key]['status']!='completed' or not path.exists():
                result=dict(plan,eligible=False,reason='construction_failed_or_uncertain',status='failed')
                write(eligibility,result); plans.append(result); continue
            state=read(path)
        else:
            journal.reserve('trajectory',key); began=time.perf_counter()
            try:
                state=capture(root,plan,prior)
                journal.finish(key,dict(state_sha256=sha(path),simulated_hours=336),path.relative_to(root).as_posix(),seconds=time.perf_counter()-began)
            except Exception as e:
                fail=failure(root,'construction',sid,e)
                journal.finish(key,fail,failed=True,seconds=time.perf_counter()-began)
                result=dict(plan,eligible=False,reason='construction_failure',status='failed')
                write(eligibility,result); plans.append(result); continue
        req,pool=unpack(state); reason=None
        if not state['primary_eligible']: reason='historical_or_batch_duplicate'
        elif len(pool)<4: reason='pool_too_small'
        elif len(pool)>16: reason='pool_too_large'
        try:
            encoded=encode(pool,tokenizer) if pool else None
            if encoded: write(root/'states'/sid/'input-encoding.json',encoded)
        except ValueError as e:
            reason='input_overlength_or_invalid'; failure(root,'input',sid,e)
        result=dict(plan,eligible=reason is None,reason=reason,status='eligible' if reason is None else 'excluded',M=len(pool))
        if result['eligible']:
            order=list(range(len(pool))); reverse=order[::-1]
            rng=np.random.default_rng(np.random.SeedSequence([reg['permutation_seed'],plan['index']]))
            random=rng.permutation(len(pool)).tolist()
            while random==order or random==reverse: random=rng.permutation(len(pool)).tolist()
            write(root/'states'/sid/'orders.json',dict(methods=method_order(plan['index']),
                permutations=dict(reverse=reverse,random=random) if plan['index'] in reg['diagnostic_slots'] else {},
                canonical_ids=[c.id for c in pool],permutation_seed=reg['permutation_seed']))
        write(eligibility,result); plans.append(result)
        print('pool',plan['index']+1,sid,result['status'],flush=True)
    write(root/'pool-plan.json',plans)
    sealed(root,'pool-seal.json',files_in(root,('states',),('pool-plan.json','registration.json','protocol-seal.json')))
    return plans


def decide(root,reg,journal,state,model,head,a,method,namespace,phase,permutation=None):
    root=Path(root); action=root/namespace/'action.json'
    if action.exists(): return read(action)
    started_path=root/namespace/'started.json'
    if started_path.exists():
        row=dict(state_id=state['state_id'],method=method,status='failed',error='interrupted online attempt; continuous timing unavailable; no rerun')
        write(action,row); return row
    write(started_path,dict(unix=time.time(),method=method,phase=phase))
    req,_=unpack(state)
    runtime=C2Runtime(guard=method.endswith('-G'),backbone=model,p_head=head,s_head=None,a_model=a,
        evaluate=lambda request,pool,ids,b,seed:evaluate(root,journal,state,request,pool,ids,list(range(b)),seed,phase,namespace),
        evidence_writer=lambda r:write(root/namespace/'selection-evidence.json',r),slot=namespace,
        expected_pool=state['pool'],permutation=permutation)
    began=time.perf_counter()
    try:
        result=resolve_decision(req,CandidateHeadPolicySpec(method=method.replace('-G',''),planning_seed=reg['online_seed']),runtime)
        record=dict(result.record,state_id=state['state_id'],status='completed',namespace=namespace)
    except Exception as e:
        fail=failure(root,'method',state['state_id']+'-'+method+'-'+phase,e)
        record=dict(fail,method=method,status='failed',failed_path_seconds=time.perf_counter()-began,namespace=namespace)
    write(action,record)
    return record


def online(root):
    from sim_jev.qwen_pointer_runtime import load_head,HardwareSampler
    root=Path(root); reg=check(root); journal=Journal(root)
    if (root/'action-seal.json').exists() or (root/'reference-started.json').exists(): raise ValueError('online closed by action/reference barrier')
    if (root/'online-summary.json').exists(): return read(root/'online-summary.json')
    attempt=str(time.time_ns()); sampler=HardwareSampler(root/'hardware'/('online-'+attempt+'.jsonl'))
    try:
        model=make_backbone(root,journal,root/'loads'/('online-'+attempt+'.json'))
        head,_=load_head(root/'models/P11-selected.pt'); a=read(root/'models/A.json')
        # Old-state warmup belongs to the control budget and cannot use a new holdout.
        _,warm=unpack(read(root/'development/states.json')[0])
        model.forward(warm,'control/online-warmup-'+attempt)
        plans=build_pools(root,reg,journal,model.tokenizer)
        failures=[]; overlaps=0
        for p in plans:
            if not p['eligible']: continue
            sid=p['state_id']; state=read(root/'states'/sid/'state.json'); orders=read(root/'states'/sid/'orders.json')
            actions={}
            for method in orders['methods']:
                actions[method]=decide(root,reg,journal,state,model,head,a,method,'online/'+sid+'/'+method,'online')
            for method in ('A8','P8'):
                one,two=actions[method],actions[method+'-G']
                if one['status']=='completed' and two['status']=='completed':
                    for key in ('input_candidate_ids','ranking_ids','predictions','forward_cache_key'):
                        if one[key]!=two[key]: failures.append(dict(state_id=sid,reason='independent guard rank drift',method=method,field=key))
            shared={}
            for method in METHODS:
                if actions[method]['status']!='completed': failures.append(actions[method]); continue
                for row in lines(root/'online'/sid/method/'branches.jsonl'):
                    key=(row['candidate_id'],row['scenario_index']); value=scientific(row)
                    if key in shared:
                        overlaps+=1
                        if shared[key]!=value: failures.append(dict(state_id=sid,reason='overlapping CRN scientific mismatch',pair=list(key)))
                    else: shared[key]=value
            req,pool=unpack(state)
            for tag,perm in orders['permutations'].items():
                ns='permutation/'+sid+'/'+tag
                r=decide(root,reg,journal,state,model,head,a,'P8',ns,'permutation',perm)
                if r['status']!='completed': failures.append(r); continue
                diagnostic=root/ns/'alignment.json'
                if not diagnostic.exists() and actions['P8']['status']=='completed':
                    mixed=mixed_readout(head,root/'vectors/online'/sid/'P8.npz',root/'vectors/permutation'/sid/(tag+'.npz'))
                    aok=predict(a,pool)==predict(a,[pool[i] for i in perm])
                    hok=[c.id for c in heuristic_ranking(pool)]==[c.id for c in heuristic_ranking([pool[i] for i in perm])]
                    write(diagnostic,dict(mixed=mixed,ID_alignment=set(r['predictions'])==set(actions['P8']['predictions']),
                        A8_order_invariant=aok,heuristic_order_invariant=hok,
                        top2_equal=set(r['retained_ids'])==set(actions['P8']['retained_ids']),
                        command_equal=r['command']==actions['P8']['command'],
                        P8G_selected_candidate_id=r['guard_selected_id'],
                        logit_min_adjacent_gap=min(abs(x-y) for x,y in zip(sorted(r['predictions'].values()),sorted(r['predictions'].values())[1:]))))
                    if not aok or not hok: failures.append(dict(state_id=sid,reason='non-Qwen permutation control failed'))
            print('online',p['index']+1,sid,dict(journal.counts),flush=True)
        frozen=model.prove_frozen()
        write(root/'online-validation.json',dict(failures=failures,overlap_comparisons=overlaps,frozen=frozen))
        result=dict(status='completed' if not failures else 'partial',eligible=sum(p['eligible'] for p in plans),
            failures=len(failures),counts=dict(journal.counts),forward_counts=dict(journal.forward_counts))
        write(root/'online-summary.json',result)
        return result
    finally: sampler.close()


def diagnose(root):
    root=Path(root); reg=check(root); verify_seal(root,'pool-seal.json'); journal=Journal(root)
    if (root/'action-seal.json').exists() or (root/'reference-started.json').exists(): raise ValueError('diagnostic action barrier closed')
    if (root/'diagnose-summary.json').exists(): return read(root/'diagnose-summary.json')
    if not (root/'online-summary.json').exists(): raise ValueError('online must finish first')
    results=[]
    for p in read(root/'pool-plan.json'):
        if p['index'] not in reg['diagnostic_slots'] or not p['eligible']: continue
        sid=p['state_id']; path=root/'rerank'/sid/'selection.json'
        if path.exists(): results.append(read(path)); continue
        try:
            actions={m:read(root/'online'/sid/m/'action.json') for m in ('P8','A8')}
            if any(a['status']!='completed' for a in actions.values()): raise ValueError('formal retained set unavailable')
            state=read(root/'states'/sid/'state.json'); req,pool=unpack(state)
            candidates=sorted(set(c for a in actions.values() for c in a['retained_ids']))
            rows,_,_=evaluate(root,journal,state,req,pool,candidates,list(range(16)),reg['rerank_seed'],'rerank','rerank/'+sid)
            selections={}
            for method,a in actions.items():
                selections[method]={}
                for name,idx in [('B4-'+str(i),list(range(i*4,(i+1)*4))) for i in range(4)]+[('B8',list(range(8))),('B16',list(range(16)))]:
                    means={c:math.fsum(r['score'] for r in rows if r['candidate_id']==c and r['scenario_index'] in idx)/len(idx) for c in a['retained_ids']}
                    selected=min(means,key=lambda c:(-means[c],c))
                    selections[method][name]=dict(selected=selected,indices=idx,float_means=means)
            record=dict(state_id=sid,status='completed',retained={k:a['retained_ids'] for k,a in actions.items()},
                union=candidates,selections=selections,actual_des=len(rows),official_candidate=False,independent_latency=False)
        except Exception as e:
            record=dict(failure(root,'rerank',sid,e),status='failed')
        write(path,record); results.append(record)
        print('rerank',sid,record['status'],flush=True)
    result=dict(status='completed' if all(r['status']=='completed' for r in results) else 'partial',
        slots=len(results),counts=dict(journal.counts))
    write(root/'diagnose-summary.json',result); return result


def seal(root):
    root=Path(root); reg=check(root); verify_seal(root,'pool-seal.json')
    if (root/'reference-started.json').exists(): raise ValueError('cannot seal after reference starts')
    if (root/'action-seal.json').exists(): return verify_seal(root,'action-seal.json')
    if not (root/'online-summary.json').exists() or not (root/'diagnose-summary.json').exists():
        raise ValueError('all online and diagnostic slots must have terminal records')
    supplement={}; top1={}; omissions=[]
    for p in read(root/'pool-plan.json'):
        if not p['eligible']: continue
        sid=p['state_id']; actions={m:read(root/'online'/sid/m/'action.json') for m in METHODS}
        if any(a['status']!='completed' for a in actions.values()): omissions.append(sid); continue
        variants=[]
        for tag in read(root/'states'/sid/'orders.json')['permutations']:
            row=read(root/'permutation'/sid/tag/'action.json')
            if row['status']=='completed': variants.append(row)
        supplement[sid]=supplement_ids(actions,variants)
        top1[sid]={m[0]+'0':actions[m]['ranking_ids'][0] for m in ('P8','A8')}
    write(root/'reference-candidates.json',supplement)
    write(root/'top1-actions.json',top1)
    shutil.copyfile(root/'compute-journal.jsonl',root/'pre-reference-compute-journal.jsonl')
    write(root/'action-summary.json',dict(complete_states=len(supplement),omitted_failed_states=omissions,
        reference_indices_base=list(range(32)),reference_indices_extra=list(range(32,64)),
        optimizer_updates=0,autoregressive_calls=0,remote_calls=0,official_jev_requests=0))
    sealed(root,'action-seal.json',files_in(root,
        ('online','permutation','rerank','vectors','forwards','models','source'),
        ('registration.json','pool-seal.json','protocol-seal.json','reference-candidates.json','top1-actions.json',
         'pre-reference-compute-journal.jsonl','online-summary.json','online-validation.json','diagnose-summary.json','action-summary.json')))
    return dict(status='actions_sealed',states=len(supplement),omissions=omissions,sha256=sha(root/'action-seal.json'))


def reference(root):
    root=Path(root); reg=check(root,action=True); journal=Journal(root)
    if (root/'reference-summary.json').exists(): return read(root/'reference-summary.json')
    if not (root/'reference-started.json').exists():
        write(root/'reference-started.json',dict(unix=time.time(),action_seal_sha256=sha(root/'action-seal.json')))
    elif read(root/'reference-started.json')['action_seal_sha256']!=sha(root/'action-seal.json'):
        raise ValueError('reference resumed with different action seal')
    wanted=read(root/'reference-candidates.json'); failures=[]; complete=[]
    for p in read(root/'registration.json')['planned']:
        sid=p['state_id']
        if sid not in wanted: continue
        try:
            state=read(root/'states'/sid/'state.json'); req,pool=unpack(state)
            evaluate(root,journal,state,req,pool,[c.id for c in pool],list(range(32)),reg['reference_seed'],'r32','reference/r32/'+sid)
            evaluate(root,journal,state,req,pool,wanted[sid]['all'],list(range(32,64)),reg['reference_seed'],'r64','reference/r64/'+sid)
            complete.append(sid)
        except Exception as e: failures.append(failure(root,'reference',sid,e))
        print('reference',len(complete),'of',len(wanted),sid,dict(journal.counts),flush=True)
    result=dict(status='completed' if not failures else 'partial',completed=complete,failures=failures,counts=dict(journal.counts))
    write(root/'reference-summary.json',result)
    return dict(status=result['status'],completed=len(complete),failures=len(failures),counts=result['counts'])
