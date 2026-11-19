# LIBERO 接入复审（2026-09-20）

[English](libero-integration-review.md) · [**简体中文**](libero-integration-review.zh-CN.md)

本次按当前工作区完成解耦后的源码审计；未安装/运行真实 LIBERO 仿真，未改动接入实现。
上游 master 已核对为 `8f1084e3132a39270c3a13ebe37270a43ece2a01`。
结论：以标准闭环评测为范围，静态检查没有发现必须再次修改框架核心的阻塞点。
预计核心源文件修改 0 个，新增独立插件包、配置、依赖锁和测试即可。
这一结论不表示已完成真实依赖、GPU 渲染或模型效果验证，也不适用于仍安装旧版框架的运行环境。

## 1. 与上一轮审计的差异

| 边界 | 当前证据 | LIBERO 接入方式 |
| --- | --- | --- |
| 任务参数 | runner._apply_request_overrides 合并 environment.parameters 与 case/request 覆盖 | 传 suite/task_order_index/init_state_id/settle_steps 等，无需修改 Runner |
| 环境会话 | Runner 通过注册 Adapter.open(SessionSpec) 创建 Session | 独立 Environment 插件封装 OffScreenRenderEnv |
| 动作执行 | Body 由配置选择，注入 session/observation_updates/stop_executor | 新 Body 执行 LIBERO 动作，沿现有 Body 服务做 chunk/中断/反馈 |
| 动作描述 | feedback.io.action 声明 format/dimensions | 使用如 libero_osc_pose_7d 的插件格式，不改 Action schema |
| 图与结果 | Graph 显式装配 Module、body.execute、env.observe 和 outputs.success | 新建评测图，以原生环境成功判定为准 |
| 批量 | TaskAdapter 插件枚举，覆盖可按实例 ID 指定 | 不再需要给 experiment.py 添加 LIBERO 分支 |
| 执行/缓存/展示 | 已有内置 process、观测缓存、结果与视频路径 | 常规接入复用现有实现，不需要新增基础设施后端 |

当前 Xiaomi **Tool** 强制 12 维 RoboCasa365 接口，不能直接接 LIBERO。
但 `modules/xiaomi_robotics_1.py` 的动作打包 **Module** 已读取 EnvironmentIO 的 format/dimensions，
没有把维数写死；`robocasa365_terminal.py` 也基于通用反馈和步数终止。
最小接入可以复用这两个 Module，插件 Tool 返回其要求的 vectors/format。
为避免新配置继续暴露旧供应方名称，建议另加 rollout.py 注册自己的动作/终止节点；这属于插件代码，
不是框架核心改造。不能把“Module 可复用”误解为 Xiaomi 的 checkpoint 或 Tool 已适配。

## 2. 文件清单

建议独立包目录为 `integrations/libero/`；以下均是计划新增文件，不是已实现功能。

| 文件（相对插件包目录） | 必要性 | 内容 |
| --- | --- | --- |
| pyproject.toml | 必需 | 独立安装 robotenon_libero 包，不修改核心项目依赖 |
| src/robotenon_libero/__init__.py | 必需 | 轻量包入口，import 不连接环境或加载模型 |
| src/robotenon_libero/tasks.py | 必需 | 枚举 suite/task/trial，稳定任务 ID，记录 task_order_index、初始状态索引和 horizon |
| src/robotenon_libero/environment.py | 必需 | Adapter/Session、路径配置初始化、reset/set_init_state、观测与 IO、仿真线程、视频和关闭 |
| src/robotenon_libero/body.py | 必需 | 7 维动作校验和控制语义、四返回值 step、逐步反馈及成功/超时停止 |
| src/robotenon_libero/policy.py | 模型闭环必需 | 所选 LIBERO 策略的 Tool，图片/状态预处理、反归一化、动作队列、end_session |
| src/robotenon_libero/rollout.py | 推荐 | 动作打包与终止节点；也可先复用现有数据驱动 Module，减少初版文件 |
| requirements-libero.lock | 必需 | 固定验证过的 Python/仿真/策略依赖，记录上游 commit |
| scripts/bootstrap.py | 可选 | 若不在 environment.py 内实现，则集中生成非交互 LIBERO 路径配置并做资产检查 |
| tests/test_contracts.py | 必需 | reset/step、IO、7 维动作、成功与 horizon、缓存观测、关闭和状态隔离 |
| tests/test_rollout.py | 必需 | 固定初态重复运行、动作回放、模型基线、连续任务及进程执行 |

框架仓库新增 `configs/robotenon_libero.json`，可在一个文件里放环境/Body/Tool/Module/TaskAdapter/Graph 清单，
无需照搬 XR365 的多文件拆分。通过 plugin_environments/plugin_bodies/plugin_tools/plugin_modules/
plugin_task_adapters 导入上述包。按需再增加 `configs/experiments/libero_parallel.json`。

