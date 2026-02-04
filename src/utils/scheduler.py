"""Lightweight APScheduler wrapper.

IMPORTANT: Do not auto-start schedulers at import time.
Starting background threads on import causes unnecessary CPU usage and can
surprise callers (especially in packaged desktop apps).
"""

from __future__ import annotations

from apscheduler.schedulers.background import BackgroundScheduler

_scheduler: BackgroundScheduler | None = None


def get_scheduler() -> BackgroundScheduler:
	global _scheduler
	if _scheduler is None:
		_scheduler = BackgroundScheduler()
	return _scheduler


def start_scheduler() -> BackgroundScheduler:
	sch = get_scheduler()
	try:
		running = bool(getattr(sch, "running", False))
	except Exception:
		running = False
	if not running:
		sch.start()
	return sch


def shutdown_scheduler(wait: bool = True) -> None:
	global _scheduler
	sch = _scheduler
	if sch is None:
		return
	try:
		running = bool(getattr(sch, "running", False))
	except Exception:
		running = False
	if running:
		sch.shutdown(wait=wait)


# Backwards-compatible symbol for any code that expects `scheduler`.
# Note: it is NOT started automatically.
scheduler = get_scheduler()
