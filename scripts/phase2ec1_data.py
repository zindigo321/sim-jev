"""Reuse sealed 2E-B supervision without generating any new DES labels."""
from collections import Counter
from pathlib import Path
import hashlib
import json
import math
import re
import time
import zipfile
import numpy as np
from scripts.phase2ec1_common import (ROOT, PARENT, PARENT_SHA, A_SHA, LIMITS,
    read, sha, write, canonical_sha256)
from scripts.phase2d_local import unpack
from scripts.phase2d_r1_data_audit import _scalar
from scripts.phase2e_diagnostic import request_pool_identity, seed_usage
from sim_jev.candidates import generate_candidates
from sim_jev.qwen_pointer import numeric, ENCODING


def soft_targets(matrix, rng):
    """One shared scenario sample for every candidate; exact numerical ties."""
    m, b = matrix.shape
    if b != 32 or m < 1:
        raise ValueError('32 references per candidate required')
    indices = rng.integers(0, b, size=(512, b))
    means = matrix[:, indices].mean(axis=2).T
    winners = means == means.max(axis=1, keepdims=True)
    votes = (winners / winners.sum(axis=1, keepdims=True)).mean(axis=0)
    return .98*votes + .02/m, canonical_sha256(indices.tolist())


def verify_library(raw, state, seed, count):
    req, pool = unpack(state)
    by = {c.id: c for c in pool}
    pairs = set()
    for row in raw:
        key = row['candidate_id'], row['scenario_index']
        if key in pairs or key[0] not in by or not 0 <= key[1] < count:
            raise ValueError('duplicate/invalid branch')
        pairs.add(key)
        _scalar(row, req, by[key[0]], seed)
    if pairs != {(c.id, j) for c in pool for j in range(count)}:
        raise ValueError('incomplete original library')
    scores = {(r['candidate_id'], r['scenario_index']):
              (r['counts']['success']/r['counts']['offered'] if r['counts']['offered'] else 0.) +
              (r['counts']['earned_value']/r['counts']['offered_value'] if r['counts']['offered_value'] else 0.)
              for r in raw}
    return np.array([[scores[c.id, j] for j in range(count)] for c in pool])


