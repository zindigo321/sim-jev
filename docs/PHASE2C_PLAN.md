# 2C 实施顺序与验收门槛

日期：2026-11-02（Asia/Shanghai）。在已有项目上增量实现，不改变历史研究计划、论文、输出或归档。

1. M0：修复历史命令合法性、独立 cohort/as-of 核算、Git 夹具及源码指纹作用域。保留改前复现与修后反例；先通过全部旧回归。
2. M1：合法 `DecisionRequest` 和公开参数白名单；旧策略也经同步 resolver、统一校验和唯一提交。分支终点先排空物理事件，再停止于下一窗公布之前。
3. M2：从请求和独立规划随机源构建规划世界；条件寿命、条件维修服务、公开 ETA 和现有占用驱动同一物理处理器，记录规划 origin，不制造执行历史。
4. M3：确定性/随机有限尝试构造至多 16 个唯一可行候选；固定 K=4、B=8。预筛后 DES 与直接 DES 使用相同总分支预算，记录实际数量和时长。短窗目标是当前 offer cohort 的已实现 DSR+RCR，无终端价值。
5. M4：固定 jev-1.14.0 Choice HTTP 协议、类型校验、有界重试、显式 fallback、原子真实响应缓存；live/real_replay/stub 分开。凭据仅运行时注入。
6. 验收：两场景各五个配对环境种子的六种方法；同状态离线比较、隐藏信息不变性、分支隔离、跨进程恢复、旧科学输出/归档兼容。源代码冻结后打包到 G:\\sim-jev-audit，再全新无 Git 解压核对和实际执行。

各阶段失败先修复再继续。环境固定为 `D:\\conda_envs\\sim-jev\\python.exe`；不安装新依赖，不提交或推送。

组件边界：`decision.py`（请求和策略描述）→ `candidates.py`（纯候选生成）→ `jev.py`（仅合法请求的 Choice 适配）/`planning.py`（独立规划世界）→ `decision_resolver.py`（选择和预算）→ 现有周提交/固定日执行/物理事件。完整内部 checkpoint 和执行随机 manifest 不进入决策边界。

交付分别报告 `engine_offline_verified`、`jev_live_smoke_verified`、`jev_live_pilot_completed`。此规模是管线检查，不是正式显著性实验；PPO、长期价值和 PHM 收益均不计入 2C 成果。
