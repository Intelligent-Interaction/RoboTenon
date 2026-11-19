# 本机原生仿真验证：2026-09-28

[English](native-smoke-2026-09-28.md) · [**简体中文**](native-smoke-2026-09-28.zh-CN.md)

## 范围与判定

本轮逐一尝试 18 个非 Isaac benchmark；按用户要求排除 BEHAVIOR-1K、RoboDojo。宿主为 macOS Apple Silicon；ManiSkill2、Simpler、RLBench 另在本机 Docker 的 Linux amd64 环境尝试。

每项取一个代表任务 / 一个 episode，发送 3 次 fake action。通过要求：真实 native step 接受动作、观测或仿真计数推进、图片有效且每一步至少一个相机画面变化、success 可读取。已通过项目的图片另作人工查看。**不代表全任务、全种子、完整 episode 成功率或评分完全对齐。** fake action 不要求任务 success=True。

大部分检查使用 optional support 的 Environment/Session 和观测编解码，并不执行完整 Graph。RLBench 官方镜像为 Python 3.8，使用原生接口单独检查；RoboTenon / support 要求 Python ≥3.10，其 Graph 在该镜像中未验证。

## 结果

| Benchmark | 结果 | 说明 |
| --- | --- | --- |
| calvin | 未通过 / 受阻 | pyhash 在 macOS ARM 编译失败；Linux 镜像下载多次重试后 EOF。 |
| duobench | 未通过 / 受阻 | RCS 原生库已编译，创建相机时 SIGSEGV，栈位于 ensure_current / Renderer::register_context。 |
| kinetix | 通过 | 3 次动作、有效且逐步变化的图片、success 信号通过。 [末帧](native-smoke-2026-09-28/kinetix.png) |
| libero | 通过 | 3 次动作、有效且逐步变化的图片、success 信号通过。 [末帧](native-smoke-2026-09-28/libero.png) |
| libero_mem | 通过 | 3 次动作、有效且逐步变化的图片、success 信号通过。 [末帧](native-smoke-2026-09-28/libero_mem.png) |
| libero_plus | 通过 | 3 次动作、有效且逐步变化的图片、success 信号通过。 [末帧](native-smoke-2026-09-28/libero_plus.png) |
| libero_pro | 通过 | 3 次动作、有效且逐步变化的图片、success 信号通过。 [末帧](native-smoke-2026-09-28/libero_pro.png) |
| maniskill2 | 通过 | 3 次动作、有效且逐步变化的图片、success 信号通过。 [末帧](native-smoke-2026-09-28/maniskill2.png) |
| mikasa | 未通过 / 受阻 | 固定 SAPIEN 无 macOS ARM 安装包；Linux 镜像下载多次重试后 EOF。 |
| molmospaces | 未通过 / 受阻 | 依赖已安装；官方资产服务 TLS EOF，franka_cap 元数据尚未下载成功。 |
| rlbench | 未通过 / 受阻 | 原生接口接受 3 步动作并返回 success=False，但图像全黑；启用 OpenGL3 插件后渲染器崩溃。 |
| robocasa | 替代后端通过 | 仅替代的 RoboCasa 1.0.1 后端出图通过；要求的 v0.2 未验证。 |
| robocasa365 | 通过 | 3 次动作、有效且逐步变化的图片、success 信号通过。 [末帧](native-smoke-2026-09-28/robocasa365.png) |
| robocerebra | 通过 | 3 次动作、有效且逐步变化的图片、success 信号通过。 [末帧](native-smoke-2026-09-28/robocerebra.png) |
| robomme | 未通过 / 受阻 | 固定源码与 ManiSkill fork 已安装；reset 导入 mplib，固定 0.1.1 无可安装的 macOS 发行包。 |
| robotwin | 未通过 / 受阻 | 当前本机无 NVIDIA CUDA；RoboTwin/CuRobo 运行栈不可用，未进入仿真。 |
| simpler | 未通过 / 受阻 | CPU lavapipe 两次尝试均在 reset 阶段超时（360 秒）；日志含 CUDA/OptiX 驱动错误，未确认根因。 |
| vlabench | 通过 | 3 次动作、有效且逐步变化的图片、success 信号通过。 [末帧](native-smoke-2026-09-28/vlabench.png) |

## 复现与证据

完整命令、参数、JSON 及逐帧 PNG 位于本机工作目录：

```text
/data/benchmark-assets
```

仓库内保留 [结果与图片指标](native-smoke-2026-09-28/matrix.json) 及通过项目的末帧。每个 benchmark 使用独立依赖环境，不能把各环境的 pip 依赖合并安装。Docker 镜像使用当时的 `latest`，不能据此声称镜像内容等于固定源码 commit。

统一探针示例（先安装对应 simulator、资产、extension 和 optional support）：

```sh
python -m robotenon_benchmark_support.smoke \
  --class robotenon_maniskill2.native.benchmark:ManiSkill2Benchmark \
  --params '{"tasks":["PickCube-v0"],"render_resolution":[256,256],"enabled_cameras":["base_camera"]}' \
  --action '{"actions":[0.05,0,0,0,0,0,1]}' \
  --steps 3 --render cpu --save-frames --output runs/maniskill2-smoke.json
```

`--render cpu` 仅用于相应 benchmark 支持且机器已装软件渲染驱动的环境。通过条件需要同时检查 `status`、`images_valid`、`image_changed_each_step` 和 `success_signal_readable`；单独 `status=passed` 仅表示执行/推进检查通过，不能排除黑屏或不变图片。

## 运行中确认的限制

- RLBench / CoppeliaSim Edu 学术、教育非商业条款已由用户明确接受。图像失败与许可无关。
- RLBench 镜像的 OpenGL3 插件被禁用；恢复插件、补 `libdbus`、使用 Xvfb/xcb 后，在本机模拟执行环境仍发生渲染器崩溃。
- DuoBench 的 macOS 试编译只在测试目录改了 RCS 的动态库查找和缺少的 `<numbers>` 头文件，未改 RoboTenon 核心或 benchmark 物理逻辑；该移植未通过相机验证。
- LIBERO-Plus 使用官方资产包；RoboCerebra 使用固定 LIBERO fork 及官方 Ideal/case1 数据。
- RoboCasa 原始版与 RoboCasa365 / 新版本不可互相替代验证。

## 本轮仓库改动与测试

- optional support 的 smoke 命令新增 `--render gpu|cpu`，默认保持 gpu，用于实际软件渲染检查。
- 新增 CPU 渲染必须在 simulator 构造前配置的回归测试。
- `extensions/benchmarks/benchmark-support/tests`：30 项通过。
- 本轮未修改 `robotenon/` 核心。

后续需要在满足固定依赖、资产和渲染要求的 Linux 环境复测未通过项目，才能声称所有非 Isaac 仿真均已验证。
