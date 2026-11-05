# 阶段 2D-R1：实际入口与续跑边界

项目为 `G:\sim-jev`，批次为 `G:\sim-jev\runs\phase2d-r1-20261103-191950`，解释器固定为 `D:\conda_envs\sim-jev\python.exe`。以下每个 PowerShell 代码块单独执行，末尾 `exit $LASTEXITCODE` 将 Python 退出码传给外层进程。已有阶段不能无条件重跑；原失败日志和收据保留。

当前工程门、20 次真实 v2 开发验收、24 状态 diagnostics 和参考核算、60-run/300-decision pilot、真实新进程恢复 verifier、300/300 selected scenario-0 补充审计，以及原目录完整 cache-only replay 均已通过。正式 DES 合计 34,048；本轮真实生成 141 次（20 开发 + 21 diagnostics + 100 pilot），累计 1,105.2702459 秒。最终打包、解压后的普通测试与回放仍为待执行，不预写通过。

## 科学源与外置审计身份

科学源码始终为 `88a245221a079462ee8d079d6585aa573f0924ce79a4cc2861255e3dc802af06`，未为后续审计修补更改源码、配置、测试、模板或模型。

- `postfreeze-audit-r1/driver.py` 绑定科学源和 freeze，加载审计器 `58f1b0e0e140b397d3dd804fdeb8fe863acdbf2f84fb5b56f4b6e07c895cb60b`；修补 manifest canonical SHA256 为 `b00351ba4f6db0c59579127dbe117161607ebf6e9d8e846e3ea258710250af22`。
- `postfreeze-pilot-export-r1/driver.py` 再绑定上述审计入口，只修复已验证原始记录的四份 pilot 派生导出，并实现登记暂停。manifest canonical SHA256 为 `dd7deabb8026d71dbde7e43b21602c9d07f442c2459085a932c720ef26411e48`；driver SHA256 为 `d3e1fb27c117f2456bbef674032a31f12936f8fb933a0a1787ae2ca6bf74fdbc`。

原冻结 reference 入口已真实失败：排序后的 JSON 改变了 renderer 的字典遍历顺序，原审计器触发 `cache_identity_mismatch`。当时尚未生成任何参考标签；`reference.log` 和失败 receipt 保留。外置修补先证明请求/候选全部值、数组顺序及 canonical hash 精确相等，再采用工厂原对象顺序复核原 prompt/cache。冻结 renderer 仍不保证任意 JSON 对象键重排后的 prompt 相同。

## 已使用的 reference 与核算入口

以下 reference 已实际完成，不能在已有 evaluation 目录无条件重跑：

```powershell
Set-Location -LiteralPath 'G:\sim-jev'
& 'D:\conda_envs\sim-jev\python.exe' -u 'G:\sim-jev\runs\phase2d-r1-20261103-191950\postfreeze-audit-r1\driver.py' reference --output 'G:\sim-jev\runs\phase2d-r1-20261103-191950'
exit $LASTEXITCODE
```

完整核算使用修补后的严格入口，不直接调用旧 data auditor。它新增 DES/模型调用为 0，但有公开轨迹重建成本：

```powershell
Set-Location -LiteralPath 'G:\sim-jev'
& 'D:\conda_envs\sim-jev\python.exe' -u 'G:\sim-jev\runs\phase2d-r1-20261103-191950\postfreeze-audit-r1\driver.py' verify-data --output 'G:\sim-jev\runs\phase2d-r1-20261103-191950'
exit $LASTEXITCODE
```

## 真实 pilot 暂停与继续

首次启动实际使用下列暂停参数；它只允许在尚无完成 pilot run 时使用，不能对当前批次再加一次：

```powershell
Set-Location -LiteralPath 'G:\sim-jev'
& 'D:\conda_envs\sim-jev\python.exe' -u 'G:\sim-jev\runs\phase2d-r1-20261103-191950\postfreeze-pilot-export-r1\driver.py' pilot --output 'G:\sim-jev\runs\phase2d-r1-20261103-191950' --pause-before-run 14
exit $LASTEXITCODE
```

