"""Independent audit arithmetic and a real offline run; no new heldout seeds."""
from copy import deepcopy
import math
from pathlib import Path
import tempfile
import unittest

from sim_jev.planning import PlanningInput,PlanningScenario
from scripts.phase2d_local import read,save,run_pilot_case
from scripts.phase2d_r1 import build_case,policy,capture_states
from scripts.phase2d_r1_data_audit import (
    candidate_reference,recompute_metrics,diagnostics_aggregates,_scalar,verify_pilot_run)
from scripts.phase2d_local import unpack


class IndependentR1DataAuditTests(unittest.TestCase):
    def test_known_reference_samples_paired_ties_and_regret(self):
        scores={'a':[1.,0.]*32,'b':[0.,0.]*32,'c':[1.,0.]*32}
        means,winner,rows=candidate_reference(scores,'fixture')
        self.assertEqual(means,{'a':.5,'b':0.,'c':.5});self.assertEqual(winner,'a')
        summaries={row['candidate_id']:row for row in rows}
        self.assertTrue(summaries['c']['numerical_tie']);self.assertTrue(summaries['c']['mc_approximate_tie'])
        self.assertFalse(summaries['b']['mc_approximate_tie'])
        self.assertEqual(summaries['c']['paired_difference_se'],0.)
        self.assertAlmostEqual(summaries['a']['standard_error'],math.sqrt(16/63)/8)
        state={'state_id':'fixture','scenario':'balanced','seed':10,'window_start':168,'window_end':336,
               'actual_M':3,'independent_identity':'id','cross_split_duplicate':False,'within_holdout_duplicate':False}
        action={'method':'direct-des','selected_candidate_id':'b','top_k':None,'status':'completed'}
        result=recompute_metrics(state,action,scores,means,winner)
        self.assertEqual(result['action_regret'],.5);self.assertIsNone(result['regret_at_k'])
        action.update(method='local-des',top_k=['b'])
        self.assertEqual(recompute_metrics(state,action,scores,means,winner)['regret_at_k'],.5)

    def test_missing_reference_and_duplicate_retained_ids_are_rejected(self):
        with self.assertRaisesRegex(AssertionError,'64 samples'):candidate_reference({'a':[0.]},'bad')
        state={key:None for key in ('state_id','scenario','seed','window_start','window_end','actual_M',
                                   'independent_identity','cross_split_duplicate','within_holdout_duplicate')}
        with self.assertRaisesRegex(AssertionError,'preselection'):
            recompute_metrics(state,{'top_k':['a','a'],'selected_candidate_id':'a'},{'a':[0.]*64},{'a':0.},'a')

    def test_unique_aggregate_excludes_both_duplicate_kinds(self):
        rows=[]
        for seed,point,cross,within,regret in ((10,168,False,False,.2),(10,336,False,False,.4),
                                              (11,168,True,False,1.),(11,336,False,True,1.)):
            rows.append({'scenario':'balanced','method':'local-des','status':'completed','seed':seed,
                         'window_start':point,'cross_split_duplicate':cross,'within_holdout_duplicate':within,
                         'action_regret':regret,'regret_at_k':regret,'best_candidate_recall':False})
        summary=next(row for row in diagnostics_aggregates(rows) if row['scope']=='unique_holdout' and
                     row['scenario']=='balanced' and row['method']=='local-des')
        self.assertEqual(summary['states'],2);self.assertEqual(summary['trajectories'],1)
        self.assertAlmostEqual(summary['action_regret'],.3)

    def test_real_branch_scalar_audit_rejects_changed_denominator_and_score(self):
        row=next(row for row in capture_states((10,),(168,)) if row['scenario']=='scarce')
        request,pool=unpack(row);candidate=pool[0]
        result=PlanningScenario(PlanningInput(request,20261103,0)).evaluate(candidate.command)
        _scalar(result,request,candidate,20261103)
        for mutation in (lambda value:value['counts'].update(offered=value['counts']['offered']-1),
                         lambda value:value.update(score=value['score']+.01),
                         lambda value:value.update(scenario_id='wrong')):
            bad=deepcopy(result);mutation(bad)
            with self.assertRaises(AssertionError):_scalar(bad,request,candidate,20261103)

    def test_real_offline_pilot_checkpoint_three_ledgers_and_result_corruption(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary)/'balanced-10-heuristic-des'
            run_pilot_case(directory,'balanced',10,'heuristic-des',None,
                           build_case_fn=build_case,policy_fn=policy)
            row,details,account=verify_pilot_run(directory,{'identity':None})
            self.assertEqual(row['decisions'],5);self.assertEqual(len(details),5)
            self.assertEqual(account['logical_branches'],160)
            self.assertEqual(account['actual_compute_attempts'],160)
            self.assertEqual(account['persistent_audit_identities'],160)
            result=read(directory/'result.json');result['actual_des']-=1;save(directory/'result.json',result)
            with self.assertRaisesRegex(AssertionError,'committed DES count'):verify_pilot_run(directory,{'identity':None})


if __name__=='__main__':unittest.main()
