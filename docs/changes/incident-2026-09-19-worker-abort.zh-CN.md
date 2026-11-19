# 2026-09-19 实验失败排查与修复

[English](incident-2026-09-19-worker-abort.md) · [**简体中文**](incident-2026-09-19-worker-abort.zh-CN.md)

## 历史结果

| 实验 | 总任务 | 旧统计 completed | 环境判定成功 | 旧统计 error |
| --- | ---: | ---: | ---: | ---: |
| exp-20260919-003015-08bbd403 | 317 | 124 | 97 | 193 |
| exp-20260919-102712-2a54c875 | 317 | 139 | 59 | 178 |

两轮均处理完 317 个任务。批次 `status.json` 的 `completed` 表示调度结束；
`runner_cli.py` 在 `num_errors > 0` 时返回 1，Portal 也将含 error 的已结束实验标为
`failed`。这不表示所有任务都失败，也不表示实验尚未跑完。

371 个旧 error 均为 `WorkerLost: worker connection closed`。逐项核对退出码：
第一轮 189 次 -6（SIGABRT）、4 次 -11（SIGSEGV）；第二轮 177 次 -6、1 次 -11。
其中 369 份可解析的 worker fault 堆栈（第一轮 192、第二轮 177）指向：

```text
robosuite/utils/binding_utils.py:174 in read_pixels
mujoco.mjr_readPixels(...)
... camera_rgb -> step -> concurrent.futures.thread
```

这是仿真相机渲染路径中的进程级崩溃，不是普通的模型任务失败。历史记录中的
`env_steps=0` 是异常分支硬编码的占位值，并不说明没有执行动作。例如第二轮
OpenToasterOvenDoor 的 live 文件已记录 112 步，随后在 body_execute 中丢失 worker。

两轮 GPU 6 分别正常执行结束 53、49 个任务，没有 error；其余 7 个 worker GPU
承载了全部 371 个 error。这是设备/启动条件相关性的证据，但不能据此直接认定
某张 GPU 损坏或断言驱动内部根因。日志没有提供足以确认 CUDA OOM 的证据。

第二轮还有 60 条旧 completed 记录的 reason 为 `continue`。逐条复核，其中 59 条
对应 planner 已输出 stop、remaining 为空，视觉 verifier 认为子任务成功，但环境
未确认成功；另外 1 条 AssembleCookingArray 的 graph 实际 failed，Qwen 输出达到
1024 token 上限，JSON 被截断。另有 20 个任务达到 horizon。

第一轮另有 25 个 horizon 任务，以及 CoolBakedCake、StartElectricKettle 两个
观测缓存 URI 读取失败（graph failed）。旧 evaluator 把返回结果中的 graph failed
也一律计为 completed，因而三条执行异常未进入旧 error 计数。历史表格保留原统计口径。

## 本次代码修复

1. `runtime/parallel.py` 在 spawn 之前暂时设置子进程的 CUDA/EGL 设备与线程环境，
   随后恢复父进程环境。服务器 sitecustomize 会在 worker 入口之前导入 Transformers/
   Torch，因此仅在 `_worker_main` 设置设备不是可靠的初始化边界。此项是初始化加固，
   尚不能单独证明它解释了全部历史原生崩溃。
2. WorkerLost 信息附带信号名、worker fault 日志路径与首个崩溃栈帧。
3. 异常任务从同一 run 的 live 文件恢复最后已知步数，标记 `env_steps_partial=true`，
   保留活动节点与进度时间；不读取其他 run 的旧进度，不将部分步数当作最终精确步数。
4. RoboCasa 配置启用 `policy.planner_stop_requires_environment_success`。环境尚未确认
   成功时，planner 的 stop 被拒绝，恢复原始任务指令继续执行。其他环境保持默认行为。
   被接受的 planner stop 显式记录 `reason=planner_stop`，不再遗留 continue。
5. 8 卡配置默认 `max_retries=1`：worker 丢失时用新 worker、新 attempt 重试一次。
   原始 lost attempt 保留，不掩盖失败历史。正常执行但未达到任务目标不因此重试。
6. 返回的 graph failed/error 在任务和 attempt 两层都记录为执行失败，并使用
   `reason=graph_error` 和实际错误信息；不再误计为 completed/succeeded。

旧实验文件与结果未改写。复用旧的完整 experiment_override.json 时，其中显式保存的
max_retries=0 仍会覆盖新默认值；应创建新实验或明确更新覆盖参数。

## 验证和边界

