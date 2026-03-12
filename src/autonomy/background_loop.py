from __future__ import annotations

import asyncio
from typing import Any

try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
except Exception:
    AsyncIOScheduler = None

from src.agents.agent_controller import AgentController
from src.autonomy.goal_manager import GoalManager
from src.autonomy.evaluation_engine import EvaluationEngine
from src.autonomy.execution_engine import ExecutionEngine
from src.learning.reflection_engine import ReflectionEngine
from src.planning.task_planner import TaskPlanner
from src.safety.risk_engine import RiskEngine


class AutonomousLoopService:
    """Continuous autonomous loop: goals -> plan -> dispatch -> evaluate."""

    def __init__(
        self,
        *,
        goal_manager: GoalManager,
        task_planner: TaskPlanner,
        controller: AgentController,
        execution_engine: ExecutionEngine,
        evaluation_engine: EvaluationEngine,
        risk_engine: RiskEngine,
        reflection_engine: ReflectionEngine,
        poll_interval_seconds: int = 20,
        enabled: bool = True,
    ):
        self.goal_manager = goal_manager
        self.task_planner = task_planner
        self.controller = controller
        self.execution_engine = execution_engine
        self.evaluation_engine = evaluation_engine
        self.risk_engine = risk_engine
        self.reflection_engine = reflection_engine
        self.poll_interval_seconds = max(5, int(poll_interval_seconds))
        self.enabled = bool(enabled)
        self._task: asyncio.Task | None = None
        self._stopping = False
        self._paused = False
        self._scheduler = None
        self._processing_goal_ids: set[str] = set()

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    def control_state(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.enabled),
            "paused": bool(self._paused),
            "poll_interval_seconds": int(self.poll_interval_seconds),
            "processing_goal_ids": sorted(list(self._processing_goal_ids)),
        }

    async def tick_once(self) -> None:
        await self._tick()

    async def start(self) -> None:
        if not self.enabled:
            return
        if AsyncIOScheduler is not None:
            if self._scheduler is None:
                self._scheduler = AsyncIOScheduler(timezone="UTC")
                self._scheduler.add_job(self._tick_job, "interval", seconds=self.poll_interval_seconds, id="jarvis_autonomy_tick", replace_existing=True)
                self._scheduler.start()
            return
        if self._task and not self._task.done():
            return
        self._stopping = False
        self._task = asyncio.create_task(self._run_forever(), name="jarvis-autonomous-loop")

    async def stop(self) -> None:
        self._stopping = True
        if self._scheduler is not None:
            try:
                self._scheduler.shutdown(wait=False)
            except Exception:
                pass
            self._scheduler = None
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except Exception:
                pass

    async def _tick_job(self) -> None:
        if self._stopping:
            return
        try:
            await self._tick()
        except Exception:
            return

    async def _run_forever(self) -> None:
        while not self._stopping:
            try:
                await self._tick()
            except asyncio.CancelledError:
                break
            except Exception:
                pass
            await asyncio.sleep(self.poll_interval_seconds)

    async def _tick(self) -> None:
        if self._paused:
            return
        goals = self.goal_manager.list_goals(statuses=["pending", "running"], limit=5)
        for goal in goals:
            goal_id = str(goal.get("_id") or "")
            if not goal_id:
                continue
            await self._process_goal(goal_id, goal)

    async def _process_goal(self, goal_id: str, goal: dict[str, Any]) -> None:
        if goal_id in self._processing_goal_ids:
            return
        self._processing_goal_ids.add(goal_id)
        self.goal_manager.update_goal_status(goal_id, "running")
        graph = await self.task_planner.plan_goal(str(goal.get("goal") or ""))
        needs_confirmation = False
        try:
            while True:
                ready = graph.ready_tasks()
                if not ready:
                    break

                for node in ready:
                    graph.mark_in_progress(node.task_id)
                    node_result = await self.execution_engine.run_node(node)
                    status = str(node_result.get("status") or "error")
                    risk = node_result.get("risk") or {}

                    if status == "blocked":
                        graph.mark_blocked(node.task_id, "blocked_by_safety", requires_confirmation=False)
                        self.goal_manager.append_report(
                            goal_id,
                            {
                                "task_id": node.task_id,
                                "status": "blocked",
                                "risk": risk,
                            },
                        )
                        continue

                    if status == "awaiting_confirmation":
                        needs_confirmation = True
                        graph.mark_blocked(node.task_id, "confirmation_required", requires_confirmation=True)
                        self.goal_manager.append_report(
                            goal_id,
                            {
                                "task_id": node.task_id,
                                "status": "awaiting_confirmation",
                                "risk": risk,
                            },
                        )
                        continue

                    result = node_result.get("result") if isinstance(node_result, dict) else None
                    exec_result = node_result.get("execution") if isinstance(node_result, dict) else None
                    if status == "success":
                        graph.mark_complete(node.task_id, exec_result)
                    else:
                        graph.mark_failed(node.task_id, str((exec_result or {}).get("error") or "execution_failed"))

                    self.goal_manager.append_report(goal_id, {"task_id": node.task_id, "result": result})
                    self.reflection_engine.reflect(
                        task={
                            "task_id": node.task_id,
                            "title": node.title,
                            "description": node.description,
                            "agent": node.metadata.get("agent"),
                        },
                        outcome=exec_result or {},
                    )

            if needs_confirmation:
                self.goal_manager.update_goal_status(goal_id, "awaiting_confirmation")
            elif graph.has_failures():
                self.goal_manager.update_goal_status(goal_id, "failed", last_error="One or more task nodes failed")
            elif graph.has_blocked():
                self.goal_manager.update_goal_status(goal_id, "blocked", last_error="One or more task nodes blocked by safety policy")
            else:
                self.goal_manager.update_goal_status(goal_id, "completed")

            graph_doc = graph.to_dict()
            self.goal_manager.append_report(goal_id, {"graph": graph_doc})
            self.evaluation_engine.evaluate_goal(goal=goal, graph=graph_doc, reports=list(goal.get("reports") or []))
        finally:
            self._processing_goal_ids.discard(goal_id)
