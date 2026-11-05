# 阶段2D交付验证

本轮为partial：指定Qwen在GPU真实运行，最终20个有效完整请求覆盖7个独立开发状态；scarce-10-168在两模板和固定纠错下仍输出重复ID。正式local诊断及60运行pilot未执行，建议先修输入或排序。

实际42次本地生成、411650输入/2786输出tokens、237.320秒；元数据90次、TypeSafe0次。24状态非模型基线及24576参考分支完成；在线2304 DES，独立审计通过，排除2个跨划分重复后22身份/8轨迹。

原测试308项，科学批次01为321项，批次02/03为322项，最终05为324项（新增16项）；最终validate/checkpoint/weekly/兼容性入口通过。旧34快照、18科学文件、旧2C的60checkpoint/540科学文件核对一致。20真实响应的短窗cache-only回放通过；真实闭环恢复未验证。

科学源码：62ae8329586a6c7dbdadc5024b0438a1b67edbe551661fd48d57a4bf2debb7e9。
最终交付源码：770f367ec6611c882c81b385f022a80d5e3c635d2d0ec0a27d933034aff7019a。中间审计及打包修复03/04不增加实验行；03包因签名URL漏脱敏被拒交，05已通过实际日志扫描。01–04源码分别在各自frozen-source，全部进入最终包。

报告：G:/gpt-file/Codex/outputs/phase2d-local-20261103-05/PHASE2D_REPORT.zh-CN.md。
审计包：G:/sim-jev-audit/sim-jev-phase2d-local-20261103-05.zip。
无Git测试、真实响应零推理回放及所有条目重读以同名.zip.verification.json和.zip.all-entry-reread.json为准，均是打包后伴随证据，不声称重新验证GPU。
执行/续跑：docs/PHASE2D_COMMANDS.md。未提交、推送、PR或tag。
