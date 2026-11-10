"""Offline paired statistics and independent raw-evidence reconciliation."""
from collections import Counter
from pathlib import Path
import itertools
import json
import math
import shutil
import subprocess
import time
import zipfile
import numpy as np
from scripts.phase2ec1_common import (ROOT,read,write,sha,manifest,verify_files,lines,
                                     Journal,canonical_sha256,LIMITS)
from scripts.phase2d_local import unpack
from scripts.phase2e_diagnostic import request_pool_identity
from sim_jev.residual_ranker import ranked_ids,predict


def branch_matrix(root,state,folder='reference',seed=None,count=32,retained=None):
    """Independently reconstruct DSR+RCR from business numerators/denominators."""
    req,pool=unpack(state)
    by={c.id:c for c in pool}
    ids=retained if retained is not None else [c.id for c in pool]
    rows=lines(Path(root)/folder/'branches.jsonl')
    lookup={}
    legal_hash=canonical_sha256(req.to_dict())
    scenario_ids={j:'planning-'+canonical_sha256(dict(legal_input_hash=legal_hash,planning_seed=seed,scenario_index=j)) for j in range(count)}
    offered=[m for m in req.observation.missions if req.context.window_start<=m['start_time']<req.context.window_end]
    for row in rows:
        cid,j=row['candidate_id'],row['scenario_index']
        assert (cid,j) not in lookup and cid in ids and 0<=j<count
        c=row['counts']
        assert c['offered']==len(offered) and c['offered_value']==math.fsum(m['value'] for m in offered)
        assert c['accepted']==len(by[cid].command.accepted_ids)
        assert c['accepted']+c['rejected']==c['offered']
        assert sum(c[k] for k in ('success','failed','accepted_not_executed','pending_execution','running'))==c['accepted']
        assert c['unfinished']==c['pending_execution']+c['running']
        for ratio,num,den in [('DSR','success','offered'),('RCR','earned_value','offered_value'),('ASR','success','accepted')]:
            expected=c[num]/c[den] if c[den] else None
            assert c[ratio]==expected and c['validity'][ratio]==bool(c[den])
        score=(c['DSR'] or 0)+(c['RCR'] or 0)
        assert row['score']==score and row['command']==by[cid].command.to_dict()
        assert row['legal_input_hash']==legal_hash and row['scenario_id']==scenario_ids[j]
        assert row['origin']==336 and row['window_end']==504 and row['duration_hours']==168
        assert row['audit']['verified'] and row['audit']['score']==score
        lookup[cid,j]=score
    assert set(lookup)=={(cid,j) for cid in ids for j in range(count)}
    return np.array([[lookup[cid,j] for j in range(count)] for cid in ids]),rows


def paired(matrix,env_indices,mc_indices):
    means=matrix.mean(axis=1)
    env=means[env_indices].mean(axis=1)
    mc=np.zeros(len(env))
    for i in range(len(matrix)):
        mc+=matrix[i][mc_indices[i]].mean(axis=1)/len(matrix)
    return dict(n=len(matrix),mean=float(means.mean()),
        environment_ci95=np.quantile(env,[.025,.975]).tolist(),environment_lower95=float(np.quantile(env,.05)),
        environment_upper95=float(np.quantile(env,.95)),reference_mc_ci95=np.quantile(mc,[.025,.975]).tolist(),
        reference_mc_lower95=float(np.quantile(mc,.05)),reference_mc_upper95=float(np.quantile(mc,.95)),
        wins=int((means>0).sum()),ties=int((means==0).sum()),losses=int((means<0).sum()))


