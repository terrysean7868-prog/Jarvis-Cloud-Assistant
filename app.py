import os
import asyncio
from fastapi import FastAPI, BackgroundTasks, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from dotenv import load_dotenv
from typing import List, Optional

from src.core.llm_adapter import LLMAdapter
from src.core.jarvis_brain import JarvisBrain
from src.core.executor import ActionExecutor
from src.utils.git_sync import git_sync, setup_ssh_trust
from src.utils.self_update import parse_voice_command, self_update_file, self_add_feature
from src.utils.voice_auth import voice_auth
from src.utils.db import db as database
from src.utils.email_generator import email_generator
from src.utils.screen_access import screen_access
from src.utils.app_manager import app_manager
from src.utils.task_manager import task_manager
from src.utils.error_handler import error_handler
from src.utils.telegram_bot import telegram_bot
from src.utils.session_manager import session_manager, start_session_cleanup_task
from src.utils.mcp_file_ops import file_ops

# Import system_operations safely (may fail on headless systems)
try:
    from src.utils.system_operations import system_ops
    SYSTEM_OPS_AVAILABLE = True
except (ImportError, KeyError, Exception) as e:
    SYSTEM_OPS_AVAILABLE = False
    logger = __import__('logging').getLogger(__name__)
    logger.warning(f"System operations not available: {e}")

# =========================================================
# FastAPI Initialization
# =========================================================
app = FastAPI(title="Jarvis Cloud Assistant")
load_dotenv()

# =========================================================
# CORS Configuration
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
# Core Initialization
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
# Voice Authentication Endpoints
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


# Simple health endpoint used by local startup checks
@app.get("/health")
async def health_check():
    return JSONResponse({"status": "ok"}, status_code=200)

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
# Telegram Bot Endpoints
# =========================================================
class TelegramAuthRequest(BaseModel):
    user_id: str
    username: str
    action: str  # "register" or "login"
    voice_sample_hash: str | None = None
    password: str | None = None
    role: str | None = None

@app.post("/api/telegram/register-start")
async def telegram_register_start(req: dict):
    """Start Telegram registration process"""
    user_id = req.get("user_id")
    username = req.get("username")
    
    if not user_id or not username:
        return {"status": "error", "message": "user_id and username required"}
    
    result = telegram_bot.start_registration(user_id, username)
    return result

@app.post("/api/telegram/process-voice")
async def telegram_process_voice(req: dict):
    """Process voice sample from Telegram"""
    user_id = req.get("user_id")
    voice_file_id = req.get("voice_file_id")
    # In production, download voice file from Telegram and get bytes
    voice_bytes = req.get("voice_bytes", b"")
    
    if not user_id or not voice_file_id:
        return {"status": "error", "message": "user_id and voice_file_id required"}
    
    result = telegram_bot.process_voice_sample(user_id, voice_file_id, voice_bytes)
    return result

@app.post("/api/telegram/complete-registration")
async def telegram_complete_registration(auth_req: TelegramAuthRequest):
    """Complete Telegram registration"""
    if not auth_req.voice_sample_hash or not auth_req.password:
        return {"status": "error", "message": "voice_sample_hash and password required"}
    
    result = telegram_bot.complete_registration(
        auth_req.user_id,
        auth_req.voice_sample_hash,
        auth_req.password,
        auth_req.username,
        auth_req.role or "user"
    )
    return result

@app.post("/api/telegram/login")
async def telegram_login(auth_req: TelegramAuthRequest):
    """Handle Telegram user login"""
    if not auth_req.voice_sample_hash:
        return {"status": "error", "message": "voice_sample_hash required"}
    
    result = telegram_bot.telegram_login(
        auth_req.user_id,
        auth_req.username,
        auth_req.voice_sample_hash
    )
    return result

@app.post("/api/telegram/validate-session")
async def telegram_validate_session(req: dict):
    """Validate Telegram user session"""
    user_id = req.get("user_id")
    if not user_id:
        return {"status": "error", "message": "user_id required"}
    
    is_valid, username = telegram_bot.validate_telegram_session(user_id)
    return {
        "valid": is_valid,
        "username": username,
        "user_info": telegram_bot.get_user_info(user_id)
    }

@app.post("/api/telegram/logout")
async def telegram_logout(req: dict):
    """Logout Telegram user"""
    user_id = req.get("user_id")
    if not user_id:
        return {"status": "error", "message": "user_id required"}
    
    success = telegram_bot.logout_telegram_user(user_id)
    return {
        "status": "success" if success else "error",
        "message": "Logged out successfully" if success else "User not found"
    }

