# Qwen3-VL high-level planner

[**English**](README.md) · [简体中文](README.zh-CN.md)

This is a real-model HLP backend for hierarchical systems. It receives structured requests through `BatchProvider` and returns decisions rather than robot actions. Multiple requests share one `model.generate()` call. The current RoboCasa configuration disables the visual verifier. It checks native success/terminal after each sequence of Xiaomi actions and proceeds to the next HLP call if the episode is still active.

The default provider supports local **Qwen3-VL Instruct** checkpoints. Qwen3.5 uses the separate provider and inference environment below. Each architecture requires its matching weights. Record the model version and checkpoint together.

## Installation and assets

Install dependencies in the model Python environment:

```bash
/model/python -m pip install -r extensions/benchmarks/systems/robocasa_hierarchical/tool/qwen/requirements.txt
/model/python -m pip install -e extensions/benchmarks/systems/robocasa_hierarchical/tool/qwen
```

Weights require the full configuration, processor/tokenizer files, and all safetensors shards. Use an existing server path or download on the target server:

```bash
hf download Qwen/Qwen3-VL-4B-Instruct --local-dir /models/Qwen3-VL-4B-Instruct
```

The backend uses `local_files_only=True` and performs no runtime download or execution of remote repository code. The recorded environment used torch 2.6.0+cu124 and transformers 4.57.1. The checkpoint's CPU image processor was tested. Real-model 32-step smoke tests covered single samples and dynamic batching on separate and shared GPUs without errors. Full task success requires separate evaluation.

## Interface and configuration

Factory: `robotenon_qwen_reasoner.backend:Provider`.

Parameters include `model_path`, `max_new_tokens` (default 256), `max_response_retries` (default 2), and optional `metrics_dir`.

The shared `benchmark_model` tool supplies:

- `purpose`, either `planner` or `verifier`.
- `prompt` and `system_prompt`, containing the graph module's task, STM, and JSON schema.
- `observation.images`, current uint8 HWC RGB images in native orientation, without extra cropping, flipping, or Xiaomi-specific processing. The planner caches its previous views per session and labels previous/current explicitly. The first call has only current views.
- `task` and episode metadata from the actual benchmark task.

Generation uses BF16, SDPA, and greedy decoding. A complete top-level JSON object ends that sample immediately, preventing extra analysis from exhausting the token budget. Batch outputs return by request_id. The tool validates the complete JSON object. PlanModule supplies planner field defaults and normalizes types. Syntax or truncation errors retry only failed items with their original prompts. Only truncation doubles the token budget, up to four times its initial value. Exhausted retries raise an error.

Each session stores task metadata and the previous planner image until episode completion. `qwen-results/<pid>.json` records planner/verifier call counts, model forwards, the latest eight outputs, and the latest 16 raw failures (up to 16,384 characters each). `verifier` should remain zero for this configuration. Request history is bounded.

## Running the provider

Use the [hierarchical graph entry point](../../README.md#launch-the-graph-with-the-unified-evaluator) to manage Qwen and Xiaomi through one evaluation lifecycle. No manual HTTP service is needed, and the simulator environment does not import their model dependencies.

The af74484a contract correction passed local regression tests. A server disconnection prevented real-model verification after the fix. Earlier GPU smoke tests apply to the preceding version.

## Qwen3.5-9B vision-language model

Use `robotenon_qwen_reasoner.backend_qwen35:Provider` with a checkpoint declaring `model_type=qwen3_5`. The provider reuses the same image, JSON parsing, selective retry, session, and batching contracts. The vision-language model is `Qwen/Qwen3.5-9B`.

Install `requirements-qwen35.txt` in a Qwen-specific Python environment. Retain the Transformers 4 environment for Xiaomi. Run Qwen in a standalone framework batch service so the simulator remains independent of those dependencies.

```bash
/opt/envs/qwen35/bin/python -m pip install -r extensions/benchmarks/systems/robocasa_hierarchical/tool/qwen/requirements-qwen35.txt
/opt/envs/qwen35/bin/python -m pip install -e . \
  -e extensions/benchmarks/systems/robocasa_hierarchical/tool/qwen
```

Copy [reasoner.qwen35.json](../../configs/reasoner.qwen35.json) to a local configuration directory and set the weight path before launching:

```bash
/opt/envs/qwen35/bin/python -m robotenon.runtime.batch_services \
  --config /data/configs/reasoner.qwen35.json \
  --ready-file /data/runs/system/reasoner-ready.json \
  --metrics-dir /data/runs/system/qwen-batch-metrics \
  --startup-timeout 1800 --shutdown-timeout 30
```

Create the output parent directory first. `--config` points to the service configuration. `--ready-file` receives socket addresses when every model is ready. Use a dedicated file per evaluation and a separate `--metrics-dir` for batching statistics. The default startup and shutdown examples allow 1,800 seconds for loading and 30 seconds for graceful cleanup. Adjust to measured times.

`devices` lists model GPUs. Begin with `["0"]` for a single-GPU check. The throughput configuration uses `max_batch_size=32` and `max_wait_ms=500`. The wait window limits extra batching time while a GPU is idle. Requests may also queue behind a previous forward pass. The production progress protocol returns only `status` and a short subgoal, with `max_new_tokens=256` by default. Complete JSON stops early. Truncation activates the existing retry and budget expansion rules.

Remove the managed reasoner with the same name from the system `execution-config` and add `external_batch_services.reasoner`. Supply one shared address or one address per worker in worker order. With multiple GPUs, repeat the interleaved `model_devices` order `workers_per_device` times so workers connect to a local reasoner. Read addresses from the ready JSON rather than hardcoding temporary sockets. See [system integration](../../../../SYSTEMS.md) for routing and lifecycle. Stop the external Qwen service after the evaluation.

Unit tests cover architecture selection and shared contracts. Validate real weights, shared-GPU capacity, complete rollouts, and accuracy on the target hardware.

### Optional thinking mode

Provider option `enable_thinking` defaults to `false`. For checkpoints that support thinking, set it to `true`
and increase `max_new_tokens`, for example starting at 1024. This changes inference only and leaves weights unchanged.
When enabled, JSON completion detection examines only the final answer after `</think>`. JSON examples in reasoning do not end generation early.
Incomplete reasoning is handled as truncation. The graph receives only the final JSON.

This mode increases latency and token use. Its impact on RoboCasa365 success requires a controlled GPU evaluation.
Report accuracy from measured results, independently of whether thinking was enabled.
