# git_sync.py
import os
import subprocess
import tempfile
import shutil
from pathlib import Path
import re
from datetime import datetime
from src.utils.db import db

def run(cmd: str, cwd: str = ".", check=True, env=None):
    """Execute a shell command and return its output"""
    result = subprocess.run(
        cmd, cwd=cwd, shell=True, text=True,
        capture_output=True, env=env or os.environ.copy()
    )
    if check and result.returncode != 0:
        error = result.stderr.strip() or result.stdout.strip()
        db.save_git_sync(
            commit_hash=None,
            message=f"Command failed: {cmd}",
            status="error",
            details=error
        )
        raise RuntimeError(error)
    return result.stdout.strip()


def setup_git_identity():
    """Ensure Git user identity is configured."""
    name = os.getenv("GIT_USER_NAME", "Jarvis Cloud Assistant")
    email = os.getenv("GIT_USER_EMAIL", "jarvis@render.com")
    run(f'git config --global user.name "{name}"', check=False)
    run(f'git config --global user.email "{email}"', check=False)
    print(f"[GIT SYNC] ✅ Git identity set to {name} <{email}>")


def setup_ssh():
    """Setup SSH key and trust GitHub host for Render environment."""
    ssh_key = os.getenv("SSH_KEY")
    if not ssh_key:
        print("[GIT SYNC] ⚠️ No SSH_KEY found, skipping SSH setup.")
        return

    ssh_dir = os.path.expanduser("~/.ssh")
    os.makedirs(ssh_dir, exist_ok=True)
    key_path = os.path.join(ssh_dir, "id_rsa")

    with open(key_path, "w") as f:
        f.write(ssh_key)
    os.chmod(key_path, 0o600)

    # Trust GitHub host key (fixes 'Host key verification failed')
    subprocess.run("ssh-keyscan github.com >> ~/.ssh/known_hosts", shell=True, check=False)
    run("eval $(ssh-agent -s) && ssh-add ~/.ssh/id_rsa", check=False)
    print("[GIT SYNC] 🔑 SSH key added and GitHub host trusted.")


def ensure_remote(repo_path: str, repo_url: str):
    """Ensure 'origin' exists and points to correct URL."""
    try:
        current = run("git remote get-url origin", cwd=repo_path, check=False)
        if not current:
            run(f"git remote add origin {repo_url}", cwd=repo_path)
            print(f"[GIT SYNC] ✅ Added remote origin: {repo_url}")
        elif current != repo_url:
            run(f"git remote set-url origin {repo_url}", cwd=repo_path)
            print(f"[GIT SYNC] 🔄 Updated remote URL to: {repo_url}")
        else:
            print(f"[GIT SYNC] 🔗 Remote already set to: {current}")
    except Exception as e:
        print(f"[GIT SYNC] ⚠️ Could not verify remote: {e}")
        run(f"git remote add origin {repo_url}", cwd=repo_path)


def fix_git_error(error_msg: str, repo_path: str):
    """Auto-fix common Git errors."""
    if "permission denied" in error_msg.lower():
        setup_ssh()
        return True
    elif "please tell me who you are" in error_msg.lower():
        setup_git_identity()
        return True
    elif "failed to push some refs" in error_msg.lower():
        run("git fetch origin", cwd=repo_path)
        run("git rebase origin/main", cwd=repo_path)
        return True
    elif "unrelated histories" in error_msg.lower():
        run("git pull origin main --allow-unrelated-histories", cwd=repo_path)
        return True
    return False


def git_sync(repo_path=".", commit_msg="Jarvis auto-sync", max_retries=3):
    """Sync repository with remote GitHub repo using SSH or HTTPS fallback."""
    repo_path = os.path.abspath(repo_path)
    print(f"[GIT SYNC] 🚀 Starting sync in: {repo_path}")

    setup_git_identity()
    setup_ssh()

    github_repo = os.getenv("GITHUB_REPO", "")
    github_token = os.getenv("GITHUB_TOKEN")
    github_user = os.getenv("GITHUB_USERNAME")
    github_pass = os.getenv("GITHUB_PASSWORD")

    if not github_repo:
        raise RuntimeError("[GIT SYNC] ❌ GITHUB_REPO not provided.")

    # Determine remote URL
    if github_repo.startswith("git@"):
        repo_url = github_repo
    elif github_token:
        slug = github_repo.replace("https://github.com/", "").replace("github.com/", "")
        repo_url = f"https://{github_token}@github.com/{slug}.git"
    elif github_user and github_pass:
        slug = github_repo.replace("https://github.com/", "").replace("github.com/", "")
        repo_url = f"https://{github_user}:{github_pass}@github.com/{slug}.git"
    else:
        repo_url = f"git@github.com:{github_repo}.git"

    ensure_remote(repo_path, repo_url)

    retry_count = 0
    while retry_count < max_retries:
        try:
            run("git add -A", cwd=repo_path)
            status = run("git status --porcelain", cwd=repo_path)
            if status:
                run(f'git commit -m "{commit_msg}"', cwd=repo_path)

            run("git pull origin main --rebase --autostash", cwd=repo_path)
            run("git push origin main", cwd=repo_path)
            print("[GIT SYNC] ✅ Successfully synced with remote.")
            return True

        except Exception as e:
            err = str(e)
            print(f"[GIT SYNC] ⚠️ Error: {err}")

            if fix_git_error(err, repo_path):
                retry_count += 1
                print(f"[GIT SYNC] 🔄 Retrying {retry_count}/{max_retries}...")
                continue

            if github_token and "Host key verification failed" in err:
                # Fallback to HTTPS
                https_url = repo_url.replace("git@github.com:", f"https://{github_token}@github.com/")
                ensure_remote(repo_path, https_url)
                run("git push origin main", cwd=repo_path)
                print("[GIT SYNC] 🔁 HTTPS fallback push successful.")
                return True

            if retry_count >= max_retries - 1:
                print("[GIT SYNC] ❌ Max retries reached. Sync failed.")
                raise
            retry_count += 1

    return False
