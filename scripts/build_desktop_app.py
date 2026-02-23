import argparse
import shutil
import subprocess
import sys
from pathlib import Path


def run(cmd: list[str], cwd: Path) -> None:
    print(f"[RUN] {' '.join(cmd)}")
    proc = subprocess.run(cmd, cwd=str(cwd))
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
        for path in [repo_root / "build" / "Jarvis", repo_root / "dist" / "Jarvis", repo_root / "dist" / "Jarvis.exe"]:
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
