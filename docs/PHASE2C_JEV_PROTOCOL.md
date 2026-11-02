# 阶段 2C Jev Choice 协议

实现：`sim_jev/jev.py`。接口协议 `typesafe-choice-adapter-v1`，冻结模板
`weekly-portfolio-choice-v1`，模型固定 `jev-1.14.0`，不切换 `latest`。
这是合成周任务组合的选择适配器。Choice 概率和 confidence 只用于排名及诊断，
不是任务成功率、失效概率、风险约束或安全保证；不按 confidence 增加 DES 预算。

## 官方核对

核对日期为 **2026-11-02（Asia/Shanghai）**。本轮读取的官方页面：

- [模型与价格](https://docs.typesafe.ai/models)
- [System One 请求与响应](https://docs.typesafe.ai/api/system-one)
- [Choice primitive](https://docs.typesafe.ai/primitives/choice)
- [Confidence](https://docs.typesafe.ai/confidence)
- [错误分类](https://docs.typesafe.ai/api/errors)
- [重试策略](https://docs.typesafe.ai/sdk/python/api/retries)

本项目使用标准库 HTTP；不安装 SDK。此日期的输入价格为每百万 input tokens
**USD 0.042**，输出 tokens 免费。记录 `usage.input_tokens/output_tokens` 和价格来源，
费用字段是依据返回 usage 的估算，不声称是账单。限流等无 usage 的失败仍计请求数。
将来价格、模型或接口改变时应产生新实验身份，不回写历史结果。

## 请求、数据权限与排名

`build_choice_request(decision_request, candidates)` 只接受合法 `DecisionRequest.to_dict()`
和统一候选的 `id/summary`，构造：

```json
{
  "model": "jev-1.14.0",
  "state": {
    "decision": "合法公开请求对象",
    "candidate_summary_version": "public-portfolio-summary-v1",
    "units": {
      "time": "hours",
      "aircraft_hours": "aircraft-hours",
      "value": "synthetic whole-mission value points"
    },
    "template_version": "weekly-portfolio-choice-v1"
  },
  "questions": {
    "portfolio": {
      "type": "choice",
      "instructions": "代码中冻结的 INSTRUCTIONS 全文",
      "criteria": {"稳定候选ID": "该候选的公开摘要对象"}
    }
  }
}
```

图示中的说明字符串是说明占位，实际发送对象；没有完整 checkpoint、原研究文档、
执行 seed、真实寿命、维修 due_at、事件队列或执行 RNG。适配器递归拒绝相关禁用字段。
请求与响应存储均不包含 Authorization 或密钥。密钥仅在真实调用时从
`TYPESAFE_API_KEY` 读取，作为瞬时参数传入 HTTP 函数。

请求发往 `POST https://api.typesafe.ai/v1/systemone`，使用 Bearer 认证。所有字典键使用
确定性的排序；criteria 按稳定候选 ID 呈现。请求 SHA256 对完整严格 JSON 计算，
独立保存模板哈希及候选映射哈希。固定呈现顺序是可重复输入约定，**没有验证真实模型对
不同呈现顺序完全不敏感**。没有 live 对照时，呈现敏感性只能标为未测，不能从 stub 推断。

响应校验要求：

- `model` 与固定版本精确相同；`answers.portfolio` 是对象，`type` 若返回必须为 `choice`。
- `choice` 是池内字符串 ID；`probabilities` 的键与整个候选池完全相同，不补缺失候选的零概率。
- `choice` 必须属于概率最大值集合；严格低于最大值的池内 ID 同样拒绝，任意并列最大值合法。
- 概率为非布尔有限数，位于 `[0,1]`，总和与 1 的绝对差不大于 **1e-6**。
- confidence 为非布尔有限数且在 `[0,1]`；usage 两个 tokens 字段为 `[0,2^63-1]` 整数，拒绝布尔数及无法安全计费的超大数。
- 只保留模型、Choice 的三个字段和 tokens usage；不保存可能回显凭据的任意服务端错误正文。

客户端按 `(-probability, candidate_id)` 排序，再由 resolver 截 Top-K；API 不提供 Top-K。
响应的 `choice` 另行保留；并列最大值时可以不同于客户端按稳定 ID 选出的第一项。空池或单候选返回确定性路径，
`api_skipped_reason=empty_pool/singleton_pool`，HTTP 次数为 0，`valid_jev=false`。

## 三种模式及失败处理

| 模式 | 数据来源 | 对科学结果的标注 |
| --- | --- | --- |
| `live` | 默认 urllib transport 的真实 HTTP 响应 | 通过完整校验才 `valid_jev=true` |
| `real_replay` | 既有 `.live.json`，来源为 live，完整请求/模型/模板/候选映射匹配 | 实际网络 0 次，保留原 live 来源；不作为新的服务调用 |
| `stub` | `artificial-stable-id-triangular-v1` 人工 fixture，按 ID 分配三角权重 | `valid_response=true`、`valid_jev=false`，只用于管线验收 |

人工 fixture 不读取价值启发式或未来 DES 标签。注入 `transport=` 只允许 live 协议单元测试，
显式标记 `transport_kind=injected_test_transport`、`valid_jev=false`，保存到 `.fixture.json`。
这些文件不能被 `real_replay` 当作真实响应使用，也不构成外部 live 验证。

生产 transport 固定官方 HTTPS 端点，并使用禁重定向 opener 拒绝全部 3xx。
不跟随同源、跨主机、跨端口或 HTTPS 降级 Location，不保存 Location 或错误正文。
真实 HTTPError 正文不读取；这类尝试的 usage 明确为未知，而不是推定为零。

无效响应、认证失败、版本不符或 replay 缺失均抛出 `JevError`，携带脱敏 `code/audit`。
适配器本身不伪造成功排名；主 resolver 捕获错误后执行预先冻结的 heuristic fallback，
必须单列失败、fallback、有效 Jev 比例及混合策略结果。严格 smoke 可以直接失败。

## 调用预算、持久化与恢复

`JevConfig` 是可序列化参数，不含 API 对象或凭据。live 需要 `allow_live=True` 和
`cache_dir`，默认最多 **10 次 HTTP（含重试）**、总时限 **120 秒**、单请求 **30 秒**、
单次 rank 最多 **2 次重试**。缓存目录也是本次调用预算的单写入者目录；正式 pilot 应
显式设置总调用数及总时限，并使用自己的新目录。旧目录的预算配置不允许被静默改大。

实际 transport 运行于每个已预扣尝试对应的 daemon worker，调用方以单调时钟的绝对
deadline 等待结果，等待上限取单请求时限与当前剩余总时限的较小者。因此连续慢速返回
字节不能通过重置 socket inactivity timeout 延长调用方等待。worker 不持有 adapter、
账本或缓存写入回调；超时后的迟到响应被丢弃，不能补写成功缓存。未结束 worker 数受
HTTP 尝试总数约束（默认最多 10），而远端处理是否已经发生仍为 uncertain；这不是
服务端取消保证。持久化文件操作仍遵从操作系统 I/O 行为。

400/401/422 分别归为请求/认证/schema 错误，不重试。429/529 有界指数退避，同时读取
`Retry-After`（秒或 HTTP 日期）及 `retry-after-ms`。初始 0.5 秒、上限 8 秒；等待时间
计入总时限。若服务器要求的最短等待超过退避上限或剩余总时限，明确以
`retry_after_exceeds_budget` 停止，不截短服务器要求后提前重发。网络超时也有限重试，
标为 uncertain。每次网络前持久写入预算 reservation，
避免重启退还可能已经发生的调用；崩溃遗留 reservation 保持 uncertain 并占用预算。

成功响应使用同目录临时文件、flush/fsync、`os.replace` 原子写入；完整请求身份匹配才可命中。
保存了命令的执行 checkpoint 恢复由 engine 直接续行，不再次调用过去的 resolver。
缓存命中时 `actual_http_requests=0`、`actual_usage=0`，原 response usage 另行保留。
实际延迟、网络尝试、缓存命中和错误审计独立于模型逻辑等价比较，不能把这些实测字段改为
相等来制造恢复一致性。

### 本轮计量扩展与 cache-only 恢复

请求/模板/适配器协议仍为 v1；新增计量协议 `http-attempt-accounting-v2`，不改变科学模型。
每次发送前持久化 `budget_id:number` 唯一 attempt ID、phase/run/decision 归属和 reservation。
每次 attempt 的 `known_usage` 只接受两个 `[0,2^63-1]` 整数 tokens；缺失或非法计量为 null。
`actual_usage` 累加本次所有新 HTTP 的已知量；例如先429 `(7,1)`、后200 `(11,2)` 为 `(18,3)`。
`response_usage` 保留成功响应原计量，`source_usage` 保留缓存来源计量；cache hit 的新 HTTP/usage 为零。
`unknown_usage_attempts`、`usage_complete`、`uncertain_attempts` 分开记录。
费用仅是已知 input tokens 按核对价格估算，不是账单；未知费用未被估成零。

旧持久预算可确定性补充 attempt ID，原 started_at、次数/时间额度不变；无法恢复的旧计量标未知。
live 身份的 `network_policy=cache_only` 只允许真实成功缓存，禁止网络且无需凭据；缺原预算拒绝初始化，
缓存缺失明确失败。过期 deadline 不阻断已完成缓存的读取和无网审计。已有历史策略/记录不改成 real_replay。
全局 campaign 以持久 attempt ID 去重核算；run 只导出自己的新增 audit_log 区间。

当前严格接入入口、3/3/24 总预算和复验方法见 [真实接入验收](PHASE2C_LIVE_ACCEPTANCE.md)。

外部服务不承诺 exactly-once。响应未持久化即崩溃或超时可能已经被服务处理；原始 ledger
保留 `uncertain`、`prior_uncertain_attempts` 及已消耗预算。缓存与哈希用于匹配和完整性核查，
不是服务端签名或来源的密码学认证。

## 独立验证入口

```powershell
& 'D:\conda_envs\sim-jev\python.exe' -m unittest discover -s tests -p 'test_phase2c_jev.py' -v
```

测试覆盖完整/缺失概率、非法 ID/类型/模型/数值、稳定排序、空/单候选、显式 live 开关、
缺凭据、400/401/422、429/529、超时、重试和总调用上限、总时限、崩溃 uncertain、响应缓存
写入失败、缓存命中及 replay 缺失/身份不匹配、凭据脱敏和测试 transport 隔离。
另外用阻塞 transport 和真实本地 HTTP 逐字节慢速响应验证调用方绝对时限，
确认迟到成功不修改预算账本或写入缓存，并检验超过预算的 Retry-After 不会提前重试。
实际最终测试数和退出码以对应运行日志为准。未运行真实 HTTP 时，交付必须保持
`jev_live_smoke_verified=false`、`jev_live_pilot_completed=false`；不能用这组测试代替 live 证据。
