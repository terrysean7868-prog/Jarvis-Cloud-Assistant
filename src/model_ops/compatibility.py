from __future__ import annotations

from typing import Any


def is_model_compatible_with_finetune(model: dict[str, Any], profile: dict[str, Any]) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    fmt = str(profile.get("export_format") or "jsonl").lower()

    if fmt == "lora" and not bool(model.get("supports_lora", False)):
        reasons.append("model_does_not_support_lora")

    if fmt in {"jsonl", "instruction"} and not bool(model.get("supports_instruction_tuning", False)):
        reasons.append("model_does_not_support_instruction_tuning")

    return (len(reasons) == 0, reasons)
