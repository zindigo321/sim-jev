# 阶段 2D-R1 验证记录

本文区分已发生的验证和未完成阶段。科学源码固定为 `88a245221a079462ee8d079d6585aa573f0924ce79a4cc2861255e3dc802af06`；外置审计、派生导出修补和报告工具各有独立 SHA256，不能冒充为 88a 科学源码的一部分。实际命令见 `PHASE2D_R1_COMMANDS.md`。

## 已通过与未完成

| 范围 | 当前实际证据 |
| --- | --- |
| 封存起点普通测试 | 324 项通过，退出码 0；原日志保留 |
| 最终科学源码普通测试 | 369 项通过，退出码 0；原有测试未删除或修改 |
| legacy 2A/2B | 34 份旧快照、18 份科学文件通过 |
| legacy 2C | 60 份旧快照复验通过；另以当前源码 fresh 运行旧 60 个无学习基线，540 份科学文件通过 |
| 历史真实 v1 | 20 份捕获禁网、零推理回放通过；不计入本轮 v2 验收 |
| 本轮真实 v2 开发 | 20 次真实生成、8 个开发状态通过，包含 scarce-10-168；独立 raw/token/GPU/POST 核验通过 |
| 新留出 diagnostics/reference | 24 状态、8 轨迹、144 动作；3,072 在线 DES、24,576 参考 DES和独立原始核算通过 |
| 真实 pilot 暂停 | first-12 gate 通过；13 run/65 决策后、run 14 前暂停；Python 75、当时 PowerShell 1 有真实收据 |
| 真实 pilot 新进程继续 | 已实际完成 60 run/300 决策，6,400 pilot DES、100 次真实生成；最终严格审计通过 |
| 真实恢复 verifier | 13-run 登记暂停后由新进程继续；OS 进程、原 13 份原始证据及完整 generation 前缀核验 passed |
| selected scenario-0 补充审计 | 真实 300/300 覆盖 passed；复用 200 个产物，新增 100 个 audit-only DES，不用 mock 代替 |
| 原目录完整 pilot cache-only replay | 60 run、540 科学文件通过；退出码 0、627.655453 秒，额外 6,400 DES、921,600 模拟小时；新生成和 metadata 为 0 |
| 正式合计与真实生成 | 34,048 正式 DES；141 次生成＝20 开发＋21 diagnostics＋100 pilot，累计 1,105.2702459 秒，无 pending/uncertain |
| 本任务服务停止 | shutdown receipt 与独立检查 passed；本任务残留进程 0、监听 0、monitor 已退出 |
| 最终 ZIP、逐项哈希、独立解压测试与回放 | not_run，不能声明包已验证 |

旧 2C 的 terminal-only 导出检查曾只有 480/540 项可直接比较，该范围保留；fresh 60-run/540-file 是另外实际运行的证据，不能重写早期检查。历史 42 次模型生成和历史正式 DES 保持原账，不并入本轮新验收。

## 恢复、协议与独立核算

- F1：原审计排序键反例；CLI 和 `science/checkpoint.json` 两条真实写入路径；多新进程恢复及请求/候选/命令/事件/账本精确比较。
- F2：64→160 已提交分支持久身份；计算预约、审计写入前后、checkpoint 提交前后、已有 result 与重复恢复；无法确认的工作保持 pending/uncertain，阻止重算。
- F3：31.3、47.125、95.75、191.375、335.2、511.625、671.875 小时停点；四无学习基线、local-direct/des 显式离线缓存控制、损坏锚点/未知协议拒绝、legacy 加载及跨版本核对。
- v2：重复/缺失/多余键、布尔、非有限、越界、固定按分数与原稳定 ID 排序、全同分、v1/v2 隔离、真实 attempt 身份、一次纠错上限、跨进程累计预算、cache 实际计量。
- 阶段门：缺证、v1 冒充 v2、手写 passed、fixture、uncertain 和缺 scarce-10-168 均不能解锁；诊断失败不产生参考标签。
- 已见 seed10 离线控制：实际 M16、128 在线 DES、1,024 参考 DES、每候选 64 分数、配对统计、六方法、CSV、分支三账与固定物理产物。

