# sim-jev：机队任务与保障离散事件仿真

当前阶段2C已实现：**合法公开状态与规则 → 统一候选池 → Jev Choice预筛 → 同预算短窗DES → 周级承诺 → 固定日执行器**。
底层任务、飞行故障、备件、维修和可用度共用显式事件机制，支持实体键控随机数、内部checkpoint恢复与运行溯源。

**验证状态：离线引擎、候选前瞻、人工stub及本地HTTP协议测试已通过验收；真实Jev smoke和闭环pilot尚未验证。**
live、真实响应重放与人工stub分别记录证据。没有真实TypeSafe响应时，不能据离线结果评价Jev效果。
当前接口与边界见 [2C契约](docs/PHASE2C_CONTRACT.md) 和 [真实接入验收](docs/PHASE2C_LIVE_ACCEPTANCE.md)；
测试数量与结果以当前实际运行日志为准。

**配置全部为合成示例参数。本项目不是原论文的严格复现，运行结果不是对真实机队的验证。**
尚未实现PPO参考、长期终端价值、真实PHM/RUL观测以及论文成本/J720；短窗结果不构成长期收益或安全保证。

## 快速运行

使用Python 3.11或更高版本，在仓库根目录执行以下命令。`python`应指向你选择的Python环境，
不要求开发机的盘符、Conda名称或解释器路径：

```powershell
python --version
python -m pip install -e .
python -m sim_jev --config configs/default.json --seed 42 --output runs/my-demo
python -m sim_jev --mode weekly --config configs/weekly_default.json --output runs/my-weekly-demo
python -m unittest discover -s tests -v
```

安装后，基本示例和测试不需要历史`runs/`、外部审计ZIP、本机G盘资料或TypeSafe凭据。
完整离线决策管线实验默认使用人工stub，可另行运行：

```powershell
python scripts/phase2c_experiment.py --output runs/my-offline-pilot
```

输出目录必须不存在或为空；重复运行请换一个目录名。程序拒绝覆盖已有结果。
`--seed` 覆盖 JSON 内的 seed，并写入输出的实际配置。默认输出目录为 `runs/demo`。

可重复执行基本验收（单元测试、两份CLI示例、两场景各seed 0—9及文件核验）：

```powershell
python scripts/validate.py --output runs/my-validation
```

阶段验收记录见 [2C验收](docs/PHASE2C_VALIDATION.md) 与 [真实接入验收](docs/PHASE2C_LIVE_ACCEPTANCE.md)；
较早阶段的记录见 [docs/VALIDATION.md](docs/VALIDATION.md)。科学结果不使用运行日期或墙钟时间。

`scripts/phase2c_compatibility.py`和`scripts/phase2c_science_regression.py`是**可选的历史档案核验**，
需要另行提供历史审计ZIP；前者保留开发机G盘的默认档案路径，换机器应显式指定`--archive`。
这些ZIP及历史实验输出不随源码提交，也不是新checkout基本示例和单元测试的前置条件。

## 历史开发环境与VS Code

以下保留开发机环境记录；这些绝对路径仅适用于原机器。其他机器使用上面的通用命令，
在VS Code中选择自己的Python 3.11+解释器。

1. 在 VS Code 打开 `G:\sim-jev`。
2. 命令面板执行 **Python: Select Interpreter** → **Enter interpreter path**，选择
   `D:\conda_envs\sim-jev\python.exe`。
3. 在终端检查：

   ```powershell
   & 'D:\conda_envs\sim-jev\python.exe' -c "import sys; print(sys.executable); print(sys.version)"
   ```

验证环境为 Python 3.11.18、NumPy 2.4.6、Pandas 3.1.0、Matplotlib 3.11.2。
已安装的 SimPy 和 SciPy 在本阶段引擎中不使用；无需 PyTorch、Gymnasium 或 Jev SDK。
本次未写入或覆盖 VS Code 设置。机器专用的 `.vscode/` 已忽略，不应提交解释器绝对路径。
从项目根执行 `python -m sim_jev` 即可，无需安装本项目。

