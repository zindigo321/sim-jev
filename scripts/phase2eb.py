"""Phase 2E-B frozen residual-ranking and real DES-budget experiment.

Commands are separated at the test-label barrier. All live computation is
persistently reserved; evidence auditing never invokes DES or a model.
"""
from __future__ import annotations
import argparse
from collections import Counter
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import traceback
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import numpy as np
from sim_jev.candidates import generate_candidates, heuristic_ranking
from sim_jev.decision import DecisionResult
from sim_jev.local_provider import LocalProvider, validate_record, render_for_identity
from sim_jev.planning import PlanningInput, PlanningScenario
from sim_jev.provenance import canonical_sha256, code_fingerprint
from sim_jev.residual_ranker import FEATURES, QWEN_FEATURES, GRID, features, fit, predict, ranked_ids
from sim_jev.weekly_policy import PolicySpec, choose_command, validate_command
from scripts.phase2d_local import unpack
from scripts.phase2d_r1 import immutable, build_case
from scripts.phase2e_diagnostic import (read, file_sha, request_pool_identity, collect_seen_public, seed_usage)
from scripts.phase2e_live import save, TOKEN_PY, TOKEN_JSON
from scripts.planning_verify import audit_planning_branch
from scripts.weekly_verify import audit_business_log
from scripts.phase2eb_runtime import service, Provider

A_ROOT = Path('G:/gpt-file/Codex/outputs/phase2e-a-20261104-181352')
R1_ROOT = ROOT / 'runs/phase2d-r1-20261103-191950'
LIMITS = dict(reference=49152, train_online=8192, test_online=2048)
METHODS = ('h0', 'h32', 'h8', 'A8', 'B8', 'q8')


def jsonlines(path):
    with Path(path).open(encoding='utf-8') as stream:
        return [json.loads(line) for line in stream if line.strip()]


def append(path, row):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(row, ensure_ascii=False, separators=(',', ':'), allow_nan=False) + '\n')
        stream.flush()
        os.fsync(stream.fileno())


def manifest(folder, exclude=()):
    folder = Path(folder)
    return [dict(path=p.relative_to(folder).as_posix(), bytes=p.stat().st_size, sha256=file_sha(p))
            for p in sorted(folder.rglob('*')) if p.is_file() and p.name not in exclude]


def check(root):
    f = read(Path(root) / 'freeze.json')
    if canonical_sha256(f) != read(Path(root) / 'freeze-sha.json')['sha256']:
        raise ValueError('freeze modified')
    if code_fingerprint(ROOT) != f['source']:
        raise ValueError('execution source differs from freeze')
    if file_sha(ROOT / 'docs/PHASE2EB_CONTRACT.md') != f['contract_sha256']:
        raise ValueError('contract differs from freeze')
    return f


