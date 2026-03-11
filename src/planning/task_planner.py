from __future__ import annotations

from src.planning.task_graph import TaskGraph


class TaskPlanner:
    """Builds dependency-aware task graphs instead of flat checklists."""

    async def plan_goal(self, goal: str) -> TaskGraph:
        graph = TaskGraph(goal=goal)
        g = (goal or "").strip().lower()

        graph.add_task(
            task_id="t1",
            title="Define Scope",
            description=f"Interpret goal and define objective boundaries: {goal}",
            metadata={"agent": "ResearchAgent"},
        )

        if any(k in g for k in ["research", "compare", "best", "find"]):
            graph.add_task(
                task_id="t2",
                title="Research Sources",
                description="Collect high quality sources and references.",
                dependencies=["t1"],
                metadata={"agent": "ResearchAgent"},
            )
            graph.add_task(
                task_id="t3",
                title="Synthesize Findings",
                description="Compare candidates and extract decision criteria.",
                dependencies=["t2"],
                metadata={"agent": "ResearchAgent"},
            )
            graph.add_task(
                task_id="t4",
                title="Generate Report",
                description="Produce a concise recommendation report with evidence.",
                dependencies=["t3"],
                metadata={"agent": "CodingAgent", "output": "report"},
            )
            return graph

        if any(k in g for k in ["deploy", "docker", "environment", "ci", "release"]):
            graph.add_task(
                task_id="t2",
                title="Generate Build Assets",
                description="Create Dockerfile and deployment scripts for the target app.",
                dependencies=["t1"],
                metadata={"agent": "DevOpsAgent"},
            )
            graph.add_task(
                task_id="t3",
                title="Validate Runtime",
                description="Run health checks and verify local deployment viability.",
                dependencies=["t2"],
                metadata={"agent": "MonitoringAgent"},
            )
            return graph

        if any(k in g for k in ["device", "pc", "screen", "system", "multi device", "agents"]):
            graph.add_task(
                task_id="t2",
                title="Discover Devices",
                description="Inspect connected PC agents and available capabilities.",
                dependencies=["t1"],
                metadata={"agent": "DeviceAgent"},
            )
            graph.add_task(
                task_id="t3",
                title="Run Device Workflow",
                description="Dispatch selected actions across one or more target devices.",
                dependencies=["t2"],
                metadata={"agent": "DeviceAgent"},
            )
            graph.add_task(
                task_id="t4",
                title="Assess Stability",
                description="Check post-execution health and collect operational telemetry.",
                dependencies=["t3"],
                metadata={"agent": "MonitoringAgent"},
            )
            return graph

        graph.add_task(
            task_id="t2",
            title="Design Solution",
            description="Create implementation plan and architecture split.",
            dependencies=["t1"],
            metadata={"agent": "CodingAgent"},
        )
        graph.add_task(
            task_id="t3",
            title="Implement",
            description="Build code and tools required by the objective.",
            dependencies=["t2"],
            metadata={"agent": "CodingAgent"},
        )
        graph.add_task(
            task_id="t4",
            title="Validate",
            description="Run checks, verify output, and capture lessons.",
            dependencies=["t3"],
            metadata={"agent": "MonitoringAgent"},
        )
        return graph
