"""Episode order only: core process workers dynamically consume the shared queue."""
import random


def order_cases(cases, scheduling):
    strategy = scheduling.get('strategy', 'global_queue')
    if strategy not in {'global_queue', 'static'}:
        raise ValueError('strategy must be global_queue or static')
    if len({case.key for case in cases}) != len(cases):
        raise ValueError('duplicate episode identity')
    cases = list(cases)
    if scheduling.get('shuffle', True):
        random.Random(scheduling.get('seed', 7)).shuffle(cases)
    return cases
