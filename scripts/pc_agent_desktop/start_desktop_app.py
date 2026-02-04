#!/usr/bin/env python
"""Launcher for the PC Agent Desktop app."""

import subprocess
import sys
from pathlib import Path


def _find_repo_root(start: Path) -> Path:
    for candidate in [start, *start.parents]:
        if (candidate / "pc_agent.py").exists():
            return candidate
    return start


def main() -> None:
    here = Path(__file__).resolve().parent
    repo_root = _find_repo_root(here)
    desktop_app = here / "pc_agent_desktop.py"

    if not desktop_app.exists():
        print(f"Error: Desktop app not found at {desktop_app}")
        raise SystemExit(1)

    python_candidates = [
        repo_root / ".venv" / "Scripts" / "python.exe",
        repo_root / "venv" / "Scripts" / "python.exe",
        repo_root / ".venv" / "bin" / "python",
        repo_root / "venv" / "bin" / "python",
    ]

    python_exe = sys.executable
    for candidate in python_candidates:
        if candidate.exists():
            python_exe = str(candidate.resolve())
            break

    try:
        subprocess.run([python_exe, str(desktop_app)], cwd=str(repo_root))
    except KeyboardInterrupt:
        return


if __name__ == "__main__":
    main()
