# 阶段 2C：论文周/日/事件结构上的 Jev 决策变体

本实现沿用周承诺、固定日执行器与事件物理层，替换周级选择端。它是合成研究变体，没有复现原论文 PPO，没有长期价值函数、PHM/RUL、成本模型或 J720。实际完成等级与数字见 `PHASE2C_VALIDATION.md`，不能以 stub 测试替代真实 Jev 效果数据。

## 决策路径和数据权限

`_publish_request` 在该周点物理闭包之后公布任务/规则，生成 `DecisionRequest`；同步 `resolve_decision` 返回一个命令及决策记录；统一 `validate_command` 后 `_commit_command` 只提交一次；随后原日命令、派机和物理事件继续执行。

`ModelParameters` 显式列出机队数、机位数、指数故障率、维修/交期上下界、订货目标、24h节拍、720h时域和固定日执行器版本。执行 seed、任务生成参数、initial_lifetimes、真实剩余寿命、维修 due_at、队列与 manifest 不进入请求。观察内每一类嵌套记录也用字段白名单；请求身份只对合法内容求哈希。

执行 `InternalCheckpoint` 仍可包含完整真值，用于同世界恢复和内部审计。`PlanningInput` 只接受合法请求和独立规划种子/情景编号。不能将执行快照或 Simulation 传入情景构造器；候选间只复制已经合法构造、domain=planning 的世界。

旧 mandatory-first/scripted 经过同一个 resolver，保持旧策略描述、模型/schema/随机版本以及业务日志形状。新 `DecisionPolicySpec` 的版本为 `candidate-choice-des-v1`，新请求为 `legal-weekly-request-v1`。新策略才增加带显式协议版本的决策记录；旧 2A 和 2B JSON 快照不迁移、不改全局旧版本。

## 合法条件情景和终点

详细条件分布、origin账本与独立核算见 [规划契约](PHASE2C_PLANNING.md)。健康/在飞部件重抽独立指数条件残余寿命；零率为无穷。待修/在修维持故障状态。在修总服务条件于已服务且尚未完成；固定时长采用精确可推导完成时刻，空支持报错。公开订单 ETA 原样续行，已在飞任务保留飞机分配和原计划终点。公开未来规则可只含 mandatory ID，不补读未来任务。

所有候选共享同一规划情景的外生键；候选 ID/顺序/算法不进入物理噪声键。规划与执行 RNG 完全隔离，记录 origin、公开输入哈希、情景 ID、条件分布与随机协议。原物理处理器和积分负责实际推进，没有第二套旁路仿真。

`run_window(end)` 排空 end 的物理事件和同刻因果链，再停在 end 的新公布/周命令/日命令/派机之前。它不会用 nextafter 跳过边界，末窗 672→720 为48h，720终结只记一次。非720的这种边界不是可保存执行 checkpoint 的稳定屏障；规划世界使用独立规划产物，后续普通 `run()` 才进入下一窗控制阶段。

## 候选、分数与预算

候选协议和摘要单位见 [候选契约](PHASE2C_CANDIDATES.md)。目标 M=16，保留 mandatory-first 参考，结合价值/AFH/短任务贪心、局部增删交换和有限哈希随机组合；每项用同一校验器，去重并按命令内容生成稳定 ID。池不足时如实记录实际 M；不能在预筛前调用 DES 或制造未来标签。筛选入口重新计算候选摘要，拒绝属于旧状态的资源/任务摘要。

| 方法 | 选择方式 | 默认 DES 次数 |
| --- | --- | ---: |
| mandatory-first | 原规则贪心参考 | 0 |
| random-des | 独立规划种子哈希排序 Top-K | 32 |
| heuristic-des | 冻结公开计划分数排序 Top-K | 32 |
| jev-direct | 有效 Choice 的 choice；失败时固定启发式 | 0 |
| jev-des | 概率降序、稳定 ID 破同分取 Top-K，再 DES | 32 |
| direct-des | 稳定 ID 次序循环分配全部候选 | 32 |

