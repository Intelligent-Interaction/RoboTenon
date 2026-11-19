"""Emit a small stream of metrics that is visible in the bundled Grafana.

The demo writes to ``runs/metrics.jsonl``.  The Compose ``metricsd`` service
mounts the same directory and exposes the WAL at ``:9100/metrics``; Prometheus
scrapes it every second and Grafana refreshes the dashboard every five seconds.
Explicit bucket layouts make the p90/p95 panels real histogram quantiles rather
than placeholder gauges.
This keeps the example runnable from the host without requiring a socket mount.
"""

from __future__ import annotations

import argparse
import asyncio
import math
from pathlib import Path

from robotenon.infra.metrics import MetricsClient, MetricsDaemon


ACTION_TYPES = ("joint_position", "cartesian_pose", "gripper", "wait")
ACTION_LATENCY_BUCKETS_MS = (25, 50, 75, 100, 150, 250, 500, 1000, 2500)
FEEDBACK_AGE_BUCKETS_MS = (10, 25, 50, 75, 100, 150, 250, 500, 1000)


async def emit_demo(*, wal: str, environment: str, interval: float, duration: float) -> None:
    daemon = MetricsDaemon(Path(wal))
    client = MetricsClient(daemon, flush_interval=min(max(interval / 2, 0.05), 0.5))
    await client.start()
    print(f"writing metrics to {wal} (environment={environment}; Ctrl-C to stop)")
    loop = asyncio.get_running_loop()
    started = loop.time()
    step = 0
    try:
        while duration <= 0 or loop.time() - started < duration:
            action_type = ACTION_TYPES[step % len(ACTION_TYPES)]
            phase = step / 3.0
            action_latency = 55.0 + 12.0 * math.sin(phase)
            feedback_age = 38.0 + 8.0 * math.cos(phase)

            # Keep labels low-cardinality.  These are the dimensions exposed
            # by the provisioned RoboTenon dashboard's dropdowns and filters.
            common = {"environment": environment, "scope": "step"}
            action_labels = {**common, "metric_group": "actions", "type": action_type}
            client.counter(
                "robotenon_actions_total",
                labels=action_labels,
            )
            client.gauge(
                "robotenon_feedback_sequence",
                float(step + 1),
                labels={**common, "metric_group": "feedback"},
            )
            # Histograms expose _bucket, _sum and _count series.  Grafana
            # computes windowed averages and p90/p95 quantiles from them.
            client.histogram(
                "robotenon_action_latency_ms",
                action_latency,
                labels=action_labels,
                buckets=ACTION_LATENCY_BUCKETS_MS,
                unit="ms",
            )
            client.histogram(
                "robotenon_feedback_age_ms",
                feedback_age,
                labels={**common, "metric_group": "feedback"},
                buckets=FEEDBACK_AGE_BUCKETS_MS,
                unit="ms",
            )
            if step and step % 12 == 0:
                client.counter(
                    "robotenon_evaluator_stale_total",
                    labels={**common, "metric_group": "evaluator"},
                )

            # Explicit flush makes the example immediately visible.  In a
            # normal control loop emit() stays non-blocking and the client's
            # resident background task flushes at its own cadence.
            await client.flush()
            print(f"step={step:04d} action={action_type:16s} latency={action_latency:5.1f} ms")
            step += 1
            await asyncio.sleep(max(interval, 0.01))
    finally:
        await client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="emit demo MetricsClient samples for RoboTenon Grafana")
    parser.add_argument("--wal", default="runs/metrics.jsonl", help="WAL path shared with Compose metricsd")
    parser.add_argument("--environment", default="demo", help="low-cardinality environment label")
    parser.add_argument("--interval", type=float, default=1.0, help="seconds between samples")
    parser.add_argument("--duration", type=float, default=60.0, help="seconds; 0 runs until Ctrl-C")
    args = parser.parse_args()
    try:
        asyncio.run(emit_demo(wal=args.wal, environment=args.environment, interval=args.interval, duration=args.duration))
    except KeyboardInterrupt:
        print("stopped")


if __name__ == "__main__":
    main()
