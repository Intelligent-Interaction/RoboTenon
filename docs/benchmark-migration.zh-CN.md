# 独立 benchmark 扩展迁移

[English](benchmark-migration.md) · [**简体中文**](benchmark-migration.zh-CN.md)

各 benchmark 过去分别维护一份纳入版本控制的 Graph 配置。迁移后，每个扩展维护自己的评测协议，统一入口根据协议生成 Graph。

## 当前接口约定

目录中每个 benchmark 都遵循以下约定。

- `vla.py` 是模型接入的唯一接口。
- `robotenon_benchmark_support.evaluate` 是统一评测命令。
- `benchmark.py` 负责创建仿真器并实现评测协议。
- `_internal/` 保存迁入扩展的私有辅助代码。
- `configs/*.json` 保存根据上游配置编写的协议 profile。
- `benchmark-source.json`、`LICENSE.upstream`、`NOTICE.upstream` 和 `protocol-reference/` 记录代码来源与许可信息。
- 运行时代码均不导入 `vla-evaluation-harness`。

迁移移除了旧版 `native/` 包、`native_test*.json`、`native_eval.json`、根目录下的 Portal 配置副本，以及各 benchmark 单独维护的 Graph 和 Module 清单。旧版 LIBERO 的策略和 Graph 适配器、XR365 的规划与验证路径，以及 RoboDojo 通过 RPC 执行 Graph 的路径，也已从这些扩展中移除。

## 使用方法

```bash
# 1. 实现独立的模型后端。
$EDITOR /path/to/my_model/vla.py

# 2. 选择上游协议配置并运行评测。
Tenon evaluate --benchmark libero \
  --backend /path/to/my_model/vla.py --model-python /path/to/model/python \
  --sim-python /path/to/libero/python \
  --profile extensions/benchmarks/libero/configs/spatial.json \
  --output runs/libero
```

RoboCasa365 还支持 `--profile xiaomi_official --backend ./extensions/benchmarks/systems/robocasa_hierarchical/tool/xiaomi/vla.py --backend-config ./extensions/benchmarks/systems/robocasa_hierarchical/tool/xiaomi/config.json`，可启用多个模型副本和共享动态合批。测速工具独立于准确率评测。

## 验证项目

仓库测试覆盖公开包结构、目录与协议配置的一致性、从 profile 生成 Graph、隔离环境下导入、生命周期调用顺序、观测传输时的数据类型、动作校验、遇到终止状态时立即结束、按任务和维度汇总、Worker 回收及失败统计。

`docs/changes/` 保存真实仿真器的冒烟测试和差异对比记录。各报告注明了自己的验证范围。只用合成动作跑通仿真器，尚不足以说明结果能复现上游官方得分。验证 Isaac 仿真器需要受支持的 RTX 主机和经过授权的资产。

## 特殊情况

- Kinetix 保留 `realtime.json` 以追溯协议来源。同步评测器会拒绝该配置，实时评测需要专用的实时 Graph。
- BEHAVIOR-1K 和 RoboDojo 依赖不同版本的 Isaac Sim，应使用独立环境。
- 在 RoboCasa365 上评测 Xiaomi 模型的准确率时，使用官方 target50 协议；测量吞吐时使用单独的测速工具。