def prepare(root):
    root = Path(root)
    if (root / 'freeze.json').exists():
        raise ValueError('freeze already exists')
    gate = read(root / 'engineering-tests/command.json')
    if gate['status'] != 'passed' or not gate['source_unchanged'] or gate['source'] != code_fingerprint(ROOT):
        raise ValueError('full engineering gate must pass on the frozen source')
    prior = seed_usage([ROOT / 'runs', A_ROOT])
    # Include planning/reference seeds from all historical source and manifests.
    historical_seed_numbers = {v['seed'] for v in prior}
    token_hits = []
    for base in (ROOT / 'scripts', ROOT / 'docs', A_ROOT):
        for p in base.rglob('*'):
            if p.is_file() and p.suffix in ('.py', '.md', '.json') and p.stat().st_size < 2_000_000:
                if any(part in ('responses', 'evaluation', 'http') for part in p.relative_to(base).parts):
                    continue
                text = p.read_text(encoding='utf-8', errors='replace')
                for n in (2026110901, 2026110902):
                    # The current 2E-B contract/source is the declaration, not history.
                    if 'phase2eb' in p.name.lower():
                        continue
                    if str(n) in text:
                        historical_seed_numbers.add(n)
                        token_hits.append(dict(path=str(p), seed=n))
    plan, conflicts = [], []
    for scene, start, replacement in (('balanced', 3000, 5000), ('scarce', 4000, 6000)):
        used = {v['seed'] for v in prior if v.get('scenario') in (None, scene)}
        used |= set(range(start, start + 48))
        for index in range(48):
            original = start + index
            conflict = any(v['seed'] == original and v.get('scenario') in (None, scene) for v in prior)
            seed = original
            if conflict:
                while replacement in used:
                    replacement += 1
                seed = replacement
                used.add(seed)
                conflicts.append(dict(scene=scene, requested=original, replacement=seed))
            split = 'train' if index < 24 else 'validation' if index < 32 else 'test'
            plan.append(dict(scenario=scene, seed=seed, requested_seed=original, index=index,
                             split=split, state_id=f'{scene}-{seed}-336', window_start=336, window_end=504))
    online_seed, ref_seed = 2026110901, 2026110902
    while online_seed in historical_seed_numbers:
        online_seed += 100
    while ref_seed in historical_seed_numbers or ref_seed == online_seed:
        ref_seed += 100
    seen = collect_seen_public(R1_ROOT) + collect_seen_public(A_ROOT)
    for row in seen:
        row['exact_input_sha256'], row['business_input_sha256'] = request_pool_identity(row['request'], row['pool'])
    immutable(root / 'history/seed-usage.json', prior)
    immutable(root / 'history/new-seed-search-hits.json', token_hits)
    immutable(root / 'history/seen-public.json', seen)
    old_states = read(A_ROOT / 'diagnostics/states.json')
    order_states = [s for scene in ('balanced', 'scarce') for s in [r for r in old_states if r['scenario'] == scene][:2]]
    immutable(root / 'history/order-states.json', order_states)
    identity = read(A_ROOT / 'freeze.json')['identity']
    if (file_sha(TOKEN_PY) != identity['tokenizer_runtime']['python_executable_sha256'] or
            file_sha(TOKEN_JSON) != identity['tokenizer_sha256']):
        raise ValueError('tokenizer byte identity differs')
    archive = Path('G:/sim-jev-audit/sim-jev-phase2e-a-20261104-181352.zip')
    declared = archive.with_suffix('.zip.sha256').read_text().split()[0]
    if file_sha(archive) != declared:
        raise ValueError('2E-A archive SHA mismatch')
    with zipfile.ZipFile(archive) as z:
        if z.testzip() is not None:
            raise ValueError('2E-A archive CRC error')
        comparisons = []
        for rel in ('freeze.json', 'diagnostics/states.json', 'diagnostics/evaluation/paired-summary.json'):
            name = 'reports/' + rel
            equal = z.read(name) == (A_ROOT / rel).read_bytes()
            comparisons.append(dict(member=name, matches=equal))
            if not equal:
                raise ValueError('2E-A folder differs from fixed ZIP: ' + rel)
    immutable(root / 'history/parent-verification.json', dict(archive=str(archive), sha256=declared, comparisons=comparisons))
    source = code_fingerprint(ROOT)
    hardware = {}
    commands = {
        'system': ['powershell', '-NoProfile', '-Command',
            'Get-CimInstance Win32_ComputerSystem | Select-Object Manufacturer,Model,TotalPhysicalMemory,NumberOfLogicalProcessors | ConvertTo-Json -Compress'],
        'cpu': ['powershell', '-NoProfile', '-Command',
            'Get-CimInstance Win32_Processor | Select-Object Name,NumberOfCores,NumberOfLogicalProcessors | ConvertTo-Json -Compress'],
        'gpu': ['nvidia-smi', '--query-gpu=name,driver_version,memory.total', '--format=csv,noheader'],
        'numpy': [sys.executable, '-c', 'import numpy as np;np.show_config()']}
    for name, command in commands.items():
        p = subprocess.run(command, capture_output=True, text=True, encoding='utf8', errors='replace', timeout=30,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        hardware[name] = dict(argv=command, exit_code=p.returncode, stdout=p.stdout, stderr=p.stderr)
    immutable(root / 'hardware/system.json', hardware)
    for item in source['files']:
        dest = root / 'source' / item['path']
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / item['path'], dest)
    shutil.copytree(ROOT / 'docs', root / 'source/docs', dirs_exist_ok=True)
    slots = [f'order-{i}' for i in range(8)] + [r['state_id'] for r in plan]
    frozen = dict(schema='phase2eb-v1', source=source, identity=identity,
        contract_sha256=file_sha(ROOT / 'docs/PHASE2EB_CONTRACT.md'), planned=plan,
        seed_conflicts=conflicts, online_seed=online_seed, reference_seed=ref_seed,
        generation_seed=20261102, features=list(FEATURES), qwen_features=list(QWEN_FEATURES), grid=list(GRID),
        history_sha256=canonical_sha256(seen), historical_seed_usage_sha256=canonical_sha256(prior),
        formal_limits=LIMITS, methods=list(METHODS), order_diagnostics=[r['state_id'] for r in order_states],
        environment_bootstrap_seed=2026111001, mc_bootstrap_seed=2026111002, bootstrap_n=5000,
        gates=dict(train_min=12, validation_min=4, test_min=12, regret_upper=.005, p50_ratio=.8, p95_ratio=1.),
        prewarm='first registered order request cold; no unregistered warmup; no extra CPU DES',
        method_order='rotate first five methods by global test index modulo five; q8 always last',
        fallback='Qwen unavailable => h8, including failed request latency; trained B unavailable => partial route',
        test_labels='closed until all test actions and raw request ledger sealed',
        probe_des_limit=128, ordinary_test_des_limit=100000, clean_test_des_limit=10000,
        created_unix=time.time())
    immutable(root / 'freeze.json', frozen)
    immutable(root / 'freeze-sha.json', dict(sha256=canonical_sha256(frozen)))
    immutable(root / 'generation-budget.json', dict(protocol='phase2eb-generation-v1',
        max_attempts=208, max_seconds=5400, max_logical=104, slots=slots, attempts=[], seconds=0.))
    immutable(root / 'http-budget.json', dict(protocol='phase2eb-local-http-v1', max_metadata=450, requests=[]))
    return dict(planned=len(plan), conflicts=conflicts, source_sha256=source['sha256'])