## 旧模式模型规则（阶段2A历史接口）

- 默认 12 架同型机，每机一个关键可更换部件，时间单位统一为小时。
- 任务按泊松过程到达，时长和价值均匀抽样、需机数为闭区间离散均匀抽样。
  也可以直接提供确定性任务列表。任务只在计划时刻启动，飞机不足则未执行。
- 空闲飞机按 ID 稳定排序派出；必须全部参飞机完成才成功。任一飞机故障立即中止整项任务，
  健康飞机回到空闲，故障飞机立即进入地面保障；不模拟返航、救援。
- 故障寿命服从按飞行小时计的指数分布；只累计实际飞行暴露。
  地面、等件、等机位、维修均不消耗寿命。换件完成后恢复如新。
- 维修按故障请求时间及维修 ID 先到先修；同时需要一件备件和一个机位。
  缺件不占机位，实际开始维修时扣件一次。
- 每 24 小时检查一次补货（含 t=0，可配置）。库存位置为
  `在库 + 在途 − 等待维修的飞机数`，订购量为 `max(0, 目标 − 库存位置)`。
  备件下单即确认并公开交期，第一阶段没有确认后的额外延误。
- 处理截止时刻的事件；此后不清空队列。任务、维修、待修和在途订单保持未完成状态。
- **可用 = 空闲 + 执行任务中**，与“当前可派的空闲飞机”不同。

同刻优先级、等待时间归因、指标分母、零值行为和观测边界详见
[docs/MODEL_ASSUMPTIONS.md](docs/MODEL_ASSUMPTIONS.md)。

## 旧模式配置

[configs/default.json](configs/default.json) 是常规合成示例；
[configs/stress.json](configs/stress.json) 增大任务/故障负荷、延长保障时间并减少机位和初始库存。

| 参数组 | 主要字段 | 单位/含义 |
| --- | --- | --- |
| 基础 | `horizon_hours`, `seed`, `fleet_size` | 小时、非负整数种子、飞机数 |
| 任务 | `mission_rate_per_hour` | 任务/小时，0 表示不随机生成任务 |
| 任务属性 | `mission_duration_min_hours/max_hours`, `mission_aircraft_min/max`, `mission_value_min/max` | 时长、需机数、合成价值点数 |
| 故障 | `failure_rate_per_flight_hour` | 每飞行小时的危险率，0 表示无限寿命 |
| 维修 | `repair_bays`, `repair_duration_min_hours/max_hours` | 机位数、均匀维修时长 |
| 备件 | `initial_spares`, `reorder_target`, `reorder_interval_hours` | 初始库存、库存位置目标、检查周期 |
| 供货 | `lead_time_min_hours/max_hours` | 均匀交期，订购时抽样并确认 |

字段表中的 `min_hours/max_hours` 表示两条完整字段，如 `mission_duration_min_hours` 和
`mission_duration_max_hours`。完整值以 JSON 为准；省略字段使用 `Config` 中明确的合成默认值。
未知字段、布尔值冒充数字、负数、非有限值、上下限倒置等会报出明确错误。
危险率是强度而非概率，可大于 1；本阶段没有独立概率配置字段。

## 旧模式Python接口与确定性输入

```python
from dataclasses import replace
from sim_jev import Config, MissionSpec, Simulation

config = replace(Config(), horizon_hours=12, fleet_size=2,
                 failure_rate_per_flight_hour=0)
missions = [MissionSpec("test-1", start_time=1, duration_hours=3,
                        aircraft_required=2, value=100)]
sim = Simulation(config, missions=missions)
sim.run(until=2)                 # 包含 t=2 的事件，之后可继续推进
observation = sim.get_observation()
summary = sim.run()              # 推进至配置截止时刻
assert summary["missions_success"] == 1
sim.reset(seed=42)               # 回到 t=0，保留构造时输入任务
```

`missions=None` 表示随机任务，`missions=[]` 明确表示零任务。
测试可用 `initial_lifetimes=[...]` 指定每机的初始飞行寿命；它是实验输入，
不代表策略能观测真实寿命，维修后仍按配置的故障分布重新抽样。

