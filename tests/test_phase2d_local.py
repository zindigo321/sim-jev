"""Offline protocol/corruption/recovery tests; no GPU, network or historical runs."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from sim_jev.candidates import generate_candidates
from sim_jev.decision import DecisionPolicySpec,DecisionResult,policy_from_dict
from sim_jev.decision_resolver import DecisionRuntime,select_from_pool,logical_decision_record
from sim_jev.local_policy import LocalDecisionPolicySpec
from sim_jev.local_provider import (LocalProvider,LocalError,GenerationBudget,identity,render,factor,unfactor,
                                    validate_ranking,validate_record,MODEL)
from sim_jev.provenance import canonical_sha256
from sim_jev.weekly import WeeklySimulation
from sim_jev.weekly_policy import PolicySpec,choose_command
from scripts.phase2c_experiment import build_case
from scripts.phase2d_local import compare_replay

def test_identity():
    return identity({'digest':'sha256:'+'a'*64,'version':{'version':'test-only'},'runtime_sha256':'b'*64,
                     'show':{'template':'unit fixture only'}},num_ctx=16384)

def fixture(body):
    return {'model':MODEL,'response':json.dumps({'ranked_ids':body['format']['properties']['ranked_ids']['items']['enum']}),
            'done':True,'done_reason':'stop','prompt_eval_count':100,'eval_count':10}

def captured(seed=10,time=168):
    requests=[]
    def capture(req,p,runtime=None):
        requests.append(req);return DecisionResult(choose_command(req.observation,req.context,p))
    with patch('sim_jev.weekly.resolve_decision',side_effect=capture):
        build_case('balanced',seed,PolicySpec()).run(time)
    req=requests[-1];return req,generate_candidates(req,16,20261102)

def fixture_provider(store=None,transport=fixture):
    return LocalProvider(test_identity(),store,mode='fixture',transport=transport)

class LocalProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.req,cls.pool=captured()

    def test_strict_old_shape_and_new_roundtrip(self):
        old=DecisionPolicySpec().to_dict();self.assertEqual(policy_from_dict(old).to_dict(),old)
        with self.assertRaises(ValueError): policy_from_dict({**old,'identity':test_identity()})
        spec=LocalDecisionPolicySpec(identity=test_identity());self.assertEqual(policy_from_dict(spec.to_dict()),spec)
        with self.assertRaises(ValueError): LocalDecisionPolicySpec.from_dict({**spec.to_dict(),'url':'secret'})
        runtime=DecisionRuntime('old-adapter','old-auditor');self.assertEqual(runtime.jev_adapter,'old-adapter');self.assertEqual(runtime.branch_auditor,'old-auditor')

    def test_lossless_float_types_and_reordered_pool(self):
        value={'x':[{'a':1,'b':1.12345678901234},{'a':1.0,'b':1.12345678901234}],'y':[1,1.0]}
        self.assertEqual(json.dumps(unfactor(factor(value)),sort_keys=True),json.dumps(value,sort_keys=True))
        self.assertEqual(render(self.req,self.pool),render(self.req,list(reversed(self.pool))))

    def test_legal_boundary_excludes_hidden_seed_and_truth(self):
        a=[]
        for seed in (10,11):
            req,pool=captured(seed,0);a.append(render(req,pool))
        self.assertEqual(a[0],a[1])
        self.assertNotIn('remaining_life_hours',json.dumps(a[0]));self.assertNotIn('due_at',json.dumps(a[0]))

    def test_reject_bad_permutations_and_duplicate_keys(self):
        for bad in ({'ranked_ids':['x','x']},{'ranked_ids':['x']},{'ranked_ids':['x','z']},{'ranked_ids':['x',1]},
                    {'ranked_ids':['x','y'],'confidence':.9},'not json','{"ranked_ids":["x","y"],"ranked_ids":["y","x"]}'):
            with self.subTest(bad=bad),self.assertRaises((ValueError,TypeError)): validate_ranking(bad,['x','y'])

    def test_fixture_not_live_and_does_not_write_cache(self):
        with tempfile.TemporaryDirectory() as d:
            p=fixture_provider(d);v=p.rank(self.req,self.pool)
            self.assertEqual(v['evidence'],'fixture');self.assertFalse(v['valid_local_model']);self.assertFalse(v['valid_jev'])
            self.assertEqual(v['local_generation_attempts'],0);self.assertEqual(list(Path(d).iterdir()),[])
            validate_record(v,self.req,self.pool,test_identity())

    def test_exactly_one_format_correction(self):
        calls=[]
        def transport(body):
            calls.append(body)
            return fixture(body) if len(calls)==2 else {'model':MODEL,'response':'{"ranked_ids":[]}'}
        v=fixture_provider(transport=transport).rank(self.req,self.pool)
        self.assertEqual(len(calls),2);self.assertEqual(len(v['attempts']),2);self.assertFalse(v['first_round_valid'])
        calls.clear()
        def always_bad(body): calls.append(body);return {'model':MODEL,'response':'{}'}
        with self.assertRaises(LocalError): fixture_provider(transport=always_bad).rank(self.req,self.pool)
        self.assertEqual(len(calls),2)

    def test_identity_timeout_and_endpoint_rejection(self):
        with self.assertRaisesRegex(LocalError,'response_model_mismatch'):
            fixture_provider(transport=lambda b:{'model':'wrong','response':'{}'}).rank(self.req,self.pool)
        def timeout(body): raise TimeoutError('unit timeout')
        with self.assertRaisesRegex(LocalError,'timeout_uncertain'): fixture_provider(transport=timeout).rank(self.req,self.pool)
        for url in ('https://example.com','http://localhost:11434','http://127.0.0.1:1/x','http://user:secret@127.0.0.1:1'):
            with self.assertRaises(ValueError): LocalProvider(test_identity(),base_url=url)
        with self.assertRaises(ValueError): LocalProvider(test_identity(),mode='live',transport=fixture)

    def test_cache_miss_and_fixture_cache_rejected_without_http(self):
        with tempfile.TemporaryDirectory() as d:
            p=LocalProvider(test_identity(),d)
            with patch.object(p,'_http',side_effect=AssertionError('network forbidden')):
                with self.assertRaisesRegex(LocalError,'local_cache_miss'):p.rank(self.req,self.pool)
                v=fixture_provider().rank(self.req,self.pool)
                Path(d,v['cache_key']+'.json').write_text(json.dumps(v))
                with self.assertRaisesRegex(LocalError,'cache_identity_mismatch'):p.rank(self.req,self.pool)

    def test_corrupt_response_and_cache_identity(self):
        v=fixture_provider().rank(self.req,self.pool)
        for mutate in (lambda x:x['identity'].update(model_digest='wrong'),lambda x:x.update(cache_key='bad'),
                       lambda x:x['ranked_ids'].reverse(),lambda x:x.update(valid_jev=True),
                       lambda x:x['attempts'][0]['body'].update(prompt='changed public input'),
                       lambda x:x['attempts'][0]['body'].update(format={'type':'object'}),
                       lambda x:x.update(probability=.8),lambda x:x.update(valid_local_model=True)):
            bad=deepcopy(v);mutate(bad)
            with self.assertRaises((ValueError,LocalError)): validate_record(bad,self.req,self.pool,test_identity())

    def test_failed_fixture_never_writes_response_directory(self):
        with tempfile.TemporaryDirectory() as d:
            def timeout(body):raise TimeoutError('fixture timeout')
            with self.assertRaises(LocalError):fixture_provider(d,timeout).rank(self.req,self.pool)
            self.assertEqual(list(Path(d).iterdir()),[])

    def test_persisted_budget_counts_uncertainty_and_time(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'budget.json'; b=GenerationBudget(path,{'development':2},10)
            i,timeout=b.reserve('development','one');self.assertEqual(timeout,10)
            with self.assertRaisesRegex(LocalError,'uncertain_prior_generation'):GenerationBudget(path,{'development':2},10).reserve('development','two')
            b.finish(i,6,'completed');i,_=b.reserve('development','two');b.finish(i,4,'completed')
            with self.assertRaisesRegex(LocalError,'local_budget_exhausted'):b.reserve('development','three')
        with tempfile.TemporaryDirectory() as d:
            b=GenerationBudget(Path(d)/'b.json',{'development':4},10);i,_=b.reserve('development','x');b.finish(i,2,'uncertain')
            with self.assertRaisesRegex(LocalError,'uncertain_prior_generation'):b.reserve('development','y')

    def test_local_budgets_and_checkpoint_no_recommit(self):
        for method,expected in (('local-direct',0),('local-des',32)):
            with self.subTest(method=method):
                spec=LocalDecisionPolicySpec(method=method,identity=test_identity());p=fixture_provider()
                result=select_from_pool(self.req,spec,self.pool,DecisionRuntime(local_provider=p))
                self.assertEqual(result.record['budget']['actual_des'],expected)
                sim=build_case('balanced',10,spec,DecisionRuntime(local_provider=p));sim.run(0);checkpoint=sim.to_checkpoint()
                with patch('sim_jev.weekly.resolve_decision',side_effect=AssertionError('already committed')):
                    restored=WeeklySimulation.from_checkpoint(checkpoint);restored.run(24)
                self.assertEqual(len(restored.weekly_state['weekly_commands']),1)

    def test_cache_saved_before_command_commit_and_fresh_process_restore(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);spec=LocalDecisionPolicySpec(method='local-direct',identity=test_identity())
            sim=build_case('balanced',10,spec,DecisionRuntime(local_provider=fixture_provider()))
            sim.run(0);save=sim.to_checkpoint();(root/'checkpoint.json').write_text(json.dumps(save))
            code='from sim_jev.weekly import WeeklySimulation; import json,sys; from unittest.mock import patch; s=WeeklySimulation.from_checkpoint(json.load(open(sys.argv[1]))); s.run(24); print(len(s.weekly_state["weekly_commands"]))'
            r=subprocess.run([sys.executable,'-c',code,str(root/'checkpoint.json')],capture_output=True,text=True)
            self.assertEqual(r.returncode,0,r.stderr);self.assertEqual(r.stdout.strip(),'1')
            failed=build_case('balanced',10,spec,DecisionRuntime(local_provider=LocalProvider(test_identity(),root/'missing')))
            with self.assertRaises(LocalError): failed.run(0)
            self.assertEqual(failed.weekly_state['weekly_commands'],[])

    def test_synthetic_transport_production_cache_and_cross_process_replay(self):
        # Deliberately synthetic unit evidence, confined to a temporary directory.
        # Production cache machinery is exercised; this never counts as GPU proof.
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);ident=test_identity();budget=GenerationBudget(root/'budget.json')
            p=LocalProvider(ident,root/'responses',mode='live',budget=budget,token_counter=lambda _:100)
            def fake_http(route,data=None,timeout=120):
                if route=='/api/tags':return {'models':[{'name':MODEL,'digest':ident['model_digest']}]}
                if route=='/api/version':return {'version':'test-only'}
                return fixture(data)
            spec=LocalDecisionPolicySpec(method='local-direct',identity=ident)
            sim=build_case('balanced',10,spec,DecisionRuntime(local_provider=p))
            with patch.object(p,'_http',side_effect=fake_http):
                sim.run(0);checkpoint=sim.to_checkpoint();sim.run(720)
            (root/'start.json').write_text(json.dumps(checkpoint));(root/'end.json').write_text(json.dumps(sim.to_checkpoint()))
            self.assertEqual(p.generations,5)
            code='''import json,sys
from pathlib import Path
from unittest.mock import patch
from sim_jev.weekly import WeeklySimulation
from sim_jev.local_provider import LocalProvider
from sim_jev.decision_resolver import DecisionRuntime
from scripts.phase2d_local import compare_replay
r=Path(sys.argv[1]);s=WeeklySimulation.from_checkpoint(json.loads((r/'start.json').read_text()))
p=LocalProvider(s.weekly_state['policy_spec']['identity'],r/'responses',mode='cache_only')
s._decision_runtime=DecisionRuntime(local_provider=p)
with patch.object(p,'_http',side_effect=AssertionError('network forbidden')):s.run(720)
expected=WeeklySimulation.from_checkpoint(json.loads((r/'end.json').read_text()))
compare_replay(expected,s)
assert p.generations==0 and p.metadata_queries==0
print('zero-inference exact replay passed')
'''
            child=subprocess.run([sys.executable,'-c',code,str(root)],capture_output=True,text=True)
            self.assertEqual(child.returncode,0,child.stderr);self.assertIn('passed',child.stdout)
            replay=LocalProvider(ident,root/'responses',mode='cache_only')
            req,pool=captured(10,0)
            self.assertEqual(replay.rank(req,pool)['local_generation_attempts'],0)
            # A generation result that was saved before an interrupted commit can
            # be reused when rebuilding from the last stable boundary.
            fresh=build_case('balanced',10,spec,DecisionRuntime(local_provider=replay));fresh.run(0)
            self.assertEqual(fresh.weekly_state['weekly_commands'],checkpoint['weekly_state']['weekly_commands'])

if __name__=='__main__':unittest.main()
