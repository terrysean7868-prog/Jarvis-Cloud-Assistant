# Cloud Owned Model (No OpenAI/Groq Billing)

This project can run against your own cloud-hosted model endpoint.

## 1. Run model service

Install model service dependencies:

```bash
pip install -r requirements/model_service.txt
```

Start the OpenAI-compatible model server:

```bash
python -m apps.model_service.server
```

By default it serves:

- `POST /v1/chat/completions`
- `GET /health`

Env options:

- `SELF_HOSTED_LLM_MODEL` (default: `Qwen/Qwen2.5-7B-Instruct`)
- `MODEL_SERVICE_HOST` (default: `0.0.0.0`)
- `MODEL_SERVICE_PORT` (default: `8010`)

## 2. Point Jarvis API to your model service

Configure code defaults in `src/config/runtime_defaults.py`:

- `SELF_HOSTED_LLM_ENABLED = True`
- `SELF_HOSTED_LLM_ENDPOINT = "http://127.0.0.1:8010/v1/chat/completions"` (or your remote service URL)
- `SELF_HOSTED_LLM_MODEL = "Qwen/Qwen2.5-7B-Instruct"`

Optional auth (only if your model service requires it):

- `SELF_HOSTED_LLM_API_KEY` as an environment secret.

Jarvis adapter routes primary and fallback LLM calls to this endpoint when enabled.

## 3. Train your own adapter (LoRA)

Prepare `data/ai_training/sft.jsonl` with records containing either:

- `user` + `assistant`, or
- `input` + `output`

Then run:

```bash
python scripts/train_self_hosted_lora.py --dataset data/ai_training/sft.jsonl --base-model Qwen/Qwen2.5-7B-Instruct --output-dir models/jarvis-lora --epochs 1
```

Notes:

- This is real model training (LoRA), not prompt-only tuning.
- GPU is strongly recommended.
- For best quality, use 500+ high-quality task/response pairs.

## 4. Production recommendation

- Deploy model service on GPU cloud (RunPod, Vast, Lambda, etc.)
- Keep Jarvis API and model service separate for scaling
- Add request auth/rate limits at the model service ingress
