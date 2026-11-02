# 2C 统一可行候选协议

候选生成器 `generate_candidates(request, target_size=16, planning_seed=20261102)` 仅接收经过白名单和内容哈希验证的 `DecisionRequest`。返回 `Candidate(id, command, summary)` 列表；实际 M 为列表长度，不复制候选补足目标数。各方法使用同一生成器和 `validate_command`。函数不运行 DES，不访问执行世界、RNG、内部 checkpoint 或未来标签。

`public-feasible-pool-v1` 固定如下生成顺序：mandatory-first 参考；mandatory-only；按价值、价值/完整 AFH、短 AFH、早计划结束排列的贪心组合；已有组合的局部删除/增加/交换；独立哈希随机子集与随机顺序贪心组合。最多进行 `min(8192,max(256,128*M_target))` 次生成可行性检查和 `64*M_target` 个随机尝试。可选任务不超过四项时穷举有限组合后返回。每个新增结果还进行一次共用验证，规范化命令诊断。

参考候选始终位于非空生成结果首项。mandatory 或 carry 本身不可行时，直接返回既有统一确定性 fallback 一项，保留义务、缺口和原因。无 offer 时返回唯一合法空命令，全 mandatory 时直接返回参考；显式 `target_size=0` 返回禁用的空池供边界处理，正式策略配置要求目标至少为一。其余不足目标数的池在有限搜索后如实返回，没有重复填充。保留参考不保证最终所选策略的实绩不下降。

稳定 ID 为 `C-` 加 SHA256 前 24 个十六进制字符，哈希内容为协议版本、window_start 和排序后的 accepted_ids。ID 与生成顺序、算法名称、候选位置及执行 seed 无关。生成随机键 `sha256-request-search-v1` 只含独立 planning_seed、合法 request_id、尝试号、任务 ID 和用途；这些是候选搜索键，与 DES 外生情景键分开。候选字典按 ID 去重。

摘要 `public-portfolio-summary-v1` 用严格有限 JSON 保存公开任务规格、接纳数量/合成价值、当前 offer 数量/价值分母、计划 AFH/峰值/最小备用、carry 规格、原 mandatory/缺口、fallback、当前规则满足情况、已公开规则，以及飞机/维修/订单/库存公开状态。`units` 固定时间为 h、需机为架、AFH 为飞机小时、价值为合成价值点、库存为单 LRU 件、比率为无量纲；不对浮点值另作舍入。没有真实寿命、未来成功率、参考 Q 或 MC 标签。

固定启发式 `planned-completions-dsr-rcr-afh-v1` 对当前 offer 中接纳且计划结束不晚于本窗终点的任务，计算 `计划完成数量/全部offer数量 + 计划完成价值/全部offer价值`。零分母分项为 0，另存有效性。carry 只影响资源；跨终点任务不提前计入计划完成价值。按该值降序、计划总 AFH 升序、稳定 ID 升序排序。它是名义计划代理量，并非 DES 已实现分数、成功概率、真实收益或终端价值；实际故障和派机不足仍由后续 DES 及固定日执行器处理。

验收 `tests/test_phase2c_candidates.py` 覆盖 M=16 去重/可行性、参考保留、mandatory、carry 超限 fallback、空/单/不足池、跨终点价值、稳定排序、随机隔离、公开输出隔离、参数边界以及禁止 DES 预评。
