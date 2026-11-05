# R2 运行身份与恢复

`scripts/phase2d_local.py recovery-run` 与 R1 共用 `run_pilot_case` 在首次计算前保存 `run-identity.json`，协议为 `phase2d-immutable-run-v1`。其内容包括请求的场景、seed、method，完整策略（含模型、renderer 与评分身份），有效配置，外生任务与规则的完整有序数组、初始寿命，模型/RNG/checkpoint/算术/请求/日执行器版本，实验冻结内容及 R2 执行源码内容指纹。冻结科学源码与 R2 执行源码是不同身份，不会改写旧 freeze。

每次调用先纯读校验该身份的内容及 SHA256，再严格导入 checkpoint 并核对其全部不可变输入和独立 exogenous 记录。这个检查发生在创建账本、预约分支/模型、执行耗时记账、前向执行与任何运行输出写入之前。JSON 对象键序允许改变；数组顺序与数字类型/值受 canonical 内容哈希约束。配置、规则、完整策略或冻结内容不同即非零退出，不能使用 checkpoint 参数替代调用参数。

已完成结果走相同前置检查，再通过 `BranchAuditStore(..., read_only=True)` 逐项校验 checkpoint/账本和科学摘要。同身份幂等返回不调用 `run()`/DES、不新增执行 attempt，也不重写结果或账本。纯读账本要求 attempts/audits/artifacts 三个目录都存在；需要修复的 pending 记录必须交给显式恢复流程，审计不自动改写。默认可写 journal 保留 R1 F2 的保守崩溃恢复规则。

缺少该清单的历史 partial/completed，无法从 checkpoint 可靠证明原执行源码及实验 freeze 绑定，因此 runner 拒绝自动续跑并提供原因。历史正式数据应由 R2 的只读严格审计入口核验，或在独立新目录中按绑定旧版本及已封存缓存从零回放；禁止补写身份冒充原生 R2 运行。历史证据与失败运行保留原样。

离线恢复示例（不访问模型、metadata 或 HTTP）：

```powershell
& 'D:\conda_envs\sim-jev\python.exe' scripts/phase2d_local.py recovery-run --output 'G:\gpt-file\Codex\outputs\my-new-recovery' --scenario balanced --seed 30 --method heuristic-des --until 168
& 'D:\conda_envs\sim-jev\python.exe' scripts/phase2d_local.py recovery-run --output 'G:\gpt-file\Codex\outputs\my-new-recovery' --scenario balanced --seed 30 --method heuristic-des --until 720
```

`--planning-seed`、`--top-k` 与 `--scenarios-per-candidate` 可显式指定完整无学习策略。中途改变任一值会被拒绝。`--until` 是推进目标，不是实验身份；实验终点始终为 720h。恢复 CLI 用 socket 拒绝护栏执行，独立命令收据默认位于运行目录的同级 `<目录名>-receipts`；可用 `--receipt-output` 指定其他独立目录，不能放在运行目录内。

测试：`python -m unittest tests.test_phase2d_r2_resume -v`。测试实际驱动 runner、严格 checkpoint、fixture-only local-direct 与新进程 CLI；fixture 从不计为真实模型生成。原始审计中的两步 CLI 误标反例和修复后拒绝/不变性证据分开放在本轮交付报告的 `resume/` 目录。
