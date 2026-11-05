"""Executable engineering gates, evidence capture and standalone audit packaging."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import zipfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from sim_jev.provenance import code_fingerprint, canonical_sha256, git_state
from scripts.phase2d_local import read, save


def logged(argv, cwd, folder, name):
    folder.mkdir(parents=True, exist_ok=True)
    log = folder / (name + '.log')
    if log.exists():
        raise ValueError('command evidence already exists: ' + str(log))
    started = time.perf_counter()
    with log.open('x', encoding='utf-8') as stream:
        process = subprocess.run([str(x) for x in argv], cwd=cwd, stdout=stream, stderr=subprocess.STDOUT)
    receipt = {'argv': [str(x) for x in argv], 'cwd': str(cwd), 'exit_code': process.returncode,
        'seconds': time.perf_counter() - started, 'log': str(log)}
    save(folder / (name + '.command.json'), receipt)
    print(name, process.returncode, receipt['seconds'], flush=True)
    if process.returncode:
        raise RuntimeError('verification failed: ' + str(log))
    return receipt


def legacy_replay(root, verification):
    folder = root / ('legacy-v1' if not (root / 'legacy-v1').exists() else 'legacy-v1-' + str(time.time_ns()))
    folder.mkdir(exist_ok=True)
    source = ROOT / 'runs/phase2d-local-20261103-01/smoke'
    target = folder / 'smoke'
    target.mkdir(exist_ok=False)
    paths = sorted(source.glob('completion-*.json'))
    if len(paths) != 20:
        raise ValueError('historical 20 real response captures missing')
    for path in paths:
        shutil.copy2(path, target / path.name)
    code = ('import socket; from unittest.mock import patch; from pathlib import Path; '
        'from scripts.phase2d_local import captured_replay; '
        'guard=patch.object(socket,"socket",side_effect=AssertionError("network forbidden")); '
        'guard.start(); print(captured_replay(Path(__import__("sys").argv[1])))')
    logged([sys.executable, '-c', code, folder], ROOT, verification, 'legacy-v1-replay')
    value = read(folder / 'captured-replay/status.json')
    if value['status'] != 'passed' or value['captures'] != 20 or value['new_local_generations'] != 0:
        raise AssertionError('legacy capture replay incomplete')
    return value, folder


def engineering(root):
    start_source = code_fingerprint(ROOT)
    out = root / ('verification' if not (root / 'verification').exists() else 'verification-' + str(time.time_ns()))
    out.mkdir(exist_ok=True)
    receipts = []
    receipts.append(logged([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-v'], ROOT, out, 'ordinary-tests'))
    text = (out / 'ordinary-tests.log').read_text(encoding='utf-8')
    count = int(re.findall(r'Ran (\d+) tests?', text)[-1])
    if count < 324 or '\nOK' not in text:
        raise AssertionError('ordinary suite did not retain historical tests')
    receipts.append(logged([sys.executable, ROOT / 'scripts/phase2c_compatibility.py',
        '--output', out / 'legacy-2a2b'], ROOT, out, 'legacy-2a2b'))
    receipts.append(logged([sys.executable, ROOT / 'scripts/phase2c_science_regression.py',
        '--archive', 'G:/sim-jev-audit/sim-jev-phase2c-audit-20261102-626224fb.zip',
        '--experiment', ROOT / 'runs/phase2d-local-20261103-01/old2c-regenerated',
        '--output', out / 'legacy-2c'], ROOT, out, 'legacy-2c'))
    legacy, legacy_folder = legacy_replay(root, out)
    comparison = read(out / 'legacy-2c/comparison.json')
    if comparison['status'] != 'passed' or comparison['restored_checkpoints'] != 60 or comparison['identical_science_files'] != 540:
        raise AssertionError('legacy 2C verification incomplete')
    if start_source != code_fingerprint(ROOT):
        raise ValueError('source changed during engineering verification; retain evidence and rerun in new folder')
    from scripts.phase2d_r1 import ENGINEERING_GATES
    proofs = []
    for path in sorted(out.rglob('*')):
        if path.is_file() and (path.suffix == '.log' or path.name in ('comparison.json', 'compatibility.json')):
            proofs.append({'path': path.relative_to(root).as_posix(), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    proofs.append({'path': (legacy_folder / 'captured-replay/status.json').relative_to(root).as_posix(),
                   'sha256': hashlib.sha256((legacy_folder / 'captured-replay/status.json').read_bytes()).hexdigest()})
    result = {key: True for key in ENGINEERING_GATES}
    result.update(status='passed', source_sha256=start_source['sha256'], ordinary_test_count=count,
        exit_code=0, proofs=proofs, commands=receipts, legacy_v1=legacy,
        legacy_2c={'checkpoints': 60, 'science_files': 540, 'comparison': 'legacy original protocol; existing regenerated science compared, 60 snapshots independently restored/continued'})
    save(root / 'engineering-gates.json', result)
    return result


def package(root, archive, destination):
    if archive.exists() or destination.exists():
        raise ValueError('archive and extraction destinations must be new')
    files = []
    for name in ('sim_jev', 'scripts', 'tests', 'configs', 'docs'):
        files.extend(p for p in (ROOT / name).rglob('*') if p.is_file() and '__pycache__' not in p.parts)
    files.extend(ROOT / name for name in ('README.md', 'pyproject.toml', '.gitignore'))
    files.extend(p for p in root.rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix not in ('.pyc', '.pyo'))
    # Root-local source copies and explicit scientific evidence only. No model
    # weights, runtime, environment, user credentials or unrelated runs are visited.
    files = sorted(set(files))
    entries = []
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, 'x', zipfile.ZIP_DEFLATED) as bundle:
        for path in files:
            if path.suffix.lower() in ('.exe', '.dll', '.gguf', '.pem', '.key') or path.name.startswith('.env'):
                raise ValueError('unexpected sensitive/binary artifact: ' + str(path))
            payload = path.read_bytes()
            name = 'project/' + path.relative_to(ROOT).as_posix()
            entries.append({'path': name, 'size': len(payload), 'sha256': hashlib.sha256(payload).hexdigest()})
            bundle.writestr(name, payload)
        manifest = {'schema': 'phase2d-r1-audit-package-v1', 'source': code_fingerprint(ROOT),
            'files': entries, 'evidence_relative': root.relative_to(ROOT).as_posix(),
            'historical_evidence': 'explicit copies only; original runs/archives are unchanged',
            'excluded': ['.git', 'weights', 'runtime binaries', 'Conda/tokenizer environments', 'secrets', 'unrelated historical runs']}
        bundle.writestr('MANIFEST.json', json.dumps(manifest, indent=2))
    with archive.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    archive.with_suffix('.zip.sha256').write_text(digest + '  ' + archive.name + '\n', encoding='utf-8')
    destination.mkdir(parents=True)
    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
        if len(names) != len(set(names)) or set(names) != {x['path'] for x in entries} | {'MANIFEST.json'}:
            raise AssertionError('archive entry list differs')
        for item in entries:
            payload = bundle.read(item['path'])
            if len(payload) != item['size'] or hashlib.sha256(payload).hexdigest() != item['sha256']:
                raise AssertionError('archive entry hash differs')
        for name in names:
            if not (destination / name).resolve().is_relative_to(destination.resolve()):
                raise AssertionError('unsafe archive path')
        bundle.extractall(destination)
    extracted = destination / 'project'
    if (extracted / '.git').exists() or code_fingerprint(extracted) != manifest['source']:
        raise AssertionError('standalone extracted source identity differs')
    receipts = [logged([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-v'], extracted, destination, 'ordinary-tests')]
    evidence = extracted / manifest['evidence_relative']
    legacy = evidence / 'legacy-v1'
    if (legacy / 'captured-replay/status.json').exists():
        code = ('import socket; from unittest.mock import patch; from pathlib import Path; '
            'from scripts.phase2d_local import captured_replay; '
            'guard=patch.object(socket,"socket",side_effect=AssertionError("network forbidden")); '
            'guard.start(); print(captured_replay(Path(__import__("sys").argv[1]),label="extracted-replay"))')
        receipts.append(logged([sys.executable, '-c', code, legacy], extracted, destination, 'legacy-real-captured-replay'))
    if (evidence / 'pilot/status.json').exists() and read(evidence / 'pilot/status.json')['status'] == 'passed':
        # The supplied replay is retained in the ZIP. Extracted replay writes to
        # a separate evidence clone using only the input caches and live results.
        replay_root = evidence / 'extracted-replay-input'
        replay_root.mkdir()
        shutil.copy2(evidence / 'freeze.json', replay_root / 'freeze.json')
        shutil.copytree(evidence / 'pilot', replay_root / 'pilot')
        receipts.append(logged([sys.executable, extracted / 'scripts/phase2d_r1.py', 'replay',
            '--output', replay_root], extracted, destination, 'r1-live-pilot-replay'))
    result = {'status': 'passed', 'archive_sha256': digest, 'entries_verified': len(entries) + 1,
        'source_sha256': manifest['source']['sha256'], 'commands': receipts, 'no_git': True,
        'no_external_historical_runs_needed': True, 'gpu_revalidated': False,
        'all_entries_read_and_hashed': True, 'extracted_to': str(destination)}
    save(archive.with_suffix('.zip.verification.json'), result)
    return result


def report(root, destination):
    from scripts.phase2d_r1 import ALL_GATES, ENGINEERING_GATES
    destination.mkdir(parents=True, exist_ok=True)
    engineering_path = root / 'engineering-gates.json'
    engineering = read(engineering_path) if engineering_path.exists() else {}
    development = read(root / 'development/status.json') if (root / 'development/status.json').exists() else {}
    data = read(root / 'data-audit.json') if (root / 'data-audit.json').exists() else {}
    pilot = read(root / 'pilot/status.json') if (root / 'pilot/status.json').exists() else {}
    replay = read(root / 'cache-only-replay/status.json') if (root / 'cache-only-replay/status.json').exists() else {}
    budget = read(root / 'generation-budget.json') if (root / 'generation-budget.json').exists() else {'attempts': [], 'seconds': 0.}
    stages = {key: engineering.get(key, False) for key in ENGINEERING_GATES}
    stages.update(all_development_states_valid=development.get('all_development_states_valid', False),
        new_live_stability_verified=development.get('new_live_stability_verified', False),
        formal_diagnostics_completed=data.get('status') == 'passed',
        pilot_completed=pilot.get('local_pilot_completed', False),
        cache_only_replay_verified=replay.get('cache_only_replay_verified', False))
    counts = {phase: sum(row['phase'] == phase for row in budget['attempts']) for phase in ('development', 'diagnostics', 'pilot')}
    remaining = {phase: limit - counts[phase] for phase, limit in (('development', 80), ('diagnostics', 48), ('pilot', 200))}
    result = {'status': 'completed' if all(stages.values()) else 'partial', **stages,
        'historical_generations': 42, 'r1_generations': len(budget['attempts']),
        'known_total_generations': 42 + len(budget['attempts']), 'r1_phase_counts': counts,
        'r1_remaining_generations': remaining, 'r1_generation_seconds': budget['seconds'],
        'r1_remaining_seconds': max(0, 7200-budget['seconds']),
        'uncertain_generations': sum(a['status'] != 'completed' for a in budget['attempts']),
        'historical_formal_des': 26880, 'r1_formal_des': data.get('formal_des'),
        'r1_pilot_replay_des': replay.get('extra_audit_des', 0),
        'remote_paid_generations': 0, 'official_jev_accepted': False,
        'current_source': code_fingerprint(ROOT), 'git': git_state(ROOT)}
    save(root / 'phase2d-r1-status.json', result)
    save(destination / 'phase2d-r1-status.json', result)
    save(root / 'git-delivery.json', result['git'])
    save(root / 'final-source-fingerprint.json', result['current_source'])
    stage_lines = '\n'.join(f'| {key} | {str(result[key]).lower()} |' for key in ALL_GATES)
    failures = []
    for path in sorted(root.glob('*/failure-*.json')):
        value = read(path)
        failures.append(f"- `{path.relative_to(root).as_posix()}`：{value.get('error')}")
    model_lines = []
    for label, path in (('固定状态诊断', root / 'diagnostics/evaluation/by-scenario.json'),
                        ('720h闭环', root / 'pilot/by-scenario.json')):
        if path.exists():
            model_lines.append('\n' + label + '：\n')
            for row in read(path):
                if label == '固定状态诊断' and row.get('scope') != 'unique_holdout':
                    continue
                shown = {key: row[key] for key in ('scenario', 'method', 'states', 'runs', 'action_regret', 'regret_at_k',
                    'DSR', 'RCR', 'ASR', 'mandatory_shortfall_terminal', 'local_seconds', 'end_to_end_seconds') if key in row}
                model_lines.append('- `' + json.dumps(shown, ensure_ascii=False) + '`')
    body = f'''# 阶段2D-R1审计交付

本轮状态：**{result['status']}**。原2D结论仍为partial，42次历史真实生成、26880正式DES保持原账，不计入本轮成功。

| 阶段 | 已验证 |
| --- | --- |
{stage_lines}

F1修复CLI和科学报告两条checkpoint写入路径，均保留字典插入顺序；F2使用持久计算预约与审计身份，checkpoint前完成证据落盘。64→160恢复实例要求三账完整一致。未提交而不能确认的工作保留pending/uncertain，保守停止，不重选补账、不退额度、不声称exactly-once。为确定选中分支0，账本额外保存已计算候选的情景0物理产物；保存本身不新增DES。

F3使用显式event-anchor-binary64-v2、legal-weekly-request-v2与checkpoint schema2。legacy加载不迁移。31.3h及另外6个固定普通停点、多次新进程恢复严格核对公开累计、请求、候选、情景、缓存、命令和完整事件。独立反例与离线缓存控制明确标为synthetic，绝不算真实模型。跨版本核算与新基准见recovery-f3和PHASE2D_R1_ARITHMETIC.md。

普通测试实际数量：{engineering.get('ordinary_test_count', '尚未通过完整工程门')}；工程退出码：{engineering.get('exit_code', 'not_run/failed')}。旧34份2A/2B快照和18科学文件、旧60份2C快照和540科学文件按legacy复验；2C的540文件与既有regenerated产物比较，60快照重新恢复/继续，未冒充重新生成全部旧60闭环。旧20份真实v1响应另在禁网络新进程零推理回放，额外640 DES单独列账。

本轮新生成：{len(budget['attempts'])}，分阶段{counts}；已耗生成时间{budget['seconds']:.6f}秒。剩余额度{remaining}，剩余时间{result['r1_remaining_seconds']:.6f}秒；pending/uncertain={result['uncertain_generations']}。单次120秒，最多一次固定格式纠错，两模板共享campaign。远程/付费/TypeSafe生成均0，官方Jev未验收。

开发验收：{json.dumps(development, ensure_ascii=False)}

v2分数是0–100整数的未校准偏好，不是概率、成功率、confidence或长期Q；同分按原稳定候选ID排序。同分合法但不证明筛选能力。20次真实验收从独立attempt文件、原始响应和持久campaign复核，不能用v1、cache或fixture替代。

正式数据核算：{json.dumps(data, ensure_ascii=False)}

新留出固定seed40–43、24窗口来自8条轨迹；开发10–11和旧20–23均属已见，同新协议重建公开状态去重。跨集重复单列并排除独立效果汇总，不换seed。全部在线动作核验封存后才生成64共同参考情景。q_ref仅为当前offer短窗DSR+RCR，有限池MC max存在选择偏差；不报告正式p值、显著性、规则泛化或全局最优。

pilot核算：{json.dumps(pilot, ensure_ascii=False)}

禁网缓存回放：{json.dumps(replay, ensure_ascii=False)}

R1正式在线/参考DES上限34048，不是整个工程过程的全部DES；工程测试、兼容和额外重放见各自账本。采样GPU/内存最大值仅是外部采样，不是CUDA分配器精确峰值；无法测量项为null。能耗和货币成本未估算。原始响应保留load/prompt/eval/total duration与独立token数；真实生成与cache耗时分开。

{chr(10).join(model_lines) if model_lines else '正式效果数据尚未生成，筛选收益、闭环收益和额外时间是否值得均未得到本轮正式证据。缺失不是零效果。'}

{chr(10).join(failures) if failures else '本轮阶段失败记录见各分工验证目录；已修复的前置失败日志保留。'}

源码仍在G:\\sim-jev，未提交、推送、创建PR或tag。当前源码指纹：{result['current_source']['sha256']}。冻结科学源码身份以freeze.json为准；若未通过开发门则不会产生正式freeze。完整源码、Git状态、原始响应、预算、CSV/JSON、反例、测试日志及逐条SHA256清单一并进入审计ZIP。解压普通测试不依赖.git或外部历史runs；有真实证据的回放禁网，不冒充重新验证GPU。
'''
    (root / 'PHASE2D_R1_REPORT.zh-CN.md').write_text(body, encoding='utf-8')
    (destination / '阶段2D-R1审计报告.md').write_text(body, encoding='utf-8')
    (destination / '阶段2D-R1审计报告.txt').write_text(body, encoding='utf-8')
    return {key: value for key, value in result.items() if key not in ('current_source', 'git')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['engineering', 'package', 'report'])
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--archive', type=Path)
    parser.add_argument('--extract', type=Path)
    parser.add_argument('--report-dir', type=Path)
    args = parser.parse_args()
    if args.action == 'engineering':
        result = engineering(args.output.resolve())
    elif args.action == 'package':
        result = package(args.output.resolve(), args.archive.resolve(), args.extract.resolve())
    else:
        result = report(args.output.resolve(), args.report_dir.resolve())
    print(json.dumps({k: v for k, v in result.items() if k not in ('proofs', 'commands', 'legacy_v1')}, indent=2))


if __name__ == '__main__':
    main()
