# 阶段 2C 实际交付与验收

日期：2026-11-02（Asia/Shanghai）。源码与实验位于 `G:\sim-jev`，解释器为 `D:\conda_envs\sim-jev\python.exe`。

**离线引擎与完整决策管线已验收；真实 Jev smoke/pilot 未验证。** 当前缺少 `TYPESAFE_API_KEY`，实际 TypeSafe HTTP 为0次。人工 stub 的闭环结果只验证管线，不能作为 Jev 效果数据。

| 交付等级 | 结果 |
| --- | --- |
| engine_offline_verified | true |
| jev_live_smoke_verified | false（缺凭据） |
| jev_live_pilot_completed | false |

## 实现与数据权限

公开观察、规则与白名单模型参数 → 统一可行候选池 → Jev Choice/随机/启发式预筛 → 独立规划情景一周 DES → 统一校验并提交唯一周命令 → 原固定日执行器。

新请求、策略、规划模型、条件分布、候选摘要、预算及 API 模板分别版本化；旧2A/2B模式与快照身份保持。执行seed、真实寿命、维修due、未公布任务/规则、执行队列和manifest不进入决策器。规划世界从合法输入建立，使用planning域随机数、条件残余维修/寿命、公开ETA及现有占用；没有复制执行未来。

分支先完成窗口终点的物理闭包，再停止在下一窗公布/承诺/日命令之前；末窗为48h。当前cohort DSR+RCR是已实现短窗分数，拒绝不改分母，跨窗旧任务仅占资源，未终结仍unresolved，没有终端价值。

契约：[总契约](PHASE2C_CONTRACT.md)、[规划及独立核算](PHASE2C_PLANNING.md)、[候选与冻结排序](PHASE2C_CANDIDATES.md)、[Jev协议](PHASE2C_JEV_PROTOCOL.md)、[配对实验](PHASE2C_EXPERIMENT.md)、[打包作用域](PHASE2C_PACKAGE_SCOPE.md)。

## 2B 修补与审查反例

- 规则reserve改为1、同步公告却仍接受A得10的恢复漏洞，现按历史offers/rule/carry和统一validate_command拒绝；预算、mandatory、伪fallback、fallback原因损坏均有反例，合法超限carry fallback仍可恢复。
- 三个Git测试使用自建临时仓库/无Git/父仓库fixture，历史schema使用明确元数据。包级MANIFEST移到project外，source scope v2排除包产物并保留v1历史核验。
- cohort.RCR=-1、offered_value+123、关闭快照success=999现均被独立核算拒绝；所有分类/分母/率/validity/as_of关闭快照按实际日志重建。
- 额外审查发现的旧候选摘要、篡改Jev概率/分支score/scenario_id以及重hash伪造历史公开状态，均有修后拒绝测试。API补绝对等待deadline和Retry-After超预算停止，真实本地slow-drip测试通过。

改前基线166/166通过；M0冻结后186/186通过。首轮M0全量曾因并行改scope造成内存/磁盘定义不同失败，固定后重跑通过；历史兼容开发运行也曾正确拒绝源码变化。原失败日志保留，未抹除或改写为成功。

## 冻结源码的实际验证

| 验收 | 实测结果 |
| --- | --- |
| 完整unittest | 264/264，退出0 |
| validate.py | 两场景seed0–9、各重复两次，共40次；两个seed42科学输出与独立核算通过 |
| 2A checkpoint_verify | 20组、160次JSON往返、18组新进程，通过 |
| 2B weekly_verify | 20组、160次JSON往返、18组新进程，通过 |
| 历史包兼容 | 34/34快照（16份2A、18份2B）通过；旧18/18科学文件字节相同 |
| 新策略恢复 | 五种新方法实际新进程续跑；比较完整命令/物理日志、配置/reset输入、cohort、队列/RNG与逻辑账本，保留实际网络/墙钟审计差异 |
| 闭环+固定状态 | 60组、300次周决策；6528次DES，每支独立日志核算通过 |

源码scope为 `project-source-and-config-v2`，指纹 `626224fb574592c92bbad196f8710ae7cb5710557e0eed98be49060958180c98`。正式命令前后源码一致。累计积分沿用rel_tol=1e-12、abs_tol=1e-10；事件与邻接浮点边界、整数、分母严格，不删业务日志。

实际命令（下列路径均相对项目根）：

```powershell
& 'D:\conda_envs\sim-jev\python.exe' scripts/validate.py --output runs\phase2c-20261102\final-validation  # exit 0
& 'D:\conda_envs\sim-jev\python.exe' scripts/checkpoint_verify.py --output runs/phase2c-20261102/final-checkpoint  # exit 0
& 'D:\conda_envs\sim-jev\python.exe' scripts/weekly_verify.py --output runs/phase2c-20261102/final-weekly  # exit 0
& 'D:\conda_envs\sim-jev\python.exe' scripts/phase2c_compatibility.py --output runs/phase2c-20261102/compatibility-final  # exit 0
& 'D:\conda_envs\sim-jev\python.exe' scripts/phase2c_experiment.py --output runs/phase2c-20261102/pilot-final  # exit 0
& 'D:\conda_envs\sim-jev\python.exe' scripts/jev_smoke.py --allow-live --max-http-requests 10 --total-timeout-seconds 120 --output runs/phase2c-20261102/live-smoke-environment-check  # exit 2
```

