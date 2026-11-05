"""Offline phase2e transport, independent accounting and full new-cache replay."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import zipfile

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from scripts.phase2d_r2_delivery import checked_entries,digest,extract,write,PARENT_SHA

INPUTS={
 'sim-jev-phase2d-r2-20261104-160218.zip':'68506420139ac22e177d5030e0611e23315e8bc9754acd0f1cd9ee6b2b505c4e',
 'sim-jev-phase2d-r2-20261104-160218-postverify.zip':'a48414f54badcdb908a56bd986f80f832436272528e26b56b26ceadf8369217d',
 'sim-jev-phase2d-r1-20261103-191950-02.zip':PARENT_SHA}


def strict_json(payload):
    def pairs(items):
        result={}
        for key,value in items:
            if key in result:raise ValueError('duplicate JSON object key: '+key)
            result[key]=value
        return result
    def reject(value):raise ValueError('non-finite JSON value: '+value)
    return json.loads(payload,object_pairs_hook=pairs,parse_constant=reject)


def verify_zip(archive,expected=None):
    archive=Path(archive)
    sha=digest(archive)
    if expected and sha!=expected:raise ValueError('fixed archive SHA mismatch')
    rows=[]
    with zipfile.ZipFile(archive) as z:
        entries=checked_entries(z)
        m=strict_json(z.read('MANIFEST.json'))
        index={r['path']:r for r in m['files']}
        if len(index)!=len(m['files']):raise ValueError('duplicate manifest rows')
        payload={i.filename for i in entries if not i.is_dir() and i.filename!='MANIFEST.json'}
        supplemental=payload-set(index)
        fixed_parent_transport={'DIRECTORY-MANIFEST.json','PARENT-SHA256SUM.txt','PARENT-VERIFICATION.json',
            'POSTSEAL-REPORT-FINALIZER.py','TRANSPORT-PLAN.json','TRANSPORT-REPAIR.py','postverify-r2.ps1'}
        if set(index)-payload or (supplemental and not (sha==PARENT_SHA and
                all(n in fixed_parent_transport or n.startswith('PARENT-VALIDATION/') for n in supplemental))):
            raise ValueError('manifest payload set differs')
        for item in entries:
            if item.is_dir():continue
            data=z.read(item)
            actual=hashlib.sha256(data).hexdigest()
            if item.filename in index:
                row=index[item.filename]
                if actual!=row['sha256'] or len(data)!=row.get('size_bytes',row.get('size')):
                    raise ValueError('payload mismatch '+item.filename)
            rows.append(dict(path=item.filename,size_bytes=len(data),sha256=actual))
        dirs=[i.filename.rstrip('/') for i in entries if i.is_dir()]
        if m.get('schema')=='phase2e-a-dependent-package-v1' and 'directories' not in m:
            raise ValueError('new package requires complete directory topology')
        if 'directories' in m and len(m['directories'])!=len(set(m['directories'])):
            raise ValueError('duplicate manifest directory')
        if 'directories' in m and set(m['directories'])!=set(dirs):
            raise ValueError('directory topology differs')
    return dict(status='passed',archive=str(archive),archive_sha256=sha,files=rows,
        directories=dirs,manifest_payloads=len(index),supplemental_fixed_parent_transport=sorted(supplemental))


def verify_inputs(output):
    output=Path(output)
    output.mkdir(parents=True,exist_ok=False)
    results=[]
    for name,sha in INPUTS.items():
        result=verify_zip(Path('G:/sim-jev-audit')/name,sha)
        write(output/(name+'.json'),result)
        results.append(dict(name=name,sha256=sha,files=len(result['files']),directories=len(result['directories'])))
    review=Path('G:/gpt-file/Codex/outputs/phase2d-r2-review-next-20261104/03_独立核验证据.zip')
    with zipfile.ZipFile(review) as z:
        checked_entries(z)
        selected={n:z.read(n).decode('utf-8') for n in z.namelist() if n.startswith('audit/renderer-review/') and
            ('noninjective' in n or n.endswith('REVIEW.txt'))}
    write(output/'independent-counterexample.json',dict(archive=str(review),sha256=digest(review),files=selected))
    write(output/'summary.json',dict(status='passed',inputs=results,new_model_generations=0,new_des=0,http=0))
    return results


def pack(source,reports,archive):
    source,reports,archive=map(Path,(source,reports,archive))
    if archive.exists():raise ValueError('archive must be new')
    entries=[];dirs=set()
    for folder,prefix in ((source,'source'),(reports,'reports')):
        for current,ds,ns in os.walk(folder):
            ds[:]=sorted(d for d in ds if d not in ('.git','__pycache__','runs','audit-record','parent-evidence',
                'final-extracted','package-staging','review-evidence') and not (Path(current)/d).is_symlink())
            rel=Path(current).relative_to(folder)
            dirs.add((Path(prefix)/rel).as_posix())
            for n in sorted(ns):
                p=Path(current)/n
                if p.is_symlink() or p.suffix in ('.pyc','.pyo'):continue
                if prefix=='source' and rel==Path('.') and n not in ('.gitignore','README.md','pyproject.toml'):continue
                entries.append((p,(Path(prefix)/rel/n).as_posix()))
    manifest=dict(schema='phase2e-a-dependent-package-v1',fixed_parent_sha256=PARENT_SHA,
        external_dependencies='source/docs/PHASE2E_COMMANDS.md',files=[],directories=sorted(dirs))
    archive.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(archive,'x',zipfile.ZIP_DEFLATED) as z:
        for p,arc in sorted(entries,key=lambda r:r[1]):
            data=p.read_bytes();z.writestr(arc,data)
            manifest['files'].append(dict(path=arc,size_bytes=len(data),sha256=hashlib.sha256(data).hexdigest()))
        for d in sorted(dirs):z.writestr(d+'/',b'')
        z.writestr('MANIFEST.json',json.dumps(manifest,ensure_ascii=False,indent=2))
    checked=verify_zip(archive)
    return dict(archive=str(archive),sha256=checked['archive_sha256'],files=len(checked['files']),directories=len(dirs))


def audit_raw_attempts(root,frozen,budget,http):
    """Bind every received attempt, including failed parsing, by its unique ID.

    Equal request bodies are legal during stability repeats. Only the persisted
    attempt ID may join the HTTP and campaign ledgers. Observed model failures
    remain evidence; inconsistent hashes or links are rejected as corruption.
    """
    from sim_jev.local_provider import (_attempt_prompt,_output_schema,render_for_identity,
        validate_preference_scores)
    from sim_jev.provenance import canonical_sha256
    from scripts.phase2d_local import unpack
    root=Path(root)
    read=lambda path:strict_json(Path(path).read_text(encoding='utf-8'))
    ident=frozen['identity'];attempt_ids=set();scalars=[];prompts=[];prior={}
    by_http={}
    for row in http['requests']:
        if row['route']=='/api/generate':
            aid=row.get('generation_attempt_id')
            if aid is None or aid in by_http:raise ValueError('generation HTTP attempt ID missing/reused')
            by_http[aid]=row
    for reservation in budget['attempts']:
        aid=reservation['attempt_id']
        if aid in attempt_ids:raise ValueError('duplicate attempt ID')
        attempt_ids.add(aid)
        if reservation['status']!='completed':continue
        store=root/('development' if reservation['phase']=='development' else 'diagnostics')/'responses'
        path=store/reservation['artifact']['path']
        if path.name!='attempt-'+aid+'.json' or path.resolve().parent!=store.resolve():
            raise ValueError('raw attempt path escapes its phase store')
        evidence=read(path)
        if canonical_sha256(evidence)!=reservation['artifact']['sha256']:
            raise ValueError('raw attempt content differs')
        attempt=evidence['attempt'];body=attempt['body'];rendered=evidence['rendered']
        if (canonical_sha256(evidence['identity'])!=canonical_sha256(ident) or attempt['attempt_id']!=aid or
            attempt['logical_request_id']!=reservation['logical_request_id'] or
            attempt['attempt_index']!=reservation['attempt_index'] or
            attempt['seconds']!=reservation['seconds']):
            raise ValueError('raw attempt reservation identity differs')
        req,pool=unpack({'request':rendered['canonical']['decision'],'pool':rendered['canonical']['candidates']})
        if canonical_sha256(render_for_identity(req,pool,ident))!=canonical_sha256(rendered):
            raise ValueError('raw attempt rendered public input differs')
        key=canonical_sha256({'identity':ident,'rendered':rendered})
        if evidence['cache_key']!=key or reservation['key']!=key:
            raise ValueError('raw attempt cache identity differs')
        logical=reservation['logical_request_id'];index=reservation['attempt_index']
        if index==0:
            if logical in prior:raise ValueError('duplicate initial logical attempt')
            previous=''
        elif index==1 and logical in prior and prior[logical][0]['attempt_index']==0 and prior[logical][1]==key:
            previous=prior[logical][0]['raw'].get('response','')
        else:raise ValueError('raw correction has no unique initial attempt')
        expected_body={'model':ident['model'],'prompt':_attempt_prompt(rendered,ident,index,previous),
            'raw':True,'stream':False,'format':_output_schema(ident,rendered['candidate_mapping']),
            'options':ident['decoding'],'keep_alive':'24h','truncate':False,'shift':False}
        if canonical_sha256(body)!=canonical_sha256(expected_body):
            raise ValueError('raw attempt exact generation body differs')
        prior[logical]=(attempt,key)
        row=by_http.get(aid)
        if row is None or row['status']!='completed':raise ValueError('completed attempt has no completed HTTP')
        request=read(root/'http'/f"{row['index']:04d}-request.json")
        response=read(root/row['artifact'])
        if (canonical_sha256(request['data'])!=canonical_sha256(body) or
            canonical_sha256(response['response'])!=canonical_sha256(attempt['raw'])):
            raise ValueError('raw generation HTTP body/response differs from attempt')
        raw=attempt['raw'];errors=[]
        if (raw.get('model')!=ident['model'] or raw.get('done') is not True or raw.get('done_reason')!='stop'):
            errors.append('backend_model_completion_or_truncation_failure')
        n=attempt['independent_prompt_tokens']
        if type(n) is not int or n<0:raise ValueError('independent token count must be a nonnegative integer')
        if raw.get('prompt_eval_count')!=n:errors.append('independent_backend_token_mismatch')
        if n+ident['decoding']['num_predict']>ident['decoding']['num_ctx']:
            raise ValueError('received generation was submitted beyond frozen context budget')
        if reservation['seconds']>reservation['reserved_deadline_seconds']:
            errors.append('absolute_generation_deadline_exceeded')
        try:validate_preference_scores(raw.get('response'),rendered['candidate_mapping'])
        except (ValueError,TypeError) as error:errors.append('strict_scores_parse_failure: '+str(error))
        prompts.append(body['prompt'])
        scalars.append(dict(attempt_id=aid,phase=reservation['phase'],tokens=n,
            output_tokens=raw.get('eval_count'),seconds=reservation['seconds'],
            backend_load_seconds=raw['load_duration']/1e9 if 'load_duration' in raw else None,
            backend_prompt_eval_seconds=raw['prompt_eval_duration']/1e9 if 'prompt_eval_duration' in raw else None,
            backend_output_eval_seconds=raw['eval_duration']/1e9 if 'eval_duration' in raw else None,
            engineering_failures=errors))
    if set(by_http)-attempt_ids:raise ValueError('HTTP generation has no campaign reservation')
    return scalars,prompts


def audit(root,output,*,tokenizer_python=None,tokenizer_json=None):
    from sim_jev.provenance import canonical_sha256,code_fingerprint
    from sim_jev.local_provider import LocalProvider,validate_record
    from scripts.phase2d_local import unpack
    from scripts.phase2e_live import audit_development,CampaignBudget,TokenCounter,audit_http
    from scripts.phase2d_r2 import offline_evidence
    root,output=Path(root).resolve(),Path(output).resolve()
    read=lambda path:strict_json(Path(path).read_text(encoding='utf-8'))
    if root.is_relative_to(output) or output.is_relative_to(root):
        raise ValueError('read-only evidence and audit output must be separate')
    output.mkdir(parents=True,exist_ok=False)
    started=time.perf_counter();source=code_fingerprint(ROOT)
    frozen=read(root/'freeze.json')
    if frozen['source']!=source:raise ValueError('delivered execution source fingerprint differs')
    costs={'new_des':0,'new_model_generations':0,'new_metadata_requests':0,'new_http_requests':0}
    with offline_evidence(root,allow_des=False,cost=costs):
        development=audit_development(root)
        budget=CampaignBudget(root/'generation-budget.json').read()
        http=read(root/'http-budget.json')
        http_checked=audit_http(root)
        scalars,prompts=audit_raw_attempts(root,frozen,budget,http)
        attempt_ids={a['attempt_id'] for a in budget['attempts']}
        if len(budget['attempts'])>120:raise ValueError('total attempt budget exceeded')
        if abs(sum(a.get('seconds',0) for a in budget['attempts'])-budget['seconds'])>1e-9:
            raise ValueError('generation wall-time ledger differs')
        phase={}
        for name,limit in budget['limits'].items():
            group=[a for a in budget['attempts'] if a['phase']==name]
            seconds=sum(a.get('seconds',0) for a in group)
            if len(group)>limit or seconds>budget['phase_max_seconds'][name]+.1:
                raise ValueError('phase hard budget exceeded')
            phase[name]=dict(attempts=len(group),seconds=seconds,limit=limit,max_seconds=budget['phase_max_seconds'][name])
        if budget['seconds']>7200.1:raise ValueError('total time budget exceeded')
        routes={}
        for row in http['requests']:
            routes[row['route']]=routes.get(row['route'],0)+1
        missing_http=[a['attempt_id'] for a in budget['attempts'] if not any(
            r.get('generation_attempt_id')==a['attempt_id'] for r in http['requests'])]
        if any(a['status']=='completed' and a['attempt_id'] in missing_http for a in budget['attempts']):
            raise ValueError('completed generation missing HTTP attempt')
        cache=[]
        rows=read(root/'development/states.json')
        for path in sorted((root/'development').glob('completion-*.json')):
            index=int(path.stem.split('-')[-1]);req,pool=unpack(rows[index%8]);record=read(path)
            validate_record(record,req,pool,frozen['identity'])
            store=output/'cache-replay'/path.stem;store.mkdir(parents=True)
            shutil.copyfile(path,store/(record['cache_key']+'.json'))
            p=LocalProvider(frozen['identity'],store,mode='cache_only')
            replayed=p.rank(req,pool)
            for key in ('identity','cache_key','rendered','ranked_ids','choice','preference_scores','attempts'):
                if canonical_sha256(replayed[key])!=canonical_sha256(record[key]):
                    raise ValueError('new completion cache replay differs '+key)
            if p.generations or p.metadata_queries:raise ValueError('cache replay generated requests')
            cache.append(dict(completion=path.name,cache_key=record['cache_key'],passed=True))
        diagnostic=None
        if (root/'diagnostics/online-seal.json').exists():
            from scripts.phase2e_diagnostic import verify_online,verify_reference
            diagnostic=verify_online(root)
            if (root/'diagnostics/evaluation/status.json').exists() and read(root/'diagnostics/evaluation/status.json')['status']=='passed':
                diagnostic['reference']=verify_reference(root)
            diagnostic['full_cache_des_replay']='required separately: scripts/phase2e_diagnostic.py replay'
        elif (root/'diagnostics/online-started.json').exists():
            diagnostic=dict(status='partial',reference_opened=(root/'diagnostics/evaluation').exists())
            if diagnostic['reference_opened']:raise ValueError('reference opened without completed online seal')
    token_result=None
    if prompts:
        counter=TokenCounter(tokenizer_python or frozen['tokenizer_python'],tokenizer_json or frozen['tokenizer_json'])
        counted=[counter(p) for p in prompts]
        if counted!=[s['tokens'] for s in scalars]:raise ValueError('independent offline token recount mismatch')
        token_result=dict(attempts=len(counted),max_tokens=max(counted),calls=counter.calls,
                          tokenizer_python_sha256=digest(counter.python),tokenizer_json_sha256=digest(counter.asset))
        if token_result['tokenizer_python_sha256']!=frozen['identity']['tokenizer_runtime']['python_executable_sha256'] or token_result['tokenizer_json_sha256']!=frozen['identity']['tokenizer_sha256']:
            raise ValueError('independent offline tokenizer identity differs')
    result=dict(status='passed',scope='strict accounting of actual evidence; gate status is reported separately',
        source=source,development=development,diagnostic=diagnostic,phase=phase,http_routes=routes,http=http_checked,
        raw_attempts=scalars,cache_completions_replayed=cache,independent_token_recount=token_result,
        reservations_without_http=missing_http,
        pending_uncertain=sum(a['status'] in ('pending','uncertain') for a in budget['attempts']),
        costs=costs,seconds=time.perf_counter()-started)
    write(output/'strict-audit.json',result)
    return dict(status='passed',development_gate=development['status'],attempts=len(attempt_ids),
                cache_completions=len(cache),extra_des=0)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=('verify-inputs','verify-zip','pack','extract','audit'))
    p.add_argument('--root',type=Path);p.add_argument('--output',type=Path)
    p.add_argument('--archive',type=Path);p.add_argument('--source',type=Path);p.add_argument('--reports',type=Path)
    p.add_argument('--destination',type=Path);p.add_argument('--receipt',type=Path)
    p.add_argument('--tokenizer-python',type=Path);p.add_argument('--tokenizer-json',type=Path)
    a=p.parse_args()
    if a.action=='verify-inputs':r=verify_inputs(a.output)
    elif a.action=='pack':r=pack(a.source,a.reports,a.archive)
    elif a.action=='verify-zip':
        full=verify_zip(a.archive);write(a.receipt,full);r={k:v for k,v in full.items() if k not in ('files','directories')}
    elif a.action=='extract':
        verify_zip(a.archive);r=extract(a.archive,a.destination,a.receipt)
    else:r=audit(a.root,a.output,tokenizer_python=a.tokenizer_python,tokenizer_json=a.tokenizer_json)
    print(json.dumps(r,ensure_ascii=False))
if __name__=='__main__':main()
