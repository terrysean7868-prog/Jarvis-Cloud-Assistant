# app.py
import os
import asyncio
from fastapi import FastAPI, BackgroundTasks, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from llm_adapter import LLMAdapter
from jarvis_brain import JarvisBrain
from git_sync import GitSync
from executor import ActionExecutor

app = FastAPI(title="Jarvis Cloud Assistant")

# CORS middleware for Render deployment and local development
cors_origins = [
    "http://localhost:3000",
    "http://localhost:5173",  # Vite default port
    "https://jarvis-frontend.onrender.com",
]

# Add environment variable origins
if os.getenv("FRONTEND_URL"):
    cors_origins.append(os.getenv("FRONTEND_URL"))

# Add CORS_ORIGINS if set
if os.getenv("CORS_ORIGINS"):
    cors_origins.extend([origin.strip() for origin in os.getenv("CORS_ORIGINS").split(",") if origin.strip()])

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

GITHUB_REPO = os.getenv("GITHUB_REPO")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")

llm = LLMAdapter()
brain = JarvisBrain(llm=llm)
# Initialize git_sync even if repo_url/token not provided (will work if repo exists locally)
git_sync = GitSync(repo_url=GITHUB_REPO, token=GITHUB_TOKEN)
executor = ActionExecutor(brain=brain, git_sync=git_sync)

class MessageIn(BaseModel):
    user: str | None = "user"
    text: str
    mode: str | None = "chat"  # "chat" or "command"

@app.post("/api/chat")
async def chat_endpoint(msg: MessageIn, background_tasks: BackgroundTasks):
    """
    Primary chat endpoint. Returns LLM response and proposed actions.
    If AUTO_APPLY=true and actions pass allowlist, actions will be executed in background.
    """
    response = await brain.handle_message(msg.text, mode=msg.mode)
    actions = response.get("actions", [])
    if actions:
        # queue execution but return immediately
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

@app.on_event("startup")
async def startup_event():
    # start background periodic pull only if configured
    interval = int(os.getenv("GIT_PULL_INTERVAL_SEC", "0"))
    if interval > 0:
        asyncio.create_task(git_sync.periodic_pull(interval=interval))
    print("✅ Jarvis server started")
