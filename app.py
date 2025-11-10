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

# === Training data ===
try:
    from seed_training_data import seed_training_data
    TRAINING_DATA_AVAILABLE = True
except ImportError:
    TRAINING_DATA_AVAILABLE = False

# === Background Job Scheduler ===
try:
    from job_scheduler import initialize_scheduler, shutdown_scheduler
    SCHEDULER_AVAILABLE = True
except ImportError:
    SCHEDULER_AVAILABLE = False

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

# === Initialize training data on startup ===
if TRAINING_DATA_AVAILABLE:
    try:
        print("🤖 Initializing JARVIS training data...")
        seed_training_data()
        print("✅ Training data loaded successfully")
    except Exception as e:
        print(f"⚠️ Could not load training data: {e}")
        print("   Bot will still function, but with reduced contextual awareness")

# === Initialize background job scheduler ===
if SCHEDULER_AVAILABLE:
    try:
        print("⚙️ Initializing Background Job Scheduler...")
        initialize_scheduler()
        print("✅ Background jobs initialized (GitHub sync, DB cleanup, etc.)")
    except Exception as e:
        print(f"⚠️ Could not initialize scheduler: {e}")
        print("   Bot will still function, but without automatic background tasks")

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


@app.post("/api/search")
async def web_search(message: MessageIn):
    """
    Perform web search with internet access
    
    Args:
        message: MessageIn with 'text' as search query
        
    Returns:
        Search results with sources
    """
    try:
        from internet import get_internet
        
        print(f"🔍 Web search requested: {message.text}")
        
        internet = await get_internet()
        results = await internet.search(message.text, num_results=5)
        
        return {
            "status": "ok",
            "query": message.text,
            "results_count": len(results),
            "results": results
        }
        
    except ImportError:
        return {
            "status": "error",
            "message": "Internet module not available. Install beautifulsoup4 and lxml."
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/api/research")
async def research_topic(message: MessageIn):
    """
    Perform deep research on a topic with multiple sources
    
    Args:
        message: MessageIn with 'text' as topic
        
    Returns:
        Research data with sources and summary
    """
    try:
        from internet import get_internet
        
        print(f"🔬 Research requested: {message.text}")
        
        internet = await get_internet()
        research = await internet.research_topic(message.text, depth=3)
        
        return {
            "status": "ok",
            "topic": message.text,
            "research": research
        }
        
    except ImportError:
        return {
            "status": "error",
            "message": "Internet module not available. Install beautifulsoup4 and lxml."
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/api/answer")
async def answer_question(message: MessageIn):
    """
    Get answer to a question from web search
    
    Args:
        message: MessageIn with 'text' as question
        
    Returns:
        Answer and sources
    """
    try:
        from internet import get_internet
        
        print(f"❓ Question asked: {message.text}")
        
        internet = await get_internet()
        answer = await internet.answer_question(message.text)
        sources = await internet.search(message.text, num_results=3)
        
        return {
            "status": "ok",
            "question": message.text,
            "answer": answer,
            "sources": sources
        }
        
    except ImportError:
        return {
            "status": "error",
            "message": "Internet module not available. Install beautifulsoup4 and lxml."
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/api/news")
async def get_news(message: MessageIn):
    """
    Get latest news on a topic
    
    Args:
        message: MessageIn with 'text' as news topic
        
    Returns:
        Latest news articles
    """
    try:
        from internet import get_internet
        
        topic = message.text or "technology"
        print(f"📰 News requested for: {topic}")
        
        internet = await get_internet()
        news = await internet.get_news(topic, num_results=5)
        
        return {
            "status": "ok",
            "topic": topic,
            "news_count": len(news),
            "news": news
        }
        
    except ImportError:
        return {
            "status": "error",
            "message": "Internet module not available. Install beautifulsoup4 and lxml."
        }
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


# =========================================================
# 🛑 Shutdown Event Handler
# =========================================================
@app.on_event("shutdown")
async def shutdown_event():
    """Cleanup when app shuts down"""
    print("\n⛔ Shutting down JARVIS...")
    
    # End memory session if available
    if brain.memory:
        brain.memory.end_session()
        print("  ✓ Memory session ended")
    
    # Shutdown background scheduler if available
    if SCHEDULER_AVAILABLE:
        shutdown_scheduler()
        print("  ✓ Background scheduler stopped")
    
    print("✅ JARVIS shutdown complete")

