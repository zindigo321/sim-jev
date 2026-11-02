# 阶段 2A 修补及阶段 2B 实施验收

日期：2026-11-02（Asia/Shanghai）。两项工作均完成，范围止于 2B。
全部执行使用 `D:\conda_envs\sim-jev\python.exe`；没有安装依赖、重建环境、修改其他 Conda 环境、
调用外部付费模型、自动暂存、提交或推送。论文、原研究计划和历史 ZIP 只读。

本轮证据根目录：[`runs/phase2b-20261102-170159/`](../runs/phase2b-20261102-170159/)。
最终接受的源码指纹为 `38b099f36a5c5fd3b875b8727d11b5dbdd896bd8f9014d8498774f1a6964a01d`。
代码指纹包括未跟踪源文件/配置/测试/脚本，排除 runs、缓存、环境和文档；审计 ZIP 的逐文件清单另覆盖文档。

## 1. 先完成的 2A 修补

开始时当前 31 个对应项目文件与原 2A ZIP 字节一致，没有用旧包覆盖工作区。
改前源码清单、源码副本、Git 状态、原 **84 项测试**和 default/stress seed42 输出均固化在 `00-before/`，
三个命令均退出0。原包 SHA256 为 `4677b88e40e50dc743bb0db81930d61989e4cc96f9715f3b68d58f9988dbab76`。

| 修补 | 实际证据/结果 |
| --- | --- |
| 同刻启动序号 | A/B 两个唯一 sequence 交换并合法排序后，明确拒绝 `initialization identity` 不符；正常价值仍为10 |
| 固定维修支持 | 1h开修、4h时长的 due_at 和事件同步改100，明确拒绝配置支持范围；正常7h任务成功 |
| 固定供货支持 | 0h下单、8h交期的 ETA/事件/日志同步改100，明确拒绝；正常20h库存为1 |
| 浮点边界 | 零时长、小数累计、nextafter、活动/完成维修和订单均有真实回归；时刻支持使用原引擎加法边界，不扩大容差 |
| 完整比较器 | config、原 missions 输入和 initial_lifetimes 严格比较；None/空/override不混淆；改输入后reset轨迹不同的反例被比较器捕获 |
| 溯源 | Git核验真实show-toplevel并记录项目/父仓库关系；schema拒绝bool；格式、自洽、预期源码身份三项分开，历史来源可单独指定 |
| 优化模式 | 验收脚本实际用`-O`调用被明确拒绝，不会删除断言后宣称通过 |

三类损坏快照的正常/损坏 JSON 及错误消息在 `01-2a-fixed/*-valid.json`、`*-corrupt.json`、
`corruption-rejections.json`。独立里程碑0在进入2B前已经通过：**107项测试、40次旧模式重复运行、
160次JSON恢复、18组跨进程**；16份原归档快照续跑通过；每场景9个科学文件不变。
该阶段源码副本/指纹和完整结果保存在 `01-2a-fixed/`，不是用最终源码伪称阶段0原样运行。

## 2. 已实现的 2B

详细 API、版本、公式及范围见 [PHASE2B_CONTRACT.md](PHASE2B_CONTRACT.md)。

- `WeeklySimulation` 独立入口：模型 `synthetic-weekly-single-lru-v1`，随机协议 `sha256-entity-counter-v1`，
  checkpoint schema 1（与旧模型分别分派）。旧模型/协议/schema保持原值。
- 周点公布窗口任务并冻结接纳账本；日点发布固定ID派机、FIFO纠正维修、库存位置补货规则。
  同刻屏障位于真实引擎内部；共用旧物理处理器和积分，不另造旁路仿真器。
- 显式 `PolicyObservation/RuleContext/WeeklyCommand/PolicySpec/RuleSpec`。脚本可序列化；
  固定 mandatory-first 是软件基线。名义备用、区间峰值、交叠AFH预算与实际空闲/实际飞行分开。
- announced_at/effective_at分开；跨周义务保持；mandatory不可行时保留原义务、结构化原因和实际fallback承诺。
- cohort冻结offered/accepted/rejected及价值；跨周结果按真实完成补原窗；关闭时as_of快照不提前读未来。
  DSR/RCR/ASR、接纳率、mandatory短缺、积分和终点子账均实现；零分母null并附有效性。
- SHA256规范实体键：任务用arrival_index/属性，寿命和维修用飞机/LRU槽/换件代次，供货用固定日机会index。
  数量、策略名、调用顺序不入键；公开ETA来自下单时该机会的交期样本。完整内部manifest随恢复接续。
- 新快照持久化命令、规则、cohort、已执行周/日点、代次与manifest；恢复不reset、不抽过去样本、
  不调用过去策略。完整manifest还核对实体/配置/时间加法，缺失、多余或矛盾样本明确拒绝。

