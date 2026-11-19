# Xiaomi system tool compatibility entry point

[**English**](README.md) · [简体中文](README.zh-CN.md)

`vla.py` forwards to the [standalone Xiaomi backend](../../../../models/xiaomi/README.md) for existing system configurations.
For Xiaomi-only RoboCasa365 evaluation, use `extensions/benchmarks/models/xiaomi/vla.py` directly.
Model inference and preprocessing are maintained in `models/xiaomi`. This directory reuses that implementation.
