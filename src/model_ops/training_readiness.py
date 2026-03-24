from __future__ import annotations

from typing import Any


def compute_readiness(dataset_stats: dict[str, Any], *, model_supports_finetune: bool = True) -> dict[str, Any]:
    total = int(dataset_stats.get("total_samples") or 0)
    instruction = int(dataset_stats.get("instruction_samples") or 0)
    conversation = int(dataset_stats.get("conversation_samples") or 0)
    task = int(dataset_stats.get("task_samples") or 0)
    error = int(dataset_stats.get("error_samples") or 0)
    duplicate_rate = float(dataset_stats.get("duplicate_rate") or 0.0)
    masked_ok = bool(dataset_stats.get("masked_sensitive", False))

    score = 0.0
    missing: list[str] = []
    suggestions: list[str] = []

    if total >= 200:
        score += 25
    elif total >= 80:
        score += 15
    else:
        missing.append("dataset_size")
        suggestions.append("Collect more runtime examples before tuning")

    if instruction >= 40:
        score += 15
    else:
        missing.append("instruction_coverage")

    if conversation >= 30:
        score += 12
    else:
        missing.append("conversation_coverage")

    if task >= 25:
        score += 15
    else:
        missing.append("task_delegation_coverage")

    if error >= 20:
        score += 10
    else:
        missing.append("error_fix_coverage")

    if duplicate_rate <= 0.2:
        score += 10
    else:
        missing.append("duplicate_cleanup")
        suggestions.append("Deduplicate repeated command/response pairs")

    if masked_ok:
        score += 8
    else:
        missing.append("sensitive_data_masking")
        suggestions.append("Mask tokens/secrets before export")

    if model_supports_finetune:
        score += 5
    else:
        missing.append("model_compatibility")

    ready = score >= 65 and len([m for m in missing if m in {"dataset_size", "sensitive_data_masking", "model_compatibility"}]) == 0

    recommended_type = "none"
    if not ready:
        if instruction > conversation and instruction > task:
            recommended_type = "instruction_tuning"
        elif task >= instruction and task >= conversation:
            recommended_type = "task_reasoning_tuning"
        elif conversation >= instruction:
            recommended_type = "conversation_tuning"
    else:
        recommended_type = "LoRA" if total < 3000 else "instruction_tuning"

    return {
        "ready": ready,
        "readiness_score": round(score, 2),
        "missing_data_categories": sorted(set(missing)),
        "dataset_cleanup_suggestions": suggestions,
        "recommended_finetune_type": recommended_type,
        "stats": dataset_stats,
    }
