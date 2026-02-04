"""
Advanced Decision-Making System for Jarvis PC Agent
Provides intelligent command parsing, context awareness, and PC configuration knowledge
for proper task execution and instruction following.
"""

import asyncio
import os
import re
import platform
import json
from typing import Dict, List, Any, Optional, Tuple
from datetime import datetime
from pathlib import Path
import psutil

from src.utils.db import db
from src.internet.web_scraper import WebScraper


class PCConfiguration:
    """Detects and caches PC hardware and OS configuration"""
    
    def __init__(self):
        self.config_cache = {}
        self.last_update = None
        self.cache_ttl_seconds = 3600  # 1 hour
        
    async def get_system_info(self) -> Dict[str, Any]:
        """Get comprehensive system information"""
        # Check cache
        if self._is_cache_valid():
            return self.config_cache
        
        try:
            self.config_cache = {
                "os": {
                    "name": platform.system(),
                    "version": platform.version(),
                    "release": platform.release(),
                    "machine": platform.machine(),
                },
                "cpu": {
                    "count": psutil.cpu_count(logical=False),
                    "count_logical": psutil.cpu_count(logical=True),
                    "freq_mhz": psutil.cpu_freq().current if psutil.cpu_freq() else 0,
                    "percent": psutil.cpu_percent(interval=0.5),
                },
                "memory": {
                    "total_gb": psutil.virtual_memory().total / (1024**3),
                    "available_gb": psutil.virtual_memory().available / (1024**3),
                    "percent": psutil.virtual_memory().percent,
                },
                "disk": {
                    "total_gb": psutil.disk_usage('/').total / (1024**3) if platform.system() == "Linux" else psutil.disk_usage('C:\\').total / (1024**3),
                    "free_gb": psutil.disk_usage('/').free / (1024**3) if platform.system() == "Linux" else psutil.disk_usage('C:\\').free / (1024**3),
                    "percent": psutil.disk_usage('/').percent if platform.system() == "Linux" else psutil.disk_usage('C:\\').percent,
                },
                "boot_time": datetime.fromtimestamp(psutil.boot_time()).isoformat(),
                "screen_info": self._get_screen_info(),
                "is_gaming_laptop": self._detect_gaming_laptop(),
                "is_windows_11": platform.system() == "Windows" and "11" in platform.version(),
                "is_hp_pavilion": self._detect_hp_pavilion(),
            }
            self.last_update = datetime.now()
            return self.config_cache
        except Exception as e:
            print(f"Error getting system info: {e}")
            return {"error": str(e)}
    
    def _is_cache_valid(self) -> bool:
        """Check if cached config is still valid"""
        if not self.last_update or not self.config_cache:
            return False
        age = (datetime.now() - self.last_update).total_seconds()
        return age < self.cache_ttl_seconds
    
    def _get_screen_info(self) -> Dict[str, Any]:
        """Get screen/monitor information"""
        try:
            import tkinter as tk
            root = tk.Tk()
            root.withdraw()
            return {
                "width": root.winfo_screenwidth(),
                "height": root.winfo_screenheight(),
                "dpi": root.winfo_fpixels("1i"),
            }
        except Exception:
            return {"error": "Could not detect screen info"}
    
    def _detect_gaming_laptop(self) -> bool:
        """Detect if this is likely a gaming laptop"""
        try:
            # Check for gaming indicators
            cpu_count = psutil.cpu_count(logical=True) or 0
            memory_gb = psutil.virtual_memory().total / (1024**3)
            
            # Gaming laptop typically has 8+ cores and 16+ GB RAM
            if cpu_count >= 8 and memory_gb >= 16:
                # Check for high-end GPU indicators
                try:
                    import subprocess
                    result = subprocess.run(
                        ["wmic", "path", "win32_videocontroller", "get", "name"],
                        capture_output=True, text=True, timeout=5
                    )
                    gpu_info = result.stdout.lower()
                    gaming_indicators = ["rtx", "gtx", "radeon", "rx", "arc"]
                    return any(ind in gpu_info for ind in gaming_indicators)
                except Exception:
                    return True  # High spec + unknown GPU = likely gaming
            return False
        except Exception:
            return False
    
    def _detect_hp_pavilion(self) -> bool:
        """Detect if this is an HP Pavilion laptop"""
        try:
            import subprocess
            result = subprocess.run(
                ["wmic", "computersystem", "get", "model"],
                capture_output=True, text=True, timeout=5
            )
            model = result.stdout.lower()
            return "pavilion" in model or "hp" in model
        except Exception:
            return False


