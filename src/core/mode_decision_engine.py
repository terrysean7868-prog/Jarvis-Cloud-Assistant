"""
Advanced Mode-Aware Decision-Making System
Handles multiple operational modes: LEARN, UPDATE, EXECUTE, ANALYZE, DEVELOP, CREATIVE, INTERACT
Each mode has different decision patterns and priorities
"""

import asyncio
from enum import Enum
from typing import Dict, List, Any, Optional, Tuple
from datetime import datetime
from pathlib import Path
import re

from src.utils.db import db


class OperationalMode(Enum):
    """Different operational modes for the assistant"""
    LEARN = "learn"           # Learning new information from web/user
    UPDATE = "update"         # Updating/modifying existing things
    EXECUTE = "execute"       # Executing tasks and commands
    ANALYZE = "analyze"       # Analyzing data and patterns
    DEVELOP = "develop"       # Self-improvement and development
    CREATIVE = "creative"     # Creative problem-solving
    INTERACT = "interact"     # Normal human interaction


class ModeDecisionEngine:
    """Makes decisions based on current operational mode"""
    
    def __init__(self):
        self.current_mode = OperationalMode.INTERACT
        self.mode_priorities = self._initialize_priorities()
        self.mode_handlers = self._initialize_handlers()
        self.mode_history = []
        
    def _initialize_priorities(self) -> Dict[OperationalMode, Dict[str, int]]:
        """Define decision priorities for each mode"""
        return {
            OperationalMode.LEARN: {
                "web_search": 10,        # Highest priority - fetch web info
                "fetch_url": 9,          # Fetch specific documentation
                "open_app": 5,           # Open relevant apps
                "execute_command": 3,    # Less priority for commands
                "type_text": 2,          # Low priority for typing
            },
            
            OperationalMode.UPDATE: {
                "read": 9,               # Read current state
                "edit": 10,              # Edit files/content
                "write": 10,             # Write changes
                "execute_command": 8,    # Run update commands
                "open_app": 6,           # Open editor apps
                "fetch_url": 4,          # Get update info
                "web_search": 3,         # Lower priority
            },
            
            OperationalMode.EXECUTE: {
                "open_app": 10,          # Open apps to execute
                "execute_command": 10,   # Run commands
                "type_text": 9,          # Type to interact
                "press_key": 9,          # Press keys for navigation
                "hotkey": 9,             # Use hotkeys
                "web_search": 2,         # Low priority
                "fetch_url": 1,          # Minimal
            },
            
            OperationalMode.ANALYZE: {
                "web_search": 10,        # Get data to analyze
                "fetch_url": 9,          # Fetch source data
                "read": 8,               # Read files/data
                "open_app": 6,           # Open analysis tools
                "execute_command": 5,    # Run analysis scripts
                "type_text": 2,          # Type queries
            },
            
            OperationalMode.DEVELOP: {
                "read": 10,              # Read code
                "edit": 10,              # Edit code
                "execute_command": 9,    # Run dev commands
                "open_app": 8,           # Open IDEs
                "fetch_url": 7,          # Fetch documentation
                "web_search": 6,         # Search for solutions
                "write": 9,              # Write new code
            },
            
            OperationalMode.CREATIVE: {
                "web_search": 8,         # Research ideas
                "fetch_url": 7,          # Get inspiration
                "open_app": 9,           # Open creative tools
                "type_text": 10,         # Write/create content
                "execute_command": 4,    # Run generation commands
                "press_key": 6,          # Navigate tools
            },
            
            OperationalMode.INTERACT: {
                "open_url": 10,          # Open websites user asks
                "open_app": 10,          # Open apps user asks
                "web_search": 9,         # Search for info
                "type_text": 8,          # Type content
                "fetch_url": 7,          # Get web info
                "execute_command": 5,    # Run safe commands
            }
        }
    
    def _initialize_handlers(self) -> Dict[OperationalMode, callable]:
        """Initialize mode-specific decision handlers"""
        return {
            OperationalMode.LEARN: self._handle_learn_mode,
            OperationalMode.UPDATE: self._handle_update_mode,
            OperationalMode.EXECUTE: self._handle_execute_mode,
            OperationalMode.ANALYZE: self._handle_analyze_mode,
            OperationalMode.DEVELOP: self._handle_develop_mode,
            OperationalMode.CREATIVE: self._handle_creative_mode,
            OperationalMode.INTERACT: self._handle_interact_mode,
        }
    
    def set_mode(self, mode: OperationalMode) -> Dict[str, Any]:
        """Switch to a different operational mode"""
        self.current_mode = mode
        self.mode_history.append({
            "mode": mode.value,
            "timestamp": datetime.now().isoformat()
        })
        
        return {
            "status": "mode_changed",
            "new_mode": mode.value,
            "description": self._get_mode_description(mode),
            "priorities": self._get_mode_priorities_readable(mode),
            "timestamp": datetime.now().isoformat()
        }
    
    def get_current_mode(self) -> OperationalMode:
        """Get current operational mode"""
        return self.current_mode
    
    def make_decision(self, instruction: str, parsed_intent: Dict[str, Any]) -> Dict[str, Any]:
        """
        Make a decision based on current mode and instruction
        
        Returns:
            {
                "mode": current mode,
                "recommended_action": action to execute,
                "confidence": confidence level,
                "reasoning": why this decision was made,
                "alternatives": list of alternative actions,
                "mode_context": mode-specific context
            }
        """
        handler = self.mode_handlers.get(self.current_mode)
        if not handler:
            handler = self._handle_interact_mode
        
        decision = handler(instruction, parsed_intent)
        decision["mode"] = self.current_mode.value
        decision["timestamp"] = datetime.now().isoformat()
        
        return decision
    
    def _handle_learn_mode(self, instruction: str, parsed_intent: Dict[str, Any]) -> Dict[str, Any]:
        """
        LEARN MODE: Prioritize web search and information gathering
        Assistant is in learning/research mode
        """
        intent = parsed_intent.get("intent", "unknown")
        target = parsed_intent.get("target", "")
        
        # In learn mode, always prefer web sources
        if any(marker in instruction.lower() for marker in 
               ["what", "how", "learn", "understand", "teach", "explain", "tell me"]):
            return {
                "recommended_action_type": "web_search",
                "recommended_action": {
                    "type": "web_search",
                    "query": target or instruction,
                    "num_results": 8  # More results in learn mode
                },
                "confidence": 0.95,
                "reasoning": "Learn mode: prioritize information gathering",
                "mode_context": {
                    "priority": self.mode_priorities[OperationalMode.LEARN].get("web_search"),
                    "goal": "Gather comprehensive information"
                },
                "alternatives": [
                    {"type": "fetch_url", "reason": "Fetch specific documentation"},
                    {"type": "open_app", "reason": "Open learning app"}
                ]
            }
        
        # Open learning apps if mentioned
        learning_apps = ["github", "stackoverflow", "jupyter", "vscode", "python", "docs"]
        if any(app in instruction.lower() for app in learning_apps):
            return {
                "recommended_action_type": "open_app",
                "recommended_action": {
                    "type": "open_app",
                    "app_name": target
                },
                "confidence": 0.90,
                "reasoning": "Learn mode: open learning tool",
                "mode_context": {
                    "priority": self.mode_priorities[OperationalMode.LEARN].get("open_app"),
                    "goal": "Access learning resources"
                },
                "alternatives": [
                    {"type": "web_search", "reason": "Search for learning resources"}
                ]
            }
        
        # Default: web search
        return {
            "recommended_action_type": "web_search",
            "recommended_action": {
                "type": "web_search",
                "query": instruction,
                "num_results": 8
            },
            "confidence": 0.85,
            "reasoning": "Learn mode: gather information from web",
            "mode_context": {
                "priority": self.mode_priorities[OperationalMode.LEARN].get("web_search"),
                "goal": "Continuous learning"
            },
            "alternatives": []
        }
    
    def _handle_update_mode(self, instruction: str, parsed_intent: Dict[str, Any]) -> Dict[str, Any]:
        """
        UPDATE MODE: Prioritize reading, editing, and writing
        Assistant is in update/modification mode
        """
        intent = parsed_intent.get("intent", "unknown")
        target = parsed_intent.get("target", "")
        instruction_lower = instruction.lower()
        
        # Check for edit operations
        if any(verb in instruction_lower for verb in 
               ["update", "modify", "edit", "change", "fix", "rewrite", "format", "improve"]):
            return {
                "recommended_action_type": "edit",
                "recommended_action": {
                    "type": "edit",
                    "target": target or "current_file",
                    "instruction": instruction
                },
                "confidence": 0.95,
                "reasoning": "Update mode: prioritize editing/modification",
                "mode_context": {
                    "priority": self.mode_priorities[OperationalMode.UPDATE].get("edit"),
                    "goal": "Modify and improve content"
                },
                "alternatives": [
                    {"type": "read", "reason": "Read before editing"},
                    {"type": "web_search", "reason": "Research best practices"}
                ]
            }
        
        # Check for write operations
        if any(verb in instruction_lower for verb in 
               ["write", "create", "generate", "add", "append", "create new"]):
            return {
                "recommended_action_type": "write",
                "recommended_action": {
                    "type": "write",
                    "content": instruction
                },
                "confidence": 0.90,
                "reasoning": "Update mode: prioritize writing new content",
                "mode_context": {
                    "priority": self.mode_priorities[OperationalMode.UPDATE].get("write"),
                    "goal": "Create new content"
                },
                "alternatives": []
            }
        
        # Default: read first
        return {
            "recommended_action_type": "read",
            "recommended_action": {
                "type": "read",
                "target": target or "current"
            },
            "confidence": 0.85,
            "reasoning": "Update mode: read before modifying",
            "mode_context": {
                "priority": self.mode_priorities[OperationalMode.UPDATE].get("read"),
                "goal": "Understand current state"
            },
            "alternatives": [
                {"type": "edit", "reason": "Make modifications"},
                {"type": "web_search", "reason": "Research updates"}
            ]
        }
    
    def _handle_execute_mode(self, instruction: str, parsed_intent: Dict[str, Any]) -> Dict[str, Any]:
        """
        EXECUTE MODE: Prioritize task execution and commands
        Assistant is in execution/task mode
        """
        intent = parsed_intent.get("intent", "unknown")
        target = parsed_intent.get("target", "")
        instruction_lower = instruction.lower()
        
        # App execution
        if intent == "open" or any(verb in instruction_lower for verb in 
                                    ["open", "launch", "start", "run", "execute"]):
            return {
                "recommended_action_type": "open_app",
                "recommended_action": {
                    "type": "open_app",
                    "app_name": target
                },
                "confidence": 0.95,
                "reasoning": "Execute mode: prioritize launching applications",
                "mode_context": {
                    "priority": self.mode_priorities[OperationalMode.EXECUTE].get("open_app"),
                    "goal": "Execute requested application"
                },
                "alternatives": [
                    {"type": "execute_command", "reason": "Execute via command line"}
                ]
            }
        
        # Command execution
        if any(verb in instruction_lower for verb in 
               ["run", "execute", "perform", "do", "make", "build"]):
            return {
                "recommended_action_type": "execute_command",
                "recommended_action": {
                    "type": "execute_command",
                    "command": instruction
                },
                "confidence": 0.90,
                "reasoning": "Execute mode: run command",
                "mode_context": {
                    "priority": self.mode_priorities[OperationalMode.EXECUTE].get("execute_command"),
                    "goal": "Complete task execution"
                },
                "alternatives": []
            }
        
        # Default: open app
        return {
            "recommended_action_type": "open_app",
            "recommended_action": {
                "type": "open_app",
                "app_name": target or "default"
            },
            "confidence": 0.80,
            "reasoning": "Execute mode: prepare for task execution",
            "mode_context": {
                "priority": self.mode_priorities[OperationalMode.EXECUTE].get("open_app"),
                "goal": "Set up environment for execution"
            },
            "alternatives": []
        }
    
    def _handle_analyze_mode(self, instruction: str, parsed_intent: Dict[str, Any]) -> Dict[str, Any]:
        """
        ANALYZE MODE: Prioritize data gathering and pattern recognition
        Assistant is in analysis/research mode
        """
        instruction_lower = instruction.lower()
        target = parsed_intent.get("target", "")
        
        # Check for analysis keywords
        analysis_keywords = ["analyze", "research", "compare", "evaluate", "assess", "review", "examine"]
        
        if any(keyword in instruction_lower for keyword in analysis_keywords):
            return {
                "recommended_action_type": "web_search",
                "recommended_action": {
                    "type": "web_search",
                    "query": instruction,
                    "num_results": 10  # More results for comprehensive analysis
                },
                "confidence": 0.95,
                "reasoning": "Analyze mode: gather comprehensive data",
                "mode_context": {
                    "priority": self.mode_priorities[OperationalMode.ANALYZE].get("web_search"),
                    "goal": "Collect data for analysis"
                },
                "alternatives": [
                    {"type": "fetch_url", "reason": "Fetch specific sources"},
                    {"type": "read", "reason": "Read local data"}
                ]
            }
        
        # Open analysis tools
        analysis_tools = ["excel", "python", "jupyter", "tableau", "powerbi", "calculator"]
        if any(tool in instruction_lower for tool in analysis_tools):
            return {
                "recommended_action_type": "open_app",
                "recommended_action": {
                    "type": "open_app",
                    "app_name": target
                },
                "confidence": 0.90,
                "reasoning": "Analyze mode: open analysis tool",
                "mode_context": {
                    "priority": self.mode_priorities[OperationalMode.ANALYZE].get("open_app"),
                    "goal": "Perform analysis"
                },
                "alternatives": []
            }
        
        # Default: web search
        return {
            "recommended_action_type": "web_search",
            "recommended_action": {
                "type": "web_search",
                "query": instruction
            },
            "confidence": 0.85,
            "reasoning": "Analyze mode: gather information",
            "mode_context": {
                "priority": self.mode_priorities[OperationalMode.ANALYZE].get("web_search"),
                "goal": "Support analysis"
            },
            "alternatives": []
        }
    
    def _handle_develop_mode(self, instruction: str, parsed_intent: Dict[str, Any]) -> Dict[str, Any]:
        """
        DEVELOP MODE: Prioritize code development and improvement
        Assistant is in development/improvement mode
        """
        instruction_lower = instruction.lower()
        target = parsed_intent.get("target", "")
        
        # Code reading
        if any(verb in instruction_lower for verb in 
               ["read", "show", "view", "display", "check", "review", "examine code"]):
            return {
                "recommended_action_type": "read",
                "recommended_action": {
                    "type": "read",
                    "target": target or "source"
                },
                "confidence": 0.95,
                "reasoning": "Develop mode: read code before modification",
                "mode_context": {
                    "priority": self.mode_priorities[OperationalMode.DEVELOP].get("read"),
                    "goal": "Understand code structure"
                },
                "alternatives": []
            }
        
        # Code editing/writing
        if any(verb in instruction_lower for verb in 
               ["fix", "improve", "refactor", "optimize", "modify", "update", "write", "code", "implement"]):
            return {
                "recommended_action_type": "edit",
                "recommended_action": {
                    "type": "edit",
                    "target": target or "source"
                },
                "confidence": 0.95,
                "reasoning": "Develop mode: modify code",
                "mode_context": {
                    "priority": self.mode_priorities[OperationalMode.DEVELOP].get("edit"),
                    "goal": "Improve code quality"
                },
                "alternatives": [
                    {"type": "web_search", "reason": "Research best practices"},
                    {"type": "fetch_url", "reason": "Check documentation"}
                ]
            }
        
        # Open IDE
        if any(app in instruction_lower for app in ["vscode", "ide", "editor", "code editor", "visual studio"]):
            return {
                "recommended_action_type": "open_app",
                "recommended_action": {
                    "type": "open_app",
                    "app_name": "vscode"
                },
                "confidence": 0.90,
                "reasoning": "Develop mode: open IDE",
                "mode_context": {
                    "priority": self.mode_priorities[OperationalMode.DEVELOP].get("open_app"),
                    "goal": "Code development environment"
                },
                "alternatives": []
            }
        
        # Default: read code
        return {
            "recommended_action_type": "read",
            "recommended_action": {
                "type": "read",
                "target": "current"
            },
            "confidence": 0.85,
            "reasoning": "Develop mode: review code",
            "mode_context": {
                "priority": self.mode_priorities[OperationalMode.DEVELOP].get("read"),
                "goal": "Development process"
            },
            "alternatives": []
        }
    
    def _handle_creative_mode(self, instruction: str, parsed_intent: Dict[str, Any]) -> Dict[str, Any]:
        """
        CREATIVE MODE: Prioritize creative tools and content generation
        Assistant is in creative problem-solving mode
        """
        instruction_lower = instruction.lower()
        target = parsed_intent.get("target", "")
        
        # Creative tools
        creative_tools = ["photoshop", "gimp", "blender", "audacity", "davinci", "final cut", "word", "docs", "canva"]
        
        if any(tool in instruction_lower for tool in creative_tools):
            return {
                "recommended_action_type": "open_app",
                "recommended_action": {
                    "type": "open_app",
                    "app_name": target
                },
                "confidence": 0.95,
                "reasoning": "Creative mode: open creative tool",
                "mode_context": {
                    "priority": self.mode_priorities[OperationalMode.CREATIVE].get("open_app"),
                    "goal": "Enable creative work"
                },
                "alternatives": []
            }
        
        # Creative content writing
        if any(verb in instruction_lower for verb in 
               ["write", "create", "generate", "design", "compose", "brainstorm", "ideate"]):
            return {
                "recommended_action_type": "type_text",
                "recommended_action": {
                    "type": "type_text",
                    "text": instruction
                },
                "confidence": 0.90,
                "reasoning": "Creative mode: create content",
                "mode_context": {
                    "priority": self.mode_priorities[OperationalMode.CREATIVE].get("type_text"),
                    "goal": "Generate creative content"
                },
                "alternatives": [
                    {"type": "web_search", "reason": "Get inspiration"}
                ]
            }
        
        # Research inspiration
        if any(verb in instruction_lower for verb in 
               ["research", "find", "look for", "search", "brainstorm", "inspire"]):
            return {
                "recommended_action_type": "web_search",
                "recommended_action": {
                    "type": "web_search",
                    "query": instruction,
                    "num_results": 8
                },
                "confidence": 0.90,
                "reasoning": "Creative mode: gather inspiration",
                "mode_context": {
                    "priority": self.mode_priorities[OperationalMode.CREATIVE].get("web_search"),
                    "goal": "Find creative inspiration"
                },
                "alternatives": []
            }
        
        # Default: open creative tool
        return {
            "recommended_action_type": "open_app",
            "recommended_action": {
                "type": "open_app",
                "app_name": "creative_tool"
            },
            "confidence": 0.80,
            "reasoning": "Creative mode: prepare creative environment",
            "mode_context": {
                "priority": self.mode_priorities[OperationalMode.CREATIVE].get("open_app"),
                "goal": "Enable creative work"
            },
            "alternatives": []
        }
    
    def _handle_interact_mode(self, instruction: str, parsed_intent: Dict[str, Any]) -> Dict[str, Any]:
        """
        INTERACT MODE: Normal human interaction mode (default)
        Balanced approach based on intent
        """
        intent = parsed_intent.get("intent", "unknown")
        target = parsed_intent.get("target", "")
        
        # Use standard decision logic
        if intent == "open":
            return {
                "recommended_action_type": "open_app",
                "recommended_action": {
                    "type": "open_app",
                    "app_name": target
                },
                "confidence": 0.95,
                "reasoning": "User explicitly requested to open app",
                "mode_context": {
                    "priority": self.mode_priorities[OperationalMode.INTERACT].get("open_app"),
                    "goal": "Normal task execution"
                },
                "alternatives": []
            }
        
        elif intent == "search":
            return {
                "recommended_action_type": "web_search",
                "recommended_action": {
                    "type": "web_search",
                    "query": target
                },
                "confidence": 0.95,
                "reasoning": "User explicitly requested web search",
                "mode_context": {
                    "priority": self.mode_priorities[OperationalMode.INTERACT].get("web_search"),
                    "goal": "Information gathering"
                },
                "alternatives": []
            }
        
        elif intent == "web":
            return {
                "recommended_action_type": "open_url",
                "recommended_action": {
                    "type": "open_url",
                    "url": target
                },
                "confidence": 0.90,
                "reasoning": "User wants to visit website",
                "mode_context": {
                    "priority": self.mode_priorities[OperationalMode.INTERACT].get("open_url"),
                    "goal": "Web browsing"
                },
                "alternatives": []
            }
        
        # Default
        return {
            "recommended_action_type": "unknown",
            "recommended_action": None,
            "confidence": 0.50,
            "reasoning": "Unclear intent - normal interaction mode",
            "mode_context": {
                "priority": 5,
                "goal": "Clarification needed"
            },
            "alternatives": []
        }
    
    def _get_mode_description(self, mode: OperationalMode) -> str:
        """Get human-readable description of a mode"""
        descriptions = {
            OperationalMode.LEARN: "🎓 Learning Mode - Focus on information gathering and understanding",
            OperationalMode.UPDATE: "✏️ Update Mode - Focus on modification and improvement",
            OperationalMode.EXECUTE: "⚡ Execute Mode - Focus on task execution",
            OperationalMode.ANALYZE: "📊 Analyze Mode - Focus on data analysis and research",
            OperationalMode.DEVELOP: "👨‍💻 Develop Mode - Focus on code development",
            OperationalMode.CREATIVE: "🎨 Creative Mode - Focus on creative problem-solving",
            OperationalMode.INTERACT: "💬 Interact Mode - Normal balanced interaction",
        }
        return descriptions.get(mode, "Unknown Mode")
    
    def _get_mode_priorities_readable(self, mode: OperationalMode) -> Dict[str, str]:
        """Get readable priority list for a mode"""
        priorities = self.mode_priorities.get(mode, {})
        sorted_priorities = sorted(priorities.items(), key=lambda x: x[1], reverse=True)
        
        return {
            action: f"Priority {priority}"
            for action, priority in sorted_priorities
        }
    
    def get_mode_summary(self) -> Dict[str, Any]:
        """Get summary of all available modes"""
        return {
            "current_mode": self.current_mode.value,
            "available_modes": [mode.value for mode in OperationalMode],
            "mode_descriptions": {
                mode.value: self._get_mode_description(mode)
                for mode in OperationalMode
            },
            "recent_modes": self.mode_history[-5:] if self.mode_history else []
        }


# Global singleton
mode_engine = None

async def initialize_mode_engine() -> ModeDecisionEngine:
    """Initialize the global mode engine"""
    global mode_engine
    mode_engine = ModeDecisionEngine()
    return mode_engine

async def get_mode_engine() -> ModeDecisionEngine:
    """Get the mode engine instance"""
    global mode_engine
    if mode_engine is None:
        mode_engine = ModeDecisionEngine()
    return mode_engine
