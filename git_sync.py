# git_sync.py
import os
import subprocess
import tempfile
import shutil
import paramiko
from pathlib import Path
import re
from datetime import datetime
from utils.db import db
import hashlib

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

def get_commit_hash(repo_path: str):
    """Get the current commit hash"""
    return run("git rev-parse HEAD", cwd=repo_path)

def setup_ssh_key(ssh_key: str):
    """Configure SSH key for Git operations."""
    ssh_dir = os.path.expanduser("~/.ssh")
    os.makedirs(ssh_dir, exist_ok=True)
    
    key_path = os.path.join(ssh_dir, "id_rsa")
    with open(key_path, "w") as f:
        f.write(ssh_key)
    os.chmod(key_path, 0o600)
    
    # Add key to SSH agent
    run("eval $(ssh-agent -s) && ssh-add ~/.ssh/id_rsa", check=False)
    
    # Test SSH connection
    try:
        run("ssh -T git@github.com -o StrictHostKeyChecking=no", check=False)
    except:
        pass  # Expected to fail with "Hi username!" message

def fix_git_error(error_msg: str, repo_path: str):
    """Automatically fix common Git errors."""
    if "Permission denied (publickey)" in error_msg:
        # SSH key issue
        ssh_key = os.getenv("SSH_KEY")
        if ssh_key:
            setup_ssh_key(ssh_key)
            return True
            
    elif "refusing to merge unrelated histories" in error_msg:
        # Unrelated histories error
        run("git pull origin main --allow-unrelated-histories", cwd=repo_path)
        return True
        
    elif "please tell me who you are" in error_msg.lower():
        # Git identity not set
        name = os.getenv("GIT_USER_NAME", "Jarvis Bot")
        email = os.getenv("GIT_USER_EMAIL", "jarvis@example.com")
        run(f'git config --global user.name "{name}"', cwd=repo_path)
        run(f'git config --global user.email "{email}"', cwd=repo_path)
        return True
        
    elif re.search(r"error: failed to push some refs to", error_msg):
        # Remote has changes we don't have
        run("git fetch origin", cwd=repo_path)
        run("git rebase origin/main", cwd=repo_path)
        return True
        
    return False

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


def get_changes_summary(repo_path: str):
    """Get a summary of changes to be committed"""
    return run("git status --porcelain", cwd=repo_path)

def get_diff_stats(repo_path: str):
    """Get statistics about the changes"""
    return run("git diff --stat", cwd=repo_path)