def get_scores(record):
    if record is None or record['evidence'] == 'deterministic':
        return None
    return {record['rendered']['candidate_mapping'][k]: v for k, v in record['preference_scores'].items()}


def historical(root):
    """0 new DES. Index split is persisted before reading any historic labels."""
    root = Path(root)
    immutable(root / 'exploration/index-split.json', dict(online=list(range(8)),
        evaluation='available indices >=8', K=[1, 2, 4], B=[2, 4, 8], heldout=False,
        timer='offline evidence query only; never online latency'))
    rows = []
    started = time.perf_counter()
    for source_name, base in (('R1', R1_ROOT), ('2E-A', A_ROOT)):
        for state in read(base / 'diagnostics/states.json'):
            req, pool = unpack(state)
            path = base / 'diagnostics/evaluation' / state['state_id'] / 'branches.jsonl'
            if not path.exists():
                rows.append(dict(source=source_name, state_id=state['state_id'], status='N/A', reason='missing branches'))
                continue
            raw = jsonlines(path)
            by = {(r['candidate_id'], r['scenario_index']): r['score'] for r in raw}
            indices = sorted(set(r['scenario_index'] for r in raw) - set(range(8)))
            if not indices or any((c.id, j) not in by for c in pool for j in indices):
                rows.append(dict(source=source_name, state_id=state['state_id'], status='N/A', reason='incomplete quality branches'))
                continue
            quality = {c.id: math.fsum(by[c.id, j] for j in indices) / len(indices) for c in pool}
            order = [c.id for c in heuristic_ranking(pool)]
            for k, b in [(1, 0)] + [(k, b) for k in (1, 2, 4) for b in (2, 4, 8)]:
                kept = order[:k]
                missing = b and any((c, j) not in by for c in kept for j in range(b))
                chosen = None if missing else (kept[0] if b == 0 else min(kept, key=lambda c: (-math.fsum(by[c, j] for j in range(b)) / b, c)))
                rows.append(dict(source=source_name, state_id=state['state_id'], scenario=state['scenario'],
                    K=k, B=b, status='N/A' if missing else 'available', selected=chosen,
                    retained=kept, simulated_online_des=len(kept) * b, new_actual_des=0,
                    quality_indices=indices, final_regret=max(quality.values()) - quality[chosen] if chosen else None,
                    screening_loss=max(quality.values()) - max(quality[c] for c in kept),
                    within_selection_loss=max(quality[c] for c in kept) - quality[chosen] if chosen else None))
    immutable(root / 'exploration/results.json', dict(rows=rows, new_des=0, new_generations=0,
        offline_query_seconds=time.perf_counter() - started))
    return len(rows)


class Journal:
    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root / 'des-journal.jsonl'
        self.binding = read(self.root / 'freeze-sha.json')['sha256']
        self.entries = {}
        for e in jsonlines(self.path) if self.path.exists() else []:
            if e['freeze_sha256'] != self.binding:
                raise ValueError('DES journal identity differs')
            if e['status'] == 'pending':
                if e['key'] in self.entries:
                    raise ValueError('duplicate DES reservation')
            elif e['status'] != 'completed' or self.entries.get(e['key'], {}).get('status') != 'pending':
                raise ValueError('invalid DES completion')
            self.entries[e['key']] = e
        self.counts = Counter(e['phase'] for e in self.entries.values())
        self.pending = {k for k, e in self.entries.items() if e['status'] != 'completed'}
        if any(self.counts[k] > v for k, v in LIMITS.items()):
            raise ValueError('DES budget exceeded')

    def reserve(self, phase, key):
        if self.pending:
            raise ValueError('uncertain DES prevents recomputation')
        if phase not in LIMITS or self.counts[phase] >= LIMITS[phase] or key in self.entries:
            raise ValueError('DES cap or duplicate')
        e = dict(freeze_sha256=self.binding, phase=phase, key=key, status='pending', unix=time.time())
        append(self.path, e)
        self.entries[key] = e
        self.pending.add(key)
        self.counts[phase] += 1

    def finish(self, key, row, artifact):
        e = dict(self.entries[key], status='completed', scalar_sha256=canonical_sha256(row), artifact=artifact)
        append(self.path, e)
        self.entries[key] = e
        self.pending.remove(key)


