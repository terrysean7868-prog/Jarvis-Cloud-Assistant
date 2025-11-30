"""
Digital Assistant System Operations
Enables PC automation, screen operations, process management, and system control
"""

import os
import logging
import subprocess
import asyncio
from typing import Dict, Any, List, Optional
from pathlib import Path
from datetime import datetime

logger = logging.getLogger(__name__)

try:
    import pyautogui
    import psutil
    PYAUTOGUI_AVAILABLE = True
except ImportError:
    PYAUTOGUI_AVAILABLE = False
    logger.warning("pyautogui or psutil not available")

try:
    import win32gui
    import win32con
    import win32process
    PYWIN32_AVAILABLE = True
except ImportError:
    PYWIN32_AVAILABLE = False
    logger.warning("pywin32 not available for Windows automation")


class SystemOperations:
    """Handles system-level operations for digital assistant functionality"""
    
    @staticmethod
    def get_system_info() -> Dict[str, Any]:
        """Get current system information"""
        try:
            if not PYAUTOGUI_AVAILABLE:
                return {"status": "error", "message": "psutil not available"}
            
            return {
                "status": "success",
                "cpu_percent": psutil.cpu_percent(interval=1),
                "memory_percent": psutil.virtual_memory().percent,
                "disk_percent": psutil.disk_usage('/').percent,
                "boot_time": datetime.fromtimestamp(psutil.boot_time()).isoformat(),
                "cpu_count": psutil.cpu_count(),
                "process_count": len(psutil.pids())
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}
    
    @staticmethod
    def list_processes(filter_name: Optional[str] = None) -> Dict[str, Any]:
        """List running processes"""
        try:
            if not PYAUTOGUI_AVAILABLE:
                return {"status": "error", "message": "psutil not available"}
            
            processes = []
            for proc in psutil.process_iter(['pid', 'name', 'status']):
                try:
                    if filter_name and filter_name.lower() not in proc.name().lower():
                        continue
                    
                    processes.append({
                        "pid": proc.pid,
                        "name": proc.name(),
                        "status": proc.status()
                    })
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            
            return {
                "status": "success",
                "processes": processes,
                "count": len(processes)
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}
    
    @staticmethod
    def kill_process(process_name: str) -> Dict[str, Any]:
        """Kill a process by name"""
        try:
            if not PYAUTOGUI_AVAILABLE:
                return {"status": "error", "message": "psutil not available"}
            
            killed = []
            for proc in psutil.process_iter(['name']):
                try:
                    if process_name.lower() in proc.name().lower():
                        proc.kill()
                        killed.append(proc.name())
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            
            if killed:
                return {
                    "status": "success",
                    "message": f"Killed {len(killed)} process(es)",
                    "killed_processes": killed
                }
            else:
                return {
                    "status": "error",
                    "message": f"No processes found matching: {process_name}"
                }
        except Exception as e:
            return {"status": "error", "message": str(e)}
    
    @staticmethod
    def launch_application(app_path: str, args: Optional[List[str]] = None) -> Dict[str, Any]:
        """Launch an application"""
        try:
            cmd = [app_path] + (args or [])
            process = subprocess.Popen(cmd)
            
            return {
                "status": "success",
                "message": f"Application launched: {app_path}",
                "pid": process.pid
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}
    
    @staticmethod
    def execute_command(command: str, timeout: int = 30) -> Dict[str, Any]:
        """Execute a shell command"""
        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout
            )
            
            return {
                "status": "success",
                "command": command,
                "return_code": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr
            }
        except subprocess.TimeoutExpired:
            return {
                "status": "error",
                "message": f"Command timed out after {timeout}s"
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}
    
    @staticmethod
    def get_screen_info() -> Dict[str, Any]:
        """Get screen/display information"""
        try:
            if not PYAUTOGUI_AVAILABLE:
                return {"status": "error", "message": "pyautogui not available"}
            
            screen_size = pyautogui.size()
            mouse_pos = pyautogui.position()
            
            return {
                "status": "success",
                "screen_width": screen_size.width,
                "screen_height": screen_size.height,
                "mouse_x": mouse_pos.x,
                "mouse_y": mouse_pos.y
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}
    
    @staticmethod
    def take_screenshot(save_path: Optional[str] = None) -> Dict[str, Any]:
        """Take a screenshot"""
        try:
            if not PYAUTOGUI_AVAILABLE:
                return {"status": "error", "message": "pyautogui not available"}
            
            if save_path is None:
                save_path = f"screenshots/screenshot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
            
            Path(save_path).parent.mkdir(parents=True, exist_ok=True)
            screenshot = pyautogui.screenshot()
            screenshot.save(save_path)
            
            return {
                "status": "success",
                "message": f"Screenshot saved: {save_path}",
                "path": save_path
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}
    
    @staticmethod
    def move_mouse(x: int, y: int) -> Dict[str, Any]:
        """Move mouse to position"""
        try:
            if not PYAUTOGUI_AVAILABLE:
                return {"status": "error", "message": "pyautogui not available"}
            
            pyautogui.moveTo(x, y, duration=0.5)
            
            return {
                "status": "success",
                "message": f"Mouse moved to ({x}, {y})"
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}
    
    @staticmethod
    def click_mouse(x: int, y: int, button: str = "left") -> Dict[str, Any]:
        """Click mouse at position"""
        try:
            if not PYAUTOGUI_AVAILABLE:
                return {"status": "error", "message": "pyautogui not available"}
            
            pyautogui.click(x, y, button=button)
            
            return {
                "status": "success",
                "message": f"Mouse clicked at ({x}, {y}) with {button} button"
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}
    
    @staticmethod
    def type_text(text: str, interval: float = 0.1) -> Dict[str, Any]:
        """Type text using keyboard"""
        try:
            if not PYAUTOGUI_AVAILABLE:
                return {"status": "error", "message": "pyautogui not available"}
            
            pyautogui.typewrite(text, interval=interval)
            
            return {
                "status": "success",
                "message": f"Typed {len(text)} characters"
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}
    
    @staticmethod
    def press_key(key: str) -> Dict[str, Any]:
        """Press a keyboard key"""
        try:
            if not PYAUTOGUI_AVAILABLE:
                return {"status": "error", "message": "pyautogui not available"}
            
            pyautogui.press(key)
            
            return {
                "status": "success",
                "message": f"Key pressed: {key}"
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}
    
    @staticmethod
    def open_file(file_path: str) -> Dict[str, Any]:
        """Open a file with default application"""
        try:
            if os.name == 'nt':  # Windows
                os.startfile(file_path)
            else:  # Linux/Mac
                subprocess.run(['xdg-open', file_path])
            
            return {
                "status": "success",
                "message": f"Opened: {file_path}"
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}
    
    @staticmethod
    def get_open_windows() -> Dict[str, Any]:
        """Get list of open windows (Windows only)"""
        if not PYWIN32_AVAILABLE:
            return {"status": "error", "message": "pywin32 not available"}
        
        try:
            windows = []
            
            def callback(hwnd, ctx):
                if win32gui.IsWindowVisible(hwnd):
                    title = win32gui.GetWindowText(hwnd)
                    if title:
                        windows.append({
                            "hwnd": hwnd,
                            "title": title
                        })
                return True
            
            win32gui.EnumWindows(callback, None)
            
            return {
                "status": "success",
                "windows": windows,
                "count": len(windows)
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}
    
    @staticmethod
    def focus_window(window_title: str) -> Dict[str, Any]:
        """Focus a window by title (Windows only)"""
        if not PYWIN32_AVAILABLE:
            return {"status": "error", "message": "pywin32 not available"}
        
        try:
            hwnd = win32gui.FindWindow(None, window_title)
            if hwnd:
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                win32gui.SetForegroundWindow(hwnd)
                return {
                    "status": "success",
                    "message": f"Focused window: {window_title}"
                }
            else:
                return {
                    "status": "error",
                    "message": f"Window not found: {window_title}"
                }
        except Exception as e:
            return {"status": "error", "message": str(e)}


# Global instance
system_ops = SystemOperations()