所有假响应均显式标为 synthetic，模型/HTTP 为 0。早期 F3 prototype 属于冻结前实现：旧缓存计时字段被最终 88a validator 拒绝的失败保留，不能称为当前源码历史 v2 兼容证明。最终 88a 另在 source-only 目录新建并持久化 `frozen-pause-31_3.json`，由新进程 cache-only 续至 `frozen-resumed-720.json`；完整逻辑 checkpoint、事件、summary 与连续运行精确一致。这一 local-direct 控制为 0 DES，不替代 local-des 测试范围。

本轮 20 条真实开发 capture 也已在无 `.git`、不依赖原项目/模型/tokenizer/历史 runs 的迁移目录零推理回放通过。它证明捕获响应和 cache 身份，不是新 GPU 验收或完整 720h 闭环回放。

## 冻结后审计修补与保留限制

原冻结 reference 入口真实触发 `cache_identity_mismatch`，在任何参考标签生成前失败；原 `reference.log` 和 receipt 保留。JSON 对象键排序改变 factor renderer 的字典遍历顺序，即使请求/候选值及 canonical hash 相同，也可能产生不同 dN 引用顺序与 prompt 字节。

审计器 SHA256 为 `58f1b0e0e140b397d3dd804fdeb8fe863acdbf2f84fb5b56f4b6e07c895cb60b`，manifest canonical SHA256 为 `b00351ba4f6db0c59579127dbe117161607ebf6e9d8e846e3ea258710250af22`。它只修审计重建：精确核对持久请求/候选的全部值、数组及 hash，再用内存工厂原顺序调用未改生产 validator，复核原 prompt、raw score、cache 和动作。数组、浮点 ULP、prompt、评分、动作及物理 hash 篡改均有拒绝证据。冻结 renderer 未改；任意 JSON 对象键重排后的 prompt 不变性仍不成立。

pilot 导出修补另由 manifest `dd7deabb8026d71dbde7e43b21602c9d07f442c2459085a932c720ef26411e48` 绑定。原 runner 恢复时会先导出较短的完成前缀，严格 auditor 正确拒绝它与完整原始记录不一致。外置工具逐项验证所有 raw run 与 JSON/CSV 后，仅重建四份派生汇总、保留旧字节，再调用原审计；不改选择、请求、seed、响应、预算、checkpoint 或执行顺序。17 项编排测试及另 10 项 live-resume verifier 隔离测试通过；另外已经取得真实 60-run 新进程恢复的通过证据，两者分别记录，未用 fixtures 替代真实运行。

真实暂停发生于第 14 个 `balanced/51/heuristic-des` 调用前，前 13 份 result/checkpoint/账本和 generation 前缀封存。暂停新增模型与 DES 为 0。Python 75 与当时外层 PowerShell 1 分别保留；新进程无 flag 已完成继续。最终 verifier 已实际核对 OS 进程、前 13 份原始字节、完整 attempt 前缀及 60/300 全部记录并 passed，见 `final-independent-review/live-resume-1793712274350827300.json`。

## 当前短窗口效果与成本

按预登记排除 5 个跨集重复公开输入后为 19 个去重身份；24 个计划状态和 8 条轨迹全部保留。19 身份不当作 19 次独立实验。标准库独立审阅从 24,576 行 raw counts 重算标量、均值、配对误差及全部逐状态/场景指标，与导出精确一致。

local-des 的 Regret@K 为 0、最佳候选召回 100%，heuristic-des 为 0.00052083 和 94.74%；但行动遗憾 local-des 为 0.00612208，略高于 heuristic-des 的 0.00543677。相对 local-direct，local-des 行动遗憾减少 0.02825292。只描述当前 offer 窗口 DSR+RCR，不推断长期闭环收益、泛化、全局最优或正式显著性；有限场景 MC 最大值有选择偏差。

正式 DES、兼容复验、工程测试、禁网回放和 selected-artifact 补充审计分别列账。补充审计登记为累计 100 DES/14,400 DES 小时和 10 条 mandatory 请求重建/7,200 执行小时，恢复复验不能重复算新增。严格数据 auditor 每次成功公开轨迹重建另有 5,376 执行小时，不能因 0 DES 声称 0 物理执行。完整工程成本未全面仪表化时保持 null，已知子账仅作下界。

