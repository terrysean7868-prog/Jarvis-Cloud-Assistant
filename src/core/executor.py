# executor.py
import os
import shutil
import asyncio
import time
import webbrowser
import subprocess
import platform
import re
from typing import List
import json
from urllib.parse import urljoin
from src.core.jarvis_brain import JarvisBrain
from src.config import runtime_defaults as rd
from src.config import env
from src.config.settings import settings as jarvis_settings
from src.config.secrets import n8n_secrets
from src.utils.git_sync import git_sync  # ✅ now importing the function, not a class
from src.utils.self_update import self_update_file, self_add_feature, parse_voice_command
from src.utils.screen_access import screen_access
from src.utils.email_generator import email_generator
from src.utils.app_manager import app_manager
from src.utils.task_manager import task_manager
from src.utils.error_handler import error_handler

_n8n = n8n_secrets()
N8N_WEBHOOK_BASE = _n8n.base_url
N8N_WEBHOOK_TOKEN = _n8n.token
N8N_WEBHOOK_SECRET = _n8n.secret

# Optional dependencies (avoid hard failures on cloud builds)
SELENIUM_AVAILABLE = False
webdriver = None
By = None
Service = None
Options = None
try:
    from selenium import webdriver  # type: ignore
    from selenium.webdriver.common.by import By  # type: ignore
    from selenium.webdriver.chrome.service import Service  # type: ignore
    from selenium.webdriver.chrome.options import Options  # type: ignore
    SELENIUM_AVAILABLE = True
except Exception:
    SELENIUM_AVAILABLE = False

TRANSLATE_AVAILABLE = False
Translator = None
try:
    from googletrans import Translator  # type: ignore
    TRANSLATE_AVAILABLE = True
except Exception:
    TRANSLATE_AVAILABLE = False

AIOHTTP_AVAILABLE = False
try:
    import aiohttp  # type: ignore
    AIOHTTP_AVAILABLE = True
except Exception:
    AIOHTTP_AVAILABLE = False

# Runtime mode (single source of truth)
CLOUD_MODE = bool(jarvis_settings.cloud_mode)
AUTO_GIT_SYNC = bool(rd.AUTO_GIT_SYNC)

# Optional: pyautogui for screen navigation (only on desktop with display)
# Check for headless environment before importing
PYAUTOGUI_AVAILABLE = False
pyautogui = None

# Detect headless environment - pyautogui requires DISPLAY on Linux
_is_headless_env = False
if platform.system() != "Windows":
    if "DISPLAY" not in os.environ:
        _is_headless_env = True
    # Check for server environment indicators
    # Render sets PORT, RENDER_SERVICE_ID, or path contains /opt/render
    if (env.get("RENDER") or env.get("DYNO") or env.get("DOCKER") or 
        env.get("PORT") or "/opt/render" in os.getcwd()):
        _is_headless_env = True

if not _is_headless_env:
    try:
        # Set DISPLAY if needed to prevent KeyError in mouseinfo
        if platform.system() != "Windows" and "DISPLAY" not in os.environ:
            os.environ["DISPLAY"] = ":0"
        import pyautogui
        PYAUTOGUI_AVAILABLE = True
    except (ImportError, KeyError, Exception):
        # KeyError: 'DISPLAY' environment variable missing (headless/server environment)
        PYAUTOGUI_AVAILABLE = False
        pyautogui = None
else:
    # Headless environment - skip import entirely
    PYAUTOGUI_AVAILABLE = False
    pyautogui = None

# Internet access
try:
    from src.internet.internet import get_internet, close_internet
    INTERNET_AVAILABLE = True
except ImportError:
    INTERNET_AVAILABLE = False

