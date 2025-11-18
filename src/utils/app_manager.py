# src/utils/app_manager.py
"""
Application Management System
Allows Jarvis to open, close, switch, and manage applications on the PC
"""
import os
import platform
import subprocess
import time
from typing import Dict, List, Optional, Tuple

# Process utilities - optional dependency
try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False
    psutil = None

# Windows-specific imports
WIN32_AVAILABLE = False
win32gui = None
win32con = None
win32process = None

if platform.system() == "Windows":
    try:
        import win32gui
        import win32con
        import win32process
        WIN32_AVAILABLE = True
    except ImportError:
        WIN32_AVAILABLE = False
        win32gui = None
        win32con = None
        win32process = None
else:
    WIN32_AVAILABLE = False


class AppManager:
    """Manage applications and windows on the system"""
    
    def __init__(self):
        self.running_apps = {}
        self.app_paths = self._load_app_paths()
        self.active_window = None
    
    def _load_app_paths(self) -> Dict[str, str]:
        """Load common application paths"""
        system = platform.system()
        paths = {}
        
        if system == "Windows":
            paths = {
                "notepad": "notepad.exe",
                "calculator": "calc.exe",
                "paint": "mspaint.exe",
                "chrome": r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                "firefox": r"C:\Program Files\Mozilla Firefox\firefox.exe",
                "edge": r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                "vscode": r"C:\Users\{}\AppData\Local\Programs\Microsoft VS Code\Code.exe".format(os.getenv("USERNAME", "")),
                "excel": r"C:\Program Files\Microsoft Office\root\Office16\EXCEL.EXE",
                "word": r"C:\Program Files\Microsoft Office\root\Office16\WINWORD.EXE",
                "powerpoint": r"C:\Program Files\Microsoft Office\root\Office16\POWERPNT.EXE",
                "outlook": r"C:\Program Files\Microsoft Office\root\Office16\OUTLOOK.EXE",
                "explorer": "explorer.exe",
                "cmd": "cmd.exe",
                "powershell": "powershell.exe",
                "taskmgr": "taskmgr.exe",
            }
        elif system == "Darwin":  # macOS
            paths = {
                "safari": "/Applications/Safari.app",
                "chrome": "/Applications/Google Chrome.app",
                "firefox": "/Applications/Firefox.app",
                "finder": "/System/Library/CoreServices/Finder.app",
                "terminal": "/Applications/Utilities/Terminal.app",
                "textedit": "/Applications/TextEdit.app",
                "calculator": "/Applications/Calculator.app",
            }
        else:  # Linux
            paths = {
                "firefox": "firefox",
                "chrome": "google-chrome",
                "gedit": "gedit",
                "nano": "nano",
                "terminal": "gnome-terminal",
            }
        
        return paths
    
    def open_app(self, app_name: str, args: Optional[List[str]] = None) -> Dict:
        """Open an application"""
        try:
            app_name_lower = app_name.lower().strip()
            
            # Check if app is in known paths
            if app_name_lower in self.app_paths:
                app_path = self.app_paths[app_name_lower]
            else:
                # Try to find app by name
                app_path = self._find_app(app_name)
            
            if not app_path:
                return {
                    "status": "error",
                    "message": f"Application '{app_name}' not found"
                }
            
            # Open application
            if platform.system() == "Windows":
                if args:
                    subprocess.Popen([app_path] + args, shell=False)
                else:
                    subprocess.Popen(app_path, shell=False)
            elif platform.system() == "Darwin":
                subprocess.Popen(["open", "-a", app_path] + (args or []))
            else:  # Linux
                subprocess.Popen([app_path] + (args or []))
            
            time.sleep(1)  # Wait for app to start
            
            # Track running app
            self.running_apps[app_name_lower] = {
                "name": app_name,
                "path": app_path,
                "started_at": time.time()
            }
            
            return {
                "status": "success",
                "message": f"Opened {app_name}",
                "app": app_name_lower
            }
        except Exception as e:
            return {
                "status": "error",
                "message": f"Failed to open {app_name}: {str(e)}"
            }
    
    def _find_app(self, app_name: str) -> Optional[str]:
        """Try to find application by name"""
        system = platform.system()
        
        if system == "Windows":
            # Try common locations
            common_paths = [
                r"C:\Program Files",
                r"C:\Program Files (x86)",
                os.path.expanduser("~\\AppData\\Local\\Programs"),
            ]
            
            for base_path in common_paths:
                for root, dirs, files in os.walk(base_path):
                    for file in files:
                        if app_name.lower() in file.lower() and file.endswith('.exe'):
                            return os.path.join(root, file)
        
        return None
    
    def close_app(self, app_name: str) -> Dict:
        """Close an application"""
        if not PSUTIL_AVAILABLE or not psutil:
            return {
                "status": "error",
                "message": "Process management not available (psutil not installed)"
            }
        
        try:
            app_name_lower = app_name.lower().strip()
            
            # Find process by name
            for proc in psutil.process_iter(['pid', 'name']):
                try:
                    proc_name = proc.info['name'].lower()
                    if app_name_lower in proc_name or proc_name in app_name_lower:
                        proc.terminate()
                        time.sleep(0.5)
                        if proc.is_running():
                            proc.kill()
                        
                        if app_name_lower in self.running_apps:
                            del self.running_apps[app_name_lower]
                        
                        return {
                            "status": "success",
                            "message": f"Closed {app_name}"
                        }
                except Exception:
                    continue
            
            return {
                "status": "error",
                "message": f"Application '{app_name}' not found or already closed"
            }
        except Exception as e:
            return {
                "status": "error",
                "message": f"Failed to close {app_name}: {str(e)}"
            }
    
    def switch_to_app(self, app_name: str) -> Dict:
        """Switch to an application window"""
        if platform.system() == "Windows" and not WIN32_AVAILABLE:
            return {
                "status": "error",
                "message": "Windows API not available"
            }
        
        try:
            app_name_lower = app_name.lower().strip()
            
            if platform.system() == "Windows" and WIN32_AVAILABLE and win32gui and win32con:
                def enum_handler(hwnd, ctx):
                    if win32gui.IsWindowVisible(hwnd):
                        window_title = win32gui.GetWindowText(hwnd)
                        if app_name_lower in window_title.lower():
                            win32gui.SetForegroundWindow(hwnd)
                            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                            return False
                    return True
                
                win32gui.EnumWindows(enum_handler, None)
                self.active_window = app_name_lower
                return {
                    "status": "success",
                    "message": f"Switched to {app_name}"
                }
            else:
                # For other platforms or headless, try to bring app to front
                return self.open_app(app_name)
        except Exception as e:
            return {
                "status": "error",
                "message": f"Failed to switch to {app_name}: {str(e)}"
            }
    
    def list_running_apps(self) -> List[Dict]:
        """List all running applications"""
        if not PSUTIL_AVAILABLE or not psutil:
            return []
        
        apps = []
        for proc in psutil.process_iter(['pid', 'name', 'memory_info']):
            try:
                apps.append({
                    "name": proc.info['name'],
                    "pid": proc.info['pid'],
                    "memory_mb": proc.info['memory_info'].rss / 1024 / 1024
                })
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return apps
    
    def get_active_window(self) -> Optional[Dict]:
        """Get currently active window"""
        if platform.system() == "Windows" and WIN32_AVAILABLE and win32gui:
            try:
                hwnd = win32gui.GetForegroundWindow()
                window_title = win32gui.GetWindowText(hwnd)
                return {
                    "title": window_title,
                    "hwnd": hwnd
                }
            except:
                return None
        return None
    
    def execute_command(self, command: str, wait: bool = True) -> Dict:
        """Execute a system command"""
        try:
            if platform.system() == "Windows":
                result = subprocess.run(
                    command,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=30 if wait else None
                )
            else:
                result = subprocess.run(
                    command,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=30 if wait else None
                )
            
            return {
                "status": "success",
                "stdout": result.stdout,
                "stderr": result.stderr,
                "returncode": result.returncode
            }
        except subprocess.TimeoutExpired:
            return {
                "status": "error",
                "message": "Command timed out"
            }
        except Exception as e:
            return {
                "status": "error",
                "message": str(e)
            }


# Global instance
app_manager = AppManager()

