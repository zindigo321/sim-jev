# R2 离线重建与验收命令

R2包显式依赖R1父包，SHA256必须为 `6acec98dcb5b9842a5639231b266047c6dc03d19f98fec758183fd95e15c0dcd`。父包保留旧科学源88a及全部证据；验收执行R2包内source，绝不将旧freeze改标R2。R2最终ZIP SHA及包后结果见ZIP外绑定回执；封存内报告是包前快照。

外部依赖均已安装，本轮不修改/下载：

| 依赖 | 已核验身份 | 默认定位，可由参数替换 |
| --- | --- | --- |
| 仿真及测试 | Python 3.11.18；python.exe SHA256 `214c014a3f7891c7c34dfdb7027051e4d1a26e2ca2427bc568c8918ff1c4146e`；NumPy 2.4.6 | `D:\conda_envs\sim-jev\python.exe` |
| 独立token运行时 | Python 3.11.18；python.exe SHA256 `241928ce21b67ac51c3ed93f69d018d96b2f8a655601b90eb2ee00f4b57c67c4`；tokenizers 0.22.2 | `G:\sim-jev-runtime\tokenizer-env\Scripts\python.exe` |
| 离线token资产 | tokenizer.json SHA256 `aeb13307a71acd8fe81861d94ad54ab689df773318809eed3cbe794b4492dae4` | `G:\sim-jev-models\tokenizer\cdbee75f17c01a7cc42f958dc650907174af0554\tokenizer.json` |

tokenizer.json及独立运行时不在R1父包或R2包内，必须提供上述已核验离线依赖；缺失时token门为partial，不联网补资产。其路径通过下面两个参数显式配置，不依赖旧绝对路径。兼容审计与cache-only回放不需要模型权重、推理服务或token运行时。不要安装包或创建环境。

下面所有输出目录均须全新，`$Work`可任选无旧项目内容的目录；无需.git或原G:\sim-jev。先核对R2 ZIP的SHA与外部回执，然后直接从已核对ZIP取出独立标准库传输器执行安全提取。传输器检查重复/越界路径、逐项大小/SHA以及真实空目录；失败提取目录保留，下一次改用新目录。

```powershell
$Py = 'D:\conda_envs\sim-jev\python.exe'
$R2 = 'G:\sim-jev-audit\实际R2文件名.zip' # 使用外部回执中的最终文件
$Parent = 'G:\sim-jev-audit\sim-jev-phase2d-r1-20261103-191950-02.zip'
$Work = 'G:\r2v-new' # Windows短路径；父包最长相对路径206字符，过长根目录会被提前拒绝
& $Py -c "import sys,zipfile; z=zipfile.ZipFile(sys.argv[1]); code=z.read('source/scripts/phase2d_r2_delivery.py'); sys.argv=['phase2d_r2_delivery.py',*sys.argv[2:]]; exec(compile(code,'phase2d_r2_delivery.py','exec'))" $R2 extract --archive $R2 --destination "$Work\r2" --receipt "$Work\r2-extraction.json"
if ($LASTEXITCODE -ne 0) { throw 'R2 extraction failed' }
& $Py "$Work\r2\source\scripts\phase2d_r2_delivery.py" parent --archive $Parent --destination "$Work\parent" --receipt "$Work\parent-extraction.json"
if ($LASTEXITCODE -ne 0) { throw 'parent extraction failed' }
$Evidence = "$Work\parent\project\runs\phase2d-r1-20261103-191950"
Set-Location -LiteralPath "$Work\r2\source"
```

原测试有4项启动临时本机HTTP/TLS fixture，与本轮HTTP=0约束冲突。默认以下入口原样发现全部测试，在外部runner明确跳过这4项并记录partial，其他测试有禁网护栏。它不把skip写为全部通过。只有用户明确另行允许测试fixture HTTP，才可加 `--allow-fixture-http`；该参数仍禁止外网。完整验收口径及实际数量以包后回执为准，不预写通过。

```powershell
& $Py scripts/phase2d_r2_tests.py --output "$Work\acceptance\tests-zero-http"
if ($LASTEXITCODE -ne 0) { throw 'test failures' }
& $Py scripts/phase2d_r2.py audit --evidence-root $Evidence --parent-archive $Parent --output "$Work\acceptance\strict-audit"
if ($LASTEXITCODE -ne 0) { throw 'strict audit failed' }
& $Py scripts/phase2d_r2.py captures --evidence-root $Evidence --parent-archive $Parent --output "$Work\acceptance\captures"
if ($LASTEXITCODE -ne 0) { throw 'capture compatibility failed' }
& $Py scripts/phase2d_r2_tokens.py --evidence-root $Evidence --parent-archive $Parent --tokenizer-python 'G:\sim-jev-runtime\tokenizer-env\Scripts\python.exe' --tokenizer-json 'G:\sim-jev-models\tokenizer\cdbee75f17c01a7cc42f958dc650907174af0554\tokenizer.json' --output "$Work\acceptance\tokens.json"
if ($LASTEXITCODE -ne 0) { throw 'offline token validation failed' }
& $Py scripts/phase2d_r2.py replay --evidence-root $Evidence --parent-archive $Parent --output "$Work\acceptance\full-replay"
if ($LASTEXITCODE -ne 0) { throw '60-run byte replay failed' }
```

audit读取既有24576参考标签并核算，不计算新参考DES；重建5376物理执行小时另计。replay从零运行60个闭环，读取原真实cache，额外6400审计DES、预期921600分支小时，540科学文件逐字节一致，无容差/忽略字段扩展。runtime.json等来源差异独立报告。小探针可用 `replay --probe-run balanced-50-local-direct` 或 `--probe-run balanced-50-local-des`，永远只标probe_passed；它们不能冒充完整60-run。

新renderer token门只渲染已有8开发输入×2已登记模板；还独立重计141原始prompt token。无新增生成、metadata或HTTP。新renderer真实推理、GPU与效果均未验证。规则、模型、prompt、freeze、旧响应和cache均不改写。

源码仍含原2D/R1尚未提交的工作；包内 `reports/protected-existing-work.zip` 与 `reports/before-source.json` 记录R2前归属，`reports/r2-change-list.json` 单列本轮增量。所有失败与重跑命令保留，最终外部回执绑定封存ZIP SHA。