旧模式调用 `Simulation.load_checkpoint()`，新模式调用 `WeeklySimulation.load_checkpoint()`。
CLI `--resume` 自动按版本分派；显式错误模式拒绝。`--mode weekly --config configs/weekly_default.json`
或 `weekly_stress.json` 建立新周实验。runtime、checkpoint和文档均记录实际模型/协议/schema/策略身份。

## 3. 最终实际验收

| 验收 | 最终结果与证据 |
| --- | --- |
| 原84与所有新增回归 | **166/166**，`04-final-validation/unittest.txt`，29.423秒；原六个测试文件没有修改 |
| 旧实际validate | 40次重复运行，两组seed42输出、独立表格/积分核算；`04-final-validation/validation.json` |
| 旧checkpoint_verify | 20配对、160 JSON、18新进程；`04-final-legacy-checkpoint/verification.json` |
| 新weekly_verify | 2场景各10seed：20配对、160 JSON、18新进程；`04-final-weekly/verification.json` |
| 新进程状态覆盖 | 周边界；在修`t=66.45630168923577`（在修1、在途5）；在途`t=0`（在途2）；保存源/终态JSON、实际argv/日志 |
| 最终CLI恢复 | 独立整段、暂停168退出、另进程恢复stress seed42；`05-final-evidence/commands.json`及`weekly-cli-comparison.json` |
| 旧归档兼容 | 最终代码再加载原包中16份快照并续跑比较；`05-final-evidence/archived-2a-compatibility-final.json` |
| 旧科学文件 | default/stress各config、events、missions、summary、states、repairs、orders、state、overview共**18文件字节一致**；仅runtime溯源按预期变化 |
| 源码身份 | 新周验收开始/结束指纹相同，逐seed runtime使用独立预期指纹；`source_stable=true`、`source_identity_required=true` |
| 独立实现审查 | 11真实探针修后全过，另2非法类型探针通过；原失败与修后证据保留在`review/` |

新旧恢复均比较完整业务日志、事件ID/顺序/时刻、输入、配置、实体、队列、随机状态及新周规则/命令/账本。
仅累计飞行/等待/积分及其派生比例允许 `rel_tol=1e-12, abs_tol=1e-10`。
新周20配对和18跨进程共记录43处累计浮点差，最大 `9.094947017729282e-13`；
没有合并邻接浮点时间、丢业务事件、改分母或放宽容差。
history仅规范化额外的无变化、非业务时刻暂停采样；业务日志从不删行。

真实专项回归涵盖：0时先承诺再派机；nextafter(168)前/正好/后及672/720归属；167→169中间态；
跨周容量阻塞；拒绝全部、零任务/价值、实际无机、故障失败、mandatory不可行、备用/预算边界；
同日复用、日间FIFO到货开工、每天唯一补货；故障与完成同刻失败；零寿命/维修/交期闭包；
720成功/故障/到货及后果只计一次；脚本/观察深复制；两种真实策略的共同任务表；
A001额外维修使A002的全局维修ID从R000001变R000002，但同代次维修/寿命**键和值**完全相同；
改策略名称不改外生样本；不同隐藏寿命、未公告任务及维修实现下，本轮公开输入与命令相同。
没有通过改公开ETA制造不一致，也没有据此声称2C预测情景已无泄漏。

独立核算直接从已记录业务事实重建派机/释放、等待、库存流和时间积分，核对计数、价值、实际AFH、
各物理周窗积分、计划AFH及cohort分母；不生成另一套任务/事件/随机世界。
每seed的完整missions、events、history、commands、rules、manifest、checkpoint、runtime和核算结果均保留。

开发期 `03-weekly-verification/` 也曾运行通过，但当时并行开发使源码指纹变化，明确只作为集成证据；
**最终验收以04-final目录和05-final-evidence为准**。

## 4. 实际命令、退出码和发现处理

以下最终命令均实际退出 **0**，原始输出及argv保存在 `04-final-commands.json` 和相应日志：

```powershell
& 'D:\conda_envs\sim-jev\python.exe' scripts/validate.py --output runs/phase2b-20261102-170159/04-final-validation
& 'D:\conda_envs\sim-jev\python.exe' scripts/checkpoint_verify.py --output runs/phase2b-20261102-170159/04-final-legacy-checkpoint
& 'D:\conda_envs\sim-jev\python.exe' scripts/weekly_verify.py --output runs/phase2b-20261102-170159/04-final-weekly
& 'D:\conda_envs\sim-jev\python.exe' runs/phase2b-20261102-170159/final_evidence.py
```

`validate`真实调用的单元测试命令是 `python -m unittest discover -s tests -v`（实际解释器如上）；
其完整stdout/stderr和166项结果保留。全部跨进程实际argv/exit在各验收子目录`commands.json`。
改前、M0、集成、最终、CLI与审查命令分别有对应命令账本；交付的`commands-ledger.json`汇总这些记录，
不把工具读取/静态浏览伪装成运行测试。