def prepare(root):
    root = Path(root)
    if (root/'registration.json').exists():
        raise ValueError('already prepared; never reset campaign')
    if sha(PARENT) != PARENT_SHA:
        raise ValueError('original ZIP checksum mismatch')
    with zipfile.ZipFile(PARENT) as z:
        entries = json.loads(z.read('MANIFEST.json'))
        for row in entries:
            value = z.read(row['path'])
            if len(value) != row['bytes'] or hashlib.sha256(value).hexdigest() != row['sha256']:
                raise ValueError('ZIP manifest mismatch: '+row['path'])
        def get(name):
            return json.loads(z.read('reports/'+name))
        def copy(name, dest):
            dest = root/dest
            dest.parent.mkdir(parents=True, exist_ok=True)
            with dest.open('xb') as f:
                f.write(z.read('reports/'+name))
        original = get('freeze.json')
        for sealname, prefix in (('model-seal.json', 'models/'), ('test-action-seal.json', '')):
            seal = get(sealname)
            if canonical_sha256(seal['files']) != seal['files_sha256']:
                raise ValueError('original seal digest differs')
            for row in seal['files']:
                if hashlib.sha256(z.read('reports/'+prefix+row['path'])).hexdigest() != row['sha256']:
                    raise ValueError('original sealed bytes differ')
            copy(sealname, 'parent/'+sealname)
        copy('freeze.json', 'parent/freeze.json')
        copy('models/A.json', 'models/A.json')
        if sha(root/'models/A.json') != A_SHA:
            raise ValueError('fixed A8 mismatch')
        history = get('history/seen-public.json')
        all_states = [get('states/'+p['state_id']+'/state.json') for p in original['planned']]
        history += all_states
        for row in history:
            row['exact_input_sha256'], row['business_input_sha256'] = request_pool_identity(row['request'], row['pool'])
        states = sorted([s for s in all_states if s['primary_eligible'] and s['split'] in ('train','validation')],
                        key=lambda s: (('train','validation').index(s['split']), s['state_id']))
        counts = Counter(s['split']+'/'+s['scenario'] for s in states)
        if counts != {'train/balanced':22,'train/scarce':24,'validation/balanced':7,'validation/scarce':8}:
            raise ValueError('original eligible split differs')
        if len({s['business_input_sha256'] for s in states}) != 61:
            raise ValueError('supervision business duplicate')
        rng = np.random.default_rng(2026111007)
        perm_rng = np.random.default_rng(2026111008)
        dataset = []
        for s in states:
            req, pool = unpack(s)
            rebuilt = sorted(generate_candidates(req, 16, 20261102), key=lambda c:c.id)
            if [c.to_dict() for c in rebuilt] != s['pool']:
                raise ValueError('original request/pool provenance differs')
            matrices = {}
            for folder, seed, n in [('reference',original['reference_seed'],32),
                                    ('online-library',original['online_seed'],8)]:
                rel = folder+'/'+s['state_id']+'/branches.jsonl'
                raw = [json.loads(line) for line in z.read('reports/'+rel).splitlines()]
                matrices[folder] = verify_library(raw,s,seed,n)
                copy(rel, 'reused/'+rel)
            matrix = matrices['reference']
            target, bootstrap_sha = soft_targets(matrix,rng)
            permutations = [list(range(len(pool)))]
            if s['split'] == 'train':
                wanted = min(4, math.factorial(min(len(pool), 4)))
                while len(permutations) < wanted:
                    p = perm_rng.permutation(len(pool)).tolist()
                    if p not in permutations:
                        permutations.append(p)
            label = dict(candidate_ids=[c.id for c in pool], target=target.tolist(),
                q_ref=[math.fsum(row)/32 for row in matrix], reference=matrix.tolist(),
                online=matrices['online-library'].tolist(), bootstrap_indices_sha256=bootstrap_sha,
                branch_sha256=sha(root/'reused/reference'/s['state_id']/'branches.jsonl'))
            dataset.append(dict(state=s, numeric=numeric(pool), labels=label,
                                label_sha256=canonical_sha256(label), permutations=permutations))
    # Original inventories plus all existing project run registrations. B2 is only planned.
    prior = seed_usage([ROOT/'runs', Path('G:/gpt-file/Codex/outputs/phase2e-a-20261104-181352'),
                       Path('G:/gpt-file/Codex/outputs/phase2e-b-20261110-141616')])
    used = {r['seed'] for r in prior} | {s['seed'] for s in all_states} | set(range(7000,7256))
    # Conservatively reserve B2's replacement block as well.
    used |= set(range(8000,8256))
    historical_planning = {original['online_seed'],original['reference_seed'],2026111003,2026111004}
    hits = []
    for base in (ROOT/'scripts',ROOT/'docs',ROOT/'runs'):
        for p in base.rglob('*'):
            if not p.is_file() or p.suffix not in ('.py','.json','.md') or p.stat().st_size > 2_000_000:
                continue
            if 'phase2ec1' in p.name.lower() or any(v in p.parts for v in ('responses','evaluation','branch-audit-store')):
                continue
            value = p.read_text(encoding='utf8',errors='replace')
            for n in (2026111013,2026111014):
                if str(n) in value:
                    historical_planning.add(n)
                    hits.append(dict(path=str(p),seed=n))
    plans, conflicts = [], []
    replacement = 10000
    for index, requested in enumerate(range(9000,9064)):
        seed = requested
        if seed in used:
            while replacement in used:
                replacement += 1
            seed = replacement
            conflicts.append(dict(requested=requested,replacement=seed))
        used.add(seed)
        plans.append(dict(index=index,scenario='scarce',seed=seed,requested_seed=requested,
                          state_id=f'scarce-{seed}-336',split='test',window_start=336,window_end=504))
    online, reference = 2026111013,2026111014
    while online in historical_planning:
        online += 100
    while reference in historical_planning or reference == online:
        reference += 100
    write(root/'data/dataset.json',dataset)
    write(root/'history/seen-public.json',history)
    write(root/'history/seed-usage.json',prior)
    write(root/'history/planning-seed-search.json',hits)
    write(root/'parent/verification.json',dict(archive=str(PARENT),sha256=PARENT_SHA,
          verified_manifest_files=len(entries),sealed_A_sha256=A_SHA,counts=dict(counts),new_des=0,
          labels_recomputed=len(states)*16*32, historical_test_used_for_learning=False))
    registration = dict(protocol='phase2ec1-v1',created_unix=time.time(),planned=plans,
        conflicts=conflicts,online_seed=online,reference_seed=reference,generation_seed=20261102,
        encoding=ENCODING,data_sha256=sha(root/'data/dataset.json'),history_sha256=sha(root/'history/seen-public.json'),
        limits=LIMITS,forward_max_seconds=3600,forward_attempt_seconds_reservation=60,
        bootstrap_n=5000,environment_bootstrap_seed=2026111015,mc_bootstrap_seed=2026111016,
        target_seed=2026111007,training_permutation_seed=2026111008,test_permutation_seed=2026111017,
        init_seeds=[11,29],formal_seed=11,epochs=20,group_batch=8,lr=.001,weight_decay=.01,clip=1.,
        saved_epochs=[0,2,5,10,15,20],gates=dict(min_environments=48,min_permutations=24,
            top2_consistency=.9,increment=.002,h32_regret_upper=.005,p50_ratio=.8,p95_ratio=1.),
        methods=['H32','H42','A8','S8','P8'],permutation_slots=list(range(16)),
        b2_reserved_ranges=[[7000,7255],[8000,8255]],reference_barrier='verified online-action-seal first')
    write(root/'registration.json',registration)
    return dict(states=len(dataset),counts=dict(counts),planned=len(plans),conflicts=conflicts)
