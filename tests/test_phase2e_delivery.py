"""Transport tamper controls and offline accounting of honest partial evidence."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from scripts.phase2e_delivery import audit,audit_raw_attempts,pack,strict_json,verify_zip
from scripts.phase2e_live import LIMITS,TIME_LIMITS
from sim_jev.local_provider import LocalProvider
from sim_jev.decision import DecisionRequest
from sim_jev.candidates import generate_candidates
from sim_jev.provenance import canonical_sha256,code_fingerprint
from tests.test_phase2d_local import captured
from tests.test_phase2d_r1_scores import scores_fixture
from tests.test_phase2e_renderer import reversible_identity


def save(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False,allow_nan=False),encoding='utf-8')


def snapshot(root):
    return {path.relative_to(root).as_posix():hashlib.sha256(path.read_bytes()).hexdigest()
            if path.is_file() else None for path in root.rglob('*')}


class Phase2eTransportTests(unittest.TestCase):
    def archive(self,path,payload=b'original',*,extra=False,directories=None,claimed=None):
        manifest={'schema':'phase2e-a-dependent-package-v1',
                  'files':[{'path':'payload','size_bytes':len(payload),'sha256':hashlib.sha256(claimed or payload).hexdigest()}],
                  'directories':['empty/branch-audit-store'] if directories is None else directories}
        with zipfile.ZipFile(path,'w') as z:
            z.writestr('payload',payload)
            z.writestr('empty/branch-audit-store/',b'')
            if extra:z.writestr('unlisted',b'must fail')
            z.writestr('MANIFEST.json',json.dumps(manifest))

    def test_strict_payload_set_tamper_and_directory_topology(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'case.zip'
            self.archive(path)
            receipt=verify_zip(path)
            self.assertIn('empty/branch-audit-store',receipt['directories'])
            for kwargs,error in (({'extra':True},'payload set'),
                                 ({'claimed':b'tampered'},'payload mismatch'),
                                 ({'directories':[]},'topology'),
                                 ({'directories':['empty/branch-audit-store']*2},'duplicate manifest directory')):
                self.archive(path,**kwargs)
                with self.subTest(kwargs=kwargs),self.assertRaisesRegex(ValueError,error):verify_zip(path)

    def test_unsafe_member_and_duplicate_json_manifest_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'case.zip'
            with zipfile.ZipFile(path,'w') as z:
                z.writestr('../outside',b'bad');z.writestr('MANIFEST.json','{"files":[],"directories":[]}')
            with self.assertRaisesRegex(ValueError,'unsafe'):verify_zip(path)
            with zipfile.ZipFile(path,'w') as z:
                z.writestr('MANIFEST.json','{"files":[],"files":[],"directories":[]}')
            with self.assertRaisesRegex(ValueError,'duplicate JSON'):verify_zip(path)

    def test_pack_preserves_real_empty_audit_store_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'source';reports=root/'reports'
            (source/'scripts').mkdir(parents=True);reports.mkdir()
            (source/'scripts/test.py').write_text('value=1\n')
            (reports/'empty/branch-audit-store/artifacts').mkdir(parents=True)
            result=pack(source,reports,root/'new.zip')
            self.assertEqual(result['sha256'],verify_zip(root/'new.zip')['archive_sha256'])
            with zipfile.ZipFile(root/'new.zip') as z:
                self.assertIn('reports/empty/branch-audit-store/artifacts/',z.namelist())
            with self.assertRaisesRegex(ValueError,'must be new'):pack(source,reports,root/'new.zip')


class Phase2eRawAccountingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        old,_=captured()
        cls.request=DecisionRequest.create(old.observation,old.context,old.parameters,
            protocol_version='legal-weekly-request-v2')
        cls.pool=generate_candidates(cls.request,16,20261102)
        cls.ident=reversible_identity()
        cls.record=LocalProvider(cls.ident,mode='fixture',transport=scores_fixture).rank(cls.request,cls.pool)

    def fixture(self,root):
        frozen={'identity':deepcopy(self.ident)};budget={'attempts':[]};http={'requests':[]}
        for index in range(2):
            aid=str(index+1).zfill(32)
            attempt=deepcopy(self.record['attempts'][0])
            attempt.update(attempt_id=aid,logical_request_id='logical-'+str(index),attempt_index=0,
                           independent_prompt_tokens=100,seconds=1.)
            parsed=json.loads(attempt['raw']['response'])
            parsed['preference_scores']={key:50+index for key in parsed['preference_scores']}
            attempt['raw']['response']=json.dumps(parsed)
            evidence={'identity':deepcopy(self.ident),'cache_key':self.record['cache_key'],
                      'rendered':deepcopy(self.record['rendered']),'attempt':attempt}
            name='attempt-'+aid+'.json'
            save(root/'development/responses'/name,evidence)
            budget['attempts'].append({'attempt_id':aid,'phase':'development','status':'completed',
                'logical_request_id':attempt['logical_request_id'],'attempt_index':0,
                'key':self.record['cache_key'],'seconds':1.,'reserved_deadline_seconds':120.,
                'artifact':{'path':name,'sha256':canonical_sha256(evidence)}})
            save(root/'http'/f'{index:04d}-request.json',{'data':attempt['body']})
            response_path=f'http/{index:04d}-response.json'
            save(root/response_path,{'response':attempt['raw']})
            http['requests'].append({'route':'/api/generate','generation_attempt_id':aid,'index':index,
                                     'status':'completed','artifact':response_path})
        return frozen,budget,http

    def test_identical_repeat_bodies_bind_distinct_attempts_and_raw_responses(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);frozen,budget,http=self.fixture(root)
            self.assertEqual(strict_json((root/'http/0000-request.json').read_text()),
                             strict_json((root/'http/0001-request.json').read_text()))
            rows,prompts=audit_raw_attempts(root,frozen,budget,http)
            self.assertEqual(len(rows),2);self.assertEqual(prompts[0],prompts[1])
            self.assertTrue(all(not row['engineering_failures'] for row in rows))
            swapped=deepcopy(http)
            swapped['requests'][0]['artifact'],swapped['requests'][1]['artifact']=\
                swapped['requests'][1]['artifact'],swapped['requests'][0]['artifact']
            with self.assertRaisesRegex(ValueError,'body/response differs'):
                audit_raw_attempts(root,frozen,budget,swapped)
            reused=deepcopy(http)
            reused['requests'][1]['generation_attempt_id']=reused['requests'][0]['generation_attempt_id']
            with self.assertRaisesRegex(ValueError,'missing/reused'):audit_raw_attempts(root,frozen,budget,reused)

    def test_honest_backend_or_parse_failure_preserved_but_artifact_tamper_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);frozen,budget,http=self.fixture(root)
            reservation=budget['attempts'][0];path=root/'development/responses'/reservation['artifact']['path']
            evidence=strict_json(path.read_text(encoding='utf-8'))
            evidence['attempt']['raw'].update(response='bad JSON',prompt_eval_count=99,done_reason='length')
            save(path,evidence);reservation['artifact']['sha256']=canonical_sha256(evidence)
            save(root/http['requests'][0]['artifact'],{'response':evidence['attempt']['raw']})
            rows,_=audit_raw_attempts(root,frozen,budget,http)
            self.assertIn('independent_backend_token_mismatch',rows[0]['engineering_failures'])
            self.assertTrue(any('strict_scores_parse_failure' in error for error in rows[0]['engineering_failures']))
            evidence['attempt']['seconds']=2.;save(path,evidence)
            with self.assertRaisesRegex(ValueError,'content differs'):audit_raw_attempts(root,frozen,budget,http)

    def test_strict_offline_audit_accepts_honest_partial_and_leaves_evidence_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'evidence';root.mkdir();output=Path(tmp)/'audit'
            row={'request':self.request.to_dict(),'pool':[candidate.to_dict() for candidate in self.pool]}
            save(root/'development/states.json',[{**row,'state_id':'fixture-'+str(i)} for i in range(8)])
            save(root/'freeze.json',{'source':code_fingerprint(),'identity':self.ident})
            save(root/'generation-budget.json',{'protocol':'phase2e-durable-generation-v1',
                'campaign_id':'isolated-partial','limits':LIMITS,'phase_max_seconds':TIME_LIMITS,
                'max_seconds':7200,'max_attempts':120,'attempts':[],'seconds':0.})
            save(root/'http-budget.json',{'protocol':'phase2e-local-http-v1','max_metadata':400,'requests':[]})
            before=snapshot(root)
            result=audit(root,output)
            self.assertEqual(result['status'],'passed')
            self.assertEqual(result['development_gate'],'partial')
            self.assertEqual(snapshot(root),before)
            self.assertEqual(strict_json((output/'strict-audit.json').read_text())['costs']['new_des'],0)
            with self.assertRaisesRegex(ValueError,'must be separate'):audit(root,root/'not-allowed')


if __name__=='__main__':unittest.main()
