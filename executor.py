# executor.py
import os
import shutil
import asyncio
from typing import List
from jarvis_brain import JarvisBrain
from git_sync import git_sync  # ✅ now importing the function, not a class


class ActionExecutor:
    def __init__(self, brain: JarvisBrain):
        self.brain = brain

    async def process_actions(self, actions: List[dict], user: str = "user"):
        """
        Executes actions proposed by JarvisBrain, such as writing, editing,
        deleting, or moving files. After applying file changes, it triggers
        an automatic Git push to the main branch.
        """
        results = []
        changed_files = []

        for action in actions:
            action_type = action.get("type")
            path = os.path.normpath(action.get("path", "")) if action.get("path") else None

            if not path or not self.brain.is_path_allowed(path):
                results.append({"status": "forbidden", "action": action})
                continue

            try:
                if action_type in ("write", "edit"):
                    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                    content = action.get("content", "")
                    file_existed = os.path.exists(path)

                    with open(path, "w", encoding="utf-8") as f:
                        f.write(content)

                    changed_files.append(path)
                    results.append({
                        "status": "edited" if file_existed else "written",
                        "path": path
                    })

                elif action_type == "delete":
                    if os.path.exists(path):
                        os.remove(path)
                        changed_files.append(path)
                        results.append({"status": "deleted", "path": path})
                    else:
                        results.append({"status": "not_found", "path": path})

                elif action_type == "move":
                    dest = os.path.normpath(action.get("dest", ""))
                    if not self.brain.is_path_allowed(dest):
                        results.append({"status": "forbidden_dest", "action": action})
                        continue

                    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)

                    if os.path.exists(path):
                        shutil.move(path, dest)
                        changed_files.append(dest)
                        results.append({"status": "moved", "from": path, "to": dest})
                    else:
                        results.append({"status": "source_not_found", "path": path})

                else:
                    results.append({"status": "unknown_action", "action": action})

            except Exception as e:
                results.append({
                    "status": "error",
                    "error": str(e),
                    "action": action
                })

        # === Git sync after changes ===
        if changed_files:
            try:
                print(f"🧩 Applying auto-sync for {len(changed_files)} modified files...")
                await asyncio.to_thread(
                    git_sync,  # call sync in a thread (non-blocking)
                    repo_path="."
                )
                print("✅ Auto-synced all changes to GitHub main branch.")
            except Exception as e:
                print(f"⚠️ Git sync failed: {e}")
                results.append({
                    "status": "git_error",
                    "error": str(e)
                })

        return results