def evaluate(root, journal, state, request, pool, retained, b, seed, phase, namespace):
    """Every method really evaluates its branches; only within-method worlds are shared."""
    root = Path(root)
    dest = root / namespace
    dest.mkdir(parents=True, exist_ok=False)
    candidates = {c.id: c for c in pool}
    rows, scenarios = [], {}
    timing = dict(des_seconds=0., audit_seconds=0., io_seconds=0., scenario_seconds=0.)
    for index in range(b):
        for cid in retained:
            key = f'{namespace}/{cid}/{index}'
            t = time.perf_counter()
            journal.reserve(phase, key)
            timing['io_seconds'] += time.perf_counter() - t
            t = time.perf_counter()
            if index not in scenarios:
                scenarios[index] = PlanningScenario(PlanningInput(request, seed, index))
            timing['scenario_seconds'] += time.perf_counter() - t
            t = time.perf_counter()
            result = scenarios[index].evaluate(candidates[cid].command)
            compute_seconds = time.perf_counter() - t
            timing['des_seconds'] += compute_seconds
            t = time.perf_counter()
            audit = audit_planning_branch(result, request)
            audit_seconds = time.perf_counter() - t
            timing['audit_seconds'] += audit_seconds
            t = time.perf_counter()
            row = {k: v for k, v in result.items() if k != 'final_state'}
            row.update(candidate_id=cid, physical_artifact_sha256=canonical_sha256(result['final_state']),
                       audit=audit, compute_seconds=compute_seconds, audit_seconds=audit_seconds)
            # Index-zero physical evidence is retained for each evaluated candidate.
            if index == 0:
                immutable(dest / f'physical-{cid}-0.json', result)
            append(dest / 'branches.jsonl', row)
            journal.finish(key, row, (dest / 'branches.jsonl').relative_to(root).as_posix())
            timing['io_seconds'] += time.perf_counter() - t
            rows.append(row)
    means = {cid: math.fsum(r['score'] for r in rows if r['candidate_id'] == cid) / b for cid in retained} if b else {}
    immutable(dest / 'cost.json', dict(timing, actual_des=len(rows), simulated_hours=168 * len(rows)))
    return rows, means, timing


def capture(root, planned, prior):
    root = Path(root)
    captured = []
    def intercept(req, spec, runtime=None):
        if req.context.window_start == 336:
            captured.append(req)
        return DecisionResult(choose_command(req.observation, req.context, spec))
    t = time.perf_counter()
    with patch('sim_jev.weekly.resolve_decision', side_effect=intercept):
        sim = build_case(planned['scenario'], planned['seed'], PolicySpec())
        sim.run(336)
    construction_seconds = time.perf_counter() - t
    if len(captured) != 1:
        raise ValueError('one request at 336 required')
    request = captured[0]
    t = time.perf_counter()
    pool = sorted(generate_candidates(request, 16, 20261102), key=lambda c: c.id)
    candidate_seconds = time.perf_counter() - t
    t = time.perf_counter()
    audit_business_log(sim)
    audit_seconds = time.perf_counter() - t
    row = dict(planned, request=request.to_dict(), pool=[c.to_dict() for c in pool], actual_M=len(pool))
    exact, business = request_pool_identity(row['request'], row['pool'])
    duplicate = next((s.get('state_id', s.get('origin', 'historical')) for s in prior
                      if s['exact_input_sha256'] == exact or s['business_input_sha256'] == business), None)
    row.update(exact_input_sha256=exact, business_input_sha256=business,
        exact_duplicate=any(s['exact_input_sha256'] == exact for s in prior),
        business_duplicate=any(s['business_input_sha256'] == business for s in prior),
        duplicate_of=duplicate, primary_eligible=duplicate is None)
    directory = root / 'states' / row['state_id']
    immutable(directory / 'state.json', row)
    immutable(directory / 'mandatory-trajectory-checkpoint.json', sim.to_checkpoint())
    immutable(directory / 'construction.json', dict(trajectory_build_calls=1, run_calls=1, simulated_hours=336,
        candidate_des=0, construction_seconds=construction_seconds, candidate_seconds=candidate_seconds, audit_seconds=audit_seconds))
    prior.append(row)
    return row


def qwen(root, slot, request, pool, live):
    """All failures retained, never retried as another logical request."""
    path = Path(root) / 'qwen' / (slot + '.json')
    if path.exists():
        raise ValueError('Qwen slot already attempted')
    t = time.perf_counter()
    record, receipt, error = None, None, None
    if len(pool) < 2:
        record = LocalProvider(read(Path(root) / 'freeze.json')['identity'], mode='cache_only').rank(request, pool)
    elif live is None:
        error = 'service_unavailable'
    else:
        provider = Provider(root, slot, *live)
        try:
            record = provider.rank(request, pool, force=True)
            receipt = provider.ranking_receipts[-1]
        except Exception as exc:
            error = f'{type(exc).__name__}: {exc}'
    result = dict(slot=slot, status='completed' if record else 'failed', record=record,
                  receipt=receipt, error=error, total_seconds=time.perf_counter() - t)
    immutable(path, result)
    return result


