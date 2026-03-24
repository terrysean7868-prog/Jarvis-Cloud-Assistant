from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import random
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.model_ops import (
    get_model,
    inspect_dataset,
    register_tuned_model,
    update_profile,
    update_profile_models,
)
from src.model_ops.model_registry import load_registry
from src.model_ops.runtime_router import resolve_route
from src.model_ops.utils import load_jsonl
from src.utils.db import db


ROOT = Path(__file__).resolve().parents[1]
DATASETS_DIR = ROOT / "data" / "ai_training" / "datasets"
TRAINING_DIR = ROOT / "data" / "model_ops" / "training"
RUNS_DIR = ROOT / "data" / "model_ops" / "runs"
REQUIRED_TRAIN_LIBS: tuple[str, ...] = ("torch", "transformers", "datasets", "peft")

HF_BASE_MODEL_MAP: dict[str, str] = {
    "ollama_llama3_1_8b": "meta-llama/Meta-Llama-3.1-8B-Instruct",
    "ollama_qwen2_5_7b": "Qwen/Qwen2.5-7B-Instruct",
}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _safe_text(v: Any) -> str:
    return str(v or "").strip()


def _norm_text(v: Any) -> str:
    return " ".join(_safe_text(v).lower().split())


def _dedupe_and_balance(rows_by_source: dict[str, list[dict[str, Any]]], *, target_total: int) -> list[dict[str, Any]]:
    deduped_by_source: dict[str, list[dict[str, Any]]] = {}
    global_seen: set[str] = set()

    for source, rows in rows_by_source.items():
        out: list[dict[str, Any]] = []
        for row in rows:
            prompt = _norm_text(row.get("prompt"))
            completion = _norm_text(row.get("completion"))
            if not prompt or not completion:
                continue
            key = f"{prompt}|{completion}"
            if key in global_seen:
                continue
            global_seen.add(key)
            out.append(row)
        deduped_by_source[source] = out

    sources = [s for s in ["instruction", "conversation", "task", "error"] if deduped_by_source.get(s)]
    if not sources:
        return []

    per_source = max(1, int(target_total / max(1, len(sources))))
    balanced: list[dict[str, Any]] = []
    for source in sources:
        rows = deduped_by_source.get(source, [])
        if len(rows) <= per_source:
            balanced.extend(rows)
        else:
            balanced.extend(random.sample(rows, per_source))

    # Keep balanced distribution even when one dataset is much larger.
    # If target_total exceeds balanced capacity, we intentionally keep the capped balanced set.
    balanced = balanced[: max(1, int(target_total))]

    random.shuffle(balanced)
    return balanced


def _rows_from_instruction(path: Path) -> list[dict[str, Any]]:
    rows = load_jsonl(path)
    out: list[dict[str, Any]] = []
    for r in rows:
        prompt = _safe_text(r.get("input") or r.get("prompt"))
        completion = _safe_text(r.get("expected_output") or r.get("completion"))
        if prompt and completion:
            out.append(
                {
                    "prompt": prompt,
                    "completion": completion,
                    "source": "instruction",
                    "type": _safe_text(r.get("type") or "instruction"),
                }
            )
    return out


def _rows_from_conversation(path: Path) -> list[dict[str, Any]]:
    rows = load_jsonl(path)
    out: list[dict[str, Any]] = []
    for r in rows:
        prompt = _safe_text(r.get("input"))
        completion = _safe_text(r.get("expected_output"))
        if not prompt:
            turns = r.get("turns") if isinstance(r.get("turns"), list) else []
            if turns:
                first_user = next((t for t in turns if isinstance(t, dict) and _safe_text(t.get("role")).lower() == "user"), None)
                first_assistant = next((t for t in turns if isinstance(t, dict) and _safe_text(t.get("role")).lower() == "assistant"), None)
                prompt = _safe_text((first_user or {}).get("text"))
                completion = _safe_text((first_assistant or {}).get("text"))
        if prompt and completion:
            out.append(
                {
                    "prompt": prompt,
                    "completion": completion,
                    "source": "conversation",
                    "type": "conversation",
                }
            )
    return out


