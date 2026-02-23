"""Compatibility launcher for the new desktop app.

Canonical implementation now lives in `apps/desktop/desktop_app.py`.
"""

from apps.desktop.desktop_app import main


if __name__ == "__main__":
    raise SystemExit(main())