def order_diagnostics(root, live):
    states = read(Path(root) / 'history/order-states.json')
    results = []
    for i, state in enumerate(states):
        request, pool = unpack(state)
        records = []
        for reverse in (False, True):
            ordered = pool[::-1] if reverse else pool
            rendered = render_for_identity(request, ordered, read(Path(root) / 'freeze.json')['identity'])
            if [r['id'] for r in rendered['canonical']['candidates']] != [c.id for c in ordered]:
                raise ValueError('renderer discarded candidate order')
            value = qwen(root, f'order-{2*i+int(reverse)}', request, ordered, live)
            records.append(value)
            print('order', i, reverse, value['status'], flush=True)
        a, b = (get_scores(r['record']) for r in records)
        raw = jsonlines(A_ROOT / 'diagnostics/evaluation' / state['state_id'] / 'branches.jsonl')
        lookup = {(r['candidate_id'], r['scenario_index']): r['score'] for r in raw}
        comparisons = []
        for scores in (a, b):
            order = ranked_ids(scores) if scores else None
            kept = order[:2] if order else None
            chosen = min(kept, key=lambda cid: (-math.fsum(lookup[cid, j] for j in range(4)), cid)) if kept else None
            comparisons.append(dict(top2=kept, top4=order[:4] if order else None, selected=chosen))
        results.append(dict(state_id=state['state_id'], aligned_scores_equal=a == b if a and b else None,
            aligned_score_changes={cid: b[cid]-a[cid] for cid in a} if a and b else None,
            original=comparisons[0], reversed=comparisons[1], actual_new_des=0,
            action_scope='historic index0..3 offline selector, no extra DES or deployment claim',
            seconds=[r['total_seconds'] for r in records]))
    immutable(Path(root) / 'order-diagnostics.json', results)


def train_data(root, live, journal):
    frozen = check(root)
    prior = read(Path(root) / 'history/seen-public.json')
    train = [p for p in frozen['planned'] if p['split'] == 'train']
    first = [p for p in train if p['index'] < 4]
    rest = [p for p in train if p not in first]
    validation = [p for p in frozen['planned'] if p['split'] == 'validation']
    built = []
    for index, planned in enumerate(first + rest + validation):
        state = capture(root, planned, prior)
        req, pool = unpack(state)
        qwen(root, state['state_id'], req, pool, live)
        # Labels are permitted here: these are training/validation only.
        evaluate(root, journal, state, req, pool, [c.id for c in pool], 32,
                 frozen['reference_seed'], 'reference', 'reference/' + state['state_id'])
        evaluate(root, journal, state, req, pool, [c.id for c in pool], 8,
                 frozen['online_seed'], 'train_online', 'online-library/' + state['state_id'])
        built.append(state)
        save(Path(root) / 'train-data-progress.json', dict(completed=len(built), counts=dict(journal.counts)))
        print('train/validation', len(built), state['state_id'], state['primary_eligible'], dict(journal.counts), flush=True)
        if index == 7:
            smoke = smoke_models(root, built)
            immutable(Path(root) / 'first-eight-gate.json', dict(status='passed' if smoke['B'] else 'structured_passed_qwen_partial',
                state_ids=[s['state_id'] for s in built], smoke=smoke,
                same_campaign=True, reference_des=journal.counts['reference'], online_des=journal.counts['train_online']))
    return sorted(built, key=lambda s: (('train', 'validation').index(s['split']),
                                       ('balanced', 'scarce').index(s['scenario']), s['index']))


def library(root, state, folder):
    rows = jsonlines(Path(root) / folder / state['state_id'] / 'branches.jsonl')
    return {c['id']: [r['score'] for r in sorted(rows, key=lambda r: r['scenario_index']) if r['candidate_id'] == c['id']]
            for c in state['pool']}


def select_library(order, values, k=2, b=4):
    retained = order[:k]
    return min(retained, key=lambda c: (-math.fsum(values[c][:b]) / b, c)) if len(order) > 1 else order[0] if order else None


