from __future__ import annotations

from typing import Any


class Tool:
    name = "plugin_docker"
    description = "Creates Docker run/build plans for local deployment." 

    async def run(self, **kwargs: Any) -> dict[str, Any]:
        image = str(kwargs.get("image") or "jarvis-autonomy-local").strip()
        mode = str(kwargs.get("mode") or "build").strip().lower()
        cmds = [f"docker build -t {image} ."] if mode == "build" else [f"docker run --rm {image}"]
        return {"status": "success", "mode": mode, "commands": cmds}


tool = Tool()
