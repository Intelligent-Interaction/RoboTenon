# Kinetix：协议与实现参考

[English](NATIVE_EVALUATION.md) · [**简体中文**](NATIVE_EVALUATION.zh-CN.md)

此页保留旧链接。用户使用说明已集中到以下入口，避免多份命令发生偏差：

- [任务协议、profile 与启动命令](README.zh-CN.md)
- [环境安装与资产下载](SETUP.zh-CN.md)
- [统一评测与并行说明](../EVALUATION.zh-CN.md)
- [独立模型接口](../models/README.zh-CN.md)

环境协议实现在 `src/robotenon_kinetix/benchmark.py`；模型实现不属于环境协议。`benchmark-source.json` 记录迁移来源，不能据此推断所有仿真任务或最终成功率已与上游完全一致。
