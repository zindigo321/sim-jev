"""Chinese findings and a byte-verified, self-contained audit transport."""
import csv
import json
from pathlib import Path
import subprocess
import time
import zipfile
from scripts.phase2ec2_common import *


def report(root):
    root=Path(root); r=read(root/'analysis/results.json'); g=read(root/'gate.json'); probe=read(root/'probe/result.json')
    audit=read(root/'audits/independent.json'); validation=read(root/'online-validation.json')
    controls=[read(root/'probe'/s['state_id']/'mechanism.json') for s in read(root/'development/states.json')]
    lines_out=['阶段 2E-C2：P8 换序与低预算 DES 重排的冻结验证','',
        '本批是冻结 P11/A8、336–504h 单窗口的配对研究。本地 Qwen 候选头不是官方 Jev 服务验收。',
        f'计划 128 个新 scarce 状态；完整主配对 n={r["n"]}，排除 {len(r["excluded"])}。样本不补种子。',
        f'targeted_training_eligible = {g["targeted_training_eligible"]}；pilot_science_gate = {g["pilot_science_gate"]}。',
        f'闭环准备：{g["closed_loop_readiness"]["status"]}，本批未执行 720h。工程审计：{audit["status"]}。',
        '', '一、候选换序为何改变分数',
        f'4 个预登记旧 train/validation scarce 状态，真实重复同序、前缀互换及后缀控制。超过同输入实测抖动的状态数 {probe["mechanism_states"]}/4。']
    for c in controls:
        p=c['mixed']['prefix']; s=c['mixed']['suffix']
        lines_out.append(f'{c["state_id"]}：同序分数抖动最大 {c["same_input_score_jitter_max"]:.8g}；'
            f'前缀候选侧最大效应 {max(abs(x) for x in p["candidate_effect"]):.8g}，尾部侧 {max(abs(x) for x in p["tail_effect"]):.8g}；'
            f'后缀固定候选向量差 {c["suffix_target_vector_linf"]:.8g}；18字段量化别名组 {len(c["quantized_alias_groups"])}。')
    lines_out += ['稳定 ID 只用于模型外对齐与平手；规范化入口仅消除上游排列，诊断确实改变 token 行顺序。',
        '每次前向的 token、marker 位置、ID 映射、候选向量和尾部向量均保留。混搭读出只用于定位，不构成线上方法。',
        '候选侧、尾部侧与交互项见 probe/*/mechanism.json 和 permutation/*/*/alignment.json；分数近并列与精确参考同分分别记账。']
    for tag,s in r['permutation'].items():
        c=s['penalties']['P8-G']
        lines_out.append(f'{tag}：n={s["n"]}，Top-2集合一致率={s["top2_consistency"]}，ID全对齐={s["ID_all_aligned"]}；'
            f'P8-G原序减换序 Q 均值={c.get("mean")}，环境单侧95%上界={c.get("environment_upper95")}，参考MC上界={c.get("reference_mc_upper95")}。')
    lines_out += ['', '二、B4 在哪些情况下把好候选换掉',
        '这里的帮助/伤害是独立 R64 点估计，逐环境参考 MC 区间单列。参考本身仍有估计误差。']
    for method,s in r['rerank'].items():
        lines_out.append(f'{method}：原B4换离排序第一名 {s["switched"]}/{s["n"]}；帮助 {s["help"]}，伤害 {s["harm"]}，参考精确同分 {s["tie"]}。')
        for budget,v in s['budgets'].items():
            lines_out.append(f'  {budget}：诊断n={v["n"]}，换离Top1比例={v["switch_rate"]}，集内选择损失={v["selection_loss"]}，Q={v["Q"]}。')
    lines_out += ['独立诊断 seed 的16场景被切为4个互不重叠B4，并用前8/全部16场景重选。候选保留集固定，未把B扫描升级为正式方法。',
        '在线B4差值、R64差值、严格平手、参考MC区间及每次诊断选案见 RERANK_BY_ENVIRONMENT.csv。',
        '增加预算后仍有损失时，保留集质量与参考不确定性共同存在；不能据此归因为单一噪声机制。',
        '', '三、固定 guard 的效果与 A8 公平对照',
        'Guard仅在4个配对差至少3个严格正且均值≥1/500时换选第二名。正负和阈值用业务分子/分母的精确有理数；3/4不表示统计显著。',
        '六方法独立建池、独立真实DES；P8与P8-G各一次全池前向。P0/A0只取正式排序Top1，无独立时延。',
        '方法 | Q(R64) | R32筛选损失 | R32集内损失 | R32总遗憾 | 连续P50/P95秒']
    for m,s in r['aggregate'].items():
        lines_out.append(f'{m} | {s["Q"]:.6f} | {s["R32_screening_loss"]:.6f} | {s["R32_within_selection_loss"]:.6f} | '
            f'{s["R32_total_regret"]:.6f} | {s["p50"]}/{s["p95"]}')
    for name in ('P8-G__P8','A8-G__A8','P8__A8','P8-G__A8-G','P0__A0','P8-G__A8','P8-G__H42','H32__P8-G'):
        s=r['contrasts'][name]
        lines_out.append(f'{name}（前者减后者）：均值 {s["mean"]:.6f}；环境双侧95% {s["environment_ci95"]}；'
            f'参考MC双侧95% {s["reference_mc_ci95"]}；精确胜/平/负 {s["exact_wins"]}/{s["exact_ties"]}/{s["exact_losses"]}；动作不同 {s["different_actions"]}。')
    lines_out += [f'P8-G相对A8实测时延差：{r["latency_P8G_minus_A8"]}。',
        '环境 bootstrap 5000次完整配对重采样；参考MC固定环境、环境内共同索引配对重采样5000次。两类区间分开。',
        '换序分别报告reverse/random，并另给按环境聚类的两变体平均惩罚；不将候选或分支当独立环境。',
        'R32全池最大值仅用于有限池损失分解；R64追加名单在参考前由动作决定，绝不以R64不齐全池求最大值或重新选案。',
        '', '四、什么证据允许训练或申请720h',
        '唯一正式候选是P8-G。以下是冻结门的逐项实际值；阈值、分母与失败原因也在gate.json。']
    for c in g['checks']:
        lines_out.append(f'{c["group"]}/{c["name"]}: {c["value"]} {c["operator"]} {c["threshold"]}; n={c["denominator"]}; '
            f'{"通过" if c["passed"] else "未通过"}; {c["failure_reason"] or ""}')
    if g['targeted_training_eligible']:
        lines_out += ['训练门通过：下一批可预登记单因素头训练小试；冻结0.8B底座，旧train/val分组，用同四缓存视图、同架构、同数学平票标签，仅增加按ID一致性损失。',
            '先验证Top2换序一致率≥90%、相对同标签对照遗憾增量≤.002、soft NLL增量≤.02；两个预设初始化分别报告，再用独立新留出确认。']
    else:
        lines_out += ['训练门未通过：本批不提出可执行的新训练试验。守门收益、仅胜H42或P0/A0线索均不能替代正式训练门。']
    lines_out += ['720h科学门与工程准备分开。启动前仍须真实验证策略序列化、正常周入口、checkpoint恢复动作与账本、672–720h尾窗结算，并冻结新种子、执行CRN、日执行器及初始工况。',
        'C2留出不得用于训练、选模、校准或下一批盲测。任何换头、输入或选择规则的新版本都须另用新留出过科学门。',
        '本批到此交付；无训练、注意力改造、720h、期末价值、PPO或Git提交发布。',
        '', '五、预算、真实性与审计',
        json.dumps(r['counts'],ensure_ascii=False),
        f'前向分类 {r["forward_counts"]}；累计 {r["forward_seconds"]:.3f}秒；研究DES {audit["research_des"]}。',
        'optimizer更新=0，自回归生成=0，远程推理=0，官方Jev请求=0。失败与未完成预约不会重置预算。',
        f'独立科学重叠校验 {validation["overlap_comparisons"]} 对；回退/漂移/科学不一致问题 {len(validation["failures"])}。',
        f'开发耗时探针：每DES计算+审计均值 {probe["des_compute_audit_seconds_mean"]:.4f}秒；调度推算参考上限 {probe["reference_upper_seconds_estimate"]/60:.1f}分钟。',
        '连续计时从合法请求进入到命令校验、选择证据fsync完成，包含建池、tokenize、GPU同步、读出、DES、审计和I/O；自报耗时文件写入在计时之后。',
        '冷加载见loads/和probe/load.json；显存和token见forwards/，硬件采样见hardware/。Windows进程显存若N/A不作专属显存测量。',
        f'独立只读复算：{audit["complete_environments"]}环境、{audit["audited_raw_branches"]}研究分支、{audit["gate_checks"]}门项；新增DES/前向均为0。',
        '早期prepare尚未完成时的probe调用在计算前退出，收据保留于failures/；它未产生研究样本、DES或前向。其余实际失败也保留。',
        '模型底座和Conda为外部依赖，不重复封装。历史C1/B文件、Git索引及已有工作保持原字节。',
        '', '复核入口：scripts/phase2ec2_audit_readonly.py <交付目录>（D:/conda_envs/sim-jev/python.exe）。',
        '实际命令、解释器与耗时：commands.jsonl。阶段入口：scripts/phase2ec2.py；预登记：registration.json与PREREGISTRATION.zh-CN.txt。']
    write_text(root/'REPORT.zh-CN.txt','\n'.join(lines_out)+'\n')