def smoke_models(root, states):
    """Exercise training/save/reload/scoring and selection on the first 4+4 train states.

    Fixed alpha=1 is only an engineering probe, never used to choose final models.
    It reuses the already-budgeted online library, adding zero DES.
    """
    result = {}
    for name in ('A', 'B'):
        groups, saved = [], []
        for s in states:
            if not s['primary_eligible'] or not s['pool']:
                continue
            req, pool = unpack(s)
            q = get_scores(read(Path(root) / 'qwen' / (s['state_id'] + '.json'))['record'])
            if name == 'B' and q is None:
                continue
            ref = library(root, s, 'reference')
            x = features(pool, q if name == 'B' else None)
            y = np.asarray([math.fsum(ref[c.id])/32-c.summary['heuristic']['score'] for c in pool])
            groups.append((x, y))
            saved.append((s, pool, q))
        if not groups:
            result[name] = False
            continue
        model = fit(groups, 1., qwen=name == 'B')
        path = Path(root) / 'smoke' / (name + '.json')
        immutable(path, model)
        reloaded = read(path)
        predictions = []
        for s, pool, q in saved:
            a, b = predict(model, pool, q), predict(reloaded, pool[::-1], q)
            if a != b:
                raise ValueError('smoke reload/permutation mismatch')
            chosen = select_library(ranked_ids(a), library(root, s, 'online-library'))
            predictions.append(dict(state_id=s['state_id'], scores=a, selected=chosen))
        immutable(Path(root) / 'smoke' / (name + '-predictions.json'), predictions)
        result[name] = name == 'A' or len(saved) == sum(s['primary_eligible'] and bool(s['pool']) for s in states)
    return result


def train_models(root, states):
    root = Path(root)
    gate = {scene: {split: sum(s['primary_eligible'] and s['scenario'] == scene and s['split'] == split
             for s in states) for split in ('train', 'validation')} for scene in ('balanced', 'scarce')}
    sufficient = all(v['train'] >= 12 and v['validation'] >= 4 for v in gate.values())
    models = {}
    scores_by_state = {s['state_id']: get_scores(read(root / 'qwen' / (s['state_id'] + '.json'))['record']) for s in states}
    references = {s['state_id']: library(root, s, 'reference') for s in states}
    online = {s['state_id']: library(root, s, 'online-library') for s in states}
    feature_table = []
    for s in states:
        _, pool = unpack(s)
        q = scores_by_state[s['state_id']]
        x = features(pool, q)
        for c, vector in zip(pool, x):
            feature_table.append(dict(state_id=s['state_id'], split=s['split'], eligible=s['primary_eligible'],
                candidate_id=c.id, features=[float(v) if np.isfinite(v) else None for v in vector],
                names=list(FEATURES + (QWEN_FEATURES if q else ())),
                nominal=c.summary['heuristic']['score'], q_ref=float(np.mean(references[s['state_id']][c.id]))))
    immutable(root / 'models/training-feature-table.json', feature_table)
    for name, use_qwen in (('A', False), ('B', True)):
        usable = [s for s in states if s['primary_eligible'] and len(s['pool']) > 0 and
                  (not use_qwen or scores_by_state[s['state_id']] is not None)]
        counts = {scene: {split: sum(s['scenario'] == scene and s['split'] == split for s in usable)
                        for split in ('train', 'validation')} for scene in ('balanced', 'scarce')}
        route_ok = sufficient and all(v['train'] >= 12 and v['validation'] >= 4 for v in counts.values())
        if not route_ok:
            immutable(root / 'models' / (name + '-partial.json'), dict(status='partial', counts=counts, reason='insufficient eligible train/validation or Qwen'))
            continue
        groups = []
        for state in usable:
            if state['split'] != 'train':
                continue
            _, pool = unpack(state)
            x = features(pool, scores_by_state[state['state_id']] if use_qwen else None)
            y = np.array([math.fsum(references[state['state_id']][c.id]) / 32 - c.summary['heuristic']['score'] for c in pool])
            groups.append((x, y))
        started = time.perf_counter()
        candidates, validations = {}, []
        for alpha in GRID:
            model = fit(groups, alpha, qwen=use_qwen)
            losses = []
            for state in usable:
                if state['split'] != 'validation':
                    continue
                _, pool = unpack(state)
                predictions = predict(model, pool, scores_by_state[state['state_id']])
                order = ranked_ids(predictions)
                chosen = select_library(order, online[state['state_id']])
                means = {cid: math.fsum(v) / 32 for cid, v in references[state['state_id']].items()}
                loss = max(means.values()) - means[chosen]
                losses.append(loss)
                validations.append(dict(model=name, alpha=alpha, state_id=state['state_id'],
                    predictions=predictions, retained=order[:2], selected=chosen, regret=loss))
            candidates[alpha] = (math.fsum(losses) / len(losses), model)
        immutable(root / 'models' / (name + '-grid.json'),
                  {str(alpha): model for alpha, (_, model) in candidates.items()})
        best_value = min(v[0] for v in candidates.values())
        alpha = max(a for a, v in candidates.items() if abs(v[0] - best_value) <= 1e-12)
        model = candidates[alpha][1]
        training_seconds = time.perf_counter() - started
        immutable(root / 'models' / (name + '.json'), model)
        reloaded = read(root / 'models' / (name + '.json'))
        permutation_rows = []
        for state in usable:
            _, pool = unpack(state)
            first = predict(model, pool, scores_by_state[state['state_id']])
            loaded = predict(reloaded, pool, scores_by_state[state['state_id']])
            reverse = predict(reloaded, pool[::-1], scores_by_state[state['state_id']])
            if first != loaded or first != reverse:
                raise ValueError('model reload/order score identity differs')
            permutation_rows.append(dict(state_id=state['state_id'], id_aligned_equal=True))
        immutable(root / 'models' / (name + '-validation.json'), validations)
        immutable(root / 'models' / (name + '-training.json'), dict(alpha=alpha, counts=counts,
            seconds=training_seconds, selected_validation_regret=best_value,
            training_data_sha256=canonical_sha256([r for r in feature_table if r['split'] == 'train' and r['eligible']]),
            reload_equal=True, permutation_checks=permutation_rows, qwen_weights_trained=False))
        models[name] = model
    immutable(root / 'models/gate.json', dict(eligible_counts=gate, models=list(models), status='passed' if len(models) == 2 else 'partial'))
    files = manifest(root / 'models')
    immutable(root / 'model-seal.json', dict(files=files, files_sha256=canonical_sha256(files),
        source_sha256=check(root)['source']['sha256'], sealed_unix=time.time(), test_states_built=0))
    return models


