"""Local ranking v1: lossless public inputs, strict permutations, durable replay.

The production transport accepts only literal IPv4 loopback, ignores proxies,
rejects redirects, and never reads credentials. Fixtures cannot write live caches.
"""
from __future__ import annotations
from collections import Counter
from copy import deepcopy
import json
import math
from pathlib import Path
import socket
from queue import Queue, Empty
from threading import Thread
import time
import uuid
from urllib import request as http
from urllib.parse import urlsplit
from .provenance import canonical_sha256
from .jev import _atomic_json, _public, _RejectRedirect

MODEL = 'qwen3:4b-instruct-2507-q4_K_M'
PROTOCOL = 'local-ranking-v1'
RENDERER = 'lossless-tables-and-shared-json-v1'
TEMPLATE = 'public-portfolio-ranking-v2'
INSTRUCTIONS = (
    'Rank all supplied candidate portfolios from best to worst for expected realized '
    'DSR + RCR of the CURRENT offer cohort at window_end. Rejected offers remain in '
    'the denominators. Respect mandatory obligations, current flight occupancy, repairs, '
    'spares, announced rules, mission timing, whole-mission value and aircraft-hour demand. '
    'Candidates are already nominally validated. Heuristic proxies are public estimates, '
    'not realized outcomes. There is no terminal value or future hidden information. '
    'Use only this input. Return JSON with exactly ranked_ids, containing every candidate '
    'short ID exactly once, no prose. The JSON input is losslessly factored: {$ref:N} '
    'refers to definitions[N]; {$columns:[keys],$rows:[values]} is a table of objects. '
    'candidate_mapping maps short IDs to original IDs. No information was rounded or omitted.'
)
CORRECTION = 'FORMAT ERROR. Return only the required JSON object and a complete duplicate-free permutation of the supplied short candidate IDs. Do not add or omit IDs.'

# v1 constants and rendering above remain an independently readable protocol.
SCORE_PROTOCOL = 'local-preference-scores-v2'
SCORE_RENDERER = 'lossless-tables-and-shared-json-scores-v2'
# Renderer identity is independent from the unchanged scores-v2 output protocol.
CANONICAL_RENDERER = 'lossless-canonical-tables-and-shared-json-v3'
CANONICAL_FACTOR_ENCODING = 'canonical-object-order-and-literals-v3'
CANONICAL_RENDERING_NOTE = (
    '\nRENDERER: Objects are traversed in ascending Unicode key order; array order and '
    'all numeric values are preserved. factor_encoding identifies this lossless encoding. '
    '{$literal:[[key,value],...]} represents a literal object whose shape would otherwise '
    'collide with a reserved encoding marker. Decode its values recursively and retain '
    'the literal keys without interpreting the resulting object as an encoding marker.')
REVERSIBLE_RENDERER = 'lossless-scoped-candidate-id-tables-and-shared-json-v4'
SCOPED_CANDIDATE_ENCODING = 'candidate-record-id-field-only-v4'
REVERSIBLE_RENDERING_NOTE = (
    CANONICAL_RENDERING_NOTE+
    '\nCANDIDATE ID DECODING: candidate_id_encoding is candidate-record-id-field-only-v4. '
    'First expand the factored input. Only the id field of each object in the top-level '
    'candidates array is a short candidate reference: replace that value using '
    'candidate_mapping to recover the original ID. Every other string, including '
    'strings equal to short IDs or original candidate IDs in summaries, commands, '
    'and the decision, is a literal and must remain unchanged. Output score keys '
    'are the short IDs in candidate_mapping. This scope rule is reversible.')
SCORE_SORT_RULE = 'descending-integer-score-then-original-stable-id-v1'
SCORE_SEMANTICS = 'uncalibrated candidate preference, not probability, confidence, success rate or long-term value'
SCORE_TEMPLATES = {
    'public-portfolio-scores-v2-a': (
        'Score EVERY supplied candidate portfolio with an integer preference from 0 to 100 '
        'for expected realized DSR + RCR of the CURRENT offer cohort at window_end. '
        'Higher means preferred. Rejected offers remain in the denominators. Respect mandatory '
        'obligations, current flight occupancy, repairs, spares, announced rules, mission timing, '
        'whole-mission value and aircraft-hour demand. Candidates are already nominally validated. '
        'Heuristic proxies are public estimates, not realized outcomes. There is no terminal value '
        'or future hidden information. Use only this input. Scores are uncalibrated preferences, '
        'not probabilities or confidence, and need not sum to any number. Ties are allowed. '
        'Return only a JSON object with preference_scores containing every supplied short candidate '
        'ID exactly once as a key and its integer score as the value. No other fields or prose. '
        'The input is losslessly factored: {$ref:N} refers to definitions[N]; '
        '{$columns:[keys],$rows:[values]} is a table of objects. candidate_mapping maps short IDs '
        'to original IDs. No information was rounded or omitted.'),
    'public-portfolio-scores-v2-b': (
        'Assign a 0..100 integer preference to each candidate portfolio using only the supplied '
        'public state. Prefer higher expected realized DSR + RCR for the CURRENT offer cohort '
        'at window_end; rejected offers stay in the denominators. Consider mandatory obligations, '
        'flight occupancy, repairs, spares, announced rules, mission timing, whole-mission value '
        'and aircraft-hour demand. All candidates passed nominal validation. Public heuristic '
        'estimates are not outcomes; there is no hidden future information or terminal value. '
        'These integers are uncalibrated preferences, not probabilities, confidence or long-term '
        'values. Ties are valid and scores need not sum to a fixed total. Fill the required '
        'preference_scores object: one integer for EVERY short ID in candidate_mapping, no omitted '
        'or additional keys. Return that JSON object only. Input tables encode objects with '
        '{$columns:[keys],$rows:[values]}; {$ref:N} expands definitions[N]. candidate_mapping '
        'reversibly maps short IDs to original stable IDs. Input is complete and unrounded.')}