@app.post("/api/telegram/chat")
async def telegram_chat(req: dict, background_tasks: BackgroundTasks):
    """Handle chat message from Telegram user"""
    user_id = req.get("user_id")
    text = req.get("text")
    
    if not user_id or not text:
        return {"status": "error", "message": "user_id and text required"}
    
    # Validate session
    is_valid, username = telegram_bot.validate_telegram_session(user_id)
    if not is_valid:
        return {
            "status": "auth_required",
            "message": "Please login first",
            "action": "redirect_to_login"
        }
    
    # Process message through brain
    response = await brain.handle_message(text, mode="chat")
    actions = response.get("actions", [])
    
    if actions:
        background_tasks.add_task(executor.process_actions, actions, username)
    
    return {
        "status": "success",
        "user_id": user_id,
        "username": username,
        "response": response.get("text", ""),
        "actions": actions
    }

# =========================================================
# Main Chat Endpoint (With Auth Check)
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
# Internet Access API (Web Search & Data Retrieval)
# =========================================================
class SearchRequest(BaseModel):
    query: str
    num_results: int | None = 5
    session_id: str | None = None

@app.post("/api/internet/search")
async def search_web(req: SearchRequest):
    """Search the web for information"""
    try:
        from src.internet.internet import InternetAccess
        
        internet = InternetAccess()
        await internet.initialize()
        
        results = await internet.search(req.query, num_results=req.num_results or 5)
        
        await internet.close()
        
        return {
            "status": "success",
            "query": req.query,
            "results": results,
            "count": len(results)
        }
    except Exception as e:
        return {
            "status": "error",
            "message": str(e)
        }

class FetchRequest(BaseModel):
    url: str
    include_content: bool | None = True
    session_id: str | None = None

@app.post("/api/internet/fetch")
async def fetch_webpage(req: FetchRequest):
    """Fetch and parse a webpage"""
    try:
        from src.internet.internet import InternetAccess
        
        internet = InternetAccess()
        await internet.initialize()
        
        result = await internet.fetch_webpage(req.url, include_content=req.include_content or True)
        
        await internet.close()
        
        return {
            "status": "success",
            "url": req.url,
            "content": result
        }
    except Exception as e:
        return {
            "status": "error",
            "message": str(e)
        }

@app.post("/api/internet/search-summarize")
async def search_and_summarize(req: SearchRequest):
    """Search web and get summaries of top results"""
    try:
        from src.internet.internet import InternetAccess
        
        internet = InternetAccess()
        await internet.initialize()
        
        results = await internet.search_and_summarize(req.query, num_results=req.num_results or 3)
        
        await internet.close()
        
        return {
            "status": "success",
            "query": req.query,
            "results": results,
            "count": len(results)
        }
    except Exception as e:
        return {
            "status": "error",
            "message": str(e)
        }

@app.get("/api/internet/news")
async def get_news_endpoint(topic: str = "latest", num_results: int = 5):
    """Get latest news on a topic"""
    try:
        from src.internet.internet import InternetAccess
        
        internet = InternetAccess()
        await internet.initialize()
        
        news = await internet.get_news(topic, num_results)
        
        await internet.close()
        
        return {
            "status": "success",
            "topic": topic,
            "news": news,
            "count": len(news)
        }
    except Exception as e:
        return {
            "status": "error",
            "message": str(e)
        }

# =========================================================
# Git Sync API
# =========================================================
@app.post("/api/git-sync")
async def trigger_git_sync():
    try:
        git_sync(repo_path=".")
        return {"status": "success", "message": "[OK] Code pushed to main branch."}
    except Exception as e:
        return {"status": "error", "message": str(e)}

# =========================================================
# GitHub Configuration API
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
# Self-Update API (Voice-triggered)
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
# Email Generation API
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
# Screen Access API
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
# Application Management API
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
# Task Management API
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
# Error Handling API
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
# File Operations API (Via MCP or Local)
# =========================================================
class FileRequest(BaseModel):
    path: str
    session_id: str | None = None

class FileWriteRequest(BaseModel):
    path: str
    content: str
    session_id: str | None = None

class FileCopyRequest(BaseModel):
    source: str
    destination: str
    session_id: str | None = None

