# 旧 Graph 覆盖层导致成功结果统计为零

[English](incident-2026-09-20-empty-graph-outputs.md) · [**简体中文**](incident-2026-09-20-empty-graph-outputs.zh-CN.md)

受影响实验：`exp-20260920-140038-9536464d`。

该实验覆盖层包含旧版完整 Graph 数组，没有公开 outputs。新版 runner 从 GraphResult.outputs 取成功信号，空字典使 task_success 变成 false，env_steps 变成 0，reason 变成 null。环境反馈和终止节点原始结果仍然保存完整；最初核查的 23 个完成任务中有 19 个 environment_success、4 个 horizon。

## 防止复发

Portal 提交、实验入口、worker 模型初始化前及 episode 入口检查所选 Graph 是否声明 outputs.success。缺失时明确报配置错误，不能把未配置当作失败任务。通用 GraphRuntime 不要求 success，只有实验运行器要求这个结果接口。Fork 复制旧参数仍需显式更新 Graph 映射，不能用节点名自动猜测。

## 原实验结果恢复

实验根目录的 result_recovery.json 保存显式 graphs 映射。Portal 仅在该文件存在时，对完成且 outputs 为空的记录，根据 context.root_scope 内的 node_records 解析指定输出。错误、取消、运行中、已有公开输出的结果不被重判；恢复信号必须为布尔值。汇总读取对应 episode_json，并校验 generation_started_at_ns，避免跨重跑代际取证。

修复现有结果前备份原始 JSON。运行中的旧 coordinator 保留内存中的旧统计，不能通过一次覆盖 summary.json 修复；Portal 对每次读取的汇总应用恢复映射，并重新计算成功数和成功率。该映射也覆盖后续完成的旧 worker 结果，无需中断仿真。原始 summary.json 可能仍是旧 coordinator 的统计；以恢复后的 API 结果为准。此次恢复不修改仿真轨迹、不把 completed 直接视为成功、不自动改写实验执行配置。
