# 阶段 2B：合成周模式契约

本阶段实现软件基线：周级承诺、固定日执行器、实体键控外生随机数及可恢复快照。
没有 PHM/RUL 观测、PlanningScenario、候选池、DES 前瞻筛选、价值训练、PPO、Jev、成本模型或 J720。
测试通过不代表论文严格复现、策略最优性、PHM 预测收益或真实机队有效性。论文来源和差异仍见
[PAPER_MODEL_MAP.md](PAPER_MODEL_MAP.md)；原研究计划、论文及历史 ZIP 没有改动。

## 1. 并存的入口与身份

| 模式 | Python 入口 | model_version | rng_protocol_version | checkpoint schema |
| --- | --- | --- | --- | --- |
| 旧 2A | `sim_jev.Simulation` | `synthetic-single-lru-v1` | `numpy-seedsequence-four-stream-v1` | 1 |
| 新 2B | `sim_jev.WeeklySimulation` | `synthetic-weekly-single-lru-v1` | `sha256-entity-counter-v1` | 1（独立模型命名空间） |

旧 `Config`、`default.json/stress.json`、旧科学输出和旧 2A 快照保持兼容。新模式要求
`horizon_hours=720`、`reorder_interval_hours=24`；不默默忽略冲突配置。
`configs/weekly_default.json` 与 `weekly_stress.json` 显式包含模型、scenario_id、物理配置、规则和策略输入。
两场景的负荷/保障数值来自原合成配置，新规则给出名义备用和小时预算，并包含提前公布的未来规则。

```python
from sim_jev import Config, MissionSpec, WeeklySimulation
from sim_jev.weekly_policy import PolicySpec, RuleSpec, WeeklyCommand

sim = WeeklySimulation(Config(), [MissionSpec("M", 167, 2, 1, 10)],
                       scenario_id="example", policy=PolicySpec())
sim.run(168)  # 第一 cohort 中 M 仍 running；第二周决策已完成
snapshot = sim.to_checkpoint()
restored = WeeklySimulation.from_checkpoint(snapshot)
restored.run(169)  # 成功只补到 M 原属的第一 cohort
```

`missions=None` 生成外生任务，`[]` 明确无任务；`initial_lifetimes` 是内部实验 override，
不能传入策略。reset 保留这些区别；`reset(seed=...)` 从原输入开始另一实验。
`get_observation()` 返回独立的 `PolicyObservation`，`.to_dict()` 为可序列化公开副本。
`RuleContext` 和 `WeeklyCommand` 也支持 `.to_dict()/from_dict()`。

```powershell
& 'D:\conda_envs\sim-jev\python.exe' -m sim_jev --mode weekly --config configs/weekly_default.json --until 168 --output runs/new-weekly-pause --checkpoint-out runs/new-weekly-pause.json
& 'D:\conda_envs\sim-jev\python.exe' -m sim_jev --resume runs/new-weekly-pause.json --output runs/new-weekly-resume
```

以上为使用示例，实际执行命令和退出码以验收记录为准。CLI 恢复按快照模型分派；显式 `--mode`
必须匹配，不能同时传 `--config/--seed` 覆盖恢复输入。两类 Python 恢复入口互不冒认版本。
输出目录和快照文件拒绝覆盖；不使用 pickle。新模式报告中的 `checkpoint.json` 和
`random-manifest.json` 是内部审计材料，包含隐藏真值，不是策略观察。

## 2. 时间和持续执行规则

周点为 0/168/336/504/672，日点为 0/24/.../696。各周公布全部
`start_time ∈ [t,min(t+168,720))` 的任务；显式 start=720 或更晚的任务一直 pending、不 offer、不排启动。
旧模式仍处理 start<=720。新模式在720处理已有物理事件及同刻后果，不发新的周/日指令。

每个真实时刻在 `WeeklySimulation.run()` 内按以下屏障处理：

1. 已有故障、维修完成、到货、任务结束和尝试维修事件，连同新生成的同刻物理因果链全部排空。
2. 到公布时刻的规则、当前窗任务进入公开信息，形成一次周命令。
3. 若为日点，发布固定日命令并检查一次库存位置。
4. 启动任务；每个启动引起的零寿命故障、零维修/交期等物理因果链，先于下个同刻启动处理。

物理顺序保留故障先于完成，故障与任务结束完全同刻仍记失败。时间按 binary64 严格比较，
不合并 `nextafter` 邻点。`run(t)` 返回时该时刻已经稳定；周/日点是已执行状态的一部分，恢复不重发。

