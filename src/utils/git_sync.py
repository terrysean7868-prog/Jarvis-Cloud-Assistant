# ==============================================================
# git_sync.py — Self-Healing GitHub Auto Sync via SSH (Render-ready)
# ==============================================================

import os
import subprocess
import tempfile
import shutil
import stat
import re
from datetime import datetime
from pathlib import Path


# ==============================================================
# 🧠 Utility Command Runner
# ==============================================================

def run(cmd: str, cwd: str = ".", check=True, env=None):
    """Execute a shell command and return its output."""
    result = subprocess.run(
        cmd, cwd=cwd, shell=True, text=True,
        capture_output=True, env=env or os.environ.copy()
    )
    if check and result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result.stdout.strip()


# ==============================================================
# 🔐 SSH Trust Setup (startup)
# ==============================================================

def setup_ssh_trust():
    """
    Fetch GitHub's SSH host key dynamically and trust it.
    Prevents 'Host key verification failed' or 'REMOTE HOST IDENTIFICATION HAS CHANGED'.
    """
    ssh_key = os.getenv("SSH_KEY")
    if not ssh_key:
        print("[SSH INIT] ⚠️ No SSH_KEY found in environment — skipping SSH setup.")
        return

    ssh_dir = os.path.expanduser("~/.ssh")
    os.makedirs(ssh_dir, exist_ok=True)
    key_path = os.path.join(ssh_dir, "id_rsa")

    # Save private key securely
    with open(key_path, "w") as f:
        f.write(ssh_key)
    os.chmod(key_path, stat.S_IRUSR | stat.S_IWUSR)

    # Dynamically fetch GitHub host key
    try:
        result = subprocess.run(
            ["ssh-keyscan", "github.com"],
            capture_output=True, text=True, check=True
        )
        github_host_key = result.stdout.strip()
        if github_host_key:
            known_hosts_path = os.path.join(ssh_dir, "known_hosts")
            with open(known_hosts_path, "w") as kh:
                kh.write(github_host_key + "\n")
            print("[SSH INIT] ✅ GitHub host key fetched dynamically.")
        else:
            print("[SSH INIT] ⚠️ ssh-keyscan returned no output.")
    except Exception as e:
        print(f"[SSH INIT] ⚠️ ssh-keyscan failed: {e}")

    # Start ssh-agent and add key
    try:
        subprocess.run("eval $(ssh-agent -s)", shell=True, check=False)
        subprocess.run(f"ssh-add {key_path}", shell=True, check=False)
        print("[SSH INIT] 🔑 SSH key added and GitHub trusted.")
    except Exception as e:
        print(f"[SSH INIT] ⚠️ SSH agent setup failed: {e}")


# ==============================================================
# 🧩 Ensure Git Remote Exists
# ==============================================================

def ensure_remote(repo_path: str, repo_url: str):
    """Ensure that the 'origin' remote exists and is correctly configured."""
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


# ==============================================================
# 🧩 Dynamic Re-Fetch of GitHub Host Key (mid-sync fallback)
# ==============================================================

def refresh_github_host_key():
    """Re-fetch GitHub host key and update known_hosts if mismatch occurs."""
    ssh_dir = os.path.expanduser("~/.ssh")
    os.makedirs(ssh_dir, exist_ok=True)
    known_hosts_path = os.path.join(ssh_dir, "known_hosts")

    try:
        print("[GIT SYNC] 🔁 Refreshing GitHub host key...")
        result = subprocess.run(
            ["ssh-keyscan", "github.com"],
            capture_output=True, text=True, check=True
        )
        new_key = result.stdout.strip()
        with open(known_hosts_path, "w") as kh:
            kh.write(new_key + "\n")
        print("[GIT SYNC] ✅ Host key refreshed successfully.")
    except Exception as e:
        print(f"[GIT SYNC] ⚠️ Could not refresh host key: {e}")


# ==============================================================
# 🚀 Git Sync Function (SSH-only, auto-healing)
# ==============================================================