真实暂停保留了 13 个完整 run、65 次决策和通过的 first-12 gate；第 14 个 run 尚未进入。Python driver 实际退出码是 **75**，当时外层 PowerShell 包装实际观察为 **1**，按 `pilot-first13-launch-exit.json` 原样保留。上面明确传播退出码的是修正后的复用写法，不改写历史观察。

下列无暂停参数的命令已在**新进程**完成全部 60 个 run、300 次决策，最终严格审计和真实恢复 verifier 均通过。既有 13 份原始 result/checkpoint/分支账本及 generation 前缀已核对，未重算、未重发模型请求。命令保留用于追溯，不应无条件重跑或并发启动第二个 writer：

```powershell
Set-Location -LiteralPath 'G:\sim-jev'
& 'D:\conda_envs\sim-jev\python.exe' -u 'G:\sim-jev\runs\phase2d-r1-20261103-191950\postfreeze-pilot-export-r1\driver.py' pilot --output 'G:\sim-jev\runs\phase2d-r1-20261103-191950'
exit $LASTEXITCODE
```

若出现 pending/uncertain 或已审计但未提交的工作，保留原账并停止核查，禁止删账、退款或重选解锁。派生汇总仅在逐值证明为完成记录前缀后重建；原始 checkpoint、响应、选择、预算和顺序不变。

## 已完成的后续验证与待执行打包

下列补充审计、原目录回放和真实恢复复核均已实际完成；其命令保留用于追溯。最后的 report/package 和解压复核仍按实际收据分别确认，不把原目录通过等同于解压后通过。

登记的 selected scenario-0 补充审计已达到 300/300：复用 200 个已有产物，另做 100 个 audit-only 分支。额外 14,400 DES 小时与 10 条 mandatory-first 请求重建的 7,200 执行小时单列，不反馈效果或选择；见 `selected-artifacts-r1/result.json`：

```powershell
Set-Location -LiteralPath 'G:\sim-jev'
& 'D:\conda_envs\sim-jev\python.exe' -u 'G:\sim-jev\runs\phase2d-r1-20261103-191950\selected-artifacts-r1\selected_artifacts.py' run --root 'G:\sim-jev\runs\phase2d-r1-20261103-191950'
exit $LASTEXITCODE
```

原目录完整 720h cache-only replay 已通过全部 60 个 run、540 份科学文件，退出码 0，实测 627.655453 秒。新增 6,400 个审计 DES、921,600 模拟小时另计；新模型生成、metadata 均为 0，不是 GPU 再验收；见 `cache-only-replay/status.json`：

```powershell
Set-Location -LiteralPath 'G:\sim-jev'
& 'D:\conda_envs\sim-jev\python.exe' -u 'G:\sim-jev\runs\phase2d-r1-20261103-191950\postfreeze-pilot-export-r1\driver.py' replay --output 'G:\sim-jev\runs\phase2d-r1-20261103-191950'
exit $LASTEXITCODE
```

最终 resume receipt 和两次真实 OS 进程证据已组合并独立核验通过，结果见 `final-independent-review/live-resume-1793712274350827300.json`。下面是实际入口，中间失败立即传播：

```powershell
Set-Location -LiteralPath 'G:\sim-jev'
& 'D:\conda_envs\sim-jev\python.exe' 'G:\sim-jev\runs\phase2d-r1-20261103-191950\postfreeze-pilot-export-r1\combine-process-evidence.py' --output 'G:\sim-jev\runs\phase2d-r1-20261103-191950'
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& 'D:\conda_envs\sim-jev\python.exe' 'G:\sim-jev\runs\phase2d-r1-20261103-191950\postfreeze-pilot-export-r1\verify-live-resume.py' --output 'G:\sim-jev\runs\phase2d-r1-20261103-191950'
exit $LASTEXITCODE
```