固定日策略版本为 `id-dispatch-fifo-daily-stock-v1`：按飞机 ID 派足整项任务所需飞机；
纠正维修按 `(requested_at, repair_id)` FIFO；有件有位即可在日间故障/到货后即时开工。
任务真正开始才占机，成功或中止按真实事件释放，因此同日不重叠任务可复用飞机。
唯一补货入口是每日命令调用共用单次库存检查；新模式没有旧 `reorder_check` 队列事件。
日志仍保留每日一条 `reorder_check`，有正数量才下一单。库存位置为在库+在途−全部等待维修需求。

机位固定，无抢占；维持旧模型的无限仓容、无限外部供应能力与单供应商假设。
准确 ETA 在下单时公开，不存在确认后的隐藏延误。维修 due_at 仍隐藏。

## 3. 规则、计划与实际状态

每窗一个 `RuleSpec(window_start, window_end, announced_at, effective_at, version,
mandatory_ids, min_reserve, max_aircraft_hours)`。公布和生效均在共同周点，允许提前公布未来窗口规则。
可见性按 announced_at，当前承诺的适用性按 effective_at 和 window。内部尚未公布规则不进观察。
当前有限规则机制不实现一窗内多次修订，也不实现物理资源突变。

策略只读当前合法飞机状态、占用、实际已飞小时、已公开任务/规则、订单承诺及已发生维修事实。
没有真实寿命、failure_due_at、隐藏维修 due_at、未来未公布任务、根种子、执行随机对象或队列。
实际已飞小时只是事实；指数寿命机制没有年龄递增风险或 PHM 预测信号。公开副本的修改不影响引擎。

计划采用**名义总机队**，即使当前有飞机在修也不从 nominal fleet_size 中扣除，更不读隐藏维修完成时间。
因此“计划可接纳”不保证实际开始时能派机。报告的 actual_idle（实际可派）和 actual_available（idle+flying）
与名义 minimum_reserve 分开。

计划任务占用为 `[start,start+duration)` 内的需机数叠加，使用全部区间端点扫描峰值，
不用每日抽样。窗口计划飞机小时是 `sum(required × overlap_duration)`。
跨窗已承诺且未终结任务继续占用新窗；任务终结后不再作为后续 carry。新规则不追溯取消旧承诺，
即使 carry 本身超过新规则也保留并报告不可行。每窗命令保存 `plan_usage` 的峰值、最小备用和小时数。

周策略版本 `mandatory-first-nominal-v1`：必需任务按 `(start_time,id)`；可选任务按
`(-value,start_time,id)` 逐项尝试。脚本策略为 `PolicySpec(kind="scripted", commands=(...))`，
显式给每个周点的 `WeeklyCommand(window_start, accepted_ids)`，拒绝全部可用空元组。
无 Python 回调。策略 name 仅标记，不参与抽样或排序。

普通非法命令（重复/未知/未提供 ID、漏掉可行 mandatory、超计划约束、缺少当前脚本命令）明确报错。
mandatory+carry 本身不可行时采用统一 fallback：按 `(start,id)` 贪心接纳仍可加入的 mandatory，
不接 optional；保留全部原义务，分别记录实际 accepted、missing_mandatory、fallback=true 和结构化
`nominal_reserve/planned_aircraft_hours` 原因。此路径可以接纳零项，但不能伪称原规则已满足。
日执行器只执行周承诺，实际资源不足记 accepted_not_executed，不能删掉困难任务或改接纳集。

## 4. cohort、积分和终点

`weekly_state` 持久保存各窗 offered/accepted/rejected ID、冻结价值分母、原 mandatory、实际命令及唯一归属。
统计时始终满足：

```
success + failed + accepted_not_executed + unfinished = accepted
accepted + rejected = offered
unfinished = pending_execution + running
```

跨周成功在实际完成时补到开始窗 cohort。窗口关闭时保存不可回写的 `closed_cohort_snapshots`
（as_of=window_end）；167→169 的任务在168快照是 unfinished，169以后最终 cohort 是 success。
任务拒绝仍留在 offered 分母。`mandatory_shortfall_as_of` 是截至当时未成功的原义务数，
`mandatory_shortfall_terminal` 在720之前为 null、在720才给终局未完成数；未开始不伪称终局失败。

| 指标 | 分子 / 分母 |
| --- | --- |
| DSR | success / offered |
| RCR | earned_value / offered_value（整项合成价值点） |
| ASR | success / accepted |
| acceptance_rate | accepted / offered |

零分母均 null，附 validity；全期先汇总分子/分母再除，不平均五个周比率。
launched 是实际已派任务数，仍执行的任务计 running/unfinished。
计划 AFH 按每窗冻结计划的窗口交叠相加，实际 AFH 仅积真实执行暴露；两者有不同含义。
可用、空闲、飞行、缺件等待、机位等待按实际时间分段积分，并分别保存各物理周窗。
报告中的等待是飞机/维修请求小时，库存是件。