@app.post("/api/files/read")
async def read_file_endpoint(req: FileRequest):
    """Read file content"""
    try:
        result = file_ops.read_file(req.path)
        return result
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.post("/api/files/write")
async def write_file_endpoint(req: FileWriteRequest):
    """Write content to file"""
    try:
        result = file_ops.write_file(req.path, req.content)
        return result
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.post("/api/files/list")
async def list_files_endpoint(req: FileRequest):
    """List files in directory"""
    try:
        result = file_ops.list_files(req.path)
        return result
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.post("/api/files/delete")
async def delete_file_endpoint(req: FileRequest):
    """Delete a file"""
    try:
        result = file_ops.delete_file(req.path)
        return result
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.post("/api/files/mkdir")
async def create_directory_endpoint(req: FileRequest):
    """Create a directory"""
    try:
        result = file_ops.create_directory(req.path)
        return result
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.post("/api/files/copy")
async def copy_file_endpoint(req: FileCopyRequest):
    """Copy a file"""
    try:
        result = file_ops.copy_file(req.source, req.destination)
        return result
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.post("/api/files/cleanup")
async def cleanup_project_endpoint():
    """Clean up project cache files"""
    try:
        result = file_ops.cleanup_project()
        return result
    except Exception as e:
        return {"status": "error", "message": str(e)}

# =========================================================
# System Operations API (Digital Assistant PC Control)
# =========================================================
def _system_ops_unavailable():
    """Helper function to return error when system_ops is unavailable"""
    return {"status": "error", "message": "System operations not available on this platform"}

@app.get("/api/system/info")
async def get_system_info():
    """Get current system information"""
    if not SYSTEM_OPS_AVAILABLE:
        return _system_ops_unavailable()
    return system_ops.get_system_info()

@app.get("/api/system/processes")
async def list_processes_endpoint(filter: Optional[str] = None):
    """List running processes"""
    if not SYSTEM_OPS_AVAILABLE:
        return _system_ops_unavailable()
    return system_ops.list_processes(filter)

@app.post("/api/system/process-kill")
async def kill_process_endpoint(req: dict):
    """Kill a process by name"""
    if not SYSTEM_OPS_AVAILABLE:
        return _system_ops_unavailable()
    process_name = req.get("process_name")
    if not process_name:
        return {"status": "error", "message": "process_name required"}
    return system_ops.kill_process(process_name)

@app.post("/api/system/launch-app")
async def launch_application_endpoint(req: dict):
    """Launch an application"""
    if not SYSTEM_OPS_AVAILABLE:
        return _system_ops_unavailable()
    app_path = req.get("app_path")
    args = req.get("args", [])
    if not app_path:
        return {"status": "error", "message": "app_path required"}
    return system_ops.launch_application(app_path, args)

@app.post("/api/system/execute")
async def execute_command_endpoint(req: dict):
    """Execute a shell command"""
    if not SYSTEM_OPS_AVAILABLE:
        return _system_ops_unavailable()
    command = req.get("command")
    timeout = req.get("timeout", 30)
    if not command:
        return {"status": "error", "message": "command required"}
    return system_ops.execute_command(command, timeout)

@app.get("/api/system/screen")
async def get_screen_info():
    """Get screen/display information"""
    if not SYSTEM_OPS_AVAILABLE:
        return _system_ops_unavailable()
    return system_ops.get_screen_info()

@app.post("/api/system/screenshot")
async def take_screenshot(req: dict = None):
    """Take a screenshot"""
    if not SYSTEM_OPS_AVAILABLE:
        return _system_ops_unavailable()
    save_path = req.get("save_path") if req else None
    return system_ops.take_screenshot(save_path)

@app.post("/api/system/mouse-move")
async def move_mouse_endpoint(req: dict):
    """Move mouse to position"""
    if not SYSTEM_OPS_AVAILABLE:
        return _system_ops_unavailable()
    x = req.get("x")
    y = req.get("y")
    if x is None or y is None:
        return {"status": "error", "message": "x and y required"}
    return system_ops.move_mouse(int(x), int(y))

@app.post("/api/system/mouse-click")
async def click_mouse_endpoint(req: dict):
    """Click mouse at position"""
    if not SYSTEM_OPS_AVAILABLE:
        return _system_ops_unavailable()
    x = req.get("x")
    y = req.get("y")
    button = req.get("button", "left")
    if x is None or y is None:
        return {"status": "error", "message": "x and y required"}
    return system_ops.click_mouse(int(x), int(y), button)

