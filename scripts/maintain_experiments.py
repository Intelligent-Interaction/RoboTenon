"""Preview/resume artifact retention or recover an index from individual case artifacts."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from robotenon.runtime.retention import RetentionPolicy, prune_artifacts
from robotenon.task_adapters.result_index import ResultIndex


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", default="data/experiments")
    parser.add_argument(
        "--policy", help="JSON with logs_days, traces_days, videos_days, failed_days and min_free_gb"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="delete eligible artifacts; otherwise preview only",
    )
    parser.add_argument(
        "--index-run",
        help="rebuild the query index for a terminal run (does not prune artifacts)",
    )
    args = parser.parse_args()
    if args.index_run:
        root = Path(args.index_run).resolve()
        try:
            result = ResultIndex(root).recover()
        except (RuntimeError, ValueError) as exc:
            parser.error(str(exc))
        print(json.dumps(result, ensure_ascii=False))
    else:
        policy = RetentionPolicy(
            **(json.loads(Path(args.policy).read_text()) if args.policy else {})
        )
        print(
            json.dumps(
                prune_artifacts(args.output_root, policy, apply=args.apply),
                ensure_ascii=False,
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
