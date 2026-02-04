"""
PC Knowledge Engine - Fetches and caches information about PC capabilities
Provides context for better decision-making about app availability, features, and settings
"""

import asyncio
import json
from typing import Dict, List, Any, Optional
from datetime import datetime
from src.internet.web_scraper import WebScraper


class PCKnowledgeEngine:
    """Fetches and caches knowledge about PC capabilities, Windows 11, HP Pavilion, etc."""
    
    def __init__(self):
        self.scraper = None
        self.cache = {}
        self.cache_ttl = 86400  # 24 hours
        self.last_update = {}
    
    async def initialize(self):
        """Initialize web scraper"""
        try:
            self.scraper = WebScraper()
            await self.scraper.initialize()
        except Exception as e:
            print(f"[PCKnowledge] Scraper init error: {e}")
    
    def _is_cache_valid(self, key: str) -> bool:
        """Check if cache entry is still valid"""
        if key not in self.last_update:
            return False
        age = (datetime.now() - self.last_update[key]).total_seconds()
        return age < self.cache_ttl
    
    async def get_windows_11_capabilities(self) -> Dict[str, Any]:
        """Get comprehensive Windows 11 capabilities and features"""
        if self._is_cache_valid("windows_11"):
            return self.cache.get("windows_11", {})
        
        try:
            capabilities = {
                "os_name": "Windows 11",
                "version": "22H2 and later",
                "features": [
                    "Snap Layouts - organize windows side-by-side",
                    "Virtual Desktops - multiple workspaces",
                    "Task View - switch between apps",
                    "Windows Copilot - AI assistant",
                    "Widgets Dashboard - information panel",
                    "Touch Gestures - multi-touch support",
                    "DirectStorage - faster game loading",
                    "Auto HDR - enhanced graphics",
                    "Phone Link - mirror Android on PC",
                    "Game Pass - game library subscription",
                    "Xbox App integration",
                    "Microsoft Store improvements",
                    "Windows Terminal - advanced CLI",
                ],
                "settings_categories": {
                    "System": [
                        "Display", "Sound", "Notifications", "Power",
                        "Storage", "About", "Advanced system settings"
                    ],
                    "Bluetooth & Devices": [
                        "Bluetooth", "Printers & Scanners", "USB",
                        "Mouse & Touchpad", "Keyboard"
                    ],
                    "Network & Internet": [
                        "WiFi", "Ethernet", "VPN", "Dial-up",
                        "Proxy", "Data usage"
                    ],
                    "Apps": [
                        "Installed apps", "Default apps",
                        "Startup programs", "Advanced options"
                    ],
                    "Gaming": [
                        "Game Pass", "Xbox", "Game mode",
                        "DirectX 12", "Graphics settings"
                    ]
                },
                "keyboard_shortcuts": {
                    "Win+V": "Clipboard history",
                    "Win+Shift+S": "Screenshot tool",
                    "Win+D": "Show/hide desktop",
                    "Win+E": "File Explorer",
                    "Win+I": "Settings",
                    "Win+X": "Quick menu",
                    "Win+Tab": "Task View",
                    "Win+Number": "Open taskbar app",
                    "Ctrl+Alt+Delete": "Security menu",
                    "Win+Pause/Break": "System properties"
                }
            }
            
            # Try to fetch additional info from web if scraper available
            if self.scraper:
                try:
                    web_info = await self.scraper.google_search(
                        "Windows 11 features and settings 2025",
                        num_results=3
                    )
                    if web_info:
                        capabilities["web_sources"] = web_info
                except Exception:
                    pass
            
            self.cache["windows_11"] = capabilities
            self.last_update["windows_11"] = datetime.now()
            return capabilities
        except Exception as e:
            print(f"[PCKnowledge] Error getting Windows 11 capabilities: {e}")
            return {}
    
    async def get_hp_pavilion_capabilities(self) -> Dict[str, Any]:
        """Get HP Pavilion gaming laptop specifications and features"""
        if self._is_cache_valid("hp_pavilion"):
            return self.cache.get("hp_pavilion", {})
        
        try:
            capabilities = {
                "brand": "HP Pavilion Gaming",
                "type": "Gaming Laptop",
                "typical_specs": {
                    "processor": [
                        "Intel Core i7-13th Gen or newer",
                        "Intel Core i9-13th Gen or newer",
                        "AMD Ryzen 7 7000 series",
                        "AMD Ryzen 9 7000 series"
                    ],
                    "ram": "16GB to 32GB DDR5 RAM",
                    "storage": "512GB to 1TB NVMe SSD",
                    "display": [
                        "15.6\" or 17\" screen",
                        "144Hz to 240Hz refresh rate",
                        "IPS panel technology",
                        "FHD or QHD resolution"
                    ],
                    "graphics": [
                        "NVIDIA GeForce RTX 4060 or better",
                        "NVIDIA GeForce RTX 4070 or RTX 4080",
                        "AMD Radeon RX alternative"
                    ],
                    "ports": [
                        "2-3 USB 3.2 Gen 1 Type-A",
                        "2 USB-C (Thunderbolt 3/4 capable)",
                        "HDMI 2.1 (high bandwidth)",
                        "3.5mm audio jack",
                        "SD card reader"
                    ],
                    "features": [
                        "Dual-fan cooling system",
                        "RGB backlighting (customizable)",
                        "HP Omen Command Center",
                        "Dynamic Power Mode",
                        "Vapor Chamber cooling",
                        "Glass touchpad"
                    ]
                },
                "software_included": [
                    "Windows 11 Pro",
                    "HP Support Assistant",
                    "HP Omen Game Hub",
                    "NVIDIA GeForce Experience (if RTX)",
                    "AMD Adrenaline (if Radeon)"
                ],
                "gaming_features": [
                    "High refresh rate display for smooth gameplay",
                    "RGB keyboard with multiple color zones",
                    "Optimized thermal design for extended gaming",
                    "High-speed NVMe storage for fast load times",
                    "Thunderbolt 4 for external GPU expansion",
                    "Audio tuned for gaming with HP Enhancedaudio"
                ],
                "known_capabilities": [
                    "4K video playback",
                    "Video editing (Adobe/DaVinci)",
                    "3D rendering and animation",
                    "Competitive gaming (1080p+ high/ultra settings)",
                    "Streaming with minimal performance hit",
                    "VR-ready with proper setup"
                ],
                "upgrade_paths": [
                    "RAM upgrade (most models have 2x SODIMM slots)",
                    "SSD replacement (M.2 slot typically accessible)",
                    "Thermal paste replacement (advanced users)",
                    "Cooling pad attachment recommended"
                ]
            }
            
            # Try to fetch HP Pavilion specific info from web
            if self.scraper:
                try:
                    web_info = await self.scraper.google_search(
                        "HP Pavilion Gaming laptop specifications features 2024 2025",
                        num_results=3
                    )
                    if web_info:
                        capabilities["web_sources"] = web_info
                except Exception:
                    pass
            
            self.cache["hp_pavilion"] = capabilities
            self.last_update["hp_pavilion"] = datetime.now()
            return capabilities
        except Exception as e:
            print(f"[PCKnowledge] Error getting HP Pavilion capabilities: {e}")
            return {}
    
    async def get_application_knowledge(self, app_name: str) -> Dict[str, Any]:
        """Get knowledge about a specific application"""
        cache_key = f"app_{app_name.lower()}"
        
        if self._is_cache_valid(cache_key):
            return self.cache.get(cache_key, {})
        
        try:
            # Common applications
            common_apps = {
                "notepad": {
                    "name": "Notepad",
                    "category": "Text Editor",
                    "typical_use": "Writing plain text",
                    "hotkeys": {
                        "Ctrl+N": "New",
                        "Ctrl+O": "Open",
                        "Ctrl+S": "Save",
                        "Ctrl+P": "Print",
                        "Ctrl+F": "Find"
                    }
                },
                "calculator": {
                    "name": "Calculator",
                    "category": "Utility",
                    "typical_use": "Mathematical calculations",
                    "modes": ["Standard", "Scientific", "Programmer", "Date Calculation"]
                },
                "explorer": {
                    "name": "File Explorer",
                    "category": "File Management",
                    "typical_use": "Navigate and manage files",
                    "hotkeys": {
                        "Win+E": "Open",
                        "Ctrl+D": "Delete",
                        "Ctrl+C": "Copy",
                        "Ctrl+X": "Cut",
                        "Ctrl+V": "Paste"
                    }
                },
                "chrome": {
                    "name": "Google Chrome",
                    "category": "Web Browser",
                    "typical_use": "Web browsing",
                    "features": [
                        "Tab management",
                        "Extensions",
                        "DevTools",
                        "Sync across devices"
                    ]
                },
                "firefox": {
                    "name": "Mozilla Firefox",
                    "category": "Web Browser",
                    "typical_use": "Web browsing",
                    "features": [
                        "Privacy mode",
                        "Add-ons",
                        "Screenshot tool",
                        "Container tabs"
                    ]
                },
                "vscode": {
                    "name": "Visual Studio Code",
                    "category": "Code Editor",
                    "typical_use": "Programming and code editing",
                    "features": [
                        "Syntax highlighting",
                        "Debugging",
                        "Git integration",
                        "Extensions marketplace"
                    ]
                }
            }
            
            app_lower = app_name.lower()
            knowledge = common_apps.get(app_lower)
            
            if not knowledge and self.scraper:
                # Try to fetch from web
                try:
                    web_results = await self.scraper.google_search(
                        f"{app_name} application features documentation",
                        num_results=2
                    )
                    if web_results:
                        knowledge = {
                            "name": app_name,
                            "web_results": web_results
                        }
                except Exception:
                    knowledge = None
            
            if knowledge:
                self.cache[cache_key] = knowledge
                self.last_update[cache_key] = datetime.now()
                return knowledge
            else:
                return {"name": app_name, "status": "unknown"}
        except Exception as e:
            print(f"[PCKnowledge] Error getting app knowledge for {app_name}: {e}")
            return {}
    
    async def get_feature_knowledge(self, feature_name: str) -> Dict[str, Any]:
        """Get knowledge about a specific Windows 11 feature or setting"""
        cache_key = f"feature_{feature_name.lower()}"
        
        if self._is_cache_valid(cache_key):
            return self.cache.get(cache_key, {})
        
        try:
            features = {
                "snap_layouts": {
                    "name": "Snap Layouts",
                    "description": "Organize windows side-by-side for multitasking",
                    "how_to_use": [
                        "Hover over window maximize button",
                        "Select a layout configuration",
                        "Other open windows appear for easy selection"
                    ],
                    "benefits": ["Productivity boost", "Better organization", "Quick window management"]
                },
                "virtual_desktops": {
                    "name": "Virtual Desktops",
                    "description": "Create multiple workspaces for different tasks",
                    "hotkeys": {
                        "Win+Tab": "Open Task View",
                        "Win+Ctrl+D": "New desktop",
                        "Win+Ctrl+Left/Right": "Switch desktop"
                    },
                    "use_cases": ["Keep work organized", "Separate gaming/work", "Multi-project management"]
                },
                "copilot": {
                    "name": "Windows Copilot",
                    "description": "AI assistant integrated into Windows",
                    "how_to_access": "Win+C or search 'Copilot'",
                    "capabilities": [
                        "Answer questions",
                        "Help with tasks",
                        "System information",
                        "Settings navigation"
                    ]
                }
            }
            
            feature_lower = feature_name.lower()
            knowledge = features.get(feature_lower.replace(" ", "_"))
            
            if not knowledge and self.scraper:
                # Try to fetch from web
                try:
                    web_results = await self.scraper.google_search(
                        f"Windows 11 {feature_name} how to use guide",
                        num_results=2
                    )
                    if web_results:
                        knowledge = {
                            "name": feature_name,
                            "web_results": web_results
                        }
                except Exception:
                    knowledge = None
            
            if knowledge:
                self.cache[cache_key] = knowledge
                self.last_update[cache_key] = datetime.now()
                return knowledge
            else:
                return {"name": feature_name, "status": "unknown"}
        except Exception as e:
            print(f"[PCKnowledge] Error getting feature knowledge for {feature_name}: {e}")
            return {}
    
    async def close(self):
        """Cleanup resources"""
        try:
            if self.scraper:
                await self.scraper.close()
        except Exception:
            pass


# Global singleton
pc_knowledge = None

async def initialize_pc_knowledge() -> PCKnowledgeEngine:
    """Initialize the global PC knowledge engine"""
    global pc_knowledge
    pc_knowledge = PCKnowledgeEngine()
    await pc_knowledge.initialize()
    return pc_knowledge

async def get_pc_knowledge() -> PCKnowledgeEngine:
    """Get the PC knowledge engine instance (lazy init if needed)"""
    global pc_knowledge
    if pc_knowledge is None:
        pc_knowledge = PCKnowledgeEngine()
        await pc_knowledge.initialize()
    return pc_knowledge
