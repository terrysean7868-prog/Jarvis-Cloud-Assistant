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
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
import pyautogui

# Internet access
try:
    from src.internet.internet import get_internet, close_internet
    INTERNET_AVAILABLE = True
except ImportError:
    INTERNET_AVAILABLE = False


class ActionExecutor:
    def __init__(self, brain: JarvisBrain):
        self.brain = brain
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

    async def process_actions(self, actions: List[dict], user: str = "user"):
        """
        Executes actions proposed by JarvisBrain, such as writing, editing,
        deleting, moving files, opening URLs, or searching the web.
        After applying file changes, it triggers an automatic Git push to the main branch.
        """
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
        """
        command = action.get("command")
        try:
            if command == "move_mouse":
                x, y = action.get("x", 0), action.get("y", 0)
                pyautogui.moveTo(x, y, duration=0.5)
                return {"status": "success", "message": f"Moved mouse to ({x}, {y})"}

            elif command == "click":
                button = action.get("button", "left")
                pyautogui.click(button=button)
                return {"status": "success", "message": f"Clicked {button} button"}

            elif command == "type":
                text = action.get("text", "")
                pyautogui.typewrite(text)
                return {"status": "success", "message": f"Typed '{text}'"}

            elif command == "scroll":
                amount = action.get("amount", 0)
                pyautogui.scroll(amount)
                return {"status": "success", "message": f"Scrolled {amount}"}

            else:
                return {"status": "error", "message": "Unknown screen navigation command"}

        except Exception as e:
            return {"status": "error", "message": str(e)}

    def close_browser(self):
        """Close the Selenium WebDriver."""
        if self.browser:
            self.browser.quit()
            self.browser = None

