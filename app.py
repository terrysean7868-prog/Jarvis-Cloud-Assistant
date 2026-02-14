"""Compatibility entrypoint.

The canonical backend implementation lives in apps/web/app.py.

This module re-exports the canonical module so existing imports like
`import app` (and `uvicorn app:app`) continue to work, including access to
module-level globals used in tests.
"""

from apps.web.app import *  # noqa: F401,F403

# Ensure `app:app` remains valid for ASGI servers.
from apps.web.app import app as app
