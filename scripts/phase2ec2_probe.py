"""Seen-state BF16 controls, causal interventions, and timed normal-entry probes."""
import os
from pathlib import Path
import shutil
import time
import numpy as np
from scripts.phase2ec2_common import *
from scripts.phase2ec2_prepare import environment
from scripts.phase2ec2_runtime import make_backbone,C2Runtime,evaluate,mixed_readout,scientific
from scripts.phase2d_local import unpack
from sim_jev.decision_resolver import resolve_decision
from sim_jev.qwen_pointer import encode,CandidateHeadPolicySpec,numeric
from sim_jev.candidates import heuristic_ranking
from sim_jev.residual_ranker import predict,ranked_ids


def probe(root):
    import torch
    from sim_jev.qwen_pointer_runtime import load_head
    root=Path(root); journal=Journal(root)
    if (root/'probe/result.json').exists(): return read(root/'probe/result.json')
    env=environment(); env.update(cuda_available=torch.cuda.is_available(),bf16_supported=torch.cuda.is_bf16_supported(),
        torch=torch.__version__,device=torch.cuda.get_device_name() if torch.cuda.is_available() else None)
    write(root/'probe/environment.json',env)
    if not env['cuda_available'] or not env['bf16_supported']: raise ValueError('native CUDA BF16 unavailable')
    model=make_backbone(root,journal,root/'probe/load.json'); head,_=load_head(root/'models/P11-selected.pt')
    a=read(root/'models/A.json'); reg=read(root/'registration.json'); states=read(root/'development/states.json')
    controls=[]
    for state in states:
        req,pool=unpack(state); n=len(pool)
        if n<5: raise ValueError('prefix and suffix controls require at least five candidates')
        identity=list(range(n)); prefix=identity.copy(); prefix[0],prefix[1]=prefix[1],prefix[0]
        suffix=identity.copy(); suffix[-2],suffix[-1]=suffix[-1],suffix[-2]
        orders=dict(base=identity,repeat=identity,prefix=prefix,suffix=suffix)
        evidence={}
        for name,perm in orders.items():
            perm_pool=[pool[i] for i in perm]; slot='control/'+state['state_id']+'/'+name
            c,q,f=model.forward(perm_pool,slot)
            with torch.no_grad(): logits=head(c,q).tolist()
            scores=dict(zip([v.id for v in perm_pool],logits))
            row=dict(slot=slot,permutation=perm,predictions=scores,ranking_ids=ranked_ids(scores),encoded=f['encoded'])
            write(root/'probe'/state['state_id']/(name+'.json'),row); evidence[name]=row
        enc=evidence['base']['encoded']; target=pool[2].id
        base_path=root/'vectors/control'/state['state_id']/'base.npz'
        mixed={name:mixed_readout(head,base_path,root/'vectors/control'/state['state_id']/(name+'.npz')) for name in ('repeat','prefix','suffix')}
        jitter=max(abs(v) for v in mixed['repeat']['candidate_effect']+mixed['repeat']['tail_effect']+
                   (np.array(mixed['repeat']['both'])-np.array(mixed['repeat']['original'])).tolist())
        checks={}
        for name in ('prefix','suffix'):
            e=evidence[name]['encoded']; target_row=e['candidate_ids'].index(target)
            pos=e['candidate_positions'][target_row]; base_pos=enc['candidate_positions'][2]
            # The target block is x=... followed by the candidate marker.
            start=e['candidate_positions'][target_row-1]+1; base_start=enc['candidate_positions'][1]+1
            checks[name]=dict(target_id=target,target_position=pos,base_position=base_pos,
                target_tokens_identical=e['input_ids'][start:pos+1]==enc['input_ids'][base_start:base_pos+1],
                prefix_tokens_identical=e['input_ids'][:pos+1]==enc['input_ids'][:base_pos+1])
            if pos!=base_pos or not checks[name]['target_tokens_identical']: raise ValueError('intervention moved target content/position')
        if not checks['suffix']['prefix_tokens_identical']: raise ValueError('suffix control changed target prefix')
        reverse=pool[::-1]
        a_ok=predict(a,pool)==predict(a,reverse)
        h_ok=[c.id for c in heuristic_ranking(pool)]==[c.id for c in heuristic_ranking(reverse)]
        canonical_ok=encode(sorted(reverse,key=lambda c:c.id),model.tokenizer)==enc
        quant=numeric(pool)['quantized']; aliases={}
        for c,q in zip(pool,quant): aliases.setdefault(str(q),[]).append(c.id)
        aliases=[ids for ids in aliases.values() if len(ids)>1]
        mechanism=max(abs(v) for name in ('prefix','suffix') for kind in ('candidate_effect','tail_effect') for v in mixed[name][kind])>jitter
        record=dict(state_id=state['state_id'],split=state['split'],target_id=target,checks=checks,mixed=mixed,
            same_input_score_jitter_max=jitter,mechanism_exceeds_jitter=mechanism,quantized_alias_groups=aliases,
            A8_order_invariant=a_ok,heuristic_order_invariant=h_ok,standard_canonical_input_equal=canonical_ok,
            full_pool_coverage=all(len(e['encoded']['candidate_positions'])==n and len(set(e['encoded']['candidate_ids']))==n for e in evidence.values()),
            suffix_target_vector_linf=mixed['suffix']['candidate_vector_linf'][mixed['suffix']['ids'].index(target)])
        write(root/'probe'/state['state_id']/'mechanism.json',record); controls.append(record)
        if not all((a_ok,h_ok,canonical_ok,record['full_pool_coverage'])): raise ValueError('alignment/order control failed')
        print('development control',state['state_id'],'jitter',jitter,'mechanism',mechanism,flush=True)
    state=states[0]; req,pool=unpack(state); actions=[]
    for method in METHODS:
        namespace='probe/runtime/'+method
        runtime=C2Runtime(guard=method.endswith('-G'),backbone=model,p_head=head,s_head=None,a_model=a,
            evaluate=lambda request,pool,ids,b,seed:evaluate(root,journal,state,request,pool,ids,list(range(b)),seed,'engineering_des',namespace),
            evidence_writer=lambda r:write(root/namespace/'selection-evidence.json',r),slot=namespace,expected_pool=state['pool'])
        result=resolve_decision(req,CandidateHeadPolicySpec(method=method.replace('-G',''),planning_seed=reg['online_seed']),runtime)
        write(root/namespace/'action.json',result.record); actions.append(result.record)
    overlap={}; comparisons=0
    for method in METHODS:
        for r in lines(root/'probe/runtime'/method/'branches.jsonl'):
            key=(r['candidate_id'],r['scenario_index']); value=scientific(r)
            if key in overlap:
                if overlap[key]!=value: raise ValueError('same CRN scientific branch mismatch')
                comparisons+=1
            else: overlap[key]=value
    measured=sum(r['compute_seconds']+r['audit_seconds'] for m in METHODS for r in lines(root/'probe/runtime'/m/'branches.jsonl'))/72
    footprint=sum(p.stat().st_size for p in (root/'probe/runtime').rglob('*') if p.is_file())
    result=dict(status='passed',controls=len(controls),mechanism_states=sum(r['mechanism_exceeds_jitter'] for r in controls),
        independent_online_methods=6,engineering_des=72,overlap_comparisons=comparisons,frozen=model.prove_frozen(),
        disk_free_bytes=shutil.disk_usage(root).free,probe_bytes=footprint,bytes_per_des_with_physical_samples=footprint/72,
        des_compute_audit_seconds_mean=measured,reference_upper_seconds_estimate=94208*measured,
        online_timings={r['method']:r['continuous_wall_seconds'] for r in actions},forward_counts=dict(journal.forward_counts),
        scheduling_note='C1 estimate approximately 90 minutes; measured probe extrapolation is not an online speed claim')
    write(root/'probe/execution-plan.json',dict(result,planned_research_des_max=105984,reference_des_max=94208,
        estimated_storage_upper_bytes=int(105984*footprint/72),resume='never recompute a reserved uncertain attempt'))
    write(root/'probe/result.json',result)
    return result
