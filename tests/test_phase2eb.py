"""2E-B feature leakage, weighting, tie, resume and label-barrier regression."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import numpy as np
from sim_jev.candidates import Candidate
from sim_jev.local_provider import LocalError, render_v4, decode_v4_input
from sim_jev.residual_ranker import features, fit, predict, ranked_ids, FEATURES
from scripts.phase2d_r1 import capture_states
from scripts.phase2d_local import unpack
from scripts.phase2eb import Journal, immutable, test_reference as generate_test_reference, test_action, jsonlines, qwen
from scripts.phase2eb_runtime import Budget, HTTP
from scripts.phase2eb_analysis import paired


class ResidualTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Fixed historical fixture, trajectory only, no candidate DES or network.
        cls.row = capture_states((10,), (168,))[0]
        cls.req, cls.pool = unpack(cls.row)

    def test_features_ignore_ids_positions_and_unregistered_outcomes(self):
        expected = features(self.pool)
        changed = []
        for i, c in enumerate(self.pool):
            s = deepcopy(c.summary)
            s.update(seed=999, checkpoint={'future':99}, q_ref=99, test_identity='test', outcome=999)
            for m in s['missions']:
                m['id'] = 'renamed'
            changed.append(Candidate('renamed-'+str(i), c.command, s))
        np.testing.assert_array_equal(features(changed), expected)
        np.testing.assert_array_equal(features(self.pool[::-1])[::-1], expected)

    def test_equal_state_weight_and_unpenalized_intercept(self):
        x = features(self.pool)
        y = np.linspace(-.3, .2, len(x))
        second = x + .2
        a = fit([(x, y), (second, y+.1)], 1.)
        b = fit([(np.repeat(x, 3, axis=0), np.repeat(y, 3)), (second, y+.1)], 1.)
        np.testing.assert_allclose(a['coef'], b['coef'], atol=1e-12)
        constant = fit([(x, np.ones(len(x))*.25)], 10.)
        np.testing.assert_allclose(constant['coef'][0], .25, atol=1e-12)
        np.testing.assert_allclose(constant['coef'][1:], 0., atol=1e-12)

    def test_qwen_ties_share_rank_and_prediction_reverses_by_id(self):
        q = {c.id: 50 if i < 3 else 80 for i,c in enumerate(self.pool)}
        x = features(self.pool, q)
        self.assertEqual(x[0,-1], x[1,-1])
        self.assertAlmostEqual(float(np.mean(x[:,-2])), 0.)
        model = fit([(x, np.linspace(-.2,.1,len(x)))], .1, qwen=True)
        self.assertEqual(predict(model,self.pool,q), predict(json.loads(json.dumps(model)),self.pool[::-1],q))
        self.assertEqual(ranked_ids({'z':1.,'a':1.}), ['a','z'])
        with self.assertRaises(ValueError):
            features(self.pool, {c.id:True for c in self.pool})

    def test_missing_and_scale_are_training_only(self):
        x = features(self.pool)
        x[:,3] = np.nan
        model = fit([(x, np.arange(len(x))*.01)], 1.)
        self.assertEqual(model['impute'][3], 0.)
        self.assertEqual(model['scale'][3], 1.)
        prior = deepcopy(model)
        predict(model,self.pool)
        self.assertEqual(model, prior)

    def test_actual_v4_wire_preserves_permutation_and_literals(self):
        p = self.pool[::-1]
        s = deepcopy(p[0].summary)
        s['literal'] = [p[0].id,'C00',{'$ref':'C00'}]
        p[0] = Candidate(p[0].id,p[0].command,s)
        rendered = render_v4(self.req,p)
        wire = json.loads(rendered['prompt'].split('<|im_start|>user\n')[1].split('\nOUTPUT CONTRACT:')[0])
        restored = decode_v4_input(wire)
        self.assertEqual(restored['candidates'],[c.to_dict() for c in p])

    def test_paired_mc_does_not_pseudoreplicate_environments(self):
        a = np.array([[.01]*32,[-.01]*32])
        r = paired(a,0)
        self.assertEqual(r['n'],2)
        self.assertEqual(r['reference_mc_ci95'],[0.,0.])
        self.assertLess(r['environment_ci95'][0],0.)
        self.assertEqual(r,paired(a,0))

    def test_six_methods_execute_separate_paired_branches_and_time_boundaries(self):
        x = features(self.pool)
        q = {c.id: 50 for c in self.pool}
        a = fit([(x,np.zeros(len(x)))],1.)
        b = fit([(features(self.pool,q),np.zeros(len(x)))],1.,qwen=True)
        record = dict(evidence='fixture', rendered={'candidate_mapping':{c.id:c.id for c in self.pool}},
                      preference_scores=q, cache_key='fixture-only')
        shared = dict(record=record,error=None,status='fixture')
        frozen = dict(generation_seed=20261102,online_seed=20261102)
        with tempfile.TemporaryDirectory() as d, patch('scripts.phase2eb.qwen',return_value=shared):
            root=Path(d);immutable(root/'freeze-sha.json',{'sha256':'fixture'})
            j=Journal(root); results=[]
            for method in ('h0','h32','h8','A8','B8','q8'):
                action,_=test_action(root,frozen,j,self.row,method,{'A':a,'B':b},None,shared)
                results.append(action)
            self.assertEqual([a['actual_des'] for a in results],[0,32,8,8,8,8])
            self.assertEqual(j.counts['test_online'],64)
            self.assertIsNone(results[-1]['continuous_wall_seconds'])
            self.assertGreater(results[4]['continuous_wall_seconds'],0)
            by={}
            for p in root.rglob('branches.jsonl'):
                for row in jsonlines(p):
                    key=(row['candidate_id'],row['scenario_index'])
                    if key in by:self.assertEqual(row['score'],by[key])
                    by[key]=row['score']

    def test_empty_singleton_skips_model_and_des_without_live_service(self):
        from tests.test_phase2e_renderer import reversible_identity
        for size in (0,1):
            pool=self.pool[:size]
            state=dict(self.row,pool=[c.to_dict() for c in pool])
            with tempfile.TemporaryDirectory() as d, patch('scripts.phase2eb.generate_candidates',return_value=pool):
                root=Path(d)
                immutable(root/'freeze-sha.json',{'sha256':'fixture'})
                immutable(root/'freeze.json',{'identity':reversible_identity()})
                action,shared=test_action(root,dict(generation_seed=20261102,online_seed=20261102),
                    Journal(root),state,'B8',{},None,None)
                self.assertFalse(action['fallback'])
                self.assertEqual(action['actual_des'],0)
                self.assertIsNone(action['predictions'])
                self.assertEqual(shared['record']['evidence'],'deterministic')
                self.assertEqual(shared['record']['attempts'],[])


class BudgetTests(unittest.TestCase):
    def setup_budget(self,root):
        immutable(root/'generation-budget.json',dict(max_attempts=208,max_seconds=5400,max_logical=104,
            slots=['s'],attempts=[],seconds=0.))
        return Budget(root,'s')

    def test_unknown_generation_is_durable_and_consumes_slot(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); b=self.setup_budget(root)
            i,t=b.reserve('new','key',logical_request_id='logical',attempt_index=0)
            self.assertEqual(t,120)
            with self.assertRaises(LocalError):
                Budget(root,'s').reserve('new','key',logical_request_id='logical',attempt_index=1)
            b.finish(i,3.,'uncertain')
            with self.assertRaises(LocalError):
                Budget(root,'s').reserve('new','key',logical_request_id='logical',attempt_index=1)

    def test_two_attempt_limit_and_no_new_logical_id(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); b=self.setup_budget(root)
            i,_=b.reserve('new','key',logical_request_id='logical',attempt_index=0);b.finish(i,1.,'completed')
            with self.assertRaises(LocalError):
                b.reserve('new','key',logical_request_id='other',attempt_index=1)
            i,_=b.reserve('new','key',logical_request_id='logical',attempt_index=1);b.finish(i,1.,'completed')
            with self.assertRaises(LocalError):
                b.reserve('new','key',logical_request_id='logical',attempt_index=2)

    def test_des_unknown_survives_reload_and_prevents_duplicate(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);immutable(root/'freeze-sha.json',{'sha256':'f'})
            j=Journal(root);j.reserve('test_online','one')
            with self.assertRaisesRegex(ValueError,'uncertain'):
                Journal(root).reserve('test_online','two')
            j.finish('one',{'score':1},'a')
            with self.assertRaises(ValueError):
                Journal(root).reserve('test_online','one')

    def test_reference_refuses_unsealed_actions_before_any_evaluate(self):
        with tempfile.TemporaryDirectory() as d, patch('scripts.phase2eb.check',return_value={}), \
             patch('scripts.phase2eb_audit.audit_online',side_effect=ValueError('seal missing')), \
             patch('scripts.phase2eb.evaluate') as evaluate:
            with self.assertRaisesRegex(ValueError,'seal missing'):
                generate_test_reference(Path(d))
            evaluate.assert_not_called()

    def test_http_requires_pending_generation_and_counts_metadata(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);self.setup_budget(root)
            immutable(root/'http-budget.json',dict(protocol='phase2eb-local-http-v1',max_metadata=450,requests=[]))
            h=HTTP(root)
            with self.assertRaises(LocalError):h.reserve('/api/generate',{})
            self.assertEqual(h.reserve('/api/version',None),0)
            with self.assertRaises(LocalError):h.reserve('/api/version',None)


if __name__=='__main__':unittest.main()
