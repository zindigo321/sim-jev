"""Durable campaign/gate tests. All model/HTTP values are isolated fake fixtures.

The synthetic local_inference records exercise rejection paths only and are never
research evidence. No socket, model backend, or independent tokenizer is called.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from scripts.phase2e_live import (AuditedProvider,CampaignBudget,HttpLedger,LIMITS,TIME_LIMITS,
    audit_development,audit_diagnostic_generations,audit_http,read,save)
from scripts.phase2d_local import unpack
from scripts.phase2d_r1 import capture_states
from sim_jev.local_provider import LocalError,LocalProvider
from sim_jev.provenance import canonical_sha256
from tests.test_phase2e_renderer import reversible_identity
from tests.test_phase2d_r1_scores import score_identity,scores_fixture
from tests.test_phase2d_local import test_identity,fixture as ranking_fixture
from sim_jev.local_provider import CANONICAL_RENDERER


def initial(root):
    save(root/'generation-budget.json',dict(protocol='phase2e-durable-generation-v1',
        campaign_id='isolated-test-fixture',limits=deepcopy(LIMITS),phase_max_seconds=deepcopy(TIME_LIMITS),
        max_seconds=7200,max_attempts=120,attempts=[],seconds=0.))
    save(root/'http-budget.json',dict(protocol='phase2e-local-http-v1',
        campaign_id='isolated-test-fixture',max_metadata=400,requests=[]))


class CampaignBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        initial(self.root)
        self.budget=CampaignBudget(self.root/'generation-budget.json')

    def test_pending_is_durable_before_http_and_uncertain_blocks_all_resubmission(self):
        index,deadline=self.budget.reserve('development','key',logical_request_id='logical',attempt_index=0)
        self.assertEqual(deadline,120)
        self.assertEqual(self.budget.read()['attempts'][index]['status'],'pending')
        with self.assertRaisesRegex(LocalError,'uncertain_prior_generation'):
            self.budget.reserve('diagnostics','different',logical_request_id='other',attempt_index=0)
        self.budget.finish(index,1.25,'uncertain')
        with self.assertRaisesRegex(LocalError,'uncertain_prior_generation'):
            self.budget.reserve('development','key',logical_request_id='logical',attempt_index=1)
        self.assertEqual(self.budget.read()['seconds'],1.25)

    def test_exactly_one_correction_same_phase_key_and_no_automatic_reuse(self):
        index,_=self.budget.reserve('development','a',logical_request_id='logical',attempt_index=0)
        self.budget.finish(index,.1,'completed')
        for phase,key in (('diagnostics','a'),('development','b')):
            with self.assertRaisesRegex(LocalError,'logical_request_attempt_limit'):
                self.budget.reserve(phase,key,logical_request_id='logical',attempt_index=1)
        index,_=self.budget.reserve('development','a',logical_request_id='logical',attempt_index=1)
        self.budget.finish(index,.2,'completed')
        with self.assertRaisesRegex(LocalError,'logical_request_attempt_limit'):
            self.budget.reserve('development','a',logical_request_id='logical',attempt_index=1)
        self.assertEqual(len(self.budget.read()['attempts']),2)

    def test_phase_and_total_attempt_hard_caps_preserve_all_reservations(self):
        for phase,count in (('development',40),('diagnostics',80)):
            for i in range(count):
                index,_=self.budget.reserve(phase,'key',logical_request_id=phase+str(i),attempt_index=0)
                self.budget.finish(index,0.,'completed')
            with self.assertRaisesRegex(LocalError,'local_budget_exhausted'):
                self.budget.reserve(phase,'key',logical_request_id='extra',attempt_index=0)
        self.assertEqual(len(self.budget.read()['attempts']),120)

    def test_remaining_phase_time_bounds_next_absolute_deadline(self):
        for i in range(15):
            index,_=self.budget.reserve('development','key',logical_request_id=str(i),attempt_index=0)
            self.budget.finish(index,119.,'completed')
        index,deadline=self.budget.reserve('development','key',logical_request_id='last',attempt_index=0)
        self.assertEqual(deadline,15.)
        self.budget.finish(index,15.,'completed')
        with self.assertRaisesRegex(LocalError,'local_budget_exhausted'):
            self.budget.reserve('development','key',logical_request_id='over',attempt_index=0)
        _,deadline=self.budget.reserve('diagnostics','key',logical_request_id='diag',attempt_index=0)
        self.assertEqual(deadline,120.)

    def test_invalid_duration_or_transition_cannot_modify_ledger(self):
        index,_=self.budget.reserve('development','key',logical_request_id='logical',attempt_index=0)
        before=self.budget.path.read_bytes()
        for seconds in (True,float('nan'),float('inf'),-1):
            with self.subTest(seconds=seconds),self.assertRaises(ValueError):
                self.budget.finish(index,seconds,'completed')
            self.assertEqual(before,self.budget.path.read_bytes())
        self.budget.finish(index,0.,'completed')
        with self.assertRaisesRegex(LocalError,'invalid_generation_transition'):
            self.budget.finish(index,0.,'uncertain')

    def test_http_requires_generation_reservation_and_enforces_metadata_cap(self):
        ledger=HttpLedger(self.root)
        with self.assertRaisesRegex(LocalError,'generation_http_without_reservation'):
            ledger.reserve('/api/generate',{})
        index,_=self.budget.reserve('development','key',logical_request_id='logical',attempt_index=0)
        row=ledger.reserve('/api/generate',{'model':'fixture-only'})
        self.assertEqual(read(ledger.path)['requests'][row]['generation_attempt_id'],
                         self.budget.read()['attempts'][index]['attempt_id'])
        ledger.finish(row,None,0.,error='fixture transport uncertainty')
        with self.assertRaisesRegex(LocalError,'uncertain_prior_http'):ledger.reserve('/api/version',None)
        v=read(ledger.path)
        v['requests']=[dict(kind='metadata',status='completed') for _ in range(400)]
        save(ledger.path,v)
        before=ledger.path.read_bytes()
        with self.assertRaisesRegex(LocalError,'metadata_budget_exhausted'):ledger.reserve('/api/version',None)
        self.assertEqual(before,ledger.path.read_bytes())

    def test_budget_limits_and_consumed_seconds_cannot_be_reconfigured(self):
        v=self.budget.read();v['limits']['development']=41;save(self.budget.path,v)
        with self.assertRaisesRegex(LocalError,'campaign_budget_identity_mismatch'):self.budget.read()
        initial(self.root)
        v=self.budget.read();v['seconds']=1.;save(self.budget.path,v)
        with self.assertRaisesRegex(LocalError,'campaign_time_accounting_mismatch'):self.budget.read()


class FakeCounter:
    def __init__(self):self.calls=[]
    def __call__(self,prompt):
        self.calls.append(dict(prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(),tokens=100,seconds=0.))
        return 100


class DevelopmentAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with patch.object(socket,'socket',side_effect=AssertionError('network forbidden')):
            cls.rows=capture_states((10,11),(168,672))

    def make_campaign(self,root,count=20,phase='development'):
        initial(root)
        ident=reversible_identity()
        save(root/'freeze.json',dict(identity=ident,source={'sha256':'fixture-only-no-scientific-run'}))
        save(root/'development/states.json',self.rows)
        counter=FakeCounter()
        prefix='development' if phase=='development' else 'diagnostics'
        provider=AuditedProvider(root,root/prefix/'responses',phase,base_url='http://127.0.0.1:11459',counter=counter)
        physical_calls=[]
        def fixture_http(instance,route,data=None,timeout=120):
            # The production adapter must have persisted BOTH reservations and the
            # exact raw request before this fake I/O boundary is entered.
            request=read(root/'http-budget.json')['requests'][-1]
            self.assertEqual(request['status'],'pending')
            self.assertTrue((root/'http'/f"{request['index']:04d}-request.json").is_file())
            physical_calls.append(route)
            if route=='/api/generate':
                self.assertEqual(CampaignBudget(root/'generation-budget.json').read()['attempts'][-1]['status'],'pending')
                keys=data['format']['properties']['preference_scores']['required']
                return dict(model=ident['model'],response=json.dumps({'preference_scores':{key:50 for key in keys}}),
                    done=True,done_reason='stop',prompt_eval_count=100,eval_count=10)
            if route=='/api/tags':return {'models':[{'name':ident['model'],'digest':ident['model_digest']}]}
            if route=='/api/version':return {'version':ident['runtime_version']}
            if route=='/api/ps':return {'models':[dict(name=ident['model'],digest=ident['model_digest'],size_vram=123,context_length=16384)]}
            raise AssertionError('unexpected fixture route')
        journal=[];records=[]
        with patch.object(socket,'socket',side_effect=AssertionError('network forbidden')),\
                patch.object(LocalProvider,'_http',fixture_http):
            for index in range(count):
                row=self.rows[index%8];req,pool=unpack(row)
                record=provider.rank(req,pool,force=phase=='development')
                records.append(record)
                if phase=='development':
                    save(root/'development'/f'completion-{index:02d}.json',record)
                    save(root/'development'/f'gpu-{index:02d}.json',provider.ranking_receipts[-1])
                    journal.append(dict(index=index,state_id=row['state_id'],status='completed',
                        cache_key=record['cache_key'],attempt_ids=[a['attempt_id'] for a in record['attempts']]))
        save(root/'development/live-requests.json',journal)
        self.assertEqual(physical_calls.count('/api/generate'),count if phase=='development' else min(count,8))
        return records

    def test_fake_full_gate_reconciles_every_record_and_raw_http(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);self.make_campaign(root)
            result=audit_development(root)
            self.assertEqual(result['status'],'passed')
            self.assertEqual(result['valid_completions'],20)
            self.assertEqual(result['unique_states'],8)
            self.assertEqual(result['actual_attempts'],20)
            self.assertEqual(audit_http(root)['generation_requests'],20)

    def test_passive_render_timer_preserves_legacy_scientific_record_fields(self):
        request,pool=unpack(self.rows[0])
        v3=score_identity();v3['renderer']=CANONICAL_RENDERER
        for ident,fixture in ((test_identity(),ranking_fixture),(score_identity(),scores_fixture),(v3,scores_fixture)):
            with self.subTest(renderer=ident['renderer']):
                provider=LocalProvider(ident,mode='fixture',transport=fixture)
                expected=provider.rank(request,pool)
                actual=provider.rank(request,pool)
                self.assertGreater(provider.last_render_seconds,0.)
                for field in ('protocol','identity','cache_key','rendered','ranked_ids','choice','score',
                              'probability','confidence','score_semantics','valid_local_model','valid_jev',
                              'evidence','full_request_verified','first_round_valid'):
                    self.assertEqual(canonical_sha256(expected[field]),canonical_sha256(actual[field]))
                self.assertNotIn('render_seconds',actual)
                self.assertNotIn('last_render_seconds',actual)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);self.make_campaign(root,count=1)
            receipt=read(root/'development/responses/ranking-receipts.json')[0]
            self.assertGreater(receipt['render_seconds'],0.)
            self.assertGreater(receipt['strict_validation_seconds'],0.)
            persistence=read(root/'development/responses/ranking-persistence-times.json')
            self.assertEqual(len(persistence),1)
            self.assertGreater(persistence[0]['serialization_and_atomic_persistence_seconds'],0.)

    def test_failed_middle_request_cannot_count_later_completed_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);self.make_campaign(root)
            journal=read(root/'development/live-requests.json');journal[8]['status']='failed'
            save(root/'development/live-requests.json',journal)
            result=audit_development(root)
            self.assertEqual(result['status'],'partial');self.assertEqual(result['valid_completions'],8)
            journal.append(deepcopy(journal[-1]));save(root/'development/live-requests.json',journal)
            with self.assertRaisesRegex(ValueError,'count differs'):audit_development(root)

    def test_artifact_numeric_type_mislabel_and_pending_reservation_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);self.make_campaign(root,count=1)
            budget=read(root/'generation-budget.json');row=budget['attempts'][0]
            path=root/'development/responses'/row['artifact']['path'];original=read(path)
            changed=deepcopy(original);changed['identity']['decoding']['temperature']=0.0
            self.assertEqual(original,changed)
            save(path,changed);row['artifact']['sha256']=canonical_sha256(changed);save(root/'generation-budget.json',budget)
            with self.assertRaisesRegex(ValueError,'raw attempt artifact'):audit_development(root)
            save(path,original);row['artifact']['sha256']=canonical_sha256(original);row['status']='pending'
            save(root/'generation-budget.json',budget)
            with self.assertRaisesRegex(ValueError,'reservation mismatch'):audit_development(root)

    def test_raw_http_response_and_gpu_receipt_tampering_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);self.make_campaign(root,count=1)
            gpu_path=root/'development/gpu-00.json';gpu=read(gpu_path)
            gpu['tokenizer'][0]['tokens']=99;save(gpu_path,gpu)
            with self.assertRaisesRegex(ValueError,'token receipt'):audit_development(root)
            gpu['tokenizer'][0]['tokens']=100;save(gpu_path,gpu)
            ledger=read(root/'http-budget.json');row=next(r for r in ledger['requests'] if r['kind']=='generation')
            path=root/row['artifact'];v=read(path);v['response']['eval_count']=11;save(path,v)
            from scripts.phase2d_r2_delivery import digest
            row['artifact_sha256']=digest(path);save(root/'http-budget.json',ledger)
            with self.assertRaisesRegex(ValueError,'raw generation HTTP differs'):audit_development(root)

    def test_diagnostic_same_batch_replays_have_no_additional_generation_attempts(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);records=self.make_campaign(root,count=10,phase='diagnostics')
            result=audit_diagnostic_generations(root,records)
            self.assertEqual(result['status'],'passed')
            self.assertEqual(result['local_inference'],8);self.assertEqual(result['local_replay'],2)
            self.assertEqual(result['actual_attempts'],8)
            bad=deepcopy(records);bad[-1]['attempts'][0]['raw']['eval_count']=11
            with self.assertRaisesRegex(ValueError,'replay differs'):audit_diagnostic_generations(root,bad)


if __name__=='__main__':unittest.main()