`reset()` 重建全部状态及四条独立随机流；`run(until)` 支持单调推进，不能超过配置时域。
任务、故障、维修和供货随机流由 `SeedSequence(seed).spawn(4)` 分离。
相同配置、seed、确定性输入和依赖版本可重现事件日志及科学指标。
分段推进不会改变离散事件结果，积分的末位浮点数可能有机器精度差异。

`get_observation()` 仅返回当前状态、已公布任务和已确认订单；不返回真实剩余寿命、未来故障、
随机数状态、未公布任务或完整事件队列。任务在计划开始时才公布，本阶段没有提前任务预测。

`export_state()` 导出内部审计状态，包括队列和随机流；`state.json` 不是策略输入，仍不支持恢复。
恢复必须使用独立的 `InternalCheckpoint` 协议：

```python
sim = Simulation(Config())
sim.run(until=168)               # 包含168时刻产生的全部因果事件
sim.save_checkpoint("runs/pause-168.json")  # 新文件，拒绝覆盖
restored = Simulation.load_checkpoint("runs/pause-168.json")
restored.run()                   # 精确继续同一个执行世界
branch = restored.clone()       # 可变对象及四条RNG全部隔离
data = restored.to_checkpoint() # 严格JSON字典，不共享原实例数据
another = Simulation.from_checkpoint(data)
```

新建或 `reset()` 后须先 `run(until=0)` 才能保存；handler 内和失败中断后的实例不可保存。
`reset()` 保留原显式任务/初始寿命约定，`None` 随机任务与 `[]` 零任务保持区别。
checkpoint 包含真实寿命、未来队列和执行随机状态，只能用于内部恢复/同世界重放，
**永远不能直接作为可部署规划器输入**。`clone()` 不产生合法预测情景。
无限寿命使用版本化的 `{"$float": "+inf"}` 标签；普通非有限数、缺失字段、损坏关系拒绝恢复。

真正退出进程再恢复（请使用新的结果目录和文件名）：

```powershell
& 'D:\conda_envs\sim-jev\python.exe' -m sim_jev --config configs/default.json --seed 42 --until 168 --output runs/pause-demo --checkpoint-out runs/pause-demo.json
& 'D:\conda_envs\sim-jev\python.exe' -m sim_jev --resume runs/pause-demo.json --output runs/resume-demo --checkpoint-out runs/resume-final.json
& 'D:\conda_envs\sim-jev\python.exe' scripts/checkpoint_verify.py --output runs/checkpoint-verification
```

最后一个脚本自动运行整段/暂停/新进程恢复对照及20组seed恢复冒烟，保存命令、退出码、日志、
差异和输出哈希。`--resume` 与 `--config/--seed` 互斥；恢复以checkpoint内配置及输入为准。
浮点积分容差为 `rel_tol=1e-12, abs_tol=1e-10`，完整业务事件、边界分类和整数计数严格一致。

## 旧模式输出文件

每次运行在指定目录输出以下文件，默认位于项目 `runs/` 下：

| 文件 | 内容 |
| --- | --- |
| `config.json` | 实际配置及 seed、合成参数标记 |
| `events.csv` | 事件时间、类型、实体、前后状态、详情；过期事件标为 `stale_event` |
| `missions.csv` | 任务属性、执行飞机、结果、实际时长与飞行小时、获取价值 |
| `summary.json` | 任务、价值、故障维修、等待、可用度、库存及在途汇总 |
| `states.csv` | 初始、事件后及截止状态，用于阶梯图和独立积分核验 |
| `repairs.csv`, `orders.csv` | 维修请求/开始/完成及等待累计；订单数量/交期/状态 |
| `state.json` | 含隐藏状态的内部审计导出，`resume_supported=false` |
| `runtime.json` | 模型/快照/随机协议版本、实际命令、有效配置哈希、源码内容指纹（含未跟踪源码）、Git状态、解释器和依赖版本；不参与科学指标 |
| `overview.png` | 飞机状态数量及库存/在途/待修需求变化图 |

