"""R1 score contract and actual durable provider paths; entirely offline fixtures."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from sim_jev.candidates import generate_candidates
from sim_jev.decision import DecisionRequest,policy_from_dict
from sim_jev.decision_resolver import DecisionRuntime,select_from_pool,logical_decision_record
from sim_jev.local_policy import LocalDecisionPolicySpec,LOCAL_SCORE_POLICY_VERSION
from sim_jev.local_provider import (
    MODEL,SCORE_PROTOCOL,SCORE_TEMPLATES,LocalError,LocalProvider,GenerationBudget,
    identity_v2,render_v2,render_for_identity,score_schema,validate_preference_scores,
    validate_identity,validate_record,unfactor)
from sim_jev.provenance import canonical_sha256
from tests.test_phase2d_local import captured,test_identity,fixture as old_fixture


def score_identity(template='public-portfolio-scores-v2-a'):
    return identity_v2({'digest':'sha256:'+'a'*64,'version':{'version':'test-only'},
                       'runtime_sha256':'b'*64,'show':{'template':'unit fixture only'}},
                       template=template,tokenizer_sha256='c'*64,
                       tokenizer_runtime={'python':'offline-test','tokenizers':'offline-test'})


def scores_fixture(body):
    keys=body['format']['properties']['preference_scores']['required']
    return {'model':MODEL,'response':json.dumps({'preference_scores':{key:50 for key in keys}}),
            'done':True,'done_reason':'stop','prompt_eval_count':100,'eval_count':10}


class ScoreContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        old,_=captured()
        cls.req=DecisionRequest.create(old.observation,old.context,old.parameters,
                                       protocol_version='legal-weekly-request-v2')
        cls.pool=generate_candidates(cls.req,16,20261102)
        cls.ident=score_identity()

    def test_schema_has_required_candidate_keys_no_extra(self):
        value=score_schema(['C00','C01'])
        self.assertFalse(value['additionalProperties'])
        table=value['properties']['preference_scores']
        self.assertEqual(table['required'],['C00','C01'])
        self.assertFalse(table['additionalProperties'])
        self.assertEqual(table['properties']['C00'],{'type':'integer','minimum':0,'maximum':100})

    def test_stable_sort_uses_original_id_and_all_ties_are_valid(self):
        mapping={'C00':'z','C01':'a','C02':'m'}
        value=validate_preference_scores('{"preference_scores":{"C02":50,"C00":50,"C01":50}}',mapping)
        self.assertEqual(value['ranked_ids'],['a','m','z'])
        self.assertTrue(value['ties']['all_tied']);self.assertEqual(value['ties']['tied_pair_fraction'],1.)
        value=validate_preference_scores({'preference_scores':{'C00':100,'C01':0,'C02':100}},mapping)
        self.assertEqual(value['ranked_ids'],['m','z','a'])
        self.assertFalse(value['ties']['all_tied']);self.assertEqual(value['ties']['tied_pairs'],1)

    def test_duplicate_keys_are_rejected_at_both_levels(self):
        for raw in ('{"preference_scores":{"C00":1,"C00":2}}',
                    '{"preference_scores":{"C00":1},"preference_scores":{"C00":1}}'):
            with self.subTest(raw=raw),self.assertRaisesRegex(ValueError,'duplicate'):validate_preference_scores(raw,{'C00':'x'})

    def test_noninteger_bool_float_nonfinite_and_range_rejected(self):
        for literal in ('true','false','1.0','1e1','NaN','Infinity','-Infinity','101','-1','null','"30"'):
            with self.subTest(literal=literal),self.assertRaises((ValueError,TypeError)):
                validate_preference_scores('{"preference_scores":{"C00":'+literal+'}}',{'C00':'x'})

    def test_missing_extra_and_wrong_top_level_rejected(self):
        values=({'preference_scores':{}},{'preference_scores':{'C00':1,'C01':2}},
                {'preference_scores':{'C00':1},'confidence':None},{'C00':1},
                {'preference_scores':[1]},[],None)
        for value in values:
            with self.subTest(value=value),self.assertRaises((ValueError,TypeError)):validate_preference_scores(value,{'C00':'x'})

    def test_render_lossless_reverse_mapping_and_template_separation(self):
        first=render_v2(self.req,self.pool)
        second=render_v2(self.req,list(reversed(self.pool)))
        self.assertEqual(first,second)
        decoded=unfactor(first['rendered_input']);mapping=first['candidate_mapping']
        def expand(x):
            if isinstance(x,dict):return {k:expand(v) for k,v in x.items()}
            if isinstance(x,list):return [expand(v) for v in x]
            return mapping.get(x,x) if isinstance(x,str) else x
        self.assertEqual(expand(decoded),first['canonical'])
        alternative=render_v2(self.req,self.pool,'public-portfolio-scores-v2-b')
        self.assertNotEqual(first['prompt_sha256'],alternative['prompt_sha256'])
        self.assertEqual(first['canonical'],alternative['canonical'])

    def test_identity_rejects_schema_sort_template_arithmetic_tampering(self):
        for key in ('protocol','sort_rule','template_sha256','correction_sha256','schema_builder_sha256',
                    'arithmetic_protocol','request_protocol','tokenizer_sha256','renderer'):
            with self.subTest(key=key),self.assertRaises(ValueError):
                bad=deepcopy(self.ident);bad[key]='changed';validate_identity(bad)
        self.assertNotEqual(self.ident,score_identity('public-portfolio-scores-v2-b'))

    def test_policy_protocol_pairing_and_legacy_request_rejected(self):
        with self.assertRaisesRegex(ValueError,'protocol version mismatch'):LocalDecisionPolicySpec(identity=self.ident)
        spec=LocalDecisionPolicySpec(identity=self.ident,version=LOCAL_SCORE_POLICY_VERSION)
        self.assertEqual(policy_from_dict(spec.to_dict()),spec)
        with self.assertRaisesRegex(ValueError,'protocol version mismatch'):
            LocalDecisionPolicySpec(identity=test_identity(),version=LOCAL_SCORE_POLICY_VERSION)
        old,pool=captured()
        with self.assertRaisesRegex(LocalError,'request_protocol_mismatch'):
            LocalProvider(self.ident,mode='fixture',transport=scores_fixture).rank(old,pool)

    def test_fixture_never_writes_live_evidence_and_scores_are_recomputable(self):
        with tempfile.TemporaryDirectory() as directory:
            p=LocalProvider(self.ident,directory,mode='fixture',transport=scores_fixture)
            value=p.rank(self.req,self.pool)
            self.assertFalse(value['valid_local_model']);self.assertFalse(value['valid_jev'])
            self.assertEqual(value['evidence'],'fixture');self.assertEqual(list(Path(directory).iterdir()),[])
            parsed=validate_preference_scores(value['attempts'][-1]['raw']['response'],value['rendered']['candidate_mapping'])
            self.assertEqual(value['ranked_ids'],parsed['ranked_ids'])
            self.assertEqual(value['preference_scores'],parsed['preference_scores'])
            validate_record(value,self.req,self.pool,self.ident)

    def test_one_fixed_correction_and_no_repair_of_bad_scores(self):
        calls=[]
        def transport(body):
            calls.append(body)
            return scores_fixture(body) if len(calls)==2 else {'model':MODEL,'response':'{"preference_scores":{}}'}
        result=LocalProvider(self.ident,mode='fixture',transport=transport).rank(self.req,self.pool)
        self.assertEqual(len(calls),2);self.assertFalse(result['first_round_valid'])
        validate_record(result,self.req,self.pool,self.ident)
        calls.clear()
        def bad(body):calls.append(body);return {'model':MODEL,'response':'{}'}
        with self.assertRaisesRegex(LocalError,'invalid_local_scores'):
            LocalProvider(self.ident,mode='fixture',transport=bad).rank(self.req,self.pool)
        self.assertEqual(len(calls),2)

    def test_raw_score_and_program_rank_corruption_rejected(self):
        value=LocalProvider(self.ident,mode='fixture',transport=scores_fixture).rank(self.req,self.pool)
        for change in (lambda v:v['ranked_ids'].reverse(),lambda v:v['preference_scores'].update(C00=49),
                       lambda v:v['ties'].update(all_tied=False),lambda v:v.update(sort_rule='changed'),
                       lambda v:v.update(first_round_valid=False),lambda v:v.update(protocol='local-ranking-v1')):
            bad=deepcopy(value);change(bad)
            with self.assertRaises((ValueError,LocalError)):validate_record(bad,self.req,self.pool,self.ident)

    def test_empty_singleton_skip_model_and_do_not_claim_coverage(self):
        for pool in ([],self.pool[:1]):
            p=LocalProvider(self.ident,mode='fixture',transport=lambda b:(_ for _ in ()).throw(AssertionError('must skip')))
            value=p.rank(self.req,pool)
            self.assertEqual(value['local_generation_attempts'],0);self.assertFalse(value['valid_local_model'])
            self.assertEqual(value['evidence'],'deterministic');self.assertIsNone(value['preference_scores'])
            validate_record(value,self.req,pool,self.ident)

    def test_resolver_uses_scores_and_retains_science_in_logical_record(self):
        for method,count in (('local-direct',0),('local-des',32)):
            spec=LocalDecisionPolicySpec(method=method,identity=self.ident,version=LOCAL_SCORE_POLICY_VERSION)
            p=LocalProvider(self.ident,mode='fixture',transport=scores_fixture)
            result=select_from_pool(self.req,spec,self.pool,DecisionRuntime(local_provider=p))
            self.assertEqual(result.record['budget']['actual_des'],count)
            self.assertEqual(result.record['ranking_ids'],sorted(candidate.id for candidate in self.pool))
            projected=logical_decision_record(result.record)['local']
            self.assertEqual(projected['preference_scores'],result.record['local']['preference_scores'])
            self.assertIn('ties',projected)

    def test_force_generations_keep_distinct_attempts_and_fresh_process_cache(self):
        # Synthetic transport is explicitly patched only for this temporary
        # production-path control; never accepted as a real model gate.
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);budget=GenerationBudget(root/'generation-budget.json')
            p=LocalProvider(self.ident,root/'responses',mode='live',budget=budget,token_counter=lambda _:100)
            def fake_http(route,data=None,timeout=120):
                if route=='/api/tags':return {'models':[{'name':MODEL,'digest':self.ident['model_digest']}]}
                if route=='/api/version':return {'version':'test-only'}
                return scores_fixture(data)
            with patch.object(p,'_http',side_effect=fake_http):
                first=p.rank(self.req,self.pool,force=True);second=p.rank(self.req,self.pool,force=True)
            self.assertNotEqual(first['attempts'][0]['attempt_id'],second['attempts'][0]['attempt_id'])
            self.assertEqual(len(list((root/'responses').glob('attempt-*.json'))),2)
            self.assertEqual(len(budget.read()['attempts']),2)
            for row in budget.read()['attempts']:
                artifact=json.loads((root/'responses'/row['artifact']['path']).read_text())
                self.assertEqual(row['artifact']['sha256'],canonical_sha256(artifact))
            validate_record(first,self.req,self.pool,self.ident)
            from scripts.phase2d_r1_data_audit import verify_generation_budget
            capture=root/'development'/'template'/'completion-00.json';capture.parent.mkdir(parents=True)
            capture.write_text(json.dumps(first))
            self.assertEqual(verify_generation_budget(root)['actual_generations'],2)
            counterfeit=deepcopy(first);counterfeit['attempts'][0]['attempt_id']='d'*32
            capture.write_text(json.dumps(counterfeit))
            with self.assertRaisesRegex(AssertionError,'campaign attempt'):verify_generation_budget(root)
            capture.write_text(json.dumps(first))
            (root/'input.json').write_text(json.dumps({'request':self.req.to_dict(),'identity':self.ident}))
            script='''import json,sys,socket
from pathlib import Path
from unittest.mock import patch
from sim_jev.decision import DecisionRequest
from sim_jev.candidates import generate_candidates
from sim_jev.local_provider import LocalProvider
r=Path(sys.argv[1]);v=json.loads((r/'input.json').read_text());req=DecisionRequest.from_dict(v['request'])
p=LocalProvider(v['identity'],r/'responses',mode='cache_only')
with patch('socket.socket',side_effect=AssertionError('network forbidden')):
 value=p.rank(req,generate_candidates(req,16,20261102))
assert value['evidence']=='local_replay' and p.generations==0 and p.metadata_queries==0
print(value['cache_key'])
'''
            child=subprocess.run([sys.executable,'-c',script,str(root)],capture_output=True,text=True)
            self.assertEqual(child.returncode,0,child.stderr);self.assertEqual(child.stdout.strip(),second['cache_key'])
            # Copy a v1 record under a v2 key: validation must reject it.
            old,oldpool=captured();legacy=LocalProvider(test_identity(),mode='fixture',transport=old_fixture).rank(old,oldpool)
            (root/'responses'/(second['cache_key']+'.json')).write_text(json.dumps(legacy))
            with self.assertRaisesRegex(LocalError,'cache_identity_mismatch'):LocalProvider(self.ident,root/'responses').rank(self.req,self.pool)

    def test_budget_is_shared_across_templates_restarts_and_uncertainty(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'budget.json';first=GenerationBudget(path,{'development':2},10)
            index,_=first.reserve('development','template-a',logical_request_id='one',attempt_index=0)
            first.finish(index,2,'completed')
            second=GenerationBudget(path,{'development':2},10)
            index,_=second.reserve('development','template-b',logical_request_id='two',attempt_index=0)
            second.finish(index,2,'uncertain')
            with self.assertRaisesRegex(LocalError,'uncertain_prior_generation'):first.reserve('development','retry')
            with self.assertRaisesRegex(LocalError,'campaign_budget_identity_mismatch'):
                GenerationBudget(path,{'development':80},7200).reserve('development','reset')
            self.assertEqual(len(first.read()['attempts']),2)
            self.assertEqual(first.read()['seconds'],4)

    def test_crash_before_attempt_artifact_leaves_pending_and_no_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);budget=GenerationBudget(root/'budget.json')
            p=LocalProvider(self.ident,root/'responses',mode='live',budget=budget,token_counter=lambda _:100)
            with patch.object(p,'verify_model'),patch.object(p,'_generate',side_effect=lambda body,timeout:scores_fixture(body)):
                from sim_jev.local_provider import _atomic_json
                def fail_artifact(path,value):
                    if Path(path).name.startswith('attempt-'):raise OSError('injected durability fault')
                    return _atomic_json(path,value)
                with patch('sim_jev.local_provider._atomic_json',side_effect=fail_artifact),self.assertRaises(OSError):p.rank(self.req,self.pool)
            self.assertEqual(budget.read()['attempts'][0]['status'],'pending')
            with self.assertRaisesRegex(LocalError,'uncertain_prior_generation'):budget.reserve('development','again')


if __name__=='__main__':unittest.main()
