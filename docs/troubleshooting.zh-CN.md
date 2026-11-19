# 常见问题

[English](troubleshooting.md) · [**简体中文**](troubleshooting.zh-CN.md)

先区分失败发生在安装、配置装配、模型启动、任务执行还是页面查询阶段。排查时记录配置文件、`run_id`、任务编号和对应日志。

## 先做这 4 项检查

第一次运行失败时，先不要急着改配置或安装 GPU 依赖。按顺序执行：

```bash
# 1. 确认正在使用哪个 Python，版本应为 3.10 或更高
python -c 'import sys; print(sys.version); print(sys.executable)'

# 2. 确认 robotenon 来自当前仓库，而不是另一个旧环境
python -c 'import robotenon; print(robotenon.__file__)'

# 3. 确认主配置能装配
Tenon modules-check --config configs/config.json
Tenon graphs-check --config configs/config.json

# 4. 跑不依赖外部服务的 Fake 环境
Tenon run-fake --config configs/config.json --output runs/diagnose
```

怎么判断问题在哪一层：

| 在哪一步失败 | 通常说明什么 | 先看哪里 |
| --- | --- | --- |
| 第 1、2 步 | 虚拟环境、Python 版本或安装位置不对 | 下方“安装与配置” |
| 第 3 步 | 配置路径、组件注册或插件导入有问题 | 下方“安装与配置” |
| 第 4 步 | 核心运行链路有问题 | 保留完整报错和 `runs/diagnose` |
| Fake 成功，真实任务失败 | 核心框架正常，问题更可能在模型、仿真器、资产或设备 | 下方“模型与仿真” |
| 任务成功，页面看不到 | 运行入口、数据源或 Portal 模式不匹配 | 下方“结果与 Portal” |

报错信息通常从最底部的异常类型开始看，但提交排障资料时要保留完整堆栈。只截最后一句，经常会丢掉真正的调用位置。

## 安装与配置

| 现象 | 检查方法 |
| --- | --- |
| `No module named robotenon` 或 `pydantic` | 用 `python -c 'import sys; print(sys.executable)'` 确认解释器，在同一环境执行 `python -m pip install -e '.[dev]'` |
| Python 版本相关错误 | 检查 `python --version`；包要求 Python ≥ 3.10，开发测试建议使用 Python 3.11 |
| `unknown module/tool/provider` | 核对注册名、插件是否安装到当前解释器、`plugin_*` 是否导入正确模块 |
| 子清单文件不存在 | 核对路径相对主配置所在目录的解析结果，避免把终端目录当作配置目录 |
| Graph 缺少 `outputs.success` | 为正式 episode 的入口 Graph 声明公开输出；参见[数据接口](explicit-graph-data.zh-CN.md#graph-对外输出) |
| 覆盖配置后工具或图消失 | 数组整体替换；检查覆盖层是否只保留了一个条目 |

组件配置检查使用 `Tenon modules-check --config <配置>` 等命令；正式实验的启动诊断使用 `Tenon run --preflight --config <配置>`。

## 模型与仿真

| 现象 | 检查方法 |
| --- | --- |
| 模型端点不可达 | 核对服务日志、加载是否完成、host/port 与客户端配置；启动脚本返回不代表模型就绪 |
| 显存不足 | 统计同卡上的仿真、模型和其他进程；核对物理 GPU 与 Worker 内局部编号 |
| 配了两张卡但服务仍只用一张 | 核对部署后端是否支持模型分片；`gpu_ids` 本身不会为模型增加并行能力 |
| `--dry-run` 仍报 RoboCasa 错误 | 此参数不会跳过环境初始化；无仿真环境时改跑 `run-fake` |
| Worker SIGABRT / SIGSEGV | 查看该 attempt 的故障日志和调用栈；渲染、驱动、模型错误需要分别定位 |
| `resource already reserved` | 确认同一 GPU 或机器人资源是否仍被运行进程占用；先正常停止对应实验 |

托管模型日志位于运行目录的 `workers/<worker_id>/` 下，部署清单 `deployment.json` 可用于核对模型路径和 GPU 映射。具体目录见[模型部署参考](worker-model-deployment.zh-CN.md#5-端口生命周期与日志)。

## 结果与 Portal

| 现象 | 检查方法 |
| --- | --- |
| Fake 已完成，Portal 列表却为空 | `run-fake` 不注册正式实验或生成正式 Trace；用 [Portal 教程](tutorials/03-portal.zh-CN.md)的 CPU 配置提交实验 |
| 只读服务首页显示提交已禁用 | 当前首页是完整实验列表；`--no-experiment-submit` 下使用[只读查询 API](tutorials/cli-experiments.zh-CN.md#5-通过只读-http-接口查看数据)读取正式 CLI 产物 |
| 页面没有 Trace | 核对 `--source`、运行身份、时间范围，以及任务是否写入 Trace；Web 不自动搜索其他数据库 |
| 任务结束但成功率为零 | 检查 `task_success`、`reason` 与 Graph 公开输出；`completed` 不等于任务成功 |
| 没有视频 | 检查是否开启视频保存、视频依赖和具体文件是否存在；单任务用 `--video-path`，批量用 `--save-videos` |
| 端口占用 | 使用已启动的 Portal，或明确更换 `--port`；同步调整 SSH 转发端口 |
| 停止后仍显示清理 | 查看清理进度；资源确认释放前，下一条实验可能继续排队 |
| 原地重跑被拒绝 | 检查原配置快照和资源状态；需要换配置时使用 Fork 创建新实验 |
| 历史查询超时或超限 | 缩小时间范围、减少页大小；大 Trace 使用分页查询 |

## 提供排障资料

保留以下信息即可定位大多数问题：配置名称和覆盖参数、运行目录、任务与 attempt 身份、第一条错误及其上下文、运行器与模型服务日志。涉及图像或动作时，再补充对应 Trace 和视频。

历史故障调查与复现资料见[更新记录](changes/README.zh-CN.md)。

## 对照操作步骤

不知道数据应该出现在哪里时，先对照[结果查看教程](tutorials/view-results.zh-CN.md)的文件与页面入口。网页提交从 [Portal 教程](tutorials/03-portal.zh-CN.md)开始；设备驱动和停止接口问题查[真机部署](tutorials/real-robot.zh-CN.md)。
