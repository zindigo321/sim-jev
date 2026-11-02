# 阶段2A实施与实际验收（2026-11-02）

本轮完成“冻结研究契约 + 可恢复checkpoint + 运行溯源”。全部运行使用
`D:\conda_envs\sim-jev\python.exe`，没有安装依赖、重建环境、提交或推送。
论文与原研究计划/审计ZIP只读。证据根目录为
`runs/phase2a-20261102-1cd05986/`，下文路径均相对该目录。

## 实现和范围

- `sim_jev/checkpoint.py`：严格JSON、schema 1、独立的 `InternalCheckpoint`，包含全部实体、
  事件队列及sequence、指标、四条PCG64状态、完整events/history和reset输入。
  恢复经校验直接构造状态，不调用reset或重采样。校验是内部一致性检查，不是来源真实性签名。
- `Simulation.to_checkpoint()/from_checkpoint()/save_checkpoint()/load_checkpoint()/clone()`：
  新建/reset后先 `run(until=0)`；只保存成功返回的稳定边界。handler中禁止保存；handler异常后
  不能直接继续半状态，须reset或恢复。可变实体、日志、队列和RNG均独立。
- 正无穷仅在寿命字段使用 `{"$float":"+inf"}`，编码标识 `positive-infinity-tag-v1`。
  普通字段的NaN/无穷、未知版本、缺字段、错误引用、非法时间/容量/库存/RNG均明确拒绝。
  旧 `state.json` 继续保留 `resume_supported=false`，不可导入或猜测null寿命。
- `versions.py/provenance.py`：模型 `synthetic-single-lru-v1`、随机协议
  `numpy-seedsequence-four-stream-v1`；runtime记录实际命令、有效配置哈希、代码内容指纹、
  解释器/依赖、Git revision和dirty状态。指纹覆盖未跟踪Python源码/配置/脚本，按相对路径及内容
  计算，排除runs、缓存、环境、mtime；文档不属于代码指纹，审计包SHA256清单另覆盖文档。
- CLI支持 `--until/--checkpoint-out/--resume`；恢复时不能再混入config或seed，拒绝覆盖已有
  非空目录/文件，也拒绝将checkpoint路径指定为报告文件路径。科学summary没有新增溯源字段。
- [PAPER_MODEL_MAP.md](PAPER_MODEL_MAP.md) 和 [RESEARCH_CONTRACT.md](RESEARCH_CONTRACT.md)
  冻结来源、基准、信息权限、未来周窗口/cohort/延迟结算及实体键协议。

事件处理器、故障/完成优先级、库存规则、期末分类和旧配置未改。engine的修改只增加稳定边界
防护和公开checkpoint接口；旧50项测试文件字节未改。未来周模式仍是文档契约，当前运行保持
`time<=720`启动规则。本轮没有周承诺、实体键控CRN、PHM预测、合法情景、候选前瞻、PPO/Jev或价值训练。

## 验证清单与证据

| 验收项 | 实际结果及证据 |
| --- | --- |
| 改前保护与基线 | `before-source.zip`、`before-manifest.json`、`before-vs-phase1.json`；22个当前文件与阶段1包一致；`baseline-02.log`原50测试通过，两个seed42命令exit0 |
| 最终原测试及新增测试 | `validation-accepted/unittest.txt`：84/84通过，含原50、恢复路径8、校验/隔离19、溯源CLI7；原测试不改断言 |
| 旧基准重复性 | `validation-accepted/smoke.csv`：default/stress各seed0—9，每组重复两次，共40次；summary和事件严格一致 |
| 输出独立核算 | 实际执行 `scripts/validate.py`，验证CSV、积分、计数、库存、图像和新runtime元数据；历史runs只读核验，明确legacy元数据模式 |
| checkpoint多seed | `checkpoint-verification-accepted/verification.json`：20组seed，每组0、0.125、35.375、168、336、504、672、720共8次保存/恢复，总160次；每组完整事件/终态/账本/计数/RNG一致 |
| 真正跨进程 | 上述脚本对两场景seed0的8个停点各起新进程恢复（16组），另两场景seed42用完整CLI进程分别整段/暂停168/退出/恢复（2组），共18组；命令、exit0和输出完整保留 |
| 显式输入跨进程 | `test_real_new_process_restore_explicit_inputs`在0、0.5、2、25、28、168、672、720另起进程续跑，含飞行、待修、在途、在修 |
| 真实同刻和浮点边界 | 同刻故障、维修完成、到货、任务结束、再启动；0.3/0.6/1/168/672/720及相邻 `nextafter` 边界，零交期/零维修因果链，720物理完成及旧启动规则，均严格分类与事件顺序 |
| reset、RNG和隔离 | 默认输入、空任务、显式任务、初始寿命override；四流续抽一致；clone推进/修改/存取/随机消费及导出字典不污染父实例 |
| 损坏输入拒绝 | 必填/版本/枚举/NaN/非法无穷/引用/容量/守恒/RNG/log/history/缺失事件均覆盖；极大fleet_size先检查实际记录数，不按损坏数量分配内存 |
| 独立审查 | `review/REVIEW.md`；57项独立探针和7项比较程序反例通过；5项初轮缺陷已修，修复前后证据均保留 |
| 补充隔离边界 | `review/checkpoint-final-probe/results.json`：6种输入组合的分支推进/保存/恢复/RNG隔离，以及reset(None/17)匹配全新实例；实际浮点微小超时长累计案例通过 |
| 基准与源码身份 | `baseline-comparison.json`逐文件哈希、差异和最终runtime源码指纹一致性；原tests/configs/audit-record保持不变 |

