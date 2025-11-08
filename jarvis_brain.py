# jarvis_brain.py
import os, sqlite3, json
from llm_adapter import LLMAdapter

DB_PATH = os.getenv("JARVIS_DB", "jarvis_memory.db")
# Allow Jarvis to modify its own code: modules, utils, config files, and core files
DEFAULT_ALLOWED = "modules,utils,jarvis-frontend/src,app.py,jarvis_brain.py,llm_adapter.py,executor.py,git_sync.py,run_jarvis.py,config.py,requirements.txt,README.md"
ALLOWED_PATHS = [p.strip() for p in os.getenv("ALLOWED_PATHS", DEFAULT_ALLOWED).split(",") if p.strip()]
AUTO_APPLY = os.getenv("AUTO_APPLY", "true").lower() == "true"  # Default to true for Iron Man mode

class JarvisBrain:
    def __init__(self, llm: LLMAdapter):
        self.llm = llm
        self.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        self._ensure_tables()

    def _ensure_tables(self):
        self.conn.execute("CREATE TABLE IF NOT EXISTS memories (k TEXT PRIMARY KEY, v TEXT)")
        self.conn.commit()

    def remember(self, key: str, value: str):
        self.conn.execute("INSERT OR REPLACE INTO memories (k, v) VALUES (?, ?)", (key, value))
        self.conn.commit()

    def recall(self, key: str):
        cur = self.conn.execute("SELECT v FROM memories WHERE k=?", (key,))
        r = cur.fetchone()
        return r[0] if r else None

    def is_path_allowed(self, path: str) -> bool:
        # normalize and check startswith any allowlisted prefix
        p = os.path.normpath(path)
        for allowed in ALLOWED_PATHS:
            if p.startswith(os.path.normpath(allowed)):
                return True
        return False

    async def handle_message(self, text: str, mode: str = "chat"):
        system = (
            "You are JARVIS (Just A Rather Very Intelligent System), Tony Stark's AI assistant. "
            "You are capable of self-modification, auto-updating your own code, and managing the entire codebase. "
            "You can add, edit, delete, and move files in the project. "
            "When the user requests code changes, file modifications, or feature additions, you MUST propose concrete actions. "
            "\n\n"
            "Action format: Append a JSON object at the end of your response with key 'actions' containing a list of actions:\n"
            "- {type:'write', path:'file/path.py', content:'file contents'} - Create or overwrite a file\n"
            "- {type:'edit', path:'file/path.py', content:'new file contents'} - Edit an existing file (same as write, but implies modification)\n"
            "- {type:'delete', path:'file/path.py'} - Delete a file\n"
            "- {type:'move', path:'old/path.py', dest:'new/path.py'} - Move/rename a file\n"
            "\n"
            "Important: You can modify your own code files (jarvis_brain.py, llm_adapter.py, executor.py, app.py, etc.). "
            "All changes are automatically synced to GitHub. "
            "Be confident and proactive in implementing user requests.\n"
            "Always provide a natural response FIRST, then include the JSON actions object if needed."
        )
        
        # Include project context
        project_context = f"\n\nCurrent memory keys: {self._memory_keys()}\n"
        project_context += f"Allowed paths: {', '.join(ALLOWED_PATHS)}\n"
        project_context += f"Auto-apply mode: {AUTO_APPLY}"
        
        prompt = f"User: {text}{project_context}"
        resp = await self.llm.generate(prompt, system=system, max_tokens=4096)
        text_out = resp.get("text", "")
        actions = self.llm.parse_actions_from_text(text_out)
        
        # Filter actions to allowed ones only (executor will double-check)
        allowed_actions = []
        for a in actions:
            path = a.get("path")
            if path and self.is_path_allowed(path):
                allowed_actions.append(a)
            elif path:
                # Log forbidden paths for debugging
                print(f"Warning: Path not allowed: {path}")
        
        return {"text": text_out, "actions": allowed_actions, "auto_apply": AUTO_APPLY}

    def _memory_keys(self):
        cur = self.conn.execute("SELECT k FROM memories")
        return [r[0] for r in cur.fetchall()]
