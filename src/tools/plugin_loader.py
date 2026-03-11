from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any


class PluginLoader:
    """Loads external plugin tools from a dedicated plugins directory."""

    def __init__(self, plugins_dir: Path | None = None):
        self.plugins_dir = plugins_dir or (Path(__file__).resolve().parents[2] / "plugins")
        self._loaded_modules: dict[str, ModuleType] = {}

    def discover(self) -> list[Any]:
        tools: list[Any] = []
        if not self.plugins_dir.exists():
            return tools

        for py in self.plugins_dir.glob("*.py"):
            if py.name.startswith("_"):
                continue

            module_name = f"jarvis_plugin_{py.stem}"
            try:
                spec = importlib.util.spec_from_file_location(module_name, py)
                if spec is None or spec.loader is None:
                    continue
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                self._loaded_modules[module_name] = mod
            except Exception:
                continue

            obj = getattr(mod, "tool", None)
            if obj is not None:
                tools.append(obj)
                continue

            cls = getattr(mod, "Tool", None)
            if cls is not None:
                try:
                    tools.append(cls())
                except Exception:
                    continue

        return tools