SCORE_CORRECTION = ('FORMAT ERROR. Return only {"preference_scores":{...}}. Include EVERY required '
                    'short candidate ID exactly once as a key, with an integer from 0 to 100. '
                    'No extra keys, boolean values, decimals, text or other top-level fields.')


def score_schema(ids):
    keys=list(ids)
    return {'type':'object','properties':{'preference_scores':{
        'type':'object','properties':{key:{'type':'integer','minimum':0,'maximum':100} for key in keys},
        'required':keys,'additionalProperties':False}},'required':['preference_scores'],'additionalProperties':False}


def validate_preference_scores(value,mapping):
    """Parse raw output strictly, then rank using original stable candidate IDs."""
    if isinstance(value,str):
        def pairs(items):
            result={}
            for key,item in items:
                if key in result:raise ValueError('duplicate JSON key')
                result[key]=item
            return result
        def invalid_constant(value):raise ValueError('non-finite JSON value')
        value=json.loads(value,object_pairs_hook=pairs,parse_constant=invalid_constant)
    if type(value) is not dict or set(value)!={'preference_scores'}:raise ValueError('preference score shape')
    scores=value['preference_scores']
    if type(scores) is not dict or set(scores)!=set(mapping):raise ValueError('preference score keys')
    if any(type(score) is not int or not 0<=score<=100 for score in scores.values()):
        raise ValueError('preference scores must be integers from 0 to 100')
    if type(mapping) is not dict or any(type(key) is not str or type(item) is not str for key,item in mapping.items()) or len(set(mapping.values()))!=len(mapping):
        raise ValueError('short candidate mapping must be one-to-one')
    ranked=sorted(scores,key=lambda key:(-scores[key],mapping[key]))
    counts=Counter(scores.values())
    pairs=len(scores)*(len(scores)-1)//2
    tied_pairs=sum(count*(count-1)//2 for count in counts.values())
    return {'preference_scores':{key:scores[key] for key in mapping},
            'ranked_short_ids':ranked,'ranked_ids':[mapping[key] for key in ranked],
            'ties':{'has_ties':any(count>1 for count in counts.values()),
                    'all_tied':len(scores)>1 and len(counts)==1,'tied_pairs':tied_pairs,
                    'pair_count':pairs,'tied_pair_fraction':tied_pairs/pairs if pairs else 0.}}

def compact(value): return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)

def _table(value):
    if isinstance(value,dict): return {k:_table(v) for k,v in value.items()}
    if isinstance(value,list):
        if len(value)>1 and all(isinstance(x,dict) and set(x)==set(value[0]) for x in value):
            keys=sorted(value[0])
            return {'$columns':keys,'$rows':[[_table(x[k]) for k in keys] for x in value]}
        return [_table(x) for x in value]
    return value

def factor(value):
    value=_table(value); counts=Counter()
    def count(x):
        if isinstance(x,(dict,list)):
            counts[compact(x)]+=1
            for v in (x.values() if isinstance(x,dict) else x): count(v)
    count(value); definitions={}; refs={}
    def encode(x):
        if not isinstance(x,(dict,list)): return x
        key=compact(x)
        if counts[key]>1 and len(key)>100:
            if key in refs: return {'$ref':refs[key]}
            name=f'd{len(refs)}'; refs[key]=name
            definitions[name]=None
            definitions[name]=children(x)
            return {'$ref':name}
        return children(x)
    def children(x):
        return {k:encode(v) for k,v in x.items()} if isinstance(x,dict) else [encode(v) for v in x]
    data=encode(value)
    return {'definitions':definitions,'data':data}


def factor_canonical(value):
    """Canonical JSON-object traversal, with reversible reserved-marker escaping.

    This deliberately leaves ``factor`` unchanged for historical v1/v2 prompts.
    Sorting applies only to object keys. Lists and exact JSON numbers are retained.
    """
    compact(value)  # Reject non-JSON/non-finite values before constructing references.
    def prepare(x):
        if isinstance(x,dict):
            if any(type(key) is not str for key in x):
                raise ValueError('JSON object keys must be strings')
            items=[(key,prepare(x[key])) for key in sorted(x)]
            if set(x) in ({'$ref'},{'$columns','$rows'},{'$literal'}):
                return {'$literal':[[key,item] for key,item in items]}
            return dict(items)
        if isinstance(x,list):return [prepare(item) for item in x]
        return x
    return {**factor(prepare(value)),'factor_encoding':CANONICAL_FACTOR_ENCODING}


def unfactor(packed):
    encoding=packed.get('factor_encoding')
    if encoding not in (None,CANONICAL_FACTOR_ENCODING):
        raise ValueError('unsupported factor encoding')
    def decode(x):
        if isinstance(x,dict):
            if set(x)=={'$ref'}: return decode(packed['definitions'][x['$ref']])
            if set(x)=={'$columns','$rows'}:
                columns,rows=decode(x['$columns']),decode(x['$rows'])
                if encoding==CANONICAL_FACTOR_ENCODING and columns==['$literal']:
                    return [dict(row[0]) for row in rows]
                return [dict(zip(columns,row)) for row in rows]
            if encoding==CANONICAL_FACTOR_ENCODING and set(x)=={'$literal'}:
                return dict(decode(x['$literal']))
            return {k:decode(v) for k,v in x.items()}
        return [decode(v) for v in x] if isinstance(x,list) else x
    return decode(packed['data'])