默认 K=4、B=8。实际 M>1 时四种 DES 方法预算均为 `min(K,M)*B`；直接 DES 按稳定 ID 逐个循环，每一轮 scenario_index 增1。M=16时每候选2次。空池走明确参考命令、单候选直接提交，DES=0；Jev 同时记录 `api_skipped_reason`，无需凭据。mandatory/carry 不可行时所有方法获得同一个确定性 fallback 候选。

每分支到相同真实窗口终点，以当前 offer cohort 已实现 `DSR+RCR` 评分，零分母分项为0并保留 validity。拒绝仍在分母，跨窗旧任务占资源但收益不混入当前分母，跨终点任务仍 unresolved。平均情景分数最大者获选，稳定 ID 破同分。没有 V、终端价值或高预算质量标签；参考候选在池内不保证所选策略不退化。全720h继续先累加各 cohort 分子/分母再计算指标。

## Jev 与恢复

[Jev协议](PHASE2C_JEV_PROTOCOL.md) 保存官方文档URL与2026-11-02核对日期。固定模型 `jev-1.14.0`，POST `https://api.typesafe.ai/v1/systemone`，Bearer 仅从运行时环境 `TYPESAFE_API_KEY` 读取。只做 Choice。概率和 confidence 是选择诊断，不是故障率或安全约束。无阈值自适应增预算。

live 仅指默认 HTTP transport 的真实响应；real_replay 只认完整请求/model/template/候选映射匹配的真实缓存；stub 为稳定 ID 人工 fixture。注入测试 transport 单独标记，不能进入真实缓存。失败/非法响应按预先冻结的启发式 fallback，严格 smoke 直接报错，所有失败和混合策略结果分别记录。

429/529有界退避，遵守 Retry-After；认证/schema等错误不盲重试。HTTP尝试在发送前原子持久化，超时/未持久化响应保留 uncertain；不承诺外部服务 exactly-once。总调用数含重试，运行总时限含等待。实际 usage、真实响应原usage、缓存命中和时延分开记录；DES预算与端到端费用分开报告。

运行时适配器/凭据不保存到 checkpoint。恢复已提交命令不调用过去 resolver/HTTP；未来新请求需要重新注入运行时服务。恢复独立重建历史公开状态，检查请求、候选摘要、响应协议、分支分配/计数/分数/赢家和命令。此为一致性校验，不是外部来源认证。

固定响应/确定性恢复比较完整命令、业务事件、配置/reset输入、cohort及模型逻辑状态；真实网络耗时、缓存命中、HTTP次数保留各自事实，仅在逻辑比较投影中分离，不伪造整个checkpoint字节相同。

## 运行入口

使用既有 `D:\conda_envs\sim-jev\python.exe`，所有目标必须是新目录。完整离线实验与审计实际命令由验收报告记录。

```powershell
& 'D:\conda_envs\sim-jev\python.exe' scripts/phase2c_experiment.py --output runs/phase2c-new-pilot
& 'D:\conda_envs\sim-jev\python.exe' scripts/jev_smoke.py --allow-live --max-http-requests 3 --total-timeout-seconds 120 --output runs/phase2c-new-live-smoke
```

通用周配置可以保存 `DecisionPolicySpec.to_dict()` 作为 policy。live配置运行时必须明确：

```powershell
& 'D:\conda_envs\sim-jev\python.exe' -m sim_jev --mode weekly --config <live-policy-config.json> --jev-store <new-response-store> --allow-live --max-http-requests 10 --http-total-seconds 120 --output <new-output>
```

缓存不进入源码身份；正式审计包收录脱敏请求响应和预算账本，不收运行时缓存目录、Git对象、环境或密钥。打包作用域与真实无Git解压验收见 [打包契约](PHASE2C_PACKAGE_SCOPE.md)。

本轮严格 live 验收使用 `strict_jev=True`，系统性失败立即停止阶段；以上常规 fallback 约定
不用于填充严格 live 成果。新入口与独立预算见 [真实接入验收](PHASE2C_LIVE_ACCEPTANCE.md)。