- 本地与服务器分别运行完整 pytest 套件：237 项通过（本地 12.53 秒，服务器 59.91 秒）。
- 7 张 GPU 并行纯仿真渲染：共 8,400 步正常退出；另有单 GPU 1,800 步检查通过。
- 历史 StirVegetables/seed=299 的 48 个动作原样回放通过。
- 完整 OpenToasterOvenDoor/seed=33 单任务在 117 步获得环境成功。
- 原配置的 8 worker 诊断运行 `eight-worker-native2`：8 个历史 error 任务，
  本次 horizon=160，0 error；没有获得复现 SIGABRT 的原生堆栈。
- 修复后 `postfix-eight-smoke`：同组 8 个任务均正常到达 160 步 horizon，合计
  1,280 步，0 error。该短测的任务成功数为 0，用于验证运行稳定性，不用于评估成功率。
- 两端 robotenon、tests、configs、scripts、docs、examples 目录中的 221 个源码、配置、
  测试和文档文件逐一 SHA-256 校验一致；不包含运行数据、缓存和系统安装依赖。

上述短测不能证明原生渲染崩溃已根治，也不能代替原 317 个任务的完整评测。
设备绑定加固和一次 worker 重试用于降低风险、保留诊断证据；若再次出现 SIGABRT/SIGSEGV，
仍需依据新的 fault 日志进一步定位渲染驱动/资源生命周期问题。

历史证据在服务器 `/path/to/deployment/data/xr365/portal-runs-8gpu/<实验 ID>/`，包括
summary.json、各任务的 .live 文件与 workers/*/worker-fault.log。
本次诊断产物在 `/path/to/deployment/data/xr365/diagnostic-20260919/`。
同步前修改文件备份在 `/path/to/deployment/.xr365-sync/fix-20260919-235712/`。

## 2026-09-20 后续修复

### 缓存读取竞态

`read_uri_bytes` 先查 SQLite，再查 `_pending`。持久化线程在两次查询之间提交
SQLite 并删除 `_pending` 时，两次读取都可能落空，误报 `FileNotFoundError(cache://...)`。
修复为：内存查询未命中后重新查询已经提交的磁盘索引。删除了无法弥补这个竞态的
重复 hot-ring 扫描。

新增测试用事件控制“第一次磁盘查询未命中 → 提交持久化并移除 pending → 查询内存”
的顺序。测试在修复前的真实读取方法上稳定失败，在修复后通过。这确认了代码中的
竞态；历史两条日志没有线程时序记录，因此不能逐条证明当时必定触发了同一个窗口。

### 模型响应截断

Qwen 工具检查 `finish_reason=length`，不再尝试执行截断结果。默认最多请求三次，
截断时逐次将 token 预算从 1024 增至 2048、4096；超过上限仍明确报告执行错误。
无效 JSON 同样有限重试，可用 `max_response_retries=0` 关闭。网络不可达仍直接报错。
JSON 提取仅解析第一个对象，避免把未闭合外层对象中的合法内层对象误当作完整结果。

此次修改前的四个文件已备份到服务器
`/path/to/deployment/.xr365-sync/followup-20260920/before.tar.gz`（新测试文件原先不存在）。
原生渲染路径沿用前述设备初始化、线程绑定、资源清理及 worker 重试修复，
没有根据猜测修改 MuJoCo 或显卡驱动。


### 后续验证结果

- 本地完整套件：243 项通过（13.01 秒，graph 并行改动前的快照）。
- 服务器完整套件：242 项通过，1 项模型部署测试在同时加载 8 卡模型时触发其
  3 秒启动超时；单独重跑模型部署文件 11 项全部通过（25.43 秒）。
- 服务器本次缓存与 Qwen 响应专项测试：13 项全部通过（0.31 秒）。
- 真实模型 8 卡回归 `followup-eight-600`：8 个任务正常结束，合计 4,298 步，
  0 error，8 个 worker 的 fault 日志均为空，没有 worker 重试。
  PickPlaceCounterToToasterOven 在 205 步成功，PickPlaceCounterToMicrowave 在 493 步成功；
  CoolBakedCake、StartElectricKettle、AssembleCookingArray、AddLemonToFish、
  StirVegetables、StoreLeftoversByType 均正常达到 600 步 horizon。
- 此轮沿用历史任务种子与真实模型，使用启动时的诊断配置快照；没有修改 graph 文件。
  结果位于服务器 `data/xr365/diagnostic-20260920/followup-eight-600/`。
- 本次仅同步缓存实现、Qwen 工具、对应两个测试文件和本文档，共 5 个文件；逐文件
  SHA-256 核对一致。另一个工作正在修改 graph 编排，其文件未覆盖或回滚。

回归没有复现历史原生渲染崩溃，但它仍不等同于证明驱动/渲染底层问题已根治，
也不代替完整 317 任务评测。历史实验结果未改写。
