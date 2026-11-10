"""Behavioral regressions for numeric isolation, ordering, labels and barriers."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import numpy as np
from scripts.phase2d_local import unpack
from scripts.phase2ec1_common import Journal,write,read
from scripts.phase2ec1_data import soft_targets
from sim_jev.qwen_pointer import numeric,encode,structured,CandidateHeadPolicySpec
from sim_jev.candidates import Candidate
from sim_jev.decision_resolver import resolve_decision


class ByteTokenizer:
    def encode(self,text,add_special_tokens=False):
        # Character tokenizer makes the token budget stricter in this unit test.
        return list(text.encode())


class PointerDataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        p=Path('G:/gpt-file/Codex/outputs/phase2e-b-20261110-141616/states/scarce-4000-336/state.json')
        cls.request,cls.pool=unpack(read(p))

    def test_input_ignores_identity_truth_and_arbitrary_strings(self):
        p=self.pool[:2]
        altered=[]
        for i,c in enumerate(p):
            s=deepcopy(c.summary)
            s.update(seed=123,q_ref=9,checkpoint={'hidden':8},qwen_score=100)
            for m in s['missions']:
                m['id']='|candidate||decision|seed label'
            altered.append(Candidate(str(i),c.command,s))
        a,b=encode(p,ByteTokenizer()),encode(altered,ByteTokenizer())
        self.assertEqual(a['input_ids'],b['input_ids'])
        self.assertNotEqual(a['candidate_ids'],b['candidate_ids'])
        self.assertNotIn(p[0].id,a['rendered_text'])

    def test_positions_follow_actual_chunk_concatenation(self):
        a=encode(self.pool[:2],ByteTokenizer())
        b=encode(self.pool[:2][::-1],ByteTokenizer())
        self.assertEqual(a['candidate_ids'],b['candidate_ids'][::-1])
        self.assertNotEqual(a['input_ids'],b['input_ids'])
        for pos in a['candidate_positions']:
            self.assertEqual(a['input_ids'][pos-len(a['candidate_marker_ids'])+1:pos+1],a['candidate_marker_ids'])
        self.assertEqual(a['decision_position'],len(a['input_ids'])-1)

    def test_same_quantized_values_and_exact_permutation_invariant_summary(self):
        a=numeric(self.pool)
        b=numeric(self.pool[::-1])
        ca,qa=structured(a)
        cb,qb=structured(b)
        np.testing.assert_array_equal(ca,cb[::-1])
        np.testing.assert_array_equal(qa,qb)
        self.assertEqual(ca.shape,(16,24))
        self.assertEqual(qa.shape,(36,))

    def test_all_ties_split_votes_and_common_scenarios(self):
        x=np.tile(np.arange(32),(3,1)).astype(float)
        p,key=soft_targets(x,np.random.default_rng(3))
        np.testing.assert_allclose(p,np.ones(3)/3)
        x[0]+=100
        p,_=soft_targets(x,np.random.default_rng(3))
        np.testing.assert_allclose(p,[.98+.02/3,.02/3,.02/3])
        self.assertEqual(key,soft_targets(x,np.random.default_rng(3))[1])

    def test_duplicate_empty_missing_overlong_fail_closed(self):
        for pool in ([],[self.pool[0],self.pool[0]]):
            with self.assertRaises(ValueError):numeric(pool)
        class HugeTokenizer:
            def encode(self,text,add_special_tokens=False):return [1]*2049
        with self.assertRaisesRegex(ValueError,'2048'):
            encode(self.pool[:2],HugeTokenizer())
        s=deepcopy(self.pool[0].summary)
        del s['heuristic']['DSR_proxy']
        with self.assertRaises(KeyError):numeric([Candidate('x',self.pool[0].command,s)])

    def test_weekly_entry_requires_real_explicit_runtime(self):
        with self.assertRaisesRegex(ValueError,'explicit loaded runtime'):
            resolve_decision(self.request,CandidateHeadPolicySpec())

    def test_weekly_entry_empty_and_singleton_skip_des(self):
        from sim_jev.head_decision import HeadDecisionRuntime
        for pool in ([],self.pool[:1]):
            calls=[]
            def ev(request,pool,kept,b,seed):
                calls.append((kept,b))
                return [],{},{}
            runtime=HeadDecisionRuntime(backbone=None,p_head=None,s_head=None,a_model=None,
                evaluate=ev,evidence_writer=lambda r:None,slot='fixture')
            with patch('sim_jev.head_decision.generate_candidates',return_value=pool):
                result=resolve_decision(self.request,CandidateHeadPolicySpec(method='H32'),runtime)
            self.assertEqual(calls,[([],0)])
            self.assertEqual(result.record['actual_des'],0)
            self.assertEqual(result.record['forward_calls'],0)

    def test_reservations_survive_reload_and_never_reset(self):
        with tempfile.TemporaryDirectory() as d:
            write(Path(d)/'registration.json',{'fixed':True})
            j=Journal(d)
            j.reserve('forward','f')
            other=Journal(d)
            self.assertEqual(other.counts['forward'],1)
            self.assertEqual(other.entries['f']['status'],'pending')
            with self.assertRaises(ValueError):other.reserve('forward','f')

    def test_reference_does_not_evaluate_without_action_seal(self):
        from scripts.phase2ec1_experiment import reference
        with tempfile.TemporaryDirectory() as d,patch('scripts.phase2ec1_experiment.check',return_value={}),\
             patch('scripts.phase2ec1_experiment.evaluate') as ev:
            with self.assertRaises(FileNotFoundError):reference(d)
            ev.assert_not_called()


if __name__=='__main__':unittest.main()