def git_sync(repo_path=".", commit_msg="Jarvis auto-sync", max_retries=3):
    """
    Sync repository with remote, handling common errors automatically.
    
    Args:
        repo_path: Path to git repository
        commit_msg: Commit message for changes
        max_retries: Maximum number of retry attempts for failed operations
    """
    repo_path = os.path.abspath(repo_path)
    print(f"[GIT SYNC] 🚀 Starting sync in: {repo_path}")

    # Get credentials and config
    ssh_key = os.getenv("SSH_KEY")
    github_repo = os.getenv("GITHUB_REPO")
    github_token = os.getenv("GITHUB_TOKEN")
    github_user = os.getenv("GITHUB_USERNAME")
    github_pass = os.getenv("GITHUB_PASSWORD")

    if not os.path.exists(os.path.join(repo_path, ".git")):
        raise RuntimeError(f"[GIT SYNC] ❌ Not a Git repository: {repo_path}")

    # Determine best repo URL
    repo_url = None
    if ssh_key:
        if github_repo.startswith("git@"):
            repo_url = github_repo
        else:
            repo_name = github_repo.split("/")[-1]
            repo_url = f"git@github.com:{github_user}/{repo_name}.git"
    elif github_token:
        repo_url = f"https://{github_token}@github.com/{github_repo}.git"
    elif github_user and github_pass:
        repo_url = f"https://{github_user}:{github_pass}@github.com/{github_repo}.git"
    else:
        raise RuntimeError("[GIT SYNC] ❌ No valid authentication method found")

    ensure_remote(repo_path, repo_url)
    
    retry_count = 0
    while retry_count < max_retries:
        try:
            # Stage changes
            run("git add -A", cwd=repo_path)
            
            # Only commit if there are changes
            status = run("git status --porcelain", cwd=repo_path)
            if status:
                run(f'git commit -m "{commit_msg}"', cwd=repo_path)
            
            # Pull and push
            run("git pull origin main", cwd=repo_path)
            run("git push origin main", cwd=repo_path)
            
            print("[GIT SYNC] ✅ Successfully synced with remote")
            return True
            
        except Exception as e:
            error_msg = str(e)
            print(f"[GIT SYNC] ⚠️ Error: {error_msg}")
            
            if fix_git_error(error_msg, repo_path):
                retry_count += 1
                print(f"[GIT SYNC] 🔄 Attempting fix, retry {retry_count}/{max_retries}")
                continue
            
            if retry_count >= max_retries - 1:
                print("[GIT SYNC] ❌ Max retries reached, sync failed")
                raise
                
            retry_count += 1
            
    return False
            slug = github_repo.replace("https://github.com/", "")
            repo_url = f"git@github.com:{slug}"
    elif github_token:
        slug = github_repo.split("github.com/")[-1]
        repo_url = f"https://{github_token}@github.com/{slug}"
    elif github_user and github_pass:
        slug = github_repo.split("github.com/")[-1]
        repo_url = f"https://{github_user}:{github_pass}@github.com/{slug}"
    else:
        raise RuntimeError("[GIT SYNC] ❌ No credentials provided.")

    ensure_remote(repo_path, repo_url)

    # --- prepare SSH ---
    ssh_env = os.environ.copy()
    tmp_key_file = None
    if ssh_key:
        tmp_dir = tempfile.mkdtemp()
        tmp_key_file = os.path.join(tmp_dir, "id_rsa")
        with open(tmp_key_file, "w") as f:
            f.write(ssh_key)
        os.chmod(tmp_key_file, 0o600)

        # pre-trust github host key
        known_hosts = os.path.join(tmp_dir, "known_hosts")
        with open(known_hosts, "w") as kh:
            kh.write("github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMZxZy6c+oS0tzOaFQ5s0M3m8z6z8Ef3yLa2OxuO2Hx\n")
        ssh_env["GIT_SSH_COMMAND"] = f"ssh -i {tmp_key_file} -o UserKnownHostsFile={known_hosts} -o StrictHostKeyChecking=yes"
        print("[GIT SYNC] 🧩 Using SSH authentication")

    # --- do commit / push ---
    try:
        run("git add -A", cwd=repo_path, env=ssh_env)
        subprocess.run(
            f'git commit -m "{commit_msg}"',
            cwd=repo_path, shell=True, text=True,
            capture_output=True, env=ssh_env
        )
        # skip rebase if dirty
        status = run("git status --porcelain", cwd=repo_path, check=False, env=ssh_env)
        if not status.strip():
            run("git pull origin main --rebase --autostash", cwd=repo_path, env=ssh_env)
        else:
            print("[GIT SYNC] ⚠️ Skipping rebase (working tree dirty).")
        run("git push origin HEAD:main", cwd=repo_path, env=ssh_env)
        print("[GIT SYNC] ✅ Push to main successful.")
    except Exception as e:
        print(f"[GIT SYNC] ❌ SSH push failed: {e}")
        if github_token:
            print("[GIT SYNC] 🔁 Retrying via HTTPS token...")
            try:
                slug = github_repo.split("github.com/")[-1]
                https_url = f"https://{github_token}@github.com/{slug}"
                ensure_remote(repo_path, https_url)
                run("git push origin HEAD:main", cwd=repo_path)
                print("[GIT SYNC] ✅ Push via HTTPS succeeded.")
            except Exception as e2:
                print(f"[GIT SYNC] ❌ HTTPS push also failed: {e2}")
                raise
        else:
            raise
    finally:
        if tmp_key_file:
            shutil.rmtree(os.path.dirname(tmp_key_file), ignore_errors=True)

    print("[GIT SYNC] ✅ Sync complete.")
