"""Reproduce stale-renderer GC corruption and verify the extension EGL fix.

Run in the installed simulator environment with RoboCasa assets available:
  MUJOCO_GL=egl python extensions/benchmarks/robocasa365/tools/check_egl_cleanup.py --output /tmp/egl-check
Each arm runs in a fresh process. This checks rendering/physics, without a VLA.
"""
from __future__ import annotations

import argparse
import gc
import json
import os
from pathlib import Path
import subprocess
import sys

import numpy as np


def worker(args):
    import gymnasium as gym
    import robocasa  # noqa: F401
    from robocasa.utils.env_utils import convert_action
    from robotenon_robocasa365.benchmark import STATE_KEYS, VIDEO_KEYS

    if args.worker == "protected":
        from robotenon_robocasa365._internal.rendering import install_egl_cleanup_guard
        install_egl_cleanup_guard()
    # Reclaim the stale context at a controlled point, rather than depending
    # on Python allocations or Graph scheduling to trigger collection.
    gc.disable()
    env = gym.make("robocasa/CloseFridge", split="pretrain", seed=7)
    observation, _ = env.reset(seed=57)
    actions = np.random.default_rng(42).uniform(-0.1, 0.1, (32, 12)).astype(np.float32)
    records = {}
    try:
        for step in range(33):
            for key in (*VIDEO_KEYS, *STATE_KEYS):
                records[f"{step}/{key}"] = np.asarray(observation[key]).copy()
            if step == 32:
                break
            if step == 8 and args.worker != "reference":
                gc.collect()
            observation, _, _, _, _ = env.step(convert_action(actions[step]))
    finally:
        env.close()
    np.savez_compressed(args.output / f"{args.worker}.npz", **records)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--worker", choices=("reference", "unprotected", "protected"))
    args = parser.parse_args()
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    if args.worker:
        worker(args)
        return
    environment = dict(os.environ, MUJOCO_GL="egl", PYOPENGL_PLATFORM="egl")
    for mode in ("reference", "unprotected", "protected"):
        with (args.output / f"{mode}.log").open("w") as log:
            subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), "--output",
                 str(args.output), "--worker", mode],
                env=environment, stdout=log, stderr=subprocess.STDOUT, check=True,
            )
    report = {}
    with np.load(args.output / "reference.npz") as reference:
        for mode in ("unprotected", "protected"):
            state_mismatches = []
            max_pixel_error, max_mean_pixel_error = 0.0, 0.0
            with np.load(args.output / f"{mode}.npz") as actual:
                for key in reference.files:
                    if "/video." in key:
                        delta = np.abs(reference[key].astype(float) - actual[key])
                        max_pixel_error = max(max_pixel_error, float(delta.max()))
                        max_mean_pixel_error = max(max_mean_pixel_error, float(delta.mean()))
                    elif not np.array_equal(reference[key], actual[key]):
                        state_mismatches.append(key)
            report[mode] = {
                "state_mismatches": state_mismatches,
                "max_pixel_error": max_pixel_error,
                "max_mean_pixel_error": max_mean_pixel_error,
            }
    fixed = report["protected"]
    report["passed"] = not fixed["state_mismatches"] and fixed["max_pixel_error"] <= 1
    (args.output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