class InstructionParser:
    """Advanced parser to distinguish between different command types"""
    
    # Command intent patterns
    OPEN_INTENT_PATTERNS = [
        r"\b(?:open|start|launch|run)(?:\s+(?:up|the))?\s+(.+?)(?:\s+(?:app|application|program))?\b",
        r"\b(?:can\s+you\s+)?(?:open|launch|start)\s+(.+?)(?:\s+for\s+me)?\b",
        r"(?:please\s+)?(?:open|launch|start)\s+(.+?)\b",
    ]
    
    SEARCH_INTENT_PATTERNS = [
        r"\b(?:search|look\s+up|find|query)(?:\s+(?:on|in|for))?\s+(?:(?:the\s+)?(?:web|internet|online))?\s*:?\s*(.+?)\b",
        r"\b(?:search|look\s+(?:it\s+)?up)(?:\s+(?:for|on))?\s+(.+?)\b",
        r"(?:find|search\s+for)\s+information\s+(?:on|about)\s+(.+?)\b",
        r"what\s+(?:is|are|do|does)\s+(.+?)(?:\s+mean)?\b",
    ]
    
    WEB_INTENT_PATTERNS = [
        r"\b(?:browse|go\s+to|visit|navigate\s+to)(?:\s+the)?\s+(.+?)\b",
        r"\b(?:open|visit|go\s+to)\s+(?:https?://|www\.)?(.+?)\b",
    ]
    
    ACTION_INTENT_PATTERNS = [
        r"\b(?:type|write|input)\s+(.+?)\b",
        r"\b(?:click|press|hit)\s+(?:on\s+)?(.+?)\b",
        r"\b(?:close|quit|exit|shutdown)\s+(.+?)\b",
    ]
    
    @staticmethod
    def parse_instruction(text: str) -> Dict[str, Any]:
        """
        Parse user instruction and determine intent with high precision
        
        Returns:
            {
                "intent": "open|search|web|action|unknown",
                "target": "what the user wants to do",
                "confidence": 0.0-1.0,
                "reasoning": "why this intent was chosen",
                "context_hints": ["hint1", "hint2"]
            }
        """
        text_lower = text.lower().strip()
        
        if not text:
            return {
                "intent": "unknown",
                "target": "",
                "confidence": 0.0,
                "reasoning": "Empty input",
                "context_hints": []
            }
        
        # Check for explicit web indicators
        explicit_web_markers = [
            "latest", "today", "current", "news", "documentation", "docs",
            "tutorial", "how to", "from internet", "look it up", "search online",
            "official", "api", "wikipedia", "github"
        ]
        has_web_marker = any(marker in text_lower for marker in explicit_web_markers)
        
        # Check for explicit open indicators
        explicit_open_verbs = ["open", "start", "launch", "run"]
        has_open_verb = any(f"\\b{verb}" in text_lower for verb in explicit_open_verbs)
        
        # DECISION TREE
        # Priority 1: If user explicitly says search/look up/find -> SEARCH
        if re.search(r"\b(?:search|look\s+up|find|query|investigate)\b", text_lower):
            for pattern in InstructionParser.SEARCH_INTENT_PATTERNS:
                match = re.search(pattern, text_lower)
                if match:
                    target = match.group(1).strip()
                    return {
                        "intent": "search",
                        "target": target,
                        "confidence": 0.95,
                        "reasoning": f"Explicit search verb found: {match.group(0)[:50]}",
                        "context_hints": ["search_verb_explicit", "needs_web"]
                    }
        
        # Priority 2: If user explicitly says open/launch/start + LOCAL APP -> OPEN
        if has_open_verb:
            # Extract what they want to open
            for pattern in InstructionParser.OPEN_INTENT_PATTERNS:
                match = re.search(pattern, text_lower)
                if match:
                    target = match.group(1).strip()
                    
                    # Check if it's a known local app
                    local_apps = {
                        "notepad", "calculator", "paint", "wordpad", "explorer",
                        "cmd", "powershell", "vscode", "chrome", "firefox", "edge",
                        "word", "excel", "outlook", "teams", "slack", "discord"
                    }
                    
                    target_lower = target.lower()
                    is_local_app = any(app in target_lower for app in local_apps)
                    is_url_like = re.search(r"https?://|www\.|\.com|\.org|\.net", target_lower)
                    
                    if is_local_app or not is_url_like:
                        # This is an OPEN command for a local app
                        return {
                            "intent": "open",
                            "target": target,
                            "confidence": 0.95 if is_local_app else 0.75,
                            "reasoning": f"Open verb with local app/unclear target",
                            "context_hints": ["open_verb_explicit", "local_context"]
                        }
                    else:
                        # URL-like target -> should use open_url or web_search
                        return {
                            "intent": "web",
                            "target": target,
                            "confidence": 0.85,
                            "reasoning": f"Open verb with URL-like target",
                            "context_hints": ["url_pattern_detected"]
                        }
        
        # Priority 3: If user explicitly says browse/visit/go to + URL -> WEB
        if re.search(r"\b(?:browse|visit|go\s+to|navigate)\b", text_lower):
            for pattern in InstructionParser.WEB_INTENT_PATTERNS:
                match = re.search(pattern, text_lower)
                if match:
                    target = match.group(1).strip()
                    return {
                        "intent": "web",
                        "target": target,
                        "confidence": 0.90,
                        "reasoning": "Explicit web browsing verb",
                        "context_hints": ["web_verb_explicit"]
                    }
        
        # Priority 4: If contains web markers but no explicit verb -> likely SEARCH
        if has_web_marker:
            # Extract potential search query
            query = text
            for marker in explicit_web_markers:
                query = query.replace(marker, "").strip()
            
            return {
                "intent": "search",
                "target": query or text,
                "confidence": 0.80,
                "reasoning": f"Contains web-related markers: {[m for m in explicit_web_markers if m in text_lower]}",
                "context_hints": ["web_context_markers"]
            }
        
        # Priority 5: Type/write/input intentions
        if re.search(r"\b(?:type|write|input|compose)\s+(.+?)\b", text_lower):
            return {
                "intent": "action",
                "target": "type_text",
                "confidence": 0.85,
                "reasoning": "Explicit type/write verb",
                "context_hints": ["action_type"]
            }
        
        # Default: UNKNOWN
        return {
            "intent": "unknown",
            "target": text,
            "confidence": 0.0,
            "reasoning": "Could not definitively determine intent",
            "context_hints": ["ambiguous"]
        }


