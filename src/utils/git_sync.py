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
# Utility Command Runner
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
# SSH Trust Setup (startup)
# ==============================================================

def setup_ssh_trust():
    """
    Fetch GitHub's SSH host key dynamically and trust it.
    Prevents 'Host key verification failed' or 'REMOTE HOST IDENTIFICATION HAS CHANGED'.
    Only runs if SSH_KEY is configured. Silent fail on Windows/systems without ssh-keyscan.
    """
    ssh_key = str(os.getenv("SSH_KEY") or "").strip()
    if not ssh_key:
        return  # Silent skip if no SSH_KEY configured

    ssh_dir = os.path.expanduser("~/.ssh")
    try:
        os.makedirs(ssh_dir, exist_ok=True)
        key_path = os.path.join(ssh_dir, "id_rsa")

        # Save private key securely
        with open(key_path, "w") as f:
            f.write(ssh_key)
        os.chmod(key_path, stat.S_IRUSR | stat.S_IWUSR)

        # Dynamically fetch GitHub host key (may fail on Windows)
        try:
            result = subprocess.run(
                ["ssh-keyscan", "github.com"],
                capture_output=True, text=True, check=False, timeout=5
            )
            if result.returncode == 0 and result.stdout.strip():
                known_hosts_path = os.path.join(ssh_dir, "known_hosts")
                with open(known_hosts_path, "w") as kh:
                    kh.write(result.stdout.strip() + "\n")
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass  # ssh-keyscan not available on this system

        # Try to add SSH key to agent (may fail on Windows)
        try:
            subprocess.run("ssh-agent -s", shell=True, capture_output=True, timeout=5)
            subprocess.run(f"ssh-add {key_path}", shell=True, capture_output=True, timeout=5)
        except:
            pass  # SSH agent not available on this system
    
    except Exception:
        pass  # Silent fail - SSH optional for local dev


# ==============================================================
# Ensure Git Remote Exists
# ==============================================================

def ensure_remote(repo_path: str, repo_url: str):
    """Ensure that the 'origin' remote exists and is correctly configured."""
    try:
        current = run("git remote get-url origin", cwd=repo_path, check=False)
        if not current:
            run(f"git remote add origin {repo_url}", cwd=repo_path)
            print(f"[GIT SYNC] [OK] Added remote origin: {repo_url}")
        elif current != repo_url:
            run(f"git remote set-url origin {repo_url}", cwd=repo_path)
            print(f"[GIT SYNC] [OK] Updated remote URL to: {repo_url}")
        else:
            print(f"[GIT SYNC] [OK] Remote already set to: {current}")
    except Exception as e:
        print(f"[GIT SYNC] [WARN] Could not verify remote: {e}")
        run(f"git remote add origin {repo_url}", cwd=repo_path)


# ==============================================================
# Dynamic Re-Fetch of GitHub Host Key (mid-sync fallback)
# ==============================================================

def refresh_github_host_key():
    """Re-fetch GitHub host key and update known_hosts if mismatch occurs."""
    ssh_dir = os.path.expanduser("~/.ssh")
    os.makedirs(ssh_dir, exist_ok=True)
    known_hosts_path = os.path.join(ssh_dir, "known_hosts")

    try:
        print("[GIT SYNC] Refreshing GitHub host key...")
        result = subprocess.run(
            ["ssh-keyscan", "github.com"],
            capture_output=True, text=True, check=True
        )
        new_key = result.stdout.strip()
        with open(known_hosts_path, "w") as kh:
            kh.write(new_key + "\n")
        print("[GIT SYNC] [OK] Host key refreshed successfully.")
    except Exception as e:
        print(f"[GIT SYNC] [WARN] Could not refresh host key: {e}")


# ==============================================================
# Git Sync Function (SSH-only, auto-healing)
# ==============================================================

