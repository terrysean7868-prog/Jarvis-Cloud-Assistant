# src/utils/screen_access.py
"""
Screen access and navigation utilities for Jarvis
Provides screen capture, OCR, and intelligent navigation
"""
import os
import platform
import base64
from typing import Dict, Tuple, Optional, TYPE_CHECKING
from io import BytesIO

# Screen capture
PIL_AVAILABLE = False
Image = None
ImageGrab = None

try:
    from PIL import Image, ImageGrab
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False
    Image = None
    ImageGrab = None

# For type hints only
if TYPE_CHECKING:
    try:
        from PIL import Image as PILImage
    except ImportError:
        PILImage = None

# OCR for reading screen text
try:
    import pytesseract
    TESSERACT_AVAILABLE = True
except Exception:
    TESSERACT_AVAILABLE = False

# Screen navigation
PYAUTOGUI_AVAILABLE = False
pyautogui = None

# Detect headless environment (Render, Docker, etc.)
# pyautogui requires DISPLAY environment variable on Linux
# On headless servers, skip import entirely to avoid KeyError
_is_headless = False
if platform.system() != "Windows":
    # Check if DISPLAY is set (required for X11)
    if "DISPLAY" not in os.environ:
        _is_headless = True
    # Also check for common server environment indicators
    # Render sets PORT, RENDER_SERVICE_ID, or path contains /opt/render
    if (os.getenv("RENDER") or os.getenv("DYNO") or os.getenv("DOCKER") or 
        os.getenv("PORT") or "/opt/render" in os.getcwd() or 
        "/opt/render" in str(__file__)):
        _is_headless = True

if not _is_headless:
    try:
        # Set DISPLAY if not set (prevents KeyError in mouseinfo module)
        if platform.system() != "Windows" and "DISPLAY" not in os.environ:
            os.environ["DISPLAY"] = ":0"
        
        import pyautogui
        PYAUTOGUI_AVAILABLE = True
        # Safety settings
        pyautogui.FAILSAFE = True
        pyautogui.PAUSE = 0.1
    except (ImportError, KeyError, Exception) as e:
        # If import fails, mark as unavailable
        PYAUTOGUI_AVAILABLE = False
        pyautogui = None
        # Only log in non-headless environments
        if platform.system() == "Windows" or os.getenv("DISPLAY"):
            print(f"pyautogui not available: {e}")
else:
    # Headless environment - skip import entirely
    PYAUTOGUI_AVAILABLE = False
    pyautogui = None

# Computer vision for element detection
try:
    import cv2
    import numpy as np
    CV2_AVAILABLE = True
except ImportError:
    CV2_AVAILABLE = False