最终只读 campaign 核算已确认 141 次完成的真实尝试、无 pending/uncertain，累计 1,105.2702459 秒；入口如下：

```powershell
Set-Location -LiteralPath 'G:\sim-jev'
& 'D:\conda_envs\sim-jev\python.exe' -I 'G:\sim-jev\runs\phase2d-r1-20261103-191950\postfreeze-audit-r1\campaign_raw_audit.py' --final
exit $LASTEXITCODE
```

报告交付步骤先保存冻结报告入口的原输出，再生成完整主报告；被替代旧报告保留，尚未完成的打包/解压字段仍为 partial：

```powershell
Set-Location -LiteralPath 'G:\sim-jev'
& 'D:\conda_envs\sim-jev\python.exe' 'G:\sim-jev\scripts\phase2d_r1_delivery.py' report --output 'G:\sim-jev\runs\phase2d-r1-20261103-191950' --report-dir 'G:\gpt-file\Codex\outputs\phase2d-r1-20261103-191950'
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& 'D:\conda_envs\sim-jev\python.exe' 'G:\sim-jev\runs\phase2d-r1-20261103-191950\final-report-work\finalize_report.py' --root 'G:\sim-jev\runs\phase2d-r1-20261103-191950' --report-dir 'G:\gpt-file\Codex\outputs\phase2d-r1-20261103-191950' --extract-root 'G:\r1v191950' --publish-main
exit $LASTEXITCODE
```

主报告为 `G:\sim-jev\runs\phase2d-r1-20261103-191950\REPORT-final.md`，外部为 `G:\gpt-file\Codex\outputs\phase2d-r1-20261103-191950\阶段2D-R1审计报告.md` 和同名 `.txt`；机器状态为 `phase2d-r1-final-status.json`。此前 `final-report-work/snapshot-*` 是阶段快照，不能自动当作最终完成摘要。

下列新 ZIP 名是待执行示例，archive 和提取根必须尚不存在；最终采用的包名以真实交付 receipt 为准。当前尚未执行打包与解压后验证：

```powershell
Set-Location -LiteralPath 'G:\sim-jev'
& 'D:\conda_envs\sim-jev\python.exe' 'G:\sim-jev\scripts\phase2d_r1_delivery.py' package --output 'G:\sim-jev\runs\phase2d-r1-20261103-191950' --archive 'G:\sim-jev-audit\sim-jev-phase2d-r1-20261103-191950.zip' --extract 'G:\r1v191950'
exit $LASTEXITCODE
```

打包器完成自身测试与回放后，还要运行提取后的 `G:\r1v191950\project\runs\phase2d-r1-20261103-191950\postfreeze-audit-r1\post-package-verify.ps1`。该包装器内部固定使用上述 Python，按授权精确路径检查把打包后新建、原 ZIP 清单中不存在的 replay clone 搬到提取根，写 relocation-map，再执行原 58f 数据审计与 20 条真实开发 capture 回放，避免把 clone raw attempts 重复计数。具体安全步骤见批次中的 `postfreeze-audit-r1/FINAL-VERIFICATION-COMMANDS.md`。

最终 ZIP 的 SHA256/解压验证 sidecar 独立交付，不反写封存 ZIP 来制造自包含哈希。运行时、权重、tokenizer、Conda、`.git` 和无关历史 runs 不进入包；原固定模型资产继续使用，不重新安装或下载。

## 已完成的本任务服务停止

根代理已实际停止经路径、父子关系和启动身份核对的本任务 Ollama server 与 llama-server，写入 `hardware/shutdown-receipt.json`。独立 `hardware/shutdown-independent-check.json` 为 passed：本任务残留进程 0、监听端口 0、monitor 已退出，独立检查新增 stop 操作 0。`shutdown-owned-runtime-v2.ps1` 对 `conhost.exe` 仅作观察，不显式停止它；其他非本任务进程停止数为 0。首轮失败与最终收据均保留，本次文档更新没有执行任何进程操作。


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
