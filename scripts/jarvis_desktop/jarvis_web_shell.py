"""Legacy entrypoint (compatibility stub).

Canonical implementation lives in apps/desktop/jarvis_web_shell.py.
"""

import runpy


if __name__ == "__main__":
    runpy.run_module("apps.desktop.jarvis_web_shell", run_name="__main__")
