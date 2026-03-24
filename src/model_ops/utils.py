from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT_DIR / "data" / "model_ops"
CONFIG_DIR = ROOT_DIR / "config" / "model_ops"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def load_json(path: Path, default: Any = None) -> Any:
    try:
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, data: Any) -> None:
    ensure_parent(path)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    ensure_parent(path)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        if not path.exists():
            return rows
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    rows.append(obj)
            except Exception:
                continue
    except Exception:
        return []
    return rows


def as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def stable_key(parts: list[Any]) -> str:
    return "|".join([str(p or "").strip().lower() for p in parts])


def mask_sensitive(text: str) -> str:
    t = str(text or "")
    t = re.sub(r"(?i)\b(authorization|token|api[_-]?key|secret|password)\b\s*[:=]\s*[^\s,;]+", r"\1=[REDACTED]", t)
    t = re.sub(r"(?i)bearer\s+[A-Za-z0-9\-\._~\+/=]+", "Bearer [REDACTED]", t)
    t = re.sub(r"\bsk-[A-Za-z0-9]{12,}\b", "sk-[REDACTED]", t)
    return t


def sanitize_obj(obj: Any) -> Any:
    if obj is None:
        return None
    if isinstance(obj, (int, float, bool)):
        return obj
    if isinstance(obj, str):
        return mask_sensitive(obj)
    if isinstance(obj, list):
        return [sanitize_obj(x) for x in obj[:200]]
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in list(obj.items())[:300]:
            key = str(k)
            if re.search(r"(?i)(token|secret|password|api[_-]?key|authorization)", key):
                out[key] = "[REDACTED]"
            else:
                out[key] = sanitize_obj(v)
        return out
    return mask_sensitive(str(obj))


def env_str(name: str, default: str = "") -> str:
    return str(os.getenv(name, default) or default)


def env_int(name: str, default: int) -> int:
    return as_int(os.getenv(name), default)


def env_bool(name: str, default: bool = False) -> bool:
    return as_bool(os.getenv(name), default)
