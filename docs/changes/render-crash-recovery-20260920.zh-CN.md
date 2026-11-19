# 原生渲染崩溃的恢复处理

[English](render-crash-recovery-20260920.md) · [**简体中文**](render-crash-recovery-20260920.zh-CN.md)

## 已确认的事实

实验 `exp-20260920-150610-172b0aad` 的 AddIceCubes、AddLemonToFish、AddMarshmallow、AddSweetener、AdjustHeat、AfterwashSorting 均发生 worker SIGABRT。Python 原生故障日志的当前线程停在 robosuite `binding_utils.py:174 / read_pixels`，调用链是动作执行、环境 step、相机观测、MuJoCo 像素读取。最后持久化的环境反馈没有任务成功信号。这些记录不能改判为成功。

环境创建、reset、step、close 已经在同一专用线程执行。当前证据不能确定 NVIDIA 驱动内部的具体缺陷，也不能证明简单线程迁移或上一次 outputs 统计问题是原因。旧机制仅重新启动同一个 EGL 后端，重复性故障仍可能耗尽重试次数。

## 恢复方案

在 `runtime.execution` 中配置：

```json
{
  "max_retries": 1,
  "render_retry_backend": "osmesa"
}
```

8 GPU 配置默认启用这个恢复选项；其他配置不自动改变。默认首次运行仍使用现有渲染后端。仅当 worker 因 SIGABRT/SIGSEGV 丢失，且故障日志明确指向 robosuite 的 read_pixels 时，下一次允许的重试才切换到 OSMesa。普通模型错误、超时、其他 native crash 不触发切换。仍遵守 max_retries；为 0 时不会额外重试。

软件渲染在新进程创建前设置 MUJOCO_GL/PYOPENGL_PLATFORM，必要时预加载系统 libstdc++，避免在 multiprocessing worker 中途 exec 丢失通信管道。Mesa 线程数受 worker_threads 限制。模型继续使用原 GPU；任务、seed、horizon、图像大小、成功判定不改变。

恢复的动作从同 seed 的新 attempt 开始，不在已经损坏的仿真状态上续跑。attempt/status.json 保留原始崩溃错误、retry_render_backend 和下一次实际选择的 render_backend。软件渲染 worker 在该任务完成或失败后关闭，其他任务不会永久降级。软件渲染会增加 CPU 开销。

## 生效范围

源代码和配置同步不能热替换已经运行的 coordinator。新启动或停止后原实验 ID 恢复的 coordinator 使用新重试机制。不要把旧进程后续结果当作新版验证结果。

## 验证

回归覆盖原生崩溃分类、spawn 前环境配置与恢复、后端切换、重试预算以及恢复后仍按真实任务结果统计。Linux 集成测试使用真实 os.abort，验证进程丢失后以新 attempt 和软件渲染环境启动新 worker。

服务器真实环境验证使用 AddIceCubes、seed 72、256×256 相机和该失败实验记录的动作。诊断脚本和日志位于 /tmp/robotenon-render-backend-check.py、/tmp/robotenon-osmesa-check.log、/tmp/robotenon-egl-check.log。GPU/软件渲染对比均不启动模型、不修改用户实验结果；超出已记录动作长度后重复最后一个动作，仅用于验证长时间渲染稳定性。

这是经过验证的故障恢复路径，不等同于已经修复 NVIDIA/MuJoCo 的原生缺陷。


## 本次验证与部署记录

- 本地全量测试：394 通过，1 项 Linux 专用 native abort 集成测试跳过；服务器全量 395 项通过。
- OSMesa：AddIceCubes / seed 72 / 256×256 三相机，1000 步用时约 539 秒，正常关闭。前 816 步取自失败实验记录，之后重复最后一个动作用于延长渲染压力测试。
- EGL 独立同动作回放也完成 1000 步；因此不能把此次实验归结为每次必现的固定第 800 步错误，或者声称重放已经复现原生崩溃。
- 原实验在同 ID 下重启新 coordinator（PID 3234028），保留 67 个 completed 任务，文件 SHA256 验证未改动；重新调度 250 个 error / running / pending 等非 completed 任务。运行中任务从原 seed 重新开始。新 status.config_hash 与包含备用渲染配置的当前配置一致。
- 重启前状态与完成结果哈希备份：`/path/to/deployment/render-restart-20260920-164058`。源文件部署前备份：`/path/to/deployment/render-retry-20260920-163551.tar`。

- OSMesa 图像检查：三个相机均返回 256×256×3 uint8 图像，像素标准差约 67–68，排除了全黑/常量帧。

- GDB 下完整 Graph（真实仿真、缓存、视频，模型工具由记录动作/固定规划替代）完成 1100 步，以 horizon 正常结束。该复现未运行原模型推理，不能据此排除原生产条件下的 native 故障。日志：`/tmp/robotenon-workflow-render-gdb2.log`。
