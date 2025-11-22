import os
import asyncio
from fastapi import FastAPI, BackgroundTasks, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from dotenv import load_dotenv
from typing import List

from src.core.llm_adapter import LLMAdapter
from src.core.jarvis_brain import JarvisBrain
from src.core.executor import ActionExecutor
from src.utils.git_sync import git_sync, setup_ssh_trust
from src.utils.self_update import parse_voice_command, self_update_file, self_add_feature
from src.utils.voice_auth import voice_auth
from src.utils.email_generator import email_generator
from src.utils.screen_access import screen_access
from src.utils.app_manager import app_manager
from src.utils.task_manager import task_manager
from src.utils.error_handler import error_handler

# =========================================================
# 🚀 FastAPI Initialization
# =========================================================
app = FastAPI(title="Jarvis Cloud Assistant")
load_dotenv()

# =========================================================
# 🌐 CORS Configuration
# =========================================================
cors_origins = [
    "http://localhost:3000",
    "http://localhost:5173",
    "https://jarvis-frontend.onrender.com",
    "https://jarvis-cloud-assistant.onrender.com"
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# =========================================================
# 🧠 Core Initialization
# =========================================================
llm = LLMAdapter()
brain = JarvisBrain(llm=llm)
executor = ActionExecutor(brain=brain)

class MessageIn(BaseModel):
    user: str | None = "user"
    text: str
    mode: str | None = "chat"
    session_id: str | None = None  # Voice auth session

class VoiceAuthRequest(BaseModel):
    username: str
    voice_sample_hash: str | None = None
    password: str | None = None
    action: str  # "register" or "login"
    role: str | None = None  # optional: 'admin' or 'user'

# =========================================================
# 🔐 Voice Authentication Endpoints
# =========================================================
@app.post("/api/voice-auth")
async def voice_auth_endpoint(auth_req: VoiceAuthRequest):
    """Handle voice-based authentication"""
    try:
        if auth_req.action == "register":
            if not auth_req.voice_sample_hash:
                return {"status": "error", "message": "Voice sample required for registration"}
            result = voice_auth.register_user(
                auth_req.username,
                auth_req.voice_sample_hash,
                auth_req.password,
                role=(auth_req.role or 'user')
            )
            return result
        
        elif auth_req.action == "login":
            if not auth_req.voice_sample_hash:
                return {"status": "error", "message": "Voice sample required for login"}
            is_valid, session_or_error = voice_auth.authenticate_by_voice(
                auth_req.username,
                auth_req.voice_sample_hash
            )
            if is_valid:
                return {
                    "status": "success",
                    "message": "Authentication successful",
                    "session_id": session_or_error
                }
            return {
                "status": "error",
                "message": session_or_error or "Authentication failed"
            }
        
        return {"status": "error", "message": "Invalid action"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.post("/api/validate-session")
async def validate_session_endpoint(session_id: dict):
    """Validate authentication session"""
    session = session_id.get("session_id") if isinstance(session_id, dict) else session_id
    is_valid, username = voice_auth.validate_session(session)
    return {
        "valid": is_valid,
        "username": username
    }

@app.post("/api/logout")
async def logout(session_id: str):
    """Logout and invalidate session"""
    success = voice_auth.logout(session_id)
    return {"status": "success" if success else "error"}

# =========================================================
# 💬 Main Chat Endpoint (With Auth Check)
# =========================================================
@app.post("/api/chat")
async def chat_endpoint(msg: MessageIn, background_tasks: BackgroundTasks):
    # Check authentication if session_id provided
    if msg.session_id:
        is_valid, username = voice_auth.validate_session(msg.session_id)
        if not is_valid:
            return {
                "text": "Authentication required. Please login first.",
                "actions": [],
                "auth_required": True
            }
        msg.user = username
    
    response = await brain.handle_message(msg.text, mode=msg.mode)
    actions = response.get("actions", [])
    if actions:
        background_tasks.add_task(executor.process_actions, actions, msg.user)
    return response

# Alias for backward compatibility
@app.post("/api/message")
async def message_endpoint(msg: MessageIn, background_tasks: BackgroundTasks):
    """Alias for /api/chat endpoint for backward compatibility"""
    return await chat_endpoint(msg, background_tasks)

# =========================================================
# 🛠️ Git Sync API
# =========================================================
@app.post("/api/git-sync")
async def trigger_git_sync():
    try:
        git_sync(repo_path=".")
        return {"status": "success", "message": "✅ Code pushed to main branch."}
    except Exception as e:
        return {"status": "error", "message": str(e)}

# =========================================================
# 🔧 GitHub Configuration API
# =========================================================
class GitHubConfig(BaseModel):
    repo_url: str | None = None
    username: str | None = None
    password: str | None = None
    ssh_key: str | None = None

@app.post("/api/github-config")
async def set_github_config(config: GitHubConfig):
    """Set GitHub credentials for version control."""
    import os
    from pathlib import Path
    
    try:
        env_file = Path(".env")
        env_vars = {}
        
        # Read existing .env if it exists
        if env_file.exists():
            with open(env_file, "r") as f:
                for line in f:
                    if "=" in line and not line.strip().startswith("#"):
                        key, value = line.strip().split("=", 1)
                        env_vars[key] = value
        
        # Update with new values
        if config.repo_url:
            env_vars["GITHUB_REPO"] = config.repo_url
        if config.username:
            env_vars["GITHUB_USERNAME"] = config.username
        if config.password:
            env_vars["GITHUB_PASSWORD"] = config.password
        if config.ssh_key:
            env_vars["SSH_KEY"] = config.ssh_key
        
        # Write back to .env
        with open(env_file, "w") as f:
            for key, value in env_vars.items():
                f.write(f"{key}={value}\n")
        
        # Also set in environment
        for key, value in env_vars.items():
            os.environ[key] = value
        
        return {"status": "success", "message": "GitHub configuration updated"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

# =========================================================
# 🔄 Self-Update API (Voice-triggered)
# =========================================================
class SelfUpdateRequest(BaseModel):
    command: str
    file_path: str | None = None
    description: str | None = None
    session_id: str | None = None  # session id of the caller (required for admin actions)

@app.post("/api/self-update")
async def handle_self_update(request: SelfUpdateRequest):
    """Handle self-update commands from voice input."""
    try:
        # Validate session and admin privileges before allowing self-update
        if not request.session_id:
            return {"status":"error","message":"Admin session required"}
        is_valid, username = voice_auth.validate_session(request.session_id)
        if not is_valid or not username:
            return {"status":"error","message":"Invalid or expired session"}
        if not voice_auth.is_admin(username):
            return {"status":"error","message":"Admin privileges required"}

        # Parse voice command
        parsed = parse_voice_command(request.command)
        
        if not parsed:
            # Try direct update if file_path and description provided
            if request.file_path and request.description:
                result = self_update_file(request.description, request.file_path)
                return result
            return {"status": "error", "message": "Could not parse command"}
        
        action = parsed.get("action")
        
        if action == "update" or action == "edit":
            file_path = parsed.get("target", request.file_path or "")
            description = parsed.get("description", request.description or request.command)
            result = self_update_file(description, file_path)
            return result
        
        elif action == "add":
            feature_type = parsed.get("feature_type", "module")
            description = parsed.get("description", request.description or request.command)
            result = self_add_feature(description, feature_type)
            return result
        
        return {"status": "error", "message": f"Unknown action: {action}"}
        
    except Exception as e:
        return {"status": "error", "message": str(e)}

# =========================================================
# 📧 Email Generation API
# =========================================================
class EmailRequest(BaseModel):
    recipient: str
    subject: str | None = None
    body_prompt: str
    tone: str = "professional"
    command: str | None = None  # Voice command to parse

@app.post("/api/generate-email")
async def generate_email_endpoint(email_req: EmailRequest):
    """Generate email from voice command or parameters"""
    try:
        if email_req.command:
            result = email_generator.generate_from_command(email_req.command)
        else:
            result = email_generator.generate_email(
                email_req.recipient,
                email_req.subject,
                email_req.body_prompt,
                email_req.tone
            )
        return result
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.get("/api/email-drafts")
async def get_email_drafts():
    """Get all email drafts"""
    return {"drafts": email_generator.get_drafts()}

# =========================================================
# 🖥️ Screen Access API
# =========================================================
@app.post("/api/capture-screen")
async def capture_screen_endpoint(region: dict | None = None):
    """Capture screen or region"""
    try:
        reg = None
        if region:
            reg = (region.get("x"), region.get("y"), region.get("width"), region.get("height"))
        screenshot_info = screen_access.take_screenshot_info()
        return screenshot_info
    except Exception as e:
        return {"error": str(e)}

@app.post("/api/read-screen")
async def read_screen_endpoint(region: dict | None = None):
    """Read text from screen using OCR"""
    try:
        reg = None
        if region:
            reg = (region.get("x"), region.get("y"), region.get("width"), region.get("height"))
        text = screen_access.read_screen_text(reg)
        return {"text": text, "status": "success"}
    except Exception as e:
        return {"error": str(e), "status": "error"}

# =========================================================
# 🖥️ Application Management API
# =========================================================
class OpenAppRequest(BaseModel):
    app_name: str
    args: List[str] | None = None

@app.post("/api/open-app")
async def open_app_endpoint(request: OpenAppRequest):
    """Open an application"""
    return app_manager.open_app(request.app_name, request.args)

class AppNameRequest(BaseModel):
    app_name: str

@app.post("/api/close-app")
async def close_app_endpoint(request: AppNameRequest):
    """Close an application"""
    return app_manager.close_app(request.app_name)

@app.post("/api/switch-app")
async def switch_app_endpoint(request: AppNameRequest):
    """Switch to an application"""
    return app_manager.switch_to_app(request.app_name)

@app.get("/api/running-apps")
async def get_running_apps():
    """Get list of running applications"""
    return {"apps": app_manager.list_running_apps()}

class ExecuteCommandRequest(BaseModel):
    command: str
    wait: bool = True

@app.post("/api/execute-command")
async def execute_command_endpoint(request: ExecuteCommandRequest):
    """Execute a system command"""
    return app_manager.execute_command(request.command, request.wait)

# =========================================================
# 📋 Task Management API
# =========================================================
class CreateTaskRequest(BaseModel):
    description: str
    steps: List[dict]
    priority: int = 5

@app.post("/api/create-task")
async def create_task_endpoint(request: CreateTaskRequest):
    """Create a new task"""
    task_id = task_manager.create_task(request.description, request.steps, request.priority)
    return {"status": "success", "task_id": task_id}

@app.post("/api/stop-task")
async def stop_task_endpoint():
    """Stop current task"""
    return task_manager.stop_current_task()

@app.get("/api/current-task")
async def get_current_task():
    """Get current task"""
    task = task_manager.get_current_task()
    return {"task": task} if task else {"task": None}

@app.get("/api/tasks")
async def get_all_tasks():
    """Get all tasks"""
    return {"tasks": task_manager.get_all_tasks()}

@app.get("/api/wakeup-context")
async def get_wakeup_context():
    """Get wakeup context mapping"""
    return {"context": task_manager.get_wakeup_context()}

# =========================================================
# 🔧 Error Handling API
# =========================================================
@app.post("/api/check-errors")
async def check_errors_endpoint():
    """Check for errors and auto-fix"""
    return error_handler.monitor_and_fix()

@app.get("/api/render-logs")
async def get_render_logs():
    """Get Render logs"""
    return error_handler.check_render_logs()

@app.post("/api/fix-error")
async def fix_error_endpoint(error: dict):
    """Fix a specific error"""
    return error_handler.auto_fix_error(error)

# =========================================================
# 🕒 Startup Event
# =========================================================
@app.on_event("startup")
async def startup_event():
    setup_ssh_trust()
    interval = int(os.getenv("GIT_PULL_INTERVAL_SEC", "0"))
    if interval > 0:
        asyncio.create_task(asyncio.to_thread(git_sync, repo_path="."))
    print("✅ Jarvis server started and git-sync initialized.")

# =========================================================
# 🎨 Serve Frontend (React build)
# =========================================================
frontend_build_path = os.path.join(os.getcwd(), "jarvis-frontend", "build")

if os.path.exists(frontend_build_path):
    app.mount("/", StaticFiles(directory=frontend_build_path, html=True), name="frontend")
else:
    @app.get("/")
    async def root_fallback():
        return JSONResponse({"message": "Frontend not built. Run `npm run build`."}, status_code=404)
