# 阶段 2D-R1：累计与恢复协议（实现前登记）

旧 `legal-weekly-request-v1`、weekly checkpoint schema 1 和默认构造仍按原累计规则运行；加载旧快照不会升级。旧科学字节基准属于该 legacy 协议。

新实验显式使用 `event-anchor-binary64-v2`。累计锚点只在引擎处理完物理事件或固定日/周控制时点后提交；普通 `run(until=t)` 只是从最后锚点计算到 t 的投影，不把投影结果作为下一次积分的起点。锚点记录时间、累计指标、飞机与任务飞行量、维修等待量、分窗累计量，进入 weekly checkpoint schema 2。每次推进从同一锚点、同一实体顺序、同一 binary64 运算恢复计算；不舍入公开输入、不给缓存增加容差，也不改变随机协议或物理事件规则。

新请求显式标识 `legal-weekly-request-v2`；合法规划分支从请求版本选择相同累计协议。新旧请求及 cache 身份必然分离，不要求跨协议 hash 相同。checkpoint schema 2 必须验证锚点形状、时域、实体对应及从锚点重算的当前累计量，不接受缺失锚点或未知协议。公开状态仍保持原字段与顺序。

执行停点与主契约统一为 31.3、47.125、95.75、191.375、335.2、511.625、671.875 h（主契约登记早于本次实现运行）；先固定时点，再根据截至暂停时刻可观察状态记录在飞、等待、维修及在途覆盖，不读取未来隐藏真值选择暂停。运行包括普通暂停、严格 JSON 新进程恢复、连续多次恢复、local-direct/local-des 生产缓存路径的明确离线控制和无学习基线。

跨版本验证冻结命令与同外生实体样本，严格核对业务事件、任务分类、义务、库存/维修离散账本、随机 manifest；单独报告累计数值差异及请求/候选/情景身份变化。旧浮点字节不作为新累计协议的验收条件，也不通过删除差异声称字节一致。

已执行结果（证据位于 `runs/phase2d-r1-20261103-191950/recovery-f3/`）：

- 封存源码原审计探针重现 scarce/13/local-direct 在 31.3 h 暂停后 168 h 的 `local_cache_miss`。整段 5 份缓存，恢复只完成 1 次命令；无模型或 HTTP 调用。
- `counterexample-numerics-and-identities.json` 保留 legacy 的 R000002 缺件等待 `100.49270984916274` / `100.49270984916272` 差异，以及不同 request、pool、planning hash。相同实验在 v2 下公开量和上述三个身份全部精确相同。
- 新回归 5 项综合测试通过，61.196 秒，退出码 0；另运行原 weekly 回归 52 项通过。新测试覆盖四个无学习基线和两个 v2 local 方法。local 控制使用明确的离线合成评分、生产持久缓存和 resolver，每方法连续 7 次新进程恢复，禁止 socket 连接，恢复生成数/元数据数均为 0。
- 持久化 local 控制的所有 checkpoint、离线响应、合成预算账本、新进程日志与最终比较保存于 `OFFLINE-SYNTHETIC-local-direct/` 和 `OFFLINE-SYNTHETIC-local-des/`。这不是 GPU 或模型有效性的证据。7 个停点实际覆盖在飞、缺件等待、维修中和库存运输中状态。
- balanced/scarce、seed13 的冻结命令跨版本检查中，完整事件、CRN manifest、任务义务/队列账本相同。连续运行的累计值在这两个实例没有差异；请求/候选身份因显式协议版本改变而不同。新基准独立保存，未覆盖 legacy golden。

普通暂停增加的无状态变化 history 采样行按既有 `normalized_history` 比较规则处理；公开累计值、请求、候选、情景、缓存键、命令与完整事件不删除字段、不放宽比较容差。v2 checkpoint 的历史请求公开值另增加与业务日志重建值的严格相等检查；legacy 原恢复检查保留。

实际执行命令：

```powershell
Set-Location -LiteralPath 'G:\sim-jev'
& 'D:\conda_envs\sim-jev\python.exe' -m unittest discover -s 'G:\sim-jev\tests' -p 'test_phase2d_r1_pause.py' -v
& 'D:\conda_envs\sim-jev\python.exe' -m unittest discover -s 'G:\sim-jev\tests' -p 'test_weekly*.py' -v
& 'D:\conda_envs\sim-jev\python.exe' 'G:\sim-jev\runs\phase2d-r1-20261103-191950\recovery-f3\legacy-counterexample.py'
& 'D:\conda_envs\sim-jev\python.exe' 'G:\sim-jev\runs\phase2d-r1-20261103-191950\recovery-f3\verify_v2_evidence.py'
```

证据生成脚本首版在已完成 local-direct 恢复后因汇总字段名错误退出（`branch_count` 应为 `len(branches)`）。保留首版脚本为 `verify_v2_evidence_v1_failed.py`；修正汇总后读取已有完整 checkpoint，未重做该方法选择，再完成 local-des 证据与汇总。
