from __future__ import annotations

import importlib.util
import json
import sys
from typing import Any

REQUIRED = ("torch", "transformers", "datasets", "peft")


def _is_installed(name: str) -> bool:
    return bool(importlib.util.find_spec(name))


def _gpu_available() -> tuple[bool, str | None]:
    if not _is_installed("torch"):
        return False, "torch_not_installed"
    try:
        import torch  # type: ignore

        return bool(torch.cuda.is_available()), None
    except Exception as e:
        return False, str(e)


def run() -> dict[str, Any]:
    libs = {name: _is_installed(name) for name in ["torch", "transformers", "datasets", "peft", "bitsandbytes"]}
    gpu, gpu_error = _gpu_available()

    cpu_ready = all(bool(libs.get(name)) for name in REQUIRED)

    return {
        "python_version": sys.version.split()[0],
        "torch_installed": bool(libs.get("torch")),
        "transformers_installed": bool(libs.get("transformers")),
        "datasets_installed": bool(libs.get("datasets")),
        "peft_installed": bool(libs.get("peft")),
        "bitsandbytes_installed": bool(libs.get("bitsandbytes")),
        "gpu_available": bool(gpu),
        "gpu_probe_error": gpu_error,
        "cpu_safe_training_ready": bool(cpu_ready),
        "missing_required": [name for name in REQUIRED if not bool(libs.get(name))],
        "install_commands": [
            "C:/Users/avadh/Apps/Python/Setup/python.exe -m pip install --upgrade pip",
            "C:/Users/avadh/Apps/Python/Setup/python.exe -m pip install torch transformers datasets peft",
        ],
    }


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