上述补充审计已实际完成，正式输入与生成预算未改。原目录完整 cache-only replay 也已实际完成，其 6,400 额外 DES/921,600 小时不计入正式 34,048；627.655453 秒是这次回放实测时间，不作为模型生成速度。最终解压目录中的测试和回放是另一组待执行验证，不与原目录通过混同。

硬件外部采样最大值与 CUDA allocator 精确峰值分开；未知峰值、能量及货币成本为 null。真实生成、cache、metadata、load/prompt/eval 分别计量，不用 cache-only 耗时冒充模型速度。

本任务 owned server 与 llama-server 已由根代理实际停止；`hardware/shutdown-receipt.json` 和 `hardware/shutdown-independent-check.json` 均 passed，独立核对本任务残留进程 0、监听 0、monitor 已退出。V2 shutdown helper 对 conhost 仅观察，不显式停止；其他非本任务进程停止数为 0，独立检查新增停止操作为 0。原失败和最终成功证据均保留。

## 最终交付入口

完成后的主报告为 `G:\sim-jev\runs\phase2d-r1-20261103-191950\REPORT-final.md`，外部为 `G:\gpt-file\Codex\outputs\phase2d-r1-20261103-191950\阶段2D-R1审计报告.md` 和 `.txt`；机器主状态为 `phase2d-r1-final-status.json`。当前 partial 预览不是最终完成摘要，原报告和被替代版本保留。

最终 ZIP 必须实际逐项读取验证 SHA256，在 `G:\r1v191950\project` 无 `.git`、无外部历史 runs 的新目录运行普通测试及有真实证据的零推理回放。打包新建 replay clone 经授权精确路径检查后移到提取根，避免重复 raw attempts，再执行外置 `post-package-verify.ps1`。截至本次文档更新，最终包步骤尚未运行；包后 SHA256/验证 sidecar 独立交付，不反写封存 ZIP，也不冒充 GPU 再验证。


<!-- phase2d-r1-docs-postdelivery-appendix-v1 -->

## 最终实际包后验证补充

追加时间（UTC）：2026-11-04T00:50:25.063465+00:00。本段仅在修订包、postverify-r2、最终报告发布及 delivery closure 全部实际通过后追加。上文记录保留各阶段当时状态；其中“待执行/not_run”不代表本段追加后的最终交付状态。

主交付包为 `G:\sim-jev-audit\sim-jev-phase2d-r1-20261103-191950-02.zip`，SHA256 为 `6acec98dcb5b9842a5639231b266047c6dc03d19f98fec758183fd95e15c0dcd`。ZIP 内两份 docs 是封存前快照；当前项目文档中的本补充属于外部包后核验记录，没有重新封包，也没有修改 ZIP 内原文档。科学源码保持 `88a245221a079462ee8d079d6585aa573f0924ce79a4cc2861255e3dc802af06`。

修订包在全新 `G:\r1v191950b\project` 实际通过 369 项普通测试、20 条历史真实 v1 捕获缓存回放，以及 60 个新 pilot 闭环的缓存回放；后者逐字节核对 540 份科学文件。额外的 portable20 仅复核本轮真实开发捕获。以上回放均不产生新模型请求，不是新一轮 GPU 验收。

原包 110318 个条目的 payload、元数据和顺序均保留；修订只补入 332 个真实空叶目录及 20 个传输文件条目，总计 110670 条目。原包自身的 369 项测试与 20/60 缓存回放通过，但后续严格审计因缺少空 branch-audit-store 目录失败；原失败、clone 搬移收据和原包 `d5cc304169cec796f719ae70f49513af7b95738c78870df27a948c36a71bed4a` 均保留，不能将这次失败改写为已通过。

两轮包后回放分别实际消耗 7040（640+6400）个审计 DES，累计 14080，原轮成本没有被修订抵销。本轮正式 DES 仍为 34048；已知非重叠额外 DES 下界为 34637，普通测试等内部 DES 尚未完整仪表化，因此全工程总数仍为 `null`。postverify-r2 的 DES 为 0，另有 5376 执行小时的 mandatory 轨迹重建。模型生成仍为 141（开发 20、诊断 21、pilot 100），预算与原始响应未改。

