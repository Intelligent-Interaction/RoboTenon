"""Isolated task enumeration; simulator diagnostics go to stderr."""
import contextlib
import json
import sys
from pathlib import Path

from .integration import load_class, task_manifest
from .render import apply_render_mode


def main():
    request = json.load(sys.stdin)
    with contextlib.redirect_stdout(sys.stderr):
        cls = load_class(request["class"])
        apply_render_mode(cls, request["render"], request["class"])
        benchmark = cls(**request["params"])
        try:
            result = task_manifest(benchmark)
        finally:
            benchmark.cleanup()
    if request.get("output"):
        Path(request["output"]).write_text(json.dumps(result, allow_nan=False))
    else:
        json.dump(result, sys.stdout, allow_nan=False)


if __name__ == "__main__":
    main()
