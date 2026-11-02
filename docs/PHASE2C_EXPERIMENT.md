# 2C 小型配对管线实验

`scripts/phase2c_experiment.py` 的默认实验为两个冻结合成场景（`configs/phase2c_balanced.json` 与 `phase2c_scarce.json`）、各环境 seed 0–4、六种方法，共 60 个闭环运行。两场景均每个窗口提供九个显式任务，包含相互重叠、需机数取舍、AFH 限制、mandatory 与跨窗任务。末窗为 672→720 的 48 h。任务/义务只在窗口公布，未来模板不传给决策器。

同场景/环境 seed 的全部方法使用完全相同的任务和规则；脚本记录并逐项比对 `paired_task_rule_sha256`。环境 seed 只控制执行物理随机性。规划种子默认固定为 20261102，候选协议、规则、M=16/K=4/B=8 均一致。mandatory-first 和 Jev-direct 的 DES 预算为零；其余四个方法每窗默认32次分支，池不足时按同一预算规则记录实际数，不填充或追加标签。

默认 Jev 模式为 `stub`，相关行强制标记 `evidence_role=pipeline_only_stub`，`valid_jev_decisions=0`；有效人工协议响应数量与真实 Jev 有效率分列。可用 `--jev-mode real_replay --response-cache <真实响应目录>` 精确重放已持久化真实响应，未命中显式采用冻结启发式 fallback。该 CLI 不接受 live 模式，不会启动正式 live pilot；有限 live smoke 走单独显式授权和总预算入口。

最终运行示例（目标目录必须全新或为空）：

```powershell
& 'D:\conda_envs\sim-jev\python.exe' scripts/phase2c_experiment.py --output runs/phase2c-20261102/pilot-final
```

运行前后必须保持同一源码指纹，发生编辑则保留开发证据并报告失败，待冻结后重新运行。每个实际执行日志由 `audit_business_log` 独立核算；每次 DES 分支通过运行时注入的 `audit_planning_branch` 独立重建 origin、事件、库存、积分、cohort、as_of 快照和评分，不产生额外 DES。实际分支数与独立核算次数必须一致。

输出包括：

- `per-seed.csv`：DSR/RCR/ASR、原 mandatory 终局缺口、实际 AFH、可用率与等待、实际 API 请求/有效率/fallback、实际 DES/模拟小时、端到端时间、actual usage 和费用估算。
- `per-decision.csv`：实际候选数、排名、所选候选/任务、规则 fallback、分支数/均分及该 cohort 最终真实履约。零前瞻参考单列零预算。
- 每个运行目录：`write_weekly_run` 的全部执行科学/审计输出，额外保存外生任务规则、独立核算结果、完整 `decisions.json`、API 脱敏日志。完整决策记录包括合法请求、冻结候选摘要、规划情景初态、每个实际分支的评分/身份/小时数和物理状态哈希。
- `aggregate.json/.csv`：按场景/方法汇总全720 h的累计分子/分母后计算比率，不平均周比率，不给正式显著性结论。
- `experiment.json`：实际命令、解释器、退出码、Git 状态、前后源码身份、闭环预算、固定状态预算及两者总预算，分别记录三个验证等级。

固定状态比较在168 h的同一个合法请求上冻结同一池，状态同时包含在修、缺件等待、在途订单、跨窗占用，以及 mandatory+carry 恰好用满名义备用边界。随机、启发式、Jev-direct、Jev Top-K 和直接 DES 均使用该池；mandatory-first另列零预算参考。客户端候选输入反序后请求仍按稳定 ID 排序，验证的是客户端映射稳定性。真实模型的呈现敏感性在离线模式中明确记为未测试，不以 stub 代替模型证据。

这些运行仅检验接入与决策管线。没有高预算最优质量参考、Regret@K、PHM 收益、PPO 或长期终端价值；Jev 的人工 stub 输出不纳入模型效果结论。
