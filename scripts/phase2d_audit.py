"""Independent scalar/data audit, logged regressions, reproducible minimal audit ZIP."""
from __future__ import annotations
import argparse
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import re
import statistics
import subprocess
import sys
import time
import zipfile

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from sim_jev.provenance import code_fingerprint,canonical_sha256
from sim_jev.weekly import WeeklySimulation
from sim_jev.decision_audit import validate_record
from sim_jev.local_provider import LocalProvider
from scripts.phase2d_local import read,save,unpack,policy,METHODS
from scripts.phase2c_experiment import build_case
from sim_jev.weekly_policy import PolicySpec
from scripts.weekly_verify import audit_business_log
from scripts.planning_verify import audit_planning_branch

def require(condition,message):
    if not condition:raise AssertionError(message)


def redact_download_payload(path, payload):
    """Remove temporary signed URL queries without changing unrelated bytes.

    JSON strings must be decoded before redaction: their URL separators may
    be written as \\u0026. PowerShell may also wrap a UTF-16 log URL across lines.
    A remaining signed parameter fails the export rather than leaking it.
    """
    markers = (b'x-amz-', 'x-amz-'.encode('utf-16le'), 'x-amz-'.encode('utf-16be'))
    if path.suffix.lower() not in {'.log', '.json', '.jsonl', '.txt'} or not any(marker in payload.lower() for marker in markers):
        return payload
    text = payload.decode('utf-16') if payload[:2] in {b'\xff\xfe', b'\xfe\xff'} else payload.decode('utf-8-sig')
    signed_query = re.compile(r'''[?&]X-Amz-[^"'\\<>]+''', re.IGNORECASE)
    signed_parameter = re.compile(r'X\s*-\s*Amz\s*-\s*[A-Za-z0-9-]+\s*=', re.IGNORECASE)
    changed = False

    def clean_string(value):
        nonlocal changed
        clean = signed_query.sub('?[download-signature-redacted]', value)
        if signed_parameter.search(clean):
            raise ValueError(f'unredacted signed download parameter in {path}')
        changed = changed or clean != value
        return clean

    def clean_value(value):
        if isinstance(value, str):
            return clean_string(value)
        if isinstance(value, list):
            return [clean_value(item) for item in value]
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                clean_key = clean_string(key)
                if clean_key in result:
                    raise ValueError(f'download redaction would collide JSON keys in {path}')
                result[clean_key] = clean_value(item)
            return result
        return value

    try:
        if path.suffix.lower() == '.json':
            decoded = json.loads(text)
        elif path.suffix.lower() == '.jsonl':
            decoded = [json.loads(line) for line in text.splitlines() if line.strip()]
        else:
            decoded = None
    except json.JSONDecodeError:
        # Some diagnostic files use a JSON suffix for an interrupted log.
        decoded = None

    if decoded is None:
        exported = clean_string(text)
    else:
        cleaned = clean_value(decoded)
        if path.suffix.lower() == '.jsonl':
            exported = '\n'.join(json.dumps(item, ensure_ascii=False) for item in cleaned) + '\n'
        else:
            exported = json.dumps(cleaned, ensure_ascii=False, indent=2) + '\n'
    if signed_parameter.search(exported):
        raise ValueError(f'unredacted signed download parameter in {path}')
    return exported.encode('utf-8') if changed else payload


