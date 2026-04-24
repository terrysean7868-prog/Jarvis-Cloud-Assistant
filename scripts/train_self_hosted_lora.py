from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def _read_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except Exception:
                continue
            if isinstance(item, dict):
                rows.append(item)
    return rows


def _to_text_sample(row: dict) -> str:
    user = str(row.get("user") or row.get("input") or row.get("prompt") or "").strip()
    assistant = str(row.get("assistant") or row.get("output") or row.get("response") or "").strip()
    if not user or not assistant:
        return ""
    return f"<|user|>\n{user}\n<|assistant|>\n{assistant}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Train LoRA adapter for self-hosted Jarvis model")
    parser.add_argument("--dataset", default="data/ai_training/sft.jsonl", help="Path to JSONL dataset")
    parser.add_argument("--base-model", default="Qwen/Qwen2.5-7B-Instruct", help="Base model")
    parser.add_argument("--output-dir", default="models/jarvis-lora", help="Output LoRA directory")
    parser.add_argument("--epochs", type=int, default=1)
    parser.add_argument("--batch-size", type=int, default=1)
    args = parser.parse_args()

    dataset_path = Path(args.dataset)
    if not dataset_path.exists():
        print(f"Dataset not found: {dataset_path}")
        print("Create JSONL rows with keys like user/assistant or input/output.")
        return 1

    try:
        import torch
        from datasets import Dataset
        from transformers import AutoModelForCausalLM, AutoTokenizer, TrainingArguments, Trainer
        from peft import LoraConfig, get_peft_model, TaskType
    except Exception:
        print("Missing training dependencies. Install:")
        print("pip install -r requirements/model_service.txt")
        return 1

    rows = _read_jsonl(dataset_path)
    texts = [_to_text_sample(r) for r in rows]
    texts = [t for t in texts if t]
    if len(texts) < 10:
        print("Need at least 10 valid rows for useful LoRA training.")
        return 1

    ds = Dataset.from_dict({"text": texts})

    tokenizer = AutoTokenizer.from_pretrained(args.base_model, trust_remote_code=True)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    model = AutoModelForCausalLM.from_pretrained(
        args.base_model,
        torch_dtype=torch.float16 if torch.cuda.is_available() else torch.float32,
        device_map="auto",
        trust_remote_code=True,
    )

    lora_cfg = LoraConfig(
        task_type=TaskType.CAUSAL_LM,
        r=16,
        lora_alpha=32,
        lora_dropout=0.05,
        bias="none",
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj"],
    )
    model = get_peft_model(model, lora_cfg)

    def tokenize_batch(batch: dict) -> dict:
        tok = tokenizer(
            batch["text"],
            truncation=True,
            padding="max_length",
            max_length=1024,
        )
        tok["labels"] = tok["input_ids"].copy()
        return tok

    tokenized = ds.map(tokenize_batch, batched=True, remove_columns=["text"])

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    train_args = TrainingArguments(
        output_dir=str(out_dir),
        learning_rate=2e-4,
        num_train_epochs=max(1, int(args.epochs)),
        per_device_train_batch_size=max(1, int(args.batch_size)),
        gradient_accumulation_steps=8,
        warmup_ratio=0.03,
        logging_steps=10,
        save_steps=100,
        save_total_limit=2,
        bf16=torch.cuda.is_available(),
        fp16=torch.cuda.is_available(),
        report_to=[],
    )

    trainer = Trainer(
        model=model,
        args=train_args,
        train_dataset=tokenized,
    )

    trainer.train()
    model.save_pretrained(str(out_dir))
    tokenizer.save_pretrained(str(out_dir))

    print(f"LoRA adapter saved to: {out_dir}")
    print("Set SELF_HOSTED_LLM_MODEL to the base model and mount/apply this adapter in your model service.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