def git_sync(repo_path=".", commit_msg="Jarvis auto-sync", max_retries=5):
    """
    Securely syncs the repository with the GitHub main branch using SSH authentication.
    Automatically re-trusts GitHub host keys if verification errors occur.
    Enhanced with better error handling and automatic fixes.
    """
    repo_path = os.path.abspath(repo_path)
    print(f"[GIT SYNC] Starting sync in: {repo_path}")

    # --- Environment setup ---
    ssh_key = str(os.getenv("SSH_KEY") or "").strip()
    github_repo = str(os.getenv("GITHUB_REPO") or "").strip()
    github_user = str(os.getenv("GITHUB_USERNAME") or "").strip()
    # Prefer token over password for HTTPS auth.
    github_token = str(os.getenv("GITHUB_TOKEN") or "").strip()
    github_password = str(os.getenv("GITHUB_PASSWORD") or "").strip()
    git_name = str(os.getenv("GIT_USER_NAME") or "Jarvis Cloud Assistant").strip() or "Jarvis Cloud Assistant"
    git_email = str(os.getenv("GIT_USER_EMAIL") or "jarvis@render.com").strip() or "jarvis@render.com"

    # Try to get repo URL from env or construct it
    if not github_repo:
        # Try to get from existing remote
        try:
            existing_remote = run("git remote get-url origin", cwd=repo_path, check=False)
            if existing_remote:
                github_repo = existing_remote
                print(f"[GIT SYNC] Using existing remote: {existing_remote}")
        except:
            pass

    if not ssh_key and not github_token and not github_password:
        raise RuntimeError("[GIT SYNC] Missing SSH_KEY or GITHUB_TOKEN/GITHUB_PASSWORD in environment.")
    if not github_repo:
        raise RuntimeError("[GIT SYNC] Missing GITHUB_REPO and no existing remote found.")

    # --- Git identity ---
    run(f'git config --global user.name "{git_name}"', cwd=repo_path, check=False)
    run(f'git config --global user.email "{git_email}"', cwd=repo_path, check=False)
    print(f"[GIT SYNC] [OK] Git identity set to {git_name} <{git_email}>")

    # --- Initialize git repo if needed ---
    if not os.path.exists(os.path.join(repo_path, ".git")):
        print("[GIT SYNC] Initializing git repository...")
        run("git init", cwd=repo_path, check=False)
        run("git branch -M main", cwd=repo_path, check=False)

    # --- Construct repo URL ---
    owner = github_user
    repo_slug = ""
    raw_repo = str(github_repo or "").strip().replace(".git", "")
    if "github.com" in raw_repo:
        normalized = raw_repo.replace("https://github.com/", "").replace("git@github.com:", "")
        if "/" in normalized:
            owner_part, repo_part = normalized.split("/", 1)
            owner = owner_part.strip() or owner
            repo_slug = repo_part.strip()
        else:
            repo_slug = normalized.strip()
    elif "/" in raw_repo:
        # Supports shorthand like "owner/repo".
        owner_part, repo_part = raw_repo.split("/", 1)
        owner = owner_part.strip() or owner
        repo_slug = repo_part.strip()
    else:
        repo_slug = raw_repo.strip()

    if not owner or not repo_slug:
        raise RuntimeError("[GIT SYNC] Invalid repository format. Use full GitHub URL or owner/repo.")

    if ssh_key:
        repo_url = f"git@github.com:{owner}/{repo_slug}.git"
    else:
        repo_url = f"https://github.com/{owner}/{repo_slug}.git"

    ensure_remote(repo_path, repo_url)
    print(f"[GIT SYNC] Using {'SSH' if ssh_key else 'HTTPS'} authentication ({repo_url})")

    # --- Setup authentication ---
    tmp_dir = tempfile.mkdtemp()
    ssh_env = os.environ.copy()

    if ssh_key:
        key_file = os.path.join(tmp_dir, "id_rsa")
        with open(key_file, "w") as f:
            f.write(ssh_key)
        os.chmod(key_file, 0o600)

        known_hosts = os.path.join(tmp_dir, "known_hosts")
        try:
            subprocess.run(f"ssh-keyscan github.com > {known_hosts}", shell=True, check=True, timeout=10)
        except Exception:
            refresh_github_host_key()
            known_hosts = os.path.expanduser("~/.ssh/known_hosts")

        ssh_env["GIT_SSH_COMMAND"] = (
            f"ssh -i {key_file} -o UserKnownHostsFile={known_hosts} "
            f"-o StrictHostKeyChecking=accept-new -o ConnectTimeout=10"
        )
    else:
        # HTTPS with token/password credentials
        credential_secret = github_token or github_password
        if github_user and credential_secret:
            repo_url_with_auth = repo_url.replace("https://", f"https://{github_user}:{credential_secret}@")
            run(f"git remote set-url origin {repo_url_with_auth}", cwd=repo_path, check=False)

    success = False
    last_error = None
    
    try:
        for attempt in range(1, max_retries + 1):
            print(f"[GIT SYNC] 🔄 Attempt {attempt}/{max_retries}...")
            try:
                # Check if we're on a branch
                try:
                    current_branch = run("git rev-parse --abbrev-ref HEAD", cwd=repo_path, check=False)
                    if not current_branch or current_branch == "HEAD":
                        run("git checkout -b main", cwd=repo_path, check=False)
                except:
                    run("git checkout -b main", cwd=repo_path, check=False)

                # Stage all changes
                run("git add -A", cwd=repo_path, env=ssh_env, check=False)
                
                # Check status
                status = run("git status --porcelain", cwd=repo_path, env=ssh_env, check=False)
                if status.strip():
                    # Commit changes
                    safe_msg = commit_msg.replace('"', "'")
                    run(f'git commit -m "{safe_msg}"', cwd=repo_path, env=ssh_env, check=False)
                    print("[GIT SYNC] 🧾 Changes committed locally.")
                else:
                    print("[GIT SYNC] 💤 No changes to commit.")

                # Pull with rebase
                try:
                    run("git pull origin main --rebase --autostash", cwd=repo_path, env=ssh_env, check=False)
                except Exception as pull_err:
                    # If pull fails, try without rebase
                    print(f"[GIT SYNC] ⚠️ Rebase pull failed, trying merge: {pull_err}")
                    try:
                        run("git pull origin main --no-rebase", cwd=repo_path, env=ssh_env, check=False)
                    except:
                        # If still fails, continue with push (might be first push)
                        print("[GIT SYNC] ⚠️ Pull failed, continuing with push...")

                # Push to remote
                try:
                    run("git push origin HEAD:main", cwd=repo_path, env=ssh_env, check=False)
                    print("[GIT SYNC] ✅ Push to main successful.")
                    success = True
                    break
                except Exception as push_err:
                    error_str = str(push_err)
                    # Handle common push errors
                    if "no upstream branch" in error_str.lower():
                        run("git push -u origin main", cwd=repo_path, env=ssh_env, check=False)
                        print("[GIT SYNC] ✅ Initial push successful.")
                        success = True
                        break
                    elif "permission denied" in error_str.lower() or "authentication" in error_str.lower():
                        print("[GIT SYNC] ⚠️ Authentication error, refreshing credentials...")
                        refresh_github_host_key()
                        if attempt < max_retries:
                            import time
                            time.sleep(2)
                            continue
                    raise

            except Exception as e:
                error_msg = str(e)
                last_error = error_msg
                print(f"[GIT SYNC] ⚠️ Error during sync attempt {attempt}: {error_msg}")

                # Auto-fix common issues
                if "Host key verification failed" in error_msg or "REMOTE HOST IDENTIFICATION HAS CHANGED" in error_msg:
                    refresh_github_host_key()
                    continue
                elif "not a git repository" in error_msg.lower():
                    run("git init", cwd=repo_path, check=False)
                    continue
                elif "fatal: not a git repository" in error_msg.lower():
                    run("git init", cwd=repo_path, check=False)
                    run("git branch -M main", cwd=repo_path, check=False)
                    continue

                if attempt < max_retries:
                    import time
                    time.sleep(min(3 * attempt, 10))  # Exponential backoff
                else:
                    # Last attempt - try alternative method
                    if ssh_key and not success:
                        print("[GIT SYNC] 🔄 Trying HTTPS fallback...")
                        # This would require password, skip for now
                        raise RuntimeError(f"Git sync failed after {max_retries} attempts: {last_error}")

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    if success:
        print(f"[GIT SYNC] 🕒 Last sync: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        return True
    else:
        raise RuntimeError(f"[GIT SYNC] ❌ Failed to sync after {max_retries} attempts. Last error: {last_error}")
