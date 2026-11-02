# 2C 审计包和源码身份

新增源码作用域为 `project-source-and-config-v2`。它沿用 v1 的源码/配置后缀与项目相对 POSIX 路径，覆盖未跟踪源码，忽略时间戳、链接、环境、输出、缓存和审计材料。v2 另外明确排除根目录 `MANIFEST.json`、`AUDIT-MANIFEST.json` 以及 `cache/caches/.cache` 目录；返回值中的 `scope_definition` 冻结完整规则。源码摘要仍对按路径排序的文件记录（路径、字节数、SHA256）计算严格规范 JSON 的 SHA256。

历史作用域 `project-source-and-config-v1` 保持可调用；验证历史记录时按记录里的 scope 计算，不重写历史指纹或报告。包级清单不属于源码，新的 ZIP 中它位于 `MANIFEST.json`，源码根为并列的 `project/`。包清单逐项覆盖全部打包文件（包括文档、显式选定的运行证据）；清单自身不进入其文件列表，避免自引用。

`scripts/package_audit.py` 用全新路径打包、生成 `.zip.sha256`、逐项重新读取并核对 ZIP，解压到全新目录后启动该目录中的 Python 源码核对 fingerprint。解压项目没有 `.git`，并用 `GIT_CEILING_DIRECTORIES` 排除上层仓库误归属；临时 Git fixture 仍正常执行。验证子进程不继承 `PYTHONPATH` 或 `TYPESAFE_API_KEY`。

完整自验收依次运行 unittest、旧科学输出验收、2A checkpoint 验收和 2B weekly 验收。实际命令、cwd、退出码、耗时和日志写入解压根外层的 `SELF-ACCEPTANCE.json`，没有以清单自洽代替源身份。归档包含源码、配置、测试、文档、审计记录，以及每个 `--evidence runs/...` 显式选定的证据目录；其余历史 runs 保留在原地。任何已有 ZIP、SHA256 文件或解压目标都拒绝覆盖。

示例（实际交付路径以本轮验收记录为准）：

```powershell
& 'D:\conda_envs\sim-jev\python.exe' scripts/package_audit.py --archive 'G:\sim-jev-audit\sim-jev-phase2c-NEW.zip' --evidence runs/phase2c-20261102 --verify-into 'G:\sim-jev-audit\phase2c-NEW-extracted' --acceptance full
```

测试 `test_phase2c_packaging.py` 实际创建临时合成项目，完成验收身份冻结→打包→全新解压→源码身份比较→真实子进程无 Git unittest，并验证修改包内字节会在运行测试前拒绝。它不递归运行整个项目测试、不跳过断言，也不携带真实 `.git`。
