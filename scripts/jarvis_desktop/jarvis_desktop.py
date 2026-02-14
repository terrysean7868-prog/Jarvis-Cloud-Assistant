"""Legacy entrypoint (compatibility stub).

Canonical implementation lives in apps/desktop/jarvis_desktop.py.
"""

import runpy


if __name__ == "__main__":
    runpy.run_module("apps.desktop.jarvis_desktop", run_name="__main__")
