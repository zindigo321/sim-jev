"""Post-seal, read-only audit entry; the original experiment source stays frozen.

The original auditor changed NumPy reduction order and therefore exact float tie
membership. This auditor reconstructs the frozen labels with explicit reductions
on the original array layout. It never changes labels or runs model/DES work.
"""
from pathlib import Path
import argparse
import json
import subprocess
import sys
import time
from collections import Counter
import numpy as np


def audit(root, receipt=None):
    if not __debug__:
        raise RuntimeError('audit requires normal Python')
    root = Path(root).resolve()
    snapshot = root / 'source'
    sys.path.insert(0, str(snapshot))
    from scripts.phase2ec1_common import (read, write, sha, verify_files,
                                          canonical_sha256, Journal, lines)
    from scripts.phase2ec1_analysis import branch_matrix, compute
    from scripts.phase2d_local import unpack
    from scripts.phase2e_diagnostic import request_pool_identity
    from scripts.planning_verify import audit_planning_branch
    from sim_jev.candidates import generate_candidates, heuristic_ranking
    from sim_jev.residual_ranker import ranked_ids, predict
    from sim_jev.weekly_policy import validate_command
    from sim_jev.provenance import code_fingerprint

    started = time.perf_counter()
    reg = read(root / 'registration.json')
    for name in ('model-seal.json', 'online-action-seal.json'):
        seal = read(root / name)
        assert canonical_sha256(seal['files']) == seal['files_sha256']
        verify_files(root, seal['files'])
    assert code_fingerprint(snapshot) == read(root / 'model-seal.json')['source']
    assert sha(snapshot / 'scripts/phase2ec1_analysis.py') == '7c00d8809c50c151a705b73f5a29389d4ef1e271e0c701b84c68fb45aa08912d'
    entries = Journal(root).entries
    assert all(e['status'] != 'pending' for e in entries.values())
    assert all(e['status'] == 'completed' for e in entries.values() if e['phase'] != 'engineering_des')
    assert sum(e.get('seconds', 0) for e in entries.values() if e['phase'] == 'forward') <= 3600
    assert min(e['unix'] for e in entries.values() if e['phase'] == 'reference') > read(root / 'online-action-seal.json')['sealed_unix']
    assert min(e['unix'] for e in entries.values() if e['phase'] == 'trajectory') > read(root / 'model-seal.json')['sealed_unix']

    rng = np.random.default_rng(reg['target_seed'])
    data = read(root / 'data/dataset.json')
    parent = read(root / 'parent/freeze.json')
    labels_checked = 0
    for d in data:
        s = d['state']
        matrix, _ = branch_matrix(root, s, 'reused/reference/' + s['state_id'], parent['reference_seed'])
        indices = rng.integers(0, 32, (512, 32))
        # Match the frozen float64 reduction layout, without calling soft_targets.
        means = (np.add.reduce(matrix[:, indices], axis=2) / 32).T
        winners = means == np.max(means, axis=1)[:, None]
        weights = winners / np.count_nonzero(winners, axis=1)[:, None]
        expected = .98 * (np.add.reduce(weights, axis=0) / 512) + .02 / len(matrix)
        np.testing.assert_array_equal(expected, d['labels']['target'])
        assert canonical_sha256(indices.tolist()) == d['labels']['bootstrap_indices_sha256']
        assert canonical_sha256(d['labels']) == d['label_sha256']
        assert sha(root / 'reused/reference' / s['state_id'] / 'branches.jsonl') == d['labels']['branch_sha256']
        labels_checked += len(matrix) * 32

    history = (read(root / 'history/seen-public.json') + read(root / 'history/supplement-public.json') + read(root / 'history/supplement-completion.json'))
    exact = {s['exact_input_sha256'] for s in history}
    business = {s['business_input_sha256'] for s in history}
    actionmap = {(a['state_id'], a['method']): a for a in read(root / 'test/actions.json')}
    pairrows = {(a['state_id'], a['tag']): a for a in read(root / 'permutation/actions.json')}
    observed = set()
    raw_des = physical = 0
    for plan in reg['planned']:
        sid = plan['state_id']
        s = read(root / 'states' / sid / 'state.json')
        a, b = request_pool_identity(s['request'], s['pool'])
        assert (a, b) == (s['exact_input_sha256'], s['business_input_sha256'])
        assert s['primary_eligible'] == (a not in exact and b not in business)
        exact.add(a)
        business.add(b)
        req, pool = unpack(s)
        assert [c.to_dict() for c in sorted(generate_candidates(req, 16, 20261102), key=lambda c: c.id)] == s['pool']
        groups = [('test/' + sid + '/' + m, actionmap[sid, m]) for m in reg['methods']]
        groups += [('permutation/' + sid + '/' + tag, p) for (stateid, tag), p in pairrows.items() if stateid == sid]
        shared = {}
        for folder, action in groups:
            assert action['status'] == 'completed'
            kept = action['retained_ids']
            _, rows = branch_matrix(root, s, folder, reg['online_seed'], action['B'], kept if action['B'] else [])
            import math
            values = {cid: math.fsum(r['score'] for r in rows if r['candidate_id'] == cid) / action['B'] for cid in kept} if rows else {}
            chosen = min(values, key=lambda cid: (-values[cid], cid)) if values else (action['ranking_ids'][0] if pool else None)
            assert chosen == action['selected_candidate_id']
            command = next(c.command for c in pool if c.id == chosen) if chosen else None
            if command:
                assert command.to_dict() == action['command'] and validate_command(command, req.observation, req.context) == command
            if action['predictions'] is not None:
                assert ranked_ids(action['predictions']) == action['ranking_ids']
            if action['method'] == 'A8':
                assert predict(read(root / 'models/A.json'), pool) == action['predictions']
            if action['method'] in ('H32', 'H42'):
                assert [c.id for c in heuristic_ranking(pool)] == action['ranking_ids']
            if action['method'] == 'P8':
                f = read(root / 'forwards' / (folder + '.json'))
                assert f['actual_forward_calls'] == action['forward_calls'] == 1 and not f['autoregressive_calls']
                assert f['encoded']['candidate_ids'] == action['input_candidate_ids']
                assert set(f['encoded']['candidate_ids']) == {c.id for c in pool}
                assert len(f['encoded']['input_ids']) == action['token_count'] <= 2048
            for r in rows:
                key = (r['candidate_id'], r['scenario_index'])
                if key in shared:
                    assert shared[key] == r['score']
                shared[key] = r['score']
        branch_matrix(root, s, 'reference/' + sid, reg['reference_seed'])
        groups.append(('reference/' + sid, {}))
        for folder, _ in groups:
            rows = lines(root / folder / 'branches.jsonl')
            for r in rows:
                key = folder + '/' + r['candidate_id'] + '/' + str(r['scenario_index'])
                assert key in entries and entries[key]['scalar_sha256'] == canonical_sha256(r)
                observed.add(key)
                raw_des += 1
                if r['scenario_index'] == 0:
                    p = read(root / folder / ('physical-' + r['candidate_id'] + '-0.json'))
                    assert canonical_sha256(p['final_state']) == r['physical_artifact_sha256']
                    audit_planning_branch(p, req)
                    physical += 1
        print('audit', plan['index'] + 1, sid, raw_des, flush=True)
    expected = {key for key, e in entries.items() if e['phase'] in ('online', 'permutation', 'reference')}
    assert expected == observed
    forwards = 0
    for path in (root / 'forwards').rglob('*.json'):
        f = read(path)
        key = 'forward/' + f['slot']
        assert entries[key]['scalar_sha256'] == canonical_sha256(f)
        assert f['actual_forward_calls'] == 1 and not f['trainable_backbone_parameters'] and not f['backbone_gradients']
        forwards += 1
    assert forwards == sum(e['phase'] == 'forward' for e in entries.values())
    assert compute(root) == read(root / 'analysis/results.json')
    process = subprocess.run(['D:/conda_envs/cgfedllm/python.exe', '-B', str(snapshot / 'scripts/phase2ec1_head_audit.py'), str(root)], capture_output=True, text=True, encoding='utf8', errors='replace', creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if process.returncode:
        raise ValueError('fresh-process tensor audit failed: ' + process.stderr)
    head_audit = json.loads(process.stdout)
    assert head_audit['status'] == 'passed'
    result = dict(status='passed', auditor_version='post-seal-readonly-v2', auditor_sha256=sha(__file__), original_auditor_failure_preserved=True,
        raw_research_des=raw_des, physical_samples=physical, reused_label_branches=labels_checked, actual_forwards=forwards,
        counts=dict(Counter(e['phase'] for e in entries.values())), label_barrier=True, all_seals_verified=True,
        new_des=0, new_forward=0, new_updates=0, seconds=time.perf_counter()-started,
        source_sha256=code_fingerprint(snapshot)['sha256'], head_tensor_audit=head_audit)
    if receipt is not None:
        write(Path(receipt), result)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--receipt', type=Path, help='Optional new receipt path; default only prints and never writes evidence.')
    args = parser.parse_args()
    print(json.dumps(audit(args.root, args.receipt), ensure_ascii=False))