def package(root):
    root=Path(root); check(root,action=True)
    if read(root/'audits/independent.json')['status']!='passed': raise ValueError('independent audit required')
    # Preserve every file present before C2 started, including existing untracked source.
    initial=read(root/'initial-source.json')
    for row in initial['files']:
        if 'phase2ec2' in row['path']: continue
        if sha(ROOT/row['path'])!=row['sha256']: raise ValueError('pre-existing source changed: '+row['path'])
    for name,args in [('status',['status','--porcelain=v1','--untracked-files=all']),('index',['diff','--cached','--binary']),('head',['rev-parse','HEAD'])]:
        p=subprocess.run(['git',*args],cwd=ROOT,capture_output=True,check=True)
        (root/('final-git-'+name+'.txt')).write_bytes(p.stdout)
        if name in ('index','head') and p.stdout!=(root/('initial-git-'+name+'.txt')).read_bytes(): raise ValueError('Git history/index changed')
    if not (root/'REPORT.zh-CN.txt').exists(): report(root)
    write(root/'external-dependencies.json',dict(model='G:/sim-jev-models/Qwen3.5-0.8B-Base-dc7cdfe2ee4154fa7e30f5b51ca41bfa40174e68',
        online_interpreter='D:/conda_envs/cgfedllm/python.exe',audit_interpreter='D:/conda_envs/sim-jev/python.exe',
        parent_archive=str(PARENT),parent_sha256=PARENT_SHA,environment_manifest='environment-prepare.json'))
    rows=manifest(root); write(root/'DELIVERY-MANIFEST.json',rows); archive=root.with_suffix('.zip')
    with zipfile.ZipFile(archive,'x',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for r in rows: z.write(root/r['path'],r['path'])
        z.write(root/'DELIVERY-MANIFEST.json','DELIVERY-MANIFEST.json')
    digest=sha(archive)
    import hashlib
    with zipfile.ZipFile(archive) as z:
        loaded=json.loads(z.read('DELIVERY-MANIFEST.json'))
        if loaded!=rows or set(z.namelist())!={r['path'] for r in rows}|{'DELIVERY-MANIFEST.json'}: raise ValueError('ZIP member set mismatch')
        for r in loaded:
            data=z.read(r['path'])
            if len(data)!=r['bytes'] or hashlib.sha256(data).hexdigest()!=r['sha256']: raise ValueError('ZIP reread mismatch: '+r['path'])
    if sha(archive)!=digest: raise ValueError('ZIP changed during verification')
    write_text(archive.with_suffix('.zip.sha256'),digest+'  '+archive.name+'\n')
    receipt=dict(zip=str(archive),sha256=digest,bytes=archive.stat().st_size,members_verified=len(rows)+1,
        every_member_reread=True,source_preserved=True,git_head_and_index_preserved=True)
    write(archive.with_suffix('.zip.receipt.json'),receipt)
    return receipt
