"""Read-only decomposition of fixed R1 labels and the actions actually selected.

This stdlib-only command reads a SHA-bound parent ZIP. It imports no simulator,
provider or selector and never evaluates a DES branch or performs network I/O.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import statistics
import sys
import time
import zipfile

PARENT_SHA = '6acec98dcb5b9842a5639231b266047c6dc03d19f98fec758183fd95e15c0dcd'
METHODS = ('mandatory-first', 'random-des', 'heuristic-des', 'local-direct', 'local-des', 'direct-des')
PREFILTER_METHODS = frozenset(('random-des', 'heuristic-des', 'local-des'))
NUMERICAL_TOLERANCE = 1e-12


def require(condition, message):
    if not condition:
        raise ValueError(message)


def strict_json(payload):
    def pairs(items):
        out = {}
        for key, value in items:
            require(key not in out, 'duplicate JSON object key: ' + key)
            out[key] = value
        return out
    def reject(value):
        raise ValueError('non-finite JSON value: ' + value)
    return json.loads(payload, object_pairs_hook=pairs, parse_constant=reject)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def identity(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def file_sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


class Parent:
    def __init__(self, archive):
        self.path = Path(archive)
        require(file_sha(self.path) == PARENT_SHA, 'fixed R1 parent ZIP SHA mismatch')
        self.zip = zipfile.ZipFile(self.path)
        names = self.zip.namelist()
        require(len(names) == len({n.casefold() for n in names}), 'duplicate parent ZIP path')
        for name in names:
            path = PurePosixPath(name)
            require(not path.is_absolute() and '..' not in path.parts and ':' not in name
                    and '\\' not in name and '\x00' not in name, 'unsafe parent ZIP path')
        manifest = strict_json(self.zip.read('MANIFEST.json'))
        self.prefix = 'project/' + manifest['evidence_relative'] + '/'
        self.entries = {row['path']: row for row in manifest['files']}
        require(len(self.entries) == len(manifest['files']), 'duplicate parent manifest path')
        self.consumed = {}

    def read_bytes(self, relative):
        name = self.prefix + relative
        payload = self.zip.read(name)
        row = self.entries[name]
        sha = hashlib.sha256(payload).hexdigest()
        require(sha == row['sha256'] and len(payload) == row.get('size_bytes', row.get('size')),
                'parent payload manifest mismatch: ' + relative)
        self.consumed[relative] = {'path': name, 'size_bytes': len(payload), 'sha256': sha}
        return payload

    def read(self, relative):
        return strict_json(self.read_bytes(relative))

    def close(self):
        self.zip.close()


def business_projection(state):
    """Remove only pure request/protocol/candidate identity packaging; preserve values/order."""
    request = {key: value for key, value in state['request'].items()
               if key not in ('request_id', 'protocol_version')}
    pool = [{key: value for key, value in candidate.items() if key != 'id'}
            for candidate in state['pool']]
    return {'request': request, 'pool': pool}


def paired(values_a, values_b):
    require(len(values_a) == len(values_b) == 64, 'paired reference requires 64 common scenarios')
    differences = [a - b for a, b in zip(values_a, values_b)]
    mean = math.fsum(differences) / 64
    se = statistics.stdev(differences) / 8
    return {'paired_difference_mean': mean, 'paired_difference_se': se,
            'paired_mc_normal_interval': [mean - 1.96 * se, mean + 1.96 * se],
            'mc_approximate_tie': mean - 1.96 * se <= 0 <= mean + 1.96 * se}


def scalar_scores(raw_lines, state):
    """Recompute the saved short-window DSR+RCR scalar from business counts."""
    scores = {candidate['id']: [] for candidate in state['pool']}
    for line in raw_lines.splitlines():
        row = strict_json(line)
        key = row['candidate_id']
        require(key in scores and row['scenario_index'] == len(scores[key]) < 64,
                'reference candidate/scenario order differs')
        require(row['planning_seed'] == 20261103 and row['purpose_namespace'] == 'reference-v1',
                'unexpected R1 reference identity')
        counts = row['counts']
        dsr = counts['success'] / counts['offered'] if counts['offered'] else 0.
        rcr = counts['earned_value'] / counts['offered_value'] if counts['offered_value'] else 0.
        require(row['score'] == dsr + rcr, 'reference scalar differs from raw business counts')
        require(row['score_components'] == {'DSR': dsr, 'RCR': rcr}, 'reference components differ')
        require(row['audit']['score'] == row['score'] and row['audit']['verified'] is True,
                'original independent audit scalar differs')
        require(math.isfinite(row['score']), 'non-finite reference score')
        scores[key].append(row['score'])
    require(all(len(values) == 64 for values in scores.values()), 'missing 64-scenario reference')
    return scores


def decompose(state, action, record, scores):
    method = action['method']
    selected = record['selected_candidate_id']
    require(selected == action['selected_candidate_id'] and method == record['method'], 'action/record differs')
    require(action['status'] == 'completed', 'R1 action incomplete')
    require(record['budget']['actual_des'] == action['actual_des'] == len(record['branches']),
            'action/record DES count differs')
    if method != 'mandatory-first':
        require(len(record['ranking_ids']) == len(scores) and set(record['ranking_ids']) == set(scores),
                'ranking is not a permutation of the actual pool')
    means = {key: math.fsum(values) / 64 for key, values in scores.items()}
    best_id = min(means, key=lambda key: (-means[key], key)) if means else None
    best = means.get(best_id)
    row = {'state_id': state['state_id'], 'scenario': state['scenario'], 'environment_seed': state['seed'],
           'window_start': state['window_start'], 'window_end': state['window_end'], 'method': method,
           'actual_M': len(scores), 'selected_candidate_id': selected, 'q_ref_selected': means.get(selected),
           'pool_best_candidate_id': best_id, 'q_ref_pool_best': best,
           'actual_prefilter_ids': None, 'retained_best_candidate_id': None, 'q_ref_retained_best': None,
           'screening_loss': None, 'within_retained_selection_loss': None,
           'final_action_regret': best - means[selected] if selected in means else None,
           'prefilter_status': 'N/A: method has no prefilter set',
           'actual_online_des': action['actual_des'],
           'local_direct_action': selected if method == 'local-direct' else None,
           'legacy_export_top_k': action.get('top_k')}
    if selected in means:
        row['final_regret_mc'] = paired(scores[best_id], scores[selected])
    if method in PREFILTER_METHODS:
        retained = record['ranking_ids'][:min(4, len(scores))]
        require(len(retained) == min(4, len(scores)) and retained == action['top_k'] and len(set(retained)) == len(retained)
                and set(retained) <= set(scores), 'actual retained ranking differs')
        branch_ids = {branch['candidate_id'] for branch in record['branches']}
        require(branch_ids == set(retained) and selected in retained, 'actual evaluated set differs from prefilter')
        require(len(record['branches']) == len(retained) * 8, 'prefilter budget differs')
        require(Counter(branch['candidate_id'] for branch in record['branches'])
                == Counter({key: 8 for key in retained}), 'retained candidate branch allocation differs')
        retained_best = min(retained, key=lambda key: (-means[key], key)) if retained else None
        row.update(actual_prefilter_ids=retained, retained_best_candidate_id=retained_best,
                   q_ref_retained_best=means.get(retained_best), prefilter_status='actual pre-DES retained set')
        if retained_best is not None:
            row.update(screening_loss=best - means[retained_best],
                       within_retained_selection_loss=means[retained_best] - means[selected],
                       screening_loss_mc=paired(scores[best_id], scores[retained_best]),
                       within_retained_loss_mc=paired(scores[retained_best], scores[selected]))
            require(abs(row['screening_loss'] + row['within_retained_selection_loss'] - row['final_action_regret'])
                    <= 1e-15, 'loss decomposition identity differs')
    if method == 'local-direct':
        require(not record['branches'] and action['actual_des'] == 0
                and selected == record['ranking_ids'][0], 'local-direct was not direct selection')
        row['prefilter_status'] = 'N/A: directly selected first local-ranked candidate; exported legacy top_k is descriptive only'
    if method == 'direct-des':
        require({branch['candidate_id'] for branch in record['branches']} == set(scores),
                'direct-des did not evaluate the full actual pool')
    return row


def write_json(path, value):
    with Path(path).open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')


def write_csv(path, rows):
    keys = list(dict.fromkeys(key for row in rows for key in row))
    with Path(path).open('x', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, keys)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: json.dumps(value, ensure_ascii=False, separators=(',', ':'))
                             if isinstance(value, (dict, list)) else value for key, value in row.items()})


def summarize(rows, states):
    groups = []
    state_map = {row['state_id']: row for row in states}
    for scope in ('all_24_planned', 'original_R1_unique_holdout', 'unseen_business_representatives'):
        for scene in ('balanced', 'scarce', 'all_scenes_descriptive'):
            for method in METHODS:
                selected = [row for row in rows if row['method'] == method
                            and (scene == 'all_scenes_descriptive' or row['scenario'] == scene)
                            and (scope == 'all_24_planned'
                                 or scope == 'original_R1_unique_holdout' and state_map[row['state_id']]['original_R1_unique_holdout']
                                 or scope == 'unseen_business_representatives' and state_map[row['state_id']]['unseen_business_representative'])]
                out = {'scope': scope, 'scenario': scene, 'method': method, 'states': len(selected),
                       'environment_trajectories': len({(r['scenario'], r['environment_seed']) for r in selected})}
                for key in ('screening_loss', 'within_retained_selection_loss', 'final_action_regret'):
                    values = [row[key] for row in selected if row[key] is not None]
                    out[key] = math.fsum(values) / len(values) if values else None
                    out[key + '_available_states'] = len(values)
                groups.append(out)
    return groups


def run(archive, output):
    output = Path(output)
    require(not output.exists(), 'output directory must be new')
    started = time.perf_counter()
    parent = Parent(archive)
    try:
        states = parent.read('diagnostics/states.json')
        actions = parent.read('diagnostics/online-actions.json')
        seen = parent.read('diagnostics/seen-public-states.json')
        seal = parent.read('diagnostics/online-complete.json')
        require(len(states) == 24 and len(actions) == 144 and seal['sha256'] == identity(actions),
                'R1 complete online seal differs')
        require(sum(a['actual_des'] for a in actions) == seal['actual_des'], 'sealed online budget differs')
        seen_exact = {identity({'request': s['request'], 'pool': s['pool']}) for s in seen}
        seen_business = {identity(business_projection(s)) for s in seen}
        within_exact, within_business = {}, {}
        details, state_rows, candidate_rows, pair_rows, improvements = [], [], [], [], []
        for state in states:
            sid = state['state_id']
            require(state['actual_M'] == len(state['pool']), 'actual pool size differs')
            require(state['canonical_request_sha256'] == identity(state['request'])
                    and state['pool_sha256'] == identity(state['pool']), 'original exact input hash differs')
            exact = identity({'request': state['request'], 'pool': state['pool']})
            business = identity(business_projection(state))
            score_bytes = parent.read_bytes('diagnostics/evaluation/' + sid + '/branches.jsonl')
            scores = scalar_scores(score_bytes, state)
            means = {key: math.fsum(values) / 64 for key, values in scores.items()}
            winner = min(means, key=lambda key: (-means[key], key)) if means else None
            count_exact = sum(value == means[winner] for value in means.values()) if winner else 0
            count_numeric = sum(abs(value - means[winner]) <= NUMERICAL_TOLERANCE for value in means.values()) if winner else 0
            mc_ties = 0
            for key in scores:
                check = paired(scores[winner], scores[key])
                mc_ties += int(check['mc_approximate_tie'])
                candidate_rows.append({'state_id': sid, 'candidate_id': key, 'N_common_scenarios': 64,
                                       'q_ref': means[key], 'mc_standard_error': statistics.stdev(scores[key]) / 8,
                                       'reference_scores': scores[key], 'exact_tie_with_sample_best': means[key] == means[winner],
                                       'numerical_tie_with_sample_best': abs(means[key] - means[winner]) <= NUMERICAL_TOLERANCE,
                                       **check})
            for ai, a in enumerate(sorted(scores)):
                for b in sorted(scores)[ai + 1:]:
                    pair_rows.append({'state_id': sid, 'candidate_a': a, 'candidate_b': b,
                                      'exact_reference_mean_tie': means[a] == means[b],
                                      'numerical_reference_mean_tie': abs(means[a] - means[b]) <= NUMERICAL_TOLERANCE,
                                      **paired(scores[a], scores[b])})
            current = [a for a in actions if a['state_id'] == sid]
            require(Counter(a['method'] for a in current) == Counter(METHODS), 'R1 method coverage differs')
            local = parent.read('diagnostics/' + sid + '/shared-ranking.json')
            for action in current:
                record = parent.read('diagnostics/' + sid + '/' + action['method'] + '.json')
                if action['method'] != 'mandatory-first':
                    require(identity(record['request']) == identity(state['request'])
                            and identity(record['pool']) == identity(state['pool']), 'method public input differs')
                selected_candidate = next((c for c in state['pool'] if c['id'] == record['selected_candidate_id']), None)
                require(selected_candidate is not None and record['command'] == selected_candidate['command'],
                        'selected action command differs from pool')
                details.append(decompose(state, action, record, scores))
            local_action = next(a['selected_candidate_id'] for a in current if a['method'] == 'local-des')
            heuristic_action = next(a['selected_candidate_id'] for a in current if a['method'] == 'heuristic-des')
            improvements.append({'state_id': sid, 'scenario': state['scenario'], 'environment_seed': state['seed'],
                                 'regret_heuristic_minus_regret_local': means[local_action] - means[heuristic_action],
                                 **paired(scores[local_action], scores[heuristic_action])})
            score_counts = Counter(local['preference_scores'].values())
            state_rows.append({'state_id': sid, 'scenario': state['scenario'], 'environment_seed': state['seed'],
                               'actual_M': len(scores), 'pool_reference_min': min(means.values()),
                               'pool_reference_max': max(means.values()), 'pool_reference_span': max(means.values()) - min(means.values()),
                               'exact_ties_with_sample_best_including_self': count_exact,
                               'numerical_ties_with_sample_best_including_self': count_numeric,
                               'mc_approximate_ties_with_sample_best_including_self': mc_ties,
                               'local_integer_score_tie_groups': {str(k): v for k, v in score_counts.items() if v > 1},
                               'exact_request_pool_sha256': exact, 'business_request_pool_sha256': business,
                               'cross_seen_exact_duplicate': exact in seen_exact,
                               'cross_seen_business_duplicate': business in seen_business,
                               'within_batch_exact_duplicate_of': within_exact.get(exact),
                               'within_batch_business_duplicate_of': within_business.get(business),
                               'unseen_business_representative': exact not in seen_exact and business not in seen_business
                                      and exact not in within_exact and business not in within_business,
                               'original_R1_cross_split_duplicate': state['cross_split_duplicate'],
                               'original_R1_within_holdout_duplicate': state['within_holdout_duplicate'],
                               'original_R1_unique_holdout': not state['cross_split_duplicate'] and not state['within_holdout_duplicate']})
            within_exact.setdefault(exact, sid)
            within_business.setdefault(business, sid)
        aggregates = summarize(details, state_rows)
        summary = {'schema': 'phase2e-r1-decomposition-v1', 'status': 'passed',
                   'parent_archive_sha256': PARENT_SHA, 'states': len(states), 'method_rows': len(details),
                   'actual_pool_sizes': dict(Counter(s['actual_M'] for s in state_rows)),
                   'reference_scalars_read': sum(len(row['reference_scores']) for row in candidate_rows),
                   'original_R1_unique_holdout_states': sum(row['original_R1_unique_holdout'] for row in state_rows),
                   'cross_seen_exact_duplicates': sum(row['cross_seen_exact_duplicate'] for row in state_rows),
                   'cross_seen_business_duplicates': sum(row['cross_seen_business_duplicate'] for row in state_rows),
                   'within_batch_exact_duplicates': sum(row['within_batch_exact_duplicate_of'] is not None for row in state_rows),
                   'within_batch_business_duplicates': sum(row['within_batch_business_duplicate_of'] is not None for row in state_rows),
                   'unseen_business_representatives': sum(row['unseen_business_representative'] for row in state_rows),
                   'numeric_absolute_tolerance': NUMERICAL_TOLERANCE,
                   'prefilter_methods': sorted(PREFILTER_METHODS),
                   'no_prefilter_methods': sorted(set(METHODS) - PREFILTER_METHODS),
                   'new_des': 0, 'new_generations': 0, 'new_http_requests': 0,
                   'ordinary_tests_des': None, 'whole_engineering_des': None,
                   'business_projection': 'request without request_id/protocol_version plus full ordered pool without candidate id; no business values or numeric types removed',
                   'representative_rule': 'first persisted R1 state order; exclude exact or business duplicates against earlier seen inputs and earlier batch rows',
                   'mc_rule': 'candidate-paired 64-scenario difference mean +/- 1.96 sample SD / 8; sample-best fixed; descriptive, not simultaneous inference',
                   'boundary': 'Finite actual pool, short current-offer-cohort DSR+RCR reference; not pool-external coverage, long-term Q, global optimum, equal-wall-clock superiority or 720h noninferiority. Within-set selection loss has several causes and is not all attributed to DES noise. R1 limited-benefit conclusion remains.',
                   'seconds': time.perf_counter() - started}
        output.mkdir(parents=True)
        for name, rows in (('per-state-method', details), ('states-and-duplicates', state_rows),
                           ('per-candidate-reference', candidate_rows), ('candidate-pairs', pair_rows),
                           ('paired-local-vs-heuristic', improvements), ('aggregates', aggregates)):
            write_json(output / (name + '.json'), rows)
            write_csv(output / (name + '.csv'), rows)
        write_json(output / 'consumed-inputs.json', sorted(parent.consumed.values(), key=lambda row: row['path']))
        write_json(output / 'summary.json', summary)
        report_lines = ['R1既有标签机会与损失分解（只读）', '',
                        '固定父包SHA256：' + PARENT_SHA,
                        f"实际24状态、144方法行动、{summary['reference_scalars_read']}条原参考分支标量；新增DES/生成/HTTP均为0。",
                        f"原R1去重口径保留{summary['original_R1_unique_holdout_states']}状态；本次精确request+pool/去身份业务投影同时审查，未见业务代表{summary['unseen_business_representatives']}状态。",
                        '实际池大小：' + canonical(summary['actual_pool_sizes']), '',
                        '只有random-des、heuristic-des和local-des具有实际预筛集合S。local-direct直接选择首个本地排名动作；历史导出的top_k仅描述排名，不作为筛选集合。mandatory-first与direct-des的两项筛选分解均为N/A。', '',
                        '原R1未见去重状态的描述性均值（状态并非独立环境；不以窗口数扩大环境样本量）：']
        for row in aggregates:
            if row['scope'] == 'original_R1_unique_holdout' and row['scenario'] == 'all_scenes_descriptive':
                report_lines.append(f"{row['method']}: n={row['states']}, 筛选损失={row['screening_loss']}, 保留集内选择损失={row['within_retained_selection_loss']}, 最终动作遗憾={row['final_action_regret']}")
        report_lines += ['', 'q_ref由已保存64个共同参考情景的实际DSR+RCR标量重算。candidate-pairs逐一列出配对差、标准误和MC近似平局；per-candidate-reference保留全部64标量。局部样本最大值的选择偏差仍在，近似平局不是同时置信集合。',
                         '以上仅为有限池内、当前offer cohort的固定状态短窗诊断。筛选损失不是池外覆盖误差；保留集内选择损失也不能全部归因于DES噪声。不称长期Q或全局最优，不改变R1有限收益结论，不证明720h非劣、长期可用度改善或规则泛化。',
                         '原始记录与父包均只读；普通测试与全工程DES未知总量继续为null。']
        (output / '报告.txt').write_text('\n'.join(report_lines) + '\n', encoding='utf-8')
        write_json(output / 'command.json', {'argv': sys.orig_argv, 'interpreter': sys.executable,
                                            'cwd': str(Path.cwd()), 'script_sha256': file_sha(__file__),
                                            'utc_finished': datetime.now(timezone.utc).isoformat(),
                                            'result': summary})
        return summary
    finally:
        parent.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--parent-archive', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    result = run(args.parent_archive, args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