def test_action(root, frozen, journal, state, method, models, live, shared):
    request, _ = unpack(state)
    started = time.perf_counter()  # Receipt of the legal DecisionRequest.
    pool = sorted(generate_candidates(request, 16, frozen['generation_seed']), key=lambda c: c.id)
    build_seconds = time.perf_counter() - started
    if [c.to_dict() for c in pool] != state['pool']:
        raise ValueError('independently rebuilt candidate pool differs')
    fallback, failure, predictions, qresult = False, None, None, None
    qwen_seconds = 0.
    if method == 'B8':
        t = time.perf_counter()
        qresult = qwen(root, state['state_id'], request, pool, live)
        qwen_seconds = time.perf_counter() - t
        shared = qresult
    scores = get_scores(shared['record']) if shared else None
    t = time.perf_counter()
    order = [c.id for c in heuristic_ranking(pool)]
    if method in ('A8', 'B8') and len(pool) >= 2:
        name = method[0]
        if name in models and (name == 'A' or scores is not None or len(pool) < 2):
            if pool and (name == 'A' or scores is not None):
                predictions = predict(models[name], pool, scores)
                order = ranked_ids(predictions)
        else:
            fallback = True
            failure = shared['error'] if name == 'B' and shared and shared['error'] else 'trained_route_unavailable'
    if method == 'q8':
        if scores is not None:
            order = ranked_ids(scores)
        elif len(pool) >= 2:
            fallback, failure = True, shared['error'] if shared else 'no_shared_qwen'
    rank_seconds = time.perf_counter() - t
    k, b = (4, 8) if method == 'h32' else (1, 0) if method == 'h0' else (2, 4)
    retained = order[:k]
    if len(pool) < 2:
        b = 0
    namespace = f'test/{state["state_id"]}/{method}'
    branches, values, cost = evaluate(root, journal, state, request, pool, retained if b else [], b,
        frozen['online_seed'], 'test_online', namespace)
    chosen = min(values, key=lambda cid: (-values[cid], cid)) if values else (order[0] if order else None)
    command = next(c.command for c in pool if c.id == chosen) if chosen else choose_command(request.observation, request.context, PolicySpec())
    t = time.perf_counter()
    checked = validate_command(command, request.observation, request.context)
    if checked != command:
        raise ValueError('selected command failed canonical validation')
    validation_seconds = time.perf_counter() - t
    total_seconds = time.perf_counter() - started
    result = dict(state_id=state['state_id'], scenario=state['scenario'], method=method,
        selected_candidate_id=chosen, retained_ids=retained if method != 'h0' and b else None,
        ranking_ids=order, predictions=predictions, command=command.to_dict(), actual_des=len(branches),
        K=min(k, len(pool)), B=b, fallback=fallback, failure=failure, rule_violations=0,
        deterministic_skip='empty' if not pool else 'singleton' if len(pool) == 1 else None,
        continuous_wall_seconds=total_seconds if method != 'q8' else None,
        reused_score_path_seconds=total_seconds if method == 'q8' else None,
        timing=dict(candidate_build_seconds=build_seconds, qwen_path_seconds=qwen_seconds,
            rank_seconds=rank_seconds, command_validation_seconds=validation_seconds, **cost),
        qwen_cost_scope='new real request inside continuous B8' if method == 'B8' else
            'reuse B8 score; quality control only' if method == 'q8' else None,
        qwen_cache_key=shared['record']['cache_key'] if shared and shared['record'] and method in ('B8', 'q8') else None)
    if method in ('A8', 'B8') and predictions is not None:
        matrix = features(pool, scores if method == 'B8' else None)
        result['feature_table'] = {c.id: [float(v) if np.isfinite(v) else None for v in x] for c, x in zip(pool, matrix)}
    t = time.perf_counter()
    immutable(Path(root) / namespace / 'action.json', result)
    immutable(Path(root) / namespace / 'post-selection-io.json', dict(seconds=time.perf_counter()-t,
        scope='result serialization after validated action; excluded from decision latency'))
    return result, shared