def _render_input(legal,pool,factorize,*,preserve_candidate_order=False):
    candidates=list(pool) if preserve_candidate_order else sorted(pool,key=lambda c:c.id)
    canonical={'decision':legal.to_dict(),'candidates':[x.to_dict() for x in candidates]}
    _public(canonical)
    mapping={f'C{i:02d}':c['id'] for i,c in enumerate(sorted(canonical['candidates'],key=lambda c:c['id']))}
    reverse={v:k for k,v in mapping.items()}
    def shorten(x):
        if isinstance(x,dict): return {k:shorten(v) for k,v in x.items()}
        if isinstance(x,list): return [shorten(v) for v in x]
        return reverse.get(x,x) if isinstance(x,str) else x
    shortened=shorten(canonical); packed=factorize(shortened)
    if compact(unfactor(packed))!=compact(shortened): raise ValueError('lossless rendering roundtrip failed')
    content={'candidate_mapping':mapping,**packed}
    return {'canonical':canonical,'canonical_sha256':canonical_sha256(canonical),
            'candidate_mapping':mapping,'mapping_sha256':canonical_sha256(mapping),
            'rendered_input':content}


def render(legal,pool):
    old=_render_input(legal,pool,factor)
    content=old['rendered_input'];mapping=old['candidate_mapping']
    output_contract='\nOUTPUT CONTRACT: '+compact(schema(mapping))+'\nUse EVERY ID in this list EXACTLY ONCE: '+compact(list(mapping))+'. Before returning, check that sorting your ranked_ids gives this exact list. A repeated ID or a missing ID is invalid.'
    prompt='<|im_start|>system\n'+INSTRUCTIONS+'<|im_end|>\n<|im_start|>user\n'+compact(content)+output_contract+'<|im_end|>\n<|im_start|>assistant\n'
    return {**old,'prompt':prompt,'prompt_sha256':canonical_sha256(prompt)}


def render_v2(legal,pool,template='public-portfolio-scores-v2-a'):
    if template not in SCORE_TEMPLATES:raise ValueError('unregistered score template')
    # Reuse the exact lossless factorization, never the v1 output contract.
    old=render(legal,pool)
    return _render_scores(old,template,SCORE_RENDERER)


def render_v3(legal,pool,template='public-portfolio-scores-v2-a'):
    """Scores-v2 schema with the separate canonical renderer-v3 input format."""
    if template not in SCORE_TEMPLATES:raise ValueError('unregistered score template')
    return _render_scores(_render_input(legal,pool,factor_canonical,preserve_candidate_order=True),template,CANONICAL_RENDERER)


def decode_v4_input(content):
    """Recover canonical input from the exact JSON object sent in renderer-v4.

    Only top-level candidate-record IDs are references. All other JSON strings
    are literals, even if they equal an alias or original candidate ID.
    """
    required={'candidate_mapping','candidate_id_encoding','definitions','data','factor_encoding'}
    if type(content) is not dict or set(content)!=required:
        raise ValueError('invalid v4 rendered input shape')
    if (content['candidate_id_encoding']!=SCOPED_CANDIDATE_ENCODING or
        content['factor_encoding']!=CANONICAL_FACTOR_ENCODING):
        raise ValueError('unsupported v4 input encoding')
    mapping=content['candidate_mapping']
    if (type(mapping) is not dict or
        any(type(key) is not str or type(value) is not str or not value for key,value in mapping.items()) or
        len(set(mapping.values()))!=len(mapping) or
        mapping!={f'C{index:02d}':value for index,value in enumerate(sorted(mapping.values()))}):
        raise ValueError('invalid v4 candidate mapping')
    canonical=unfactor(content)
    if (type(canonical) is not dict or set(canonical)!={'decision','candidates'} or
        type(canonical['decision']) is not dict or type(canonical['candidates']) is not list):
        raise ValueError('invalid v4 canonical input shape')
    ids=[]
    for candidate in canonical['candidates']:
        if (type(candidate) is not dict or set(candidate)!={'id','command','summary'} or
            type(candidate['id']) is not str or candidate['id'] not in mapping):
            raise ValueError('invalid v4 candidate ID reference')
        ids.append(candidate['id'])
        candidate['id']=mapping[candidate['id']]
    if len(ids)!=len(mapping) or set(ids)!=set(mapping):
        raise ValueError('v4 candidate references must match mapping exactly once')
    return canonical


def render_v4(legal,pool,template='public-portfolio-scores-v2-a'):
    """Scores-v2 with field-scoped references and a full canonical round trip.

    Supports public JSON objects with string keys, ordered JSON arrays, strings,
    booleans, null, integers and finite floats. Object insertion order is not
    semantic; JSON numeric types and representations and all array orders are.
    """
    if template not in SCORE_TEMPLATES:raise ValueError('unregistered score template')
    canonical={'decision':legal.to_dict(),'candidates':[candidate.to_dict() for candidate in pool]}
    _public(canonical)
    ids=[candidate['id'] for candidate in canonical['candidates']]
    if any(type(value) is not str or not value for value in ids) or len(set(ids))!=len(ids):
        raise ValueError('candidate IDs must be unique nonempty strings')
    mapping={f'C{index:02d}':value for index,value in enumerate(sorted(ids))}
    reverse={value:key for key,value in mapping.items()}
    scoped=deepcopy(canonical)
    for candidate in scoped['candidates']:candidate['id']=reverse[candidate['id']]
    content={'candidate_mapping':mapping,'candidate_id_encoding':SCOPED_CANDIDATE_ENCODING,
             **factor_canonical(scoped)}
    # Exercise the actual wire JSON, including the factoring and scoped-ID decode.
    # compact distinguishes int/float/bool and signed floating zero, unlike ==.
    restored=decode_v4_input(json.loads(compact(content)))
    if compact(restored)!=compact(canonical):
        raise ValueError('v4 canonical-to-wire-to-canonical roundtrip failed')
    value={'canonical':canonical,'canonical_sha256':canonical_sha256(canonical),
           'candidate_mapping':mapping,'mapping_sha256':canonical_sha256(mapping),'rendered_input':content}
    return _render_scores(value,template,REVERSIBLE_RENDERER)


