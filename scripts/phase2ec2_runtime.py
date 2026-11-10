"""C2 runtime extends the existing resolver without changing any C1 method."""
import math
import os
from pathlib import Path
import time
import numpy as np
from scripts.phase2ec2_common import *
from sim_jev.head_decision import HeadDecisionRuntime
from sim_jev.decision import DecisionRequest,DecisionResult
from sim_jev.qwen_pointer import CandidateHeadPolicySpec
from sim_jev.candidates import generate_candidates,heuristic_ranking
from sim_jev.residual_ranker import predict,ranked_ids
from sim_jev.weekly_policy import validate_command
from sim_jev.planning import PlanningInput,PlanningScenario
from scripts.planning_verify import audit_planning_branch


def evaluate(root,journal,state,request,pool,retained,indices,seed,phase,namespace):
    """Real branches with durable raw counts. Completed references can be resumed."""
    root=Path(root); dest=root/namespace; dest.mkdir(parents=True,exist_ok=True)
    candidates={c.id:c for c in pool}; rows=lines(dest/'branches.jsonl')
    by={(r['candidate_id'],r['scenario_index']):r for r in rows}
    wanted={(c,j) for c in retained for j in indices}
    if len(by)!=len(rows) or not set(by)<=wanted: raise ValueError('duplicate or unexpected branch evidence')
    timing=dict(des_seconds=0.,audit_seconds=0.,io_seconds=0.,scenario_seconds=0.)
    for index in indices:
        scenario=None
        for cid in retained:
            key=f'{namespace}/{cid}/{index}'
            if key in journal.entries:
                e=journal.entries[key]
                if e['status']!='completed' or (cid,index) not in by or canonical_sha256(by[cid,index])!=e['scalar_sha256']:
                    raise ValueError('uncertain/failed branch retained; no automatic recomputation: '+key)
                continue
            if (cid,index) in by: raise ValueError('orphan raw branch')
            t=time.perf_counter(); journal.reserve(phase,key); timing['io_seconds']+=time.perf_counter()-t
            began=time.perf_counter()
            try:
                t=time.perf_counter()
                if scenario is None: scenario=PlanningScenario(PlanningInput(request,seed,index))
                timing['scenario_seconds']+=time.perf_counter()-t
                t=time.perf_counter(); result=scenario.evaluate(candidates[cid].command); compute=time.perf_counter()-t
                timing['des_seconds']+=compute
                t=time.perf_counter(); audit=audit_planning_branch(result,request); audit_seconds=time.perf_counter()-t
                timing['audit_seconds']+=audit_seconds
                row={k:v for k,v in result.items() if k!='final_state'}
                row.update(candidate_id=cid,physical_artifact_sha256=canonical_sha256(result['final_state']),
                    audit=audit,compute_seconds=compute,audit_seconds=audit_seconds)
                if not math.isclose(float(q_exact(row)),row['score'],rel_tol=0,abs_tol=1e-12):
                    raise ValueError('raw business score differs')
                t=time.perf_counter()
                if index==indices[0]: write(dest/f'physical-{cid}-{index}.json',result)
                append(dest/'branches.jsonl',row)
                journal.finish(key,row,(dest/'branches.jsonl').relative_to(root).as_posix(),seconds=time.perf_counter()-began)
                timing['io_seconds']+=time.perf_counter()-t
                rows.append(row); by[cid,index]=row
            except Exception as e:
                if journal.entries[key]['status']=='pending': journal.finish(key,dict(error=repr(e)),failed=True,seconds=time.perf_counter()-began)
                raise
    if set(by)!=wanted: raise ValueError('branch matrix incomplete')
    means={c:math.fsum(by[c,j]['score'] for j in indices)/len(indices) for c in retained}
    if not (dest/'cost.json').exists(): write(dest/'cost.json',dict(timing,actual_des=len(rows),simulated_hours=168*len(rows)))
    return rows,means,timing


def make_backbone(root,journal,receipt):
    from sim_jev.qwen_pointer_runtime import FrozenQwen
    from scripts.phase2ec1_assets import DEST
    class EvidenceBackbone(FrozenQwen):
        def forward(self,pool,slot):
            c,q,e=super().forward(pool,slot)
            path=Path(root)/'vectors'/(slot+'.npz'); path.parent.mkdir(parents=True,exist_ok=True)
            ids=[v.id for v in pool]; order=np.argsort(ids)
            with path.open('xb') as f:
                np.savez_compressed(f,candidates=c.numpy()[order],query=q.numpy(),ids=np.array(ids)[order],input_ids=np.array(e['encoded']['input_ids']))
                f.flush(); os.fsync(f.fileno())
            return c,q,e
    return EvidenceBackbone(DEST,journal,receipt,dtype='bfloat16')


