# executor.py
import os
import shutil
import asyncio
import webbrowser
import subprocess
import platform
from typing import List
from src.core.jarvis_brain import JarvisBrain
from src.utils.git_sync import git_sync  # ✅ now importing the function, not a class
from src.utils.self_update import self_update_file, self_add_feature, parse_voice_command
from src.utils.screen_access import screen_access
from src.utils.email_generator import email_generator
from src.utils.app_manager import app_manager
from src.utils.task_manager import task_manager
from src.utils.error_handler import error_handler
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from googletrans import Translator

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
    if (os.getenv("RENDER") or os.getenv("DYNO") or os.getenv("DOCKER") or 
        os.getenv("PORT") or "/opt/render" in os.getcwd()):
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
        self.translator = Translator()
        self.default_language = "en"  # Default language is English
        self.authenticated_users = {"admin": "password123"}  # Example credentials

    def authenticate_user(self, username: str, password: str) -> bool:
        """
        Authenticates a user based on username and password.
        """
        return self.authenticated_users.get(username) == password

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

        # Authenticate user (if password provided)
        if password and not self.authenticate_user(user, password):
            return [{"status": "error", "message": "Authentication failed"}]

        results = []
        changed_files = []

        for action in actions:
            action_type = action.get("type")
            path = os.path.normpath(action.get("path", "")) if action.get("path") else None

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
                description = action.get("description", "")
                feature_type = action.get("feature_type", "module")
                result = self_add_feature(description, feature_type)
                results.append(result)
                if result.get("status") == "success":
                    changed_files.append(result.get("path", ""))
                continue

            # Handle screen capture actions
            if action_type == "capture_screen":
                screenshot_info = screen_access.take_screenshot_info()
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
            
            print(f"📥 Fetching URL: {url}")
            internet = await get_internet()
            result = await internet.fetch_webpage(url, include_content=True)
            
            if result:
                return {
                    "status": "success",
                    "action": "fetch_url",
                    "url": url,
                    "title": result.get('title'),
                    "summary": result.get('summary', '')[:500]
                }
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
        url_name = action.get("url_name", "").lower()
        url = self.url_map.get(url_name, f"https://www.{url_name}.com")

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
                screenshot_info = screen_access.take_screenshot_info()
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

