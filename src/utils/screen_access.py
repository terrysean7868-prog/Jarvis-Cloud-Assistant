# src/utils/screen_access.py
"""
Screen access and navigation utilities for Jarvis
Provides screen capture, OCR, and intelligent navigation
"""
import os
import platform
import base64
from typing import Dict, Tuple, Optional
from io import BytesIO

# Screen capture
try:
    from PIL import Image, ImageGrab
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

# OCR for reading screen text
try:
    import pytesseract
    TESSERACT_AVAILABLE = True
except ImportError:
    TESSERACT_AVAILABLE = False

# Screen navigation
try:
    import pyautogui
    PYAUTOGUI_AVAILABLE = True
    # Safety settings
    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = 0.1
except ImportError:
    PYAUTOGUI_AVAILABLE = False

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
        if PYAUTOGUI_AVAILABLE:
            return pyautogui.size()
        return (1920, 1080)  # Default
    
    def capture_screen(self, region: Optional[Tuple[int, int, int, int]] = None) -> Optional[Image.Image]:
        """
        Capture screen or region
        region: (x, y, width, height) or None for full screen
        """
        if not PIL_AVAILABLE:
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
        if not PYAUTOGUI_AVAILABLE:
            return False
        
        try:
            pyautogui.click(x, y, button=button)
            return True
        except Exception as e:
            print(f"Click error: {e}")
            return False
    
    def type_text(self, text: str, interval: float = 0.05) -> bool:
        """Type text at current cursor position"""
        if not PYAUTOGUI_AVAILABLE:
            return False
        
        try:
            pyautogui.write(text, interval=interval)
            return True
        except Exception as e:
            print(f"Type error: {e}")
            return False
    
    def press_key(self, key: str, presses: int = 1, interval: float = 0.1) -> bool:
        """Press keyboard key"""
        if not PYAUTOGUI_AVAILABLE:
            return False
        
        try:
            pyautogui.press(key, presses=presses, interval=interval)
            return True
        except Exception as e:
            print(f"Key press error: {e}")
            return False
    
    def scroll(self, x: int = 0, y: int = 0, clicks: int = 3) -> bool:
        """Scroll screen"""
        if not PYAUTOGUI_AVAILABLE:
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
        if not PYAUTOGUI_AVAILABLE:
            return (0, 0)
        
        try:
            return pyautogui.position()
        except:
            return (0, 0)
    
    def move_mouse(self, x: int, y: int, duration: float = 0.5) -> bool:
        """Move mouse to position"""
        if not PYAUTOGUI_AVAILABLE:
            return False
        
        try:
            pyautogui.moveTo(x, y, duration=duration)
            return True
        except Exception as e:
            print(f"Mouse move error: {e}")
            return False
    
    def take_screenshot_info(self) -> Dict:
        """Get screenshot with metadata"""
        screenshot = self.capture_screen()
        if not screenshot:
            return {"error": "Failed to capture screen"}
        
        return {
            "width": screenshot.width,
            "height": screenshot.height,
            "size": self.screen_size,
            "mouse_position": self.get_mouse_position(),
            "base64": self.capture_screen_base64()
        }


# Global instance
screen_access = ScreenAccess()