最终完整数据审计、传输逐条核验和 portable20 均通过；postverify 实际回执为 `G:\r1v191950b\post-package-verification-20261104T0003213377912Z\result.json`。原冻结 reference 在生成标签前因对象键顺序导致 renderer 身份不一致而失败的记录继续保留。审计重建器仍独立标识为 `58f1b0e0e140b397d3dd804fdeb8fe863acdbf2f84fb5b56f4b6e07c895cb60b`，没有放宽数组顺序、浮点值、prompt 或 cache 校验；任意 JSON 对象键重排后的 prompt 不变性仍不成立。旧 F3 prototype 的失败范围也继续保留，不作为当前 88a 的 v2 兼容证据。

主报告为 `G:\gpt-file\Codex\outputs\phase2d-r1-20261103-191950\阶段2D-R1审计报告.md` 及同名 `.txt`，项目副本为 `G:\sim-jev\runs\phase2d-r1-20261103-191950\REPORT-final.md`。小型交付回执、`delivery-closure-manifest.json` 和验证说明位于 `G:\gpt-file\Codex\outputs\phase2d-r1-20261103-191950\final-delivery`；模型计数及成本以 `G:\gpt-file\Codex\outputs\phase2d-r1-20261103-191950\phase2d-r1-final-status.json` 为准。本段追加的 before 备份、输入/输出 SHA256 和实际调用另见同目录 `docs-final-append-manifest.json`。官方 Jev 仍未验收。

以下命令记录已经发生的验证，仅供追溯；不要据此无条件重跑。每组最后一行显式传播原生退出码。运输外层命令由主代理的实际工具调用及 PID 37384 argv 确认（session 41072）；其余命令按对应已通过回执中的实际 argv/cwd 重建为 PowerShell 引用形式。

原包已执行命令 1（回执 exit 0）

```powershell
Set-Location -LiteralPath 'G:\r1v191950\project'
& 'D:\conda_envs\sim-jev\python.exe' '-m' 'unittest' 'discover' '-s' 'tests' '-v'
exit $LASTEXITCODE
```

原包已执行命令 2（回执 exit 0）

```powershell
Set-Location -LiteralPath 'G:\r1v191950\project'
& 'D:\conda_envs\sim-jev\python.exe' '-c' 'import socket; from unittest.mock import patch; from pathlib import Path; from scripts.phase2d_local import captured_replay; guard=patch.object(socket,"socket",side_effect=AssertionError("network forbidden")); guard.start(); print(captured_replay(Path(__import__("sys").argv[1]),label="extracted-replay"))' 'G:\r1v191950\project\runs\phase2d-r1-20261103-191950\legacy-v1'
exit $LASTEXITCODE
```

原包已执行命令 3（回执 exit 0）

```powershell
Set-Location -LiteralPath 'G:\r1v191950\project'
& 'D:\conda_envs\sim-jev\python.exe' 'G:\r1v191950\project\scripts\phase2d_r1.py' 'replay' '--output' 'G:\r1v191950\project\runs\phase2d-r1-20261103-191950\extracted-replay-input'
exit $LASTEXITCODE
```

修订包 -02 已执行命令 1（回执 exit 0）

```powershell
Set-Location -LiteralPath 'G:\r1v191950b\project'
& 'D:\conda_envs\sim-jev\python.exe' '-B' '-m' 'unittest' 'discover' '-s' 'tests' '-v'
exit $LASTEXITCODE
```

修订包 -02 已执行命令 2（回执 exit 0）

```powershell
Set-Location -LiteralPath 'G:\r1v191950b\project'
& 'D:\conda_envs\sim-jev\python.exe' '-B' '-c' 'import socket; from unittest.mock import patch; from pathlib import Path; from scripts.phase2d_local import captured_replay; guard=patch.object(socket,"socket",side_effect=AssertionError("network forbidden")); guard.start(); print(captured_replay(Path(__import__("sys").argv[1]),label="extracted-replay"))' 'G:\r1v191950b\project\runs\phase2d-r1-20261103-191950\legacy-v1'
exit $LASTEXITCODE
```

修订包 -02 已执行命令 3（回执 exit 0）

