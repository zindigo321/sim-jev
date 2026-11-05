# 阶段 2D 冻结契约

Qwen/Qwen3-4B-Instruct-2507，Ollama 精确标签 `qwen3:4b-instruct-2507-q4_K_M`，Q4_K_M。通用开放权重排序基线，官方 Jev 和 NeoHorse 验收仍为 false。

原单 LRU、720 h、周窗口及末窗 48 h 不变。local-direct 为零前瞻，local-des 复用统一候选生成/校验与 PlanningScenario；M≤16、K=min(4,M)、B=8、在线 seed 20261102。direct-des 原轮转分配不变。

独立 `local-candidate-des-v1` 策略与 `local-ranking-v1` 输出。完整 ranked_ids 无重复全排列；choice 为第一项，score/probability/confidence 为 null。无 heuristic fallback。运行时 JSON schema + 客户端严格验证，最多一次固定格式纠错。

模型只读原合法 DecisionRequest 和公开候选。渲染采用短候选 ID、无损 JSON 表格和重复公开结构引用，逐请求验证反解一致，不舍入。原文、映射、渲染内容及哈希全部保存。显式 ChatML raw prompt 使用原模型角色边界，运行时原模板另存哈希。独立原模型 tokenizer 计数，要求服务 prompt_eval_count 一致、输入加1024输出预留≤上下文、truncate=false、shift=false。temperature=0、seed=42；请求 batch/concurrency=1。num_batch=512 是单序列提示处理的 token 微批，不是多请求并发。

开发：balanced/scarce × seed10、11 ×168、672，共8状态。最多两模板；本轮实际比较两套，均在同一状态出现严格排列失败。开发硬件20个有效真实完成，重复输入仅证明稳定性。原生 Ollama API 与 tokenizers 独立环境；原 Conda 包不修改。硬件指标为外部采样，精确 allocated/reserved、无法获取的计量置 null。

留出：两场景 ×seed20–23 ×168、336、672，共24计划状态。均沿 mandatory-first 在提交前捕获；按完整合法请求与池哈希标跨划分/内部重复，不替换样本。local-direct/des 共享一次排名。全部在线动作落盘封存后，评价独立通道以 seed20261103、index0–63 对每实际候选生成共同参考情景。参考与模型响应缓存隔离。

q_ref 为64情景当前offer已实现 DSR+RCR 均值。Regret@K=max_pool−max_retained；动作遗憾=max_pool−q_ref(selected)。direct-des 不报告预筛 Top-K；规则动作无法映射池时写 null/N/A。数值平局绝对差≤1e−12；MC近似平局为与点估计最优候选的配对差均值±1.96×样本SD/8包含0。MC max有选择偏差，近似平局不是多重选择校正置信集；仅有限池短窗参考，无长期Q/全局最优声明。

闭环：两场景×seed30–34×六方法，60运行300周决策。首seed30的12运行通过核算才继续。顺序按(seed−30+场景索引)循环移位六方法；每run独立响应store；实体CRN与成对任务/规则哈希。统计比率按累计分子/分母。

本地生成上限：开发80、诊断48、闭环200；合计本地生成时间7200秒、每请求120秒、每逻辑请求最多两次生成。超时/中断保留 uncertain，阻止后续补发，不宣称底层已取消。正式DES上界34048=3072+24576+6400；开发、测试、兼容与额外审计回放另账。所有实际分支由独立auditor核算；保留全部标量、身份与物理哈希，完整物理产物固定保留每状态最小稳定ID候选参考index0、每闭环决策选择分支0。

成功真实响应原子保存后才返回提交。local_inference/local_replay/fixture 分开，valid_local_model 与 valid_jev 分开。本地元数据、本地生成、远程TypeSafe分别计次；TypeSafe为0。fixture不得写真实cache。无网络cache-only缺项即失败。checkpoint只存可序列化身份与已提交决定，不存服务URL、权重、模型对象/GPU状态。

冻结清单记录源码（包括未跟踪源码）、模型完整digest/blob SHA256、运行时版本/二进制、tokenizer、许可、模板、配置、划分、预算、缺失规则。冻结后改变源码必须新批次。运行输出保留失败，不用未运行冒充零效果。无正式p值、显著性或泛化声明。
