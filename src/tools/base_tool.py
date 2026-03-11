from __future__ import annotations

from typing import Any, Protocol


class ToolProtocol(Protocol):
    name: str
    description: str

    async def run(self, **kwargs: Any) -> dict[str, Any]:
        ...
