# executor.py
import os, shutil
from typing import List
from jarvis_brain import JarvisBrain
from git_sync import GitSync

class ActionExecutor:
    def __init__(self, brain: JarvisBrain, git_sync: GitSync):
        self.brain = brain
        self.git_sync = git_sync

    async def process_actions(self, actions: List[dict], user: str = "user"):
        results = []
        changed = []
        for a in actions:
            t = a.get("type")
            path = os.path.normpath(a.get("path", "")) if a.get("path") else None
            if not path or not self.brain.is_path_allowed(path):
                results.append({"status":"forbidden","action":a})
                continue

            try:
                if t == "write" or t == "edit":
                    # Edit is same as write - create or overwrite file
                    file_existed = os.path.exists(path)
                    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                    content = a.get("content", "")
                    # If editing and file exists, we could do merge logic, but for now just overwrite
                    with open(path, "w", encoding="utf-8") as fh:
                        fh.write(content)
                    action_type = "edited" if (t == "edit" or file_existed) else "written"
                    results.append({"status": action_type, "path": path})
                    changed.append(path)

                elif t == "delete":
                    if os.path.exists(path):
                        os.remove(path)
                        results.append({"status":"deleted","path":path})
                        changed.append(path)
                    else:
                        results.append({"status":"not_found","path":path})

                elif t == "move":
                    dst = os.path.normpath(a.get("dest", ""))
                    if not self.brain.is_path_allowed(dst):
                        results.append({"status":"forbidden_dest","action":a})
                        continue
                    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
                    if os.path.exists(path):
                        shutil.move(path, dst)
                        results.append({"status":"moved","from":path,"to":dst})
                        changed.append(dst)
                    else:
                        results.append({"status":"source_not_found","path":path})

                else:
                    results.append({"status":"unknown","action":a})
            except Exception as e:
                results.append({"status":"error","error":str(e),"action":a})

        if changed:
            try:
                if self.git_sync and self.git_sync.repo:
                    self.git_sync.commit_and_push(changed, message=f"Jarvis auto-update: applied changes by {user}")
                    print(f"✅ Auto-synced {len(changed)} file(s) to GitHub")
                else:
                    print(f"⚠️  Changes made but GitHub sync not available (no repo configured)")
            except Exception as e:
                print(f"⚠️  Warning: Could not sync to GitHub: {e}")
                results.append({"status":"git_error","error":str(e)})
        return results
