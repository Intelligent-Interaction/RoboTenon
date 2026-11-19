#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT=${REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)}
: "${SHARED_ROOT:?Set SHARED_ROOT to the directory containing sources, profiles, and runs}"
MODEL_PY=${MODEL_PY:-python3}
SIM_PY=${SIM_PY:-$MODEL_PY}
BACKEND_CONFIG=${BACKEND_CONFIG:-$REPO_ROOT/extensions/benchmarks/models/pi05-robotwin2/configs/robotwin2.json}
PROFILE_ROOT=${PROFILE_ROOT:-$SHARED_ROOT/profiles/robotwin2-official}
OUTPUT=${OUTPUT:-$SHARED_ROOT/runs/pi05-robotwin2-official-full}

export PYTHONPATH="$SHARED_ROOT/sources/openpi/src${PYTHONPATH:+:$PYTHONPATH}"
export OMP_NUM_THREADS=1
export MKL_NUM_THREADS=1
export OPENBLAS_NUM_THREADS=1
export NUMEXPR_NUM_THREADS=1

"$MODEL_PY" "$REPO_ROOT/extensions/benchmarks/robotwin/scripts/generate_official_profiles.py" "$PROFILE_ROOT"

cd "$REPO_ROOT"
exec "$MODEL_PY" -m robotenon_benchmark_support.evaluate \
  --benchmark robotwin \
  --profiles "$PROFILE_ROOT"/demo_clean/*.json "$PROFILE_ROOT"/demo_randomized/*.json \
  --backend "$REPO_ROOT/extensions/benchmarks/models/pi05-robotwin2/vla.py" \
  --backend-config "$BACKEND_CONFIG" \
  --model-python "$MODEL_PY" \
  --sim-python "$SIM_PY" \
  --model-devices 0,1,2,3,4,5,6,7 \
  --workers-per-gpu 12 \
  --batch-size 32 \
  --wait-ms 50 \
  --trace-mode summary \
  --episodes-per-task 1 \
  --startup-parallelism 12 \
  --enumeration-parallelism 24 \
  --recycle-after-tasks 8 \
  --model-startup-timeout 900 \
  --worker-startup-timeout 900 \
  --task-timeout 7200 \
  --scheduling-strategy global_queue \
  --output "$OUTPUT"
