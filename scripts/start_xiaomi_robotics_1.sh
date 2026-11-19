#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

MODEL_PATH="${XR_MODEL_PATH:-/root/models/Xiaomi-Robotics-1-RoboCasa365}"
GPU_ID="${XR_GPU:-1}"
PORT="${XR_MODEL_PORT:-10086}"
HOST="${XR_MODEL_HOST:-127.0.0.1}"
PYTHON_BIN="${XR_PYTHON_BIN:-python}"
SERVER="vendor/Xiaomi-Robotics-1/deploy/server.py"
LOG_DIR="${XR365_LOG_DIR:-data/xr365}"
PID_FILE="$LOG_DIR/xiaomi_server.pid"
LOG_FILE="$LOG_DIR/xiaomi_server.log"

mkdir -p "$LOG_DIR"
if [[ -s "$PID_FILE" ]] && kill -0 "$(<"$PID_FILE")" 2>/dev/null; then
  echo "Xiaomi-Robotics-1 server is already running (pid $(<"$PID_FILE"))"
  exit 0
fi
if [[ ! -f "$SERVER" ]]; then
  echo "Xiaomi-Robotics-1 server not found: $SERVER" >&2
  exit 1
fi
if [[ ! -d "$MODEL_PATH" ]]; then
  echo "model directory not found: $MODEL_PATH" >&2
  exit 1
fi

CUDA_VISIBLE_DEVICES="$GPU_ID" nohup "$PYTHON_BIN" "$SERVER" \
  --model "$MODEL_PATH" \
  --host "$HOST" \
  --port "$PORT" >"$LOG_FILE" 2>&1 &
echo $! >"$PID_FILE"
echo "Xiaomi-Robotics-1 server started on $HOST:$PORT using GPU $GPU_ID (pid $(<"$PID_FILE"))"
