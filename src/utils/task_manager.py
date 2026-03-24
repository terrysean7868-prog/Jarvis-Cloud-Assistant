"""Task Management System.

Scalability goals:
- In cloud/multi-instance deployments, task state must be centralized and survive restarts.
- Prefer MongoDB when available.
- Avoid file-based persistence in cloud mode.
"""

import json
import time
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional

from src.config.settings import settings
from src.utils.db import db

TASK_FILE = Path(__file__).parent.parent.parent / "data" / "tasks.json"
TASK_FILE.parent.mkdir(exist_ok=True)


class TaskStatus(Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"
    PAUSED = "paused"


class TaskManager:
    """Manage tasks and operations"""
    
    def __init__(self):
        self._store_mode = (settings.task_store or "auto").strip().lower()
        # Backstop: cloud must not use file-based persistence.
        if settings.cloud_mode and self._store_mode in {"auto", "file"}:
            self._store_mode = "mongo"

        self.tasks = self._load_tasks()
        self.current_task = None
        self.task_history = []
        self.stop_requested = False
        self.wakeup_context = {}  # Context mapping for wakeup command

    def _mongo_available(self) -> bool:
        try:
            db._ensure_connected()
            return db.db is not None
        except Exception:
            return False

    def _use_mongo(self) -> bool:
        if self._store_mode == "mongo":
            return True
        if self._store_mode in {"file", "memory"}:
            return False
        # auto
        return self._mongo_available()
    
    def _load_tasks(self) -> List[Dict]:
        """Load tasks from file"""
        if self._use_mongo():
            try:
                return db.tasks_list(limit=1000) or []
            except Exception:
                return []

        if self._store_mode != "file":
            # Default when Mongo isn't available: in-memory only.
            return []

        if TASK_FILE.exists():
            try:
                with open(TASK_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return data if isinstance(data, list) else []
            except Exception:
                return []
        return []
    
    def _save_tasks(self):
        """Save tasks to file"""
        if self._use_mongo():
            try:
                for t in self.tasks or []:
                    if isinstance(t, dict):
                        db.tasks_upsert(t)
            except Exception:
                pass
            return

        if self._store_mode != "file":
            return

        try:
            with open(TASK_FILE, "w", encoding="utf-8") as f:
                json.dump(self.tasks, f, indent=2)
        except Exception as e:
            print(f"Failed to save tasks: {e}")
    
    def create_task(self, description: str, steps: List[Dict], priority: int = 5, meta: Optional[Dict] = None) -> str:
        """Create a new task"""
        # Cloud backstop: tasks must be persisted centrally.
        if settings.cloud_mode and not self._use_mongo():
            raise RuntimeError("Task store unavailable in cloud mode (MongoDB required)")
        task_id = f"task_{int(time.time())}_{int(time.time() * 1000) % 100000}"
        task = {
            "id": task_id,
            "description": description,
            "steps": steps,
            "status": TaskStatus.PENDING.value,
            "priority": priority,
            "created_at": datetime.now().isoformat(),
            "started_at": None,
            "completed_at": None,
            "current_step": 0,
            "results": [],
            "meta": meta or {},
        }
        self.tasks.append(task)
        try:
            db.log_task_event(
                {
                    "timestamp": task.get("created_at"),
                    "user_id": ((meta or {}).get("user_id") if isinstance(meta, dict) else None) or "user",
                    "session_id": ((meta or {}).get("session_id") if isinstance(meta, dict) else None) or "unknown",
                    "correlation_id": task_id,
                    "source": "task",
                    "mode": "cloud" if settings.cloud_mode else "local",
                    "lifecycle_state": TaskStatus.PENDING.value,
                    "type": "task_created",
                    "message": description,
                    "task": task,
                }
            )
        except Exception:
            pass
        self._save_tasks()
        return task_id

    def update_task(self, task_id: str, *, status: Optional[str] = None, append_result: Optional[Dict] = None, meta_update: Optional[Dict] = None) -> Dict:
        """Update an existing task (best-effort).

        Used by background jobs (e.g., research) to mark progress/completion.
        """
        task = self._find_task(task_id)
        if not task:
            return {"status": "error", "message": "Task not found"}

        if status:
            task["status"] = status
            if status in {TaskStatus.IN_PROGRESS.value} and not task.get("started_at"):
                task["started_at"] = datetime.now().isoformat()
            if status in {TaskStatus.COMPLETED.value, TaskStatus.FAILED.value, TaskStatus.STOPPED.value}:
                task["completed_at"] = datetime.now().isoformat()

        if append_result is not None:
            try:
                if not isinstance(task.get("results"), list):
                    task["results"] = []
                task["results"].append(append_result)
            except Exception:
                pass

        if isinstance(meta_update, dict) and meta_update:
            try:
                if not isinstance(task.get("meta"), dict):
                    task["meta"] = {}
                task["meta"].update(meta_update)
            except Exception:
                pass

        # Mongo: persist incremental update so other instances can see it quickly.
        if self._use_mongo():
            try:
                db.tasks_upsert(task)
            except Exception:
                pass

        try:
            db.log_task_event(
                {
                    "timestamp": datetime.now().isoformat(),
                    "user_id": ((task.get("meta") or {}).get("user_id") if isinstance(task.get("meta"), dict) else None) or "user",
                    "session_id": ((task.get("meta") or {}).get("session_id") if isinstance(task.get("meta"), dict) else None) or "unknown",
                    "correlation_id": task_id,
                    "source": "task",
                    "mode": "cloud" if settings.cloud_mode else "local",
                    "lifecycle_state": task.get("status") or "recorded",
                    "type": "task_updated",
                    "message": task.get("description") or task_id,
                    "task": task,
                }
            )
        except Exception:
            pass

        self._save_tasks()
        return {"status": "success", "task": task}
    
    def start_task(self, task_id: str) -> Dict:
        """Start executing a task"""
        task = self._find_task(task_id)
        if not task:
            return {"status": "error", "message": "Task not found"}
        
        if self.current_task and self.current_task["id"] != task_id:
            return {"status": "error", "message": "Another task is already running"}
        
        task["status"] = TaskStatus.IN_PROGRESS.value
        task["started_at"] = datetime.now().isoformat()
        task["current_step"] = 0
        self.current_task = task
        self.stop_requested = False
        try:
            db.log_task_event(
                {
                    "timestamp": datetime.now().isoformat(),
                    "user_id": ((task.get("meta") or {}).get("user_id") if isinstance(task.get("meta"), dict) else None) or "user",
                    "session_id": ((task.get("meta") or {}).get("session_id") if isinstance(task.get("meta"), dict) else None) or "unknown",
                    "correlation_id": task_id,
                    "source": "task",
                    "mode": "cloud" if settings.cloud_mode else "local",
                    "lifecycle_state": TaskStatus.IN_PROGRESS.value,
                    "type": "task_started",
                    "message": task.get("description") or task_id,
                }
            )
        except Exception:
            pass
        self._save_tasks()
        
        return {"status": "success", "task": task}
    
    def execute_next_step(self, result: Optional[Dict] = None) -> Optional[Dict]:
        """Execute next step of current task"""
        if not self.current_task:
            return None
        
        if self.stop_requested:
            self.current_task["status"] = TaskStatus.STOPPED.value
            try:
                db.log_task_event(
                    {
                        "timestamp": datetime.now().isoformat(),
                        "user_id": ((self.current_task.get("meta") or {}).get("user_id") if isinstance(self.current_task.get("meta"), dict) else None) or "user",
                        "session_id": ((self.current_task.get("meta") or {}).get("session_id") if isinstance(self.current_task.get("meta"), dict) else None) or "unknown",
                        "correlation_id": self.current_task.get("id"),
                        "source": "task",
                        "mode": "cloud" if settings.cloud_mode else "local",
                        "lifecycle_state": TaskStatus.STOPPED.value,
                        "result_status": "failed",
                        "type": "task_stopped",
                        "message": self.current_task.get("description") or "Task stopped",
                    }
                )
            except Exception:
                pass
            self._save_tasks()
            return {"status": "stopped", "message": "Task stopped by user"}
        
        # Save result of previous step
        if result:
            self.current_task["results"].append(result)
            try:
                task_id = self.current_task.get("id")
                rs = "recorded"
                if isinstance(result, dict):
                    rs_raw = str(result.get("status") or result.get("result") or "").strip().lower()
                    if rs_raw in {"success", "completed", "ok", "done"}:
                        rs = "success"
                    elif rs_raw in {"failed", "error", "blocked", "denied", "stopped"}:
                        rs = "failed"
                    elif rs_raw in {"pending", "running", "in_progress", "executing"}:
                        rs = "in_progress"
                db.log_task_event(
                    {
                        "timestamp": datetime.now().isoformat(),
                        "user_id": ((self.current_task.get("meta") or {}).get("user_id") if isinstance(self.current_task.get("meta"), dict) else None) or "user",
                        "session_id": ((self.current_task.get("meta") or {}).get("session_id") if isinstance(self.current_task.get("meta"), dict) else None) or "unknown",
                        "correlation_id": task_id,
                        "source": "task",
                        "mode": "cloud" if settings.cloud_mode else "local",
                        "lifecycle_state": TaskStatus.IN_PROGRESS.value,
                        "result_status": rs,
                        "type": "task_step_result",
                        "message": self.current_task.get("description") or str(task_id or "task"),
                        "step_index": int(max(0, (self.current_task.get("current_step") or 0) - 1)),
                        "step_result": result,
                    }
                )
            except Exception:
                pass
        
        # Check if task is complete
        if self.current_task["current_step"] >= len(self.current_task["steps"]):
            self.current_task["status"] = TaskStatus.COMPLETED.value
            self.current_task["completed_at"] = datetime.now().isoformat()
            task_id = self.current_task["id"]
            try:
                db.log_task_event(
                    {
                        "timestamp": datetime.now().isoformat(),
                        "user_id": ((self.current_task.get("meta") or {}).get("user_id") if isinstance(self.current_task.get("meta"), dict) else None) or "user",
                        "session_id": ((self.current_task.get("meta") or {}).get("session_id") if isinstance(self.current_task.get("meta"), dict) else None) or "unknown",
                        "correlation_id": task_id,
                        "source": "task",
                        "mode": "cloud" if settings.cloud_mode else "local",
                        "lifecycle_state": TaskStatus.COMPLETED.value,
                        "result_status": "success",
                        "type": "task_completed",
                        "message": self.current_task.get("description") or task_id,
                    }
                )
            except Exception:
                pass
            self.current_task = None
            self._save_tasks()
            return {"status": "completed", "task_id": task_id}
        
        # Get next step
        step = self.current_task["steps"][self.current_task["current_step"]]
        self.current_task["current_step"] += 1
        self._save_tasks()
        
        return {
            "status": "in_progress",
            "step": step,
            "step_number": self.current_task["current_step"],
            "total_steps": len(self.current_task["steps"])
        }
    
    def stop_current_task(self) -> Dict:
        """Stop current task"""
        self.stop_requested = True
        if self.current_task:
            self.current_task["status"] = TaskStatus.STOPPED.value
            self.current_task["completed_at"] = datetime.now().isoformat()
            task_id = self.current_task["id"]
            try:
                db.log_task_event(
                    {
                        "timestamp": datetime.now().isoformat(),
                        "user_id": ((self.current_task.get("meta") or {}).get("user_id") if isinstance(self.current_task.get("meta"), dict) else None) or "user",
                        "session_id": ((self.current_task.get("meta") or {}).get("session_id") if isinstance(self.current_task.get("meta"), dict) else None) or "unknown",
                        "correlation_id": task_id,
                        "source": "task",
                        "mode": "cloud" if settings.cloud_mode else "local",
                        "lifecycle_state": TaskStatus.STOPPED.value,
                        "result_status": "failed",
                        "type": "task_stop_requested",
                        "message": self.current_task.get("description") or task_id,
                    }
                )
            except Exception:
                pass
            self.current_task = None
            self._save_tasks()
            return {"status": "success", "message": "Task stopped", "task_id": task_id}
        return {"status": "error", "message": "No task running"}
    
    def pause_task(self) -> Dict:
        """Pause current task"""
        if self.current_task:
            self.current_task["status"] = TaskStatus.PAUSED.value
            try:
                db.log_task_event(
                    {
                        "timestamp": datetime.now().isoformat(),
                        "user_id": ((self.current_task.get("meta") or {}).get("user_id") if isinstance(self.current_task.get("meta"), dict) else None) or "user",
                        "session_id": ((self.current_task.get("meta") or {}).get("session_id") if isinstance(self.current_task.get("meta"), dict) else None) or "unknown",
                        "correlation_id": self.current_task.get("id"),
                        "source": "task",
                        "mode": "cloud" if settings.cloud_mode else "local",
                        "lifecycle_state": TaskStatus.PAUSED.value,
                        "type": "task_paused",
                        "message": self.current_task.get("description") or "Task paused",
                    }
                )
            except Exception:
                pass
            self._save_tasks()
            return {"status": "success", "message": "Task paused"}
        return {"status": "error", "message": "No task running"}
    
    def resume_task(self) -> Dict:
        """Resume paused task"""
        if self.current_task and self.current_task["status"] == TaskStatus.PAUSED.value:
            self.current_task["status"] = TaskStatus.IN_PROGRESS.value
            try:
                db.log_task_event(
                    {
                        "timestamp": datetime.now().isoformat(),
                        "user_id": ((self.current_task.get("meta") or {}).get("user_id") if isinstance(self.current_task.get("meta"), dict) else None) or "user",
                        "session_id": ((self.current_task.get("meta") or {}).get("session_id") if isinstance(self.current_task.get("meta"), dict) else None) or "unknown",
                        "correlation_id": self.current_task.get("id"),
                        "source": "task",
                        "mode": "cloud" if settings.cloud_mode else "local",
                        "lifecycle_state": TaskStatus.IN_PROGRESS.value,
                        "type": "task_resumed",
                        "message": self.current_task.get("description") or "Task resumed",
                    }
                )
            except Exception:
                pass
            self._save_tasks()
            return {"status": "success", "message": "Task resumed"}
        return {"status": "error", "message": "No paused task"}
    
    def _find_task(self, task_id: str) -> Optional[Dict]:
        """Find task by ID"""
        for task in self.tasks:
            if task["id"] == task_id:
                return task
        return None

    def get_task(self, task_id: str) -> Optional[Dict]:
        """Get a task by id."""
        if self._use_mongo():
            try:
                t = db.tasks_get(task_id)
                if t:
                    return t
            except Exception:
                pass
        return self._find_task(task_id)

    def is_cancel_requested(self, task_id: str) -> bool:
        """Return True if a task has a cancel requested flag set."""
        task = self.get_task(task_id)
        if not task:
            return False
        meta = task.get("meta")
        return bool(isinstance(meta, dict) and meta.get("cancel_requested"))

    def request_cancel(self, task_id: str, *, reason: Optional[str] = None) -> Dict:
        """Request cancellation for a specific task.

        Background jobs should poll `is_cancel_requested` and exit cooperatively.
        """
        task = self.get_task(task_id)
        if not task:
            return {"status": "error", "message": "Task not found"}

        meta_update = {"cancel_requested": True}
        if reason:
            meta_update["cancel_reason"] = reason
        self.update_task(task_id, meta_update=meta_update)
        return {"status": "success", "task_id": task_id, "message": "Cancel requested"}
    
    def get_current_task(self) -> Optional[Dict]:
        """Get current task"""
        return self.current_task
    
    def get_all_tasks(self) -> List[Dict]:
        """Get all tasks"""
        if self._use_mongo():
            try:
                self.tasks = db.tasks_list(limit=1000) or []
            except Exception:
                pass
        return self.tasks

    def delete_tasks_by_title(
        self,
        title: str,
        *,
        owner: Optional[str] = None,
        is_admin: bool = False,
    ) -> Dict:
        """Delete tasks by title/description text match.

        Title is matched case-insensitively against:
        - task description
        - meta.module_title
        """
        needle = (title or "").strip().lower()
        if not needle:
            return {"status": "error", "message": "title is required", "deleted": 0, "task_ids": []}

        owner_norm = (owner or "").strip().lower()

        removed_ids: List[str] = []
        kept: List[Dict] = []

        for task in (self.get_all_tasks() or []):
            if not isinstance(task, dict):
                continue

            meta = task.get("meta") if isinstance(task.get("meta"), dict) else {}
            task_owner = (meta.get("user_id") or "").strip().lower() if isinstance(meta, dict) else ""
            if (not is_admin) and owner_norm and task_owner and task_owner != owner_norm:
                kept.append(task)
                continue

            description = str(task.get("description") or "").strip().lower()
            module_title = str(meta.get("module_title") or "").strip().lower() if isinstance(meta, dict) else ""
            if (needle in description) or (needle in module_title):
                tid = str(task.get("id") or "").strip()
                if tid:
                    removed_ids.append(tid)
                continue

            kept.append(task)

        if not removed_ids:
            return {"status": "error", "message": "No matching task found", "deleted": 0, "task_ids": []}

        self.tasks = kept
        if self._use_mongo():
            try:
                db._ensure_connected()
                if db.db is not None:
                    db.db.tasks.delete_many({"id": {"$in": removed_ids}})
            except Exception:
                pass
        self._save_tasks()
        return {
            "status": "success",
            "message": f"Deleted {len(removed_ids)} task(s)",
            "deleted": len(removed_ids),
            "task_ids": removed_ids,
        }
    
    def save_wakeup_context(self, prompt: str, response: str, actions: List[Dict]):
        """Save context for wakeup command mapping"""
        context_id = f"ctx_{int(time.time())}"
        self.wakeup_context[context_id] = {
            "prompt": prompt,
            "response": response,
            "actions": actions,
            "timestamp": datetime.now().isoformat()
        }
        # Keep only last 50 contexts
        if len(self.wakeup_context) > 50:
            oldest = min(self.wakeup_context.keys())
            del self.wakeup_context[oldest]

        # Persist to MongoDB when available.
        if self._use_mongo():
            try:
                db._ensure_connected()
                if db.db is not None:
                    col = db.db.wakeup_context
                    col.update_one(
                        {"context_id": context_id},
                        {
                            "$set": {
                                "context_id": context_id,
                                "prompt": prompt,
                                "response": response,
                                "actions": actions,
                                "timestamp": datetime.utcnow(),
                            }
                        },
                        upsert=True,
                    )
                    # Prune to last 200 entries (best-effort)
                    try:
                        ids = [d.get("_id") for d in col.find({}, {"_id": 1}).sort("timestamp", -1).skip(200)]
                        ids = [i for i in ids if i]
                        if ids:
                            col.delete_many({"_id": {"$in": ids}})
                    except Exception:
                        pass
            except Exception:
                pass
    
    def get_wakeup_context(self) -> Dict:
        """Get wakeup context mapping"""
        if self._use_mongo():
            try:
                db._ensure_connected()
                if db.db is not None:
                    col = db.db.wakeup_context
                    out: dict = {}
                    for d in col.find({}).sort("timestamp", -1).limit(50):
                        try:
                            cid = str(d.get("context_id") or "").strip()
                            if not cid:
                                continue
                            out[cid] = {
                                "prompt": d.get("prompt"),
                                "response": d.get("response"),
                                "actions": d.get("actions") or [],
                                "timestamp": (
                                    d.get("timestamp").isoformat() if hasattr(d.get("timestamp"), "isoformat") else d.get("timestamp")
                                ),
                            }
                        except Exception:
                            continue
                    self.wakeup_context = out
            except Exception:
                pass
        return self.wakeup_context
    
    def create_task_from_context(self, context_id: str) -> Optional[str]:
        """Create task from wakeup context"""
        if context_id not in self.wakeup_context:
            return None
        
        context = self.wakeup_context[context_id]
        steps = []
        
        for action in context.get("actions", []):
            steps.append({
                "action": action.get("type"),
                "description": f"Execute {action.get('type')}",
                "params": action
            })
        
        task_id = self.create_task(
            description=context.get("prompt", "Task from context"),
            steps=steps
        )
        
        return task_id


# Global instance
task_manager = TaskManager()

