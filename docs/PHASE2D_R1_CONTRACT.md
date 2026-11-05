# 阶段 2D-R1 契约

本轮增量保留阶段2D的partial结论、42次真实生成和26880正式DES。现有未提交修改先封存；不得覆盖旧输出、提交或推送。仿真使用 `D:\conda_envs\sim-jev\python.exe`。只允许已固定Qwen本地推理，不读取TYPESAFE_API_KEY，不改Conda、驱动、全局PATH或代理。

执行门依次为F1/F2/F3复现和恢复修补、普通测试与legacy复核、独立v2离线协议验证、8开发状态真实覆盖和20次新生成、最终不可变预注册与源码冻结、24状态诊断和独立参考、seed50首12运行核算、全60运行、断网新进程缓存回放。门未通过时不运行新的留出状态或标签；效果为负不阻止已通过工程门的后续实验。

F1使用checkpoint专用保序原子写入器。F2审计按决策、候选、情景和物理哈希持久化，checkpoint提交前证据必须落盘；恢复核验、合并、去重。实际计算尝试与已提交逻辑分支分账。无法确认的在途尝试保持pending/uncertain并阻止自动重算，无exactly-once声明。F3采用显式新算术与请求协议；旧协议默认保持。具体锚点与兼容方案见PHASE2D_R1_ARITHMETIC.md。

离线普通暂停预先固定为31.3、47.125、95.75、191.375、335.2、511.625、671.875小时。选点只依赖时钟，不查询未来隐藏真值；报告各点已公开的在飞、等待、维修、在途覆盖。比较连续、多次暂停和新进程JSON恢复的公开累计、请求、池、规划情景和缓存身份，不舍入、不放宽精确比较。

v1的解析器、模板、渲染和cache保留。新local-preference-scores-v2要求每个实际短候选ID恰出现一次，0至100整数，拒绝布尔、非有限、重复、遗漏和额外字段。程序按(-score,原稳定候选ID)排序。评分是未校准偏好，不是概率或长期Q。同分合法，另报同分和全同分率。最多一次固定格式纠错，无fallback，无额外模型修复。两套v2模板在生成前预登记并共享持久额度。

固定模型Qwen/Qwen3-4B-Instruct-2507；Ollama标签qwen3:4b-instruct-2507-q4_K_M；digest=sha256:0edcdef34593eac1aa2be9c7d06c432dcf81945adca5eca2f27662c18f168ba0；Ollama0.40.13。解码temperature=0、seed=42、num_ctx=16384、num_predict=1024、num_batch=512，raw=true、truncate=false、shift=false。保留无损反向映射、固定tokenizer独立计数和实际prompt_eval_count核对。服务只监听回环，禁云，仅管理本任务PID。

开发为balanced/scarce×seed10/11×168/672，共8状态。每套模板先完整尝试8状态，不能跳过scarce-10-168；选定模板全部覆盖后，按冻结顺序补足20次不同attempt ID的新真实完成。cache/fixture/旧v1不能算新成功。

新留出为balanced/scarce×seed40/41/42/43×168/336/672，共24计划状态、8条mandatory-first轨迹。开发seed10/11及旧诊断seed20–23均为已见；同新协议重建公开请求用于去重，不读旧MC标签供选择器。记录request+pool以及剔除纯身份包装后的业务投影重叠，跨集重复排除独立效果汇总，内部身份只计一次，不换seed。

六方法mandatory-first、random-des、heuristic-des、local-direct、local-des、direct-des，共享合法信息、M≤16、K=min(4,M)、B=8、planning_seed=20261102。固定状态两个local方法共享一次真实评分。空/单候选跳过模型及额外DES。四DES方法每状态最多32分支，direct-des全池轮转。全部在线动作和响应封存核验后，才生成reference_seed=20261103、index0–63、每实际候选64共同情景，标签与选择器cache隔离。严格协议失败保留partial，不自动生成该批参考。

q_ref为当前offer本窗已实现DSR+RCR均值；Regret@K=max_pool−max_retained，动作遗憾=max_pool−selected。direct-des的Regret@K为N/A，不构造事后Top-K。数值差≤1e−12为数值平局；64成对差均值±1.96×样本SD/8包含0为MC近似平局。保留全部分支标量、标准误和配对差。按唯一身份及8预定轨迹汇总，不当作24独立样本、不报告正式p值、显著性或规则泛化。明确有限池与MC max选择偏差。

pilot为balanced/scarce×seed50–54×六方法，共60运行300周决策。先seed50的12运行核算，再继续51–54。每(seed,scenario)按(seed−50+scenario索引)循环移位六方法，方法交错；每run独立响应store。任务/规则哈希和实体CRN成对一致，各方法用自己的合法状态。DSR/RCR/ASR按累计分子分母计算，拒绝任务保留分母。并列义务缺口、接纳/成功、AFH、可用度、等待、库存/在途及实际计算成本。

R1共享campaign额度：开发80、诊断48、pilot200次生成，总328；累计7200秒，单次120秒。超时/中断保留uncertain额度且不立即补发。元数据独立计数，远程推理0。正式DES上限34048=3072在线诊断+24576参考+6400pilot；开发、兼容、额外审计重放另账。每实际分支经过独立auditor。完整物理产物固定保留每状态最小稳定ID候选参考情景0，以及每闭环决策选中候选分支0；额外重放不反馈选择。

源码/配置/模板冻结后才能正式运行，任何代码修改需要新批次。回放仅允许预先声明的I/O与墙钟字段差异；精确身份和科学输出不得删除字段或增加容差。无法测量的资源指标为null，采样最大值不称CUDA精确峰值，不伪造能耗或货币成本。valid_local_model和valid_jev独立；官方Jev仍未验收。
