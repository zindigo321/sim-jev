# 阶段2D实际命令与续跑

命令在新PowerShell窗口使用完整路径。运行目录必须全新；下列NEXT/REPLAY-NEW是需要选择的新批次名称，已有路径会拒绝覆盖。原始命令、退出码、耗时见各批次的commands目录、hardware/*.json、regression/*.command.json及独立导出的commands-ledger.json。

## 本轮实际执行

```powershell
$py = 'D:\conda_envs\sim-jev\python.exe'
$a = 'G:\sim-jev\runs\phase2d-local-20261103-01'
$b = 'G:\sim-jev\runs\phase2d-local-20261103-02'
$e = 'G:\sim-jev\runs\phase2d-local-20261103-05'

# install/start成功；pull失败；download第一次后续小文件超时，第二次成功。
& $py 'G:\sim-jev\scripts\phase2d_runtime.py' install --output "$a\hardware"
& $py 'G:\sim-jev\scripts\phase2d_runtime.py' start --output "$a\hardware"
& $py 'G:\sim-jev\scripts\phase2d_runtime.py' pull --output "$a\hardware"
& $py 'G:\sim-jev\scripts\phase2d_runtime.py' download --output "$a\hardware"
# preflight由成功download内部实际调用，完整blob核验和API元数据已保存。

& $py 'G:\sim-jev\scripts\phase2d_local.py' development --output $a
& $py 'G:\sim-jev\scripts\phase2d_local.py' smoke --output $a
# smoke有失败与续跑收据；最终20有效完成，严格状态覆盖partial。
& $py 'G:\sim-jev\scripts\phase2d_local.py' freeze --offline-only --output $a
& $py 'G:\sim-jev\scripts\phase2d_local.py' diagnostics --baseline-only --output $a
& $py 'G:\sim-jev\scripts\phase2d_local.py' reference --baseline-only --output $a
& $py 'G:\sim-jev\scripts\phase2d_local.py' captured-replay --output $a
& $py 'G:\sim-jev\scripts\phase2d_audit.py' regress --output $a
& $py 'G:\sim-jev\scripts\phase2c_experiment.py' --output "$a\old2c-regenerated"
& $py 'G:\sim-jev\scripts\phase2c_science_regression.py' --archive 'G:\sim-jev-audit\sim-jev-phase2c-audit-20261102-626224fb.zip' --experiment "$a\old2c-regenerated" --output "$a\old2c-comparison"

# 批次01数据审计曾因not_run缺selected_candidate_id失败；原收据保留。
# 批次02独立审计修复后重读同一批原始产物，不生成新DES标签。
& $py 'G:\sim-jev\scripts\phase2d_audit.py' verify-data --baseline-only --source-evidence $a --output $b
& $py 'G:\sim-jev\scripts\phase2d_audit.py' regress --output $b
& $py 'G:\sim-jev\scripts\phase2c_science_regression.py' --archive 'G:\sim-jev-audit\sim-jev-phase2c-audit-20261102-626224fb.zip' --experiment "$a\old2c-regenerated" --output "$b\old2c-comparison"

# 03/04打包修复尝试及失败收据保留；05含两项新增脱敏回归测试。
& $py 'G:\sim-jev\scripts\phase2d_audit.py' regress --output $e

# 打包同时重新读取各文件、全新解压、运行普通测试和20真实响应零推理回放。
# 是否实际完成以生成的同名.zip.verification.json为准；再次执行须换archive/extract路径。
& $py 'G:\sim-jev\scripts\phase2d_audit.py' package --output $e --archive 'G:\sim-jev-audit\sim-jev-phase2d-local-20261103-05.zip' --extract 'G:\sim-jev-audit\phase2d-local-20261103-05-extracted'
```

批次01模型服务已经关闭。其模型缓存和便携二进制保留在指定G盘路径。只关闭了本轮记录的PID42712及已验证直接子runner42776，没有操作其他用户服务。

## 重建运行时和tokenizer（说明命令，不代表本轮再次执行）

```powershell
$py = 'D:\conda_envs\sim-jev\python.exe'
$next = 'G:\sim-jev\runs\phase2d-local-NEXT'
New-Item -ItemType Directory -Path $next -ErrorAction Stop
& $py 'G:\sim-jev\scripts\phase2d_runtime.py' install --output "$next\hardware"
& $py 'G:\sim-jev\scripts\phase2d_runtime.py' start --output "$next\hardware" --port 11439
& $py 'G:\sim-jev\scripts\phase2d_runtime.py' download --output "$next\hardware" --port 11439
& $py 'G:\sim-jev\scripts\phase2d_runtime.py' preflight --output "$next\hardware" --port 11439
$manifest = Get-Content -LiteralPath "$next\hardware\model-manifest.json" -Raw | ConvertFrom-Json
if ($manifest.digest -ne 'sha256:0edcdef34593eac1aa2be9c7d06c432dcf81945adca5eca2f27662c18f168ba0') { throw '固定模型digest不一致，停止，不进入实验' }

# 仅在新机器缺少该独立环境时创建；不修改已验收Conda环境。
& $py -m venv 'G:\sim-jev-runtime\tokenizer-env'
& 'G:\sim-jev-runtime\tokenizer-env\Scripts\python.exe' -m pip install --index-url https://pypi.org/simple tokenizers==0.22.2
$tokenDir = 'G:\sim-jev-models\tokenizer\cdbee75f17c01a7cc42f958dc650907174af0554'
New-Item -ItemType Directory -Path $tokenDir -Force
Invoke-WebRequest -Uri 'https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507/resolve/cdbee75f17c01a7cc42f958dc650907174af0554/tokenizer.json' -OutFile "$tokenDir\tokenizer.json"
# 按scientific-freeze.json的tokenizer.sha256核对实际文件，再运行development。
Get-FileHash -LiteralPath "$tokenDir\tokenizer.json" -Algorithm SHA256
```

服务端口冲突时start会失败，不停止原服务；可选择新端口。当前实验provider使用11439，若修改端口配置属于新的源码/配置批次，不能在已冻结批次中改动。正式阶段不重新pull模型标签；直接preflight验证现有固定缓存。

## 真模型正式流程与断点恢复（入口已实现，本轮未实跑）

当前批次01的formal_live_allowed=false，下列正式诊断/pilot会被门槛拒绝。下一轮必须先获得新的开发授权，解决严格排序失败、遵守新的模板/预算契约并冻结；不能直接把本轮false标志改成true。

```powershell
$py = 'D:\conda_envs\sim-jev\python.exe'
$next = 'G:\sim-jev\runs\phase2d-local-NEXT'
& $py 'G:\sim-jev\scripts\phase2d_local.py' development --output $next
& $py 'G:\sim-jev\scripts\phase2d_local.py' smoke --output $next
& $py 'G:\sim-jev\scripts\phase2d_local.py' freeze --output $next
& $py 'G:\sim-jev\scripts\phase2d_local.py' diagnostics --output $next
& $py 'G:\sim-jev\scripts\phase2d_local.py' reference --output $next
& $py 'G:\sim-jev\scripts\phase2d_audit.py' verify-data --output $next
& $py 'G:\sim-jev\scripts\phase2d_local.py' pilot --output $next

# 同一冻结批次中断后，重复pilot入口会跳过已完成run，从各run稳定checkpoint续跑。
# 必须复用原generation-budget.json；pending/uncertain请求会阻止补发，不能删除预算账本解锁。
& $py 'G:\sim-jev\scripts\phase2d_local.py' pilot --output $next
# 完整pilot已完成且所需响应均在各run独立store时，执行零推理回放。
& $py 'G:\sim-jev\scripts\phase2d_local.py' replay --output $next
```

已提交checkpoint恢复不调用过去的resolver或重跑过去DES；未提交成功响应可由原子cache复用。cache-only缺失即失败，不补发生成。每run独立store，不能使用其他方法缓存获取不披露的计时优势。未完成的pilot未作实测成功声明。

## 在无模型、无网络条件下复核本轮真实捕获

```powershell
$py = 'D:\conda_envs\sim-jev\python.exe'
$fresh = 'G:\sim-jev-audit\phase2d-replay-NEW'
if (Test-Path -LiteralPath $fresh) { throw '请选择新的解压目录' }
Expand-Archive -LiteralPath 'G:\sim-jev-audit\sim-jev-phase2d-local-20261103-05.zip' -DestinationPath $fresh
& $py "$fresh\project\scripts\phase2d_local.py" captured-replay --output "$fresh\project\runs\phase2d-local-20261103-01" --replay-label extracted-replay
```

该入口完全不需要模型服务或tokenizer环境；显式禁止网络调用，检查20份真实排名、对应local-direct/local-des命令、32分支预算及短窗物理身份。额外640 DES/65280模拟小时单列为审计重放。它不验证新GPU推理，也不是未执行的60运行闭环。
