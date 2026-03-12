from __future__ import annotations

from src.agents.agent_controller import AgentController
from src.autonomy.background_loop import AutonomousLoopService
from src.autonomy.evaluation_engine import EvaluationEngine
from src.autonomy.execution_engine import ExecutionEngine
from src.autonomy.goal_manager import GoalManager
from src.autonomy.oss_stack import OSSStack
from src.autonomy.research_pipeline import ResearchPipeline
from src.learning.reflection_engine import ReflectionEngine
from src.memory.knowledge_store import KnowledgeStore
from src.planning.task_planner import TaskPlanner
from src.safety.risk_engine import RiskEngine
from src.self_improvement.code_analyzer import CodeAnalyzer
from src.self_improvement.patch_generator import PatchGenerator
from src.self_improvement.patch_validator import PatchValidator
from src.self_improvement.tool_generator import ToolGenerator
from src.tools.tool_registry import ToolRegistry
from src.config.settings import settings


class AutonomyRuntime:
    """Top-level autonomy assembly for the Jarvis autonomous engineer stack."""

    def __init__(self, *, device_hub, enabled: bool = True, poll_interval_seconds: int = 20):
        self.tools = ToolRegistry()
        self.goals = GoalManager()
        self.planner = TaskPlanner()
        self.knowledge = KnowledgeStore()
        self.reflection = ReflectionEngine()
        self.risk_engine = RiskEngine()
        self.oss_stack = OSSStack()
        self.evaluation_engine = EvaluationEngine()
        self.code_analyzer = CodeAnalyzer()
        self.patch_generator = PatchGenerator()
        self.patch_validator = PatchValidator()
        self.tool_generator = ToolGenerator()

        self.controller = AgentController(
            tool_registry=self.tools,
            device_hub=device_hub,
            reflection_engine=self.reflection,
            health_check=self._health_check,
        )
        self.execution_engine = ExecutionEngine(controller=self.controller, risk_engine=self.risk_engine)
        self.research_pipeline = ResearchPipeline(tools=self.tools, knowledge_store=self.knowledge)

        self.loop = AutonomousLoopService(
            goal_manager=self.goals,
            task_planner=self.planner,
            controller=self.controller,
            execution_engine=self.execution_engine,
            evaluation_engine=self.evaluation_engine,
            risk_engine=self.risk_engine,
            reflection_engine=self.reflection,
            poll_interval_seconds=poll_interval_seconds,
            enabled=enabled,
        )

    def _health_check(self) -> dict:
        return {
            "status": "ok",
            "service": "jarvis-autonomy",
            "tools": len(self.tools.list_tools()),
            "agents": len(self.controller.list_agents()),
            "oss": self.oss_stack.capabilities(),
            "self_improvement": {
                "enabled": not bool(settings.cloud_mode),
                "cloud_mode": bool(settings.cloud_mode),
            },
        }

    async def start(self) -> None:
        await self.tools.discover_tools()
        await self.loop.start()

    async def stop(self) -> None:
        await self.loop.stop()

    def set_paused(self, paused: bool) -> None:
        if paused:
            self.loop.pause()
        else:
            self.loop.resume()

    def control_state(self) -> dict:
        return self.loop.control_state()

    async def run_tick_once(self) -> None:
        await self.loop.tick_once()