def compute(root):
    root=Path(root)
    reg=read(root/'registration.json')
    actions=read(root/'test/actions.json')
    refs={r['state_id']:r for r in read(root/'reference-summary.json')}
    methods=reg['methods']
    valid,excluded,metrics,by={},[],[],{}
    states=[]
    for plan in reg['planned']:
        sid=plan['state_id']
        path=root/'states'/sid/'state.json'
        group={a['method']:a for a in actions if a['state_id']==sid and a['status']=='completed'}
        reason=[]
        if not path.exists():reason.append('construction_failed')
        else:
            state=read(path)
            if not state['primary_eligible']:reason.append('historical_or_batch_duplicate')
            if state['actual_M']<4:reason.append('M_lt_4')
        if set(group)!=set(methods):reason.append('incomplete_method_pairing')
        if refs.get(sid,{}).get('status')!='completed':reason.append('reference_failed')
        if reason:
            excluded.append(dict(state_id=sid,reasons=reason))
            continue
        states.append(state)
        matrix,raw=branch_matrix(root,state,'reference/'+sid,reg['reference_seed'])
        ids=[c['id'] for c in state['pool']]
        index={cid:i for i,cid in enumerate(ids)}
        means={cid:math.fsum(matrix[i])/32 for cid,i in index.items()}
        for method,a in group.items():
            selected=a['selected_candidate_id']
            top=max(means[cid] for cid in a['retained_ids'])
            chosen=[r for r in raw if r['candidate_id']==selected]
            business={k:float(np.mean([r['counts'][k] for r in chosen])) for k in
                      ('offered','accepted','rejected','success','failed','accepted_not_executed','unfinished',
                       'offered_value','accepted_value','earned_value','mandatory_shortfall_as_of','DSR','RCR')}
            fleet=state['request']['parameters']['fleet_size']
            business['availability']=float(np.mean([r['metrics_delta']['available_aircraft_hours']/(fleet*168) for r in chosen]))
            metrics.append(dict(state_id=sid,method=method,selected=selected,screening_loss=max(means.values())-top,
                within_selection_loss=top-means[selected],final_regret=max(means.values())-means[selected],
                selected_reference_score=means[selected],top1_regret=max(means.values())-means[a['ranking_ids'][0]],
                continuous_seconds=a['continuous_wall_seconds'],token_count=a['token_count'],forward_seconds=a['forward_seconds'],
                forward_calls=a['forward_calls'],actual_des=a['actual_des'],business=business))
            by[sid,method]=matrix[index[selected]]
        valid[sid]=dict(state=state,matrix=matrix,ids=ids,index=index,actions=group,means=means)
    n=len(states)
    aggregates={}
    for method in methods:
        rows=[m for m in metrics if m['method']==method]
        if not rows:continue
        aggregate={k:float(np.mean([r[k] for r in rows])) for k in
                   ('screening_loss','within_selection_loss','final_regret','top1_regret','selected_reference_score','forward_seconds','token_count')}
        aggregate.update(n=len(rows),p50=float(np.median([r['continuous_seconds'] for r in rows])),
            p95=float(np.quantile([r['continuous_seconds'] for r in rows],.95)),
            business={k:float(np.mean([r['business'][k] for r in rows])) for k in rows[0]['business']})
        aggregates[method]=aggregate
    contrasts={}
    resampling={}
    if n:
        erng=np.random.default_rng(reg['environment_bootstrap_seed'])
        mrng=np.random.default_rng(reg['mc_bootstrap_seed'])
        ei=erng.integers(0,n,(5000,n))
        mi=[mrng.integers(0,32,(5000,32)) for _ in range(n)]
        resampling=dict(environment_indices_sha256=canonical_sha256(ei.tolist()),
                        mc_indices_sha256=canonical_sha256([canonical_sha256(x.tolist()) for x in mi]))
        for first,second in itertools.combinations(methods,2):
            matrix=np.array([by[s['state_id'],first]-by[s['state_id'],second] for s in states])
            item=paired(matrix,ei,mi)
            item['different_actions']=sum(valid[s['state_id']]['actions'][first]['selected_candidate_id'] !=
                                          valid[s['state_id']]['actions'][second]['selected_candidate_id'] for s in states)
            contrasts[first+'-'+second]=item
        improvements={baseline:paired(np.array([by[s['state_id'],'P8']-by[s['state_id'],baseline] for s in states]),ei,mi)
                      for baseline in ('S8','A8','H42')}
        h32=paired(np.array([by[s['state_id'],'H32']-by[s['state_id'],'P8'] for s in states]),ei,mi)
    else:
        improvements,h32={},{}
    perm_rows=[]
    rng=np.random.default_rng(reg['mc_bootstrap_seed'])
    for p in read(root/'permutation/actions.json'):
        sid=p['state_id']
        if p['status']!='completed' or sid not in valid:continue
        v=valid[sid]
        a=v['actions']['P8']
        assert set(a['predictions'])==set(p['predictions'])==set(v['ids'])
        delta=v['matrix'][v['index'][a['selected_candidate_id']]]-v['matrix'][v['index'][p['selected_candidate_id']]]
        boot=delta[rng.integers(0,32,(5000,32))].mean(axis=1)
        perm_rows.append(dict(state_id=sid,tag=p['tag'],top2_equal=set(a['retained_ids'])==set(p['retained_ids']),
            action_equal=a['selected_candidate_id']==p['selected_candidate_id'],
            score_changes={cid:p['predictions'][cid]-a['predictions'][cid] for cid in v['ids']},
            reference_loss_change=float(delta.mean()),reference_mc_ci95=np.quantile(boot,[.025,.975]).tolist(),
            baseline_action=a['selected_candidate_id'],permuted_action=p['selected_candidate_id'],id_alignment_valid=True,
            actual_des=p['actual_des'],continuous_seconds=p['continuous_wall_seconds']))
    permutation_rate=float(np.mean([r['top2_equal'] for r in perm_rows])) if perm_rows else None
    gates=dict(sample=n>=48,quality_increment=bool(improvements) and all(
        r['mean']>=.002 and r['environment_lower95']>0 and r['reference_mc_lower95']>0 for r in improvements.values()),
        h32_noninferiority=bool(h32) and h32['environment_upper95']<=.005,
        p50=bool(aggregates) and aggregates['P8']['p50']<=.8*aggregates['H32']['p50'],
        p95=bool(aggregates) and aggregates['P8']['p95']<=aggregates['H32']['p95'],
        permutation=len(perm_rows)>=24 and permutation_rate>=.9,
        legality=all(a.get('rule_violations')==0 for a in actions if a['status']=='completed') and
                 not any(a['status']!='completed' for a in actions))
    gates['expand']=all(gates.values())
    return dict(planned=64,n=n,excluded=excluded,aggregates=aggregates,contrasts=contrasts,
        P8_improvement=improvements,P8_extra_regret_vs_H32=h32,permutation_pairs=len(perm_rows),
        permutation_top2_consistency=permutation_rate,gates=gates,resampling=resampling,
        metrics=metrics,permutation_diagnostics=perm_rows)