```powershell
Set-Location -LiteralPath 'G:\r1v191950b\project'
& 'D:\conda_envs\sim-jev\python.exe' '-B' 'G:\r1v191950b\project\scripts\phase2d_r1.py' 'replay' '--output' 'G:\r1v191950b\project\runs\phase2d-r1-20261103-191950\extracted-replay-input'
exit $LASTEXITCODE
```

运输外层实际工具调用（由主代理确认；未使用 -I 或 -u）：

```powershell
& 'D:\conda_envs\sim-jev\python.exe' -B 'G:\gpt-file\Codex\outputs\phase2d-r1-20261103-191950\transport-repair-r1\TRANSPORT-REPAIR.py' run --plan 'G:\gpt-file\Codex\outputs\phase2d-r1-20261103-191950\transport-repair-r1\TRANSPORT-PLAN-r2.json' *> 'G:\gpt-file\Codex\outputs\phase2d-r1-20261103-191950\transport-repair-r1\actual-run.log'
exit $LASTEXITCODE
```

原包后严格审计的真实失败命令（exit 1，保留失败）

```powershell
Set-Location -LiteralPath 'G:\r1v191950\project'
& 'D:\conda_envs\sim-jev\python.exe' '-I' 'G:\r1v191950\project\runs\phase2d-r1-20261103-191950\postfreeze-audit-r1\driver.py' 'verify-data' '--output' 'G:\r1v191950\project\runs\phase2d-r1-20261103-191950'
exit $LASTEXITCODE
```

postverify-r2：transport-precheck（回执 exit 0）

```powershell
Set-Location -LiteralPath 'G:\r1v191950b\project'
& 'D:\conda_envs\sim-jev\python.exe' '-I' 'G:\r1v191950b\post-package-verification-20261104T0003213377912Z\transport-precheck.py' 'G:\r1v191950b' 'G:\sim-jev-audit\sim-jev-phase2d-r1-20261103-191950-02.zip' 'G:\r1v191950b\post-package-verification-20261104T0003213377912Z\transport-precheck-result.json' '9ba1448f256888ac096fd34deb86edbfd617eb420e8c10be66e1eb81318b06ce'
exit $LASTEXITCODE
```

postverify-r2：verify-data（回执 exit 0）

```powershell
Set-Location -LiteralPath 'G:\r1v191950b\project'
& 'D:\conda_envs\sim-jev\python.exe' '-I' 'G:\r1v191950b\project\runs\phase2d-r1-20261103-191950\postfreeze-audit-r1\driver.py' 'verify-data' '--output' 'G:\r1v191950b\project\runs\phase2d-r1-20261103-191950'
exit $LASTEXITCODE
```

postverify-r2：captured-real-development-replay（回执 exit 0）

```powershell
Set-Location -LiteralPath 'G:\r1v191950b\project'
& 'D:\conda_envs\sim-jev\python.exe' '-I' 'G:\r1v191950b\project\runs\phase2d-r1-20261103-191950\postfreeze-audit-r1\captured_replay.py' '--require-clean-extraction' '--output' 'G:\r1v191950b\post-package-verification-20261104T0003213377912Z\captured-replay'
exit $LASTEXITCODE
```

已执行最终报告发布（finalizer manifest exit 0）

```powershell
Set-Location -LiteralPath 'G:\sim-jev'
& 'D:\conda_envs\sim-jev\python.exe' 'G:\r1v191950b\POSTSEAL-REPORT-FINALIZER.py' '--root' 'G:\sim-jev\runs\phase2d-r1-20261103-191950' '--report-dir' 'G:\gpt-file\Codex\outputs\phase2d-r1-20261103-191950' '--extract-root' 'G:\r1v191950b' '--archive' 'G:\sim-jev-audit\sim-jev-phase2d-r1-20261103-191950-02.zip' '--publish-main'
exit $LASTEXITCODE
```

已执行交付 closure（passed manifest 原始 argv）

```powershell
& 'D:\conda_envs\sim-jev\python.exe' 'G:\gpt-file\Codex\outputs\phase2d-r1-20261103-191950\final-delivery\add_delivery_closure.py' '--apply'
exit $LASTEXITCODE
```

本段追加只读取小型已完成回执并更新这两份文档：新增 DES、模型、网络请求及物理执行小时均为 0。封存包、18 份绑定传输 payload、正式实验数据与科学源码均未因本段发生修改。
