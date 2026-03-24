from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RUNS_DIR = ROOT / "data" / "model_ops" / "runs"


def _latest_models() -> tuple[str, str]:
    tuned = "<tuned_model_id_after_training>"
    base = "ollama_llama3_1_8b"
    try:
        runs = sorted([p for p in RUNS_DIR.iterdir() if p.is_dir()], key=lambda p: p.stat().st_mtime, reverse=True)
        if not runs:
            return base, tuned
        report = runs[0] / "pipeline_report.json"
        if not report.exists():
            return base, tuned
        data = json.loads(report.read_text(encoding="utf-8"))
        model_sel = data.get("model_selection") if isinstance(data, dict) else {}
        if isinstance(model_sel, dict):
            base = str(model_sel.get("base_model") or base)
            tuned = str(model_sel.get("tuned_model_id") or tuned)
    except Exception:
        return base, tuned
    return base, tuned


def run() -> dict[str, Any]:
    base, tuned = _latest_models()
    install_cmds = [
        "C:/Users/avadh/Apps/Python/Setup/python.exe -m pip install --upgrade pip",
        "C:/Users/avadh/Apps/Python/Setup/python.exe -m pip install torch transformers datasets peft",
    ]
    dry_run_cmd = (
        "C:/Users/avadh/Apps/Python/Setup/python.exe scripts/train_lora.py "
        "--dry-run --target-total-samples 1200 --eval-ratio 0.12"
    )
    real_train_cmd = (
        "C:/Users/avadh/Apps/Python/Setup/python.exe scripts/train_lora.py "
        "--no-dry-run --no-apply-runtime "
        "--base-model ollama_llama3_1_8b "
        "--hf-base-model meta-llama/Meta-Llama-3.1-8B-Instruct "
        "--fallback-model ollama_qwen2_5_7b "
        "--target-total-samples 1200 --eval-ratio 0.12 "
        "--learning-rate 0.0002 --batch-size 1 --epochs 3 --max-seq-length 1024"
    )
    post_val_cmd = (
        "C:/Users/avadh/Apps/Python/Setup/python.exe scripts/validate_lora_post_training.py "
        f"--base-model {base} --tuned-model {tuned} "
        "--profile-name local_primary_api_backup --min-calls 8"
    )
    return {
        "install_commands": install_cmds,
        "dry_run_command": dry_run_cmd,
        "real_training_command": real_train_cmd,
        "post_training_validation_command": post_val_cmd,
    }


if __name__ == "__main__":
    out = run()
    print("Install commands:")
    for cmd in out["install_commands"]:
        print(f"- {cmd}")
    print("\nDry-run command:")
    print(out["dry_run_command"])
    print("\nReal training command:")
    print(out["real_training_command"])
    print("\nPost-training validation command:")
    print(out["post_training_validation_command"])