def _render_scores(old,template,renderer):
    contract='\nOUTPUT CONTRACT: '+compact(score_schema(old['candidate_mapping']))
    contract+='\nRequired score keys: '+compact(list(old['candidate_mapping']))
    contract+='\nReturn exactly one integer in [0,100] for each required key. Ties are allowed.'
    note=(REVERSIBLE_RENDERING_NOTE if renderer==REVERSIBLE_RENDERER else
          CANONICAL_RENDERING_NOTE if renderer==CANONICAL_RENDERER else '')
    prompt=('<|im_start|>system\n'+SCORE_TEMPLATES[template]+note+'<|im_end|>\n<|im_start|>user\n'+
            compact(old['rendered_input'])+contract+'<|im_end|>\n<|im_start|>assistant\n')
    return {**old,'prompt':prompt,'prompt_sha256':canonical_sha256(prompt),
            'protocol':SCORE_PROTOCOL,'renderer':renderer,'template':template,
            'schema_sha256':canonical_sha256(score_schema(old['candidate_mapping'])),
            'sort_rule':SCORE_SORT_RULE}

def schema(ids):
    return {'type':'object','properties':{'ranked_ids':{'type':'array','items':{'type':'string','enum':list(ids)},
            'minItems':len(ids),'maxItems':len(ids),'uniqueItems':True}},'required':['ranked_ids'],'additionalProperties':False}

def validate_ranking(value,ids):
    if isinstance(value,str):
        def pairs(items):
            result={}
            for k,v in items:
                if k in result: raise ValueError('duplicate JSON key')
                result[k]=v
            return result
        value=json.loads(value,object_pairs_hook=pairs)
    if type(value) is not dict or set(value)!={'ranked_ids'}: raise ValueError('ranking shape')
    ranked=value['ranked_ids']
    if type(ranked) is not list or any(type(x) is not str for x in ranked): raise ValueError('ranking ID types')
    if len(ranked)!=len(ids) or len(set(ranked))!=len(ranked) or set(ranked)!=set(ids):
        raise ValueError('ranking must be a complete permutation')
    return ranked

def identity(manifest,*,num_ctx=8192):
    return {'provider':'ollama','model':MODEL,'model_digest':manifest['digest'],'quantization':'Q4_K_M',
            'runtime_version':manifest['version']['version'],'runtime_sha256':manifest['runtime_sha256'],
            'runtime_template_sha256':canonical_sha256(manifest['show']['template']),
            'protocol':PROTOCOL,'renderer':RENDERER,'template':TEMPLATE,
            'template_sha256':canonical_sha256(INSTRUCTIONS),'correction_sha256':canonical_sha256(CORRECTION),
            'decoding':{'temperature':0,'seed':42,'num_predict':1024,'num_ctx':num_ctx,'num_batch':512},
            'request_batch_size':1,'request_concurrency':1,
            'prompt_format':'explicit-chatml-raw-v1','score_semantics':'ordinal ranking only; no probabilities or calibrated confidence'}


def identity_v2(manifest,*,template='public-portfolio-scores-v2-a',num_ctx=16384,
                arithmetic_protocol='event-anchor-binary64-v2',request_protocol='legal-weekly-request-v2',
                tokenizer_sha256,tokenizer_runtime):
    if template not in SCORE_TEMPLATES:raise ValueError('unregistered score template')
    value=identity(manifest,num_ctx=num_ctx)
    value.update(protocol=SCORE_PROTOCOL,renderer=SCORE_RENDERER,template=template,
                 template_sha256=canonical_sha256(SCORE_TEMPLATES[template]),
                 correction_sha256=canonical_sha256(SCORE_CORRECTION),score_semantics=SCORE_SEMANTICS,
                 schema_version='required-candidate-integer-0-100-v2',
                 schema_builder_sha256=canonical_sha256(score_schema(['C00','C01'])),
                 sort_rule=SCORE_SORT_RULE,arithmetic_protocol=arithmetic_protocol,request_protocol=request_protocol,
                 tokenizer_sha256=tokenizer_sha256,tokenizer_runtime=deepcopy(tokenizer_runtime))
    validate_identity(value)
    return value


def identity_v3(manifest,**kwargs):
    """Explicit opt-in to canonical renderer-v3; scoring remains scores-v2."""
    value=identity_v2(manifest,**kwargs)
    value['renderer']=CANONICAL_RENDERER
    validate_identity(value)
    return value


def identity_v4(manifest,**kwargs):
    """Opt in to reversible scoped IDs; retain the strict scores-v2 schema."""
    value=identity_v2(manifest,**kwargs)
    value['renderer']=REVERSIBLE_RENDERER
    value['template_sha256']=canonical_sha256(SCORE_TEMPLATES[value['template']]+REVERSIBLE_RENDERING_NOTE)
    validate_identity(value)
    return value


