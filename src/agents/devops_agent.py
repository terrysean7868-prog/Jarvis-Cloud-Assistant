from __future__ import annotations

from typing import Any

from src.agents.base_agent import BaseAgent
from src.devops.deployment_manager import DeploymentManager


class DevOpsAgent(BaseAgent):
    name = "DevOpsAgent"

    def __init__(self, deployment_manager: DeploymentManager):
        self.deployment_manager = deployment_manager

    async def plan(self, task: dict[str, Any]) -> dict[str, Any]:
        return {
            "agent": self.name,
            "strategy": "build_deploy_pipeline",
            "steps": ["prepare environment", "create Dockerfile", "run build scripts"],
            "task": task,
        }

    async def execute(self, task: dict[str, Any]) -> dict[str, Any]:
        app_type = str(task.get("app_type") or "python-fastapi")
        docker_status = self.deployment_manager.ensure_dockerfile(app_type=app_type)
        build_result = self.deployment_manager.run_build_script(str(task.get("build_script") or ""))
        env_result = self.deployment_manager.prepare_environment(task.get("env") if isinstance(task.get("env"), dict) else {})
        deploy_result = self.deployment_manager.deploy_local(mode=str(task.get("deploy_mode") or "dry_run"))
        return {
            "status": "success",
            "agent": self.name,
            "docker": docker_status,
            "build": build_result,
            "environment": env_result,
            "deploy": deploy_result,
            "notes": "DevOps artifacts prepared for local deployment.",
        }

    async def evaluate(self, task: dict[str, Any], execution_result: dict[str, Any]) -> dict[str, Any]:
        ok = str(execution_result.get("status") or "").lower() == "success"
        return {
            "agent": self.name,
            "task_id": task.get("task_id"),
            "status": "pass" if ok else "fail",
            "reason": execution_result.get("error") if not ok else None,
        }
