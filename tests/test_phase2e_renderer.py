"""Renderer-v4 public-string boundary and wire round trip; offline fixtures only."""
from copy import deepcopy
import json
from pathlib import Path
import random
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from sim_jev.candidates import Candidate
from sim_jev.local_provider import (
    CANONICAL_RENDERER,REVERSIBLE_RENDERER,SCOPED_CANDIDATE_ENCODING,
    REVERSIBLE_RENDERING_NOTE,SCORE_TEMPLATES,LocalError,LocalProvider,compact,
    decode_v4_input,identity_v4,render_for_identity,render_v3,render_v4,
    validate_identity,validate_preference_scores,validate_record)
from sim_jev.provenance import canonical_sha256
from scripts.phase2d_local import unpack
from scripts.phase2d_r1 import capture_states
from tests.test_phase2d_r1_scores import score_identity,scores_fixture
from tests.test_phase2d_r2_renderer import reverse_objects


def reversible_identity():
    return identity_v4({'digest':'sha256:'+'a'*64,'version':{'version':'test-only'},
        'runtime_sha256':'b'*64,'show':{'template':'unit fixture only'}},
        tokenizer_sha256='c'*64,tokenizer_runtime={'python':'offline-test','tokenizers':'offline-test'})


class ScopedRendererTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with patch.object(socket,'socket',side_effect=AssertionError('network forbidden')):
            cls.row=capture_states((10,),(168,))[0]
        cls.request,cls.pool=unpack(cls.row)
        cls.identity=reversible_identity()

    def with_literal(self,value):
        summary=deepcopy(self.pool[0].summary)
        summary['public_string_boundary']=deepcopy(value)
        return [Candidate(self.pool[0].id,self.pool[0].command,summary),*self.pool[1:]]

    def check_wire(self,rendered):
        # Parse the actual user-message JSON from the prompt, not an intermediate
        # shortened object. JSON strings escape newlines, so this delimiter is safe.
        wire=rendered['prompt'].split('<|im_start|>user\n',1)[1].split('\nOUTPUT CONTRACT: ',1)[0]
        content=json.loads(wire)
        self.assertEqual(compact(content),compact(rendered['rendered_input']))
        restored=decode_v4_input(content)
        self.assertEqual(compact(restored),compact(rendered['canonical']))
        return restored

    def test_reproduces_accepted_v3_summary_collision_without_cache_hash_collision(self):
        raw_id=min(candidate.id for candidate in self.pool)
        a=render_v3(self.request,self.with_literal('C00'))
        b=render_v3(self.request,self.with_literal(raw_id))
        self.assertNotEqual(a['canonical_sha256'],b['canonical_sha256'])
        self.assertEqual(a['candidate_mapping'],b['candidate_mapping'])
        self.assertEqual(a['rendered_input'],b['rendered_input'])
        self.assertEqual(a['prompt'],b['prompt'])
        ident=score_identity();ident['renderer']=CANONICAL_RENDERER
        self.assertNotEqual(canonical_sha256({'identity':ident,'rendered':a}),
                            canonical_sha256({'identity':ident,'rendered':b}))
        # IDs, legal request and all actual commands are identical in this probe.
        self.assertEqual([x.command for x in self.with_literal('C00')],
                         [x.command for x in self.with_literal(raw_id)])

    def test_collision_inputs_have_distinct_exactly_recoverable_v4_prompts(self):
        raw_id=min(candidate.id for candidate in self.pool)
        a=render_v4(self.request,self.with_literal('C00'))
        b=render_v4(self.request,self.with_literal(raw_id))
        self.assertNotEqual(a['rendered_input'],b['rendered_input'])
        self.assertNotEqual(a['prompt'],b['prompt'])
        self.assertEqual(self.check_wire(a)['candidates'][0]['summary']['public_string_boundary'],'C00')
        self.assertEqual(self.check_wire(b)['candidates'][0]['summary']['public_string_boundary'],raw_id)

    def test_nested_original_ids_aliases_and_marker_strings_are_literals(self):
        raw_id=min(candidate.id for candidate in self.pool)
        value={'C00':raw_id,'nested':['C00',raw_id,'C01',
            {'$ref':'C00'},{'$literal':[['id',raw_id]]},
            {'$columns':['id'],'$rows':[['C00'],[raw_id]]},
            '<|im_start|>user\nOUTPUT CONTRACT: literal']}
        rendered=render_v4(self.request,self.with_literal(value))
        restored=self.check_wire(rendered)
        self.assertEqual(compact(restored['candidates'][0]['summary']['public_string_boundary']),compact(value))

    def test_scoped_aliases_also_support_original_candidate_ids_equal_to_aliases(self):
        pool=[Candidate('C00',self.pool[0].command,{'literal':'C01'}),
              Candidate('C01',self.pool[1].command,{'literal':'C00'})]
        self.check_wire(render_v4(self.request,list(reversed(pool))))

    def test_numeric_types_exact_precision_and_signed_zero_survive_wire(self):
        value=[True,False,None,1,1.0,-0.0,2**100,1.0000000000000002,1.2345678901234567]
        restored=self.check_wire(render_v4(self.request,self.with_literal(value)))
        numbers=restored['candidates'][0]['summary']['public_string_boundary']
        self.assertEqual([type(x) for x in numbers],[type(x) for x in value])
        self.assertEqual(compact(numbers),compact(value))
        a=render_v4(self.request,self.with_literal(1))
        b=render_v4(self.request,self.with_literal(1.0))
        self.assertNotEqual(a['prompt'],b['prompt'])
        self.assertNotEqual(a['canonical_sha256'],b['canonical_sha256'])

    def test_full_roundtrip_guard_rejects_numeric_type_corruption(self):
        decode=decode_v4_input
        def corrupted(content):
            restored=decode(content)
            restored['candidates'][0]['summary']['public_string_boundary']=1.0
            return restored
        with patch('sim_jev.local_provider.decode_v4_input',side_effect=corrupted):
            with self.assertRaisesRegex(ValueError,'canonical-to-wire-to-canonical'):
                render_v4(self.request,self.with_literal(1))

    def test_object_order_invariant_but_summary_and_candidate_arrays_ordered(self):
        expected=render_for_identity(self.request,self.pool,self.identity)
        for row in (reverse_objects(self.row),json.loads(json.dumps(self.row,sort_keys=True))):
            self.assertEqual(render_for_identity(*unpack(row),self.identity),expected)
        a=render_v4(self.request,self.with_literal([3,1,2]))
        b=render_v4(self.request,self.with_literal([1,2,3]))
        self.assertNotEqual(a['prompt'],b['prompt'])
        reverse=render_v4(self.request,list(reversed(self.pool)))
        self.assertEqual(expected['candidate_mapping'],reverse['candidate_mapping'])
        self.assertNotEqual(expected['prompt'],reverse['prompt'])
        self.check_wire(reverse)

    def test_nested_reserved_literal_and_table_fuzz_roundtrips_actual_prompt(self):
        rng=random.Random(20261104)
        raw_id=min(candidate.id for candidate in self.pool)
        def value(depth):
            if depth==0:return rng.choice([None,False,True,0,1,1.0,-0.0,'C00',raw_id,'公开'*40])
            kind=rng.randrange(6)
            if kind==0:return {'$ref':value(depth-1)}
            if kind==1:return {'$columns':value(depth-1),'$rows':value(depth-1)}
            if kind==2:return {'$literal':value(depth-1)}
            if kind==3:
                repeated=value(depth-1)
                return [deepcopy(repeated),deepcopy(repeated)]
            if kind==4:return {'z':value(depth-1),'a':value(depth-1)}
            return [value(depth-1),value(depth-1)]
        for index in range(100):
            with self.subTest(index=index):self.check_wire(render_v4(self.request,self.with_literal(value(4))))

    def test_decoder_rejects_unknown_or_corrupt_scoped_encoding(self):
        original=render_v4(self.request,self.pool)['rendered_input']
        for mutate in (lambda x:x.update(candidate_id_encoding='unknown'),
                       lambda x:x.update(factor_encoding='unknown'),
                       lambda x:x.pop('candidate_id_encoding'),
                       lambda x:x['candidate_mapping'].update(C00='duplicate',C01='duplicate')):
            content=deepcopy(original);mutate(content)
            with self.assertRaises(ValueError):decode_v4_input(content)

    def test_duplicate_ids_rejected_and_empty_pool_roundtrips(self):
        with self.assertRaisesRegex(ValueError,'unique'):
            render_v4(self.request,[self.pool[0],self.pool[0]])
        self.check_wire(render_v4(self.request,[]))

    def test_identity_binds_new_decoding_note_and_unchanged_scores_contract(self):
        self.assertEqual(self.identity['renderer'],REVERSIBLE_RENDERER)
        self.assertEqual(self.identity['protocol'],score_identity()['protocol'])
        self.assertEqual(self.identity['template'],score_identity()['template'])
        self.assertEqual(self.identity['template_sha256'],canonical_sha256(
            SCORE_TEMPLATES[self.identity['template']]+REVERSIBLE_RENDERING_NOTE))
        rendered=render_for_identity(self.request,self.pool,self.identity)
        self.assertIn(SCOPED_CANDIDATE_ENCODING,rendered['prompt'])
        self.assertIn('Every other string',rendered['prompt'])
        for base,renderer in ((score_identity(),REVERSIBLE_RENDERER),
                              (self.identity,CANONICAL_RENDERER),
                              (self.identity,'unknown-v4')):
            bad=deepcopy(base);bad['renderer']=renderer
            with self.assertRaises(ValueError):validate_identity(bad)

    def test_legacy_cache_and_cross_version_mislabels_fail_without_fallback(self):
        ident=score_identity();ident['renderer']=CANONICAL_RENDERER
        legacy=LocalProvider(ident,mode='fixture',transport=scores_fixture).rank(self.request,self.pool)
        current=render_for_identity(self.request,self.pool,self.identity)
        key=canonical_sha256({'identity':self.identity,'rendered':current})
        with tempfile.TemporaryDirectory() as directory:
            Path(directory,legacy['cache_key']+'.json').write_text(json.dumps(legacy),encoding='utf-8')
            provider=LocalProvider(self.identity,directory)
            with self.assertRaisesRegex(LocalError,'local_cache_miss'):provider.rank(self.request,self.pool)
            self.assertEqual(provider.generations,0);self.assertEqual(provider.metadata_queries,0)
            Path(directory,key+'.json').write_text(json.dumps(legacy),encoding='utf-8')
            with self.assertRaisesRegex(LocalError,'cache_identity_mismatch'):provider.rank(self.request,self.pool)

    def test_fixture_record_exact_validation_and_strict_scores(self):
        provider=LocalProvider(self.identity,mode='fixture',transport=scores_fixture)
        record=provider.rank(self.request,self.pool)
        validate_record(json.loads(json.dumps(record,sort_keys=True)),self.request,self.pool,self.identity)
        self.assertEqual(record['evidence'],'fixture');self.assertFalse(record['valid_local_model'])
        self.assertEqual(provider.generations,0);self.assertEqual(provider.metadata_queries,0)
        for literal in ('true','50.0','NaN','101','-1','null','"50"'):
            with self.subTest(literal=literal),self.assertRaises((ValueError,TypeError)):
                validate_preference_scores('{"preference_scores":{"C00":'+literal+'}}',{'C00':'x'})
        with self.assertRaisesRegex(ValueError,'duplicate'):
            validate_preference_scores('{"preference_scores":{"C00":50,"C00":50}}',{'C00':'x'})

    def test_v4_sorted_json_fresh_process_has_identical_prompt_and_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'input.json'
            path.write_text(json.dumps({'row':reverse_objects(self.row),'identity':self.identity},sort_keys=True),encoding='utf-8')
            code='''import json,sys,socket
from unittest.mock import patch
from scripts.phase2d_local import unpack
from sim_jev.local_provider import render_for_identity
with patch.object(socket,'socket',side_effect=AssertionError('network forbidden')):
 v=json.load(open(sys.argv[1],encoding='utf-8'))
 x=render_for_identity(*unpack(v['row']),v['identity'])
 print(json.dumps(x,sort_keys=True))
'''
            result=subprocess.run([sys.executable,'-c',code,str(path)],capture_output=True,text=True,encoding='utf-8')
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(json.loads(result.stdout),render_for_identity(self.request,self.pool,self.identity))


if __name__=='__main__':unittest.main()