def validate_identity(value):
    required={'provider','model','model_digest','quantization','runtime_version','runtime_sha256','runtime_template_sha256',
              'protocol','renderer','template','template_sha256','correction_sha256','decoding','request_batch_size',
              'request_concurrency','prompt_format','score_semantics'}
    is_scores=isinstance(value,dict) and value.get('protocol')==SCORE_PROTOCOL
    if is_scores:
        required|={'schema_version','schema_builder_sha256','sort_rule','arithmetic_protocol','request_protocol',
                   'tokenizer_sha256','tokenizer_runtime'}
    if type(value) is not dict or set(value)!=required:raise ValueError('local identity shape mismatch')
    canonical_sha256(value)
    if (value['provider']!='ollama' or value['model']!=MODEL or value['quantization']!='Q4_K_M' or
        value['protocol'] not in (PROTOCOL,SCORE_PROTOCOL) or value['request_batch_size']!=1 or value['request_concurrency']!=1):
        raise ValueError('unsupported local model identity')
    digest=value['model_digest']
    if type(digest) is not str or not digest.startswith('sha256:') or len(digest)!=71 or any(c not in '0123456789abcdef' for c in digest[7:]):
        raise ValueError('invalid complete model digest')
    for key in ('runtime_sha256','runtime_template_sha256','template_sha256','correction_sha256'):
        if type(value[key]) is not str or len(value[key])!=64 or any(c not in '0123456789abcdef' for c in value[key]):
            raise ValueError('invalid identity hash')
    options=value['decoding']
    if type(options) is not dict or set(options)!={'temperature','seed','num_predict','num_ctx','num_batch'}:
        raise ValueError('invalid decoding shape')
    if options['temperature']!=0 or options['seed']!=42 or options['num_predict']!=1024 or options['num_ctx'] not in (8192,16384) or options['num_batch']!=512:
        raise ValueError('unsupported frozen decoding')
    if value['prompt_format']!='explicit-chatml-raw-v1':
        raise ValueError('unsupported local prompt format')
    if is_scores:
        template=value['template']
        note=REVERSIBLE_RENDERING_NOTE if value['renderer']==REVERSIBLE_RENDERER else ''
        if (template not in SCORE_TEMPLATES or value['renderer'] not in (SCORE_RENDERER,CANONICAL_RENDERER,REVERSIBLE_RENDERER) or
            value['template_sha256']!=canonical_sha256(SCORE_TEMPLATES[template]+note) or
            value['correction_sha256']!=canonical_sha256(SCORE_CORRECTION) or
            value['schema_version']!='required-candidate-integer-0-100-v2' or
            value['schema_builder_sha256']!=canonical_sha256(score_schema(['C00','C01'])) or
            value['sort_rule']!=SCORE_SORT_RULE or value['score_semantics']!=SCORE_SEMANTICS or
            value['arithmetic_protocol']!='event-anchor-binary64-v2' or
            value['request_protocol']!='legal-weekly-request-v2' or options['num_ctx']!=16384):
            raise ValueError('unsupported score protocol identity')
        digest=value['tokenizer_sha256']
        if type(digest) is not str or len(digest)!=64 or any(c not in '0123456789abcdef' for c in digest):
            raise ValueError('invalid tokenizer identity')
        if type(value['tokenizer_runtime']) is not dict or not value['tokenizer_runtime']:
            raise ValueError('tokenizer runtime identity required')
    elif (value['renderer']!=RENDERER or value['template']!=TEMPLATE or
          value['template_sha256']!=canonical_sha256(INSTRUCTIONS) or
          value['correction_sha256']!=canonical_sha256(CORRECTION) or
          value['score_semantics']!='ordinal ranking only; no probabilities or calibrated confidence'):
        raise ValueError('unsupported ranking protocol identity')


def render_for_identity(legal,pool,ident):
    validate_identity(ident)
    if ident['protocol']==SCORE_PROTOCOL:
        if legal.protocol_version!=ident['request_protocol']:raise LocalError('request_protocol_mismatch')
        if ident['renderer']==SCORE_RENDERER:return render_v2(legal,pool,ident['template'])
        if ident['renderer']==CANONICAL_RENDERER:return render_v3(legal,pool,ident['template'])
        if ident['renderer']==REVERSIBLE_RENDERER:return render_v4(legal,pool,ident['template'])
        raise ValueError('unsupported score renderer')
    return render(legal,pool)


def _output_schema(ident,mapping):
    return score_schema(mapping) if ident['protocol']==SCORE_PROTOCOL else schema(mapping)


def _attempt_prompt(rendered,ident,index,first_response=''):
    if not index:return rendered['prompt']
    correction=(SCORE_CORRECTION+' Required score keys: '+compact(list(rendered['candidate_mapping']))
                if ident['protocol']==SCORE_PROTOCOL else
                CORRECTION+' Required IDs exactly once: '+compact(list(rendered['candidate_mapping'])))
    return (rendered['prompt']+first_response+'<|im_end|>\n<|im_start|>user\n'+correction+
            '<|im_end|>\n<|im_start|>assistant\n')

class LocalError(RuntimeError):
    def __init__(self,code,audit=None): self.code=code; self.audit=audit or {}; super().__init__(code)

