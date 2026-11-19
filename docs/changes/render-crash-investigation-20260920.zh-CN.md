# AdjustToasterOvenTemperature 崩溃调查

[English](render-crash-investigation-20260920.md) · [**简体中文**](render-crash-investigation-20260920.zh-CN.md)

实验为 `exp-20260920-110524-2de9629b`，任务索引 0，种子 7。

## 证据

- 两次尝试（`8cd25285-3c3c-46c5-aca7-cad20776e9a2` 和
  `41d3aa6f-fa6d-4e5f-9bc4-9c5ccb538ae0`）均在 GPU 0 上以 SIGABRT 终止。
  两份 Python 故障堆栈均停在 robosuite 的 `binding_utils.py:174`，
  `mujoco.mjr_readPixels`.
- 两次尝试均在发生中断的批次前完成了 50 个 Body 执行批次。
  evaluator 一直报告 16 步，没有累加。
  任务配置的 horizon 为 450 步，因此崩溃前执行
  已超出原定评测预算。
- 版本为 MuJoCo 3.3.1、robosuite 1.5.2、RoboCasa 1.0.1、PyOpenGL 3.1.10。
- GPU 0 回放使用第二次尝试中记录的全部 816 个动作，
  在相同种子和 EGL 后端下通过 GDB 单独启动进程，正常退出。
  回放未运行模型推理、Portal 或 Trace 发布。
  此次回放未复现原始执行环境的所有条件，
  因此不能证明原生渲染故障已修复。
- GPU 1 有界回放使用相同动作，在第 450 步准确停止，
  环境未报告成功，渲染器正常关闭。
- NVIDIA 报告全部 8 张 GPU 的易失性不可纠正 ECC 错误数均为零。
  容器内无法访问内核日志，
  因此未核对驱动 Xid 记录。

## 修复

- 终止判断从 evaluator 自身上一次发布的结果累加步数，
  避免读取合并后仍有陈旧零值的 planner/verifier 快照。
- 动作生成使用 evaluator 的权威计数，根据剩余 horizon 限制
  最后一个动作块的长度，即使 Tool 返回过多动作向量也不会超出
  步数上限。
- 实时进度显式读取 evaluator 计数，
  后续陈旧的规划快照不能把显示的崩溃进度重置为零。
- 回归测试在真实 Graph/workspace 循环中设置 34 和 450 两种 horizon，
  并刻意提供过时的 planner/verifier 值和过长的 Tool 输出。

现有进程后端已支持 Worker 丢失后新建 Worker，并在独立的 attempt 目录下重试。
本次实验遵守 `max_retries=1`。
重试上限、种子、成功条件和已安装的模拟器版本均保持原有设置。

## 部署范围

源码已同步到 `/path/to/deployment`。已导入旧模块的现有 Worker
不会被热替换。此次没有停止或重启
活动实验，也没有改写历史结果。新启动的 Worker 和 run
会加载修复；运行过程中更换过 Worker 的实验
不能用于严格的修复前后比较。

## 尚待确认的问题

原生 SIGABRT 的具体原因尚未查明。已修复确认的 horizon 错误，
使任务不会再进入当初超出预算后发生崩溃的区间。
对预算内出现的原生崩溃，仍可在全新进程中重试。
服务器诊断日志为 `/tmp/robotenon-render-replay-gdb.log` 和
`/tmp/robotenon-render-bounded.log`.
