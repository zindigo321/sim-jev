"""Loadable weekly DecisionRequest entry shared by A8/S8/P8 and heuristics.

The callback evaluator executes audited, separately reserved real DES branches.
It does not supply precomputed scores or a hidden-state cache.
"""
from pathlib import Path
import math
import time
from .candidates import generate_candidates,heuristic_ranking
from .decision import DecisionRequest,DecisionResult
from .qwen_pointer import CandidateHeadPolicySpec,numeric,structured,PROTOCOL
from .residual_ranker import predict,ranked_ids
from .weekly_policy import validate_command,choose_command,PolicySpec


class HeadDecisionRuntime:
    def __init__(self,*,backbone,p_head,s_head,a_model,evaluate,evidence_writer,slot,
                 expected_pool=None,permutation=None):
        self.backbone,self.p_head,self.s_head = backbone,p_head,s_head
        self.a_model,self.evaluate = a_model,evaluate
        self.evidence_writer,self.slot = evidence_writer,slot
        self.expected_pool,self.permutation = expected_pool,permutation

    def decide(self,request,policy):
        if not isinstance(request,DecisionRequest) or not isinstance(policy,CandidateHeadPolicySpec):
            raise ValueError('versioned weekly request and head policy required')
        started = time.perf_counter()
        pool = sorted(generate_candidates(request,16,policy.generation_seed),key=lambda c:c.id)
        if self.expected_pool is not None and [c.to_dict() for c in pool] != self.expected_pool:
            raise ValueError('rebuilt pool differs from registered legal pool')
        for c in pool:
            if validate_command(c.command,request.observation,request.context)!=c.command:
                raise ValueError('candidate command validation failed')
        if self.permutation is not None:
            if sorted(self.permutation)!=list(range(len(pool))):
                raise ValueError('candidate permutation is not bijective')
            pool = [pool[i] for i in self.permutation]
        build_seconds = time.perf_counter()-started
        rank_start = time.perf_counter()
        scores,forward = None,None
        if pool and policy.method=='A8':
            scores = predict(self.a_model,pool)
        elif pool and policy.method in ('S8','P8'):
            import torch
            if policy.method=='P8':
                c,q,forward = self.backbone.forward(pool,self.slot)
                head = self.p_head
            else:
                c,q = [torch.tensor(v) for v in structured(numeric(pool))]
                head = self.s_head
            with torch.no_grad():
                logits = head(c,q)
                if not torch.isfinite(logits).all():
                    raise ValueError('nonfinite candidate logits')
                scores = dict(zip([v.id for v in pool],logits.tolist()))
        order = ranked_ids(scores) if scores is not None else [c.id for c in heuristic_ranking(pool)]
        rank_seconds = time.perf_counter()-rank_start
        k,b = {'H32':(4,8),'H42':(4,2),'A8':(2,4),'S8':(2,4),'P8':(2,4)}[policy.method]
        if len(pool)<2:
            b=0
        retained = order[:k]
        branches,values,timing = self.evaluate(request,pool,retained if b else [],b,policy.planning_seed)
        selected = min(values,key=lambda cid:(-values[cid],cid)) if values else (order[0] if order else None)
        command = next(c.command for c in pool if c.id==selected) if selected else choose_command(request.observation,request.context,PolicySpec())
        if validate_command(command,request.observation,request.context)!=command:
            raise ValueError('selected command failed validation')
        record = dict(protocol=PROTOCOL,method=policy.method,selected_candidate_id=selected,
            retained_ids=retained,ranking_ids=order,input_candidate_ids=[c.id for c in pool],
            predictions=scores,command=command.to_dict(),actual_des=len(branches),K=min(k,len(pool)),B=b,
            rule_violations=0,fallback=False,failure=None,token_count=len(forward['encoded']['input_ids']) if forward else 0,
            forward_calls=forward['actual_forward_calls'] if forward else 0,
            forward_seconds=forward['seconds'] if forward else 0.,
            forward_cache_key=forward['cache_key'] if forward else None,
            planning_seed=policy.planning_seed,generation_seed=policy.generation_seed,
            timing=dict(candidate_build_and_validation_seconds=build_seconds,ranking_path_seconds=rank_seconds,**timing))
        # The continuous timer includes synchronous decision evidence and its fsync.
        self.evidence_writer(record)
        record['continuous_wall_seconds'] = time.perf_counter()-started
        return DecisionResult(command,record)
