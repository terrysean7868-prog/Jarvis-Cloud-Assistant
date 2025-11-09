# app.py
import os
import asyncio
import subprocess
from fastapi import FastAPI, BackgroundTasks, UploadFile, File, Header, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from dotenv import load_dotenv

# === Core modules ===
from llm_adapter import LLMAdapter
from jarvis_brain import JarvisBrain
from executor import ActionExecutor
from git_sync import git_sync  # ✅ updated version from the new script

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

load_dotenv()

if os.getenv("FRONTEND_URL"):
    cors_origins.append(os.getenv("FRONTEND_URL"))

if os.getenv("CORS_ORIGINS"):
    cors_origins.extend(
        [o.strip() for o in os.getenv("CORS_ORIGINS").split(",") if o.strip()]
    )

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
executor = ActionExecutor(brain=brain)

# =========================================================
# 💬 Data Models
# =========================================================
class MessageIn(BaseModel):
    user: str | None = "user"
    text: str
    mode: str | None = "chat"

# =========================================================
# ⚙️ API Endpoints
# =========================================================
@app.post("/api/chat")
async def chat_endpoint(msg: MessageIn, background_tasks: BackgroundTasks):
    """
    Primary chat endpoint for message/command handling.
    """
    response = await brain.handle_message(msg.text, mode=msg.mode)
    actions = response.get("actions", [])
    if actions:
        background_tasks.add_task(executor.process_actions, actions, msg.user)
    return response


@app.post("/api/upload-module")
async def upload_module(file: UploadFile = File(...)):
    """
    Uploads a new Python module and auto-commits it.
    """
    content = await file.read()
    filename = file.filename
    rel_path = os.path.join("modules", filename)

    if not brain.is_path_allowed(rel_path):
        return {"status": "forbidden", "reason": "path not allowed"}

    os.makedirs("modules", exist_ok=True)
    with open(rel_path, "wb") as f:
        f.write(content)

    # Auto commit using git_sync
    try:
        git_sync(repo_path=".",)
        return {"status": "ok", "message": f"Module {filename} uploaded and committed."}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/api/sync")
async def sync_repo():
    """
    Pull and sync latest repo changes.
    """
    try:
        git_sync(repo_path=".")
        return {"status": "ok", "message": "Repository synced successfully"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/envcheck")
async def envcheck():
    return {"openai": bool(os.getenv("OPENAI_API_KEY"))}


# =========================================================
# 🔐 Secure Git Sync Endpoint
# =========================================================
SYNC_KEY = os.getenv("SYNC_KEY", "mysecretkey")
REPO_PATH = os.getenv("GIT_REPO_PATH", os.getcwd())

def verify_key(x_api_key: str = Header(...)):
    """Simple header-based authentication"""
    if x_api_key != SYNC_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return True


@app.post("/api/git-sync")
async def trigger_git_sync(authorized: bool = Depends(verify_key)):
    """
    Secure endpoint to push latest code to GitHub main branch.
    Uses SSH_KEY or fallback auth from env variables.
    """
    try:
        git_sync(repo_path=REPO_PATH)
        return {"status": "success", "message": "✅ Code pushed to main branch."}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# =========================================================
# 🕒 Startup Event
# =========================================================
@app.on_event("startup")
async def startup_event():
    interval = int(os.getenv("GIT_PULL_INTERVAL_SEC", "0"))
    if interval > 0:
        asyncio.create_task(asyncio.to_thread(git_sync, repo_path="."))
    print("✅ Jarvis server started and git-sync initialized.")


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
            {"message": "Frontend not built yet. Run `npm run build` inside jarvis-frontend/."},
            status_code=404,
        )
