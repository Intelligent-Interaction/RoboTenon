# 基准安装文档核验（2026-09-29）

[English](benchmark-setup-docs-2026-09-29.md) · [**简体中文**](benchmark-setup-docs-2026-09-29.zh-CN.md)

## 改动

- 新增通用安装与结果核对指南，以及 20 份基准专用 SETUP.md，各基准的 README 均链接到对应指南。
- 补充运行时版本、上游固定版本、可用的资产来源和下载命令、必需的路径配置、模拟动作命令、切换真实后端的步骤与验证范围。
- RoboDojo Office 的文档增加模拟器先决条件链接，保留其专用入口。
- 将 Simpler 的 OpenCV 固定版本从 4.12.0.88 改为 4.11.0.86。PyPI 元数据显示，Python >=3.9 下的 OpenCV 4.12 需要 NumPy >=2，与基准使用的 NumPy 1.24.4 冲突。文档指定的 Python 3.10 环境可使用 4.11。

## 核验结果

- `test_native_graph_configs.py`、`test_unified_benchmark_backend.py` 和 `test_live_benchmark.py` 共 37 项通过。测试使用 benchmark-test-env 环境、Python 3.11，PYTHONPATH 包含所有基准的 src 目录。
- 全部 157 个目录配置均可通过 `application_config`、`write_application_config` 和 `load_application_config` 生成并加载。
- 使用 `bash -n` 检查了 21 份安装文档中的 65 个 Bash 代码块。所有相对链接均可解析。
- 已确认固定版本的服务器参考资源包含 CALVIN 验证集的 `.hydra` 和统计数据，以及 LIBERO-Mem 预生成的初始状态。
- 已对照固定版本源码核对 ManiSkill2 的 `MS2_ASSET_DIR` 与 Simpler 的 `MS2_REAL2SIM_ASSET_DIR`。
- 已直接核对 PyPI 上的 OpenCV 依赖元数据。

本轮没有执行下载命令，也未逐个验证所有模拟器依赖能否在全新环境中安装。已有的原生一致性报告记录了 18 个非 Isaac 环境的有限实测。BEHAVIOR-1K 和 RoboDojo 仍缺少配有 RTX 设备和对应资产的验证。本轮没有新增全量准确率结论，没有重启评测进程，也没有向服务器正在使用的检出目录同步文件。
