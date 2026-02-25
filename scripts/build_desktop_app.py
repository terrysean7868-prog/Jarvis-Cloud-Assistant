import argparse
import shutil
import subprocess
import sys
from pathlib import Path


def resolve_executable(name: str) -> str:
    if sys.platform.startswith("win"):
        if name.lower() == "npm":
            npm_cmd = shutil.which("npm.cmd")
            if npm_cmd:
                return npm_cmd
        if name.lower() == "pyinstaller":
            pyinstaller_exe = shutil.which("pyinstaller.exe")
            if pyinstaller_exe:
                return pyinstaller_exe

    resolved = shutil.which(name)
    if resolved:
        return resolved

    raise FileNotFoundError(f"Executable not found in PATH: {name}")


def run(cmd: list[str], cwd: Path) -> None:
    executable = resolve_executable(cmd[0])
    safe_cmd = [executable, *cmd[1:]]
    print(f"[RUN] {' '.join(cmd)}")
    try:
        proc = subprocess.run(safe_cmd, cwd=str(cwd))
    except FileNotFoundError:
        raise SystemExit(
            f"Required command not found: {cmd[0]}\n"
            "Install it and ensure it is in PATH, then re-run the build."
        )
    if proc.returncode != 0:
        raise SystemExit(proc.returncode)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build latest Jarvis Desktop app")
    parser.add_argument("--skip-frontend", action="store_true", help="Skip frontend production build")
    parser.add_argument("--install-frontend", action="store_true", help="Run npm install before frontend build")
    parser.add_argument("--skip-clean", action="store_true", help="Skip cleaning build/dist folders")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
    frontend_dir = repo_root / "jarvis-frontend"
    spec_file = repo_root / "JarvisDesktop.spec"

    if not spec_file.exists():
        raise SystemExit(f"Spec file not found: {spec_file}")

    if not args.skip_frontend:
        if args.install_frontend or not (frontend_dir / "node_modules").exists():
            run(["npm", "install"], cwd=frontend_dir)
        run(["npm", "run", "build"], cwd=frontend_dir)

    if not args.skip_clean:
        for path in [
            repo_root / "build" / "Jarvis",
            repo_root / "build" / "JarvisDesktop",
            repo_root / "dist" / "Jarvis",
            repo_root / "dist" / "JarvisDesktop",
            repo_root / "dist" / "Jarvis.exe",
            repo_root / "dist" / "JarvisDesktop.exe",
        ]:
            try:
                if path.is_dir():
                    shutil.rmtree(path, ignore_errors=True)
                elif path.exists():
                    path.unlink(missing_ok=True)
            except Exception:
                pass

    run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", str(spec_file.name)], cwd=repo_root)

    exe_candidates = [repo_root / "dist" / "Jarvis.exe", repo_root / "dist" / "Jarvis" / "Jarvis.exe"]
    out = next((p for p in exe_candidates if p.exists()), None)
    if out:
        print(f"[OK] Desktop build completed: {out}")
    else:
        print("[OK] Desktop build completed. Check dist/ for output.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
