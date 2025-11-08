# git_sync.py
import os, shutil, subprocess
from git import Repo, GitCommandError, InvalidGitRepositoryError

class GitSync:
    def __init__(self, repo_url: str = None, token: str = None):
        self.repo_url = repo_url
        self.token = token
        self.local_path = os.getcwd()  # Work with main repository
        self.repo = None
        self._ensure_repo()

    def _auth_url(self):
        if not self.token or not self.repo_url:
            return self.repo_url
        # Handle different URL formats
        if "://" in self.repo_url and "@" not in self.repo_url:
            return self.repo_url.replace("https://", f"https://{self.token}@")
        return self.repo_url

    def _ensure_repo(self):
        """Initialize or connect to git repository in current directory."""
        try:
            # Try to open existing repo
            self.repo = Repo(self.local_path)
            # If repo_url is provided, ensure remote is set
            if self.repo_url:
                try:
                    origin = self.repo.remote("origin")
                    if origin.url != self._auth_url():
                        # Update remote URL if token/auth changed
                        origin.set_url(self._auth_url())
                except ValueError:
                    # No origin remote, add it
                    self.repo.create_remote("origin", self._auth_url())
        except (InvalidGitRepositoryError, Exception) as e:
            # Not a git repo, initialize if repo_url provided
            if self.repo_url:
                try:
                    # Clone if repo_url is different from current dir
                    self.repo = Repo.init(self.local_path)
                    if self.repo_url:
                        try:
                            self.repo.create_remote("origin", self._auth_url())
                        except:
                            pass
                except Exception as init_error:
                    print(f"Warning: Could not initialize git repo: {init_error}")
                    self.repo = None
            else:
                print(f"Warning: Not a git repository and no GITHUB_REPO provided: {e}")
                self.repo = None

    def commit_and_push(self, paths, message="Jarvis auto-update: applied changes"):
        """Commit changes and push to GitHub."""
        if not self.repo:
            print("Warning: No git repository available. Changes not pushed.")
            return {"status": "no_repo"}
        
        try:
            # Add changed files
            if paths:
                for p in paths:
                    abs_path = os.path.abspath(p)
                    if os.path.exists(abs_path):
                        try:
                            self.repo.index.add([p])
                        except Exception as e:
                            print(f"Warning: Could not add {p}: {e}")
            
            # Check if there are changes to commit
            if self.repo.is_dirty() or self.repo.untracked_files:
                # Commit
                self.repo.index.commit(message)
                print(f"✅ Committed changes: {message}")
                
                # Push to origin
                try:
                    origin = self.repo.remote("origin")
                    origin.push()
                    print(f"✅ Pushed to GitHub")
                    return {"status": "pushed", "message": message}
                except Exception as push_error:
                    print(f"Warning: Could not push to GitHub: {push_error}")
                    return {"status": "committed_but_not_pushed", "error": str(push_error)}
            else:
                return {"status": "no_changes"}
        except Exception as e:
            print(f"Error during commit/push: {e}")
            return {"status": "error", "error": str(e)}

    def pull_and_update(self):
        """Pull latest changes from GitHub."""
        if not self.repo:
            return {"status": "no_repo"}
        
        try:
            origin = self.repo.remote("origin")
            origin.pull()
            print("✅ Pulled latest changes from GitHub")
            return {"status": "pulled"}
        except Exception as e:
            print(f"Warning: Could not pull from GitHub: {e}")
            return {"status": "error", "error": str(e)}

    async def periodic_pull(self, interval=300):
        """Periodically pull updates from GitHub."""
        import asyncio
        while True:
            try:
                self.pull_and_update()
            except Exception as e:
                print(f"Error in periodic pull: {e}")
            await asyncio.sleep(interval)