def git_sync(repo_path=".", commit_msg="Jarvis auto-sync", max_retries=3):
    """
    Securely syncs the repository with the GitHub main branch using SSH authentication.
    Automatically re-trusts GitHub host keys if verification errors occur.
    """
    repo_path = os.path.abspath(repo_path)
    print(f"[GIT SYNC] 🚀 Starting sync in: {repo_path}")

    # --- Environment setup ---
    ssh_key = os.getenv("SSH_KEY")
    github_repo = os.getenv("GITHUB_REPO")
    github_user = os.getenv("GITHUB_USERNAME")
    git_name = os.getenv("GIT_USER_NAME", "Jarvis Cloud Assistant")
    git_email = os.getenv("GIT_USER_EMAIL", "jarvis@render.com")

    if not ssh_key:
        raise RuntimeError("[GIT SYNC] ❌ SSH_KEY not set in environment.")
    if not github_repo or not github_user:
        raise RuntimeError("[GIT SYNC] ❌ Missing GITHUB_REPO or GITHUB_USERNAME.")

    # --- Git identity ---
    run(f'git config --global user.name "{git_name}"', cwd=repo_path)
    run(f'git config --global user.email "{git_email}"', cwd=repo_path)
    print(f"[GIT SYNC] ✅ Git identity set to {git_name} <{git_email}>")

    # --- Construct SSH URL ---
    repo_slug = github_repo.split("/")[-1].replace(".git", "")
    repo_url = f"git@github.com:{github_user}/{repo_slug}.git"
    ensure_remote(repo_path, repo_url)
    print(f"[GIT SYNC] 🧩 Using SSH authentication ({repo_url})")

    # --- Temporary SSH environment ---
    tmp_dir = tempfile.mkdtemp()
    key_file = os.path.join(tmp_dir, "id_rsa")
    with open(key_file, "w") as f:
        f.write(ssh_key)
    os.chmod(key_file, 0o600)

    known_hosts = os.path.join(tmp_dir, "known_hosts")
    # Always fetch current GitHub host key dynamically
    try:
        subprocess.run(f"ssh-keyscan github.com > {known_hosts}", shell=True, check=True)
    except Exception:
        refresh_github_host_key()

    ssh_env = os.environ.copy()
    ssh_env["GIT_SSH_COMMAND"] = (
        f"ssh -i {key_file} -o UserKnownHostsFile={known_hosts} -o StrictHostKeyChecking=yes"
    )

    success = False
    try:
        for attempt in range(1, max_retries + 1):
            print(f"[GIT SYNC] 🔄 Attempt {attempt}/{max_retries}...")
            try:
                run("git add -A", cwd=repo_path, env=ssh_env)
                status = run("git status --porcelain", cwd=repo_path, env=ssh_env)
                if status.strip():
                    run(f'git commit -m "{commit_msg}"', cwd=repo_path, env=ssh_env)
                    print("[GIT SYNC] 🧾 Changes committed locally.")
                else:
                    print("[GIT SYNC] 💤 No changes to commit.")

                run("git pull origin main --rebase --autostash", cwd=repo_path, env=ssh_env)
                run("git push origin HEAD:main", cwd=repo_path, env=ssh_env)
                print("[GIT SYNC] ✅ Push to main successful.")
                success = True
                break

            except Exception as e:
                error_msg = str(e)
                print(f"[GIT SYNC] ⚠️ Error during sync attempt {attempt}: {error_msg}")

                # If host key verification failed, refresh keys and retry
                if ("Host key verification failed" in error_msg or
                        "REMOTE HOST IDENTIFICATION HAS CHANGED" in error_msg):
                    refresh_github_host_key()
                    continue

                if attempt < max_retries:
                    import time; time.sleep(3)
                else:
                    raise

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    if success:
        print(f"[GIT SYNC] 🕒 Last sync: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        return True
    else:
        raise RuntimeError("[GIT SYNC] ❌ Failed to sync after retries.")
