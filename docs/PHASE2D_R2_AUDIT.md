# R2 正式离线严格审计与历史兼容

正式入口为 `scripts/phase2d_r2.py`。所有验证器、旧版本renderer、仿真与缓存读取均执行当前R2代码。父包中的旧源码只作内容身份核验，不作为执行代码动态加载，也不替换模块或伪造 `code_fingerprint`。

兼容关系 `phase2d-r2-historical-r1-compat-v1` 只接受以下固定来源：

| 身份 | 值 |
| --- | --- |
| R1父ZIP SHA256 | `6acec98dcb5b9842a5639231b266047c6dc03d19f98fec758183fd95e15c0dcd` |
| 原科学源码指纹 | `88a245221a079462ee8d079d6585aa573f0924ce79a4cc2861255e3dc802af06` |
| freeze canonical SHA256 | `83567c1fb765ebd1180edf24d07035e220b79e8a141a237229c6f4a53353e91d` |
| 历史评分协议 | `local-preference-scores-v2` |
| 历史评分renderer | `lossless-tables-and-shared-json-scores-v2` |

入口先核验整个父ZIP SHA、重复/越界/符号链接条目、freeze及其封印，并逐项从父ZIP读取92份原科学源码/配置，核对大小与SHA。当前执行源码指纹与审计器文件集合指纹另列在每份 `command.json`；旧freeze原样保留。原有R1生产 `check_freeze` 仍要求执行源码与原freeze完全一致；本入口通过内容绑定的显式历史接口执行审计。

在终端把下面四个变量设为实际解压位置。`$Evidence` 是只读R1批次目录，`$R2` 是R2项目目录；父包源文件、证据目录和输出目录必须各自独立。解释器必须是已核验的仿真环境。各命令输出目录须不存在，重复执行请使用新名称。

```powershell
$Python = 'D:\conda_envs\sim-jev\python.exe'
$R2 = (Resolve-Path '.\r2\project').Path
$Parent = (Resolve-Path '.\sim-jev-phase2d-r1-20261103-191950-02.zip').Path
$Evidence = (Resolve-Path '.\parent\project\runs\phase2d-r1-20261103-191950').Path

& $Python -B "$R2\scripts\phase2d_r2.py" captures --evidence-root $Evidence --parent-archive $Parent --output '.\acceptance-captures'
& $Python -B "$R2\scripts\phase2d_r2.py" audit --evidence-root $Evidence --parent-archive $Parent --output '.\acceptance-strict-audit'
& $Python -B "$R2\scripts\phase2d_r2.py" replay --evidence-root $Evidence --parent-archive $Parent --output '.\acceptance-replay'
```

`captures` 对20份旧v1真实捕获和20份R1 v2开发真实捕获分别执行生产严格解析与独立cache-only读取。它没有新DES。`audit` 严格核对24计划窗口、19去重输入、141次原始生成、60运行300决策、34048正式DES与三方持久账本；参考标签直接读取重算标量，不重新执行24576参考DES。历史prompt的factory顺序只在持久request/pool的全部值、数组次序和canonical hash精确相等后用于临时审计视图。原始评分、排序、schema、prompt、cache key、动作和物理分支哈希不放松。

`replay` 从零构造60个720h世界，用旧版本cache-only路径执行全部五个决策。额外6400次DES和实际分支模拟小时独立记入收据，不算作原34048预算。九份固定科学文件每运行逐字节比较，完整通过应为540/540；checkpoint仅沿用R1既有 `logical_decision_record` 和 `normalized_history` 规则，不扩展科学忽略字段或数值容差。`runtime.json` 来源变化单列。每次实际DES调用另行计数并与持久分支账本核对。

小探针可以精确选择一个真实运行，例如：

```powershell
& $Python -B "$R2\scripts\phase2d_r2.py" replay --evidence-root $Evidence --parent-archive $Parent --output '.\probe-local-direct' --probe-run balanced-50-local-direct
& $Python -B "$R2\scripts\phase2d_r2.py" replay --evidence-root $Evidence --parent-archive $Parent --output '.\probe-local-des' --probe-run balanced-50-local-des
```

探针结果为 `probe_passed`，`cache_only_replay_verified=false`，不声明完整验收。`--limit` 同样只作探针。正式完整回放不要传这些选项。

入口禁用socket、HTTP、模型检查及metadata路径，并以Python审计钩子禁止证据写入、目录创建、重命名和删除。标量审计/捕获命令另禁止 `PlanningScenario.evaluate`。只读账本拒绝缺失目录、缺失账本、pending/uncertain和任何需要恢复修补的状态。失败收据保留在独立输出目录；退出码非零。不会尝试live修复cache miss。

严格审计和旧缓存回放不需要模型服务、GPU、tokenizer或密钥。新renderer离线token预算使用单独公开工具及另列外部tokenizer资产/运行时；这三个命令保持原始独立token计数与服务器计数的严格一致检查，不声称重新执行新renderer真实推理。
