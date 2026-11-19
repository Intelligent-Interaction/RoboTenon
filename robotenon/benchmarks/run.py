"""Run a native benchmark Graph configuration through RoboTenon experiments."""
import argparse
import asyncio
import json
from pathlib import Path
from ..runtime.experiment import ExperimentRequest, run_experiment


def experiment_failed(result):
    if result.get('num_errors', 0) or result.get('num_cancelled', 0):
        return True
    for adapter in result.get('task_adapters', {}).values():
        for episode in adapter.get('episodes', []):
            graph = episode.get('result', {})
            if graph.get('graph_status') != 'completed':
                return True
    return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--output-dir', type=Path, default=Path('runs'))
    parser.add_argument('--run-id', required=True)
    args = parser.parse_args()
    result = asyncio.run(run_experiment(ExperimentRequest(
        config=args.config, output_dir=args.output_dir, run_id=args.run_id)))
    print(json.dumps(result, indent=2, default=str))
    if experiment_failed(result):
        raise SystemExit(1)

if __name__ == '__main__':
    main()
