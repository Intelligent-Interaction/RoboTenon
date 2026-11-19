"""Compatibility command; use robotenon_benchmark_support.evaluate for new runs."""
import sys
from robotenon_benchmark_support.evaluate import main as evaluate


def main():
    args = sys.argv[1:]
    if '--profile' not in args:
        args += ['--profile', 'eval']
    evaluate(['--benchmark', 'robocerebra', *args])


if __name__ == '__main__':
    main()
