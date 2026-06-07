from __future__ import annotations

import os
import time
import uuid
import logging
from typing import Any
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionsRequest(BaseModel):
    model: str | None = None
    messages: list[ChatMessage] = Field(default_factory=list)
    temperature: float = 0.4
    max_tokens: int = 512
    stream: bool = False


class LoRAWeightsRequest(BaseModel):
    adapter_name: str = Field(..., description="Name/path of LoRA adapter to load")
    merge: bool = Field(False, description="Whether to merge adapter weights into base model")


class _ModelRuntime:
    def __init__(self) -> None:
        self._tokenizer = None
        self._model = None
        self._model_id = ""
        self._lora_adapter_name = None  # Track loaded LoRA adapter
        self._lora_config = None

    def _resolve_model_id(self, requested: str | None) -> str:
        return (
            str(requested or "").strip()
            or (os.getenv("SELF_HOSTED_LLM_MODEL") or "").strip()
            or "Qwen/Qwen2.5-7B-Instruct"
        )

    def ensure_loaded(self, requested_model: str | None) -> str:
        model_id = self._resolve_model_id(requested_model)
        if self._model is not None and self._tokenizer is not None and model_id == self._model_id:
            return model_id

        try:
            # pyrefly: ignore [missing-import]
            import torch
            # pyrefly: ignore [missing-import]
            from transformers import AutoModelForCausalLM, AutoTokenizer
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail=(
                    "transformers/torch are not installed for model service. "
                    "Install requirements/model_service.txt first."
                ),
            ) from exc

        dtype = getattr(torch, "float16", None)
        if not torch.cuda.is_available():
            dtype = getattr(torch, "float32", None)

        self._tokenizer = AutoTokenizer.from_pretrained(model_id, trust_remote_code=True)
        self._model = AutoModelForCausalLM.from_pretrained(
            model_id,
            torch_dtype=dtype,
            device_map="auto",
            trust_remote_code=True,
        )
        self._model_id = model_id
        self._lora_adapter_name = None
        logger.info(f"Loaded base model: {model_id}")

        # Try to auto-load existing LoRA adapter if available
        self._auto_load_lora()

        return model_id

    def _auto_load_lora(self):
        """Auto-load LoRA adapter if it exists in the default location."""
        default_lora_paths = [
            Path("models/jarvis-lora"),  # train_self_hosted_lora.py default
            Path("models/jarvis_custom"),  # train_model_job.py default
        ]

        for lora_path in default_lora_paths:
            if lora_path.exists() and (lora_path / "adapter_config.json").exists():
                try:
                    logger.info(f"Found LoRA adapter at {lora_path}, loading...")
                    result = self.load_lora_adapter(str(lora_path), merge=False)
                    logger.info(f"Auto-loaded LoRA adapter: {result}")
                    return
                except Exception as e:
                    logger.warning(f"Failed to auto-load LoRA from {lora_path}: {e}")
                    continue

    def load_lora_adapter(self, adapter_name: str, merge: bool = False) -> dict[str, Any]:
        """Load LoRA adapter weights into the model."""
        if not self._model:
            raise HTTPException(status_code=400, detail="Base model not loaded. Call ensure_loaded first")

        try:
            from peft import PeftModel
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail="peft library is not installed. Install requirements/model_service.txt first.",
            ) from exc

        try:
            # Load LoRA adapter
            model_with_lora = PeftModel.from_pretrained(self._model, adapter_name)

            if merge:
                # Merge LoRA weights into base model (one-way operation)
                model_with_lora = model_with_lora.merge_and_unload()
                self._model = model_with_lora
                self._lora_adapter_name = None
                logger.info(f"LoRA adapter '{adapter_name}' merged into base model")
                return {
                    "status": "merged",
                    "adapter_name": adapter_name,
                    "message": f"LoRA weights merged into {self._model_id}",
                }
            else:
                # Keep adapter separate
                self._model = model_with_lora
                self._lora_adapter_name = adapter_name
                logger.info(f"LoRA adapter '{adapter_name}' loaded")
                return {
                    "status": "loaded",
                    "adapter_name": adapter_name,
                    "message": f"LoRA adapter loaded: {adapter_name}",
                }
        except FileNotFoundError:
            raise HTTPException(
                status_code=404,
                detail=f"LoRA adapter not found: {adapter_name}",
            )
        except Exception as exc:
            logger.error(f"Error loading LoRA adapter '{adapter_name}': {exc}")
            raise HTTPException(
                status_code=500,
                detail=f"Failed to load LoRA adapter: {str(exc)}",
            ) from exc

    def generate(self, req: ChatCompletionsRequest) -> tuple[str, str]:
        model_id = self.ensure_loaded(req.model)

        try:
            # pyrefly: ignore [missing-import]
            import torch
        except Exception as exc:
            raise HTTPException(status_code=503, detail="torch is unavailable") from exc

        if not req.messages:
            raise HTTPException(status_code=400, detail="messages cannot be empty")

        safe_max_new_tokens = max(16, min(int(req.max_tokens or 256), 1024))
        safe_temperature = max(0.0, min(float(req.temperature or 0.4), 1.5))

        conversation: list[dict[str, str]] = []
        for m in req.messages:
            role = str(m.role or "user").strip().lower()
            content = str(m.content or "").strip()
            if not content:
                continue
            if role not in {"system", "user", "assistant"}:
                role = "user"
            conversation.append({"role": role, "content": content})

        if not conversation:
            raise HTTPException(status_code=400, detail="messages cannot be empty")

        tokenizer = self._tokenizer
        model = self._model
        assert tokenizer is not None
        assert model is not None

        if hasattr(tokenizer, "apply_chat_template"):
            prompt = tokenizer.apply_chat_template(
                conversation,
                tokenize=False,
                add_generation_prompt=True,
            )
        else:
            prompt = "\n".join([f"{x['role']}: {x['content']}" for x in conversation]) + "\nassistant:"

        encoded = tokenizer(prompt, return_tensors="pt")
        encoded = {k: v.to(model.device) for k, v in encoded.items()}

        with torch.no_grad():
            output = model.generate(
                **encoded,
                do_sample=(safe_temperature > 0.0),
                temperature=max(0.05, safe_temperature),
                max_new_tokens=safe_max_new_tokens,
                pad_token_id=tokenizer.eos_token_id,
                eos_token_id=tokenizer.eos_token_id,
            )

        generated = tokenizer.decode(output[0][encoded["input_ids"].shape[-1] :], skip_special_tokens=True).strip()
        if not generated:
            generated = "I am ready. Please tell me what you want to do next."
        return model_id, generated


