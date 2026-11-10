"""C2 boundary, alignment, budget persistence and reference barrier regressions."""
from copy import deepcopy
from fractions import Fraction
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from scripts.phase2ec2_common import (write,read,Journal,guard_select,q_exact,supplement_ids,
    method_order,sealed,verify_seal,files_in,METHODS)


def paired_rows(deltas,denominator=1000):
    return [dict(candidate_id=c,scenario_index=j,counts=dict(success=100+(d if c=='b' else 0),
        offered=denominator,earned_value=0,offered_value=10)) for j,d in enumerate(deltas) for c in ('a','b')]


class C2Tests(unittest.TestCase):
    def test_guard_three_positive_and_exact_boundary(self):
        selected,receipt=guard_select(['a','b'],paired_rows([3,3,3,-1]))
        self.assertEqual(selected,'b')
        self.assertEqual(receipt['mean_difference']['numerator'],1)
        self.assertEqual(receipt['mean_difference']['denominator'],500)
        self.assertTrue(receipt['common_denominators'])
        self.assertEqual(guard_select(['a','b'],paired_rows([3,3,3,-2]))[0],'a')

    def test_two_positive_with_sufficient_mean_does_not_switch(self):
        self.assertEqual(guard_select(['a','b'],paired_rows([4,4,0,0]))[0],'a')
        self.assertEqual(guard_select(['a','b'],paired_rows([0,0,0,0]))[0],'a')

    def test_exact_score_ties_and_missing_denominator(self):
        first=dict(counts=dict(success=1,offered=3,earned_value=2,offered_value=3))
        second=dict(counts=dict(success=1,offered=1,earned_value=0,offered_value=0))
        self.assertEqual(q_exact(first),Fraction(1)); self.assertEqual(q_exact(first),q_exact(second))
        self.assertEqual(guard_select(['a','b'],paired_rows([3,3,3,-1])[::-1])[0],'b')

    def test_guard_fails_incomplete_duplicate_or_wrong_indices(self):
        rows=paired_rows([3,3,3,-1])
        for bad in (rows[:-1],rows+[rows[0]],rows[1:]+[rows[1]]):
            with self.assertRaises(ValueError): guard_select(['a','b'],bad)

    def test_supplemental_set_is_from_actions_not_reference(self):
        a={'P8':{'retained_ids':['a','b']},'A8':{'retained_ids':['b','c']},
           'H42':{'selected_candidate_id':'d'},'H32':{'selected_candidate_id':'e'}}
        p=[{'retained_ids':['a','f']},{'retained_ids':['g','h']}]
        self.assertEqual(supplement_ids(a,p),dict(base=['a','b','c','d','e'],extra=['f','g','h'],all=list('abcdefgh')))
        self.assertEqual(supplement_ids(a)['all'],list('abcde'))

    def test_reservation_survives_failure_and_unknown_attempt(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); write(root/'registration.json',dict(fixed=True))
            j=Journal(root); j.reserve('online','one'); j.finish('one',dict(error='expected'),failed=True)
            j.reserve('forward','forward/online/two')
            other=Journal(root)
            self.assertEqual(other.counts['online'],1); self.assertEqual(other.forward_counts['online'],1)
            for phase,key in [('online','one'),('forward','forward/online/two')]:
                with self.assertRaises(ValueError): other.reserve(phase,key)

    def test_control_subbudget_is_not_reset(self):
        with tempfile.TemporaryDirectory() as d:
            write(Path(d)/'registration.json',dict(fixed=True)); j=Journal(d)
            for i in range(40):
                j.reserve('forward','forward/control/'+str(i)); j.finish('forward/control/'+str(i),{},seconds=.1)
            with self.assertRaises(ValueError): Journal(d).reserve('forward','forward/control/extra')

    def test_seal_tampering_and_reference_before_seal_fail_closed(self):
        from scripts.phase2ec2_experiment import reference
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); write(root/'action.json',dict(chosen='a'))
            sealed(root,'test-seal.json',files_in(root,names=('action.json',)))
            (root/'action.json').write_text('{"chosen":"b"}',encoding='utf8')
            with self.assertRaises(ValueError): verify_seal(root,'test-seal.json')
            with patch('scripts.phase2ec2_experiment.evaluate') as ev:
                with self.assertRaises(FileNotFoundError): reference(root)
                ev.assert_not_called()

    def test_rotation_reverses_cycles(self):
        self.assertEqual(method_order(0),METHODS)
        self.assertEqual(method_order(1),METHODS[1:]+METHODS[:1])
        self.assertEqual(method_order(6),METHODS[::-1])
        self.assertEqual(method_order(12),METHODS)

    def test_new_runtime_goes_through_existing_resolver_and_old_scores_unchanged(self):
        from scripts.phase2d_local import unpack
        from scripts.phase2ec2_runtime import C2Runtime
        from sim_jev.head_decision import HeadDecisionRuntime
        from sim_jev.qwen_pointer import CandidateHeadPolicySpec
        from sim_jev.decision_resolver import resolve_decision
        state=read(Path('G:/gpt-file/Codex/outputs/phase2e-b-20261110-141616/states/scarce-4000-336/state.json'))
        request,pool=unpack(state)
        from sim_jev.residual_ranker import predict,ranked_ids
        model=read(Path('G:/gpt-file/Codex/outputs/phase2e-c1-20261110-181827/models/A.json'))
        order=ranked_ids(predict(model,pool)); scores={order[0]:.2,order[1]:.4}
        rows=[]
        for j in range(4):
            for cid in order[:2]: rows.append(dict(candidate_id=cid,scenario_index=j,counts=dict(success=2 if cid==order[0] else 4,offered=10,earned_value=0,offered_value=1)))
        kwargs=dict(backbone=None,p_head=None,s_head=None,a_model=model,evaluate=lambda *args:(rows,scores,{}),
            evidence_writer=lambda row:None,slot='fixture',expected_pool=state['pool'])
        old=resolve_decision(request,CandidateHeadPolicySpec(method='A8'),HeadDecisionRuntime(**kwargs))
        new=resolve_decision(request,CandidateHeadPolicySpec(method='A8'),C2Runtime(**kwargs))
        self.assertEqual(old.command,new.command)
        for key in ('predictions','ranking_ids','retained_ids','selected_candidate_id'): self.assertEqual(old.record[key],new.record[key])
        guard=resolve_decision(request,CandidateHeadPolicySpec(method='A8'),C2Runtime(guard=True,**kwargs))
        self.assertEqual(guard.record['method'],'A8-G'); self.assertEqual(guard.record['selected_candidate_id'],order[1])


if __name__=='__main__': unittest.main()
