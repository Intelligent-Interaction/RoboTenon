# RoboCasa365 protocol and implementation reference

[**English**](NATIVE_EVALUATION.md) · [简体中文](NATIVE_EVALUATION.zh-CN.md)

This page preserves older links. The following documents contain the current commands and protocol details.

- [Task protocols, profiles, and launch commands](README.md)
- [Environment installation and asset downloads](SETUP.md)
- [Evaluation and parallel execution](../EVALUATION.md)
- [Model interface](../models/README.md)

The environment protocol is implemented in `src/robotenon_robocasa365/benchmark.py`. Each model has a separate implementation. `benchmark-source.json` records the migration source. Confirm task behavior and final success rates separately when comparing against upstream.

## Xiaomi parity checks

Use the same task indices, episode seeds, checkpoint, and task horizons when
comparing with Xiaomi's original evaluator. The single-sample reference uses
one simulator per model service. Dynamic batching requires a separate success
rate comparison; matching one action chunk does not establish rollout parity.

The loader explicitly casts the whole model to BF16 after loading, as Xiaomi's
server does. HF's load-time dtype alone leaves RoPE frequency buffers in FP32.
The protocol tests cover that distinction using a model with FP32 buffers and
BF16 weights.

The extension also guards robosuite EGL renderer cleanup. robosuite 1.5.2 can
garbage-collect a renderer from an earlier reset while another renderer is
current. Its destructor frees GPU resources without binding the owning GL
context, corrupting the live renderer's images while physics continues normally.
`_internal/rendering.py` binds the owner's context during cleanup and restores
the previous live context afterward. This changes resource cleanup, not physics,
camera settings, or episode seeds. It is installed only for EGL workers.

Run the simulator regression independently of model inference:

```bash
MUJOCO_GL=egl python extensions/benchmarks/robocasa365/tools/check_egl_cleanup.py \
  --output runs/robocasa365-egl-check
```

The script runs three fresh processes: a reference without mid-rollout GC,
an unprotected run with forced GC, and a protected run with forced GC. It saves
observations and checks identical robot states and a maximum RGB pixel difference
of one for the protected run. The one-level tolerance accommodates the minor
rasterization differences also seen between repeated reference runs.

During evaluation, read live progress from `status.json` or `episodes.sqlite3`.
An initial `summary.json` may exist before any episode completes; its existence
alone does not mean the run finished.
