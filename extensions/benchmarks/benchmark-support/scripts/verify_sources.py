"""Verify native source hashes and AST after documented import substitutions.

Usage: python verify_benchmark_sources.py ROBOTENON_ROOT UPSTREAM_CHECKOUT
"""
import argparse
import ast
import hashlib
import json
from pathlib import Path

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('robotenon', type=Path)
    parser.add_argument('upstream', type=Path)
    args = parser.parse_args()
    count = 0
    catalog = json.loads((args.robotenon/'docs/benchmark-catalog.json').read_text())
    for entry in catalog:
        extension = args.robotenon/entry['path']
        lock = json.loads((extension/'benchmark-source.json').read_text())
        for item in lock['files']:
            original = (args.upstream/item['source']).read_text()
            if hashlib.sha256(original.encode()).hexdigest() != item['sha256']:
                raise ValueError(f"Source hash mismatch: {item['source']}")
            for old, new in item['import_rewrites'].items():
                original = original.replace(old, new)
            original = original.replace('vla_eval.', 'robotenon_benchmark_support.')
            target = (extension/item['target']).read_text()
            if ast.dump(ast.parse(original)) != ast.dump(ast.parse(target)):
                raise ValueError(f"Native AST mismatch: {item['target']}")
            count += 1
    for name, source in [('base', 'benchmarks/base'), ('types', 'types'),
                         ('specs', 'specs'), ('rotation', 'rotation')]:
        original = (args.upstream/f'src/vla_eval/{source}.py').read_text()
        original = original.replace('vla_eval.benchmarks.base', 'robotenon_benchmark_support.base')
        original = original.replace('vla_eval.', 'robotenon_benchmark_support.')
        target = (args.robotenon/f'extensions/benchmarks/benchmark-support/src/robotenon_benchmark_support/{name}.py').read_text()
        if ast.dump(ast.parse(original)) != ast.dump(ast.parse(target)):
            raise ValueError(f'Shared AST mismatch: {name}')
    print(json.dumps({'extensions': len(catalog), 'native_files_verified': count,
                      'shared_contracts_verified': 4, 'simulation_parity': 'not_tested'}))

if __name__ == '__main__':
    main()
