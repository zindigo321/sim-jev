# 2C 真实 Jev 接入验收

本轮只修接入与审计，并提供冻结案例的严格 live 入口。没有新增 PPO、PHM、终端价值、成本/J720
或正式统计实验。原离线实验入口默认仍为 stub；原配置、候选、短窗评分、M16/K4/B8 和
planning_seed=20261102 不变。真实接入状态必须以对应 `smoke.json` / `stage.json` 的实际证据为准。

## 顺序和总预算

先在本地安全配置运行进程的 `TYPESAFE_API_KEY`，不写入源码、日志或 checkpoint。
每次初次执行使用一个全新目录，以下后缀应按执行批次替换。只有前一阶段真实通过后执行下一阶段。

```powershell
& 'D:\conda_envs\sim-jev\python.exe' scripts/jev_smoke.py --allow-live --max-http-requests 3 --total-timeout-seconds 120 --output runs/jev-live-smoke-next
& 'D:\conda_envs\sim-jev\python.exe' scripts/phase2c_live.py fixed-state --allow-live --max-http-requests 3 --total-timeout-seconds 120 --output runs/jev-live-fixed-next
& 'D:\conda_envs\sim-jev\python.exe' scripts/phase2c_live.py pilot --scenarios balanced scarce --seeds 0 --allow-live --max-http-requests 24 --total-timeout-seconds 1800 --output runs/jev-live-pilot-next
```

| 阶段 | 全阶段 HTTP 尝试上限（含重试） | HTTP 总时限 | 固定范围 |
| --- | ---: | ---: | --- |
| smoke | 3 | 120秒 | 有效真实Choice → DES → 唯一周命令；新进程恢复已提交命令 |
| fixed-state | 3 | 120秒 | 同一合法状态、同池六方法；Jev-direct/des共用完全相同响应 |
| pilot | 24 | 1800秒 | balanced/scarce，各seed0，六方法，12组/60次周决策 |

三阶段最多30次尝试。pilot 的24次是整个阶段的唯一额度，不是每run额度。
四种DES方法正常32次/周，闭环目标1280次；空池、单候选和mandatory fallback按原规则计实际数。
HTTP时限到期后禁止新请求，已完成缓存读取、DES及离线审计可以继续；总墙钟另行报告。
生产使用固定 `jev-1.14.0` 与官方 HTTPS；2026-11-02官方文档仍列出该模型，
输入价格 USD 0.042/Mtok、输出免费。文档检查不等于账户级真实服务可用性验证。

缺 key 在 HTTP 前退出2并保存 not_run；协议、认证、模型、预算或缓存错误退出1并保留证据。
strict_jev=True 禁止用 heuristic fallback 凑齐严格验收。fixture永远单独标记，不能成为live响应缓存。

## 计量与恢复

每阶段复用一个 ChoiceAdapter。发送前持久 reservation；每 attempt 有唯一ID及phase/run/decision归属。
启动时间和总额度恢复时保持，崩溃或uncertain不退还额度。重试已知usage求和，未知计量单列；
成功响应原usage、本次HTTP计量、缓存来源usage分离。金额只是已知量估算，不是账单。
run只导出自身audit增量；campaign按唯一attempt ID与持久账本核对。

live成功后自动精确real_replay。也可以独立复验捕获证据：

```powershell
& 'D:\conda_envs\sim-jev\python.exe' scripts/phase2c_live.py replay --source runs/jev-live-fixed-next --output runs/jev-fixed-replay-next
& 'D:\conda_envs\sim-jev\python.exe' scripts/phase2c_live.py replay --source runs/jev-live-pilot-next --output runs/jev-pilot-replay-next
& 'D:\conda_envs\sim-jev\python.exe' scripts/phase2c_live.py resume --checkpoint runs/jev-live-pilot-next/runs/balanced/seed-0/jev-des/resume-point.json --expected-checkpoint runs/jev-live-pilot-next/runs/balanced/seed-0/jev-des/checkpoint.json --response-store runs/jev-live-pilot-next/response-store --max-http-requests 24 --total-timeout-seconds 1800 --output runs/jev-resume-next
```

`resume`保持live策略/历史，强制cache_only、无凭据、零HTTP。缺缓存严格失败；没有将live快照
改写成real_replay。未来未捕获的新请求仍需显式注入持有原预算的受控live运行时。
replay新实验可用real_replay模式；允许的I/O差异列于 `IO_PROJECTION` 与产物中，包含模式、
墙钟、HTTP/usage、缓存和attempt审计。保留物理配置、完整事件/队列、规划seed、候选规则/池、
合法请求/响应内容、方法、任务义务、命令、cohort和积分。外部服务不承诺exactly-once。

## 分支物理证据与打包

恢复校验增加完整metrics schema及域/时窗容量界；原科学模型不改版本。
请求和模板协议保留v1，计量扩展为 `http-attempt-accounting-v2`；旧预算确定性补ID并保留未知计量。
历史合法2A/2B/2C快照继续由原入口加载。

固定状态全分支与闭环每决策第0支进行显式确定性审计重放，保存完整产物并运行原独立auditor。
审计DES/小时/墙钟单列，不反馈候选选择。哈希与重放只证明一致性，不提供外部来源签名。

包内保留 `response-evidence.json` 中白名单请求、响应、来源和attempt预算证据，不打包整个
`response-store`；replay从证据重建临时store。没有真实响应时明确标未验证，不能用fixture填补。
包后全新无Git解压，核源码身份与完整测试；存在真实证据才运行零HTTP真实重放。

`engine_offline_verified`、`jev_live_smoke_verified`、`jev_live_pilot_completed`分别记录。
只有真实smoke完成、或12组严格pilot全部完成且预算/配对/日志核算通过，才分别置后两项为true。
12组仅每场景一个seed，不做p值、显著性、泛化或Jev优越性结论；真实模型呈现敏感性仍未测试。

本轮实际命令、退出码和交付状态见项目新目录 `runs/jev-acceptance-20261102-200026/` 的报告与账本。
