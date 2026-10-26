#!/usr/bin/env bash
set -euo pipefail

name=agent_scaffold_qwen38_vllm_20261025
port=8108

if docker inspect "$name" >/dev/null 2>&1; then
  echo "container already exists: $name" >&2
  echo "Remove it explicitly with: docker rm -f $name" >&2
  exit 1
fi
if ss -ltn | awk '{print $4}' | grep -Eq ":${port}$"; then
  echo "port already in use: $port" >&2
  exit 1
fi
nvidia-smi --query-gpu=index,memory.used,utilization.gpu --format=csv,noheader

docker run -d \
  --name "$name" \
  --label codex-task=trajectory-export \
  --gpus all \
  --ipc=host \
  --shm-size=32g \
  -p "$port:$port" \
  -v /dev/shm/models:/slow_share/models \
  -e VLLM_ALLOW_LONG_MAX_MODEL_LEN=1 \
  -e VLLM_USE_RUST_FRONTEND=1 \
  vllm/vllm-openai:nightly \
  /slow_share/models/Qwen3.8-27B \
    --served-model-name qwen3.8-27b \
    --host 0.0.0.0 \
    --port "$port" \
    --api-key EMPTY \
    --tensor-parallel-size 8 \
    --trust-remote-code \
    --mm-encoder-tp-mode data \
    --reasoning-parser qwen3 \
    --max-model-len 1000000 \
    --max-num-seqs 64 \
    --gpu-memory-utilization 0.97 \
    --enable-auto-tool-choice \
    --tool-call-parser qwen3_coder \
    --media-io-kwargs '{"video": {"num_frames": -1}}' \
    --limit-mm-per-prompt '{"image":999,"video":50}' \
    --enable-prefix-caching \
    --mm-processor-cache-gb 0 \
    --enable-prompt-tokens-details