日志时间窗按半开区间归属；720 的故障/完成/到货及因果后果在
`terminal_boundary={time:720,event_ids:[...]}` 中列出，且只记一次。
该边界子账归末窗，全文事件/结果不重复累计。未完成任务/维修/在途和720之后物理队列保留。

本轮只有账本、诊断和截至当时的已知结果，没有发训练奖励。未来按真实完成时刻结算的项目约定，
与论文逐周 DSR+RCR 的奖励时刻、折扣及信用分配并不等价，也不等于全域 J720。

## 5. 实体键控随机协议

规范键是 UTF-8 JSON，`sort_keys=True, separators=(',',':'), ensure_ascii=False, allow_nan=False`：

```
protocol / root_seed / domain / scenario / entity_type / entity_id /
generation / purpose / draw_index
```

协议为 `sha256-entity-counter-v1`，分布变换为 `inverse-cdf-v1`。
SHA256 摘要前64位右移12位形成52位整数，`u=(integer+0.5)/2**52`，严格在(0,1)。
均匀为 `low+(high-low)*u`，指数为 `-log1p(-u)/rate`，零率寿命为正无穷标记。
闭区间整数均匀使用完整256位摘要及拒绝重哈希消除取模偏差。无 Python hash()、墙钟或调用顺序输入。
同键重复读取返回已有值、不重复登记；同键改分布报错。

| 用途 | entity_type / entity_id / generation / purpose |
| --- | --- |
| 任务 | arrival / arrival_index字符串 / 0 / interval、duration、aircraft_required、value |
| 初始/换件寿命 | component / Axxx/LRU0 / 换件代次（初始0） / lifetime |
| 维修时长 | component / Axxx/LRU0 / 本次故障所属部件代次 / repair_duration |
| 供货交期 | supplier / single/LRU0 / 固定日机会index（0..29） / lead_time |

draw_index 当前各用途为0；维修完成后该机代次加1。全局 R 编号只是日志实体 ID，不能决定随机样本。
外生任务在策略之外生成，arrival_index 不随接纳改变，越过720的最后到达间隔也留审计记录。
交期与订单数量独立，数量不入键；无订单的日机会不压缩后续 index。下单时以当日机会键抽交期，
公开 ETA=`ordered_at+lead`，物理到货使用同一个时刻。

策略名、算法名、候选位置、线程、调用顺序均不入键。不同策略可产生不同事件/代次；只保证同语义
实体、代次、用途的配对，不要求轨迹相同。manifest 保存规范键、摘要、变换版本、分布参数和值，
仅供内部审计。execution 与 planning 为不同协议域；本轮不生成预测情景或实现 PlanningScenario。

## 6. checkpoint 与溯源校验的边界

新快照复用原物理实体解析和校验，由显式 weekly profile 添加状态/日志规则；不将新状态伪装成旧模型。
保存物理实体、队列序号、全部日志/history/积分、原输入、规则公布/生效信息、冻结 cohort、周/日命令、
已执行点、策略输入/版本、实体代次、日机会和完整键控 manifest。恢复用 `__new__` 接续，
不 reset、不重新抽过去的样本、不调用过去策略、不生成历史命令。导出和 clone 深度隔离。

2A 修补要求旧初始化 mission_start 的完整 start/ID 对应序号；维修/交期支持范围用
`started+low <= due <= started+high` 的原引擎加法结果精确比较。固定时长只允许该一个浮点时间，
不以积分容差吞掉 nextafter 分类。新模式有自己的任务排队规则，按实际 profile 核验。
累计飞行/等待/积分恢复比较容差为 rel_tol=1e-12、abs_tol=1e-10；事件、输入、规则、命令、分母严格比较。

runtime 保存真实模型/协议/schema/策略身份、实际命令/解释器、有效配置摘要、包含未跟踪源码的指纹和 Git 状态。
Git 明确 project_root、真实 repo_root、相对关系，父仓库 HEAD/dirty 只代表父仓库。
`metadata_valid`、`manifest_self_consistent`、`source_identity_verified` 分开；需要身份核验时传独立预期指纹
或 source_root。历史报告不强制与今天源码匹配。内部一致性和哈希不是密码学来源认证。

单实例不支持并发修改；二进制浮点/数学库及依赖环境改变可能影响逐位结果。验收使用既有解释器和依赖，
没有重建环境、调用外部模型、自动提交或推送。验收程序显式拒绝优化 Python，不能删掉断言后宣称通过。