零任务时 CSV 仍带表头。输出使用 UTF-8；列表字段为 JSON 单元格。
`runs/`、缓存、环境、构建产物和密钥类文件已加入 `.gitignore`。

## 代码位置与路线

```text
sim_jev/      config、models、policy、engine、metrics、reporting、CLI
configs/      常规与压力合成配置
tests/        确定性场景、非法输入、随机复现、真实 CLI 与输出测试
scripts/      完整验收脚本
docs/         实施路线、假设、测试清单、实测验证记录
audit-record/ 原有研究背景记录（仅本地保留，Git忽略）
runs/         本地实验输出（Git 忽略）
```

[docs/PLAN.md](docs/PLAN.md) 保留第一阶段路线记录。
阶段2A历史合成基准冻结为 `synthetic-single-lru-v1`，四流协议为 `numpy-seedsequence-four-stream-v1`；
现有配置与科学指标语义不变。来源对照见 [docs/PAPER_MODEL_MAP.md](docs/PAPER_MODEL_MAP.md)，
当时拟定的周承诺、实体键控随机数与信息边界见 [docs/RESEARCH_CONTRACT.md](docs/RESEARCH_CONTRACT.md)，
实际2A验收见 [docs/PHASE2A_VALIDATION.md](docs/PHASE2A_VALIDATION.md)。
以上为阶段2A的历史交付记录；阶段2B和2C增量见下文。


## 阶段 2B：独立合成周模式

新增 `WeeklySimulation`：周承诺、固定日派机/FIFO维修/单次补货、公开动态任务规则、冻结cohort账本、
实体键控随机数和JSON跨进程恢复。旧 `Simulation`、旧配置和2A快照继续工作。
详细接口与边界见 [阶段2B契约](docs/PHASE2B_CONTRACT.md)，实际验证见 [阶段2B验收](docs/PHASE2B_VALIDATION.md)。

```powershell
python -m sim_jev --mode weekly --config configs/weekly_default.json --output runs/weekly-demo
python -m sim_jev --mode weekly --config configs/weekly_stress.json --until 168 --output runs/weekly-pause --checkpoint-out runs/weekly-pause.json
python -m sim_jev --resume runs/weekly-pause.json --output runs/weekly-resume
python scripts/weekly_verify.py --output runs/weekly-verification
```

输出使用新目录；示例名称已存在时请换名。周模式末窗是 `[672,720)`，恰在720开始的任务不offer；
720仍处理已有物理事件。名义计划可行不保证实际履约。阶段2B交付时没有PlanningScenario或Jev；
后续2C实现见下文。PHM/RUL、价值训练、PPO、成本和J720仍未实现。

## 阶段 2C：合法候选、Jev Choice 与同预算 DES

在旧周/日/事件结构上增加白名单 `DecisionRequest`、条件 `PlanningScenario`、统一可行候选池、
Jev Choice Top-K 和同预算短窗 DES，并保留旧模式/快照。模型固定 `jev-1.14.0`，支持显式有界
live、严格真实缓存重放和人工 stub；三者证据分开。没有终端价值或长期收益结论。

见 [2C契约](docs/PHASE2C_CONTRACT.md)、[Jev协议](docs/PHASE2C_JEV_PROTOCOL.md)
及 [2C实际验收](docs/PHASE2C_VALIDATION.md)。

```powershell
python scripts/phase2c_experiment.py --output runs/phase2c-new-pilot
python scripts/jev_smoke.py --allow-live --max-http-requests 3 --total-timeout-seconds 120 --output runs/phase2c-new-live-smoke
```

live 仅在运行时读取 `TYPESAFE_API_KEY`。缺凭据时离线实验和stub验证仍可执行，不能将其计作真实Jev科学结果。

本轮增加严格、有阶段全局预算的 `scripts/phase2c_live.py fixed-state/pilot`，以及零网络的
`replay/resume`。完整命令、预算、计量兼容及证据边界见
[2C真实接入验收](docs/PHASE2C_LIVE_ACCEPTANCE.md)。没有真实响应时，live smoke/pilot 仍为待验证。
