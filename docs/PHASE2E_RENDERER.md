# 阶段 2E-A：公开字符串可逆编码

新 renderer 为 `lossless-scoped-candidate-id-tables-and-shared-json-v4`，评分仍为
`local-preference-scores-v2`：每个短 ID 一个 0—100 严格整数；布尔、浮点分数、
重复或缺失键、额外字段失败。同分按原稳定候选 ID 排序。最终沿用 template-a
`public-portfolio-scores-v2-a` 的语义目标和评分 schema。

v3 对整个 canonical 的所有字符串按候选 ID 反向映射缩写。公开 summary 扩展字段
分别包含字面量 `C00` 或该短 ID 对应的原候选 ID 时，两个合法 canonical 不同，
但实际 prompt 相同。外层 canonical/cache 身份仍不同；这是接受 API 域内的编码
非单射边界，不是缓存哈希碰撞，也没有证据证明原 R1 冻结候选池受污染。

v4 只缩写 canonical 顶层 `candidates` 数组中每个候选记录的 `id` 字段。其他任何
字符串保持字面量，包括 decision、command、summary 的任意嵌套位置中与短 ID
或原候选 ID 恰好相等的字符串。不修改业务 ID、不删除字段、不舍入。

模型输入增加 `candidate_id_encoding=candidate-record-id-field-only-v4`。解码顺序：

1. 按 `canonical-object-order-and-literals-v3` 解开共享引用、表和 `$literal`。
2. 仅将顶层 `candidates[i].id` 的短 ID 用 `candidate_mapping` 替换为原 ID。
3. 其他字符串不替换。模型输出评分键仍为 mapping 中的短 ID。

真实 system prompt 明确写出这套作用域约定。`identity_v4` 生成新身份，v4 的
`template_sha256` 绑定基础 SCORE 模板加完整 v4 解码说明；renderer 身份、协议、
schema 和全部实际 prompt 同时参与缓存身份。只改 renderer 标签而不更新绑定
hash 的跨版本错标失败。v1/v2/v3 的模板 hash、解释、prompt 和分派保持原样。

编码支持公开 JSON：对象键为字符串；数组有序；值可为字符串、布尔、null、
整数或有限浮点数。对象插入键序无语义，按 Unicode 键序编码。数组和候选数组
顺序保留；整数/浮点/布尔类型、浮点精度和有符号浮点零保留。候选 ID 必须是
唯一非空字符串，空候选池也支持。非有限数和非 JSON 类型不在支持域内。
`$ref`、`$columns`/`$rows`、`$literal` 形状的业务对象由既有规范转义保留；
它们的字符串内容与候选别名不发生全局替换。

`decode_v4_input` 从真实 rendered_input JSON 恢复原 canonical。`render_v4`
在每次渲染时实际序列化并解析即将发送的完整输入，再用该 decoder 解码，使用
排序的严格 JSON 文本与原 canonical 比较，拒绝数字类型或值不一致；不依赖
Python 将 `1 == 1.0 == True` 视为相等的语义。检查覆盖完整
canonical → wire encoding → decode 链路。

新 API：`identity_v4`、`render_v4`、`decode_v4_input`、`REVERSIBLE_RENDERER`。
`render_for_identity` 先严格验证再按版本分派。未知 renderer、未知输入编码、
跨版本错标、cache miss 均明确失败，不尝试旧 renderer 或联网 fallback。

离线回归入口：

```powershell
& 'D:\conda_envs\sim-jev\python.exe' -m unittest tests.test_phase2e_renderer tests.test_phase2d_r2_renderer -v
```

测试从实际 prompt 提取 wire JSON 后解码，覆盖原反例、嵌套字符串、reserved
markers、数值类型、数组顺序、键序与新进程、缓存隔离及严格评分。fixture
不记作新真实推理。完整开发输入 token 核对与新 renderer 真实 GPU 推理门由
各自独立收据报告，代码测试不代替真实门验收。
