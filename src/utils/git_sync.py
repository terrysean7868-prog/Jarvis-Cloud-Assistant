# ==============================================================
# git_sync.py — Secure GitHub Auto Sync via SSH for Render
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
# 🔐 SSH Trust Setup (fixes 'Host key verification failed')
# ==============================================================

def setup_ssh_trust():
    """
    Preconfigure SSH to trust github.com and load SSH key into the agent.
    Prevents 'Host key verification failed' errors on Render.
    """
    ssh_key = os.getenv("SSH_KEY")
    if not ssh_key:
        print("[SSH INIT] ⚠️ No SSH_KEY found in environment — skipping SSH setup.")
        return

    ssh_dir = os.path.expanduser("~/.ssh")
    os.makedirs(ssh_dir, exist_ok=True)
    key_path = os.path.join(ssh_dir, "id_rsa")

    # Write private key securely
    with open(key_path, "w") as f:
        f.write(ssh_key)
    os.chmod(key_path, stat.S_IRUSR | stat.S_IWUSR)

    # Trust GitHub SSH host
    known_hosts_path = os.path.join(ssh_dir, "known_hosts")
    github_key = "github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMZxZy6c+oS0tzOaFQ5s0M3m8z6z8Ef3yLa2OxuO2Hx\n"
    with open(known_hosts_path, "w") as kh:
        kh.write(github_key)

    # Start ssh-agent and add key
    try:
        subprocess.run("eval $(ssh-agent -s)", shell=True, check=False)
        subprocess.run(f"ssh-add {key_path}", shell=True, check=False)
        print("[SSH INIT] 🔑 SSH key added and github.com trusted.")
    except Exception as e:
        print(f"[SSH INIT] ⚠️ SSH setup failed: {e}")

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
# 🚀 Git Sync Function (SSH-only, Render-safe)
# ==============================================================

def git_sync(repo_path=".", commit_msg="Jarvis auto-sync", max_retries=3):
    """
    Securely syncs the repository with the GitHub main branch using SSH authentication.
    Fully compatible with Render deployment environments.
    """
    repo_path = os.path.abspath(repo_path)
    print(f"[GIT SYNC] 🚀 Starting sync in: {repo_path}")

    # --- Environment ---
    ssh_key = os.getenv("SSH_KEY")
    github_repo = os.getenv("GITHUB_REPO")
    github_user = os.getenv("GITHUB_USERNAME")
    git_name = os.getenv("GIT_USER_NAME", "Jarvis Cloud Assistant")
    git_email = os.getenv("GIT_USER_EMAIL", "jarvis@render.com")

    if not ssh_key:
        raise RuntimeError("[GIT SYNC] ❌ SSH_KEY not set in environment.")

    if not github_repo or not github_user:
        raise RuntimeError("[GIT SYNC] ❌ Missing GITHUB_REPO or GITHUB_USERNAME.")

    # --- Ensure Git identity ---
    run(f'git config --global user.name "{git_name}"', cwd=repo_path)
    run(f'git config --global user.email "{git_email}"', cwd=repo_path)
    print(f"[GIT SYNC] ✅ Git identity set to {git_name} <{git_email}>")

    # --- Construct SSH URL ---
    repo_slug = github_repo.split("/")[-1].replace(".git", "")
    repo_url = f"git@github.com:{github_user}/{repo_slug}.git"
    ensure_remote(repo_path, repo_url)
    print(f"[GIT SYNC] 🧩 Using SSH authentication ({repo_url})")

    # --- Create temporary SSH environment ---
    tmp_dir = tempfile.mkdtemp()
    key_file = os.path.join(tmp_dir, "id_rsa")
    with open(key_file, "w") as f:
        f.write(ssh_key)
    os.chmod(key_file, 0o600)

    known_hosts = os.path.join(tmp_dir, "known_hosts")
    with open(known_hosts, "w") as kh:
        kh.write("github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMZxZy6c+oS0tzOaFQ5s0M3m8z6z8Ef3yLa2OxuO2Hx\n")

    ssh_env = os.environ.copy()
    ssh_env["GIT_SSH_COMMAND"] = f"ssh -i {key_file} -o UserKnownHostsFile={known_hosts} -o StrictHostKeyChecking=yes"

    # --- Perform sync ---
    success = False
    try:
        for attempt in range(1, max_retries + 1):
            print(f"[GIT SYNC] 🔄 Attempt {attempt}/{max_retries}...")

            try:
                # Stage and commit
                run("git add -A", cwd=repo_path, env=ssh_env)
                status = run("git status --porcelain", cwd=repo_path, env=ssh_env)
                if status.strip():
                    run(f'git commit -m "{commit_msg}"', cwd=repo_path, env=ssh_env)
                    print("[GIT SYNC] 🧾 Changes committed locally.")
                else:
                    print("[GIT SYNC] 💤 No changes to commit.")

                # Pull and push
                run("git pull origin main --rebase --autostash", cwd=repo_path, env=ssh_env)
                run("git push origin HEAD:main", cwd=repo_path, env=ssh_env)
                print("[GIT SYNC] ✅ Push to main successful.")
                success = True
                break

            except Exception as e:
                print(f"[GIT SYNC] ⚠️ Error during sync attempt {attempt}: {e}")
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
