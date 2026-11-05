# 阶段2E-A命令与离线依赖

仿真解释器固定 `D:\conda_envs\sim-jev\python.exe`（Python3.11.18，NumPy2.4.6）。
独立token解释器 `G:\sim-jev-runtime\tokenizer-env\Scripts\python.exe`（Python3.11.18，
tokenizers0.22.2，exe SHA241928ce21b67ac51c3ed93f69d018d96b2f8a655601b90eb2ee00f4b57c67c4）。
tokenizer JSON位于 `G:\sim-jev-models\tokenizer\cdbee75f17c01a7cc42f958dc650907174af0554\tokenizer.json`，
SHAaeb13307a71acd8fe81861d94ad54ab689df773318809eed3cbe794b4492dae4。
路径可通过prepare/audit的`--tokenizer-python`及`--tokenizer-json`替换，身份必须一致。
不安装、不下载、不创建环境。权重及tokenizer不随包；纯cache回放不需要模型服务。

固定R1父包为 `G:\sim-jev-audit\sim-jev-phase2d-r1-20261103-191950-02.zip`，
SHA6acec98dcb5b9842a5639231b266047c6dc03d19f98fec758183fd95e15c0dcd。
R2主包及包后证据作为历史来源，SHA见本轮input-verification-02目录。所有新输出
必须采用新目录。下列`$Batch`指本轮实际报告根；命令成功与否以实际收据为准。

```powershell
$Py = 'D:\conda_envs\sim-jev\python.exe'
$Batch = 'G:\gpt-file\Codex\outputs\phase2e-a-实际时间戳'
& $Py scripts/phase2d_r2_tests.py --allow-fixture-http --output "$Batch\engineering-tests"
& $Py scripts/phase2e_live.py prepare --root $Batch --legacy 'G:\sim-jev\runs\phase2d-r1-20261103-191950'
& $Py scripts/phase2e_diagnostic.py preregister --root $Batch --seen-evidence 'G:\sim-jev\runs\phase2d-r1-20261103-191950' --history-root 'G:\sim-jev\runs'
```

诊断预注册由`phase2e_diagnostic.preregister`读取旧公开输入及种子使用清单后生成；
不构造新状态。完整源码和分析脚本冻结之后，以下入口自启自停隐藏本机服务，
不接管其他服务。G1失败即保存partial，不生成新环境或参考标签。

```powershell
& $Py scripts/phase2e_live.py live --root $Batch --diagnostic --port 11459
```

只有全部在线选择封存且独立核验后，才允许执行`phase2e_diagnostic.reference($Batch)`，
其函数再次检查全部动作与封存hash。分析入口：

```powershell
& $Py scripts/phase2e_diagnostic.py analyze --root $Batch
```

生成参考的正式命令为 `& $Py scripts/phase2e_diagnostic.py reference --root $Batch`；
不能在已存在evaluation目录重复执行，失败保留partial。

独立核算与cache回放可以完全离线执行。先核对最终ZIP外置SHA，再用R2标准库
传输器提取；新包保留逐项SHA、目录及空目录拓扑。在新目录以**包内source**执行，
无需原项目、.git或私下脚本。包后回执保存ZIP外，绑定最终ZIP SHA；封存后不回写。

```powershell
$Zip = 'G:\sim-jev-audit\实际phase2e文件名.zip'
$Work = 'G:\e2a-new'
& $Py -c "import sys,zipfile;z=zipfile.ZipFile(sys.argv[1]);c=z.read('source/scripts/phase2d_r2_delivery.py');sys.argv=['transport',*sys.argv[2:]];exec(compile(c,'transport','exec'))" $Zip extract --archive $Zip --destination $Work --receipt "$Work-extraction.json"
Set-Location -LiteralPath "$Work\source"
& $Py scripts/phase2e_delivery.py audit --root "$Work\reports" --output "$Work-strict-audit"
& $Py scripts/phase2e_diagnostic.py audit-reference --root "$Work\reports" --output "$Work-reference-audit.json"
& $Py scripts/phase2e_diagnostic.py replay --root "$Work\reports" --output "$Work-cache-replay"
```

若G1或G3失败，没有对应新诊断/参考，后两条不适用；独立核算仍验证实际已产生的
证据并保留partial门状态。开发cache replay验证每一份新完成的精确prompt/cache/
评分映射；诊断replay验证全部新动作和真实在线DES标量，额外DES单列，均非新推理。

R1分解独立入口仅依赖固定父ZIP与标准库：

```powershell
& $Py scripts/phase2e_r1_decompose.py --help
```

历史兼容按`phase2d_r2.py captures`及必要的单run`replay --probe-run`验证；
不重复无关历史60-run，探针和普通测试成本另计。Git变更清单和提交建议在报告中，
本轮不执行add、commit或push。
