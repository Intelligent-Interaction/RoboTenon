# RoboDojo Office extension

[**English**](README.md) · [简体中文](README.zh-CN.md)

Simulator prerequisites and downloads: [robodojo setup](../robodojo/SETUP.md). This legacy integration uses the specialized command/Graph described below, not the unified native evaluate command. Its model/service prerequisites remain separate.


The project uses `robodojo-office` to mean **official evaluation**. This extension uses
RoboDojo as the official benchmark. RoboDojo owns Isaac Sim, reset,
step, success, seed/layout, video, recovery, and result directories. RoboTenon supplies the XPolicyLab
policy service. Its TaskAdapter launches the official client and collects official results.

## Comparison with `robodojo`

```text
robodojo-office (this extension, official evaluation)
RoboTenon TaskAdapter -> official robodojo.sh client -> Isaac Sim
                                      |
                                      +-- WebSocket -> RoboTenon policy server

robodojo (framework-managed environment for research and debugging)
RoboTenon Graph -> EnvironmentSession/Body -> RPC -> Isaac Sim service
```

| Feature | `robodojo-office` | `robodojo` |
| --- | --- | --- |
| Control loop | RoboDojo | RoboTenon Graph |
| Official evaluation semantics | Preserved | Exact leaderboard parity unverified |
| Graph usage | Policy inference Graph/Executor | Full `reset -> action -> body -> observe` graph |
| Batching, multiple GPUs, recovery | Managed by RoboDojo | Initial release supports one environment and one worker |
| Use cases | Official scores, regression, leaderboard comparisons | Step debugging, safety control, trajectory research |

The two extensions are independent and register separate providers.

## Source layout

- `tasks.py` enumerates official cases and invokes the runner.
- `runner.py` constructs an argv array without shell concatenation and manages process groups and timeouts. Each unique run ID
  identifies its result without guessing from file timestamps.
- `results.py` retains the native result and projects it into RoboTenon metrics.
- `policy_bridge.py` maintains XPolicyLab protocol state. An explicitly injected
  `PolicyExecutor` runs the actual model or graph.
- `server.py` loads a `module:callable` executor factory and starts the XPolicyLab WebSocket service.
- `deployment.py` lets RoboTenon manage the policy server process.
- `assets/xpolicylab_policy/RoboTenon/` contains the deployment shim required by the RoboDojo client.

There is no implicit model discovery. Configure the production executor explicitly with `--executor-factory module:callable`
and implement `reset()`, `infer()`, and `trial_end()`. The built-in
`create_zero_executor` is for connectivity smoke tests.

## 1. Install the code

```bash
python -m pip install -e .
python -m pip install -e 'extensions/robodojo-office[server]'
```

Use a separate RoboDojo checkout outside the RoboTenon repository:

```bash
export ROBODOJO_ROOT=/data/RoboDojo
git clone --recurse-submodules \
  https://github.com/RoboDojo-Benchmark/RoboDojo.git \
  "$ROBODOJO_ROOT"
git -C "$ROBODOJO_ROOT" submodule update --init --recursive
```

This adapter targets RoboDojo `main` and the XPolicyLab WebSocket protocol. Pin the revision
validated for the deployment instead of pulling the latest source at each launch.

## 2. Download simulator assets

The official script downloads only the following subset from the Hugging Face dataset `RoboDojo-Benchmark/RoboDojo`:
`Assets/**`. Git LFS is required.

```bash
git lfs install
cd "$ROBODOJO_ROOT"
bash scripts/init_assets.sh
```

After downloading, verify that the following paths exist:

```text
$ROBODOJO_ROOT/Assets/Robots
$ROBODOJO_ROOT/Assets/Object
$ROBODOJO_ROOT/Assets/Material
$ROBODOJO_ROOT/Assets/Eval_Layout
```

Keep `Assets/` outside the RoboTenon repository. Record the asset revision for production. Use
`HF_REVISION=<commit-or-tag> bash scripts/init_assets.sh` to pin it.

## 3. Install the Isaac Sim environment

Native installation requires Linux and an NVIDIA GPU. The upstream installation script uses Python 3.11, Isaac Sim
5.1, and CUDA 12.8 dependencies:

```bash
cd "$ROBODOJO_ROOT"
bash scripts/install.sh -i
```

Alternatively, build the official Docker image:

```bash
cd "$ROBODOJO_ROOT"
docker build -t robodojo:cuda12.8 .
```

Upstream documentation estimates a 200 GB image. Allow at least 300 GB of disk space and use an NVIDIA driver compatible with CUDA 12.8.
macOS can be used for adapter development. The Isaac Sim evaluator requires the Linux GPU environment.

## 4. Install the policy shim

The RoboDojo `client` command checks
`XPolicyLab/policy/<POLICY>/deploy.py`. Copy the supplied shim into the pinned checkout of
RoboDojo checkout:

```bash
cp -R extensions/benchmarks/robodojo-office/assets/xpolicylab_policy/RoboTenon \
  "$ROBODOJO_ROOT/XPolicyLab/policy/RoboTenon"
```

The two shim files implement the standard `reset/update_obs/get_action` loop and contain no model code.

## 5. Weights and training data

RoboDojo has no universal checkpoint for every policy. Each XPolicyLab adapter
declares its own model repository, weight location, and setup script. Follow
`$ROBODOJO_ROOT/XPolicyLab/policy/<POLICY>/README.md` for the intended policy:

```bash
cd "$ROBODOJO_ROOT/XPolicyLab/policy/<POLICY>"
bash install.sh
# Download the checkpoint according to the policy README and set deploy.yml or specify its path.
```

For training or offline debugging, the official converted data are available:

```bash
cd "$ROBODOJO_ROOT/XPolicyLab"
bash scripts/RoboDojo/download_robodojo_data.sh demo
# Download the required format: hdf5, lerobot_v3.0, lerobot_v2.1, or real.
```

Online evaluation requires `Assets/` and the actual policy's checkpoint. Training datasets are optional.

## 6. Launch and verify

Start with the zero-action executor to verify the protocol. It is expected to leave tasks unsolved:

```bash
python -m robotenon_robodojo_office.server \
  --xpolicylab-root "$ROBODOJO_ROOT/XPolicyLab" \
  --executor-factory robotenon_robodojo_office.executors:create_zero_executor \
  --host 0.0.0.0 --port 19000
```

Start one official case in another terminal:

```bash
export ROBODOJO_ROOT=/data/RoboDojo
Tenon run \
  --config extensions/benchmarks/robodojo-office/configs/robotenon_robodojo_office.json \
  --output-dir data/robodojo-office
```

When `robodojo_root` is `null`, the configuration reads `ROBODOJO_ROOT`. Set the task, policy port,
action type, and evaluation count explicitly in
`configs/robotenon_robodojo_office_task_adapters.json`.

## Evaluation integrity

- Treat RoboDojo's native result as the official score. RoboTenon adds a lossless metric projection.
- Each TaskCase uses a unique `ROBODOJO_RUN_ID`. A missing result fails the run instead of falling back to older files.
- Preserve upstream seeds, layouts, success, scores, video, and restart behavior.
- `eval_batch` defaults to off in the `RoboTenon` shim. Implement and verify a batch executor before enabling it.
- The Xiaomi RoboCasa365 12D checkpoint cannot automatically serve as a RoboDojo dual-X5 policy.
  The actual executor must explicitly adapt the action head, cameras, and coordinate frames.

Optional `--scheduling-strategy static` assigns stable case indices round-robin to fixed Worker slots. The default `global_queue` refills any idle Worker and reduces load imbalance.
