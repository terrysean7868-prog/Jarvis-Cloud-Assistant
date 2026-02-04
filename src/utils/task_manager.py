# src/utils/task_manager.py
"""
Task Management System
Manages tasks, operations, and step-by-step execution tracking
"""
import json
import time
from datetime import datetime
from typing import Dict, List, Optional
from pathlib import Path
from enum import Enum

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
        self.tasks = self._load_tasks()
        self.current_task = None
        self.task_history = []
        self.stop_requested = False
        self.wakeup_context = {}  # Context mapping for wakeup command
    
    def _load_tasks(self) -> List[Dict]:
        """Load tasks from file"""
        if TASK_FILE.exists():
            try:
                with open(TASK_FILE, 'r') as f:
                    return json.load(f)
            except:
                return []
        return []
    
    def _save_tasks(self):
        """Save tasks to file"""
        try:
            with open(TASK_FILE, 'w') as f:
                json.dump(self.tasks, f, indent=2)
        except Exception as e:
            print(f"Failed to save tasks: {e}")
    
    def create_task(self, description: str, steps: List[Dict], priority: int = 5, meta: Optional[Dict] = None) -> str:
        """Create a new task"""
        task_id = f"task_{int(time.time())}"
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
        self._save_tasks()
        
        return {"status": "success", "task": task}
    
    def execute_next_step(self, result: Optional[Dict] = None) -> Optional[Dict]:
        """Execute next step of current task"""
        if not self.current_task:
            return None
        
        if self.stop_requested:
            self.current_task["status"] = TaskStatus.STOPPED.value
            self._save_tasks()
            return {"status": "stopped", "message": "Task stopped by user"}
        
        # Save result of previous step
        if result:
            self.current_task["results"].append(result)
        
        # Check if task is complete
        if self.current_task["current_step"] >= len(self.current_task["steps"]):
            self.current_task["status"] = TaskStatus.COMPLETED.value
            self.current_task["completed_at"] = datetime.now().isoformat()
            task_id = self.current_task["id"]
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
            self.current_task = None
            self._save_tasks()
            return {"status": "success", "message": "Task stopped", "task_id": task_id}
        return {"status": "error", "message": "No task running"}
    
    def pause_task(self) -> Dict:
        """Pause current task"""
        if self.current_task:
            self.current_task["status"] = TaskStatus.PAUSED.value
            self._save_tasks()
            return {"status": "success", "message": "Task paused"}
        return {"status": "error", "message": "No task running"}
    
    def resume_task(self) -> Dict:
        """Resume paused task"""
        if self.current_task and self.current_task["status"] == TaskStatus.PAUSED.value:
            self.current_task["status"] = TaskStatus.IN_PROGRESS.value
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
        return self._find_task(task_id)

    def is_cancel_requested(self, task_id: str) -> bool:
        """Return True if a task has a cancel requested flag set."""
        task = self._find_task(task_id)
        if not task:
            return False
        meta = task.get("meta")
        return bool(isinstance(meta, dict) and meta.get("cancel_requested"))

    def request_cancel(self, task_id: str, *, reason: Optional[str] = None) -> Dict:
        """Request cancellation for a specific task.

        Background jobs should poll `is_cancel_requested` and exit cooperatively.
        """
        task = self._find_task(task_id)
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
        return self.tasks
    
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
    
    def get_wakeup_context(self) -> Dict:
        """Get wakeup context mapping"""
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