runtime = _ModelRuntime()
app = FastAPI(title="Jarvis Self-Hosted Model Service", version="1.1.0")


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "loaded_model": runtime._model_id or None,
        "lora_adapter": runtime._lora_adapter_name or None,
    }


@app.post("/v1/chat/completions")
def chat_completions(req: ChatCompletionsRequest) -> dict[str, Any]:
    if req.stream:
        raise HTTPException(status_code=400, detail="stream=true is not supported in this service")

    started = time.time()
    model_id, text = runtime.generate(req)
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"

    prompt_tokens = 0
    completion_tokens = max(1, len(text.split()))

    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model_id,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
        "latency_s": round(max(0.0, time.time() - started), 3),
    }


@app.post("/v1/load-lora-weights")
def load_lora_weights(req: LoRAWeightsRequest) -> dict[str, Any]:
    """Load LoRA adapter weights into the current model."""
    try:
        result = runtime.load_lora_adapter(req.adapter_name, merge=req.merge)
        return result
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Unexpected error loading LoRA weights: {exc}")
        raise HTTPException(status_code=500, detail=f"Internal error: {str(exc)}") from exc


if __name__ == "__main__":
    import uvicorn

    # Setup logging
    logging.basicConfig(level=logging.INFO)

    uvicorn.run(
        "apps.model_service.server:app",
        host=os.getenv("MODEL_SERVICE_HOST", "0.0.0.0"),
        port=int(os.getenv("MODEL_SERVICE_PORT", "8010")),
        reload=False,
    )
