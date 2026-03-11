from __future__ import annotations

import os
import platform
from typing import Any

import psutil


class SystemHealthTool:
    name = "system_health"
    description = "Return CPU, memory, disk, and platform health metrics."

    async def run(self, **kwargs: Any) -> dict[str, Any]:
        path = str(kwargs.get("path") or os.getcwd())
        disk = psutil.disk_usage(path)
        return {
            "status": "success",
            "platform": platform.platform(),
            "cpu_percent": psutil.cpu_percent(interval=0.2),
            "memory_percent": psutil.virtual_memory().percent,
            "disk_percent": disk.percent,
        }