def analyze(root):
    root=Path(root)
    value=compute(root)
    write(root/'analysis/results.json',value)
    return {k:v for k,v in value.items() if k not in ('metrics','permutation_diagnostics','resampling')}


def audit(root):
    """Read only all scientific evidence; no new model calls or simulations."""
    if not __debug__:
        raise RuntimeError('audit requires normal Python; optimized mode is forbidden')
    from scripts.planning_verify import audit_planning_branch
    from sim_jev.candidates import generate_candidates,heuristic_ranking
    from sim_jev.weekly_policy import validate_command
    from sim_jev.provenance import code_fingerprint
    root=Path(root)
    started=time.perf_counter()
    reg=read(root/'registration.json')
    for name in ('model-seal.json','online-action-seal.json'):
        seal=read(root/name)
        assert canonical_sha256(seal['files'])==seal['files_sha256']
        verify_files(root,seal['files'])
    assert code_fingerprint(ROOT)==read(root/'model-seal.json')['source']
    entries=Journal(root).entries
    assert all(e['status']!='pending' for e in entries.values()),'uncertain compute remains reserved'
    assert all(e['status']=='completed' for e in entries.values()
               if e['phase']!='engineering_des'),'non-engineering computation failed'
    assert sum(e.get('seconds',0) for e in entries.values() if e['phase']=='forward')<=3600
    assert min(e['unix'] for e in entries.values() if e['phase']=='reference')>read(root/'online-action-seal.json')['sealed_unix']
    assert min(e['unix'] for e in entries.values() if e['phase']=='trajectory')>read(root/'model-seal.json')['sealed_unix']
    # Independently replay distribution construction from original scalar evidence.
    rng=np.random.default_rng(reg['target_seed'])
    data=read(root/'data/dataset.json')
    parent=read(root/'parent/freeze.json')
    labels_checked=0
    for d in data:
        s=d['state']
        matrix,_=branch_matrix(root,s,'reused/reference/'+s['state_id'],parent['reference_seed'])
        indices=rng.integers(0,32,(512,32))
        votes=np.zeros(len(matrix))
        for draw in indices:
            means=np.array([row[draw].mean() for row in matrix])
            wins=np.flatnonzero(means==means.max())
            votes[wins]+=1/len(wins)/512
        expected=.98*votes+.02/len(matrix)
        np.testing.assert_allclose(expected,d['labels']['target'],rtol=0,atol=2e-15)
        assert canonical_sha256(indices.tolist())==d['labels']['bootstrap_indices_sha256']
        assert canonical_sha256(d['labels'])==d['label_sha256']
        assert sha(root/'reused/reference'/s['state_id']/'branches.jsonl')==d['labels']['branch_sha256']
        labels_checked+=len(matrix)*32
    history=(read(root/'history/seen-public.json')+read(root/'history/supplement-public.json')+
             read(root/'history/supplement-completion.json'))
    exact={s['exact_input_sha256'] for s in history}
    business={s['business_input_sha256'] for s in history}
    actionmap={(a['state_id'],a['method']):a for a in read(root/'test/actions.json')}
    pairrows={(a['state_id'],a['tag']):a for a in read(root/'permutation/actions.json')}
    observed=set()
    raw_des=physical=0
    for plan in reg['planned']:
        sid=plan['state_id']
        s=read(root/'states'/sid/'state.json')
        a,b=request_pool_identity(s['request'],s['pool'])
        assert (a,b)==(s['exact_input_sha256'],s['business_input_sha256'])
        assert s['primary_eligible']==(a not in exact and b not in business)
        exact.add(a);business.add(b)
        req,pool=unpack(s)
        assert [c.to_dict() for c in sorted(generate_candidates(req,16,20261102),key=lambda c:c.id)]==s['pool']
        groups=[('test/'+sid+'/'+m,actionmap[sid,m]) for m in reg['methods']]
        groups += [('permutation/'+sid+'/'+tag,p) for (stateid,tag),p in pairrows.items() if stateid==sid]
        shared={}
        for folder,action in groups:
            assert action['status']=='completed'
            kept=action['retained_ids']
            _,rows=branch_matrix(root,s,folder,reg['online_seed'],action['B'],kept if action['B'] else [])
            values={cid:math.fsum(r['score'] for r in rows if r['candidate_id']==cid)/action['B'] for cid in kept} if rows else {}
            chosen=min(values,key=lambda cid:(-values[cid],cid)) if values else (action['ranking_ids'][0] if pool else None)
            assert chosen==action['selected_candidate_id']
            command=next(c.command for c in pool if c.id==chosen) if chosen else None
            if command:
                assert command.to_dict()==action['command'] and validate_command(command,req.observation,req.context)==command
            if action['predictions'] is not None:
                assert ranked_ids(action['predictions'])==action['ranking_ids']
            if action['method']=='A8':assert predict(read(root/'models/A.json'),pool)==action['predictions']
            if action['method'] in ('H32','H42'):assert [c.id for c in heuristic_ranking(pool)]==action['ranking_ids']
            if action['method']=='P8':
                f=read(root/'forwards'/(folder+'.json'))
                assert f['actual_forward_calls']==action['forward_calls']==1 and not f['autoregressive_calls']
                assert f['encoded']['candidate_ids']==action['input_candidate_ids']
                assert set(f['encoded']['candidate_ids'])=={c.id for c in pool}
                assert len(f['encoded']['input_ids'])==action['token_count']<=2048
            for r in rows:
                key=(r['candidate_id'],r['scenario_index'])
                if key in shared:assert shared[key]==r['score']
                shared[key]=r['score']
        refmatrix,refrows=branch_matrix(root,s,'reference/'+sid,reg['reference_seed'])
        groups.append(('reference/'+sid,{}))
        for folder,_ in groups:
            rows=lines(root/folder/'branches.jsonl')
            for r in rows:
                key=folder+'/'+r['candidate_id']+'/'+str(r['scenario_index'])
                assert key in entries and entries[key]['scalar_sha256']==canonical_sha256(r)
                observed.add(key);raw_des+=1
                if r['scenario_index']==0:
                    p=read(root/folder/('physical-'+r['candidate_id']+'-0.json'))
                    assert canonical_sha256(p['final_state'])==r['physical_artifact_sha256']
                    audit_planning_branch(p,req)
                    physical+=1
        print('audit',plan['index']+1,sid,raw_des,flush=True)
    expected={key for key,e in entries.items() if e['phase'] in ('online','permutation','reference')}
    assert expected==observed
    # Every forward call has a unique reservation and evidence, including probes.
    forwards=0
    for path in (root/'forwards').rglob('*.json'):
        f=read(path)
        key='forward/'+f['slot']
        assert entries[key]['scalar_sha256']==canonical_sha256(f)
        assert f['actual_forward_calls']==1 and not f['trainable_backbone_parameters'] and not f['backbone_gradients']
        forwards+=1
    assert forwards==sum(e['phase']=='forward' for e in entries.values())
    assert compute(root)==read(root/'analysis/results.json')
    process=subprocess.run(['D:/conda_envs/cgfedllm/python.exe',str(ROOT/'scripts/phase2ec1_head_audit.py'),str(root)],
        capture_output=True,text=True,encoding='utf8',errors='replace',
        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    if process.returncode:
        raise ValueError('fresh-process tensor audit failed: '+process.stderr)
    head_audit=json.loads(process.stdout)
    assert head_audit['status']=='passed'
    result=dict(status='passed',raw_research_des=raw_des,physical_samples=physical,
        reused_label_branches=labels_checked,actual_forwards=forwards,counts=dict(Counter(e['phase'] for e in entries.values())),
        label_barrier=True,all_seals_verified=True,new_des=0,new_forward=0,seconds=time.perf_counter()-started,
        source_sha256=code_fingerprint(ROOT)['sha256'],head_tensor_audit=head_audit)
    # Receipts are separate from sealed scientific evidence.
    ix=len(list((root/'audits').glob('audit-*.json')))
    write(root/'audits'/f'audit-{ix}.json',result)
    return result


def package(root):
    root=Path(root)
    files=manifest(root)
    write(root/'DELIVERY-MANIFEST.json',files)
    dest=root.with_suffix('.zip')
    with zipfile.ZipFile(dest,'x',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for row in files:
            z.write(root/row['path'],row['path'])
        z.write(root/'DELIVERY-MANIFEST.json','DELIVERY-MANIFEST.json')
    digest=sha(dest)
    with dest.with_suffix('.zip.sha256').open('x',encoding='ascii') as f:
        f.write(digest+'  '+dest.name+'\n')
    return dict(zip=str(dest),sha256=digest,files=len(files),external_dependencies='official pinned Qwen assets and existing Conda interpreters')


def report(root):
    import csv
    root=Path(root)
    r=read(root/'analysis/results.json')
    training=read(root/'training/summary.json')
    entries=Journal(root).entries
    counts=dict(Counter(e['phase'] for e in entries.values()))
    forwards=[read(p) for p in (root/'forwards').rglob('*.json')]
    peak=max(f['memory_after']['max_allocated'] for f in forwards if f['identity']['dtype']=='bfloat16')/1024**3
    forward_seconds=sum(e.get('seconds',0) for e in entries.values() if e['phase']=='forward')
    sections=[
        '阶段 2E-C1：本地 Qwen 一次前向候选决策头 v0',
        '实际运行日期：2026-11-10（Asia/Shanghai）。本地 Jev 式原型；非官方 Jev 验收或论文严格复现。',
        '',
        '一、交付与结论',
        f'官方 Qwen3.5-0.8B-Base 已真实加载并冻结；完成两种头×两个初始化种子的监督训练。'
        f'正式比较固定 seed11，seed29只诊断。新 scarce 计划64个环境，完整去重配对 n={r["n"]}。',
        '所选 P/S 的权重、全部阶段存档、优化器/RNG、真实前向记录与逐状态动作均在本目录。',
        '扩大实验：'+('通过本轮门。' if r['gates']['expand'] else '未通过全部预登记门；不自动追加模型、种子或重训。'),
        '',
        '二、相同环境、候选池和在线 CRN 的实测',
        '方法 | 筛选损失 | 集内选择损失 | 最终遗憾 | 连续P50秒 | 连续P95秒']
    for method,a in r['aggregates'].items():
        sections.append(f'{method} | {a["screening_loss"]:.6f} | {a["within_selection_loss"]:.6f} | '
                        f'{a["final_regret"]:.6f} | {a["p50"]:.4f} | {a["p95"]:.4f}')
    sections += ['', 'P8相对对照的最终遗憾改善（正为P8更好）；环境区间与条件参考MC区间分开：']
    for name,c in r['P8_improvement'].items():
        sections.append(f'对{name}：均值 {c["mean"]:.6f}；环境双侧95% {c["environment_ci95"]}；'
                        f'单侧95%下界 {c["environment_lower95"]:.6f}；条件参考MC双侧95% {c["reference_mc_ci95"]}。')
    c=r['P8_extra_regret_vs_H32']
    sections += [f'对H32额外遗憾：均值 {c.get("mean")}；单侧95%环境上界 {c.get("environment_upper95")}。',
        f'换序：有效对照 {r["permutation_pairs"]}，Top2集合一致率 {r["permutation_top2_consistency"]}；逐ID分数、动作变化和参考MC见analysis/results.json。',
        '逐门结果：'+json.dumps(r['gates'],ensure_ascii=False),
        '', '三、训练真实性、容量、成本与速度边界']
    for t in training:
        sections.append(f'{t["kind"]} seed{t["seed"]}：{t["parameters"]}个可训练参数，120次更新；'
                        f'验证选中epoch{t["selected_epoch"]}，训练/选模/存档 {t["training_seconds"]:.3f}s，'
                        f'存档I/O {t["checkpoint_seconds"]:.3f}s、{t["checkpoint_bytes"]}bytes。')
    sections += [f'实际底座前向 {counts.get("forward",0)} 次，累计 {forward_seconds:.3f}s；BF16峰值PyTorch分配显存 {peak:.3f}GiB。',
        'GPU整卡和进程采样见hardware目录；Windows WDDM的进程显存可能为N/A，不能冒充已测进程专属峰值。',
        '因果注意力、batch=1、无padding、无词表logits；现有PyTorch参考DeltaNet/conv内核。没有安装加速包。',
        'P输入来自冻结Qwen隐藏态；S输入来自完全相同的18字段量化值，但表示维度和参数容量不同。P-S不能分离全部架构因素。',
        '正式在线计时包括候选构建、tokenize、GPU同步、真实一次前向、读出、独立DES、同步审计/账本I/O、命令校验和选择证据fsync。'
        '最终自报计时文件写入在计时之后，冷加载单列。各方法未共享DES计算结果。',
        '', '四、预算、失败和证据边界',json.dumps(counts,ensure_ascii=False),
        f'新轨迹 {counts.get("trajectory",0)} 条，共 {counts.get("trajectory",0)*336} 模拟小时。原训练/验证标签新增DES=0。',
        f'在线方法失败 {len(read(root/"online-failures.json"))}；预检与其他异常记录文件 {len(list((root/"failures").glob("*.json")))}，均保留。',
        '真实自回归生成=0；远程推理=0；官方Jev请求=0。',
        '可用度与mandatory等业务指标为所选动作的独立参考窗内统计，见METHODS.csv与analysis/results.json；不是720h闭环成绩。',
        '训练/验证按环境分组，原测试不参与训练；新测试参考只在全部在线动作与模型封存后生成。',
        '有限池参考max有选择误差；主要行动差消去同池max。64个计划环境只提供原型信号，不能替代2E-B2或跨规则闭环确认。',
        '', '五、加载与复核',
        'P11-selected.pt / S11-selected.pt 位于models/；step0、epoch2/5/10/15/20位于checkpoints/。',
        '源码入口：scripts/phase2ec1_load_example.py；正常周决策入口为sim_jev.decision_resolver.resolve_decision，'
        '配合CandidateHeadPolicySpec和HeadDecisionRuntime。使用已有cgfedllm解释器。加载示例是显式可选命令，会继续占用本任务预算。',
        '阶段CLI：prepare/probe/cache/train/runtime-probe/seal/online/reference/analyze/audit/tests/report/package。'
        '已执行的研究阶段为不可覆盖，不能在同目录直接重跑。',
        '干净源码副本和审计收据见source/、regression/、audits/；审计只读重算，不重跑正式研究。',
        '外部依赖：G:/sim-jev-models/Qwen3.5-0.8B-Base-dc7cdfe2ee4154fa7e30f5b51ca41bfa40174e68；'
        'D:/conda_envs/cgfedllm与D:/conda_envs/sim-jev。审计包不重复包含底座或Conda。',
        '官方模型：https://huggingface.co/Qwen/Qwen3.5-0.8B-Base；原生接口：https://huggingface.co/docs/transformers/model_doc/qwen3_5。']
    (root/'REPORT.zh-CN.txt').write_text('\n'.join(sections)+'\n',encoding='utf8')
    with (root/'METHODS.csv').open('w',encoding='utf-8-sig',newline='') as f:
        flat=[dict(method=k,**{x:y for x,y in v.items() if x!='business'},**v['business']) for k,v in r['aggregates'].items()]
        writer=csv.DictWriter(f,fieldnames=list(flat[0]));writer.writeheader();writer.writerows(flat)
    with (root/'STATE_METRICS.csv').open('w',encoding='utf-8-sig',newline='') as f:
        rows=[{k:v for k,v in m.items() if k!='business'} for m in r['metrics']]
        writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    methods=list(r['aggregates'])
    fig,axes=plt.subplots(1,2,figsize=(11,4),layout='constrained')
    x=np.arange(len(methods))
    screen=[r['aggregates'][m]['screening_loss'] for m in methods]
    inside=[r['aggregates'][m]['within_selection_loss'] for m in methods]
    axes[0].bar(x,screen,label='Screening loss',color='#3973a6')
    axes[0].bar(x,inside,bottom=screen,label='Selection loss',color='#e9a23b')
    axes[0].set(ylabel='DSR + RCR regret',xticks=x,xticklabels=methods,title=f'New scarce holdout (n={r["n"]})')
    axes[0].legend()
    for off,key,color in [(-.18,'p50','#3973a6'),(.18,'p95','#e9a23b')]:
        axes[1].bar(x+off,[r['aggregates'][m][key] for m in methods],.36,label=key.upper(),color=color)
    axes[1].set(ylabel='Continuous decision seconds',xticks=x,xticklabels=methods,title='Measured end-to-end latency')
    axes[1].legend()
    fig.savefig(root/'RESULTS.png',dpi=180)
    plt.close(fig)
    return dict(report=str(root/'REPORT.zh-CN.txt'),plot=str(root/'RESULTS.png'))
