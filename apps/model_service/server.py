from __future__ import annotations

import os
import time
import uuid
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionsRequest(BaseModel):
    model: str | None = None
    messages: list[ChatMessage] = Field(default_factory=list)
    temperature: float = 0.4
    max_tokens: int = 512
    stream: bool = False


class _ModelRuntime:
    def __init__(self) -> None:
        self._tokenizer = None
        self._model = None
        self._model_id = ""

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
            import torch
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
        return model_id

    def generate(self, req: ChatCompletionsRequest) -> tuple[str, str]:
        model_id = self.ensure_loaded(req.model)

        try:
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
app = FastAPI(title="Jarvis Self-Hosted Model Service", version="1.0.0")


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "loaded_model": runtime._model_id or None}


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


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "apps.model_service.server:app",
        host=os.getenv("MODEL_SERVICE_HOST", "0.0.0.0"),
        port=int(os.getenv("MODEL_SERVICE_PORT", "8010")),
        reload=False,
    )
