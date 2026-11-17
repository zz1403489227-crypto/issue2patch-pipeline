#!/usr/bin/env bash
# Model server for the pipeline: Qwen3.6-35B-A3B-FP8 on two RTX 4090s, thinking mode on.
#
# Flags come from the official model card (Qwen/Qwen3.6-35B-A3B-FP8 README, "vLLM" section):
#   --reasoning-parser qwen3 --enable-auto-tool-choice --tool-call-parser qwen3_coder
#   --language-model-only (text only; skips the vision encoder)
#   "maintain a context length of at least 128K tokens to preserve thinking capabilities"
# Project-specific, each recorded in docs/decision-log.md:
#   --tensor-parallel-size 2   the FP8 weights (34.9 GiB) do not fit one 24 GB card
#   --max-model-len 131072     the 128K minimum from the card; KV cache is the limit on 2 cards
#   --max-num-seqs 8           tasks run concurrently
#   --enable-prompt-tokens-details  so the gateway can record cached prompt tokens
#   VLLM_USE_FLASHINFER_SAMPLER=0   measured: vLLM 0.33 + FlashInfer 0.7.2 JIT-compile the sampler with the
#                                   system nvcc, which rejects --compress-mode and the engine dies at startup;
#                                   with 0, vLLM samples with its PyTorch implementation (same top-k/top-p)
# Sampling is left to generation_config.json (temperature 1.0, top_p 0.95, top_k 20), which is
# the setting the card reports for its SWE-bench evaluation.
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
GPUS=${GPUS:-1,3}
PORT=${PORT:-18949}
MAX_SEQS=${MAX_SEQS:-8}
MAX_LEN=${MAX_LEN:-131072}
MODEL=$ROOT/var/models/Qwen3.6-35B-A3B-FP8   # verified against the official revision, see MANIFEST.json
mkdir -p $ROOT/var/vllm-cache $ROOT/var/tmp $ROOT/var/logs
export CUDA_VISIBLE_DEVICES=$GPUS VLLM_USE_FLASHINFER_SAMPLER=0 HF_HUB_OFFLINE=1 VLLM_CACHE_ROOT=$ROOT/var/vllm-cache TMPDIR=$ROOT/var/tmp
exec $ROOT/.venv-vllm/bin/vllm serve "$MODEL" \
  --served-model-name qwen3.6-35b-a3b \
  --host 127.0.0.1 --port "$PORT" \
  --tensor-parallel-size 2 \
  --max-model-len "$MAX_LEN" --max-num-seqs "$MAX_SEQS" \
  --reasoning-parser qwen3 --enable-auto-tool-choice --tool-call-parser qwen3_coder \
  --language-model-only \
  --enable-prompt-tokens-details
