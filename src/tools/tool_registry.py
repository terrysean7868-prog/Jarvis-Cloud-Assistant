from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

from src.tools.plugin_loader import PluginLoader
from src.tools.builtin.system_health_tool import SystemHealthTool


class ToolRegistry:
    """Dynamic plugin loader for tools inside src/tools."""

    def __init__(self, tools_dir: Path | None = None):
        self.tools_dir = tools_dir or (Path(__file__).resolve().parent)
        self.plugin_loader = PluginLoader()
        self._tools: dict[str, Any] = {}
        self._module_cache: dict[str, ModuleType] = {}
        self._lock = asyncio.Lock()
        self.register_tool(SystemHealthTool())

    def register_tool(self, tool: Any) -> None:
        name = str(getattr(tool, "name", "")).strip()
        if not name:
            return
        self._tools[name] = tool

    async def discover_tools(self) -> dict[str, Any]:
        async with self._lock:
            for file_path in self.tools_dir.rglob("*.py"):
                rel = file_path.relative_to(self.tools_dir).as_posix()
                if rel in {"base_tool.py", "tool_registry.py", "__init__.py"}:
                    continue
                if rel.startswith("builtin/"):
                    continue
                if "__pycache__" in rel or rel.startswith("generated/__pycache__"):
                    continue

                module_name = f"jarvis_tool_{rel.replace('/', '_').replace('.py', '')}"
                try:
                    spec = importlib.util.spec_from_file_location(module_name, file_path)
                    if spec is None or spec.loader is None:
                        continue
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                    self._module_cache[module_name] = module
                except Exception:
                    continue

                tool_obj = getattr(module, "tool", None)
                if tool_obj is not None:
                    self.register_tool(tool_obj)
                    continue

                tool_cls = getattr(module, "Tool", None)
                if tool_cls is not None:
                    try:
                        self.register_tool(tool_cls())
                    except Exception:
                        continue

            # Load external plugins after internal tools to allow additive extension.
            for plugin_tool in self.plugin_loader.discover():
                self.register_tool(plugin_tool)

        return self._tools

    async def reload(self) -> dict[str, Any]:
        async with self._lock:
            keep_builtin = {k: v for k, v in self._tools.items() if getattr(v, "__class__", object).__name__ == "SystemHealthTool"}
            self._tools = keep_builtin
            self._module_cache = {}
        return await self.discover_tools()

    def list_tools(self) -> list[dict[str, str]]:
        return [
            {"name": name, "description": str(getattr(tool, "description", ""))}
            for name, tool in sorted(self._tools.items(), key=lambda kv: kv[0])
        ]

    async def run_tool(self, name: str, **kwargs: Any) -> dict[str, Any]:
        tool = self._tools.get(name)
        if tool is None:
            return {"status": "error", "message": f"Tool not found: {name}"}
        run = getattr(tool, "run", None)
        if run is None:
            return {"status": "error", "message": f"Tool has no run(): {name}"}
        if asyncio.iscoroutinefunction(run):
            return await run(**kwargs)
        return run(**kwargs)
