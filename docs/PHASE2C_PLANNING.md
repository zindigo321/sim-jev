# 2C 合法条件规划与短窗评分

实现入口为 `PlanningInput(DecisionRequest, planning_seed, scenario_index)` 与
`PlanningScenario(inputs)`。仅接受版本化公开请求；不接收执行 Simulation、执行 checkpoint、
真实 seed、真实寿命、维修 due_at、未来队列或未公布任务。输入按值复制，合法请求规范 JSON
的 SHA256、独立规划 seed 和情景号共同确定情景身份。`domain=planning`；候选 ID、算法名、
候选排列与调用次数不进入外生随机键。

模型版本 `conditional-weekly-single-lru-v1`，输入版本 `legal-decision-planning-input-v1`，
条件分布版本 `exponential-memoryless-uniform-survival-v1`，随机协议沿用显式 planning 域的
`sha256-entity-counter-v1`。这些身份与原执行模型版本并存，未修改旧 2A/2B 模型语义。

`PlanningSimulation` 直接从公开事实建立实体，复用已有故障、任务结束、到货、维修及日执行
处理器。不调用完整执行构造器或 checkpoint 恢复。已公开未来规则可以带有尚无任务属性的
mandatory ID；构造不为此读取下一窗任务。初始账本只有真实 origin 时刻的一条状态记录，
过去事件不被伪造。公开历史实体全部保留以维持原实体编号、既有承诺与占用。

- 正常/在飞部件按公开指数危险率抽条件残余飞行寿命；指数无记忆性使其与已存活年龄独立。
  零危险率对应无穷。待修与在修部件的寿命为零，直到原维修完成处理器执行换件。
- 在修服务按 `Uniform(low,high) | 总服务时长 > 已服务时长` 抽样。固定服务的完成时刻为
  `started_at + duration`。条件支持为空或无法表示严格未来的抽样时刻时明确报错；不读取真值
  补救。时刻比较为严格 binary64，不合并相邻浮点数。
- 已确认在途订单按公开 ETA 和数量续行，无额外隐蔽延误。等待维修遵循原 FIFO 和资源约束。
- 运行任务保留原分配、已发生启动及原计划结束。既有承诺占资源，当前窗提供集冻结；未公告
  执行任务不进入场景。

origin 记录合法公开状态、规则上下文、模型参数与指标偏移。累计已飞小时保留以满足共享物理
守恒；维修/订单累计计数从公开实体推导。初始备件守恒偏移由
`on_hand + 已启动维修数 − 已到货件数` 推导，不读取真实 Config。可用/空闲/等待积分从 origin
开始；branch 的 `metrics_delta` 扣除记录的偏移，绝不把旧累计暴露重复算作本窗暴露。

`evaluate(command)` 深拷贝已经合法构造的规划世界，共用 `validate_command` 与唯一提交门，
当前候选只提交一次，然后调用 `run_window(window_end)`。终点物理事件及其同刻后果全部排空，
再停于下一窗公布、日命令及派机之前。末窗 672→720 为 48 小时，720 终结账本只有一次。
重复、换序、插入其他候选评估不会改变同一候选/情景，也不改变源场景或真实执行世界。

评分版本 `current-offer-realized-dsr-plus-rcr-v1`：仅用当前 offer cohort 在实际终点已经实现的
`DSR + RCR`。拒绝任务仍在 offered 分母；零分母评分分项为 0，并单独保留 validity=false。
旧跨窗任务的收益不进入本窗分子/分母；未结束任务保持 unresolved。没有终端价值、长期价值
训练或高预算质量标签，此短窗得分不是全周期最优性或长期保障收益证明。

每个分支产物包含版本、情景身份、合法输入哈希、实际模拟时长、唯一命令、各评分分项、当前
cohort 分类、独立 planning 随机清单、完整分支事件/状态、origin 及最终物理实体。它是规划
审计产物，明确禁止当作执行 checkpoint 恢复。`scripts/planning_verify.py` 从 origin 公开事实
与分支事件独立重建物理、库存、积分、当前 cohort 和得分，不调用引擎 summary 自证。

M2 实际测试命令：

```powershell
& 'D:\conda_envs\sim-jev\python.exe' -m unittest discover -s tests -p 'test_phase2c_planning.py' -v
```

日志及在修/等待/在途/跨窗的完整示例位于 `runs/phase2c-20261102/m2-planning/`；最终总测试
状态以 2C 汇总验收记录为准。

持久决策记录现在对完整 Metrics schema、有限非负积分、严格非负整数计数和时窗×机队的
delta 容量上界进行恢复期核验；重复故障次数没有一机一次的错误上限。合法旧记录无需迁移。
`scripts/phase2c_branch_replay.py` 从合法 request、planning_seed、scenario_index 和候选命令
重建分支，核对 `physical_artifact_sha256`、完整摘要及独立事件/物理核算。
固定状态审计全部分支，闭环按预先冻结的“每次决策第0支”抽样。产物和审计 DES/模拟小时/墙钟
单列，不反馈选择，不计入决策时的 MC 预算。摘要与重放是内部一致性证据，不是外部来源签名。
