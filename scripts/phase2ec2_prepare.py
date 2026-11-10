"""Verify C1 bytes and reserve a new holdout before any new trajectory exists."""
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import zipfile
from scripts.phase2ec2_common import *
from scripts.phase2e_diagnostic import request_pool_identity,seed_usage
from sim_jev.qwen_pointer import ENCODING

PROMPT=Path('C:/Users/acc04/.codex/attachments/955a8fe8-24db-49cb-9f36-04fb277a9bf6/已粘贴的文本.txt')


def environment():
    return dict(interpreter=sys.executable,python=sys.version,executable_sha256=sha(sys.executable),
        packages=sorted([dict(name=d.metadata['Name'],version=d.version) for d in importlib.metadata.distributions()],key=lambda d:d['name'].lower()))


def prepare(root):
    root=Path(root)
    if (root/'registration.json').exists(): raise ValueError('campaign already prepared')
    root.mkdir(parents=True,exist_ok=True)
    initial=code_fingerprint(ROOT)
    write(root/'initial-source.json',initial)
    for name,args in [('status',['status','--porcelain=v1','--untracked-files=all']),('diff',['diff','--binary']),
                      ('index',['diff','--cached','--binary']),('head',['rev-parse','HEAD'])]:
        p=subprocess.run(['git',*args],cwd=ROOT,capture_output=True,check=True)
        (root/('initial-git-'+name+'.txt')).write_bytes(p.stdout)
    write(root/'environment-prepare.json',environment())
    if sha(PARENT)!=PARENT_SHA: raise ValueError('C1 ZIP SHA256 mismatch')
    source_checks=[]
    with zipfile.ZipFile(PARENT) as z:
        listed=json.loads(z.read('DELIVERY-MANIFEST.json'))
        for r in listed:
            b=z.read(r['path'])
            if len(b)!=r['bytes'] or hashlib.sha256(b).hexdigest()!=r['sha256']:
                raise ValueError('C1 delivery manifest mismatch: '+r['path'])
        seal=json.loads(z.read('model-seal.json'))
        if canonical_sha256(seal['files'])!=seal['files_sha256']: raise ValueError('C1 model-seal digest differs')
        for r in seal['files']:
            if hashlib.sha256(z.read(r['path'])).hexdigest()!=r['sha256']: raise ValueError('C1 sealed byte mismatch')
        for r in seal['source']['files']:
            current=ROOT/r['path']; ok=current.is_file() and sha(current)==r['sha256']
            source_checks.append(dict(path=r['path'],sha256=r['sha256'],current_matches=ok))
        write(root/'parent/source-comparison.json',source_checks)
        if not all(r['current_matches'] for r in source_checks): raise ValueError('historical source differs; diagnose before proceeding')
        def copy(name,dest=None):
            p=root/(dest or ('parent/'+name)); p.parent.mkdir(parents=True,exist_ok=True)
            with p.open('xb') as f: f.write(z.read(name))
        for n in ('model-seal.json','registration.json','REPORT.zh-CN.txt','DELIVERY-MANIFEST.json',
                  'history/seed-usage.json','history/completion-receipt.json'):
            copy(n)
        for n in ('models/P11-selected.pt','models/A.json'): copy(n,n)
        dataset=json.loads(z.read('data/dataset.json'))
        old=[]
        for split in ('train','validation'):
            old += sorted([d['state'] for d in dataset if d['state']['scenario']=='scarce' and d['state']['split']==split],key=lambda s:s['state_id'])[:2]
        if len(old)!=4: raise ValueError('four old scarce controls required')
        write(root/'development/states.json',old)
        history=[]
        for n in ('history/seen-public.json','history/supplement-public.json','history/supplement-completion.json'):
            history+=json.loads(z.read(n))
        reg=json.loads(z.read('registration.json'))
        for p in reg['planned']: history.append(json.loads(z.read('states/'+p['state_id']+'/state.json')))
        if read(root/'parent/history/completion-receipt.json')['unresolved']:
            raise ValueError('C1 historical public index incomplete')
    if sha(root/'models/P11-selected.pt')!=P_SHA or sha(root/'models/A.json')!=A_SHA:
        raise ValueError('frozen head SHA mismatch')
    write(root/'parent/verification.json',dict(archive=str(PARENT),sha256=PARENT_SHA,
        manifest_verified=len(listed),model_seal_files_verified=len(seal['files']),
        source_files_verified=len(source_checks),P11_sha256=P_SHA,A_sha256=A_SHA))
    for source,dest in [(PROMPT,'requested-prompt.txt'),
        (Path('G:/sim-jev/audit-record/phm-plan.md'),'evidence/phm-plan.md'),
        (Path('G:/gpt-file/Codex/outputs/phase2e-c1-independent-audit-20261110/2E-C1独立审计.md'),'evidence/C1-independent-audit.md'),
        (Path('C:/Users/acc04/.codex/attachments/a0a835dc-9b0f-4d53-b330-040e02e36ccc/已粘贴的文本.txt'),'evidence/15-pitfalls.txt')]:
        (root/dest).parent.mkdir(parents=True,exist_ok=True); shutil.copyfile(source,root/dest)
    # C1's complete public inventory plus public states created since that campaign.
    known={r['exact_input_sha256'] for r in history}
    inventory=[]; seeds=[]
    bases=[ROOT/'runs']+[p for p in root.parent.iterdir() if p.is_dir() and p!=root and p.name.startswith('phase2')]
    for base in bases:
        seeds+=seed_usage([base])
        for current,dirs,names in os.walk(base):
            dirs[:]=[d for d in dirs if d not in ('source','reference','reused','evaluation','branch-audit-store','cache','checkpoints','responses','__pycache__')]
            for name in names:
                if name not in ('state.json','states.json','seen-public.json','supplement-public.json','supplement-completion.json'): continue
                path=Path(current)/name
                try: obj=read(path)
                except (OSError,ValueError): continue
                inventory.append(dict(path=str(path),sha256=sha(path)))
                for row in obj if isinstance(obj,list) else [obj]:
                    if not isinstance(row,dict) or 'request' not in row or 'pool' not in row: continue
                    a,b=request_pool_identity(row['request'],row['pool'])
                    if a not in known:
                        known.add(a); history.append(dict(row,exact_input_sha256=a,business_input_sha256=b,origin=str(path)))
    for r in history:
        a,b=request_pool_identity(r['request'],r['pool'])
        if a!=r['exact_input_sha256'] or b!=r['business_input_sha256']: raise ValueError('historical input fingerprint changed')
    write(root/'history/public-inputs.json',history)
    write(root/'history/public-files.json',inventory)
    write(root/'history/seed-usage.json',seeds)
    used={r['seed'] for r in seeds}|{r['seed'] for r in history if 'seed' in r}
    used|=set(range(7000,7256))|set(range(8000,8256))|set(range(9000,9064))
    plans=[]; replacement=12000
    for i in range(128):
        original=11000+i; seed=original
        if seed in used:
            while replacement in used: replacement+=1
            seed=replacement
        used.add(seed)
        plans.append(dict(index=i,seed=seed,requested_seed=original,scenario='scarce',split='test',
            state_id=f'scarce-{seed}-336',window_start=336,window_end=504,
            reason='historical occupied/reserved seed' if seed!=original else 'unused default'))
    # Search historical declarations and manifests for random-purpose seed collisions.
    hits=[]; historical=set()
    for base in [ROOT/'scripts',ROOT/'docs',*bases]:
        for current,dirs,names in os.walk(base):
            dirs[:]=[d for d in dirs if d not in ('source','reference','reused','cache','evaluation','branch-audit-store','responses','__pycache__','checkpoints','test','permutation','forwards')]
            for name in names:
                p=Path(current)/name
                if p.suffix not in ('.json','.md','.txt','.py') or p.stat().st_size>2_000_000 or 'phase2ec2' in name.lower(): continue
                nums=set(int(n) for n in re.findall(r'(?<!\d)2026\d{6}(?!\d)',p.read_text(encoding='utf8',errors='replace')))
                if nums: historical|=nums; hits.append(dict(path=str(p),seeds=sorted(nums)))
    fixed={}; mapping=[]
    for i,key in enumerate(('online_seed','rerank_seed','reference_seed','permutation_seed','environment_bootstrap_seed','mc_bootstrap_seed')):
        original=2026111021+i; seed=original
        while seed in historical or seed in fixed.values(): seed+=100
        fixed[key]=seed; mapping.append(dict(purpose=key,requested=original,actual=seed,reason='historical collision' if seed!=original else 'unused default'))
    write(root/'history/planning-seed-search.json',hits)
    registration=dict(protocol='phase2ec2-v1',created_unix=time.time(),planned=plans,seeds=mapping,**fixed,
        methods=METHODS,method_rotation='offset index mod6; reverse every alternate six-slot cycle',
        diagnostic_slots=list(range(0,128,4)),generation_seed=20261102,encoding=ENCODING,
        development_ids=[s['state_id'] for s in old],limits=LIMITS,forward_limits=FORWARD_LIMITS,
        forward_seconds_limit=3600,research_des_limit=105984,bootstrap_n=5000,
        guards=dict(positive_at_least=3,paired_count=4,mean_minimum='1/500'),
        gates=dict(min_paired=112,improvement=.002,lower95_strict=0,extra_regret_H32=.005,
            guard_extra_regret=.002,p50_ratio=.8,p95_ratio=1,permutation_min=24,
            top2_consistency=.9,permutation_penalty=.002,mandatory_increment_upper=0,development_mechanism_min=2),
        primary_candidate='P8-G',optimizer_updates=0,autoregressive_calls=0,remote_calls=0,official_jev_requests=0,
        historical_sd_for_planning=.0192,approximate_one_sided_halfwidth=.0028,
        history_sha256=sha(root/'history/public-inputs.json'),prompt_sha256=sha(root/'requested-prompt.txt'))
    write(root/'registration.json',registration)
    write_text(root/'PREREGISTRATION.zh-CN.txt',PROMPT.read_text(encoding='utf8')+'\n\n机器可执行登记见 registration.json。协议、源码、资产及回归通过后封存；任何新状态在其后构建。\n')
    return dict(status='prepared',planned=128,history_inputs=len(history),parent_verified=len(listed),seeds=mapping)


def freeze(root):
    root=Path(root)
    if (root/'states').exists(): raise ValueError('states already built before freeze')
    if read(root/'probe/result.json')['status']!='passed': raise ValueError('probe failed')
    receipts=sorted((root/'regression').glob('receipt-*.json'))
    if not receipts or not read(receipts[-1])['passed']: raise ValueError('regression failed')
    source=code_fingerprint(ROOT)
    if read(receipts[-1])['source']!=source: raise ValueError('source changed since regression')
    for r in source['files']:
        p=root/'source'/r['path']; p.parent.mkdir(parents=True,exist_ok=True); shutil.copyfile(ROOT/r['path'],p)
    write(root/'frozen-source.json',source)
    return sealed(root,'protocol-seal.json',files_in(root,
        ('models','source','development','history','probe','parent','regression'),
        ('registration.json','requested-prompt.txt','PREREGISTRATION.zh-CN.txt','frozen-source.json')))