原始history可因非事件暂停多出采样。比较程序只删除非业务时刻、与前状态完全相同的中间采样，
保留初末行、所有状态变化、所有业务时间行及同刻行；events日志从不删改。事件ID、时刻、
优先级、sequence、任务分类、离散账本与整数计数严格一致。只有累计积分/飞行/等待/由其导出的比例
使用 `rel_tol=1e-12, abs_tol=1e-10`。容差没有扩大；相邻浮点时刻不合并。

独立探针实际找到连续累计飞行比当前时刻多 `2.2737367544323206e-13` 的合法加法舍入案例；
恢复按上述积分一致性检查接受它，故障/完成锚定时刻仍严格比较。它不构成更改时间边界的理由。

## 旧基准保持情况

两个seed42场景与改前基线的 `config.json/events.csv/missions.csv/summary.json/states.csv/repairs.csv/orders.csv/state.json/overview.png`
均逐字节相同（每场景9文件）。仅 `runtime.json` 因新增溯源字段预期变化。科学指标没有变化：

| 指标 | default | stress |
| --- | ---: | ---: |
| 提供任务 | 101 | 621 |
| 成功 / 失败 / 未执行 / 仍执行 | 91 / 8 / 0 / 2 | 36 / 44 / 540 / 1 |
| 实际飞机飞行小时 | 747.8118988514635 | 801.318016009783 |
| 可用度（空闲+飞行） | 0.9932787919825568 | 0.190471413463148 |
| 期末在库 / 在途 / 待修 | 4 / 0 / 0 | 7 / 3 / 8 |

上述18组跨进程与20组恢复冒烟的实际差异和最大绝对差保存在 `baseline-comparison.json`，
不以“允许容差”替代记录实际差异。`validation-final/` 和 `checkpoint-verification-final/` 是
最后一处损坏数量防护修正前的成功运行，保留作开发证据；**对应最终源码的验收以accepted目录为准**。

## 实际命令

以下命令均已实际执行，退出码0；具体argv和日志在同名 `*-command.json` 与 `*.log`。

```powershell
& 'D:\conda_envs\sim-jev\python.exe' -m unittest discover -s tests -v
& 'D:\conda_envs\sim-jev\python.exe' scripts/validate.py --output runs/phase2a-20261102-1cd05986/validation-accepted
& 'D:\conda_envs\sim-jev\python.exe' scripts/checkpoint_verify.py --output runs/phase2a-20261102-1cd05986/checkpoint-verification-accepted
& 'D:\conda_envs\sim-jev\python.exe' runs/phase2a-20261102-1cd05986/compare_baseline.py
```

跨进程示例的实际argv在 `checkpoint-verification-accepted/commands.json`，包含暂停168保存JSON、
该进程退出后创建恢复进程、恢复至720以及独立整段进程。脚本不是在同一实例中继续run来代替恢复。
所有持久新结果在本任务runs目录，历史目录未覆盖。

## 假设、限制与审计包

checkpoint是执行真状态，包含隐藏未来；其可恢复性不赋予策略读取权限。
不支持从旧state.json迁移，不承诺跨任意NumPy版本逐位一致，不支持多线程同时修改实例。
默认模型仍为单LRU合成基准，准确订单ETA为公开承诺，补充材料缺失不阻断本轮开发。
周级cohort补记与延迟奖励结算是明确标记的项目约定，不冒称原论文训练实现。

Git HEAD仍为 `cb86953963fc50b1cc440f5c38712a8fafa50669`，dirty=true；源文件仍主要未跟踪，
未自动暂存/提交/推送。只使用单命令safe.directory，未修改全局Git配置。完整Git状态随包保存。

审计ZIP创建到 `G:\sim-jev-audit` 的新文件名，包含对应完整源码、配置、测试、本文及来源/契约、
研究计划只读副本、恢复示例、实际日志/元数据/checkpoint、开发与修复证据及SHA256清单。
排除.git对象、环境、缓存和密钥。ZIP生成后重新读取所有条目核验清单，另输出ZIP自身SHA256。
本轮止于2A验收；后续依次为周承诺、实体键控随机数、合法规划情景。
