from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TaskNode:
    task_id: str
    title: str
    description: str
    dependencies: set[str] = field(default_factory=set)
    metadata: dict[str, Any] = field(default_factory=dict)
    status: str = "pending"
    result: dict[str, Any] | None = None


class TaskGraph:
    """Directed task graph for goal execution planning."""

    def __init__(self, goal: str):
        self.goal = goal
        self.nodes: dict[str, TaskNode] = {}

    def add_task(
        self,
        *,
        task_id: str,
        title: str,
        description: str,
        dependencies: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> TaskNode:
        node = TaskNode(
            task_id=task_id,
            title=title,
            description=description,
            dependencies=set(dependencies or []),
            metadata=metadata or {},
        )
        self.nodes[task_id] = node
        return node

    def mark_in_progress(self, task_id: str) -> None:
        node = self.nodes.get(task_id)
        if node:
            node.status = "in_progress"

    def mark_complete(self, task_id: str, result: dict[str, Any] | None = None) -> None:
        node = self.nodes.get(task_id)
        if node:
            node.status = "completed"
            node.result = result or {}

    def mark_failed(self, task_id: str, error: str) -> None:
        node = self.nodes.get(task_id)
        if node:
            node.status = "failed"
            node.result = {"error": error}

    def mark_blocked(self, task_id: str, reason: str, *, requires_confirmation: bool = False) -> None:
        node = self.nodes.get(task_id)
        if node:
            node.status = "blocked"
            node.result = {
                "reason": reason,
                "requires_confirmation": bool(requires_confirmation),
            }

    def ready_tasks(self) -> list[TaskNode]:
        ready: list[TaskNode] = []
        for node in self.nodes.values():
            if node.status != "pending":
                continue
            if all(self.nodes.get(dep) and self.nodes[dep].status == "completed" for dep in node.dependencies):
                ready.append(node)
        return ready

    def blocked_tasks(self) -> list[TaskNode]:
        return [n for n in self.nodes.values() if n.status == "blocked"]

    def is_finished(self) -> bool:
        if not self.nodes:
            return True
        return all(node.status in {"completed", "failed", "blocked"} for node in self.nodes.values())

    def has_failures(self) -> bool:
        return any(node.status == "failed" for node in self.nodes.values())

    def has_blocked(self) -> bool:
        return any(node.status == "blocked" for node in self.nodes.values())

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "nodes": [
                {
                    "task_id": n.task_id,
                    "title": n.title,
                    "description": n.description,
                    "dependencies": sorted(list(n.dependencies)),
                    "metadata": n.metadata,
                    "status": n.status,
                    "result": n.result,
                }
                for n in self.nodes.values()
            ],
        }