class GenerationBudget:
    """Single-writer durable quota. An interrupted reservation fails closed."""
    def __init__(self,path,limits=None,max_seconds=7200):
        self.path=Path(path); self.limits=limits or {'development':80,'diagnostics':48,'pilot':200}; self.max_seconds=max_seconds
    def read(self):
        value=json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else {'attempts':[], 'seconds':0.}
        if 'limits' in value and (value['limits']!=self.limits or value['max_seconds']!=self.max_seconds):
            raise LocalError('campaign_budget_identity_mismatch')
        return value
    def reserve(self,phase,key,*,logical_request_id=None,attempt_index=None):
        v=self.read()
        if any(x['status']=='pending' or x['status']=='uncertain' for x in v['attempts']): raise LocalError('uncertain_prior_generation')
        if phase not in self.limits:raise LocalError('unknown_generation_phase')
        if sum(x['phase']==phase for x in v['attempts'])>=self.limits[phase] or v['seconds']>=self.max_seconds:
            raise LocalError('local_budget_exhausted')
        if logical_request_id is not None:
            previous=[row for row in v['attempts'] if row.get('logical_request_id')==logical_request_id]
            if attempt_index not in (0,1) or len(previous)!=attempt_index:
                raise LocalError('logical_request_attempt_limit')
        v.setdefault('protocol','durable-generation-campaign-v2')
        v.setdefault('campaign_id',uuid.uuid4().hex)
        v.setdefault('limits',deepcopy(self.limits));v.setdefault('max_seconds',self.max_seconds)
        v['attempts'].append({'phase':phase,'key':key,'status':'pending','started_unix':time.time(),
                              'attempt_id':uuid.uuid4().hex,'logical_request_id':logical_request_id,
                              'attempt_index':attempt_index})
        _atomic_json(self.path,v); return len(v['attempts'])-1,min(120.,self.max_seconds-v['seconds'])
    def finish(self,index,seconds,status,*,artifact=None):
        v=self.read()
        if status not in ('completed','uncertain') or v['attempts'][index]['status']!='pending':
            raise LocalError('invalid_generation_transition')
        if type(seconds) not in (int,float) or not math.isfinite(seconds) or seconds<0:
            raise ValueError('invalid generation duration')
        v['attempts'][index].update(seconds=seconds,status=status)
        if artifact is not None:v['attempts'][index]['artifact']=deepcopy(artifact)
        v['seconds']+=seconds; _atomic_json(self.path,v)

