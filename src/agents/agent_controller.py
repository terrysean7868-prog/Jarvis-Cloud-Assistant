from __future__ import annotations

from typing import Any

from src.agents.automation_agent import AutomationAgent
from src.agents.coding_agent import CodingAgent
from src.agents.data_agent import DataAgent
from src.agents.device_agent import DeviceAgent
from src.agents.devops_agent import DevOpsAgent
from src.agents.monitoring_agent import MonitoringAgent
from src.agents.research_agent import ResearchAgent
from src.agents.security_agent import SecurityAgent
from src.devops.deployment_manager import DeploymentManager
from src.learning.reflection_engine import ReflectionEngine
from src.safety.risk_engine import RiskEngine
from src.tools.tool_registry import ToolRegistry


class AgentController:
    """Dynamic router for specialized autonomous agents."""

    def __init__(
        self,
        *,
        tool_registry: ToolRegistry,
        device_hub: Any,
        reflection_engine: ReflectionEngine,
        health_check: Any = None,
    ):
        self.tool_registry = tool_registry
        self.reflection_engine = reflection_engine

        self.agents = {
            "ResearchAgent": ResearchAgent(tool_registry),
            "CodingAgent": CodingAgent(tool_registry),
            "DevOpsAgent": DevOpsAgent(DeploymentManager()),
            "AutomationAgent": AutomationAgent(),
            "DeviceAgent": DeviceAgent(device_hub),
            "MonitoringAgent": MonitoringAgent(health_check=health_check),
            "DataAgent": DataAgent(),
            "SecurityAgent": SecurityAgent(RiskEngine()),
        }

    def route_task(self, task: dict[str, Any]) -> str:
        hint = str(task.get("agent") or "").strip()
        if hint in self.agents:
            return hint

        text = f"{task.get('title', '')} {task.get('description', '')}".lower()
        if any(k in text for k in ["deploy", "docker", "build", "env", "release", "pipeline", "compose"]):
            return "DevOpsAgent"
        if any(k in text for k in ["research", "compare", "analysis", "best", "find", "docs", "documentation"]):
            return "ResearchAgent"
        if any(k in text for k in ["device", "pc", "screen", "application", "system state", "multi device", "agent"]):
            return "DeviceAgent"
        if any(k in text for k in ["monitor", "health", "error", "restart failed"]):
            return "MonitoringAgent"
        if any(k in text for k in ["workflow", "automation", "n8n", "trigger"]):
            return "AutomationAgent"
        if any(k in text for k in ["security", "risk", "permission", "policy", "audit"]):
            return "SecurityAgent"
        if any(k in text for k in ["data", "memory", "knowledge", "semantic"]):
            return "DataAgent"
        return "CodingAgent"

    def list_agents(self) -> list[dict[str, Any]]:
        return [{"name": name, "class": agent.__class__.__name__} for name, agent in self.agents.items()]

    async def dispatch(self, task: dict[str, Any]) -> dict[str, Any]:
        name = self.route_task(task)
        agent = self.agents[name]

        plan = await agent.plan(task)
        execution = await agent.execute(task)
        evaluation = await agent.evaluate(task, execution)
        reflection = self.reflection_engine.reflect(task=task, outcome=execution)

        return {
            "agent": name,
            "plan": plan,
            "execution": execution,
            "evaluation": evaluation,
            "reflection": reflection,
        }