def verify_data(root,*,baseline_only=False,output=None):
    folder=root/('baseline-diagnostics' if baseline_only else 'diagnostics');states=read(folder/'states.json');actions=read(folder/'online-actions.json')
    records=read(folder/'evaluation/per-state.json');summaries={}
    with (folder/'evaluation/per-candidate-reference.csv').open(encoding='utf-8-sig',newline='') as f:
        for row in csv.DictReader(f):summaries[row['state_id'],row['candidate_id']]=row
    count=0;online=0
    for state in states:
        req,pool=unpack(state);by_id={c.id:c for c in pool};scores={c.id:[] for c in pool}
        sim=build_case(state['scenario'],state['seed'],PolicySpec());sim.run(state['window_start'])
        for method in METHODS:
            if baseline_only and method.startswith('local-'):continue
            item=read(folder/state['state_id']/(method+'.json'))
            if method!='mandatory-first':validate_record(item,policy(method,read(root/'freeze.json')['identity']),sim)
            online+=item['budget']['actual_des']
            a=next(x for x in actions if x['state_id']==state['state_id'] and x['method']==method)
            require(a['selected_candidate_id']==item['selected_candidate_id'],'online selected ID changed')
        with (folder/'evaluation'/state['state_id']/'branches.jsonl').open(encoding='utf-8') as stream:
            for line in stream:
                row=json.loads(line);c=by_id[row['candidate_id']];index=len(scores[c.id])
                require(row['scenario_index']==index and index<64,'reference scenario count/order')
                sid='planning-'+canonical_sha256({'legal_input_hash':canonical_sha256(req.to_dict()),'planning_seed':20261103,'scenario_index':index})
                require(row['scenario_id']==sid and row['planning_seed']==20261103 and row['purpose_namespace']=='reference-v1','reference RNG identity')
                require(row['command']==c.command.to_dict(),'reference command mismatch')
                counts=row['counts'];dsr=counts['success']/counts['offered'] if counts['offered'] else 0.
                rcr=counts['earned_value']/counts['offered_value'] if counts['offered_value'] else 0.
                require(row['score']==dsr+rcr==row['audit']['score'] and row['audit']['verified'],'reference independent score mismatch')
                require(row['window_end']==min(state['window_start']+168,720),'reference endpoint')
                scores[c.id].append(row['score']);count+=1
        means={k:statistics.fmean(v) for k,v in scores.items()};best=max(means.values())
        for key,values in scores.items():
            require(len(values)==64,'missing reference branches')
            summary=summaries[state['state_id'],key]
            require(float(summary['q_ref'])==means[key] and json.loads(summary['scores'])==values,'reference CSV differs from raw branches')
            require(float(summary['standard_error'])==statistics.stdev(values)/8,'reference standard error')
        for row in [x for x in records if x['state_id']==state['state_id']]:
            if row['status']!='completed':
                require(row['action_regret'] is None and row['regret_at_k'] is None,'unrun method has an effect value')
                continue
            selected=row['selected_candidate_id'];top=row['top_k']
            require(row['action_regret']==(best-means[selected] if selected in means else None),'action regret mismatch')
            require(row['regret_at_k']==(best-max(means[k] for k in top) if top else None),'screening regret mismatch')
        physical=read(folder/'evaluation'/state['state_id']/'physical-reference-0.json')
        audit_planning_branch(physical,req)
    result={'status':'passed','states':len(states),'reference_branches':count,'online_branches':online,
            'extra_selection_des':0,'raw_scalar_audit':True,'retained_physical_artifacts_audited':len(states),
            'evidence_source_sha256':read(root/'freeze.json')['source']['sha256'],'auditor_source_sha256':code_fingerprint(ROOT)['sha256']}
    if (root/'pilot/status.json').exists():
        paired={};runs=0;pilot_des=0;decisions=0
        for directory in sorted((root/'pilot').iterdir()):
            if not directory.is_dir() or not (directory/'result.json').exists():continue
            row=read(directory/'result.json');sim=WeeklySimulation.from_checkpoint(read(directory/'checkpoint.json'))
            audit_business_log(sim);summary=sim.summary()
            for key in ('offered','accepted','success','offered_value','earned_value','DSR','RCR','ASR','flight_hours','availability'):
                require(row[key]==summary[key],'pilot aggregate differs from independently audited run')
            key=(row['scenario'],row['seed']);digest=canonical_sha256(read(directory/'exogenous.json'))
            require(digest==row['paired_task_rule_sha256']==paired.setdefault(key,digest),'paired input mismatch')
            for artifact in directory.glob('window-*/branch-0.json'):audit_planning_branch(read(artifact))
            runs+=1;pilot_des+=row['actual_des'];decisions+=row['decisions']
        result.update(pilot_runs=runs,pilot_decisions=decisions,pilot_des=pilot_des,formal_des=count+online+pilot_des)
        require(result['formal_des']<=34048,'formal DES exceeded')
    result['local_methods_included']=not baseline_only
    save((output or root)/('baseline-data-audit.json' if baseline_only else 'data-audit.json'),result);return result

def run_logged(argv,cwd,folder,name):
    folder.mkdir(parents=True,exist_ok=True);log=folder/(name+'.log')
    if log.exists():raise ValueError('command log exists; choose a new verification directory')
    t=time.perf_counter()
    with log.open('wb') as stream:r=subprocess.run(argv,cwd=cwd,stdout=stream,stderr=subprocess.STDOUT)
    record={'argv':[str(x) for x in argv],'cwd':str(cwd),'exit_code':r.returncode,'seconds':time.perf_counter()-t,'log':str(log)}
    save(folder/(name+'.command.json'),record);print(name,r.returncode,round(record['seconds'],2),flush=True)
    if r.returncode:raise RuntimeError(f'{name} failed; inspect {log}')
    return record

def regress(root):
    folder=root/'regression';folder.mkdir(exist_ok=False);commands=[]
    # validate.py includes the complete ordinary unittest suite.
    for script,name in [('validate.py','validate'),('checkpoint_verify.py','checkpoint'),('weekly_verify.py','weekly'),('phase2c_compatibility.py','compatibility')]:
        commands.append(run_logged([sys.executable,str(ROOT/'scripts'/script),'--output',str(folder/name)],ROOT,folder,name))
    save(folder/'commands.json',commands);return {'status':'passed','commands':commands}