class LocalProvider:
    def __init__(self,identity,store=None,*,mode='cache_only',base_url='http://127.0.0.1:11439',budget=None,
                 phase='development',transport=None,token_counter=None):
        validate_identity(identity)
        self.identity=deepcopy(identity); self.store=Path(store) if store else None
        self.mode=mode; self.budget=budget; self.phase=phase; self.transport=transport; self.token_counter=token_counter
        self.metadata_queries=0; self.generations=0; self.audit_log=[]
        u=urlsplit(base_url)
        if u.scheme!='http' or u.hostname!='127.0.0.1' or u.username or u.password or u.path or u.query or u.fragment:
            raise ValueError('local provider requires literal loopback URL')
        if mode not in {'live','cache_only','fixture'} or (transport is not None and mode!='fixture'):
            raise ValueError('injected transports are fixtures only')
        if mode=='live' and (budget is None or self.store is None or token_counter is None):
            raise ValueError('live requires durable budget, store, and independent token counter')
        self.base_url=base_url

    def _http(self,route,data=None,timeout=120):
        opener=http.build_opener(http.ProxyHandler({}),_RejectRedirect())
        req=http.Request(self.base_url+route,data=None if data is None else compact(data).encode(),headers={'Content-Type':'application/json'})
        with opener.open(req,timeout=timeout) as result: return json.load(result)

    def _generate(self,body,timeout):
        # A socket timeout alone does not bound a slow-drip response. On deadline
        # the reservation remains uncertain; no second request may be submitted.
        queue=Queue(maxsize=1)
        def work():
            try:queue.put((True,self._http('/api/generate',body,timeout)))
            except Exception as exc:queue.put((False,exc))
        Thread(target=work,daemon=True).start()
        try:okay,value=queue.get(timeout=timeout)
        except Empty as exc:raise TimeoutError('absolute local generation deadline') from exc
        if not okay:raise value
        return value

    def verify_model(self):
        self.metadata_queries+=1; tags=self._http('/api/tags')
        found=next((x for x in tags['models'] if x['name']==self.identity['model']),None)
        if found is None or found['digest'].removeprefix('sha256:')!=self.identity['model_digest'].removeprefix('sha256:'):
            raise LocalError('model_identity_mismatch')
        self.metadata_queries+=1; version=self._http('/api/version')
        if version['version']!=self.identity['runtime_version']: raise LocalError('runtime_identity_mismatch')

    def rank(self,legal,pool,*,force=False):
        started=time.perf_counter()
        render_started=time.perf_counter()
        try:
            rendered=render_for_identity(legal,pool,self.identity)
        finally:
            # Passive execution timing stays outside scientific/cache records.
            self.last_render_seconds=time.perf_counter()-render_started
        is_scores=self.identity['protocol']==SCORE_PROTOCOL
        key=canonical_sha256({'identity':self.identity,'rendered':rendered})
        if is_scores and len(pool)<2:
            ids=sorted(candidate.id for candidate in pool)
            record={'protocol':SCORE_PROTOCOL,'identity':self.identity,'cache_key':key,'rendered':rendered,
                    'ranked_ids':ids,'choice':ids[0] if ids else None,'score':None,'probability':None,'confidence':None,
                    'score_semantics':SCORE_SEMANTICS,'preference_scores':None,'ties':None,'sort_rule':SCORE_SORT_RULE,
                    'valid_local_model':False,'valid_jev':False,'evidence':'deterministic','attempts':[],
                    'local_generation_attempts':0,'local_metadata_queries':0,'remote_typesafe_requests':0,
                    'actual_input_tokens':0,'actual_output_tokens':0,'cache_seconds':0.,'generation_seconds':0.,
                    'score_parse_sort_seconds':0.,'full_request_verified':False,'first_round_valid':None,
                    'api_skipped_reason':'empty_pool' if not pool else 'singleton_pool'}
            self.audit_log.append(record);return record
        path=self.store/(key+'.json') if self.store else None
        if path and path.exists() and not force and self.mode!='fixture':
            value=json.loads(path.read_text(encoding='utf-8')); validate_record(value,legal,pool,self.identity)
            if value['cache_key']!=key or value['evidence']!='local_inference': raise LocalError('cache_identity_mismatch')
            value=deepcopy(value); value.update(evidence='local_replay',local_generation_attempts=0,local_metadata_queries=0,
                                               cache_seconds=time.perf_counter()-started,actual_input_tokens=0,actual_output_tokens=0)
            if is_scores:
                value.update(source_generation_seconds=value['generation_seconds'],generation_seconds=0.,
                             score_parse_sort_seconds=None)
            self.audit_log.append(value); return value
        if self.mode=='cache_only': raise LocalError('local_cache_miss')
        before=self.metadata_queries
        if self.mode=='live': self.verify_model()
        mapping=rendered['candidate_mapping']; attempts=[]; ranked=None; parsed_scores=None;parse_seconds=0.
        logical_request_id=uuid.uuid4().hex
        for index in range(2):
            prompt=_attempt_prompt(rendered,self.identity,index,attempts[0]['raw'].get('response','') if index else '')
            options=self.identity['decoding']; tokens=self.token_counter(prompt) if self.token_counter else None
            if self.mode=='live' and tokens+options['num_predict']>options['num_ctx']:
                raise LocalError('full_prompt_exceeds_context',{'prompt_tokens':tokens,'num_ctx':options['num_ctx']})
            body={'model':self.identity['model'],'prompt':prompt,'raw':True,'stream':False,'format':_output_schema(self.identity,mapping),
                  'options':options,'keep_alive':'24h','truncate':False,'shift':False}
            timer=time.perf_counter(); reservation=None
            if self.mode=='live':
                reservation,timeout=self.budget.reserve(self.phase,key,logical_request_id=logical_request_id,attempt_index=index)
                attempt_id=self.budget.read()['attempts'][reservation]['attempt_id']
            else: timeout=120
            try:
                self.generations+=int(self.mode=='live')
                raw=self.transport(body) if self.mode=='fixture' else self._generate(body,timeout)
            except (TimeoutError,socket.timeout) as exc:
                if reservation is not None: self.budget.finish(reservation,time.perf_counter()-timer,'uncertain')
                if self.store and self.mode=='live':
                    _atomic_json(self.store/(key+'.failure.json'),{'code':'timeout_uncertain','attempts':attempts,'identity':self.identity,'rendered':rendered})
                raise LocalError('timeout_uncertain') from exc
            except Exception as exc:
                if reservation is not None: self.budget.finish(reservation,time.perf_counter()-timer,'uncertain')
                if self.store and self.mode=='live':
                    _atomic_json(self.store/(key+'.failure.json'),{'code':'transport_uncertain','error_type':type(exc).__name__,
                                 'attempts':attempts,'identity':self.identity,'rendered':rendered})
                raise LocalError('transport_uncertain') from exc
            elapsed=time.perf_counter()-timer
            attempt={'raw':raw,'body':body,'seconds':elapsed,'independent_prompt_tokens':tokens}
            if reservation is not None:
                attempt.update(attempt_id=attempt_id,logical_request_id=logical_request_id,attempt_index=index)
                attempt_path=self.store/('attempt-'+attempt_id+'.json')
                # Immutable per-attempt evidence is durable before releasing the
                # pending reservation. A crash in between fails closed.
                if attempt_path.exists():raise LocalError('generation_attempt_id_collision')
                evidence={'identity':self.identity,'cache_key':key,'rendered':rendered,'attempt':attempt}
                _atomic_json(attempt_path,evidence)
                self.budget.finish(reservation,elapsed,'completed',artifact={
                    'path':attempt_path.name,'sha256':canonical_sha256(evidence)})
            attempts.append(attempt)
            if self.store and self.mode=='live': _atomic_json(self.store/(key+'.attempts.json'),{'identity':self.identity,'rendered':rendered,'attempts':attempts})
            if raw.get('model')!=self.identity['model']: raise LocalError('response_model_mismatch')
            if self.mode=='live' and (raw.get('prompt_eval_count')!=tokens or raw.get('done_reason')!='stop' or raw.get('done') is not True):
                raise LocalError('truncation_or_token_count_mismatch',attempt)
            try:
                parse_started=time.perf_counter()
                if is_scores:
                    parsed_scores=validate_preference_scores(raw.get('response'),mapping)
                    ranked=parsed_scores['ranked_short_ids']
                else:ranked=validate_ranking(raw.get('response'),mapping)
                break
            except (ValueError,TypeError) as exc:
                attempt['format_error']=str(exc)
            finally:parse_seconds+=time.perf_counter()-parse_started
        if ranked is None: raise LocalError('invalid_local_scores' if is_scores else 'invalid_local_ranking',{'attempts':attempts})
        ids=[mapping[k] for k in ranked]
        record={'protocol':self.identity['protocol'],'identity':self.identity,'cache_key':key,'rendered':rendered,
                'ranked_ids':ids,'choice':ids[0] if ids else None,'score':None,'probability':None,'confidence':None,
                'score_semantics':self.identity['score_semantics'],'valid_local_model':self.mode=='live','valid_jev':False,
                'evidence':'local_inference' if self.mode=='live' else 'fixture','attempts':attempts,
                'local_generation_attempts':len(attempts) if self.mode=='live' else 0,
                'local_metadata_queries':self.metadata_queries-before,'remote_typesafe_requests':0,
                'actual_input_tokens':sum(x['raw'].get('prompt_eval_count',0) for x in attempts) if self.mode=='live' else 0,
                'actual_output_tokens':sum(x['raw'].get('eval_count',0) for x in attempts) if self.mode=='live' else 0,
                'cache_seconds':0.,'generation_seconds':sum(x['seconds'] for x in attempts),
                'full_request_verified':self.mode=='live','first_round_valid':len(attempts)==1}
        if is_scores:
            record.update(preference_scores=parsed_scores['preference_scores'],ties=parsed_scores['ties'],
                          sort_rule=SCORE_SORT_RULE,logical_request_id=logical_request_id,
                          score_parse_sort_seconds=parse_seconds)
        if self.mode=='live': _atomic_json(path,record)
        self.audit_log.append(record); return record