def _rows_from_task(path: Path) -> list[dict[str, Any]]:
    rows = load_jsonl(path)
    out: list[dict[str, Any]] = []
    for r in rows:
        prompt = _safe_text(r.get("input") or r.get("task"))
        completion = _safe_text(r.get("expected_output") or r.get("result"))
        if prompt and completion:
            out.append(
                {
                    "prompt": prompt,
                    "completion": completion,
                    "source": "task",
                    "type": _safe_text(r.get("type") or "task"),
                }
            )
    return out


def _rows_from_error(path: Path) -> list[dict[str, Any]]:
    rows = load_jsonl(path)
    out: list[dict[str, Any]] = []
    for r in rows:
        prompt = _safe_text(r.get("input") or r.get("error"))
        completion = _safe_text(r.get("expected_output") or r.get("fix_suggestion"))
        if prompt and completion:
            out.append(
                {
                    "prompt": prompt,
                    "completion": completion,
                    "source": "error",
                    "type": _safe_text(r.get("type") or "error"),
                }
            )
    return out


def _split_train_eval(rows: list[dict[str, Any]], *, eval_ratio: float) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not rows:
        return [], []
    ratio = min(0.4, max(0.05, float(eval_ratio)))
    eval_count = max(1, int(len(rows) * ratio))
    eval_rows = rows[:eval_count]
    train_rows = rows[eval_count:]
    if not train_rows:
        train_rows = rows[:-1]
        eval_rows = rows[-1:]
    return train_rows, eval_rows


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _prepare_training_data(*, seed: int, target_total: int, eval_ratio: float) -> dict[str, Any]:
    random.seed(seed)
    instruction = _rows_from_instruction(DATASETS_DIR / "instruction_dataset.jsonl")
    conversation = _rows_from_conversation(DATASETS_DIR / "conversation_dataset.jsonl")
    task = _rows_from_task(DATASETS_DIR / "task_dataset.jsonl")
    error = _rows_from_error(DATASETS_DIR / "error_dataset.jsonl")

    merged = _dedupe_and_balance(
        {
            "instruction": instruction,
            "conversation": conversation,
            "task": task,
            "error": error,
        },
        target_total=max(200, int(target_total)),
    )

    train_rows, eval_rows = _split_train_eval(merged, eval_ratio=eval_ratio)

    train_jsonl = [
        {
            "instruction": r.get("prompt"),
            "input": "",
            "output": r.get("completion"),
            "source": r.get("source"),
            "sample_type": r.get("type"),
        }
        for r in train_rows
    ]
    eval_jsonl = [
        {
            "instruction": r.get("prompt"),
            "input": "",
            "output": r.get("completion"),
            "source": r.get("source"),
            "sample_type": r.get("type"),
        }
        for r in eval_rows
    ]

    TRAINING_DIR.mkdir(parents=True, exist_ok=True)
    _write_jsonl(TRAINING_DIR / "train.jsonl", train_jsonl)
    _write_jsonl(TRAINING_DIR / "eval.jsonl", eval_jsonl)

    source_counts = defaultdict(int)
    for r in merged:
        source_counts[str(r.get("source") or "unknown")] += 1

    dataset_version = f"lora_ds_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}"
    dataset_stats = inspect_dataset(str(DATASETS_DIR))
    meta = {
        "dataset_version": dataset_version,
        "created_at": _now_iso(),
        "target_total": int(target_total),
        "merged_count": len(merged),
        "train_count": len(train_jsonl),
        "eval_count": len(eval_jsonl),
        "source_counts": dict(source_counts),
        "dataset_stats": dataset_stats,
        "paths": {
            "train_jsonl": str(TRAINING_DIR / "train.jsonl"),
            "eval_jsonl": str(TRAINING_DIR / "eval.jsonl"),
        },
    }
    (TRAINING_DIR / "dataset_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return meta


def _validate_model_candidates() -> dict[str, Any]:
    primary_id = "ollama_llama3_1_8b"
    secondary_id = "ollama_qwen2_5_7b"

    primary = get_model(primary_id) or {}
    secondary = get_model(secondary_id) or {}

    def card(m: dict[str, Any], model_id: str) -> dict[str, Any]:
        mem = m.get("memory_requirements") if isinstance(m.get("memory_requirements"), dict) else {}
        supports_long = bool(m.get("supports_long_context", False))
        context_estimate = 8192 if supports_long else 2048
        return {
            "model_id": model_id,
            "supports_lora": bool(m.get("supports_lora", False)),
            "supports_instruction_tuning": bool(m.get("supports_instruction_tuning", False)),
            "context_length_estimate": context_estimate,
            "memory_requirements": {
                "min_ram_gb": int(mem.get("min_ram_gb") or 0),
                "vram_gb": int(mem.get("vram_gb") or 0),
            },
            "provider": _safe_text(m.get("provider_type") or "unknown"),
        }

    p = card(primary, primary_id)
    s = card(secondary, secondary_id)

    # Keep llama as the primary baseline candidate unless incompatible.
    base = primary_id
    fallback = secondary_id
    if not p["supports_lora"] and s["supports_lora"]:
        base, fallback = secondary_id, primary_id

    return {
        "primary_candidate": p,
        "secondary_candidate": s,
        "selected_base_model": base,
        "selected_fallback_model": fallback,
    }


def _resolve_hf_base_model(runtime_base_model: str, explicit_hf_base_model: str) -> str:
    explicit = _safe_text(explicit_hf_base_model)
    if explicit:
        return explicit
    runtime_id = _safe_text(runtime_base_model)
    return _safe_text(HF_BASE_MODEL_MAP.get(runtime_id) or runtime_id)


def _install_commands() -> list[str]:
    return [
        "C:/Users/avadh/Apps/Python/Setup/python.exe -m pip install --upgrade pip",
        "C:/Users/avadh/Apps/Python/Setup/python.exe -m pip install torch transformers datasets peft",
    ]


def _collect_lib_status() -> dict[str, bool]:
    out: dict[str, bool] = {}
    for name in ["transformers", "peft", "datasets", "torch", "bitsandbytes"]:
        out[name] = bool(importlib.util.find_spec(name))
    return out


def _train_or_dry_run(
    *,
    dry_run: bool,
    base_model: str,
    hf_base_model: str,
    output_dir: Path,
    learning_rate: float,
    batch_size: int,
    epochs: int,
    max_seq_length: int,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)

    # Optional stack check (free/open-source first).
    lib_status = _collect_lib_status()

    torch = None
    for name in ["torch"]:
        try:
            mod = __import__(name)
            if name == "torch":
                torch = mod
        except Exception:
            torch = None

    device = "cpu"
    gpu_available = False
    if torch is not None:
        try:
            gpu_available = bool(torch.cuda.is_available())
            device = "cuda" if gpu_available else "cpu"
        except Exception:
            device = "cpu"

    train_config = {
        "base_model": base_model,
        "hf_base_model": hf_base_model,
        "learning_rate": float(learning_rate),
        "batch_size": int(batch_size),
        "epochs": int(epochs),
        "max_seq_length": int(max_seq_length),
        "device": device,
        "gpu_available": gpu_available,
        "cpu_safe_mode": not gpu_available,
        "quantization_backend": "bitsandbytes" if (gpu_available and bool(lib_status.get("bitsandbytes"))) else "none_cpu_safe",
        "dry_run": bool(dry_run),
        "libs": lib_status,
        "install_commands": _install_commands(),
    }

    (output_dir / "training_config.json").write_text(json.dumps(train_config, indent=2), encoding="utf-8")

    if dry_run:
        return {
            "status": "dry_run",
            "message": "Dry-run only. No training executed.",
            "config": train_config,
            "adapter_path": None,
            "cpu_safe_ready": all(bool(lib_status.get(k)) for k in REQUIRED_TRAIN_LIBS),
        }

    if not all(bool(lib_status.get(k)) for k in REQUIRED_TRAIN_LIBS):
        missing = [k for k in REQUIRED_TRAIN_LIBS if not bool(lib_status.get(k))]
        return {
            "status": "blocked",
            "message": f"Missing required training libs: {', '.join(missing)}.",
            "config": train_config,
            "adapter_path": None,
            "install_commands": _install_commands(),
            "cpu_safe_ready": False,
        }

    try:
        _torch = importlib.import_module("torch")
        _datasets = importlib.import_module("datasets")
        _transformers = importlib.import_module("transformers")
        _peft = importlib.import_module("peft")
    except Exception as e:
        return {
            "status": "blocked",
            "message": f"Training imports unavailable: {e}",
            "config": train_config,
            "adapter_path": None,
        }

    train_path = TRAINING_DIR / "train.jsonl"
    eval_path = TRAINING_DIR / "eval.jsonl"
    if not train_path.exists() or not eval_path.exists():
        return {
            "status": "blocked",
            "message": "Training dataset files not found. Run dataset preparation first.",
            "config": train_config,
            "adapter_path": None,
        }

    ds = _datasets.load_dataset("json", data_files={"train": str(train_path), "eval": str(eval_path)})
    tokenizer = _transformers.AutoTokenizer.from_pretrained(hf_base_model, use_fast=True)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    def _format_and_tokenize(batch: dict[str, list[Any]]) -> dict[str, list[Any]]:
        texts: list[str] = []
        instr = batch.get("instruction") or []
        outputs = batch.get("output") or []
        for i, o in zip(instr, outputs):
            text = f"### Instruction:\n{_safe_text(i)}\n\n### Response:\n{_safe_text(o)}"
            texts.append(text)
        tok = tokenizer(texts, truncation=True, max_length=int(max_seq_length), padding="max_length")
        tok["labels"] = [list(x) for x in tok["input_ids"]]
        return tok

    tokenized_train = ds["train"].map(_format_and_tokenize, batched=True, remove_columns=ds["train"].column_names)
    tokenized_eval = ds["eval"].map(_format_and_tokenize, batched=True, remove_columns=ds["eval"].column_names)

    torch_dtype = _torch.float16 if bool(_torch.cuda.is_available()) else _torch.float32
    model = _transformers.AutoModelForCausalLM.from_pretrained(hf_base_model, torch_dtype=torch_dtype)
    lora_cfg = _peft.LoraConfig(
        r=8,
        lora_alpha=16,
        lora_dropout=0.05,
        bias="none",
        task_type="CAUSAL_LM",
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "up_proj", "down_proj", "gate_proj"],
    )
    model = _peft.get_peft_model(model, lora_cfg)

    adapter_path = output_dir / "adapter"
    args = _transformers.TrainingArguments(
        output_dir=str(output_dir / "hf_output"),
        learning_rate=float(learning_rate),
        per_device_train_batch_size=max(1, int(batch_size)),
        per_device_eval_batch_size=max(1, int(batch_size)),
        num_train_epochs=max(1, int(epochs)),
        evaluation_strategy="epoch",
        save_strategy="epoch",
        logging_steps=20,
        gradient_accumulation_steps=1,
        report_to=[],
        fp16=bool(_torch.cuda.is_available()),
        no_cuda=not bool(_torch.cuda.is_available()),
        dataloader_pin_memory=bool(_torch.cuda.is_available()),
    )
    trainer = _transformers.Trainer(
        model=model,
        args=args,
        train_dataset=tokenized_train,
        eval_dataset=tokenized_eval,
        tokenizer=tokenizer,
        data_collator=_transformers.DataCollatorForLanguageModeling(tokenizer=tokenizer, mlm=False),
    )
    trainer.train()
    trainer.save_model(str(adapter_path))

    return {
        "status": "trained",
        "message": "LoRA training completed in CPU-safe mode." if not bool(_torch.cuda.is_available()) else "LoRA training completed.",
        "config": train_config,
        "adapter_path": str(adapter_path),
        "cpu_safe_ready": True,
    }


