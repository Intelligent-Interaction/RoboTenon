#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

MODEL_PATH="${QWEN_MODEL_PATH:-/root/models/Qwen3.5-9B}"
GPU_ID="${QWEN_GPU:-0}"
PORT="${QWEN_PORT:-8000}"
HOST="${QWEN_HOST:-127.0.0.1}"
TRANSFORMERS_BIN="${QWEN_TRANSFORMERS_BIN:-/root/venvs/qwen35/bin/transformers}"
LOG_DIR="${XR365_LOG_DIR:-data/xr365}"
PID_FILE="$LOG_DIR/qwen_server.pid"
LOG_FILE="$LOG_DIR/qwen_server.log"

mkdir -p "$LOG_DIR"
if [[ -s "$PID_FILE" ]] && kill -0 "$(<"$PID_FILE")" 2>/dev/null; then
  echo "Qwen3.5-9B server is already running (pid $(<"$PID_FILE"))"
  exit 0
fi
if [[ ! -x "$TRANSFORMERS_BIN" ]]; then
  echo "transformers executable not found: $TRANSFORMERS_BIN" >&2
  exit 1
fi
if [[ ! -d "$MODEL_PATH" ]]; then
  echo "model directory not found: $MODEL_PATH" >&2
  exit 1
fi

USE_HUB_KERNELS=NO CUDA_VISIBLE_DEVICES="$GPU_ID" nohup "$TRANSFORMERS_BIN" serve "$MODEL_PATH" \
  --host "$HOST" \
  --port "$PORT" \
  --device cuda:0 \
  --dtype bfloat16 \
  --attn-implementation sdpa \
  --reasoning off \
  --log-level info >"$LOG_FILE" 2>&1 &
echo $! >"$PID_FILE"
echo "Qwen3.5-9B server started on $HOST:$PORT using GPU $GPU_ID (pid $(<"$PID_FILE"))"
