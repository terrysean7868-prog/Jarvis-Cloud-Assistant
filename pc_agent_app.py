"""Compatibility entrypoint.

The canonical PC agent UI implementation lives in apps/pc_agent/pc_agent_app.py.
This module remains so existing scripts/imports keep working.
"""

if __name__ == "__main__":
    import runpy

    runpy.run_module("apps.pc_agent.pc_agent_app", run_name="__main__")
