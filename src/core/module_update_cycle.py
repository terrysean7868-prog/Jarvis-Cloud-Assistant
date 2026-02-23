import re
from typing import Any, Dict, List, Optional

from src.utils.task_manager import task_manager, TaskStatus


class ModuleUpdateCycleService:
    def __init__(self, executor, *, self_add_feature=None):
        self.executor = executor
        self.self_add_feature = self_add_feature

    @staticmethod
    def parse_start_module_title(text: str) -> Optional[str]:
        t = (text or "").strip()
        if not t:
            return None
        patterns = [
            r"^\s*(?:add|create|build)\s+(?:a\s+)?(.+?)\s+module\s*$",
            r"^\s*(?:add|create|build)\s+module\s+(.+?)\s*$",
        ]
        for pattern in patterns:
            m = re.match(pattern, t, flags=re.IGNORECASE)
            if m:
                title = (m.group(1) or "").strip(" .,!?:;\"'")
                return title or None
        return None

    @staticmethod
    def is_continue_command(text: str) -> bool:
        t = (text or "").strip().lower()
        if not t:
            return False
        keys = (
            "continue module",
            "module instruction",
            "resume module",
            "continue update cycle",
        )
        return any(k in t for k in keys)

    @staticmethod
    def _extract_instruction(text: str) -> str:
        t = (text or "").strip()
        if not t:
            return ""
        if ":" in t:
            return t.split(":", 1)[1].strip()
        m = re.search(r"\bwith\b\s+(.+)$", t, flags=re.IGNORECASE)
        if m:
            return (m.group(1) or "").strip()
        return ""

    @staticmethod
    def _active_cycles_for_user(username: str) -> List[Dict[str, Any]]:
        user = (username or "").strip().lower()
        out: List[Dict[str, Any]] = []
        for t in task_manager.get_all_tasks() or []:
            if not isinstance(t, dict):
                continue
            meta = t.get("meta") if isinstance(t.get("meta"), dict) else {}
            if (meta.get("task_type") or "") != "module_update_cycle":
                continue
            owner = (meta.get("user_id") or "").strip().lower()
            if owner and owner != user:
                continue
            if t.get("status") in {
                TaskStatus.COMPLETED.value,
                TaskStatus.FAILED.value,
                TaskStatus.STOPPED.value,
            }:
                continue
            out.append(t)
        out.sort(key=lambda x: str(x.get("created_at") or ""), reverse=True)
        return out

    async def start_cycle(self, *, title: str, username: str, background_tasks: Any) -> Dict[str, Any]:
        module_title = (title or "").strip()
        user = (username or "admin").strip().lower() or "admin"
        if not module_title:
            return {
                "handled": True,
                "response": {
                    "text": "Please tell me the module name, for example: Add currency converter module.",
                    "actions": [],
                },
            }

        steps = [
            {"action": "research", "description": "Collect web research and implementation references"},
            {"action": "clarify", "description": "Ask admin for constraints and preferences"},
            {"action": "plan", "description": "Create implementation plan"},
            {"action": "execute", "description": "Generate and apply module update"},
        ]
        task_id = task_manager.create_task(
            description=f"Module Cycle: {module_title}",
            steps=steps,
            priority=5,
            meta={
                "task_type": "module_update_cycle",
                "user_id": user,
                "module_title": module_title,
                "cycle_stage": "researching",
            },
        )
        task_manager.update_task(task_id, status=TaskStatus.IN_PROGRESS.value)

        try:
            background_tasks.add_task(self._run_research_phase, task_id, module_title, user)
        except Exception:
            await self._run_research_phase(task_id, module_title, user)

        return {
            "handled": True,
            "response": {
                "text": (
                    f"Started module cycle for '{module_title}'. I am collecting internet research now. "
                    f"You can discuss other topics in parallel. "
                    f"When ready, say: Continue module task {module_title}."
                ),
                "actions": [],
                "task_id": task_id,
            },
        }

    async def continue_cycle(self, *, text: str, username: str, background_tasks: Any) -> Dict[str, Any]:
        user = (username or "admin").strip().lower() or "admin"
        active = self._active_cycles_for_user(user)
        if not active:
            return {
                "handled": True,
                "response": {
                    "text": "No active module cycle task found. Say: Add <module name> module.",
                    "actions": [],
                },
            }

        target = active[0]
        task_id = str(target.get("id") or "")
        meta = target.get("meta") if isinstance(target.get("meta"), dict) else {}
        title = (meta.get("module_title") or target.get("description") or "module").strip()
        stage = (meta.get("cycle_stage") or "").strip().lower()
        instruction = self._extract_instruction(text)

        if stage == "awaiting_admin_input" and not instruction:
            questions = meta.get("suggestion_questions") if isinstance(meta.get("suggestion_questions"), list) else []
            summary = str(meta.get("research_summary") or "").strip()
            body = "\n".join([f"- {q}" for q in questions[:4]]) if questions else "- What exact features should be included?\n- Any API/provider preference?"
            msg = f"Research is ready for '{title}'.\n\nSummary:\n{summary or 'No summary available yet.'}\n\nPlease provide instructions.\n{body}"
            return {"handled": True, "response": {"text": msg, "actions": [], "task_id": task_id}}

        if not instruction:
            return {
                "handled": True,
                "response": {
                    "text": f"Please provide admin instruction, for example: Continue module task {title}: use exchangerate host, cache results, add unit tests.",
                    "actions": [],
                    "task_id": task_id,
                },
            }

        plan = [
            f"Define scope and interfaces for {title}",
            "Create module implementation with robust error handling",
            "Add integration points and safe defaults",
            "Run validation and keep rollback path",
        ]
        task_manager.update_task(
            task_id,
            meta_update={
                "cycle_stage": "executing",
                "admin_instruction": instruction,
                "execution_plan": plan,
            },
            append_result={"phase": "planning", "instruction": instruction, "plan": plan},
        )

        try:
            background_tasks.add_task(self._run_execution_phase, task_id, title, instruction, user)
        except Exception:
            await self._run_execution_phase(task_id, title, instruction, user)

        return {
            "handled": True,
            "response": {
                "text": (
                    f"Execution started for '{title}'.\n\nPlan:\n"
                    + "\n".join([f"- {p}" for p in plan])
                    + "\n\nYou can continue other topics while I execute this task."
                ),
                "actions": [],
                "task_id": task_id,
            },
        }

    async def _run_research_phase(self, task_id: str, module_title: str, username: str) -> None:
        query = (
            f"{module_title} module python implementation guide best practices API design "
            f"error handling security and testing"
        )
        results = await self.executor.process_actions([
            {"type": "web_search", "query": query, "num_results": 5}
        ], username)

        top_items: List[str] = []
        try:
            first = (results or [{}])[0]
            raw = first.get("results") if isinstance(first, dict) else []
            if isinstance(raw, list):
                for item in raw[:3]:
                    if not isinstance(item, dict):
                        continue
                    title = str(item.get("title") or "").strip()
                    snippet = str(item.get("snippet") or "").strip()
                    url = str(item.get("url") or "").strip()
                    text = title or snippet or url
                    if text:
                        top_items.append(text[:220])
        except Exception:
            top_items = []

        summary = "; ".join(top_items)[:1200] if top_items else "Research completed, but no rich snippets were returned."
        questions = [
            f"For {module_title}, should I use a free provider first or a paid API with SLA?",
            "Should I add caching and retry logic by default?",
            "Do you want unit tests and docs included in this update?",
        ]

        task_manager.update_task(
            task_id,
            meta_update={
                "cycle_stage": "awaiting_admin_input",
                "research_query": query,
                "research_summary": summary,
                "suggestion_questions": questions,
            },
            append_result={
                "phase": "research",
                "query": query,
                "summary": summary,
                "questions": questions,
            },
        )

    async def _run_execution_phase(self, task_id: str, module_title: str, instruction: str, username: str) -> None:
        if self.self_add_feature is None:
            task_manager.update_task(
                task_id,
                status=TaskStatus.FAILED.value,
                append_result={
                    "phase": "execute",
                    "status": "error",
                    "message": "Self-update module is unavailable",
                },
                meta_update={"cycle_stage": "failed"},
            )
            return

        task = task_manager.get_task(task_id) or {}
        meta = task.get("meta") if isinstance(task.get("meta"), dict) else {}
        summary = str(meta.get("research_summary") or "").strip()
        description = (
            f"Add {module_title} module. Admin instruction: {instruction}. "
            f"Use this research context: {summary}"
        )

        result = self.self_add_feature(description, feature_type="module", actor=username)
        status = TaskStatus.COMPLETED.value if (result or {}).get("status") == "success" else TaskStatus.FAILED.value
        final_stage = "completed" if status == TaskStatus.COMPLETED.value else "failed"

        task_manager.update_task(
            task_id,
            status=status,
            append_result={"phase": "execute", "result": result},
            meta_update={
                "cycle_stage": final_stage,
                "execution_result": result,
            },
        )
