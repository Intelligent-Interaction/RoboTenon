# Local native simulation verification (2026-09-28)

[**English**](native-smoke-2026-09-28.md) · [简体中文](native-smoke-2026-09-28.zh-CN.md)

## Scope and criteria

The run attempted each of the 18 non-Isaac benchmarks. BEHAVIOR-1K and RoboDojo were excluded as requested. The host was macOS on Apple Silicon. ManiSkill2, Simpler, and RLBench were also attempted in Linux amd64 under local Docker.

Each check selected one representative task and episode and sent 3 fake actions. Passing required the native step to accept actions, observation or simulator counters to advance, valid images with at least one changing camera after every step, and a readable success signal. Passing images were also inspected manually. **This does not establish all-task/all-seed coverage, full-episode success rates, or score parity.** Fake actions need not achieve success=True.

Most checks exercised optional support's Environment/Session and observation codec without running the full Graph. The official RLBench image uses Python 3.8 and was checked through its native interface separately. RoboTenon/support requires Python ≥3.10. Its Graph was unverified in that image.

## Results

| Benchmark | Result | Evidence or blocker |
| --- | --- | --- |
| calvin | Blocked | pyhash failed to compile on macOS ARM. Repeated Linux image download attempts ended in EOF. |
| duobench | Blocked | The native RCS library compiled, but camera creation raised SIGSEGV in ensure_current / Renderer::register_context. |
| kinetix | Passed | 3 actions, valid images changing at each step, readable success signal. [Final frame](native-smoke-2026-09-28/kinetix.png) |
| libero | Passed | 3 actions, valid images changing at each step, readable success signal. [Final frame](native-smoke-2026-09-28/libero.png) |
| libero_mem | Passed | 3 actions, valid images changing at each step, readable success signal. [Final frame](native-smoke-2026-09-28/libero_mem.png) |
| libero_plus | Passed | 3 actions, valid images changing at each step, readable success signal. [Final frame](native-smoke-2026-09-28/libero_plus.png) |
| libero_pro | Passed | 3 actions, valid images changing at each step, readable success signal. [Final frame](native-smoke-2026-09-28/libero_pro.png) |
| maniskill2 | Passed | 3 actions, valid images changing at each step, readable success signal. [Final frame](native-smoke-2026-09-28/maniskill2.png) |
| mikasa | Blocked | Pinned SAPIEN had no macOS ARM distribution. Repeated Linux image downloads ended in EOF. |
| molmospaces | Blocked | Dependencies installed, but the upstream asset service returned TLS EOF and franka_cap metadata was unavailable. |
| rlbench | Blocked | Native interface accepted 3 steps and reported success=False, but images were black. Enabling the OpenGL3 plugin crashed the renderer. |
| robocasa | Alternate backend passed | Only the substitute RoboCasa 1.0.1 backend produced verified images. Required v0.2 remained unverified. |
| robocasa365 | Passed | 3 actions, valid images changing at each step, readable success signal. [Final frame](native-smoke-2026-09-28/robocasa365.png) |
| robocerebra | Passed | 3 actions, valid images changing at each step, readable success signal. [Final frame](native-smoke-2026-09-28/robocerebra.png) |
| robomme | Blocked | Pinned source and ManiSkill fork installed. reset imported mplib, whose pinned 0.1.1 release had no installable macOS distribution. |
| robotwin | Blocked | Host lacked NVIDIA CUDA, so the RoboTwin/CuRobo stack could not start simulation. |
| simpler | Blocked | Two CPU lavapipe attempts timed out at reset (360 seconds). Logs contained CUDA/OptiX driver errors without establishing a root cause. |
| vlabench | Passed | 3 actions, valid images changing at each step, readable success signal. [Final frame](native-smoke-2026-09-28/vlabench.png) |

## Reproduction and evidence

Full commands, parameters, JSON, and per-frame PNGs were stored in the local working directory:

```text
/data/benchmark-assets
```

The repository retains the [results and image metrics](native-smoke-2026-09-28/matrix.json) and final frames from passing benchmarks. Each benchmark has a separate dependency environment. Do not merge their pip requirements. The Docker image used the contemporary `latest`, which does not establish identity with the pinned source commit.

Example unified probe after installing simulator, assets, extension, and optional support:

```sh
python -m robotenon_benchmark_support.smoke \
  --class robotenon_maniskill2.native.benchmark:ManiSkill2Benchmark \
  --params '{"tasks":["PickCube-v0"],"render_resolution":[256,256],"enabled_cameras":["base_camera"]}' \
  --action '{"actions":[0.05,0,0,0,0,0,1]}' \
  --steps 3 --render cpu --save-frames --output runs/maniskill2-smoke.json
```

`--render cpu` applies only where the benchmark supports software rendering and the driver is installed. Review `status`, `images_valid`, `image_changed_each_step`, and `success_signal_readable` together. `status=passed` alone establishes execution/progress and does not detect black or static frames.

## Observed limits

- The user explicitly accepted the RLBench / CoppeliaSim Edu academic, educational noncommercial license. The image failure was unrelated to licensing.
- The RLBench image disabled its OpenGL3 plugin. Restoring it, adding `libdbus`, and using Xvfb/xcb still led to a renderer crash in the local emulated environment.
- The DuoBench macOS build trial changed only test-directory RCS library lookup and a missing `<numbers>` header. Core and benchmark physics were unchanged. Camera validation still failed.
- LIBERO-Plus used the official asset package. RoboCerebra used its pinned LIBERO fork and official Ideal/case1 data.
- The original RoboCasa requires its own verification independently of RoboCasa365 or newer releases.

## Repository changes and tests

- Added `--render gpu|cpu` to the optional support smoke command for software rendering checks. gpu remains the default.
- Added a regression requiring CPU rendering configuration before simulator construction.
- `extensions/benchmarks/benchmark-support/tests`: 30 passed.
- No changes to the `robotenon/` core.

Remaining benchmarks require retesting on Linux with pinned dependencies, assets, and suitable rendering before claiming complete native non-Isaac coverage.
