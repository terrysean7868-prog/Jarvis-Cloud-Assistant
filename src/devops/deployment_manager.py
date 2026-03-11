from __future__ import annotations

from pathlib import Path
import os
import subprocess
from typing import Any


class DeploymentManager:
    """Local devops helper for build/deploy artifacts."""

    def __init__(self, project_root: Path | None = None):
        self.project_root = project_root or Path(__file__).resolve().parents[2]

    def ensure_dockerfile(self, app_type: str = "python-fastapi") -> dict:
        dockerfile = self.project_root / "Dockerfile"
        if dockerfile.exists():
            return {"status": "exists", "path": str(dockerfile)}

        if app_type == "react":
            content = (
                "FROM node:20-alpine\n"
                "WORKDIR /app\n"
                "COPY package*.json ./\n"
                "RUN npm ci\n"
                "COPY . .\n"
                "RUN npm run build\n"
                "CMD [\"npm\", \"run\", \"start\"]\n"
            )
        else:
            content = (
                "FROM python:3.11-slim\n"
                "WORKDIR /app\n"
                "COPY requirements.txt ./\n"
                "RUN pip install --no-cache-dir -r requirements.txt\n"
                "COPY . .\n"
                "CMD [\"uvicorn\", \"apps.web.app:app\", \"--host\", \"0.0.0.0\", \"--port\", \"8000\"]\n"
            )

        dockerfile.write_text(content, encoding="utf-8")
        return {"status": "created", "path": str(dockerfile)}

    def run_build_script(self, build_script: str) -> dict[str, Any]:
        script = (build_script or "").strip()
        if not script:
            return {"status": "skipped", "message": "No build script requested"}

        try:
            proc = subprocess.run(
                script,
                cwd=str(self.project_root),
                shell=True,
                capture_output=True,
                text=True,
                timeout=180,
            )
            return {
                "status": "success" if proc.returncode == 0 else "error",
                "code": proc.returncode,
                "stdout": (proc.stdout or "")[:3000],
                "stderr": (proc.stderr or "")[:3000],
            }
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    def prepare_environment(self, env_values: dict[str, Any]) -> dict[str, Any]:
        if not env_values:
            return {"status": "skipped", "message": "No environment changes requested"}

        env_file = self.project_root / ".env.autonomy"
        try:
            lines = [f"{k}={v}" for k, v in env_values.items() if isinstance(k, str) and k.strip()]
            env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return {"status": "success", "path": str(env_file), "count": len(lines)}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    def deploy_local(self, mode: str = "dry_run") -> dict[str, Any]:
        m = (mode or "dry_run").strip().lower()
        if m == "dry_run":
            return {"status": "success", "mode": "dry_run", "message": "Local deployment plan generated"}

        if m == "docker":
            dockerfile = self.project_root / "Dockerfile"
            if not dockerfile.exists():
                self.ensure_dockerfile()
            cmd = "docker build -t jarvis-autonomy-local ."
        else:
            cmd = "python run_local.py"

        try:
            proc = subprocess.run(
                cmd,
                cwd=str(self.project_root),
                shell=True,
                capture_output=True,
                text=True,
                timeout=180,
                env=os.environ.copy(),
            )
            return {
                "status": "success" if proc.returncode == 0 else "error",
                "mode": m,
                "code": proc.returncode,
                "stdout": (proc.stdout or "")[:3000],
                "stderr": (proc.stderr or "")[:3000],
            }
        except Exception as exc:
            return {"status": "error", "mode": m, "message": str(exc)}