def _benchmark_base_vs_tuned(base_model: str, tuned_model: str) -> dict[str, Any]:
    db._ensure_connected()
    rows = []
    if db.db is not None:
        rows = list(
            db.db["model_performance_stats"]
            .find({}, {"_id": 0, "model_id": 1, "success": 1, "latency_ms": 1, "fallback_used": 1})
            .sort("recorded_at", -1)
            .limit(2000)
        )

    by_model: dict[str, dict[str, Any]] = {}
    for r in rows:
        model_id = _safe_text(r.get("model_id") or "unknown")
        it = by_model.setdefault(model_id, {"calls": 0, "success": 0, "latency_total": 0.0, "fallback": 0})
        it["calls"] += 1
        it["success"] += 1 if bool(r.get("success")) else 0
        it["latency_total"] += float(r.get("latency_ms") or 0.0)
        it["fallback"] += 1 if bool(r.get("fallback_used")) else 0

    def metric(mid: str) -> dict[str, Any]:
        stat = by_model.get(mid, {"calls": 0, "success": 0, "latency_total": 0.0, "fallback": 0})
        calls = max(1, int(stat.get("calls") or 0))
        if int(stat.get("calls") or 0) == 0:
            return {
                "model_id": mid,
                "calls": 0,
                "accuracy": None,
                "latency_ms": None,
                "fallback_rate": None,
                "user_query_success": None,
            }
        success_rate = float(stat.get("success") or 0) / calls
        return {
            "model_id": mid,
            "calls": int(stat.get("calls") or 0),
            "accuracy": round(success_rate, 4),
            "latency_ms": round(float(stat.get("latency_total") or 0.0) / calls, 3),
            "fallback_rate": round(float(stat.get("fallback") or 0) / calls, 4),
            "user_query_success": round(success_rate, 4),
        }

    return {
        "base_model": metric(base_model),
        "tuned_model": metric(tuned_model),
    }