现有 runtime/runner.py、experiment.py、factory.py、workflow/*、services/*、schemas.py、
注册表内置清单和 Xiaomi/RoboCasa 插件预计均不需要修改。
文档入口只需增加 LIBERO 使用说明并在 docs/README.md/vendor-guide.md 链接它。

## 3. 主要技术成本

1. **依赖组合尚未验证。** 上游 README 推荐 Python 3.8.13、Torch 1.11；requirements 固定
   robosuite 1.4.0、gym 0.25.2、numpy 1.22.4。RoboTenon 要求 Python >=3.10。
   这是上游参考安装方案与本项目要求的差异，不等于 LIBERO 代码必须运行在 3.8。
   优先验证独立 Python 3.10 环境的可行组合，不直接覆盖当前 RoboCasa365 环境。
   `process` 是同解释器的 spawn，不自动切换 conda 环境。

2. **初始化和基准协议。** seed 不能替代官方 init_states。
   记录 task_order_index、init_state_id、任务语言、相机配置、控制频率、horizon 和策略版本。
   上游 lifelong/metric.py 在 set_init_state 后执行 5 个全零 7 维动作，再开始评测计步；
   若复现其他模型的评测代码，应明确使用其协议，而不是混用不同 warmup/步数口径。

3. **控制语义。** 默认 Panda/OSC_POSE/20 Hz、外部视角与腕部相机。
   验证平移/旋转增量、坐标系、夹爪符号、动作裁剪、图像方向、四元数转换与归一化。
   12 维截断成 7 维不能作为模型适配验证。

4. **成功与步数。** LIBERO 返回 obs/reward/done/info；bddl_base_domain.step 把 done 设为
   _check_success 的结果。插件应显式处理步数上限，把成功与超时区分开；chunk 中途成功应立即停止。
   observe 必须保留最终反馈，不能清掉已报告的成功状态。

5. **无人值守与渲染。** 首次 import 会在缺少 config.yaml 时调用 input；启动前生成 LIBERO_CONFIG_PATH 配置。
   wrapper.reset 存在无限重试逻辑，适配层需要受控 reset 重试/进程超时。
   创建、step、render、close 使用同一仿真线程，EGL 设备在导入前配置；避免额外套上 LIBERO 自身多进程向量环境。

6. **Suite 命名。** 优先用 libero_spatial/object/goal/90/10。
   此 commit 注册了 LIBERO_100 类，但 task_maps 初始化列表没有 libero_100；不要因名字可枚举就认定构造可用。
   如需整体 100 任务，明确组合 90/10 并保留分组与任务顺序，不必改上游或框架。

## 4. 若必须保留 Python 3.8

增加插件侧 `bridge_server.py`、`remote_environment.py`（或 environment.py 的远程模式）、
启动配置与桥接测试。框架侧 Body 通过插件向外部仿真发送动作，Environment 获取观测；
环境和 Body 共享一个远程会话。媒体使用共享文件引用或另外的传输，不把 ndarray 直接塞进 JSON。
纯 3.8 服务进程不能直接安装当前要求 >=3.10 的 RoboTenon；需轻量独立桥接或验证兼容的服务实现。
已有 RPC 插件入口提供接入能力，不是现成的 LIBERO 服务端，因此不能把这条路线按零开发成本估算。

## 5. 工期估算与验收

估算前提：一名熟悉本项目与 robosuite 的开发者、Linux/NVIDIA 环境可用，已有可运行的 LIBERO 策略/权重，
范围是闭环评测，不包含训练、下载等待或全量评测运行时长。

| 阶段 | 估算人日 |
| --- | --- |
| 依赖、资产与离屏渲染验证 | 0.5–1 |
| TaskAdapter + Environment + Body | 1–2 |
| 策略 Tool + Graph/节点配置 | 1–2 |
| 固定初态、重复/并行、视频/结果与回归验证 | 1–2 |

合计约 **4–7 人日**；若必须采用 Python 3.8 外部桥接，再预留 **3–5 人日**。
如果目标是把现有 RoboCasa365 Xiaomi 权重迁移到 LIBERO，效果与训练成本不能由接口改造工期推断。

验收顺序：单任务 reset/渲染/零动作 → 固定初态动作回放 → 已知策略单任务 → 多初态/多任务 →
并行与异常回收；每一步检查成功率口径和步数统计，而不只是进程正常结束。
本次重跑环境参数、外部插件和 Runner 装配相关回归：29 passed。此前 440 项完整回归是框架验证，
不能作为 LIBERO 已可运行的证据。

## 源码依据

- 本地：runner.py 参数合并与 SessionSpec 装配；factory.py 插件导入与资源装配；
  modules/xiaomi_robotics_1.py 的 IO 驱动打包；tools/xiaomi_robotics_1.py 的 12 维限制。
- [上游安装说明](https://github.com/Lifelong-Robot-Learning/LIBERO/blob/8f1084e3132a39270c3a13ebe37270a43ece2a01/README.md)
- [依赖](https://github.com/Lifelong-Robot-Learning/LIBERO/blob/8f1084e3132a39270c3a13ebe37270a43ece2a01/requirements.txt)
- [环境包装](https://github.com/Lifelong-Robot-Learning/LIBERO/blob/8f1084e3132a39270c3a13ebe37270a43ece2a01/libero/libero/envs/env_wrapper.py)
- [成功判定](https://github.com/Lifelong-Robot-Learning/LIBERO/blob/8f1084e3132a39270c3a13ebe37270a43ece2a01/libero/libero/envs/bddl_base_domain.py)
- [评测循环](https://github.com/Lifelong-Robot-Learning/LIBERO/blob/8f1084e3132a39270c3a13ebe37270a43ece2a01/libero/lifelong/metric.py)
- [任务定义与初态](https://github.com/Lifelong-Robot-Learning/LIBERO/blob/8f1084e3132a39270c3a13ebe37270a43ece2a01/libero/libero/benchmark/__init__.py)
