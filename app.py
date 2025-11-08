# app.py
import os
import asyncio
import subprocess
from fastapi import FastAPI, BackgroundTasks, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from fastapi import FastAPI, HTTPException, Depends, Header
from dotenv import load_dotenv

# === Core modules ===
from llm_adapter import LLMAdapter
from jarvis_brain import JarvisBrain
from git_sync import GitSync
from executor import ActionExecutor


# =========================================================
# 🚀 FastAPI Initialization
# =========================================================
app = FastAPI(title="Jarvis Cloud Assistant")

# =========================================================
# 🌐 CORS Configuration
# =========================================================
cors_origins = [
    "http://localhost:3000",
    "http://localhost:5173",  # Vite default port
    "https://jarvis-frontend.onrender.com",
]
# Load environment variables
load_dotenv()

# Add environment variable-based origins (optional)
if os.getenv("FRONTEND_URL"):
    cors_origins.append(os.getenv("FRONTEND_URL"))

if os.getenv("CORS_ORIGINS"):
    cors_origins.extend([
        origin.strip()
        for origin in os.getenv("CORS_ORIGINS").split(",")
        if origin.strip()
    ])

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# =========================================================
# 🧠 Initialize Core Components
# =========================================================
GITHUB_REPO = os.getenv("GITHUB_REPO")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")

llm = LLMAdapter()
brain = JarvisBrain(llm=llm)
git_sync = GitSync(repo_url=GITHUB_REPO, token=GITHUB_TOKEN)
executor = ActionExecutor(brain=brain, git_sync=git_sync)

# =========================================================
# 💬 Data Models
# =========================================================
class MessageIn(BaseModel):
    user: str | None = "user"
    text: str
    mode: str | None = "chat"  # "chat" or "command"

# =========================================================
# ⚙️ API Endpoints
# =========================================================
@app.post("/api/chat")
async def chat_endpoint(msg: MessageIn, background_tasks: BackgroundTasks):
    """
    Primary chat endpoint. Returns LLM response and proposed actions.
    If AUTO_APPLY=true and actions pass allowlist, actions will be executed in background.
    """
    response = await brain.handle_message(msg.text, mode=msg.mode)
    actions = response.get("actions", [])
    if actions:
        background_tasks.add_task(executor.process_actions, actions, msg.user)
    return response


@app.post("/api/upload-module")
async def upload_module(file: UploadFile = File(...)):
    content = await file.read()
    filename = file.filename
    rel_path = os.path.join("modules", filename)
    if not brain.is_path_allowed(rel_path):
        return {"status": "forbidden", "reason": "path not allowed"}
    os.makedirs("modules", exist_ok=True)
    with open(rel_path, "wb") as f:
        f.write(content)
    git_sync.commit_and_push([rel_path], message=f"Add module {filename}")
    return {"status": "ok", "path": rel_path}


@app.post("/api/sync")
async def sync_repo():
    result = git_sync.pull_and_update()
    return {"status": "ok", "result": result}


@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/envcheck")
async def envcheck():
    return {"openai": bool(os.getenv("OPENAI_API_KEY"))}

# =========================================================
# 🕒 Startup Event
# =========================================================
@app.on_event("startup")
async def startup_event():
    interval = int(os.getenv("GIT_PULL_INTERVAL_SEC", "0"))
    if interval > 0:
        asyncio.create_task(git_sync.periodic_pull(interval=interval))
    print("✅ Jarvis server started")

# 🧩 Get repository path and optional auth key from environment
REPO_PATH = os.getenv("GIT_REPO_PATH", os.getcwd())
SYNC_KEY = os.getenv("SYNC_KEY", "mysecretkey")

def verify_key(x_api_key: str = Header(...)):
    """Simple header-based auth for security."""
    if x_api_key != SYNC_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return True

def run_git_command(command: str):
    """Safely run a Git command and return output or error."""
    try:
        result = subprocess.run(
            command,
            cwd=REPO_PATH,
            shell=True,
            text=True,
            capture_output=True,
            check=True
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, detail=f"Git error: {e.stderr.strip()}")

@app.post("/sync_repo")
def sync_repo(commit_message: str = "Auto-sync changes", authorized: bool = Depends(verify_key)):
    """
    Sync the local repository with GitHub:
      1. Pull latest changes
      2. Commit local changes (if any)
      3. Push back to GitHub
    """
    try:
        # 1️⃣ Pull from GitHub
        pull_output = run_git_command("git pull origin main")

        # 2️⃣ Add and commit local changes
        run_git_command("git add .")
        commit_process = subprocess.run(
            f'git commit -m "{commit_message}"',
            cwd=REPO_PATH,
            shell=True,
            text=True,
            capture_output=True
        )

        if "nothing to commit" in commit_process.stdout.lower():
            return {"status": "ok", "message": "Already up to date", "pull_output": pull_output}

        # 3️⃣ Push updates
        push_output = run_git_command("git push origin main")

        return {
            "status": "success",
            "message": "Repository synced successfully",
            "repo_path": REPO_PATH,
            "pull_output": pull_output,
            "commit_output": commit_process.stdout.strip(),
            "push_output": push_output
        }
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# =========================================================
# 🎨 Serve Frontend (React build)
# =========================================================
frontend_build_path = os.path.join(os.getcwd(), "jarvis-frontend", "build")

if os.path.exists(frontend_build_path):
    print(f"✅ Frontend build found at: {frontend_build_path}")
    app.mount("/", StaticFiles(directory=frontend_build_path, html=True), name="frontend")
else:
    print("⚠️ Frontend build not found. Run `npm run build` inside jarvis-frontend/.")

    @app.get("/")
    async def root_fallback():
        return JSONResponse(
            {"message": "Frontend not built yet. Please run `npm run build` inside jarvis-frontend/."},
            status_code=404
        )