class ContextAwareDecisionMaker:
    """Makes intelligent decisions about what action to take"""
    
    def __init__(self):
        self.pc_config = PCConfiguration()
        self.parser = InstructionParser()
        self.scraper = None
        self.knowledge_cache = {}
        
    async def initialize(self):
        """Initialize the decision maker with system info and web scraper"""
        try:
            self.scraper = WebScraper()
            await self.scraper.initialize()
            
            # Get PC configuration
            pc_info = await self.pc_config.get_system_info()
            self.knowledge_cache["system"] = pc_info
            
            # Cache Windows 11 capabilities if applicable
            if pc_info.get("is_windows_11"):
                await self._cache_windows_11_knowledge()
            
            # Cache HP Pavilion specific info if applicable
            if pc_info.get("is_hp_pavilion"):
                await self._cache_hp_pavilion_knowledge()
            
            print("[DecisionMaker] Initialized with system knowledge")
            return self
        except Exception as e:
            print(f"[DecisionMaker] Init error: {e}")
            return self
    
    async def _cache_windows_11_knowledge(self):
        """Cache Windows 11 capabilities and features"""
        try:
            # In production, you could fetch this from your web scraper
            self.knowledge_cache["windows_11"] = {
                "features": [
                    "Windows Copilot", "Snap Layouts", "Virtual Desktops",
                    "Task View", "Widgets", "Touch Keyboard", "Game Pass",
                    "Phone Link", "Recovery Drive"
                ],
                "settings_shortcuts": {
                    "display": "ms-settings:display",
                    "sound": "ms-settings:sound",
                    "network": "ms-settings:network",
                    "bluetooth": "ms-settings:bluetooth",
                    "keyboard": "ms-settings:keyboard",
                    "mouse": "ms-settings:mousetouchpad",
                    "apps": "ms-settings:appsfeatures",
                    "privacy": "ms-settings:privacy",
                }
            }
        except Exception as e:
            print(f"[DecisionMaker] Windows 11 knowledge caching failed: {e}")
    
    async def _cache_hp_pavilion_knowledge(self):
        """Cache HP Pavilion specific capabilities and known issues"""
        try:
            self.knowledge_cache["hp_pavilion"] = {
                "specs": {
                    "typical_cpu": "Intel i7/i9 or AMD Ryzen 7/9",
                    "typical_ram": "16-32 GB DDR5",
                    "typical_gpu": "NVIDIA RTX 4060/4070 or AMD equivalent",
                    "typical_storage": "512 GB - 2 TB SSD",
                },
                "features": [
                    "Gaming-optimized cooling", "RGB keyboard",
                    "Thunderbolt 3/4", "Multiple USB ports",
                    "HDMI 2.1", "High-refresh display (144Hz+)"
                ],
                "gaming_features": [
                    "NVIDIA GeForce Experience (if RTX GPU)",
                    "AMD Radeon Software (if Radeon GPU)",
                    "HP Omen Command Center (if available)"
                ]
            }
        except Exception as e:
            print(f"[DecisionMaker] HP Pavilion knowledge caching failed: {e}")
    
    async def decide_action(self, user_instruction: str, context: str = "") -> Dict[str, Any]:
        """
        Make intelligent decision about what action to execute
        
        Returns:
            {
                "instruction": original text,
                "parsed_intent": {...from parser...},
                "recommended_action_type": "open_app|search|fetch_url|open_url|etc",
                "recommended_action": {...},
                "confidence": 0.0-1.0,
                "reasoning": "why this action was chosen",
                "alternatives": [...],
                "pc_context": {...system info...}
            }
        """
        try:
            # Parse the instruction
            parsed = self.parser.parse_instruction(user_instruction)
            
            # Get current system state
            pc_info = self.knowledge_cache.get("system") or await self.pc_config.get_system_info()
            
            # Make the decision
            decision = await self._make_decision(parsed, user_instruction, context, pc_info)
            
            return decision
        except Exception as e:
            print(f"[DecisionMaker] Error: {e}")
            return {
                "instruction": user_instruction,
                "error": str(e),
                "parsed_intent": {"intent": "error"},
                "recommended_action_type": "unknown"
            }
    
    async def _make_decision(
        self,
        parsed: Dict[str, Any],
        instruction: str,
        context: str,
        pc_info: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Execute the decision logic"""
        
        intent = parsed.get("intent", "unknown")
        target = parsed.get("target", "")
        confidence = parsed.get("confidence", 0.0)
        
        decision = {
            "instruction": instruction,
            "parsed_intent": parsed,
            "pc_context": pc_info,
            "timestamp": datetime.now().isoformat(),
        }
        
        if intent == "open":
            # User wants to OPEN a local application
            decision["recommended_action_type"] = "open_app"
            decision["recommended_action"] = {
                "type": "open_app",
                "app_name": target,
                "args": []
            }
            decision["reasoning"] = f"User explicitly requested to open: {target}"
            decision["confidence"] = confidence
            
        elif intent == "search":
            # User wants to SEARCH the web
            decision["recommended_action_type"] = "web_search"
            decision["recommended_action"] = {
                "type": "web_search",
                "query": target,
                "num_results": 5
            }
            decision["reasoning"] = f"User explicitly requested web search for: {target}"
            decision["confidence"] = confidence
            
        elif intent == "web":
            # User wants to visit/browse a website
            # First try to detect if it's a URL or site name
            target_lower = target.lower()
            
            if re.search(r"https?://", target):
                # Direct URL
                decision["recommended_action_type"] = "open_url"
                decision["recommended_action"] = {
                    "type": "open_url",
                    "url": target
                }
            else:
                # Site name - could be URL or search
                if any(tld in target_lower for tld in [".com", ".org", ".net", ".io", ".dev"]):
                    decision["recommended_action_type"] = "open_url"
                    url = target if target.startswith("http") else f"https://{target}"
                    decision["recommended_action"] = {
                        "type": "open_url",
                        "url": url
                    }
                else:
                    # Ambiguous - could be app name or site
                    # Default to web search
                    decision["recommended_action_type"] = "web_search"
                    decision["recommended_action"] = {
                        "type": "web_search",
                        "query": target,
                        "num_results": 5
                    }
            
            decision["reasoning"] = f"User wants to browse/visit: {target}"
            decision["confidence"] = confidence
            
        elif intent == "action":
            # Some other action like type/click/etc
            decision["recommended_action_type"] = "execute_action"
            decision["recommended_action"] = {
                "type": "action_required",
                "description": instruction
            }
            decision["reasoning"] = "User wants to perform an action on current screen"
            decision["confidence"] = confidence
            
        else:
            # Unknown - make best guess
            decision["recommended_action_type"] = "unknown"
            decision["recommended_action"] = None
            decision["reasoning"] = parsed.get("reasoning", "Could not determine intent")
            decision["confidence"] = 0.0
        
        decision["alternatives"] = self._get_alternatives(intent, target)
        
        return decision
    
    def _get_alternatives(self, intent: str, target: str) -> List[Dict[str, Any]]:
        """Get alternative action interpretations"""
        alternatives = []
        
        if intent == "open":
            alternatives.append({
                "type": "web_search",
                "query": target,
                "reason": "Could search the web instead if app is not installed"
            })
        elif intent == "search":
            alternatives.append({
                "type": "fetch_url",
                "reason": "Could fetch a specific URL if target is a documentation link"
            })
        elif intent == "web":
            alternatives.append({
                "type": "web_search",
                "query": target,
                "reason": "Could search instead if the specific site is not accessible"
            })
        
        return alternatives
    
    async def close(self):
        """Cleanup resources"""
        try:
            if self.scraper:
                await self.scraper.close()
        except Exception:
            pass


# Global singleton
decision_maker = None

async def initialize_decision_maker() -> ContextAwareDecisionMaker:
    """Initialize the global decision maker"""
    global decision_maker
    decision_maker = ContextAwareDecisionMaker()
    await decision_maker.initialize()
    return decision_maker

async def get_decision_maker() -> ContextAwareDecisionMaker:
    """Get the decision maker instance (lazy init if needed)"""
    global decision_maker
    if decision_maker is None:
        decision_maker = ContextAwareDecisionMaker()
        await decision_maker.initialize()
    return decision_maker
