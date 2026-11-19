# Benchmark requirements and success-rate breakdowns

[**English**](benchmark-requirements-and-results.md) · [简体中文](benchmark-requirements-and-results.zh-CN.md)

## Runtime requirements

Each standalone benchmark owns `extensions/benchmarks/<benchmark>/requirements.txt`. Install it
inside a dedicated simulator environment, then install RoboTenon, benchmark support,
and that extension:

```sh
python -m pip install -r extensions/benchmarks/libero/requirements.txt
python -m pip install -e . -e extensions/benchmarks/benchmark-support -e extensions/benchmarks/libero
```

The files pin Python packages and source repositories to the reviewed upstream
environment. System libraries, licensed simulators, datasets and assets cannot be
expressed safely as pip requirements; the first comments in each requirements file
and `protocol-reference/upstream-environment.txt` identify those prerequisites.
Keep conflicting simulators in separate environments. In particular, BEHAVIOR-1K
and RoboDojo require different Isaac Sim versions.

Downloads should run on the execution server. Asset archives are not routed through
the developer workstation or committed to Git.

## Success rates

Every episode output now contains `native_dimensions`. The result aggregator emits:

- `overall`: total successes, errors and success rate across terminal episodes;
- `by_task`: success rate for each native task;
- `by_dimension`: success rate for every extension-declared dimension and value;
- legacy top-level `mean_success` and `tasks` fields for compatibility.

Errors and failed attempts count as unsuccessful. Retry deduplication still keeps the
last attempt for each stable TaskCase identity. Pending/running episodes are excluded
and make the summary `partial` when the expected count has not completed.

```sh
python -m robotenon_benchmark_support.results \
  runs/my-eval/**/episode_*.json \
  --expected-episodes 100 \
  --output runs/my-eval/summary.json
```

Example shape:

```json
{
  "overall": {"num_episodes": 100, "num_successes": 63, "success_rate": 0.63},
  "by_dimension": {
    "suite": [
      {"value": "seen", "num_episodes": 50, "num_successes": 36, "success_rate": 0.72},
      {"value": "unseen", "num_episodes": 50, "num_successes": 27, "success_rate": 0.54}
    ]
  }
}
```

## Declared dimensions

| Benchmark | Success-rate dimensions |
| --- | --- |
| BEHAVIOR-1K | suite, task |
| CALVIN | evaluation sequence, initial condition |
| DuoBench | task |
| Kinetix | level |
| LIBERO | suite, task |
| LIBERO-Mem | suite, task |
| LIBERO-Plus | suite, category, difficulty, task |
| LIBERO-Pro | suite, task |
| ManiSkill2 | environment |
| MIKASA | task |
| MolmoSpaces | task type, task |
| RLBench | task |
| RoboCasa | task |
| RoboCasa365 | suite, task |
| RoboCerebra | task type, case |
| RoboDojo | suite, task |
| RoboMME | environment |
| RoboTwin | suite, task |
| Simpler | task, environment, scene, robot |
| VLABench | task, robot |

Only dimensions present in an episode are emitted. This prevents empty or fabricated
breakdowns when a profile does not provide optional metadata.
