#!/usr/bin/env bash
set -euo pipefail

root=/home/tsq/workspace/agent-scaffold-trajectory-export
name=agent_scaffold_qwen35_2b_megatron_20261025_retry10
image=3d-code-gen:swift4.5.3
dataset_dir="$root/e2e/converted-20261025-qwen38-401-full"
output_dir="$root/e2e/training/qwen3.5-2b-full-20261025-retry10"
max_length="${MAX_LENGTH:-8192}"
dataset_file="${DATASET_FILE:-train.megatron.jsonl}"

if docker inspect "$name" >/dev/null 2>&1; then
  echo "container already exists: $name" >&2
  exit 1
fi
if [[ -e "$output_dir" ]]; then
  echo "output directory already exists: $output_dir" >&2
  exit 1
fi
if [[ ! -f "$dataset_dir/$dataset_file" ]]; then
  echo "prepared dataset not found: $dataset_dir/$dataset_file" >&2
  exit 1
fi

mkdir -p "$output_dir"
docker run --rm \
  --name "$name" \
  --label codex-task=trajectory-export-training \
  --gpus '"device=4,6"' \
  --ipc=host \
  --shm-size=32g \
  -v /slow_share/models:/slow_share/models:ro \
  -v "$dataset_dir:/data:ro" \
  -v "$output_dir:/output" \
  -e "MAX_LENGTH=$max_length" \
  -e "DATASET_FILE=$dataset_file" \
  -e 'ROPE_SCALING={"rope_type":"yarn","mrope_interleaved":true,"mrope_section":[11,11,10],"rope_theta":10000000,"partial_rotary_factor":0.25,"factor":4.0,"original_max_position_embeddings":262144}' \
  --entrypoint bash \
  "$image" -lc '
    export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
    export NPROC_PER_NODE=2
    export CUDA_DEVICE_MAX_CONNECTIONS=1
    export HF_HUB_OFFLINE=1
    export TRANSFORMERS_OFFLINE=1
    export IMAGE_MAX_TOKEN_NUM=1024
    megatron sft \
      --model /slow_share/models/Qwen3.5-2B \
      --agent_template qwen3_5 \
      --tuner_type full \
      --finetune true \
      --freeze_llm false \
      --freeze_vit false \
      --freeze_aligner false \
      --dataset "/data/$DATASET_FILE" \
      --output_dir /output \
      --tensor_model_parallel_size 1 \
      --context_parallel_size 2 \
      --micro_batch_size 1 \
      --global_batch_size 1 \
      --rope_scaling "$ROPE_SCALING" \
      --max_model_len 1000000 \
      --max_length "$MAX_LENGTH" \
      --truncation_strategy left \
      --recompute_granularity full \
      --recompute_method uniform \
      --recompute_num_layers 1 \
      --lr 1e-5 \
      --lr_warmup_fraction 0.05 \
      --min_lr 1e-6 \
      --num_train_epochs 1 \
      --save_strategy epoch \
      --save_safetensors true \
      --no_save_optim true \
      --no_save_rng true \
      --dataset_num_proc 1 \
      --dataloader_num_workers 0
  '
