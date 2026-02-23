from src.jobs.job_scheduler import JobScheduler, progressive_llm_brain_update
from src.config import runtime_defaults as rd


def test_register_default_jobs_includes_progressive_llm_job(monkeypatch):
    captured = []

    scheduler = JobScheduler()

    def _fake_add_job(func, interval_seconds=300, job_id=None):
        captured.append((func, interval_seconds, job_id))

    monkeypatch.setattr(scheduler, "add_job", _fake_add_job)
    monkeypatch.setattr(rd, "ENABLE_PROGRESSIVE_LLM_UPDATE_JOB", True)
    monkeypatch.setattr(rd, "PROGRESSIVE_LLM_UPDATE_INTERVAL_SECONDS", 86400)

    scheduler.register_default_jobs()

    job_ids = [j[2] for j in captured]
    assert "progressive_llm_brain_update" in job_ids


def test_progressive_llm_brain_update_calls_targets(monkeypatch):
    calls = []

    monkeypatch.setattr(rd, "PROGRESSIVE_LLM_UPDATE_TARGET_FILES_CSV", "src/core/llm_adapter.py,src/core/jarvis_brain.py")
    monkeypatch.setattr(rd, "PROGRESSIVE_LLM_UPDATE_DESCRIPTION", "small safe incremental improvement")
    monkeypatch.setattr(rd, "PROGRESSIVE_LLM_UPDATE_DRY_RUN", True)
    monkeypatch.setattr(rd, "PROGRESSIVE_LLM_UPDATE_AUTO_INSTALL_DEPS", False)
    monkeypatch.setattr(rd, "PROGRESSIVE_LLM_UPDATE_ACTOR", "scheduler")

    def _fake_self_update_file(description, file_path, *, actor=None, auto_install_deps=None, dry_run=False):
        calls.append({
            "description": description,
            "file_path": file_path,
            "actor": actor,
            "auto_install_deps": auto_install_deps,
            "dry_run": dry_run,
        })
        return {"status": "success", "path": file_path}

    monkeypatch.setattr("src.utils.self_update.self_update_file", _fake_self_update_file)
    monkeypatch.setattr("src.jobs.job_scheduler._db_available", lambda: False)

    progressive_llm_brain_update()

    assert len(calls) == 2
    assert calls[0]["file_path"] == "src/core/llm_adapter.py"
    assert calls[1]["file_path"] == "src/core/jarvis_brain.py"
    assert all(c["dry_run"] is True for c in calls)
