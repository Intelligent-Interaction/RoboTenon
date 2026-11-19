"""GPU load benchmark, NOT a pretrained VLA or a simulator accuracy test.

Uses the actual Graph worker pool, shared service, image RPC and CUDA forward
passes. Compare single/batch and 1/2 replicas with the same episode workload.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import time
from pathlib import Path

import numpy as np

from robotenon.modules.base import BaseModule
from robotenon.runtime.registry import register_module


class CudaVisionPolicy:
    """Random-weight vision transformer as a controlled inference workload."""

    def __init__(self, batch_size=8):
        import torch
        from torch import nn

        self.torch = torch
        torch.manual_seed(123)
        torch.set_num_threads(1)
        self.patch = (
            nn.Conv2d(3, 512, kernel_size=16, stride=16).cuda().bfloat16().eval()
        )
        layer = nn.TransformerEncoderLayer(512, 8, 2048, dropout=0, batch_first=True)
        self.encoder = (
            nn.TransformerEncoder(layer, 8, enable_nested_tensor=False)
            .cuda()
            .bfloat16()
            .eval()
        )
        self.head = nn.Linear(512, 7).cuda().bfloat16().eval()
        self.sessions = set()
        with torch.inference_mode():
            for size in (1, batch_size):
                for _ in range(3):
                    self.forward(
                        torch.zeros(
                            size, 3, 224, 224, device="cuda", dtype=torch.bfloat16
                        )
                    )
            torch.cuda.synchronize()

    def forward(self, images):
        tokens = self.patch(images).flatten(2).transpose(1, 2)
        return self.head(self.encoder(tokens).mean(dim=1))

    def open_session(self, session_id, metadata):
        self.sessions.add(session_id)

    def infer_batch(self, requests):
        torch = self.torch
        arrays = np.stack([r["observation"]["image"] for r in requests])
        with torch.inference_mode():
            images = (
                torch.from_numpy(arrays)
                .to("cuda", dtype=torch.bfloat16)
                .permute(0, 3, 1, 2)
                / 255
            )
            actions = self.forward(images).float().cpu().numpy()
        return [
            {
                "request_id": r["request_id"],
                "output": {"action": action, "batch_size": len(requests)},
            }
            for r, action in zip(requests, actions)
        ]

    def close_session(self, session_id, result):
        self.sessions.remove(session_id)


@register_module("cuda_batch_load", kind="agent")
class CudaBatchLoad(BaseModule):
    async def run(self, ctx):
        # Different episode lengths exercise automatic refill.
        trial = int(getattr(ctx.task_item, "episode", 0) or 0)
        steps = 24 + (trial % 3) * 8
        image = np.random.default_rng(123).integers(
            0, 256, (224, 224, 3), dtype=np.uint8
        )
        sizes = []
        for step in range(steps):
            result = await ctx.call_tool("model", {"image": image, "step": step})
            if not result.ok or not np.all(np.isfinite(result.output["action"])):
                raise RuntimeError("invalid model action")
            sizes.append(result.output["batch_size"])
        return {"success": True, "requests": steps, "batch_sizes": sizes}


async def benchmark(args):
    from robotenon.runtime.experiment import ExperimentRequest, run_experiment

    baseline = json.loads(Path("examples/graph_batching/config.json").read_text())
    baseline["plugin_modules"] = ["examples.graph_batching.benchmark"]
    baseline["modules"] = [{"id": "ask", "name": "cuda_batch_load", "kind": "agent"}]
    baseline["graphs"][0]["outputs"] = {
        "success": {"source": "nodes.ask.outputs.success"}
    }
    baseline["task_adapters"][0]["params"]["num_trials"] = args.episodes
    rows = []
    args.output.mkdir(parents=True, exist_ok=True)
    for repeat in range(args.repeats):
        # Alternate order to reduce temporal/thermal bias.
        modes = ["single", "batch"] if repeat % 2 == 0 else ["batch", "single"]
        for mode in modes:
            config = copy.deepcopy(baseline)
            execution = config["parameters"]["runtime"]["execution"]
            execution.update(
                max_workers=args.workers,
                startup_timeout_s=180,
                task_timeout_s=180,
                shutdown_timeout_s=10,
                batch_services=[
                    {
                        "name": "echo",
                        "factory": "examples.graph_batching.benchmark:CudaVisionPolicy",
                        "params": {"batch_size": args.batch_size},
                        "devices": args.devices.split(","),
                        "mode": mode,
                        "max_batch_size": args.batch_size,
                        "max_wait_ms": args.wait_ms,
                    }
                ],
            )
            label = f"{mode}-{repeat}"
            path = args.output / f"{label}.json"
            path.write_text(json.dumps(config))
            start = time.perf_counter()
            report = await run_experiment(
                ExperimentRequest(config=path, output_dir=args.output, run_id=label)
            )
            elapsed = time.perf_counter() - start
            metrics = [
                json.loads(p.read_text())
                for p in (args.output / label / "batch-metrics").glob("*.json")
            ]
            if report["num_errors"] or report["successes"] != args.episodes:
                raise RuntimeError(f"load benchmark failed: {report}")
            requests = sum(m["requests"] for m in metrics)
            row = {
                "mode": mode,
                "repeat": repeat,
                "devices": args.devices,
                "workers": args.workers,
                "episodes": args.episodes,
                "requests": requests,
                "wall_sec": elapsed,
                "requests_per_sec": requests / elapsed,
                "episodes_per_hour": args.episodes * 3600 / elapsed,
                "model_metrics": metrics,
            }
            rows.append(row)
            (args.output / "measurements.json").write_text(json.dumps(rows, indent=2))
            print(json.dumps(row), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--devices", default="1")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--wait-ms", type=float, default=5)
    parser.add_argument("--episodes", type=int, default=16)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--output", required=True, type=Path)
    asyncio.run(benchmark(parser.parse_args()))


if __name__ == "__main__":
    main()