def package(root,archive,destination):
    require(not archive.exists() and not destination.exists(),'archive/extraction must be new')
    files=[]
    for directory in ('sim_jev','scripts','tests','configs','docs'):
        files.extend(p for p in (ROOT/directory).rglob('*') if p.is_file() and '__pycache__' not in p.parts)
    files.extend(ROOT/name for name in ('pyproject.toml','README.md','.gitignore'))
    # Include only this explicit evidence batch, not all caches or historical runs.
    evidence_roots=[root]
    if (root/'evidence-sources.json').exists():
        for relative in read(root/'evidence-sources.json')['sources']:
            candidate=(ROOT/relative).resolve()
            require(candidate.is_relative_to((ROOT/'runs').resolve()) and candidate.is_dir(),'invalid evidence source')
            evidence_roots.append(candidate)
    for selected in evidence_roots:
        files.extend(p for p in selected.rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix not in {'.pyc','.zip'})
    files=sorted(set(files));manifest=[];archive.parent.mkdir(parents=True,exist_ok=True)
    with archive.open('xb') as stream,zipfile.ZipFile(stream,'w',zipfile.ZIP_DEFLATED) as z:
        for path in files:
            relative=path.relative_to(ROOT).as_posix();payload=path.read_bytes()
            # Public download redirects contain temporary signed query strings.
            # Original local logs are retained; export removes those query values.
            payload=redact_download_payload(path,payload)
            item={'path':'project/'+relative,'size':len(payload),'sha256':hashlib.sha256(payload).hexdigest()}
            manifest.append(item);z.writestr(item['path'],payload)
        transport={'version':'phase2d-audit-package-v1','source':code_fingerprint(ROOT),'files':manifest,
                   'evidence_relative':root.relative_to(ROOT).as_posix(),'evidence_roots':[x.relative_to(ROOT).as_posix() for x in evidence_roots],
                   'excludes':['weights','runtime binaries','environments','historical runs','unrelated response caches','credentials']}
        z.writestr('MANIFEST.json',json.dumps(transport,indent=2))
    digest=hashlib.file_digest(archive.open('rb'),'sha256').hexdigest()
    archive.with_suffix('.zip.sha256').write_text(digest+'  '+archive.name+'\n',encoding='utf-8')
    checks=[];destination.mkdir(parents=True)
    with zipfile.ZipFile(archive) as z:
        require(set(z.namelist())=={x['path'] for x in manifest}|{'MANIFEST.json'},'ZIP member scope differs')
        for item in manifest:
            payload=z.read(item['path']);okay=len(payload)==item['size'] and hashlib.sha256(payload).hexdigest()==item['sha256']
            require(okay,'ZIP entry hash mismatch');checks.append({**item,'verified':okay})
        for member in z.namelist():
            require((destination/member).resolve().is_relative_to(destination.resolve()),'unsafe ZIP path')
        z.extractall(destination)
    extracted=destination/'project';require(not (extracted/'.git').exists(),'unexpected git metadata')
    require(code_fingerprint(extracted)==transport['source'],'extracted source fingerprint differs')
    receipts=[]
    receipts.append(run_logged([sys.executable,'-m','unittest','discover','-s','tests','-v'],extracted,destination,'ordinary-tests'))
    # Original replay output is excluded by moving the new replay destination to
    # a fresh evidence name; preserve the original package evidence untouched.
    for index,relative in enumerate(transport['evidence_roots']):
        evidence=extracted/relative
        if (evidence/'pilot/status.json').exists() and read(evidence/'pilot/status.json')['status']=='passed':
            receipts.append(run_logged([sys.executable,str(extracted/'scripts/phase2d_local.py'),'replay','--output',str(evidence),'--replay-label','extracted-replay'],
                                       extracted,destination,f'zero-inference-replay-{index}'))
        elif (evidence/'captured-replay/status.json').exists():
            receipts.append(run_logged([sys.executable,str(extracted/'scripts/phase2d_local.py'),'captured-replay','--output',str(evidence),'--replay-label','extracted-replay'],
                                       extracted,destination,f'zero-inference-captured-replay-{index}'))
    verification={'status':'passed','archive_sha256':digest,'entries':checks,'commands':receipts,
                  'no_git':True,'no_historical_runs':True,'model_gpu_revalidated':False,
                  'source_sha256':transport['source']['sha256']}
    save(archive.with_suffix('.zip.verification.json'),verification);return {'status':'passed','entries_verified':len(checks),'archive':str(archive),'sha256':digest}

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=['verify-data','regress','package'])
    p.add_argument('--output',type=Path,required=True);p.add_argument('--archive',type=Path);p.add_argument('--extract',type=Path)
    p.add_argument('--baseline-only',action='store_true')
    p.add_argument('--source-evidence',type=Path)
    a=p.parse_args();t=time.perf_counter();receipt={'argv':sys.orig_argv}
    try:
        result=verify_data(a.source_evidence or a.output,baseline_only=a.baseline_only,output=a.output) if a.action=='verify-data' else regress(a.output) if a.action=='regress' else package(a.output.resolve(),a.archive,a.extract)
        receipt.update(exit_code=0,result=result)
    except Exception as e:
        import traceback
        receipt.update(exit_code=1,error=str(e),traceback=traceback.format_exc());print(receipt['traceback'],file=sys.stderr)
    receipt['seconds']=time.perf_counter()-t;save(a.output/'commands'/f'{a.action}-{time.time_ns()}.json',receipt)
    print(json.dumps({k:v for k,v in receipt.items() if k!='traceback'},indent=2));return receipt['exit_code']

if __name__=='__main__':raise SystemExit(main())
