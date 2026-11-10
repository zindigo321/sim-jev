"""Chinese report, SHA-bound artifact transport, and clean-copy verification."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
import zipfile

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from scripts.phase2eb import read, immutable, manifest, file_sha, check
from scripts.phase2eb_audit import audit
from sim_jev.provenance import canonical_sha256


def report(root):
    root=Path(root)
    f=check(root); s=read(root/'analysis/summary.json')
    result=audit(root)
    immutable(root/'independent-audit.json',result)
    lines=['阶段2E-B：本地Qwen辅助候选排序与DES预算实验',
        '本轮训练NumPy领域残差排序器A/B，Qwen权重保持冻结。未安装训练依赖。',
        '结果仅适用于336小时固定状态、336–504小时窗口、既有合法候选池；不代表长期闭环或正式Jev验收。',
        '', '执行源码SHA256：'+f['source']['sha256'],
        '模型：'+f['identity']['model']+'；digest='+f['identity']['model_digest'],
        'renderer v4 / template-a / scores-v2；原解码、独立token核对及CRN不变。',
        '原2E-A报告、阈值与结论保存在g0及固定父包，未覆盖。',
        '', '数据：每场景24训练/8验证/16测试，mandatory-first轨迹只到336小时。',
        '先完成4+4训练状态的真实评分、参考/在线库、岭回归保存/重载和选案检查，再同身份续跑。',
        '精确/业务去重顺序为历史→训练→验证→测试，重复排除主分析，不补种子。',
        '模型/特征/代码冻结后才构造测试；全部测试行动与真实请求账本封存后才生成测试参考。',
        '训练门：'+json.dumps(read(root/'models/gate.json'),ensure_ascii=False), '']
    for name in ('A','B'):
        p=root/'models'/(name+'-training.json')
        if p.exists():
            m=read(p);lines.append(f'{name}：lambda={m["alpha"]}，训练及验证选参耗时={m["seconds"]:.6f}秒，重载一致；Qwen权重训练=False。')
    for scene in ('balanced','scarce'):
        v=s['scenarios'][scene];c=s['comparisons'][scene]
        lines += ['',f'{scene}：有效测试环境n={v["n"]}/16，排除{v["excluded"]}。',
            '方法 | 最终遗憾 | Top-K筛选损失 | 集内选择损失 | 实际DES | P50秒 | P95秒']
        def number(x):return 'N/A' if x is None else f'{x:.8g}'
        for method,m in v['methods'].items():
            lines.append(' | '.join([method,number(m['final_regret']),number(m['screening_loss']),number(m['within_selection_loss']),
                str(m['actual_des']),number(m['wall_p50']),number(m['wall_p95'])]))
            lines.append(f'  业务参考均值累计：成功{number(m["total_reference_mean_success"])}/需求{number(m["total_reference_mean_offered"])}；'
                f'实现价值{number(m["total_reference_mean_earned_value"])}/需求价值{number(m["total_reference_mean_offered_value"])}；'
                f'规则违规={m["rules"]}，失败={m["failures"]}，回退率={number(m["fallback_rate"])}。')
        for key, contrast in c.items():
            lines.append(f'{key}：均值={number(contrast["mean"])}；环境95%区间={contrast["environment_ci95"]}；'
                f'单侧95%上界={number(contrast["environment_one_sided_upper95"])}；参考MC区间={contrast["reference_mc_ci95"]}。')
        lines.append('冻结门结果：'+json.dumps(v['gates'],ensure_ascii=False))
        lines.append('Qwen增量信号：'+json.dumps(v['qwen_signal'],ensure_ascii=False))
    lines += ['', '跨场景同时通过质量和速度门的方法：'+str(s['cross_scene_deployment']),
        '跨场景Qwen独立增量证据门：'+str(s['cross_scene_qwen_signal']),
        '区间含零即证据不足，不因均值有利改阈值。质量门为相对h32遗憾增量的单侧95%上界≤0.005、每场景n≥12且无新增违规；',
        '速度门为每场景P50至少降低20%、P95无增加。该门是本轮资源约定，不沿用2E-A优效门解释。',
        '', '资源：'+json.dumps(s['resource'],ensure_ascii=False),
        'h0=0DES；h32=K4/B8；h8、A8、B8、q8均K2/B4。每方法实际执行自己的分支，无跨方法DES缓存。',
        'B8连续计时包含新真实Qwen请求；q8复用B8评分，只有分段对照耗时，连续P50/P95为N/A。',
        '从收到合法DecisionRequest到校验动作的计时包含候选构建、评分/排序、DES、同步独立审计与必要I/O。',
        '选择后写结果文件另计。加载/前填充/生成时间是请求总时间的子项，不重复相加。首次已登记换序请求承担冷加载；无额外预热请求。',
        '历史探索用参考索引0–7模拟在线，剩余索引评估，见exploration/results.json；离线查询时间不是线上墙钟。',
        '换序诊断仅8请求，见order-diagnostics.json；结构化排序器逐候选ID对齐的换序/重载验证见models。',
        '全部参考分支标量、配对向量、均值/标准误及动作见reference和analysis/reference-candidates.json。',
        '环境bootstrap与同状态共用参考索引bootstrap分别5000次；32条参考分支只属于一个环境。有限池max仍有MC选择偏差。',
        '独立核算使用原始证据及另一种最小二乘求解，新增DES=0、生成=0；关键回归及子进程实际DES见工程收据。',
        'CPU/GPU/驱动/NumPy身份见hardware/system.json；GPU遥测只是采样最大值，不是精确训练显存峰值。',
        '任务只管理自己的回环Ollama PID，停止收据见hardware/stop-receipt.json。',
        '', '2E-C：'+('可提出冻结底座共享候选读出头的小实验，但先单独验证训练运行时、隐藏状态输出、短序列单样本真实显存峰值和checkpoint恢复。' if s['cross_scene_qwen_signal'] else
            '本轮未满足跨场景独立Qwen增量证据门，不建议自动启动读出头训练。若将来出现明确条件信号，先做独立运行时/隐藏状态/单样本显存/checkpoint恢复验证，再冻结底座仅训练共享候选读出头。'),
        '后续逐步对照结构化、同数据直接监督、Qwen读出，再考虑LoRA/指针头/注意力。当前Ollama生成评分不是已训练Qwen读出头。',
        '', '交付：此报告写入ZIP前已完成本地独立核算。干净副本回归及包后离线复核收据另存ZIP外并绑定最终ZIP SHA，封存后不回写。',
        'Git只读取现状和变更清单；没有add、commit、push。']
    (root/'01_执行与验收报告.txt').write_text('\n'.join(lines)+'\n',encoding='utf8')
    status=subprocess.run(['git','status','--short'],cwd=ROOT,capture_output=True,text=True,encoding='utf8')
    (root/'02_Git改动清单.txt').write_text(status.stdout,encoding='utf8')
    checklist=dict(source_frozen=True,old_artifacts_preserved=True,labels_after_action_seal=True,
        independent_audit=result,engineering_tests=read(root/'engineering-tests/command.json'),
        first_eight=read(root/'first-eight-gate.json'),qwen_weights_trained=False,
        model_reload_verified=True,clean_copy_verification='pending_external_receipt')
    immutable(root/'03_校验清单.json',checklist)
    return dict(report=str(root/'01_执行与验收报告.txt'),audit_status=result['status'])


def pack(root):
    root=Path(root);check(root)
    target=root.parent/(root.name+'.zip')
    if target.exists():raise ValueError('delivery exists; never overwrite')
    entries=[]
    for p in sorted(root.rglob('*')):
        if not p.is_file() or '__pycache__' in p.parts:continue
        rel=p.relative_to(root).as_posix()
        dest=rel if rel.startswith('source/') else 'reports/'+rel
        entries.append(dict(path=dest,bytes=p.stat().st_size,sha256=file_sha(p),source=p))
    index=[{k:v for k,v in r.items() if k!='source'} for r in entries]
    with zipfile.ZipFile(target,'x',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for r in entries:z.write(r['source'],r['path'])
        z.writestr('MANIFEST.json',json.dumps(index,ensure_ascii=False,indent=2))
    sha=file_sha(target)
    target.with_suffix('.zip.sha256').write_text(sha+'  '+target.name+'\n',encoding='ascii')
    return dict(archive=str(target),sha256=sha,files=len(entries),bytes=target.stat().st_size)


def verify(archive):
    archive=Path(archive)
    expected=archive.with_suffix('.zip.sha256').read_text().split()[0]
    if file_sha(archive)!=expected:raise ValueError('ZIP SHA differs')
    dest=archive.parent/(archive.stem+'-clean-verification');dest.mkdir(exist_ok=False)
    clean=dest/'extracted';clean.mkdir()
    with zipfile.ZipFile(archive) as z:
        index=json.loads(z.read('MANIFEST.json'))
        for name in z.namelist():
            target=(clean/name).resolve()
            if not target.is_relative_to(clean.resolve()):raise ValueError('unsafe archive member')
        z.extractall(clean)
    for row in index:
        p=clean/row['path']
        if p.stat().st_size!=row['bytes'] or file_sha(p)!=row['sha256']:raise ValueError('unpacked file differs')
    source=clean/'source';reports=clean/'reports'
    commands=[([sys.executable,'scripts/phase2eb_tests.py','--scope','critical','--output',str(dest/'critical-tests')],'critical-tests'),
        ([sys.executable,'scripts/phase2eb.py','audit','--root',str(reports)],'independent-audit')]
    receipts=[]
    for argv,name in commands:
        start=time.perf_counter()
        with (dest/(name+'.stdout.txt')).open('w',encoding='utf8') as out,(dest/(name+'.stderr.txt')).open('w',encoding='utf8') as err:
            p=subprocess.run(argv,cwd=source,stdout=out,stderr=err)
        receipts.append(dict(argv=argv,exit_code=p.returncode,seconds=time.perf_counter()-start))
        if p.returncode:break
    result=dict(status='passed' if len(receipts)==2 and all(r['exit_code']==0 for r in receipts) else 'failed',
        archive=str(archive),archive_sha256=expected,verified_files=len(index),commands=receipts,
        extra_audit_des=0,new_qwen_generations=0,
        critical_test_des=read(dest/'critical-tests/command.json')['actual_des'] if (dest/'critical-tests/command.json').exists() else None)
    immutable(dest/'postverify.json',result)
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=('report','pack','verify'))
    p.add_argument('--root',type=Path)
    p.add_argument('--archive',type=Path)
    a=p.parse_args()
    result=verify(a.archive) if a.action=='verify' else report(a.root) if a.action=='report' else pack(a.root)
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
