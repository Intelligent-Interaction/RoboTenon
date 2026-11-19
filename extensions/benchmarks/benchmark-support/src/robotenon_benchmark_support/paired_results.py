"""Compare complete runs by declared episode identity, never finish order.

Matching seeds do not prove identical initial simulator states. These statistics
do not establish a policy effect unless environment reproducibility is audited
separately.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import random
import sqlite3


def identity(record):
    return (record['adapter_id'], record['task_id'], record['episode'], record['seed'])


def unique(records):
    result = {}
    for record in records:
        key = identity(record)
        if key in result:
            raise ValueError(f'duplicate episode: {key}')
        result[key] = record
    return result


def read_run(directory):
    directory = Path(directory)
    protocol = json.loads((directory / 'protocol.json').read_text())
    plan = json.loads((directory / 'episode-plan.json').read_text())
    planned = unique(plan['episodes'])
    if len(planned) != plan['expected_episodes']:
        raise ValueError('episode plan count mismatch')
    run_id = protocol['execution']['run_id']
    database = directory / 'runs' / run_id / 'episodes.sqlite3'
    connection = sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)
    try:
        records = unique(json.loads(row[0]) for row in connection.execute('SELECT record_json FROM episodes'))
    finally:
        connection.close()
    if set(records) - set(planned):
        raise ValueError('unplanned episode in results')
    complete = {key: row for key, row in records.items() if row['status'] == 'completed'}
    for row in complete.values():
        if type(row.get('task_success')) is not bool:
            raise ValueError('completed episode has no boolean success')
    return protocol, planned, complete, Counter(row['status'] for row in records.values())


def paired_statistics(baseline, system, *, draws=10000, seed=7):
    if baseline.keys() != system.keys() or not baseline:
        raise ValueError('statistics require nonempty identical paired episode sets')
    if draws < 1:
        raise ValueError('bootstrap draws must be positive')
    counts = Counter(both=0, system_only=0, baseline_only=0, neither=0)
    tasks = defaultdict(list)
    for key in sorted(baseline):
        a, b = baseline[key]['task_success'], system[key]['task_success']
        counts['both' if a and b else 'system_only' if b else 'baseline_only' if a else 'neither'] += 1
        tasks[key[:2]].append((int(a), int(b)))
    n = len(baseline)
    groups = list(tasks.values())
    # Resample whole tasks to retain dependence between seeds of the same task.
    rng = random.Random(seed)
    samples = []
    for _ in range(draws):
        chosen = [groups[rng.randrange(len(groups))] for _ in groups]
        samples.append(sum(b-a for group in chosen for a,b in group) / sum(map(len, chosen)))
    samples.sort()
    per_task = []
    for key, rows in sorted(tasks.items()):
        outcomes = Counter(
            'both' if a and b else 'system_only' if b else 'baseline_only' if a else 'neither'
            for a, b in rows
        )
        per_task.append(dict(
            adapter_id=key[0], task=key[1], episodes=len(rows),
            baseline_successes=sum(a for a, b in rows),
            system_successes=sum(b for a, b in rows),
            **{name: outcomes[name] for name in counts},
        ))
    baseline_successes = counts['both'] + counts['baseline_only']
    baseline_failures = counts['system_only'] + counts['neither']
    return {
        'paired_episodes': n, 'tasks': len(tasks), **counts,
        'baseline_success_rate': (counts['both'] + counts['baseline_only']) / n,
        'system_success_rate': (counts['both'] + counts['system_only']) / n,
        'difference': (counts['system_only'] - counts['baseline_only']) / n,
        'task_cluster_bootstrap_95_interval': [samples[int(.025*draws)], samples[min(draws-1, int(.975*draws))]],
        'bootstrap_draws': draws, 'bootstrap_seed': seed,
        # Null means the conditioning group is empty, not a zero-percent rate.
        'baseline_success_retention_rate': (
            counts['both'] / baseline_successes if baseline_successes else None
        ),
        'baseline_failure_recovery_rate': (
            counts['system_only'] / baseline_failures if baseline_failures else None
        ),
        'per_task': per_task,
    }


def compare(baseline_path, system_path):
    ap, plan_a, a, statuses_a = read_run(baseline_path)
    bp, plan_b, b, statuses_b = read_run(system_path)
    for field in ('benchmark', 'effective_profile', 'backend_config', 'backend_spec', 'rng_mode'):
        if ap[field] != bp[field]:
            raise ValueError(f'paired protocol mismatch: {field}')
    if plan_a != plan_b:
        raise ValueError('episode plans differ in identity, seed, horizon, or environment parameters')
    complete = a.keys() == b.keys() == plan_a.keys()
    report = {'complete': complete, 'expected_episodes_per_arm': len(plan_a),
              'initial_state_alignment': 'not_verified',
              'interpretation': 'Pairs match declared episode identities and seeds. Initial images and '
                                'simulator states are not checked; differences and intervals must not '
                                'be interpreted as isolated policy effects without a reset audit.',
              'baseline_statuses': dict(statuses_a), 'system_statuses': dict(statuses_b),
              'baseline_completed': len(a), 'system_completed': len(b),
              'rng_mode': ap['rng_mode'], 'profile': ap['effective_profile'],
              'execution_differences': {key: [ap['execution'].get(key), bp['execution'].get(key)]
                  for key in ('batch_size', 'wait_ms', 'model_devices', 'workers_per_gpu',
                              'scheduler_seed', 'scheduling_strategy')
                  if ap['execution'].get(key) != bp['execution'].get(key)}}
    if complete:
        report.update(paired_statistics(a, b))
    else:
        report['note'] = 'Incomplete runs: no success-rate comparison; finish order can bias the sample.'
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('baseline', type=Path)
    parser.add_argument('system', type=Path)
    args = parser.parse_args()
    print(json.dumps(compare(args.baseline, args.system), indent=2))


if __name__ == '__main__':
    main()
