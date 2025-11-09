import os
import subprocess
import tempfile
import logging
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="[GIT SYNC] %(message)s")


def run_cmd(cmd, cwd=None, env=None, raise_on_fail=True):
    """Run a shell command and handle output cleanly."""
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            env=env or os.environ.copy(),
            shell=True,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            if raise_on_fail:
                logging.error(f"❌ Command failed: {cmd}")
                logging.error(result.stderr.strip())
                raise RuntimeError(result.stderr.strip())
            else:
                logging.warning(f"⚠️ Command returned {result.returncode}: {cmd}")
        return result.stdout.strip()
    except Exception as e:
        if raise_on_fail:
            raise
        logging.warning(f"Command failed: {e}")
        return ""


def setup_ssh():
    """Write SSH private key from env into temp file for git auth."""
    ssh_key = os.getenv("SSH_KEY")
    if not ssh_key:
        logging.warning("No SSH_KEY found in environment.")
        return None

    ssh_dir = Path(tempfile.gettempdir()) / "ssh_temp"
    ssh_dir.mkdir(exist_ok=True)
    key_path = ssh_dir / "id_rsa"
    with open(key_path, "w") as f:
        f.write(ssh_key.strip() + "\n")
    os.chmod(key_path, 0o600)

    wrapper_path = ssh_dir / "ssh_wrapper.sh"
    wrapper_path.write_text(f"#!/bin/sh\nexec ssh -i {key_path} -o StrictHostKeyChecking=no \"$@\"\n")
    os.chmod(wrapper_path, 0o700)

    logging.info("🔐 SSH authentication configured.")
    return str(wrapper_path)


def git_sync(repo_path="."):
    """Auto-commit and push all changes to the main branch."""
    repo_path = Path(repo_path).resolve()
    logging.info(f"📦 Syncing repo at {repo_path}")
    env = os.environ.copy()

    # Set up SSH first
    ssh_wrapper = setup_ssh()
    if ssh_wrapper:
        env["GIT_SSH"] = ssh_wrapper

    # Configure user
    username = os.getenv("GITHUB_USERNAME", "Jarvis-AutoBot")
    email = f"{username}@users.noreply.github.com"
    run_cmd(f"git config user.name '{username}'", cwd=repo_path, env=env, raise_on_fail=False)
    run_cmd(f"git config user.email '{email}'", cwd=repo_path, env=env, raise_on_fail=False)

    # Ensure we’re on the main branch
    try:
        run_cmd("git checkout main", cwd=repo_path, env=env)
    except Exception:
        logging.warning("Branch 'main' not found, staying on current branch.")

    # Stage all changes
    run_cmd("git add -A", cwd=repo_path, env=env)

    # Commit if needed
    try:
        run_cmd("git commit -m 'Auto-sync update from Jarvis'", cwd=repo_path, env=env)
        logging.info("✅ Commit created.")
    except Exception:
        logging.info("🟢 No changes to commit.")

    # Pull latest main before pushing
    try:
        run_cmd("git fetch origin main", cwd=repo_path, env=env)
        run_cmd("git pull origin main --rebase", cwd=repo_path, env=env)
    except Exception as e:
        logging.warning(f"⚠️ Pull error: {e}. Trying normal merge.")
        run_cmd("git pull origin main --no-rebase", cwd=repo_path, env=env, raise_on_fail=False)

    # Try to push with SSH
    try:
        logging.info("🚀 Pushing to main via SSH...")
        run_cmd("git push origin HEAD:main", cwd=repo_path, env=env)
        logging.info("✅ Successfully pushed to main (SSH).")
        return
    except Exception as e:
        logging.warning(f"SSH push failed: {e}")

    # Fallback 1: GitHub token HTTPS
    token = os.getenv("GITHUB_TOKEN")
    if token:
        logging.info("🔁 Retrying push via HTTPS (GitHub token)...")
        origin_url = run_cmd("git remote get-url origin", cwd=repo_path, env=env)
        if "github.com" in origin_url:
            https_url = origin_url.replace("git@github.com:", f"https://{token}@github.com/")
            run_cmd(f"git remote set-url origin {https_url}", cwd=repo_path, env=env)
        run_cmd("git push origin HEAD:main", cwd=repo_path, env=env)
        logging.info("✅ Successfully pushed to main via HTTPS token.")
        return

    # Fallback 2: Username/password HTTPS
    user = os.getenv("GITHUB_USERNAME")
    password = os.getenv("GITHUB_PASSWORD")
    if user and password:
        logging.info("🔁 Retrying push via HTTPS (username/password)...")
        origin_url = run_cmd("git remote get-url origin", cwd=repo_path, env=env)
        if "github.com" in origin_url:
            https_url = origin_url.replace("git@github.com:", f"https://{user}:{password}@github.com/")
            run_cmd(f"git remote set-url origin {https_url}", cwd=repo_path, env=env)
        run_cmd("git push origin HEAD:main", cwd=repo_path, env=env)
        logging.info("✅ Successfully pushed to main via HTTPS (username/password).")
        return

    logging.error("❌ All push attempts failed (SSH, token, username/password).")
    raise RuntimeError("Push failed")


if __name__ == "__main__":
    try:
        git_sync()
    except Exception as e:
        logging.error(f"🚫 Git sync failed: {e}")
