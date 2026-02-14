"""Compatibility entrypoint.

The canonical PC agent implementation lives in apps/pc_agent/pc_agent.py.
This module remains so existing scripts/imports keep working.
"""

from apps.pc_agent.pc_agent import run_agent

__all__ = ["run_agent"]

if __name__ == "__main__":
    import runpy

    runpy.run_module("apps.pc_agent.pc_agent", run_name="__main__")