class ActionExecutor:
    def __init__(self, brain: JarvisBrain):
        self.brain = brain
        self.stop_requested = False
        # Simple in-memory cache to reduce repeated fetch_url latency.
        # Key: normalized url, Value: (ts, result_dict)
        self._fetch_url_cache: dict[str, tuple[float, dict]] = {}
        # Mapping of common website names to URLs
        self.url_map = {
            'youtube': 'https://www.youtube.com',
            'linkedin': 'https://www.linkedin.com',
            'google': 'https://www.google.com',
            'github': 'https://www.github.com',
            'facebook': 'https://www.facebook.com',
            'twitter': 'https://www.twitter.com',
            'instagram': 'https://www.instagram.com',
            'reddit': 'https://www.reddit.com',
            'stack overflow': 'https://stackoverflow.com',
            'stackoverflow': 'https://stackoverflow.com',
            'wikipedia': 'https://www.wikipedia.org',
            'gmail': 'https://mail.google.com',
            'weather': 'https://weather.com',
            'chatgpt': 'https://chatgpt.com',
            'openai': 'https://openai.com',
            'netflix': 'https://www.netflix.com',
            'amazon': 'https://www.amazon.com',
        }

        # Extended URL mapping with more websites
        self.url_map.update({
            'bing': 'https://www.bing.com',
            'yahoo': 'https://www.yahoo.com',
            'duckduckgo': 'https://duckduckgo.com',
            'spotify': 'https://www.spotify.com',
            'apple': 'https://www.apple.com',
            'microsoft': 'https://www.microsoft.com',
            'tesla': 'https://www.tesla.com',
            'cnn': 'https://www.cnn.com',
            'bbc': 'https://www.bbc.com',
            'nytimes': 'https://www.nytimes.com',
        })
        self.browser = None
        self.translator = Translator() if TRANSLATE_AVAILABLE and Translator else None
        self.default_language = "en"  # Default language is English

    async def process_actions(self, actions: List[dict], user: str = "user", password: str = "", language: str = "en"):
        """
        Executes actions proposed by JarvisBrain, such as writing, editing,
        deleting, moving files, opening URLs, or searching the web.
        After applying file changes, it triggers an automatic Git push to the main branch.
        """
        # Check for stop request
        if self.stop_requested or task_manager.stop_requested:
            self.stop_requested = False
            task_manager.stop_requested = False
            return [{"status": "stopped", "message": "Operation stopped by user"}]

        # NOTE: API auth should be enforced at the HTTP layer.
        # The executor intentionally does not implement its own credential system.

        results = []
        changed_files = []

        for action in actions:
            action_type = action.get("type")
            path = os.path.normpath(action.get("path", "")) if action.get("path") else None

            # Cloud deployments must never execute local/PC control, file writes, or self-modifying actions.
            if CLOUD_MODE:
                if action_type in {
                    "write", "edit", "delete", "move",
                    "execute_command",
                    "open_app", "close_app", "switch_app",
                    "capture_screen", "screen_navigation",
                    "self_update", "self_add",
                    # Maintenance / self-healing actions can execute shell commands.
                    "check_errors", "fix_errors", "check_render_logs",
                }:
                    results.append({
                        "status": "forbidden",
                        "action_type": action_type,
                        "message": "Action disabled in cloud mode"
                    })
                    continue

            # Handle internet search actions
            if action_type == "web_search":
                result = await self._handle_web_search(action)
                results.append(result)
                continue
            
            # Handle web fetch actions
            if action_type == "fetch_url":
                result = await self._handle_fetch_url(action)
                results.append(result)
                continue

            # Handle N8N webhook actions
            if action_type == "n8n_webhook":
                if not AIOHTTP_AVAILABLE:
                    results.append({
                        "status": "error",
                        "action_type": "n8n_webhook",
                        "message": "aiohttp not available",
                    })
                    continue

                if not N8N_WEBHOOK_BASE:
                    results.append({
                        "status": "error",
                        "action_type": "n8n_webhook",
                        "message": "N8N webhook base not configured (JARVIS_N8N_WEBHOOK_BASE)",
                    })
                    continue

                raw_url = (action or {}).get("url")
                raw_path = (action or {}).get("path")
                if raw_url:
                    url = str(raw_url).strip()
                    if not url.startswith(N8N_WEBHOOK_BASE):
                        results.append({
                            "status": "forbidden",
                            "action_type": "n8n_webhook",
                            "message": "Webhook URL must start with configured base",
                        })
                        continue
                else:
                    path = str(raw_path or "").strip().lstrip("/")
                    if not path:
                        results.append({
                            "status": "error",
                            "action_type": "n8n_webhook",
                            "message": "Webhook path is required",
                        })
                        continue
                    url = urljoin(N8N_WEBHOOK_BASE + "/", path)

                method = str((action or {}).get("method") or "POST").strip().upper()
                if method not in ("POST", "GET", "PUT", "PATCH", "DELETE"):
                    results.append({
                        "status": "error",
                        "action_type": "n8n_webhook",
                        "message": f"Unsupported method: {method}",
                    })
                    continue

                payload = (action or {}).get("payload")
                if payload is None:
                    payload = (action or {}).get("data")

                headers = {"Content-Type": "application/json"}
                if N8N_WEBHOOK_TOKEN:
                    headers["Authorization"] = f"Bearer {N8N_WEBHOOK_TOKEN}"
                if N8N_WEBHOOK_SECRET:
                    headers["X-Jarvis-Secret"] = N8N_WEBHOOK_SECRET

                try:
                    timeout = aiohttp.ClientTimeout(total=25)
                    async with aiohttp.ClientSession(timeout=timeout) as session:
                        if method == "GET" and isinstance(payload, dict):
                            req = session.request(method, url, params=payload, headers=headers)
                        else:
                            req = session.request(method, url, json=payload, headers=headers)
                        async with req as resp:
                            text = await resp.text()
                            data = None
                            try:
                                data = json.loads(text) if text else None
                            except Exception:
                                data = text
                            if isinstance(data, str) and len(data) > 2000:
                                data = data[:2000] + "…"
                            results.append({
                                "status": "success" if 200 <= resp.status < 300 else "error",
                                "action_type": "n8n_webhook",
                                "code": resp.status,
                                "url": url,
                                "response": data,
                            })
                except Exception as e:
                    results.append({
                        "status": "error",
                        "action_type": "n8n_webhook",
                        "message": str(e),
                    })
                continue
            
            # Handle URL opening actions
            if action_type == "open_url":
                result = await self._handle_open_url(action)
                results.append(result)
                continue
            
            # Handle search actions
            if action_type == "search":
                query = action.get("query", "")
                if query:
                    search_url = f"https://www.google.com/search?q={query.replace(' ', '+')}"
                    try:
                        self._open_url(search_url)
                        results.append({
                            "status": "opened",
                            "action_type": "search",
                            "query": query
                        })
                    except Exception as e:
                        results.append({
                            "status": "error",
                            "action_type": "search",
                            "error": str(e)
                        })
                continue

            # Handle self-update actions
            if action_type == "self_update":
                if getattr(self.brain, "require_manual_approval", True):
                    results.append({
                        "status": "approval_required",
                        "action_type": "self_update",
                        "message": "Self-update requires manual approval (set REQUIRE_MANUAL_APPROVAL=false to allow)."
                    })
                    continue
                description = action.get("description", "")
                file_path = action.get("file_path", "")
                if description and file_path:
                    result = self_update_file(description, file_path)
                    results.append(result)
                    if result.get("status") == "success":
                        changed_files.append(result.get("path", ""))
                continue

            # Handle self-add actions
            if action_type == "self_add":
                if getattr(self.brain, "require_manual_approval", True):
                    results.append({
                        "status": "approval_required",
                        "action_type": "self_add",
                        "message": "Self-add requires manual approval (set REQUIRE_MANUAL_APPROVAL=false to allow)."
                    })
                    continue
                description = action.get("description", "")
                feature_type = action.get("feature_type", "module")
                result = self_add_feature(description, feature_type)
                results.append(result)
                if result.get("status") == "success":
                    changed_files.append(result.get("path", ""))
                continue

            # Handle screen capture actions
            if action_type == "capture_screen":
                reg = action.get("region")
                region = None
                if isinstance(reg, dict):
                    try:
                        region = (int(reg.get("x", 0)), int(reg.get("y", 0)), int(reg.get("width", 0)), int(reg.get("height", 0)))
                    except Exception:
                        region = None
                screenshot_info = screen_access.take_screenshot_info(region=region, include_base64=False)
                results.append({
                    "status": "success",
                    "action_type": "capture_screen",
                    "screenshot": screenshot_info
                })
                continue

            # Handle screen navigation actions
            if action_type == "screen_navigation":
                result = await self._handle_screen_navigation(action)
                results.append(result)
                continue

            # Handle email generation actions
            if action_type == "generate_email":
                recipient = action.get("recipient", "")
                subject = action.get("subject")
                body_prompt = action.get("body_prompt", action.get("description", ""))
                tone = action.get("tone", "professional")
                result = email_generator.generate_email(recipient, subject, body_prompt, tone)
                results.append(result)
                continue

            # Handle application management actions
            if action_type == "open_app":
                app_name = action.get("app_name", "")
                args = action.get("args", [])
                result = app_manager.open_app(app_name, args)
                results.append(result)
                continue

            if action_type == "close_app":
                app_name = action.get("app_name", "")
                result = app_manager.close_app(app_name)
                results.append(result)
                continue

            if action_type == "switch_app":
                app_name = action.get("app_name", "")
                result = app_manager.switch_to_app(app_name)
                results.append(result)
                continue

            if action_type == "execute_command":
                command = action.get("command", "")
                wait = action.get("wait", True)
                # Safety backstop: refuse commands that could damage OS/system.
                if self._is_dangerous_command(str(command or "")):
                    results.append({
                        "status": "forbidden",
                        "action_type": "execute_command",
                        "message": "Blocked dangerous command (OS/system safety)."
                    })
                    continue
                result = app_manager.execute_command(command, wait)
                results.append(result)
                continue

            # Handle task management
            if action_type == "create_task":
                description = action.get("description", "")
                steps = action.get("steps", [])
                priority = action.get("priority", 5)
                task_id = task_manager.create_task(description, steps, priority)
                results.append({
                    "status": "success",
                    "task_id": task_id,
                    "message": f"Task created: {description}"
                })
                continue

            if action_type == "stop_task" or action_type == "stop":
                result = task_manager.stop_current_task()
                results.append(result)
                continue

            # Handle error checking and fixing
            if action_type == "check_errors" or action_type == "fix_errors":
                result = error_handler.monitor_and_fix()
                results.append(result)
                continue

            if action_type == "check_render_logs":
                result = error_handler.check_render_logs()
                results.append(result)
                continue

            # File operations (existing code)
            # For file actions, enforce sandbox.
            if action_type in ("read", "list", "mkdir", "write", "edit", "delete", "move", "copy", "cleanup"):
                # cleanup has no path
                if action_type != "cleanup":
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

                elif action_type == "read":
                    if not os.path.exists(path) or not os.path.isfile(path):
                        results.append({"status": "not_found", "path": path})
                    else:
                        with open(path, "r", encoding="utf-8") as f:
                            results.append({"status": "success", "path": path, "content": f.read()})

                elif action_type == "list":
                    if not os.path.exists(path) or not os.path.isdir(path):
                        results.append({"status": "not_found", "path": path})
                    else:
                        items = []
                        for name in os.listdir(path):
                            full = os.path.join(path, name)
                            items.append({"name": name, "type": "directory" if os.path.isdir(full) else "file"})
                        results.append({"status": "success", "path": path, "items": items})

                elif action_type == "mkdir":
                    os.makedirs(path, exist_ok=True)
                    changed_files.append(path)
                    results.append({"status": "success", "path": path})

                elif action_type == "copy":
                    src = os.path.normpath(action.get("source", ""))
                    dest = os.path.normpath(action.get("destination", ""))
                    if not src or not dest:
                        results.append({"status": "error", "message": "source and destination required", "action": action})
                        continue
                    if not self.brain.is_path_allowed(src) or not self.brain.is_path_allowed(dest):
                        results.append({"status": "forbidden", "action": action})
                        continue
                    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
                    shutil.copy2(src, dest)
                    changed_files.append(dest)
                    results.append({"status": "copied", "from": src, "to": dest})

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

                elif action_type == "cleanup":
                    # Lightweight cleanup of common cache folders inside repo
                    deleted = 0
                    for root, dirs, _files in os.walk("."):
                        for cache_dir in ("__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"):
                            if cache_dir in dirs:
                                try:
                                    shutil.rmtree(os.path.join(root, cache_dir))
                                    deleted += 1
                                except Exception:
                                    pass
                    results.append({"status": "success", "deleted": deleted})

                else:
                    results.append({"status": "unknown_action", "action": action})

            except Exception as e:
                results.append({
                    "status": "error",
                    "error": str(e),
                    "action": action
                })

        # === Git sync after changes (opt-in only, never in cloud) ===
        if changed_files and AUTO_GIT_SYNC and (not CLOUD_MODE):
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

    @staticmethod
    def _is_dangerous_command(command: str) -> bool:
        c = (command or "").strip()
        if not c:
            return False
        cl = c.lower()

        high_risk_patterns = [
            r"\bformat\b",
            r"\bdiskpart\b",
            r"\bmkfs(\.[a-z0-9]+)?\b",
            r"\bfdisk\b",
            r"\bparted\b",
            r"\bgparted\b",
            r"\b(wipefs|dd)\b",
            r"\bbootrec\b",
            r"\bbcdedit\b",
            r"\breg(ed(it)?|\s+add|\s+delete|\s+import)\b",
            r"\bdism\b.*\/(remove-package|disable-feature)",
            r"remove-item\b.*\b(-recurse|-force)\b",
        ]
        for pat in high_risk_patterns:
            try:
                if re.search(pat, cl, re.IGNORECASE):
                    return True
            except Exception:
                continue

        if re.search(r"\brm\b\s+.*\s-\s*rf\s+/(?:\s|$)", cl):
            return True
        if "--no-preserve-root" in cl and "rm" in cl and "/" in cl:
            return True

        delete_words = ("rm ", " del ", "erase", "rmdir", " rd ", "remove-item")
        system_markers = (
            "c:\\windows",
            "\\windows\\system32",
            "system32",
            "c:\\program files",
            "c:\\program files (x86)",
            "c:\\programdata",
            "system volume information",
            "/etc/",
            "/bin/",
            "/sbin/",
            "/usr/",
            "/boot/",
            "/system/",
            "/library/",
        )
        if any(dw in cl for dw in delete_words) and any(sm in cl for sm in system_markers):
            return True

        return False

    def _open_url(self, url: str):
        """
        Open a URL in the default web browser.
        Works across Windows, macOS, and Linux.
        """
        try:
            webbrowser.open(url)
        except Exception as e:
            # Fallback for different platforms
            try:
                if platform.system() == "Darwin":  # macOS
                    subprocess.Popen(["open", url])
                elif platform.system() == "Windows":
                    os.startfile(url)  # type: ignore
                else:  # Linux and others
                    subprocess.Popen(["xdg-open", url])
            except Exception as fallback_err:
                raise RuntimeError(f"Failed to open URL {url}: {fallback_err}")
    
    async def _handle_web_search(self, action: dict) -> dict:
        """
        Handle web search actions
        
        Args:
            action: Action dict with 'query' key
            
        Returns:
            Search result dict
        """
        if not INTERNET_AVAILABLE:
            return {
                "status": "error",
                "error": "Internet module not available"
            }
        
        try:
            query = action.get("query", "")
            num_results = action.get("num_results", 5)
            
            print(f"🔍 Searching web for: {query}")
            internet = await get_internet()
            results = await internet.search(query, num_results=num_results)
            
            return {
                "status": "success",
                "action": "web_search",
                "query": query,
                "results_count": len(results),
                "results": results[:num_results]
            }
            
        except Exception as e:
            print(f"❌ Web search failed: {str(e)}")
            return {
                "status": "error",
                "action": "web_search",
                "error": str(e)
            }
    
    async def _handle_fetch_url(self, action: dict) -> dict:
        """
        Handle fetch URL actions to get webpage content
        
        Args:
            action: Action dict with 'url' key
            
        Returns:
            Fetch result dict
        """
        if not INTERNET_AVAILABLE:
            return {
                "status": "error",
                "error": "Internet module not available"
            }
        
        try:
            url = action.get("url", "")

            # Normalize URL a bit for caching.
            cache_key = (url or "").strip()
            if cache_key:
                cache_key = re.sub(r"\s+", "", cache_key)

            ttl = int(rd.FETCH_URL_CACHE_SECONDS)
            ttl = max(0, min(ttl, 60 * 60))

            if ttl and cache_key:
                cached = self._fetch_url_cache.get(cache_key)
                if cached:
                    ts, payload = cached
                    if (time.time() - ts) <= ttl and isinstance(payload, dict):
                        out = dict(payload)
                        out["cached"] = True
                        return out
            
            print(f"📥 Fetching URL: {url}")
            internet = await get_internet()
            result = await internet.fetch_webpage(url, include_content=True)
            
            if result:
                out = {
                    "status": "success",
                    "action": "fetch_url",
                    "url": url,
                    "title": result.get('title'),
                    "summary": result.get('summary', '')[:500]
                }
                if ttl and cache_key:
                    try:
                        self._fetch_url_cache[cache_key] = (time.time(), dict(out))
                        # Best-effort size cap
                        if len(self._fetch_url_cache) > 128:
                            # Drop oldest ~25%
                            items = sorted(self._fetch_url_cache.items(), key=lambda kv: kv[1][0])
                            for k, _v in items[: max(1, len(items) // 4)]:
                                self._fetch_url_cache.pop(k, None)
                    except Exception:
                        pass
                return out
            else:
                return {
                    "status": "error",
                    "action": "fetch_url",
                    "error": "Failed to fetch URL"
                }
                
        except Exception as e:
            print(f"❌ URL fetch failed: {str(e)}")
            return {
                "status": "error",
                "action": "fetch_url",
                "error": str(e)
            }
    
    def _initialize_browser(self):
        """Initialize the Selenium WebDriver."""
        if not self.browser:
            chrome_options = Options()
            chrome_options.add_argument("--headless")
            chrome_options.add_argument("--disable-gpu")
            chrome_options.add_argument("--no-sandbox")
            service = Service("chromedriver")  # Ensure chromedriver is in PATH
            self.browser = webdriver.Chrome(service=service, options=chrome_options)

    async def _handle_open_url(self, action: dict):
        """
        Handles the action to open a URL in the browser.
        """
        raw_url = (action.get("url") or "").strip()
        url_name = (action.get("url_name") or "").strip().lower()

        # Backward/forward compatible:
        # - New schema: {"type":"open_url","url":"https://..."}
        # - Legacy schema: {"type":"open_url","url_name":"youtube"}
        url = ""
        if raw_url:
            url = raw_url
        elif url_name:
            url = self.url_map.get(url_name, "")
            if not url:
                # Accept a domain-like name (e.g., example.com) or fallback to https://www.<name>.com
                if "." in url_name:
                    url = f"https://{url_name}"
                else:
                    url = f"https://www.{url_name}.com"

        if not url:
            return {"status": "error", "message": "Missing url for open_url"}

        # If the user passed a bare domain without a scheme, assume https.
        if not re.match(r"^[a-zA-Z][a-zA-Z0-9+\-.]*://", url):
            url = f"https://{url}"

        # Prefer system default browser (works without selenium/chromedriver).
        try:
            self._open_url(url)
            return {"status": "success", "message": f"Opened {url} in browser"}
        except Exception:
            # Fallback: attempt selenium (may be unavailable in some environments)
            try:
                self._initialize_browser()
                self.browser.get(url)
                return {"status": "success", "message": f"Opened {url} in browser"}
            except Exception as e:
                return {"status": "error", "message": str(e)}

    async def _handle_screen_navigation(self, action: dict):
        """
        Handles screen navigation commands like moving the mouse, clicking, typing, etc.
        Enhanced with screen access capabilities.
        """
        command = action.get("command", "")
        
        try:
            if command == "capture_screen" or command == "screenshot":
                reg = action.get("region")
                region = None
                if isinstance(reg, dict):
                    try:
                        region = (int(reg.get("x", 0)), int(reg.get("y", 0)), int(reg.get("width", 0)), int(reg.get("height", 0)))
                    except Exception:
                        region = None
                screenshot_info = screen_access.take_screenshot_info(region=region, include_base64=False)
                return {
                    "status": "success",
                    "message": "Screen captured",
                    "screenshot": screenshot_info
                }

            elif command == "read_screen" or command == "ocr":
                region = action.get("region")
                text = screen_access.read_screen_text(region)
                return {
                    "status": "success",
                    "message": "Screen text extracted",
                    "text": text
                }

            elif command == "find_text":
                search_text = action.get("text", "")
                position = screen_access.find_text_on_screen(search_text)
                if position:
                    return {
                        "status": "success",
                        "message": f"Found text at {position}",
                        "position": position
                    }
                return {
                    "status": "error",
                    "message": "Text not found on screen"
                }

            elif command == "move_mouse":
                x, y = action.get("x", 0), action.get("y", 0)
                duration = action.get("duration", 0.5)
                if screen_access.move_mouse(x, y, duration):
                    return {"status": "success", "message": f"Moved mouse to ({x}, {y})"}
                return {"status": "error", "message": "Failed to move mouse"}

            elif command == "click":
                x = action.get("x")
                y = action.get("y")
                button = action.get("button", "left")
                
                if x is not None and y is not None:
                    if screen_access.click_at_position(x, y, button):
                        return {"status": "success", "message": f"Clicked at ({x}, {y})"}
                else:
                    # Click at current position
                    if PYAUTOGUI_AVAILABLE and pyautogui:
                        try:
                            pyautogui.click(button=button)
                            return {"status": "success", "message": f"Clicked {button} button"}
                        except:
                            return {"status": "error", "message": "Click failed - no display available"}
                
                return {"status": "error", "message": "Click failed - screen access not available in headless environment"}

            elif command == "type" or command == "type_text":
                text = action.get("text", "")
                interval = action.get("interval", 0.05)
                if screen_access.type_text(text, interval):
                    return {"status": "success", "message": f"Typed '{text}'"}
                return {"status": "error", "message": "Failed to type text"}

            elif command == "press_key":
                key = action.get("key", "")
                presses = action.get("presses", 1)
                if screen_access.press_key(key, presses):
                    return {"status": "success", "message": f"Pressed key '{key}'"}
                return {"status": "error", "message": "Failed to press key"}

            elif command == "scroll":
                x = action.get("x", 0)
                y = action.get("y", 0)
                clicks = action.get("clicks", 3)
                if screen_access.scroll(x, y, clicks):
                    return {"status": "success", "message": f"Scrolled ({x}, {y})"}
                return {"status": "error", "message": "Failed to scroll"}

            elif command == "get_mouse_position":
                pos = screen_access.get_mouse_position()
                return {
                    "status": "success",
                    "message": f"Mouse at {pos}",
                    "position": pos
                }

            else:
                return {"status": "error", "message": f"Unknown screen navigation command: {command}"}

        except Exception as e:
            return {"status": "error", "message": str(e)}

    async def _translate_response(self, response: str, target_language: str) -> str:
        """
        Translates the response to the target language.
        """
        try:
            if target_language != self.default_language:
                translated = self.translator.translate(response, dest=target_language)
                return translated.text
            return response
        except Exception as e:
            return f"Error in translation: {str(e)}"

    def close_browser(self):
        """Close the Selenium WebDriver."""
        if self.browser:
            self.browser.quit()
            self.browser = None