独立审查首次退出1，发现mandatory不可行时fallback绕过脚本未知ID校验；已前置脚本存在性/ID合法性检查，
新增回归并保留原失败。相关旧新增测试曾使用缺失脚本命令作为fallback输入，修正为合法显式空接纳命令，
保留全部fallback义务/缺口断言；没有绕过测试。非法kind/domain类型现在明确ValueError。
修后审查、关联测试和最终全套均通过。所有失败开发证据保留，没有覆盖历史结果。

## 5. 逐 seed 软件验收数据

下表为新周模式最终720h。完整精度、价值、DSR/RCR/ASR、计划/实际AFH、库存、等待/可用积分及恢复差异见
[`per-seed.csv`](../runs/phase2b-20261102-170159/04-final-weekly/per-seed.csv)。
各行均通过整段/恢复配对和独立日志核算。这是软件验收，不是策略显著性或跨场景性能结论。

| 场景 | seed | offered | accepted | success | failed | 未执行 | unfinished | 实际AFH | 在库/在途 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| default | 0 | 90 | 90 | 83 | 7 | 0 | 0 | 683.123741 | 4/0 |
| default | 1 | 82 | 82 | 79 | 3 | 0 | 0 | 668.028177 | 4/0 |
| default | 2 | 74 | 74 | 69 | 5 | 0 | 0 | 582.877964 | 4/0 |
| default | 3 | 81 | 81 | 77 | 4 | 0 | 0 | 591.649908 | 4/0 |
| default | 4 | 91 | 91 | 82 | 9 | 0 | 0 | 679.703083 | 3/0 |
| default | 5 | 75 | 75 | 67 | 6 | 0 | 2 | 556.291709 | 4/0 |
| default | 6 | 87 | 87 | 80 | 6 | 0 | 1 | 712.165724 | 4/0 |
| default | 7 | 83 | 83 | 81 | 2 | 0 | 0 | 643.750500 | 4/0 |
| default | 8 | 84 | 84 | 78 | 6 | 0 | 0 | 609.424210 | 4/0 |
| default | 9 | 84 | 84 | 76 | 6 | 0 | 2 | 624.147234 | 4/0 |
| stress | 0 | 592 | 105 | 25 | 44 | 36 | 0 | 739.446585 | 5/2 |
| stress | 1 | 559 | 99 | 19 | 43 | 36 | 1 | 635.119936 | 6/1 |
| stress | 2 | 622 | 111 | 26 | 43 | 42 | 0 | 722.771590 | 4/3 |
| stress | 3 | 545 | 107 | 26 | 44 | 36 | 1 | 726.379457 | 4/3 |
| stress | 4 | 568 | 103 | 14 | 42 | 47 | 0 | 453.803001 | 7/2 |
| stress | 5 | 584 | 102 | 19 | 41 | 42 | 0 | 598.762968 | 4/4 |
| stress | 6 | 571 | 102 | 28 | 41 | 33 | 0 | 803.205788 | 4/5 |
| stress | 7 | 601 | 103 | 23 | 46 | 34 | 0 | 762.578278 | 6/2 |
| stress | 8 | 578 | 99 | 27 | 45 | 26 | 1 | 821.497629 | 3/5 |
| stress | 9 | 578 | 104 | 30 | 44 | 29 | 1 | 867.490027 | 5/4 |

## 6. 限制、Git 与审计包

当前为同型机、单LRU、指数飞行寿命、均匀维修/供货、固定机位、无限仓容/供给的合成模式。
计划使用名义fleet_size，不保证未来维修/故障下履约；没有PHM/RUL信号，不能由已飞小时推造年龄风险。
只支持每窗一个公开规则版本，不含动态机位/抢占。准确订单ETA是公开承诺；维修due隐藏。
训练奖励、成本/NSE/J720、PlanningScenario、候选池/前瞻筛选、长期价值、PPO/Jev均未实现。
快照/manifest含内部真值，不得作为规划观察。恢复校验不等于密码学来源认证，亦不保证任意依赖平台的逐位一致。

Git真实根为`G:\sim-jev`，HEAD仍为`cb86953963fc50b1cc440f5c38712a8fafa50669`，dirty=true。
README已修改，其余源码/配置/测试/docs等大多保持未跟踪状态；没有提交或推送。
完整状态见`05-final-evidence/git-state.json`；父仓库/无Git情形有真实临时仓回归，未改全局Git配置。

新审计包目标：`G:\sim-jev-audit\sim-jev-phase2b-audit-20261102-38b099f3-final.zip`。
包内含完整源码/配置/测试/docs、本轮各阶段证据、旧2A恢复样例、三损坏拒绝、跨进程样例、逐seed完整轨迹、
源码/Git指纹及SHA256清单；不含.git对象、环境、缓存或密钥。打包后重读全部条目核对大小和SHA256，
ZIP自身摘要及核验记录位于同名`.sha256`与`.verification.json`。本轮到此停止，下一轮再独立审计2B后讨论2C。
