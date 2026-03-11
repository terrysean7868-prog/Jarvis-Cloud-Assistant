from __future__ import annotations

from pathlib import Path
from typing import Any

from src.config.settings import settings


class ToolGenerator:
    """Generates new tool modules for capability extension in local-safe mode."""

    def __init__(self, repo_root: Path | None = None):
        self.repo_root = repo_root or Path(__file__).resolve().parents[2]

    def generate_tool(self, *, name: str, description: str) -> dict[str, Any]:
        if settings.cloud_mode:
            return {
                "status": "blocked",
                "message": "Tool generation is disabled in cloud mode.",
            }

        slug = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in (name or "tool").lower()).strip("_") or "tool"
        tools_dir = self.repo_root / "src" / "tools" / "generated"
        tools_dir.mkdir(parents=True, exist_ok=True)
        path = tools_dir / f"{slug}.py"

        code = (
            "from __future__ import annotations\n\n"
            "from typing import Any\n\n"
            "class Tool:\n"
            f"    name = \"{slug}\"\n"
            f"    description = \"{description or 'Generated tool'}\"\n\n"
            "    async def run(self, **kwargs: Any) -> dict[str, Any]:\n"
            "        return {\"status\": \"success\", \"tool\": self.name, \"args\": kwargs}\n\n"
            "tool = Tool()\n"
        )
        path.write_text(code, encoding="utf-8")
        return {"status": "success", "path": str(path), "tool": slug}
