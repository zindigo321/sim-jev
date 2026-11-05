"""R2 renderer contracts: offline fixtures only; no model or candidate DES calls."""
from copy import deepcopy
import json
import random
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from sim_jev.candidates import Candidate
from sim_jev.local_policy import LocalDecisionPolicySpec,LOCAL_SCORE_POLICY_VERSION
from sim_jev.local_provider import (
    CANONICAL_RENDERER,CANONICAL_FACTOR_ENCODING,RENDERER,SCORE_RENDERER,
    LocalError,LocalProvider,compact,factor_canonical,identity_v3,render_for_identity,
    render_v2,score_schema,unfactor,validate_identity,validate_preference_scores,validate_record)
from sim_jev.provenance import canonical_sha256
from scripts.phase2d_local import unpack
from scripts.phase2d_r1 import capture_states
from tests.test_phase2d_local import test_identity
from tests.test_phase2d_r1_scores import score_identity,scores_fixture


def canonical_identity():
    value=score_identity()
    value['renderer']=CANONICAL_RENDERER
    validate_identity(value)
    return value


def reverse_objects(value):
    if isinstance(value,dict):return {key:reverse_objects(value[key]) for key in reversed(value)}
    if isinstance(value,list):return [reverse_objects(item) for item in value]
    return value


def cache_key(request,pool,identity):
    return canonical_sha256({'identity':identity,'rendered':render_for_identity(request,pool,identity)})


class CanonicalRendererTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with patch.object(socket,'socket',side_effect=AssertionError('network forbidden')):
            cls.row=capture_states((40,),(168,))[0]
        cls.request,cls.pool=unpack(cls.row)
        cls.identity=canonical_identity()

    def test_historical_counterexample_stays_explicit_and_unchanged(self):
        request,pool=unpack(json.loads(json.dumps(self.row,sort_keys=True)))
        old=render_v2(self.request,self.pool)
        reordered=render_v2(request,pool)
        self.assertEqual(old['prompt_sha256'],'f2be60ac950583ea7b9fc97bae18d5f8ec32f7ccd364966f5abcd934ea288015')
        self.assertEqual(reordered['prompt_sha256'],'2afd970bc356d07bd67f8995df6dc48e2dda397a44ff48f74e6e13b717b2ab16')
        self.assertEqual(old['canonical_sha256'],reordered['canonical_sha256'])
        self.assertNotEqual(old['prompt'],reordered['prompt'])

    def test_object_key_permutations_preserve_prompt_mapping_schema_and_key(self):
        expected=render_for_identity(self.request,self.pool,self.identity)
        expected_key=cache_key(self.request,self.pool,self.identity)
        for row in (json.loads(json.dumps(self.row)),json.loads(json.dumps(self.row,sort_keys=True)),reverse_objects(self.row)):
            request,pool=unpack(row)
            actual=render_for_identity(request,pool,self.identity)
            self.assertEqual(actual,expected)
            self.assertEqual(actual['prompt'].encode(),expected['prompt'].encode())
            self.assertEqual(actual['candidate_mapping'],expected['candidate_mapping'])
            self.assertEqual(score_schema(actual['candidate_mapping']),score_schema(expected['candidate_mapping']))
            self.assertEqual(cache_key(request,pool,self.identity),expected_key)

    def test_canonical_factor_is_lossless_for_numbers_types_unicode_and_array_order(self):
        repeated={'z':'重复的公开合法数据'*20,'a':[1,1.0,-0.0,2**100,1.2345678901234567]}
        value={'z':[deepcopy(repeated),deepcopy(repeated)],'a':[False,None,3,2,1],
               'literal':[{'$ref':'d0'},{'$ref':'d0'}],
               'nested':{'$literal':{'$columns':['x'],'$rows':[[1]]}}}
        packed=factor_canonical(value)
        self.assertEqual(compact(unfactor(packed)),compact(value))
        self.assertEqual(packed,factor_canonical(reverse_objects(value)))
        decoded=unfactor(packed)
        self.assertIs(type(decoded['z'][0]['a'][0]),int)
        self.assertIs(type(decoded['z'][0]['a'][1]),float)
        self.assertEqual(compact(decoded['z'][0]['a'][2]),'-0.0')
        self.assertEqual(decoded['a'],[False,None,3,2,1])

    def test_reserved_literal_shapes_survive_nested_tables_and_shared_references(self):
        literals=[{'$ref':'d0'},{'$columns':['a'],'$rows':[[1]]},{'$literal':[['x',1]]}]
        for literal in literals:
            value={'same':[deepcopy(literal),deepcopy(literal)],
                   'normal':[{'x':deepcopy(literal)},{'x':deepcopy(literal)}]}
            self.assertEqual(compact(unfactor(factor_canonical(value))),compact(value))

    def test_nested_marker_and_table_fuzz_is_lossless_and_deterministic(self):
        rng=random.Random(20261104)
        def value(depth):
            if depth==0:return rng.choice([None,False,True,0,1,1.0,-0.0,1.0000000000000002,'公开'*60])
            kind=rng.randrange(6)
            if kind==0:return {'$ref':value(depth-1)}
            if kind==1:return {'$columns':value(depth-1),'$rows':value(depth-1)}
            if kind==2:return {'$literal':value(depth-1)}
            if kind==3:
                repeated=value(depth-1)
                return [deepcopy(repeated),deepcopy(repeated)]
            if kind==4:return {'z':value(depth-1),'a':value(depth-1)}
            return [value(depth-1),value(depth-1)]
        for index in range(200):
            original=value(4)
            with self.subTest(index=index):
                packed=factor_canonical(original)
                self.assertEqual(compact(unfactor(packed)),compact(original))
                self.assertEqual(packed,factor_canonical(reverse_objects(original)))

    def test_prompt_reverse_mapping_is_lossless(self):
        rendered=render_for_identity(self.request,self.pool,self.identity)
        decoded=unfactor(rendered['rendered_input'])
        mapping=rendered['candidate_mapping']
        def expand(value):
            if isinstance(value,dict):return {key:expand(item) for key,item in value.items()}
            if isinstance(value,list):return [expand(item) for item in value]
            return mapping.get(value,value) if isinstance(value,str) else value
        self.assertEqual(compact(expand(decoded)),compact(rendered['canonical']))
        self.assertEqual(rendered['rendered_input']['factor_encoding'],CANONICAL_FACTOR_ENCODING)

    def test_business_arrays_are_not_sorted_to_obtain_invariance(self):
        summary=deepcopy(self.pool[0].summary)
        summary['array_order_probe']=[3,1,2]
        left=[Candidate(self.pool[0].id,self.pool[0].command,summary),*self.pool[1:]]
        summary['array_order_probe']=[1,2,3]
        right=[Candidate(self.pool[0].id,self.pool[0].command,summary),*self.pool[1:]]
        a=render_for_identity(self.request,left,self.identity)
        b=render_for_identity(self.request,right,self.identity)
        self.assertNotEqual(a['prompt'],b['prompt'])
        self.assertNotEqual(cache_key(self.request,left,self.identity),cache_key(self.request,right,self.identity))

    def test_candidate_array_order_is_retained_with_stable_short_id_mapping(self):
        left=render_for_identity(self.request,self.pool,self.identity)
        right=render_for_identity(self.request,list(reversed(self.pool)),self.identity)
        self.assertEqual(left['canonical']['candidates'],[item.to_dict() for item in self.pool])
        self.assertEqual(right['canonical']['candidates'],[item.to_dict() for item in reversed(self.pool)])
        self.assertEqual(left['candidate_mapping'],right['candidate_mapping'])
        self.assertNotEqual(left['prompt'],right['prompt'])
        self.assertNotEqual(cache_key(self.request,self.pool,self.identity),
                            cache_key(self.request,list(reversed(self.pool)),self.identity))

    def test_numeric_value_and_type_changes_are_not_rounded_away(self):
        rendered=[]
        for value in (1,1.0,1.0000000000000002):
            summary=deepcopy(self.pool[0].summary);summary['exact_numeric_probe']=value
            pool=[Candidate(self.pool[0].id,self.pool[0].command,summary),*self.pool[1:]]
            rendered.append(cache_key(self.request,pool,self.identity))
        self.assertEqual(len(set(rendered)),3)

    def test_fresh_process_sorted_json_restore_preserves_complete_render_and_key(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'input.json'
            path.write_text(json.dumps({'row':self.row,'identity':self.identity},sort_keys=True),encoding='utf-8')
            script='''import json,socket,sys
from pathlib import Path
from unittest.mock import patch
from scripts.phase2d_local import unpack
from sim_jev.local_provider import render_for_identity,score_schema
from sim_jev.provenance import canonical_sha256
v=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
with patch.object(socket,'socket',side_effect=AssertionError('network forbidden')):
 r,p=unpack(v['row']);x=render_for_identity(r,p,v['identity'])
 print(json.dumps({'rendered':x,'schema':score_schema(x['candidate_mapping']),'key':canonical_sha256({'identity':v['identity'],'rendered':x})},sort_keys=True))
'''
            child=subprocess.run([sys.executable,'-c',script,str(path)],capture_output=True,text=True,encoding='utf-8')
            self.assertEqual(child.returncode,0,child.stderr)
            value=json.loads(child.stdout)
            rendered=render_for_identity(self.request,self.pool,self.identity)
            self.assertEqual(value,{'rendered':rendered,'schema':score_schema(rendered['candidate_mapping']),
                                    'key':cache_key(self.request,self.pool,self.identity)})

    def test_renderer_version_is_separate_from_scoring_protocol_and_policy(self):
        old=score_identity()
        new=identity_v3({'digest':'sha256:'+'a'*64,'version':{'version':'test-only'},
            'runtime_sha256':'b'*64,'show':{'template':'unit fixture only'}},
            tokenizer_sha256='c'*64,tokenizer_runtime={'python':'offline-test','tokenizers':'offline-test'})
        self.assertEqual(new,self.identity)
        self.assertEqual({k:v for k,v in old.items() if k!='renderer'},
                         {k:v for k,v in new.items() if k!='renderer'})
        spec=LocalDecisionPolicySpec(identity=new,version=LOCAL_SCORE_POLICY_VERSION)
        self.assertEqual(LocalDecisionPolicySpec.from_dict(spec.to_dict()),spec)
        self.assertNotEqual(cache_key(self.request,self.pool,old),cache_key(self.request,self.pool,new))

    def test_unknown_and_mixed_versions_rejected_before_transport(self):
        values=[]
        for base,renderer in ((test_identity(),SCORE_RENDERER),(test_identity(),CANONICAL_RENDERER),
                              (score_identity(),RENDERER),(self.identity,'unknown-renderer')):
            value=deepcopy(base);value['renderer']=renderer;values.append(value)
        for key in ('protocol','template','template_sha256','schema_version','prompt_format'):
            value=deepcopy(self.identity);value[key]='unknown';values.append(value)
        for value in values:
            with self.subTest(value=value),self.assertRaises(ValueError):
                render_for_identity(self.request,self.pool,value)

    def test_unknown_factor_encoding_rejected(self):
        packed=factor_canonical({'x':[1,2]});packed['factor_encoding']='unknown'
        with self.assertRaisesRegex(ValueError,'unsupported factor encoding'):unfactor(packed)

    def test_legacy_cache_never_falls_back_to_new_renderer(self):
        old=score_identity()
        record=LocalProvider(old,mode='fixture',transport=scores_fixture).rank(self.request,self.pool)
        with tempfile.TemporaryDirectory() as directory:
            Path(directory,record['cache_key']+'.json').write_text(json.dumps(record),encoding='utf-8')
            provider=LocalProvider(self.identity,directory,mode='cache_only')
            with patch.object(socket,'socket',side_effect=AssertionError('network forbidden')):
                with self.assertRaisesRegex(LocalError,'local_cache_miss'):provider.rank(self.request,self.pool)
            self.assertEqual(provider.generations,0);self.assertEqual(provider.metadata_queries,0)
            self.assertEqual(len(list(Path(directory).iterdir())),1)

    def test_legacy_response_cannot_be_relabelled_as_new_renderer(self):
        record=LocalProvider(score_identity(),mode='fixture',transport=scores_fixture).rank(self.request,self.pool)
        with tempfile.TemporaryDirectory() as directory:
            key=cache_key(self.request,self.pool,self.identity)
            Path(directory,key+'.json').write_text(json.dumps(record),encoding='utf-8')
            with self.assertRaisesRegex(LocalError,'cache_identity_mismatch'):
                LocalProvider(self.identity,directory).rank(self.request,self.pool)

    def test_same_renderer_fixture_roundtrip_is_strict_and_never_live(self):
        provider=LocalProvider(self.identity,mode='fixture',transport=scores_fixture)
        record=provider.rank(self.request,self.pool)
        request,pool=unpack(reverse_objects(json.loads(json.dumps(self.row,sort_keys=True))))
        validate_record(json.loads(json.dumps(record,sort_keys=True)),request,pool,self.identity)
        self.assertEqual(record['evidence'],'fixture');self.assertFalse(record['valid_local_model'])
        self.assertEqual(provider.generations,0);self.assertEqual(provider.metadata_queries,0)

    def test_raw_score_rank_prompt_and_schema_corruption_rejected(self):
        record=LocalProvider(self.identity,mode='fixture',transport=scores_fixture).rank(self.request,self.pool)
        for mutate in (lambda x:x['ranked_ids'].reverse(),
                       lambda x:x['attempts'][-1]['raw'].update(response='{"preference_scores":{}}'),
                       lambda x:x['attempts'][0]['body'].update(prompt='changed'),
                       lambda x:x['attempts'][0]['body'].update(format={})):
            value=deepcopy(record);mutate(value)
            with self.assertRaises((ValueError,LocalError)):validate_record(value,self.request,self.pool,self.identity)

    def test_json_numeric_types_in_identity_and_rendered_record_are_exact(self):
        record=LocalProvider(self.identity,mode='fixture',transport=scores_fixture).rank(self.request,self.pool)
        changed=deepcopy(record)
        changed['identity']['decoding']['temperature']=0.0
        self.assertEqual(changed['identity'],record['identity'])  # Python numeric equality is insufficient.
        with self.assertRaisesRegex(LocalError,'cache_identity_mismatch'):
            validate_record(changed,self.request,self.pool,self.identity)
        changed=deepcopy(record)
        parameters=changed['rendered']['canonical']['decision']['parameters']
        parameters['fleet_size']=float(parameters['fleet_size'])
        self.assertEqual(changed['rendered'],record['rendered'])
        with self.assertRaisesRegex(LocalError,'cache_identity_mismatch'):
            validate_record(changed,self.request,self.pool,self.identity)

    def test_attempt_schema_numeric_boolean_substitution_is_rejected(self):
        record=LocalProvider(self.identity,mode='fixture',transport=scores_fixture).rank(self.request,self.pool)
        changed=deepcopy(record)
        scores=changed['attempts'][0]['body']['format']['properties']['preference_scores']['properties']
        scores[next(iter(scores))]['minimum']=False
        self.assertEqual(changed['attempts'][0]['body']['format'],record['attempts'][0]['body']['format'])
        with self.assertRaisesRegex(LocalError,'cached_prompt_or_schema_mismatch'):
            validate_record(changed,self.request,self.pool,self.identity)

    def test_attempt_decoding_numeric_types_and_transport_booleans_are_exact(self):
        record=LocalProvider(self.identity,mode='fixture',transport=scores_fixture).rank(self.request,self.pool)
        # Persisted JSON contains independent objects; deepcopy retains the
        # in-memory provider's identity/options alias and would alter both.
        changed=json.loads(json.dumps(record))
        changed['attempts'][0]['body']['options']['temperature']=0.0
        self.assertEqual(changed['attempts'][0]['body']['options'],record['attempts'][0]['body']['options'])
        with self.assertRaisesRegex(LocalError,'cached_inference_metadata_mismatch'):
            validate_record(changed,self.request,self.pool,self.identity)
        for field,value in (('raw',1),('stream',0),('truncate',0),('shift',0)):
            changed=deepcopy(record);changed['attempts'][0]['body'][field]=value
            with self.subTest(field=field),self.assertRaisesRegex(LocalError,'cached_inference_metadata_mismatch'):
                validate_record(changed,self.request,self.pool,self.identity)

    def test_scores_remain_strict_integers_and_ties_use_original_ids(self):
        mapping={'C00':'z','C01':'a'}
        parsed=validate_preference_scores('{"preference_scores":{"C00":50,"C01":50}}',mapping)
        self.assertEqual(parsed['ranked_ids'],['a','z'])
        for literal in ('true','50.0','NaN','101','-1','null','"50"'):
            with self.subTest(literal=literal),self.assertRaises((ValueError,TypeError)):
                validate_preference_scores('{"preference_scores":{"C00":'+literal+',"C01":50}}',mapping)
        with self.assertRaisesRegex(ValueError,'duplicate'):
            validate_preference_scores('{"preference_scores":{"C00":50,"C00":50,"C01":50}}',mapping)


if __name__=='__main__':unittest.main()
