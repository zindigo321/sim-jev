"""C2 immutable artifacts and reservation-first accounting; C1 remains untouched."""
from collections import Counter
from fractions import Fraction
from pathlib import Path
import os
import time
from scripts.phase2ec1_common import read, write, append, lines, sha, manifest, verify_files
from sim_jev.provenance import canonical_sha256, code_fingerprint

ROOT = Path(__file__).resolve().parents[1]
PARENT = Path('G:/gpt-file/Codex/outputs/phase2e-c1-20261110-181827.zip')
PARENT_SHA = '315173cfd6ca5c5f80e252e51044e62d5b9d6c04e2904c7e5a835a41479ec4ad'
P_SHA = '3b6add09d8df40c7286aa96c3870e8b8dd05e277267b26510774e9a5f80dbc06'
A_SHA = '6d109b90d06fcf8b98537df01155a74d9588e90a75bfea095451bf346fdfae8d'
METHODS = ['H32','H42','A8','P8','A8-G','P8-G']
LIMITS = dict(online=9216, permutation=512, rerank=2048, r32=65536, r64=28672,
              engineering_des=1024, trajectory=128, forward=360)
FORWARD_LIMITS = dict(online=256, permutation=64, control=40)
RESEARCH = ('online','permutation','rerank','r32','r64')


def write_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x',encoding='utf8') as f:
        f.write(text); f.flush(); os.fsync(f.fileno())


def sealed(root, name, files):
    files = sorted(files,key=lambda r:r['path'])
    value = dict(files=files, files_sha256=canonical_sha256(files), sealed_unix=time.time())
    write(Path(root)/name,value)
    verify_seal(root,name)
    return value


def verify_seal(root, name):
    value = read(Path(root)/name)
    if canonical_sha256(value['files']) != value['files_sha256']:
        raise ValueError('seal manifest digest mismatch: '+name)
    verify_files(root,value['files'])
    return value


def files_in(root, folders=(), names=()):
    rows = [dict(r,path=folder+'/'+r['path']) for folder in folders for r in manifest(Path(root)/folder)]
    rows += [dict(path=name,bytes=(Path(root)/name).stat().st_size,sha256=sha(Path(root)/name)) for name in names]
    return rows


def check(root, action=False):
    root=Path(root)
    verify_seal(root,'protocol-seal.json')
    if code_fingerprint(ROOT) != read(root/'frozen-source.json'):
        raise ValueError('source changed after protocol freeze')
    if action:
        verify_seal(root,'pool-seal.json')
        verify_seal(root,'action-seal.json')
    return read(root/'registration.json')


class Journal:
    def __init__(self,root):
        self.root=Path(root); self.path=self.root/'compute-journal.jsonl'
        self.binding=sha(self.root/'registration.json'); self.entries={}
        for e in lines(self.path):
            if e['registration_sha256']!=self.binding:
                raise ValueError('ledger binding mismatch')
            old=self.entries.get(e['key'])
            if e['status']=='pending':
                if old is not None: raise ValueError('duplicate reservation')
            elif e['status'] not in ('completed','failed') or old is None or old['status']!='pending':
                raise ValueError('invalid completion transition')
            self.entries[e['key']]=e
        self.counts=Counter(e['phase'] for e in self.entries.values())
        self.forward_counts=Counter(e.get('forward_group') for e in self.entries.values() if e['phase']=='forward')
        if any(self.counts[k]>v for k,v in LIMITS.items()): raise ValueError('budget exceeded')
        if any(self.forward_counts[k]>v for k,v in FORWARD_LIMITS.items()): raise ValueError('forward sub-budget exceeded')

    def reserve(self,phase,key):
        if phase not in LIMITS or self.counts[phase]>=LIMITS[phase] or key in self.entries:
            raise ValueError('budget exhausted or duplicate attempt: '+key)
        group=None
        if phase=='forward':
            group='online' if key.startswith('forward/online/') else 'permutation' if key.startswith('forward/permutation/') else 'control'
            if self.forward_counts[group]>=FORWARD_LIMITS[group]: raise ValueError('forward group exhausted')
            used=sum(e.get('seconds',60.) for e in self.entries.values() if e['phase']=='forward')
            if used+60>3600: raise ValueError('forward seconds reservation exceeds 3600')
        if phase in RESEARCH and sum(self.counts[p] for p in RESEARCH)>=105984:
            raise ValueError('research DES exhausted')
        e=dict(registration_sha256=self.binding,phase=phase,key=key,status='pending',unix=time.time())
        if group: e['forward_group']=group
        append(self.path,e); self.entries[key]=e; self.counts[phase]+=1
        if group: self.forward_counts[group]+=1

    def finish(self,key,row,artifact=None,*,failed=False,seconds=None):
        if self.entries[key]['status']!='pending': raise ValueError('attempt already completed')
        e=dict(self.entries[key],status='failed' if failed else 'completed',finished_unix=time.time(),
               scalar_sha256=canonical_sha256(row),artifact=artifact)
        if seconds is not None: e['seconds']=seconds
        append(self.path,e); self.entries[key]=e


def q_exact(row):
    c=row['counts']
    return (Fraction(str(c['success']))/Fraction(str(c['offered'])) if c['offered'] else Fraction(0)) + (
        Fraction(str(c['earned_value']))/Fraction(str(c['offered_value'])) if c['offered_value'] else Fraction(0))


def rational(value):
    return dict(numerator=value.numerator,denominator=value.denominator,value=float(value))


def guard_select(order,rows):
    if len(order)!=2: raise ValueError('guard requires precisely two retained candidates')
    by={(r['candidate_id'],r['scenario_index']):r for r in rows}
    if len(rows)!=8 or len(by)!=8 or set(by)!={(c,j) for c in order for j in range(4)}:
        raise ValueError('guard requires four paired scenarios')
    diffs=[q_exact(by[order[1],j])-q_exact(by[order[0],j]) for j in range(4)]
    positive=sum(d>0 for d in diffs); mean=sum(diffs,Fraction(0))/4
    switch=positive>=3 and mean>=Fraction(1,500)
    denoms={(str(r['counts']['offered']),str(r['counts']['offered_value'])) for r in rows}
    return order[int(switch)],dict(positive_count=positive,paired_differences=[rational(d) for d in diffs],
        mean_difference=rational(mean),threshold=rational(Fraction(1,500)),switch=switch,
        common_denominators=len(denoms)==1,denominators=sorted(denoms),
        invalid_denominators=sum(not r['counts']['offered'] or not r['counts']['offered_value'] for r in rows))


def supplement_ids(actions,permutations=()):
    base=set(actions['P8']['retained_ids']+actions['A8']['retained_ids']+
             [actions[m]['selected_candidate_id'] for m in ('H42','H32')])
    if len(base)>6: raise ValueError('R64 base set exceeds six')
    extra=set(c for r in permutations for c in r['retained_ids'])-base
    if len(extra)>4: raise ValueError('R64 permutation addition exceeds four')
    return dict(base=sorted(base),extra=sorted(extra),all=sorted(base|extra))


def method_order(index):
    offset=index%len(METHODS)
    order=METHODS[offset:]+METHODS[:offset]
    return order[::-1] if (index//len(METHODS))%2 else order
