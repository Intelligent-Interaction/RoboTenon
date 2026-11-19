# 固定版本的参考实现

[English](README.md) · [**简体中文**](README.zh-CN.md)

本目录的 `src/vla_eval/runners/sync_runner.py` 原样取自 allenai/vla-evaluation-harness，
提交为 `6cc3e1bd9fc4bed26fb0a3aabd342c5fed11ca09`，使用 Apache-2.0 许可证（见 LICENSE）。
SHA256 为 `cf6234c83d8599f49dc8f212d4f8225967a71a937e8ff3e1dbc0873b6931d197`。

一致性测试直接执行上游方法的原始抽象语法树，无需导入整个上游
包。测试只移除类继承关系与类型注解导入，然后使用同一份确定性输入，
比较该方法与 RoboTenon Graph 的执行结果。