@app.post("/api/system/type-text")
async def type_text_endpoint(req: dict):
    """Type text using keyboard"""
    if not SYSTEM_OPS_AVAILABLE:
        return _system_ops_unavailable()
    text = req.get("text")
    interval = req.get("interval", 0.1)
    if not text:
        return {"status": "error", "message": "text required"}
    return system_ops.type_text(text, interval)

@app.post("/api/system/press-key")
async def press_key_endpoint(req: dict):
    """Press a keyboard key"""
    if not SYSTEM_OPS_AVAILABLE:
        return _system_ops_unavailable()
    key = req.get("key")
    if not key:
        return {"status": "error", "message": "key required"}
    return system_ops.press_key(key)

@app.post("/api/system/open-file")
async def open_file_endpoint(req: dict):
    """Open a file with default application"""
    if not SYSTEM_OPS_AVAILABLE:
        return _system_ops_unavailable()
    file_path = req.get("file_path")
    if not file_path:
        return {"status": "error", "message": "file_path required"}
    return system_ops.open_file(file_path)

@app.get("/api/system/windows")
async def get_open_windows():
    """Get list of open windows"""
    if not SYSTEM_OPS_AVAILABLE:
        return _system_ops_unavailable()
    return system_ops.get_open_windows()

@app.post("/api/system/window-focus")
async def focus_window_endpoint(req: dict):
    """Focus a window by title"""
    if not SYSTEM_OPS_AVAILABLE:
        return _system_ops_unavailable()
    window_title = req.get("window_title")
    if not window_title:
        return {"status": "error", "message": "window_title required"}
    return system_ops.focus_window(window_title)

# =========================================================
# Session Management Endpoints
# =========================================================
@app.post("/api/session/extend")
async def extend_session_endpoint(req: dict):
    """Extend current session on page reload"""
    session_id = req.get("session_id")
    if not session_id:
        return {"status": "error", "message": "session_id required"}
    
    is_valid, username = session_manager.validate_session(session_id, update_activity=True)
    if not is_valid:
        return {
            "status": "session_expired",
            "message": "Session expired. Please login again.",
            "action": "redirect_to_login"
        }
    
    # Extend session
    extended = session_manager.extend_session(session_id)
    
    return {
        "status": "success" if extended else "error",
        "message": "Session extended" if extended else "Failed to extend session",
        "username": username,
        "session_info": session_manager.get_session_info(session_id)
    }

@app.post("/api/session/check")
async def check_session_endpoint(req: dict):
    """Check if session is still valid"""
    session_id = req.get("session_id")
    if not session_id:
        return {"valid": False, "message": "No session_id provided"}
    
    is_valid, username = session_manager.validate_session(session_id, update_activity=False)
    
    return {
        "valid": is_valid,
        "username": username,
        "session_info": session_manager.get_session_info(session_id) if is_valid else None
    }

@app.post("/api/session/logout")
async def logout_session_endpoint(req: dict):
    """Logout from current session"""
    session_id = req.get("session_id")
    if not session_id:
        return {"status": "error", "message": "session_id required"}
    
    success = session_manager.invalidate_session(session_id)
    
    return {
        "status": "success" if success else "error",
        "message": "Logged out successfully" if success else "Session not found"
    }

@app.get("/api/session/stats")
async def get_session_stats():
    """Get session statistics"""
    return session_manager.get_session_stats()

# =========================================================
# Startup Event
# =========================================================

@app.on_event("startup")
async def startup_event():
    print("[OK] Jarvis server startup event running")
    try:
        database._ensure_connected()
        print("[DB] Connection check complete")
    except Exception as e:
        print(f"[INFO] DB error during startup (will retry): {e}")
    
    # Start session cleanup task
    try:
        # start_session_cleanup_task()
        print("[OK] Session cleanup task skipped for now")
    except Exception as e:
        print(f"[INFO] Could not start session cleanup (already running): {e}")
    
    print("[OK] Jarvis server started and git-sync initialized.")


# Shutdown event removed - let background threads continue gracefully
# =========================================================
# Serve Frontend (React build)
# =========================================================
frontend_build_path = os.path.join(os.getcwd(), "jarvis-frontend", "build")

if os.path.exists(frontend_build_path):
    app.mount("/", StaticFiles(directory=frontend_build_path, html=True), name="frontend")
else:
    @app.get("/")
    async def root_fallback():
        return JSONResponse({"message": "Frontend not built. Run `npm run build`."}, status_code=404)
