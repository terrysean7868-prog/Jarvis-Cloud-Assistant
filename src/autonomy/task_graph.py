from __future__ import annotations

# Backward-compatible autonomy namespace for task-graph primitives.
# Canonical implementation remains under src/planning/task_graph.py.
from src.planning.task_graph import TaskGraph, TaskNode

__all__ = ["TaskGraph", "TaskNode"]
