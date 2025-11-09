# git_sync.py
import os
import subprocess
import tempfile
import shutil


def run(cmd: str, cwd: str = ".", check=True, env=None):
    result = subprocess.run(
        cmd, cwd=cwd, shell=True, text=True,
        capture_output=True, env=env or os.environ.copy()
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


def git_sync(repo_path=".", commit_msg="Jarvis auto-sync"):
    repo_path = os.path.abspath(repo_path)
    print(f"[GIT SYNC] 🚀 Starting sync in: {repo_path}")

    ssh_key = os.getenv("SSH_KEY")
    github_repo = os.getenv("GITHUB_REPO")
    github_token = os.getenv("GITHUB_TOKEN")
    github_user = os.getenv("GITHUB_USERNAME")
    github_pass = os.getenv("GITHUB_PASSWORD")

    if not os.path.exists(os.path.join(repo_path, ".git")):
        raise RuntimeError(f"[GIT SYNC] ❌ Not a Git repository: {repo_path}")

    # pick best repo URL
    repo_url = None
    if ssh_key:
        if github_repo.startswith("git@"):
            repo_url = github_repo
        else:
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