def run_pipeline(args: argparse.Namespace) -> dict[str, Any]:
    model_validation = _validate_model_candidates()
    base_model = _safe_text(args.base_model) or _safe_text(model_validation.get("selected_base_model"))
    fallback_model = _safe_text(args.fallback_model) or _safe_text(model_validation.get("selected_fallback_model"))
    hf_base_model = _resolve_hf_base_model(base_model, _safe_text(args.hf_base_model))

    dataset_meta = _prepare_training_data(
        seed=int(args.seed),
        target_total=int(args.target_total_samples),
        eval_ratio=float(args.eval_ratio),
    )

    run_id = datetime.now(UTC).strftime("lora_%Y%m%d_%H%M%S")
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    training = _train_or_dry_run(
        dry_run=bool(args.dry_run),
        base_model=base_model,
        hf_base_model=hf_base_model,
        output_dir=run_dir,
        learning_rate=float(args.learning_rate),
        batch_size=int(args.batch_size),
        epochs=int(args.epochs),
        max_seq_length=int(args.max_seq_length),
    )

    tuned_model_id = _safe_text(args.tuned_model_id) or f"{base_model}_lora_{run_id}"

    registry_before = load_registry()
    profile_name = _safe_text(args.profile_name) or _safe_text(registry_before.get("active_profile") or "local_primary_api_backup")

    registration = {
        "registry_updated": False,
        "preferred_tuned_model": registry_before.get("preferred_tuned_model"),
        "dataset_version": dataset_meta.get("dataset_version"),
        "profile_updated": None,
        "apply_runtime": bool(args.apply_runtime),
    }

    if bool(args.apply_runtime):
        reg = register_tuned_model(
            tuned_model_id=tuned_model_id,
            base_model_id=base_model,
            dataset_version=_safe_text(dataset_meta.get("dataset_version")),
            training_run_id=run_id,
            training_manifest_path=str(run_dir / "training_config.json"),
            preferred=bool(args.prefer_tuned_model),
            metadata={
                "dry_run": bool(args.dry_run),
                "train_path": str(TRAINING_DIR / "train.jsonl"),
                "eval_path": str(TRAINING_DIR / "eval.jsonl"),
            },
        )

        update_profile_models(
            profile_name,
            primary_chat_model=tuned_model_id,
            code_debug_model=tuned_model_id,
            fallback_model=fallback_model,
        )
        update_profile(
            profile_name,
            {
                "primary": tuned_model_id,
                "code_debug": tuned_model_id,
                "fallback": fallback_model,
            },
        )
        registration = {
            "registry_updated": True,
            "preferred_tuned_model": reg.get("preferred_tuned_model"),
            "dataset_version": dataset_meta.get("dataset_version"),
            "profile_updated": profile_name,
            "apply_runtime": True,
        }

    route_chat = resolve_route("hello jarvis", mode="chat", profile_name=profile_name)
    route_debug = resolve_route("debug this traceback please", mode="chat", profile_name=profile_name)

    benchmark = _benchmark_base_vs_tuned(base_model=base_model, tuned_model=tuned_model_id)

    out = {
        "status": "success",
        "created_at": _now_iso(),
        "run_id": run_id,
        "model_selection": {
            **model_validation,
            "base_model": base_model,
            "hf_base_model": hf_base_model,
            "fallback_model": fallback_model,
            "tuned_model_id": tuned_model_id,
        },
        "dataset": dataset_meta,
        "training": training,
        "registration": registration,
        "runtime_validation": {
            "profile": profile_name,
            "chat_primary_model": ((route_chat.get("primary") or {}).get("model_id") if isinstance(route_chat.get("primary"), dict) else None),
            "debug_primary_model": ((route_debug.get("primary") or {}).get("model_id") if isinstance(route_debug.get("primary"), dict) else None),
            "fallback_model": ((route_chat.get("fallback") or {}).get("model_id") if isinstance(route_chat.get("fallback"), dict) else None),
            "fallback_safe": bool(((route_chat.get("fallback") or {}).get("model_id") if isinstance(route_chat.get("fallback"), dict) else None)),
        },
        "benchmark": benchmark,
    }

    (run_dir / "pipeline_report.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    return out


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Controlled LoRA preparation and optional training pipeline.")
    p.add_argument("--dry-run", action=argparse.BooleanOptionalAction, default=True, help="Enable dry-run mode (default: true).")
    p.add_argument("--base-model", default="", help="Base model id for tuning.")
    p.add_argument("--hf-base-model", default="", help="HF model repo/path used for actual training (defaults from base model mapping).")
    p.add_argument("--fallback-model", default="", help="Fallback model id to keep in runtime.")
    p.add_argument("--tuned-model-id", default="", help="Explicit tuned model id to register.")
    p.add_argument("--profile-name", default="", help="Deployment profile to update.")
    p.add_argument("--apply-runtime", action=argparse.BooleanOptionalAction, default=False, help="Apply tuned model to registry/profile runtime routing.")
    p.add_argument("--prefer-tuned-model", action=argparse.BooleanOptionalAction, default=True, help="When applying runtime, set tuned model as preferred model.")

    p.add_argument("--learning-rate", type=float, default=2e-4)
    p.add_argument("--batch-size", type=int, default=2)
    p.add_argument("--epochs", type=int, default=2)
    p.add_argument("--max-seq-length", type=int, default=2048)

    p.add_argument("--target-total-samples", type=int, default=2000)
    p.add_argument("--eval-ratio", type=float, default=0.1)
    p.add_argument("--seed", type=int, default=42)
    return p


if __name__ == "__main__":
    parser = build_parser()
    args = parser.parse_args()
    report = run_pipeline(args)
    print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
