from src.core.module_update_cycle import ModuleUpdateCycleService
from src.utils.task_manager import task_manager


class _DummyExecutor:
    async def process_actions(self, actions, user):
        return [{"status": "success", "action": "web_search", "results": []}]


def test_parse_start_module_title():
    service = ModuleUpdateCycleService(executor=_DummyExecutor(), self_add_feature=None)
    assert service.parse_start_module_title("Add currency converter module") == "currency converter"
    assert service.parse_start_module_title("create module weather alerts") == "weather alerts"
    assert service.parse_start_module_title("what is weather") is None


def test_delete_tasks_by_title_matches_description(monkeypatch):
    original_tasks = list(task_manager.tasks)
    monkeypatch.setattr(task_manager, "_use_mongo", lambda: False)
    monkeypatch.setattr(task_manager, "_save_tasks", lambda: None)

    task_manager.tasks = [
        {
            "id": "task_1",
            "description": "Module Cycle: currency converter",
            "meta": {"user_id": "admin", "module_title": "currency converter"},
        },
        {
            "id": "task_2",
            "description": "Research: weather api",
            "meta": {"user_id": "admin"},
        },
    ]

    result = task_manager.delete_tasks_by_title("currency converter", owner="admin", is_admin=True)
    assert result["status"] == "success"
    assert result["deleted"] == 1
    assert result["task_ids"] == ["task_1"]
    assert len(task_manager.tasks) == 1

    task_manager.tasks = original_tasks


def test_continue_command_detection():
    service = ModuleUpdateCycleService(executor=_DummyExecutor(), self_add_feature=None)
    assert service.is_continue_command("continue module task currency converter") is True
    assert service.is_continue_command("resume module") is True
    assert service.is_continue_command("hello there") is False