class ScreenAccess:
    """Screen access and navigation manager"""
    
    def __init__(self):
        self.screen_size = self.get_screen_size()
        self.last_screenshot = None
        
    def get_screen_size(self) -> Tuple[int, int]:
        """Get screen dimensions"""
        if PYAUTOGUI_AVAILABLE and pyautogui:
            try:
                return pyautogui.size()
            except:
                return (1920, 1080)  # Default
        return (1920, 1080)  # Default
    
    def capture_screen(self, region: Optional[Tuple[int, int, int, int]] = None):
        """
        Capture screen or region
        region: (x, y, width, height) or None for full screen
        Returns: PIL Image object or None
        """
        if not PIL_AVAILABLE or not ImageGrab:
            return None
        
        # Check if we're in a headless environment
        if not os.getenv("DISPLAY") and platform.system() != "Windows":
            print("Screen capture not available in headless environment")
            return None
        
        try:
            if region:
                x, y, width, height = region
                screenshot = ImageGrab.grab(bbox=(x, y, x + width, y + height))
            else:
                screenshot = ImageGrab.grab()
            
            self.last_screenshot = screenshot
            return screenshot
        except Exception as e:
            print(f"Screen capture error: {e}")
            return None
    
    def capture_screen_base64(self, region: Optional[Tuple[int, int, int, int]] = None) -> Optional[str]:
        """Capture screen and return as base64 string"""
        screenshot = self.capture_screen(region)
        if not screenshot:
            return None
        
        try:
            buffer = BytesIO()
            screenshot.save(buffer, format='PNG')
            img_str = base64.b64encode(buffer.getvalue()).decode()
            return img_str
        except Exception as e:
            print(f"Base64 encoding error: {e}")
            return None
    
    def read_screen_text(self, region: Optional[Tuple[int, int, int, int]] = None) -> str:
        """Extract text from screen using OCR"""
        if not TESSERACT_AVAILABLE:
            return ""
        
        screenshot = self.capture_screen(region)
        if not screenshot:
            return ""
        
        try:
            text = pytesseract.image_to_string(screenshot)
            return text.strip()
        except Exception as e:
            print(f"OCR error: {e}")
            return ""
    
    def find_text_on_screen(self, search_text: str) -> Optional[Tuple[int, int]]:
        """Find text on screen and return coordinates"""
        if not TESSERACT_AVAILABLE or not PYAUTOGUI_AVAILABLE:
            return None
        
        try:
            # Use pyautogui's locateOnScreen with text recognition
            # This is a simplified version - in production, use more sophisticated methods
            screenshot = self.capture_screen()
            if not screenshot:
                return None
            
            # For now, return center of screen as placeholder
            # In production, implement proper text detection
            return (self.screen_size[0] // 2, self.screen_size[1] // 2)
        except Exception as e:
            print(f"Text finding error: {e}")
            return None
    
    def click_at_position(self, x: int, y: int, button: str = "left") -> bool:
        """Click at specific screen position"""
        if not PYAUTOGUI_AVAILABLE or not pyautogui:
            return False
        
        try:
            pyautogui.click(x, y, button=button)
            return True
        except Exception as e:
            print(f"Click error: {e}")
            return False
    
    def type_text(self, text: str, interval: float = 0.05) -> bool:
        """Type text at current cursor position"""
        if not PYAUTOGUI_AVAILABLE or not pyautogui:
            return False
        
        try:
            pyautogui.write(text, interval=interval)
            return True
        except Exception as e:
            print(f"Type error: {e}")
            return False
    
    def press_key(self, key: str, presses: int = 1, interval: float = 0.1) -> bool:
        """Press keyboard key"""
        if not PYAUTOGUI_AVAILABLE or not pyautogui:
            return False
        
        try:
            pyautogui.press(key, presses=presses, interval=interval)
            return True
        except Exception as e:
            print(f"Key press error: {e}")
            return False
    
    def scroll(self, x: int = 0, y: int = 0, clicks: int = 3) -> bool:
        """Scroll screen"""
        if not PYAUTOGUI_AVAILABLE or not pyautogui:
            return False
        
        try:
            if y != 0:
                pyautogui.scroll(y * clicks)
            if x != 0:
                pyautogui.hscroll(x * clicks)
            return True
        except Exception as e:
            print(f"Scroll error: {e}")
            return False
    
    def get_mouse_position(self) -> Tuple[int, int]:
        """Get current mouse position"""
        if not PYAUTOGUI_AVAILABLE or not pyautogui:
            return (0, 0)
        
        try:
            return pyautogui.position()
        except:
            return (0, 0)
    
    def move_mouse(self, x: int, y: int, duration: float = 0.5) -> bool:
        """Move mouse to position"""
        if not PYAUTOGUI_AVAILABLE or not pyautogui:
            return False
        
        try:
            pyautogui.moveTo(x, y, duration=duration)
            return True
        except Exception as e:
            print(f"Mouse move error: {e}")
            return False
    
    def take_screenshot_info(
        self,
        region: Optional[Tuple[int, int, int, int]] = None,
        include_base64: bool = False,
    ) -> Dict:
        """Get screenshot metadata (optionally include base64 image).

        region: (x, y, width, height) or None for full screen
        """
        screenshot = self.capture_screen(region)
        if not screenshot:
            return {"error": "Failed to capture screen"}
        
        return {
            "width": screenshot.width,
            "height": screenshot.height,
            "size": self.screen_size,
            "mouse_position": self.get_mouse_position(),
            **({"base64": self.capture_screen_base64()} if include_base64 else {})
        }


# Global instance
screen_access = ScreenAccess()

