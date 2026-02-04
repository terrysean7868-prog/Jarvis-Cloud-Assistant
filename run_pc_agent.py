import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    repo_root = Path(__file__).resolve().parent

    agent_exe = repo_root / "dist" / "JarvisPCAgent.exe"
    agent_py = repo_root / "pc_agent.py"

    if agent_exe.exists():
        args = list(sys.argv[1:])
        # Default to daemon mode when launching the packaged exe.
        # Use --ui explicitly to open the UI.
        if "--ui" not in args and "--daemon" not in args:
            args = ["--daemon", *args]
        cmd = [str(agent_exe), *args]
        completed = subprocess.run(cmd, cwd=str(repo_root))
        return int(completed.returncode)

    if not agent_py.exists():
        print(f"ERROR: {agent_py} not found")
        return 1

    # Fallback to running from source.
    cmd = [sys.executable, str(agent_py), *sys.argv[1:]]
    completed = subprocess.run(cmd, cwd=str(repo_root))
    return int(completed.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
