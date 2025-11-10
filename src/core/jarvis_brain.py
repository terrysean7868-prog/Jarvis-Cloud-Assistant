# jarvis_brain.py
import os
import sqlite3
import json
from datetime import datetime
from typing import Dict, List, Optional, Any
from src.core.llm_adapter import LLMAdapter
from src.utils.db import db
import asyncio
import importlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import inspect
import ast
from src.core.cognitive_core import JarvisCognition, CognitiveMode

# Import memory system
try:
    from src.memory.memory import BotMemory
    MEMORY_AVAILABLE = True
except ImportError:
    MEMORY_AVAILABLE = False
    BotMemory = None

# Allow Jarvis to modify its own code: modules, utils, config files, and core files
DEFAULT_ALLOWED = "modules,utils,jarvis-frontend/src,app.py,jarvis_brain.py,llm_adapter.py,executor.py,git_sync.py,run_jarvis.py,config.py,requirements.txt,README.md"
ALLOWED_PATHS = [p.strip() for p in os.getenv("ALLOWED_PATHS", DEFAULT_ALLOWED).split(",") if p.strip()]
AUTO_APPLY = os.getenv("AUTO_APPLY", "true").lower() == "true"  # Default to true for Iron Man mode

class CodeAnalyzer:
    @staticmethod
    def analyze_python_file(file_path: str) -> Dict[str, Any]:
        """Analyze a Python file for functions, classes, and dependencies"""
        with open(file_path, 'r', encoding='utf-8') as f:
            code = f.read()
        
        tree = ast.parse(code)
        analysis = {
            'functions': [],
            'classes': [],
            'imports': [],
            'doc': ast.get_docstring(tree)
        }
        
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                analysis['functions'].append({
                    'name': node.name,
                    'doc': ast.get_docstring(node),
                    'args': [arg.arg for arg in node.args.args]
                })
            elif isinstance(node, ast.ClassDef):
                analysis['classes'].append({
                    'name': node.name,
                    'doc': ast.get_docstring(node),
                    'methods': [m.name for m in node.body if isinstance(m, ast.FunctionDef)]
                })
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                if isinstance(node, ast.Import):
                    for n in node.names:
                        analysis['imports'].append(n.name)
                else:
                    module = node.module if node.module else ''
                    for n in node.names:
                        analysis['imports'].append(f"{module}.{n.name}")
        
        return analysis

class ContextManager:
    def __init__(self):
        self.short_term = {}
        self.conversation_history = []
        self.task_queue = asyncio.Queue()
        self.executor = ThreadPoolExecutor(max_workers=4)
    
    def add_to_history(self, role: str, content: str):
        self.conversation_history.append({
            'role': role,
            'content': content,
            'timestamp': datetime.utcnow()
        })
        if len(self.conversation_history) > 100:
            self.conversation_history.pop(0)
    
    def get_relevant_context(self, query: str) -> str:
        # Implement semantic search on conversation history
        return "\n".join([
            f"{msg['role']}: {msg['content']}"
            for msg in self.conversation_history[-5:]
        ])