def validate_record(value,legal,pool,expected_identity):
    validate_identity(expected_identity)
    rendered=render_for_identity(legal,pool,expected_identity)
    is_scores=expected_identity['protocol']==SCORE_PROTOCOL
    if (canonical_sha256(value.get('identity'))!=canonical_sha256(expected_identity) or
        canonical_sha256(value.get('rendered'))!=canonical_sha256(rendered) or
        value.get('protocol')!=expected_identity['protocol']):
        raise LocalError('cache_identity_mismatch')
    key=canonical_sha256({'identity':expected_identity,'rendered':rendered})
    if value.get('cache_key')!=key: raise LocalError('cache_key_mismatch')
    ids=validate_ranking({'ranked_ids':value.get('ranked_ids')},[c.id for c in pool])
    if is_scores and len(pool)<2:
        if (value.get('evidence')!='deterministic' or value.get('valid_local_model') is not False or
            value.get('valid_jev') is not False or value.get('attempts')!=[] or
            value.get('preference_scores') is not None or value.get('ties') is not None or
            value.get('choice')!=(ids[0] if ids else None) or
            any(value.get(k)!=0 for k in ('local_generation_attempts','local_metadata_queries','remote_typesafe_requests'))):
            raise LocalError('invalid_deterministic_local_evidence')
        return value
    if not isinstance(value.get('attempts'),list) or not 1<=len(value['attempts'])<=2:raise LocalError('invalid_attempt_count')
    if is_scores:
        parsed=validate_preference_scores(value['attempts'][-1]['raw']['response'],rendered['candidate_mapping'])
        raw_rank=parsed['ranked_short_ids']
        if (value.get('preference_scores')!=parsed['preference_scores'] or value.get('ties')!=parsed['ties'] or
            value.get('sort_rule')!=SCORE_SORT_RULE):raise LocalError('cache_response_mismatch')
    else:raw_rank=validate_ranking(value['attempts'][-1]['raw']['response'],rendered['candidate_mapping'])
    if ids!=[rendered['candidate_mapping'][x] for x in raw_rank] or value.get('choice')!=(ids[0] if ids else None):
        raise LocalError('cache_response_mismatch')
    if value.get('evidence') not in {'fixture','local_inference','local_replay'} or value.get('valid_jev') is not False:
        raise LocalError('invalid_local_evidence')
    if value.get('valid_local_model') is not (value['evidence']!='fixture'):
        raise LocalError('invalid_local_validity')
    if not 1<=len(value['attempts'])<=2:raise LocalError('invalid_attempt_count')
    for index,attempt in enumerate(value['attempts']):
        expected_prompt=_attempt_prompt(rendered,expected_identity,index,value['attempts'][0]['raw'].get('response','') if index else '')
        body=attempt['body']
        if (body.get('prompt')!=expected_prompt or
            canonical_sha256(body.get('format'))!=canonical_sha256(_output_schema(expected_identity,rendered['candidate_mapping']))):
            raise LocalError('cached_prompt_or_schema_mismatch')
        if (canonical_sha256(body.get('options'))!=canonical_sha256(expected_identity['decoding']) or
            body.get('raw') is not True or body.get('stream') is not False or
            body.get('truncate') is not False or body.get('shift') is not False):
            raise LocalError('cached_inference_metadata_mismatch')
    if value.get('score_semantics')!=expected_identity['score_semantics']:raise LocalError('invalid_score_semantics')
    if value['evidence']!='fixture':
        if value.get('full_request_verified') is not True:raise LocalError('unverified_full_request')
        for attempt in value['attempts']:
            raw,body=attempt['raw'],attempt['body'];tokens=attempt['independent_prompt_tokens']
            if (raw.get('model')!=expected_identity['model'] or raw.get('prompt_eval_count')!=tokens or
                type(tokens) is not int or tokens+expected_identity['decoding']['num_predict']>expected_identity['decoding']['num_ctx'] or
                raw.get('done') is not True or raw.get('done_reason')!='stop' or
                body.get('model')!=expected_identity['model'] or
                body.get('truncate') is not False or body.get('shift') is not False or body.get('raw') is not True):
                raise LocalError('cached_inference_metadata_mismatch')
        if is_scores:
            attempts=value['attempts']
            if (len({attempt.get('attempt_id') for attempt in attempts})!=len(attempts) or
                any(type(attempt.get('attempt_id')) is not str or len(attempt['attempt_id'])!=32 for attempt in attempts) or
                any(attempt.get('attempt_index')!=index for index,attempt in enumerate(attempts)) or
                any(attempt.get('logical_request_id')!=value.get('logical_request_id') for attempt in attempts)):
                raise LocalError('cached_attempt_identity_mismatch')
            live=value['evidence']=='local_inference'
            expected={'local_generation_attempts':len(attempts) if live else 0,
                      'actual_input_tokens':sum(a['raw'].get('prompt_eval_count',0) for a in attempts) if live else 0,
                      'actual_output_tokens':sum(a['raw'].get('eval_count',0) for a in attempts) if live else 0,
                      'generation_seconds':sum(a['seconds'] for a in attempts) if live else 0.}
            if any(value.get(key)!=item for key,item in expected.items()):raise LocalError('local_actual_usage_mismatch')
    if value.get('first_round_valid') is not (len(value['attempts'])==1):
        raise LocalError('cached_correction_status_mismatch')
    if is_scores and len(value['attempts'])==2:
        try:validate_preference_scores(value['attempts'][0]['raw']['response'],rendered['candidate_mapping'])
        except (ValueError,TypeError):pass
        else:raise LocalError('unnecessary_score_correction')
    if any(value.get(k) is not None for k in ('score','probability','confidence')): raise LocalError('invalid_score_semantics')
    return value
