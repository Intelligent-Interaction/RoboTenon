#!/usr/bin/env python3
"""Unified configuration-driven RoboTenon entry point."""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from robotenon.runtime.runner_cli import main, parse_args, run

__all__ = ["main", "parse_args", "run"]


if __name__ == "__main__":
    raise SystemExit(main())