def test_online(root, states, models, live, journal):
    frozen = check(root)
    if manifest(Path(root) / 'models') != read(Path(root) / 'model-seal.json')['files']:
        raise ValueError('models changed before test')
    prior = read(Path(root) / 'history/seen-public.json') + states
    results = []
    tests = [p for p in frozen['planned'] if p['split'] == 'test']
    for index, planned in enumerate(tests):
        state = capture(root, planned, prior)
        base = list(METHODS[:5])
        offset = index % 5
        order = base[offset:] + base[:offset] + ['q8']
        shared = None
        for method in order:
            action, shared = test_action(root, frozen, journal, state, method, models, live, shared)
            results.append(action)
        immutable(Path(root) / 'test' / state['state_id'] / 'method-order.json', order)
        print('test-online', index+1, state['state_id'], state['primary_eligible'], dict(journal.counts), flush=True)
    immutable(Path(root) / 'test/actions.json', results)
    return results


def selection_files(root):
    root = Path(root)
    files = []
    for folder in ('test', 'models', 'states', 'responses', 'qwen', 'http', 'hardware'):
        for row in manifest(root / folder):
            files.append(dict(row, path=folder + '/' + row['path']))
    for name in ('generation-budget.json', 'http-budget.json', 'model-seal.json', 'tokenizer-live-calls.json'):
        p = root / name
        files.append(dict(path=name, bytes=p.stat().st_size, sha256=file_sha(p)))
    return sorted(files, key=lambda r: r['path'])


def run(root):
    root = Path(root)
    frozen = check(root)
    if (root / 'run-started.json').exists():
        raise ValueError('run attempted; preserve partial and do not automatically retry')
    immutable(root / 'run-started.json', dict(unix=time.time(), freeze_sha256=canonical_sha256(frozen)))
    journal = Journal(root)
    try:
        with service(root) as live:
            order_diagnostics(root, live)
            states = train_data(root, live, journal)
            models = train_models(root, states)
            test_online(root, states, models, live, journal)
        from scripts.phase2eb_audit import audit_online
        verification = audit_online(root, require_seal=False)
        files = selection_files(root)
        immutable(root / 'test-action-seal.json', dict(files=files, files_sha256=canonical_sha256(files),
            pre_reference_verification=verification, sealed_unix=time.time(), counts=dict(journal.counts),
            source_sha256=frozen['source']['sha256']))
        immutable(root / 'run-status.json', dict(status='actions_sealed', counts=dict(journal.counts), models=list(models)))
    except Exception as exc:
        immutable(root / 'run-partial.json', dict(status='partial', error=f'{type(exc).__name__}: {exc}',
            traceback=traceback.format_exc(), counts=dict(journal.counts)))
        raise


def test_reference(root):
    root = Path(root)
    frozen = check(root)
    from scripts.phase2eb_audit import audit_online
    audit_online(root)
    if (root / 'test-reference-started.json').exists():
        raise ValueError('test reference already attempted; preserve partial')
    immutable(root / 'test-reference-started.json', dict(unix=time.time(),
        action_seal_sha256=file_sha(root / 'test-action-seal.json')))
    journal = Journal(root)
    for planned in [s for s in frozen['planned'] if s['split'] == 'test']:
        state = read(root / 'states' / planned['state_id'] / 'state.json')
        req, pool = unpack(state)
        evaluate(root, journal, state, req, pool, [c.id for c in pool], 32,
            frozen['reference_seed'], 'reference', 'reference/' + state['state_id'])
        print('test-reference', state['state_id'], dict(journal.counts), flush=True)
    immutable(root / 'test-reference-status.json', dict(status='completed', counts=dict(journal.counts)))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'historical', 'run', 'reference', 'analyze', 'audit'))
    parser.add_argument('--root', type=Path, required=True)
    args = parser.parse_args(argv)
    if args.action == 'prepare':
        result = prepare(args.root)
    elif args.action == 'historical':
        result = historical(args.root)
    elif args.action == 'run':
        result = run(args.root)
    elif args.action == 'reference':
        result = test_reference(args.root)
    elif args.action == 'analyze':
        from scripts.phase2eb_analysis import analyze
        result = analyze(args.root)
    else:
        from scripts.phase2eb_audit import audit
        result = audit(args.root)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