class JarvisBrain:
    def __init__(self, llm: LLMAdapter, user_id: str = "default"):
        self.llm = llm
        self.user_id = user_id
        self.context = ContextManager()
        self.conn = sqlite3.connect('jarvis_memory.db', check_same_thread=False)
        self.cognition = JarvisCognition()  # Initialize cognitive architecture
        
        # Initialize memory system
        self.memory = BotMemory(user_id) if MEMORY_AVAILABLE else None
        
        self.setup_database()
        self.capabilities = self._load_capabilities()
        self.current_mode = CognitiveMode.INTERACT
        
    def setup_database(self):
        """Setup SQLite tables for local memory"""
        with self.conn:
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY,
                    key TEXT UNIQUE,
                    value TEXT,
                    category TEXT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY,
                    description TEXT,
                    status TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    completed_at DATETIME
                )
            """)
    
    def _load_capabilities(self) -> Dict[str, Any]:
        """Load and analyze all available capabilities"""
        capabilities = {}
        modules_dir = Path(__file__).parent / 'modules'
        
        for file_path in modules_dir.glob('*.py'):
            if file_path.stem.startswith('_'):
                continue
            
            try:
                analysis = CodeAnalyzer.analyze_python_file(str(file_path))
                capabilities[file_path.stem] = {
                    'analysis': analysis,
                    'module': importlib.import_module(f'modules.{file_path.stem}')
                }
            except Exception as e:
                print(f"⚠️ Could not load module '{file_path.stem}': {e}")
                # Continue loading other modules even if one fails
                capabilities[file_path.stem] = {
                    'analysis': {'functions': [], 'classes': [], 'imports': []},
                    'module': None,
                    'error': str(e)
                }
        
        return capabilities

    def remember(self, key: str, value: str, category: str = 'general'):
        """Store information in local memory"""
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO memories (key, value, category) VALUES (?, ?, ?)",
                (key, value, category)
            )
        # Also store in MongoDB for long-term persistence
        db.save_system_event(
            event_type='memory_store',
            description=f'Stored memory: {key}',
            status='success',
            details={'category': category, 'value': value}
        )

    def recall(self, key: str) -> Optional[str]:
        """Retrieve information from memory"""
        cur = self.conn.execute("SELECT value FROM memories WHERE key = ?", (key,))
        result = cur.fetchone()
        return result[0] if result else None

    async def analyze_code_changes(self, content: str) -> Dict[str, Any]:
        """Analyze proposed code changes for safety and improvements"""
        analysis = {
            'safe': True,
            'improvements': [],
            'warnings': [],
            'affected_modules': []
        }
        
        try:
            tree = ast.parse(content)
            for node in ast.walk(tree):
                # Check for potentially dangerous operations
                if isinstance(node, ast.Call):
                    func_name = ''
                    if isinstance(node.func, ast.Name):
                        func_name = node.func.id
                    elif isinstance(node.func, ast.Attribute):
                        func_name = node.func.attr
                        
                    dangerous_funcs = ['eval', 'exec', 'os.system', 'subprocess.call']
                    if func_name in dangerous_funcs:
                        analysis['safe'] = False
                        analysis['warnings'].append(f'Potentially dangerous function: {func_name}')
                
                # Collect affected modules
                if isinstance(node, ast.Import) or isinstance(node, ast.ImportFrom):
                    for name in node.names:
                        analysis['affected_modules'].append(name.name)
        
        except Exception as e:
            analysis['safe'] = False
            analysis['warnings'].append(f'Code analysis error: {str(e)}')
        
        return analysis

    async def execute_action(self, action: Dict[str, Any]) -> bool:
        """Execute a proposed action safely"""
        try:
            action_type = action['type']
            if action_type == 'code_change':
                analysis = await self.analyze_code_changes(action['content'])
                if not analysis['safe']:
                    return False
                    
                # Log the change
                db.save_module_change(
                    module_name=action.get('module', 'unknown'),
                    change_type='update',
                    content=action['content']
                )
                
            elif action_type == 'system_command':
                # Log the command
                db.save_system_event(
                    event_type='command_execution',
                    description=action['command'],
                    status='pending'
                )
                
            elif action_type == 'git_operation':
                # Log the git operation
                db.save_git_sync(
                    commit_hash=action.get('commit_hash'),
                    message=action['message'],
                    status='pending'
                )
                
            return True
            
        except Exception as e:
            db.save_system_event(
                event_type='action_error',
                description=str(e),
                status='error'
            )
            return False

    async def handle_message(self, text: str, mode: str = "chat", user_id: str = None) -> Dict[str, Any]:
        """Process user input and generate appropriate response based on cognitive mode"""
        session_id = user_id or 'default_session'
        
        # Add message to context
        self.context.add_to_history('user', text)
        
        # Get relevant context (both from history and memory if available)
        context = self.context.get_relevant_context(text)
        
        # Add memory context if available
        if self.memory:
            memory_context = self.memory.get_contextual_memory()
            context = f"{context}\n{memory_context}" if context and memory_context else context or memory_context
        
        try:
            # Check for mode change commands
            if text.lower().startswith('switch to'):
                requested_mode = text.lower().replace('switch to', '').strip()
                try:
                    cognitive_mode = CognitiveMode(requested_mode)
                    await self.cognition.switch_mode(cognitive_mode)
                    
                    mode_descriptions = {
                        CognitiveMode.LEARN: (
                            "Learning mode activated. I'll now focus on pattern recognition, "
                            "knowledge acquisition, and updating my understanding based on our interactions."
                        ),
                        CognitiveMode.DEVELOP: (
                            "Development mode activated. I'll focus on system improvements, "
                            "code analysis, and implementing new features."
                        ),
                        CognitiveMode.EXECUTE: (
                            "Execution mode activated. I'll focus on task completion, "
                            "command processing, and efficient operation."
                        ),
                        CognitiveMode.INTERACT: (
                            "Interaction mode activated. I'll focus on natural conversation "
                            "and assisting you with any questions or tasks."
                        )
                    }
                    
                    response_text = mode_descriptions.get(
                        cognitive_mode,
                        f"Switched to {requested_mode} mode. Ready for your instructions."
                    )
                    
                    # Store mode change in context memory
                    self.context_memory['last_mode_change'] = {
                        'timestamp': datetime.utcnow().isoformat(),
                        'from_mode': str(self.current_mode) if hasattr(self, 'current_mode') else 'unknown',
                        'to_mode': str(cognitive_mode)
                    }
                    
                    self.current_mode = cognitive_mode
                    
                    return {
                        'text': response_text,
                        'status': 'success',
                        'mode_change': True,
                        'mode': cognitive_mode.value
                    }
                    
                except ValueError:
                    available_modes = [mode.value for mode in CognitiveMode]
                    return {
                        'text': (
                            f"Invalid mode: '{requested_mode}'. Available modes are: "
                            f"{', '.join(available_modes)}"
                        ),
                        'status': 'error'
                    }
            
            # Process based on current cognitive mode
            input_data = {
                'text': text,
                'context': context,
                'session_id': session_id,
                'mode': mode,
                'capabilities': list(self.capabilities.keys())
            }
            
            if self.cognition.cognitive.current_mode == CognitiveMode.DEVELOP:
                # Development mode: focus on system improvement
                input_data['target_area'] = self._detect_target_area(text)
                response = await self.cognition.develop_system(input_data)
            
            elif self.cognition.cognitive.current_mode == CognitiveMode.EXECUTE:
                # Execution mode: focus on task completion
                response = await self.cognition.execute_task(input_data)
            
            elif self.cognition.cognitive.current_mode == CognitiveMode.LEARN:
                # Learning mode: focus on pattern recognition and knowledge acquisition
                response = await self.cognition.learn_from_interaction(input_data)
            
            else:
                # Default interactive mode
                response = await self.llm.generate_response(
                    text,
                    context=context,
                    mode=mode,
                    capabilities=list(self.capabilities.keys())
                )
            
            # Extract and execute actions if any
            actions = []
            if isinstance(response, dict) and 'actions' in response:
                actions = response['actions']
                for action in actions:
                    await self.execute_action(action)
            
            response_text = response.get('text', str(response)) if isinstance(response, dict) else str(response)
            intent = response.get('intent') if isinstance(response, dict) else None
            
            # Store conversation in memory if available
            if self.memory:
                self.memory.save_conversation(
                    user_input=text,
                    bot_response=response_text,
                    intent=intent
                )
            
            # Store interaction in MongoDB
            db.save_chat(
                user_input=text,
                bot_response=response_text,
                session_id=session_id,
                intent=intent,
                context={
                    'mode': self.cognition.cognitive.current_mode.value,
                    'actions': actions,
                    'source': response.get('source', 'unknown') if isinstance(response, dict) else 'unknown'
                }
            )
            
            # Add response to context
            self.context.add_to_history('assistant', response_text)
            
            # Vocalize response if speech is enabled
            if mode == 'voice':
                await self.cognition.cognitive.speak(response_text)
            
            return {
                'text': response.get('text', str(response)),
                'actions': actions,
                'status': 'success',
                'mode': self.cognition.cognitive.current_mode.value
            }
            
        except Exception as e:
            error_msg = f"Error processing message: {str(e)}"
            db.save_system_event(
                event_type='message_error',
                description=error_msg,
                status='error'
            )
            return {
                'text': error_msg,
                'status': 'error'
            }

    async def process_background_tasks(self):
        """Process queued background tasks"""
        while True:
            try:
                task = await self.context.task_queue.get()
                await self.execute_action(task)
            except Exception as e:
                db.save_system_event(
                    event_type='background_task_error',
                    description=str(e),
                    status='error'
                )
            await asyncio.sleep(1)

    def start(self):
        """Start the background task processor"""
        asyncio.create_task(self.process_background_tasks())

    async def process_text(self, text: str, system: str = None) -> dict:
        """Process text input and generate a response with actions"""
        # Include project context
        project_context = f"\n\nCurrent memory keys: {self._memory_keys()}\n"
        project_context += f"Allowed paths: {', '.join(ALLOWED_PATHS)}\n"
        project_context += f"Auto-apply mode: {AUTO_APPLY}"
        
        prompt = f"User: {text}{project_context}"
        resp = await self.llm.generate(prompt, system=system, max_tokens=4096)
        text_out = resp.get("text", "")
        actions = self.llm.parse_actions_from_text(text_out)
        
        # Filter actions to allowed ones only (executor will double-check)
        allowed_actions = []
        for a in actions:
            path = a.get("path")
            if path and self.is_path_allowed(path):
                allowed_actions.append(a)
            elif path:
                # Log forbidden paths for debugging
                print(f"Warning: Path not allowed: {path}")
        
        return {"text": text_out, "actions": allowed_actions, "auto_apply": AUTO_APPLY}

    def _memory_keys(self):
        cur = self.conn.execute("SELECT k FROM memories")
        return [r[0] for r in cur.fetchall()]
        
    def _detect_target_area(self, text: str) -> str:
        """Detect the target area for system improvement from user input"""
        target_areas = {
            'memory': ['memory', 'remember', 'recall', 'store', 'database'],
            'learning': ['learning', 'learn', 'train', 'improve', 'pattern'],
            'voice': ['voice', 'speech', 'speak', 'audio', 'sound'],
            'understanding': ['understand', 'comprehend', 'process', 'analyze'],
            'execution': ['execute', 'run', 'perform', 'task', 'action']
        }
        
        text = text.lower()
        for area, keywords in target_areas.items():
            if any(keyword in text for keyword in keywords):
                return area
        
        return 'general'
