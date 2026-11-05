"""Explicit offline tokenizer dependency verification and renderer budget check."""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import zipfile

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from sim_jev.local_provider import CANONICAL_RENDERER,render_for_identity,validate_identity
from sim_jev.provenance import canonical_sha256,code_fingerprint
from scripts.phase2d_local import unpack
from scripts.phase2d_r2_delivery import digest,PARENT_SHA,write

TOKEN_CODE='''import json,sys,platform,tokenizers,socket
from unittest.mock import patch
from tokenizers import Tokenizer
with patch.object(socket,'socket',side_effect=AssertionError('network forbidden')):
 t=Tokenizer.from_file(sys.argv[1])
 prompts=json.load(sys.stdin)
 print(json.dumps({'python':platform.python_version(),'tokenizers':tokenizers.__version__,'executable':sys.executable,'counts':[len(t.encode(p,add_special_tokens=False).ids) for p in prompts]}))
'''
def verify(evidence,parent,python,tokenizer,output):
    if Path(output).exists():raise ValueError('output must be new')
    if digest(parent)!=PARENT_SHA:raise ValueError('fixed parent SHA mismatch')
    evidence=Path(evidence)
    freeze=json.loads((evidence/'freeze.json').read_text(encoding='utf-8'))
    expected=freeze['identity']
    asset_sha=digest(tokenizer);exe_sha=digest(python)
    if asset_sha!=expected['tokenizer_sha256']:raise ValueError('tokenizer asset SHA mismatch')
    if exe_sha!=expected['tokenizer_runtime']['python_executable_sha256']:raise ValueError('tokenizer executable SHA mismatch')
    prompts=[];old=[];new=[]
    with zipfile.ZipFile(parent) as z:
        m=json.loads(z.read('MANIFEST.json'));prefix='project/'+m['evidence_relative']+'/'
        budget=json.loads(z.read(prefix+'generation-budget.json'))
        index={}
        for name in z.namelist():
            if name.startswith(prefix) and '/attempt-' in name and name.endswith('.json'):
                index.setdefault(name.rsplit('/',1)[-1],[]).append(name)
        for reservation in budget['attempts']:
            paths=[p for p in index[reservation['artifact']['path']] if p.startswith(prefix+reservation['phase']+'/')]
            if len(paths)!=1:raise ValueError('attempt artifact ambiguous')
            artifact=json.loads(z.read(paths[0]))
            if canonical_sha256(artifact)!=reservation['artifact']['sha256']:raise ValueError('attempt content identity mismatch')
            attempt=artifact['attempt'];prompt=attempt['body']['prompt']
            if prompt!=artifact['rendered']['prompt']:raise ValueError('raw prompt differs')
            prompts.append(prompt)
            old.append(dict(attempt_id=reservation['attempt_id'],phase=reservation['phase'],
                            independent_prompt_tokens=attempt['independent_prompt_tokens'],
                            server_prompt_tokens=attempt['raw']['prompt_eval_count']))
    identities=json.loads((evidence/'development/identities.json').read_text(encoding='utf-8'))
    states=json.loads((evidence/'development/states.json').read_text(encoding='utf-8'))
    if len(states)!=8:raise ValueError('eight existing development inputs required')
    for template,ident in identities.items():
        ident=deepcopy(ident);ident['renderer']=CANONICAL_RENDERER;validate_identity(ident)
        for row in states:
            request,pool=unpack(row);rendered=render_for_identity(request,pool,ident)
            prompts.append(rendered['prompt'])
            new.append(dict(state_id=row['state_id'],template=template,renderer=ident['renderer'],
                            identity_sha256=canonical_sha256(ident),prompt_sha256=rendered['prompt_sha256'],
                            num_ctx=ident['decoding']['num_ctx'],num_predict=ident['decoding']['num_predict']))
    result=subprocess.run([str(python),'-c',TOKEN_CODE,str(tokenizer)],input=json.dumps(prompts,ensure_ascii=False),
                          capture_output=True,text=True,encoding='utf-8',timeout=60,check=True)
    runtime=json.loads(result.stdout)
    if runtime['tokenizers']!=expected['tokenizer_runtime']['tokenizers'] or runtime['python']!=expected['tokenizer_runtime']['python']:
        raise ValueError('tokenizer runtime version mismatch')
    counts=runtime.pop('counts')
    if len(counts)!=len(prompts):raise ValueError('token result count differs')
    for row,n in zip(old,counts):
        if n!=row['independent_prompt_tokens'] or n!=row['server_prompt_tokens']:raise ValueError('historical raw token count differs')
        row['recounted_tokens']=n
    for row,n in zip(new,counts[len(old):]):
        row['input_tokens']=n;row['remaining_tokens']=row['num_ctx']-row['num_predict']-n
        if row['remaining_tokens']<0:raise ValueError('complete development prompt exceeds context')
    if len(old)!=141:raise ValueError('141 original generations required')
    receipt=dict(status='passed',source=code_fingerprint(ROOT),parent_sha256=PARENT_SHA,
                 external_dependency=dict(tokenizer_python=str(python),python_executable_sha256=exe_sha,
                                          tokenizer_json=str(tokenizer),tokenizer_json_sha256=asset_sha,**runtime),
                 historical_attempts=old,new_renderer_development=new,
                 scope='existing development first-attempt prompts only; correction is checked by provider before use',
                 new_renderer_live_verified=False,new_des=0,new_model_generations=0,new_metadata_requests=0,new_http_requests=0)
    write(output,receipt)
    return dict(status='passed',historical_attempts=len(old),offline_development_prompts=len(new),
                max_input_tokens=max(r['input_tokens'] for r in new),new_renderer_live_verified=False)
def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('evidence-root','parent-archive','tokenizer-python','tokenizer-json','output'):p.add_argument('--'+name,type=Path,required=True)
    a=p.parse_args();print(json.dumps(verify(a.evidence_root,a.parent_archive,a.tokenizer_python,a.tokenizer_json,a.output)))
if __name__=='__main__':main()