class C2Runtime(HeadDecisionRuntime):
    def __init__(self,*,guard=False,**kwargs):
        super().__init__(**kwargs); self.guard=guard

    def decide(self,request,policy):
        if not isinstance(request,DecisionRequest) or not isinstance(policy,CandidateHeadPolicySpec):
            raise ValueError('versioned legal request required')
        started=time.perf_counter()
        pool=sorted(generate_candidates(request,16,policy.generation_seed),key=lambda c:c.id)
        if self.expected_pool is not None and [c.to_dict() for c in pool]!=self.expected_pool:
            raise ValueError('independently rebuilt pool changed')
        for c in pool:
            if validate_command(c.command,request.observation,request.context)!=c.command: raise ValueError('illegal pool command')
        if len(pool)<4: raise ValueError('C2 requires eligible M>=4')
        if self.permutation is not None:
            if sorted(self.permutation)!=list(range(len(pool))): raise ValueError('permutation not bijective')
            pool=[pool[i] for i in self.permutation]
        build=time.perf_counter()-started; t=time.perf_counter()
        forward=None; scores=None
        if policy.method=='A8': scores=predict(self.a_model,pool)
        elif policy.method=='P8':
            import torch
            c,q,forward=self.backbone.forward(pool,self.slot)
            with torch.no_grad(): logits=self.p_head(c,q)
            if not torch.isfinite(logits).all(): raise ValueError('nonfinite logits')
            scores=dict(zip([x.id for x in pool],logits.tolist()))
        elif policy.method not in ('H32','H42'): raise ValueError('unsupported C2 policy')
        order=ranked_ids(scores) if scores is not None else [c.id for c in heuristic_ranking(pool)]
        rank=time.perf_counter()-t
        k,b={'H32':(4,8),'H42':(4,2),'A8':(2,4),'P8':(2,4)}[policy.method]
        kept=order[:k]
        rows,values,timing=self.evaluate(request,pool,kept,b,policy.planning_seed)
        original=min(values,key=lambda c:(-values[c],c))
        exact_means={c:sum((q_exact(r) for r in rows if r['candidate_id']==c),Fraction(0))/b for c in kept}
        exact_selected=min(kept,key=lambda c:(-exact_means[c],c))
        guard_id,guard_evidence=guard_select(kept,rows) if k==2 else (None,None)
        selected=guard_id if self.guard else original
        command=next(c.command for c in pool if c.id==selected)
        if validate_command(command,request.observation,request.context)!=command: raise ValueError('selected command invalid')
        record=dict(protocol='phase2ec2-v1',method=policy.method+('-G' if self.guard else ''),
            selected_candidate_id=selected,original_selected_id=original,exact_mean_selected_id=exact_selected,
            float_exact_selection_differs=original!=exact_selected,guard_selected_id=guard_id,guard=guard_evidence,
            retained_ids=kept,ranking_ids=order,input_candidate_ids=[c.id for c in pool],predictions=scores,
            command=command.to_dict(),actual_des=len(rows),K=k,B=b,rule_violations=0,fallback=False,failure=None,
            forward_calls=forward['actual_forward_calls'] if forward else 0,
            forward_seconds=forward['seconds'] if forward else 0.,token_count=len(forward['encoded']['input_ids']) if forward else 0,
            forward_cache_key=forward['cache_key'] if forward else None,
            memory=forward['memory_after'] if forward else None,
            planning_seed=policy.planning_seed,generation_seed=policy.generation_seed,
            timing=dict(candidate_build_and_validation_seconds=build,ranking_path_seconds=rank,**timing))
        self.evidence_writer(record)
        record['continuous_wall_seconds']=time.perf_counter()-started
        return DecisionResult(command,record)


def scientific(row):
    return {k:v for k,v in row.items() if k not in ('compute_seconds','audit_seconds')}


def mixed_readout(head,base_path,changed_path):
    import torch
    with np.load(base_path,allow_pickle=False) as b,np.load(changed_path,allow_pickle=False) as v:
        ids=b['ids'].tolist()
        if ids!=v['ids'].tolist(): raise ValueError('mixed readout IDs differ')
        bc,bq,vc,vq=[torch.from_numpy(x.copy()) for x in (b['candidates'],b['query'],v['candidates'],v['query'])]
    with torch.no_grad():
        original=head(bc,bq).numpy(); cand=head(vc,bq).numpy(); tail=head(bc,vq).numpy(); both=head(vc,vq).numpy()
    return dict(ids=ids,original=original.tolist(),candidate_only=cand.tolist(),tail_only=tail.tolist(),both=both.tolist(),
        candidate_effect=(cand-original).tolist(),tail_effect=(tail-original).tolist(),
        interaction=(both-cand-tail+original).tolist(),candidate_vector_linf=(vc-bc).abs().max(dim=1).values.tolist(),
        tail_vector_linf=float((vq-bq).abs().max()),
        top2={name:ranked_ids(dict(zip(ids,x.tolist())))[:2] for name,x in [('original',original),('candidate_only',cand),('tail_only',tail),('both',both)]})
