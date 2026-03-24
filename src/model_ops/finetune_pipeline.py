from __future__ import annotations

from pathlib import Path
from typing import Any

from .compatibility import is_model_compatible_with_finetune
from .finetune_config import get_finetune_profile, load_finetune_profiles
from .finetune_dataset_checker import inspect_dataset
from .model_catalog import get_model
from .training_readiness import compute_readiness
from .utils import DATA_DIR, ROOT_DIR, now_iso, save_json
from .export.jsonl_exporter import export_instruction_jsonl, export_conversation_jsonl
from .export.lora_exporter import export_lora_dataset
from .export.rag_exporter import export_rag_docs
from .export.eval_exporter import export_eval_samples


def _default_profile_from_readiness(readiness: dict[str, Any]) -> str:
    t = str(readiness.get("recommended_finetune_type") or "").lower()
    if "lora" in t:
        return "lightweight_lora_chat"
    if "task" in t:
        return "task_reasoning_tuning"
    if "conversation" in t:
        return "response_style_tuning"
    return "instruction_tuning_general"


def prepare_finetune_run(
    *,
    profile_name: str | None = None,
    target_model_id: str | None = None,
    dataset_dir: str | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    ds_dir = Path(dataset_dir) if dataset_dir else (ROOT_DIR / "data" / "ai_training" / "datasets")
    stats = inspect_dataset(str(ds_dir))

    model = get_model(target_model_id or "ollama_llama3_1_8b") or {"model_id": target_model_id or "ollama_llama3_1_8b"}
    readiness = compute_readiness(stats, model_supports_finetune=bool(model.get("supports_instruction_tuning", True)))

    chosen_profile = profile_name or _default_profile_from_readiness(readiness)
    profile = get_finetune_profile(chosen_profile)
    if not profile:
        all_profiles = load_finetune_profiles().get("profiles", {})
        if isinstance(all_profiles, dict) and all_profiles:
            chosen_profile = next(iter(all_profiles.keys()))
            profile = all_profiles[chosen_profile]

    ok, reasons = is_model_compatible_with_finetune(model, profile or {})

    run_id = now_iso().replace(":", "-").replace(".", "-")
    run_dir = DATA_DIR / "exports" / f"finetune_run_{run_id}"
    run_dir.mkdir(parents=True, exist_ok=True)

    exports: dict[str, Any] = {}
    if not dry_run:
        instruction_in = ds_dir / "instruction_dataset.jsonl"
        conv_in = ds_dir / "conversation_dataset.jsonl"
        task_in = ds_dir / "task_dataset.jsonl"
        err_in = ds_dir / "error_dataset.jsonl"

        exports["instruction_jsonl"] = export_instruction_jsonl(instruction_in, run_dir / "instruction.jsonl")
        exports["conversation_jsonl"] = export_conversation_jsonl(conv_in, run_dir / "conversation.jsonl")
        exports["lora"] = export_lora_dataset(instruction_in, task_in, run_dir / "lora_dataset.jsonl")
        exports["rag_docs"] = export_rag_docs(conv_in, task_in, run_dir / "rag_docs.jsonl")
        exports["eval"] = export_eval_samples(instruction_in, err_in, run_dir / "eval_samples.jsonl")

    manifest = {
        "run_id": run_id,
        "created_at": now_iso(),
        "dry_run": bool(dry_run),
        "dataset_dir": str(ds_dir),
        "target_model_id": str(model.get("model_id") or target_model_id or ""),
        "selected_profile": chosen_profile,
        "profile": profile,
        "readiness": readiness,
        "compatibility": {
            "compatible": ok,
            "reasons": reasons,
        },
        "exports": exports,
        "reproducible_config": {
            "profile_name": chosen_profile,
            "target_model_id": str(model.get("model_id") or ""),
            "dataset_stats": stats,
        },
        "candidate_artifact": {
            "artifact_name": f"candidate_{str(model.get('model_id') or 'model')}_{run_id}",
            "artifact_dir": str(run_dir / "artifact"),
            "status": "registered",
        },
    }

    save_json(run_dir / "manifest.json", manifest)
    return manifest
