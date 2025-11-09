# git_sync.py
import os
import subprocess
import tempfile
import shutil


def run(cmd: str, cwd: str = ".", check=True):
    """Run a shell command safely and capture output."""
    result = subprocess.run(
        cmd,
        cwd=cwd,
        shell=True,
        text=True,
        capture_output=True
    )
    if check and result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result.stdout.strip()


def ensure_remote(repo_path: str, repo_url: str):
    """Ensure 'origin' exists and points to correct URL."""
    try:
        current = run("git remote get-url origin", cwd=repo_path, check=False)
        if not current:
            run(f"git remote add origin {repo_url}", cwd=repo_path)
            print(f"[GIT SYNC] ✅ Added remote origin: {repo_url}")
        else:
            print(f"[GIT SYNC] 🔗 Remote already set to: {current}")
    except Exception as e:
        print(f"[GIT SYNC] ⚠️ Could not verify remote: {e}")
        run(f"git remote add origin {repo_url}", cwd=repo_path)


def git_sync(repo_path: str = ".", commit_msg: str = "Jarvis auto-sync"):
    """
    Fully automatic Git sync:
      1. Configures SSH or HTTPS remote if missing.
      2. Adds all changes.
      3. Commits and pushes to 'main' branch.
    """
    repo_path = os.path.abspath(repo_path)
    print(f"[GIT SYNC] 🚀 Starting sync in: {repo_path}")

    # === Get environment vars ===
    ssh_key = os.getenv("SSH_KEY")
    github_repo = os.getenv("GITHUB_REPO")
    github_token = os.getenv("GITHUB_TOKEN")
    github_user = os.getenv("GITHUB_USERNAME")
    github_pass = os.getenv("GITHUB_PASSWORD")

    # === Ensure repo is a Git repository ===
    if not os.path.exists(os.path.join(repo_path, ".git")):
        raise RuntimeError(f"[GIT SYNC] ❌ Not a Git repository: {repo_path}")

    # === Ensure we have a remote ===
    repo_url = None
    if ssh_key:
        # Extract repo slug (user/repo.git)
        if github_repo and github_repo.startswith("git@"):
            repo_url = github_repo
        elif github_repo and github_repo.startswith("https://github.com/"):
            slug = github_repo.replace("https://github.com/", "")
            repo_url = f"git@github.com:{slug}"
    elif github_token:
        repo_url = f"https://{github_token}@github.com/{github_repo.split('github.com/')[-1]}"
    elif github_user and github_pass:
        repo_url = f"https://{github_user}:{github_pass}@github.com/{github_repo.split('github.com/')[-1]}"
    else:
        raise RuntimeError("[GIT SYNC] ❌ No SSH_KEY or token credentials provided.")

    ensure_remote(repo_path, repo_url)

    # === If SSH key provided, configure temp key file ===
    ssh_env = os.environ.copy()
    ssh_cmd = None
    tmp_key_file = None
    if ssh_key:
        ssh_dir = tempfile.mkdtemp()
        tmp_key_file = os.path.join(ssh_dir, "id_rsa")
        with open(tmp_key_file, "w") as f:
            f.write(ssh_key)
        os.chmod(tmp_key_file, 0o600)
        ssh_cmd = f"ssh -i {tmp_key_file} -o StrictHostKeyChecking=no"
        ssh_env["GIT_SSH_COMMAND"] = ssh_cmd
        print("[GIT SYNC] 🧩 Using SSH authentication")

    # === Perform sync ===
    try:
        run("git add .", cwd=repo_path)
        result = subprocess.run(
            f'git commit -m "{commit_msg}"',
            cwd=repo_path,
            shell=True,
            text=True,
            capture_output=True
        )
        if "nothing to commit" in result.stdout.lower():
            print("[GIT SYNC] 🔄 Nothing new to commit.")
        else:
            print("[GIT SYNC] 💾 Committed changes.")

        run("git pull origin main --rebase", cwd=repo_path)
        run("git push origin HEAD:main", cwd=repo_path, check=True)
        print("[GIT SYNC] ✅ Push to main branch successful.")
    except Exception as e:
        print(f"[GIT SYNC] ❌ Push failed: {e}")
        if github_token:
            print("[GIT SYNC] 🔁 Retrying via HTTPS token...")
            try:
                https_url = f"https://{github_token}@github.com/{github_repo.split('github.com/')[-1]}"
                ensure_remote(repo_path, https_url)
                run("git push origin HEAD:main", cwd=repo_path, check=True)
                print("[GIT SYNC] ✅ Push via HTTPS succeeded.")
            except Exception as e2:
                print(f"[GIT SYNC] ❌ HTTPS push also failed: {e2}")
                raise
        else:
            raise
    finally:
        if tmp_key_file and os.path.exists(tmp_key_file):
            shutil.rmtree(os.path.dirname(tmp_key_file), ignore_errors=True)

    print("[GIT SYNC] ✅ Sync complete.")
