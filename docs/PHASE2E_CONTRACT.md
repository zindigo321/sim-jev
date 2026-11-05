# 阶段2E-A执行契约

本轮范围由2026-11-04用户任务确定：可逆renderer v4、原R1标签损失分解、
新renderer真实开发门、通过后最多40个新环境固定状态诊断、独立参考与审计交付。
历史文档只作背景。既有dirty、freeze、失败、partial和响应全部保留；不暂存、
提交或推送。启动源码指纹749a436e56d993ba1e0f1c759e471bdf2e173a36213a18e6d439d849d0a76e39。
本轮执行源码另行冻结，修改科学执行代码须新批次；不能沿用原标签。

G0：新编码只缩写候选记录ID字段，全部其他字符串为字面量。实际wire JSON解码
后与原canonical严格JSON一致。对象键序不影响结果；数组顺序、数值类型和精度保留。
版本、模板解码说明、cache和schema身份严格绑定；旧v1/v2/v3解释保留。

G1：固定Qwen/Qwen3-4B-Instruct-2507，Ollama0.40.13、Q4_K_M、原模型digest和
运行时字节身份。仅template-a；temperature0、seed42、ctx16384、predict1024、
batch512、raw=true、truncate=false、shift=false、batch1、并发1。最多一次固定
格式纠错。先原8状态完整覆盖，再固定轮转至20真实逻辑完成。严格token/backend、
整数评分、GPU、身份和持久预算均通过且无pending/uncertain才开G2。

G2：balanced1000—1019、scarce2000—2019，每环境只构造本场景mandatory-first
轨迹至336小时，取336—504窗口。先查历史种子，冲突按确定规则替代并在新状态前
冻结。计划顺序、所有已见R1公开输入、业务投影、种子使用清单和分析代码先落盘。
输入构造成本另计，非候选DES。精确request+pool及去掉身份包装的业务投影均去重；
主分析仅未见业务代表，不补seed。

G3：六方法共享合法request/pool、M≤16、K=min(4,M)、B8、planning_seed20261102。
direct-des沿用最多32全池轮转。两个local共享一份实际评分，完全相同批内cache
在评分前声明为replay。240方法行动、原始响应、实际在线DES及三账全部核验封存后，
才允许独立reference_seed20261104、index0—63标签。参考隔离，不能反馈选择器。
所有标量和必要情景0完整物理产物保留；选择后回放另账。

不可变预算：开发40、诊断80、总120生成attempt；含纠错、失败和uncertain。
单次deadline≤120秒，开发1800秒、诊断5400秒、总7200秒。metadata≤400请求。
首次I/O前持久预约；uncertain保留占额且阻止自动补发。正式DES≤46080，分为
在线5120、参考40960，以真实分支核算。普通测试内部DES未知仍为null。
只用回环，禁下载、安装、远程模型、外网、密钥读取或环境/驱动/PATH/代理修改。
仅管理本任务自启且核对身份的PID，后台隐藏，结束保存停止收据。

主比较为regret_heuristic-des−regret_local-des，正值有利local；每场景按独立环境
配对，5000次bootstrap、固定随机种子和首出现代表规则在新标签前冻结。MC64
保留CRN配对并单列条件MC误差。分清环境误差、MC误差、样本max选择偏差及有限池
局限。不虚构unscreened方法的Top-K，不把集内损失全归因DES噪声。

仅两个场景各n≥10、均值≥0.005、各95%环境配对区间下限>0且全部工程门通过时，
建议后续更贵的跨场景公平闭环设计。该阈值是本轮资源决策约定。否则报告负结果、
条件信号或证据不足；不自动追加pilot。这里没有共同deadline/anytime，不声明
等墙钟优效、720小时mandatory非劣、长期可用度改善、长期Q或全局最优。
官方Jev仍未验收，PPO与价值训练仍未训练。
