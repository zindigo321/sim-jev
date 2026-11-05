"""Strict R2 audit behavior on isolated offline data; no historical/live counts."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from scripts.phase2d_history import HistoricalR1Binding,checked_zip_entries
from scripts.phase2d_r1_data_audit import factory_inputs,factory_record,_account,read
from scripts.phase2d_r2 import offline_evidence
from scripts.phase2d_local import save,unpack
from scripts.planning_verify import audit_planning_branch
from sim_jev.branch_audit_store import BranchAuditStore
from sim_jev.candidates import generate_candidates
from sim_jev.decision import DecisionRequest
from sim_jev.local_provider import LocalProvider,validate_record,LocalError
from sim_jev.planning import PlanningInput,PlanningScenario
from sim_jev.provenance import canonical_sha256
from tests.test_phase2d_local import captured
from tests.test_phase2d_r1_scores import score_identity,scores_fixture


class StrictR2AuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        old,_=captured()
        cls.request=DecisionRequest.create(old.observation,old.context,old.parameters,
            protocol_version='legal-weekly-request-v2')
        cls.pool=sorted(generate_candidates(cls.request,16,20261102),key=lambda c:c.id)
        cls.state={'request':cls.request.to_dict(),'pool':[item.to_dict() for item in cls.pool]}
        cls.identity=score_identity()
        cls.record=LocalProvider(cls.identity,mode='fixture',transport=scores_fixture).rank(cls.request,cls.pool)

    def test_factory_reconstruction_requires_all_values_arrays_and_hashes_equal(self):
        restored=json.loads(json.dumps(self.state,sort_keys=True))
        request,pool=factory_inputs(restored,self.request)
        self.assertEqual(request,self.request)
        self.assertEqual([item.to_dict() for item in pool],self.state['pool'])
        self.assertEqual(canonical_sha256(restored),canonical_sha256(self.state))
        self.assertNotEqual(list(restored['request']['observation']),list(self.state['request']['observation']))
        view=factory_record({**restored,'arbitrary_untouched_field':['exact',1.,0]},request,pool)
        self.assertEqual(view,{**restored,'arbitrary_untouched_field':['exact',1.,0]})
        validate_record(self.record,request,pool,self.identity)

    def test_changed_public_values_or_candidate_array_rejected_before_render(self):
        mutations=(lambda row:row['request']['observation']['inventory'].update(on_hand=999),
                   lambda row:row['pool'].reverse(),
                   lambda row:row['pool'][0]['summary'].update(extra='must not disappear'))
        for change in mutations:
            row=deepcopy(self.state);change(row)
            with self.subTest(change=change),self.assertRaises(AssertionError):factory_inputs(row,self.request)

    def test_canonical_proof_rejects_python_equal_numeric_type_substitution(self):
        row=deepcopy(self.state)
        original=row['request']['observation']['time']
        row['request']['observation']['time']=int(original)
        self.assertEqual(row,self.state)
        if canonical_sha256(row)==canonical_sha256(self.state):
            row['request']['observation']['time']=float(original)
        with self.assertRaisesRegex(AssertionError,'canonical'):factory_inputs(row,self.request)

    def test_original_scores_rank_prompt_and_unknown_renderer_are_strict(self):
        def changed_scores(value):
            raw=json.loads(value['attempts'][-1]['raw']['response'])
            first=next(iter(raw['preference_scores']));raw['preference_scores'][first]=True
            value['attempts'][-1]['raw']['response']=json.dumps(raw)
        changes=(changed_scores,
                 lambda value:value['ranked_ids'].reverse(),
                 lambda value:value['attempts'][0]['body'].update(prompt='changed'),
                 lambda value:value['rendered'].update(prompt='changed'))
        for change in changes:
            value=deepcopy(self.record);change(value)
            with self.subTest(change=change),self.assertRaises((ValueError,LocalError)):
                validate_record(value,self.request,self.pool,self.identity)
        identity=deepcopy(self.identity);identity['renderer']='unknown-renderer'
        with self.assertRaises(ValueError):validate_record(self.record,self.request,self.pool,identity)
        with self.assertRaisesRegex(ValueError,'unknown historical'):
            HistoricalR1Binding('unused','unused',version='unknown-version')

    def test_physical_hash_and_missing_durable_audit_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary)/'original';journal=BranchAuditStore(directory,audit_planning_branch)
            scenario=PlanningScenario(PlanningInput(self.request,20261102,0));candidate=self.pool[0]
            journal.before_branch(self.request,candidate,0,scenario)
            result=scenario.evaluate(candidate.command);journal(result,self.request)
            artifact=result.pop('final_state')
            branch={**result,'candidate_id':candidate.id,'physical_artifact_sha256':canonical_sha256(artifact)}
            record={'request':self.request.to_dict(),'branches':[branch]}
            account=journal.reconcile({'weekly_state':{'decision_records':[record]}})
            save(directory/'branch-accounting.json',account)
            self.assertEqual(_account(directory,[record])['logical_branches'],1)
            corrupted=deepcopy(record);corrupted['branches'][0]['physical_artifact_sha256']='0'*64
            with self.assertRaisesRegex(ValueError,'mismatch'):_account(directory,[corrupted])
            missing=Path(temporary)/'missing';shutil.copytree(directory,missing)
            next((missing/'branch-audit-store/audits').glob('*.json')).unlink()
            with self.assertRaises(ValueError):_account(missing,[record])
            absent=Path(temporary)/'absent';shutil.copytree(directory,absent)
            shutil.rmtree(absent/'branch-audit-store/artifacts')
            with self.assertRaisesRegex(AssertionError,'topology'):_account(absent,[record])

    def test_readonly_guard_rejects_writes_des_and_network_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            evidence=Path(temporary)/'evidence';evidence.mkdir();target=evidence/'value.json'
            target.write_text('{"value":1}',encoding='utf-8');before=target.read_bytes()
            with offline_evidence(evidence):
                with self.assertRaisesRegex(AssertionError,'write forbidden'):target.write_text('changed')
                with self.assertRaisesRegex(AssertionError,'directory mutation'):(evidence/'new').mkdir()
                with self.assertRaisesRegex(AssertionError,'forbidden'):
                    PlanningScenario(PlanningInput(self.request,20261102,0)).evaluate(self.pool[0].command)
                with self.assertRaisesRegex(AssertionError,'forbidden'):LocalProvider(self.identity)._http('/api/tags')
            self.assertEqual(target.read_bytes(),before)
            self.assertFalse((evidence/'new').exists())

    def test_strict_json_rejects_duplicate_and_nonfinite_persisted_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            target=Path(temporary)/'bad.json'
            for payload in ('{"a":1,"a":2}','{"a":NaN}'):
                target.write_text(payload,encoding='utf-8')
                with self.subTest(payload=payload),self.assertRaises(ValueError):read(target)

    def test_completed_audit_checks_factory_scenario_config_and_exogenous_inputs(self):
        from scripts.phase2d_local import run_pilot_case
        from scripts.phase2d_r1 import build_case,policy
        from scripts.phase2d_r1_data_audit import verify_pilot_run
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary)/'case'
            run_pilot_case(directory,'balanced',10,'mandatory-first',None,
                           build_case_fn=build_case,policy_fn=policy)
            row,details,account=verify_pilot_run(directory,{'identity':None})
            self.assertEqual(account['logical_branches'],0)
            row['scenario']='scarce';save(directory/'result.json',row)
            with self.assertRaisesRegex(AssertionError,'pilot requested'):
                verify_pilot_run(directory,{'identity':None})

    def test_archive_duplicate_escape_and_symlink_rejected(self):
        import io,stat,zipfile
        for names in (('same','SAME'),('../escape',),('G:/escape',),('path\\escape',),('trailing. /x',),
                      ('a/./b',),('a//b',),('./a',),('a//',),('a','a/')):
            buffer=io.BytesIO()
            with zipfile.ZipFile(buffer,'w') as archive:
                for name in names:
                    info=zipfile.ZipInfo('placeholder');info.filename=name
                    archive.writestr(info,b'payload')
            with zipfile.ZipFile(buffer) as archive:
                with self.subTest(names=names),self.assertRaises(ValueError):checked_zip_entries(archive)
        buffer=io.BytesIO()
        with zipfile.ZipFile(buffer,'w') as archive:
            info=zipfile.ZipInfo('link');info.external_attr=(stat.S_IFLNK|0o777)<<16
            archive.writestr(info,b'/outside')
        with zipfile.ZipFile(buffer) as archive:
            with self.assertRaises(ValueError):checked_zip_entries(archive)

    def test_raw_binding_rejects_python_equal_identity_rendered_and_attempt_changes(self):
        from unittest.mock import patch
        from scripts.phase2d_r1_data_audit import verify_generation_budget
        from sim_jev.local_provider import GenerationBudget
        # Synthetic transport drives the actual durable writer solely in this
        # temporary control. These artifacts never count as real model data.
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            provider=LocalProvider(self.identity,root/'responses',mode='live',
                budget=GenerationBudget(root/'generation-budget.json'),phase='diagnostics',
                token_counter=lambda prompt:100)
            with patch.object(provider,'verify_model'),\
                 patch.object(provider,'_generate',side_effect=lambda body,timeout:scores_fixture(body)):
                original=provider.rank(self.request,self.pool)
            capture=root/'diagnostics/synthetic/shared-ranking.json';save(capture,original)
            self.assertEqual(verify_generation_budget(root,require_complete_binding=True)['actual_generations'],1)
            def change_identity(record):record['identity']['decoding']['seed']=42.0
            def change_rendered(record):
                observation=record['rendered']['canonical']['decision']['observation']
                value=observation['time']
                observation['time']=int(value) if type(value) is float else float(value)
            def change_attempt(record):record['attempts'][0]['raw']['done']=1
            changes=((change_identity,'raw model protocol identity canonical'),
                     (change_rendered,'raw public input identity canonical'),
                     (change_attempt,'response/raw attempt contents canonical'))
            for mutate,message in changes:
                changed=deepcopy(original);mutate(changed)
                self.assertEqual(changed,original)
                self.assertNotEqual(canonical_sha256(changed),canonical_sha256(original))
                save(capture,changed)
                with self.subTest(message=message),self.assertRaisesRegex(AssertionError,message):
                    verify_generation_budget(root,require_complete_binding=True)


if __name__=='__main__':unittest.main()
