"""Compatibility command; use robotenon_benchmark_support.evaluate for new runs."""
import sys
from robotenon_benchmark_support.evaluate import main as evaluate


def main():
    args = sys.argv[1:]
    if '--profile' not in args:
        args += ['--profile', 'all-0']
    evaluate(['--benchmark', 'libero_plus', *args])


if __name__ == '__main__':
    main()