完整argv/退出码/时长见 `runs/phase2c-20261102/final-commands-ledger.json`；全部测试名见 `final-validation/unittest.txt`，跨进程子命令见各验收目录commands.json。

## 逐 seed、逐决策和预算

两个冻结场景各5个配对环境seed，六种方法各运行720h，共60组。闭环DES=6400，固定状态DES=128，总模拟小时=943104。默认M16/K4/B8；四种DES方法每个正常决策32次，mandatory-first与Jev直接选择为0次。池不足与fallback均记实际预算。

固定状态同时含在修、缺件等待、跨窗占用与mandatory容量边界；各方法共享冻结池。客户端固定候选呈现次序，反序候选输入形成相同请求；真实模型对不同呈现顺序的敏感性因缺凭据尚未测量。没有高预算质量标签，不报告Regret@K。

下表为各场景5个seed的累计分子/分母汇总；Jev两列明确是人工stub管线，不能据此评价真实模型优劣。

| 场景 | 方法 | DSR | RCR | ASR | mandatory终局缺口 | 实际AFH | DES |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| balanced | direct-des | 0.742222 | 0.661778 | 0.907609 | 1 | 1773.858616 | 800 |
| balanced | heuristic-des | 0.742222 | 0.658222 | 0.902703 | 1 | 1771.698673 | 800 |
| balanced | jev-des〔stub〕 | 0.604444 | 0.562667 | 0.883117 | 0 | 1805.031404 | 800 |
| balanced | jev-direct〔stub〕 | 0.520000 | 0.453333 | 0.900000 | 1 | 1396.564789 | 0 |
| balanced | mandatory-first | 0.737778 | 0.684000 | 0.922222 | 0 | 1902.330985 | 0 |
| balanced | random-des | 0.702222 | 0.633778 | 0.918605 | 2 | 1678.236396 | 800 |
| scarce | direct-des | 0.377778 | 0.295111 | 0.634328 | 3 | 956.171226 | 800 |
| scarce | heuristic-des | 0.386667 | 0.324000 | 0.608392 | 4 | 1022.567454 | 800 |
| scarce | jev-des〔stub〕 | 0.373333 | 0.305333 | 0.617647 | 5 | 977.693007 | 800 |
| scarce | jev-direct〔stub〕 | 0.306667 | 0.245333 | 0.644860 | 4 | 751.345457 | 0 |
| scarce | mandatory-first | 0.368889 | 0.324889 | 0.588652 | 6 | 995.094545 | 0 |
| scarce | random-des | 0.342222 | 0.275111 | 0.606299 | 6 | 882.321747 | 800 |

完整数据：[逐seed](../runs/phase2c-20261102/pilot-final/per-seed.csv)、[逐决策](../runs/phase2c-20261102/pilot-final/per-decision.csv)、[汇总](../runs/phase2c-20261102/pilot-final/aggregate.csv)。每组子目录保留原始events/missions/repairs/orders、checkpoint、cohort、候选/排序/情景/分支分数和实际履约、API与预算账本。

本机闭环实验总墙钟（含报告写入）为464.310秒；实际TypeSafe HTTP=0，token usage=0、实测API费用估算=0。stub的valid_jev=false是证据标记，不是服务可用率测试。API有效率、失败/fallback、等待/可用积分、实际飞行小时、模拟时长和端到端时间均分列于CSV。选择内部计时不含候选生成；完整端到端成本引用run级时长。部分验收同时运行，不能将这些墙钟作为模型时延性能结论。

## live、限制、Git和审计包

固定jev-1.14.0，Choice发送合法state及候选ID摘要，验证完整概率覆盖、类型/有限性/范围/总和、模型和choice，按概率降序稳定ID破同分取Top-K；confidence不当任务成功率。live/真实replay/stub隔离，失败使用固定heuristic且单列。请求前持久预算，原子成功缓存，未持久化/超时保留uncertain；外部服务无exactly-once承诺。

缺少TYPESAFE_API_KEY，live入口实际退出2且未发送HTTP；具备凭据后可运行：

```powershell
& 'D:\conda_envs\sim-jev\python.exe' scripts/jev_smoke.py --allow-live --max-http-requests 10 --total-timeout-seconds 120 --output runs/phase2c-new-live-smoke
```

本规模仅为接入与管线检查，没有正式显著性/泛化/长期保障/PHM结论。后续公平PPO参考、共同续行价值与正式Jev消融尚未实现，不计作2C成果。

Git HEAD仍为 `cb86953963fc50b1cc440f5c38712a8fafa50669`；dirty=True，README修改及源码/测试/配置/docs大多未跟踪，未提交、未推送。既有研究计划和原ZIP未修改。详情见 `runs/phase2c-20261102/final-git.json`。

审计包目标为 `G:\sim-jev-audit\sim-jev-phase2c-audit-20261102-626224fb.zip`。包级MANIFEST位于project外，排除.git、环境、缓存与密钥，包含对应源码/配置/测试/协议及本轮证据。

打包后的实际新目录解压、全条目SHA256重读、源码身份、无Git完整测试和再次60组闭环实验，记录在包旁 `.zip.verification.json` 与解压根 `SELF-ACCEPTANCE.json`。最终ZIP自身SHA256在旁置 `.zip.sha256`，不写回包内制造自引用。最终中文交付补充报告另列实际结果。
