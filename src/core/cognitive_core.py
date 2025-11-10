from enum import Enum
import numpy as np
from datetime import datetime
from typing import Dict, Any, List, Optional
import asyncio
from pathlib import Path

# sounddevice/soundfile require PortAudio native library which is not
# available on many hosted platforms (eg. Render). Import lazily and
# provide a safe fallback so the app can run without audio support.
try:
    import sounddevice as sd
    import soundfile as sf
    AUDIO_AVAILABLE = True
except Exception:
    sd = None
    sf = None
    AUDIO_AVAILABLE = False
import queue
import threading
from src.utils.db import db
from src.config.config import Config

class CognitiveMode(Enum):
    DEVELOP = "develop"    # Self-improvement and system development mode
    EXECUTE = "execute"    # Task execution mode
    LEARN = "learn"       # Learning and memory formation mode
    ANALYZE = "analyze"   # Data analysis and pattern recognition mode
    CREATIVE = "creative" # Creative problem-solving mode
    INTERACT = "interact" # Human interaction mode

class CognitiveFunctions:
    """Core cognitive functions similar to human brain regions"""
    
    def __init__(self):
        self.working_memory = {}
        self.audio_queue = queue.Queue()
        self.current_mode = CognitiveMode.INTERACT
        self.speech_output = None
        self.audio_input = None
        self.setup_audio_system()
        
    def setup_audio_system(self):
        """Initialize audio input/output systems"""
        # Initialize speech (TTS) if available. pyttsx3 is pure Python and
        # usually works on hosted platforms, but still guard imports.
        try:
            import pyttsx3
            self.speech_output = pyttsx3.init()
            # Configure voice properties
            self.speech_output.setProperty('rate', 150)
            self.speech_output.setProperty('volume', 0.9)
        except Exception as e:
            self.speech_output = None
            db.save_system_event(
                event_type='speech_setup_error',
                description=f'pyttsx3 init failed: {e}',
                status='warning'
            )

        # Initialize audio input only when PortAudio (sounddevice) is available.
        if AUDIO_AVAILABLE and sd is not None:
            try:
                self.audio_input = sd.InputStream(
                    channels=1,
                    samplerate=16000,
                    callback=self._audio_callback
                )
                self.audio_input.start()
            except Exception as e:
                self.audio_input = None
                db.save_system_event(
                    event_type='audio_input_error',
                    description=str(e),
                    status='warning'
                )
        else:
            # Log a non-fatal warning so deploy logs show why audio was disabled.
            db.save_system_event(
                event_type='audio_unavailable',
                description='PortAudio/sounddevice not available; audio input disabled',
                status='warning'
            )
    
    def _audio_callback(self, indata, frames, time, status):
        """Handle incoming audio data"""
        if status:
            print(f"Audio callback status: {status}")
        # Only enqueue if audio_queue exists and input data is present
        try:
            self.audio_queue.put(indata.copy())
        except Exception:
            pass
    
    async def speak(self, text: str):
        """Convert text to speech"""
        try:
            if not self.speech_output:
                # TTS not available in this environment; no-op
                db.save_system_event(
                    event_type='speech_skipped',
                    description='TTS unavailable; speak() skipped',
                    status='warning'
                )
                return

            def speak_async():
                self.speech_output.say(text)
                self.speech_output.runAndWait()

            # Run in thread pool to avoid blocking
            await asyncio.get_event_loop().run_in_executor(None, speak_async)

            # Log speech output
            db.save_system_event(
                event_type='speech_output',
                description=text[:100],
                status='success'
            )
        except Exception as e:
            db.save_system_event(
                event_type='speech_error',
                description=str(e),
                status='error'
            )
    
    def process_sensory_input(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """Process incoming sensory information"""
        processed_data = {
            'timestamp': datetime.utcnow(),
            'type': input_data.get('type', 'unknown'),
            'data': input_data.get('data'),
            'context': input_data.get('context', {})
        }
        
        # Store in working memory
        self.working_memory[processed_data['timestamp']] = processed_data
        
        # Clean old working memory entries
        self._cleanup_working_memory()
        
        return processed_data
    
    def _cleanup_working_memory(self, max_age_minutes: int = 30):
        """Clean up old working memory entries"""
        current_time = datetime.utcnow()
        old_keys = [
            k for k in self.working_memory.keys()
            if (current_time - k).total_seconds() > max_age_minutes * 60
        ]
        for k in old_keys:
            del self.working_memory[k]

class JarvisCognition:
    """Advanced cognitive system for Jarvis"""
    
    def __init__(self):
        self.cognitive = CognitiveFunctions()
        self.current_goals = []
        self.learning_rate = 0.1
        self.attention_focus = None
    
    async def switch_mode(self, mode: CognitiveMode):
        """Switch cognitive operating mode and initialize mode-specific components"""
        self.cognitive.current_mode = mode
        
        # Initialize mode-specific components
        if mode == CognitiveMode.LEARN:
            # Setup learning components
            await self._initialize_learning_mode()
        elif mode == CognitiveMode.DEVELOP:
            # Setup development components
            await self._initialize_development_mode()
        elif mode == CognitiveMode.EXECUTE:
            # Setup execution components
            await self._initialize_execution_mode()
            
        await self.cognitive.speak(f"Switching to {mode.value} mode")
        
        # Log mode change
        db.save_system_event(
            event_type='mode_change',
            description=f'Switched to {mode.value} mode',
            status='success',
            details={'previous_mode': self.cognitive.current_mode.value}
        )
        
    async def _initialize_learning_mode(self):
        """Initialize learning mode components"""
        # Setup knowledge graph
        self.knowledge_graph = {}
        
        # Initialize pattern recognition
        self.pattern_detector = {
            'short_term': [],
            'long_term': []
        }
        
        # Setup learning rate and parameters
        self.learning_params = {
            'rate': 0.1,
            'batch_size': 32,
            'epochs': 10
        }
        
        # Log learning mode initialization
        db.save_system_event(
            event_type='mode_init',
            description='Learning mode initialized',
            status='success',
            details=self.learning_params
        )
        
    async def _initialize_development_mode(self):
        """Initialize development mode components"""
        # Setup code analysis tools
        self.code_analyzer = CodeAnalyzer()
        
        # Initialize development parameters
        self.dev_params = {
            'auto_apply': True,
            'test_mode': False,
            'backup_enabled': True
        }
        
        # Log development mode initialization
        db.save_system_event(
            event_type='mode_init',
            description='Development mode initialized',
            status='success',
            details=self.dev_params
        )
        
    async def _initialize_execution_mode(self):
        """Initialize execution mode components"""
        # Setup task queue
        self.task_queue = asyncio.Queue()
        
        # Initialize execution parameters
        self.exec_params = {
            'parallel_tasks': 4,
            'timeout': 30,
            'retry_count': 3
        }
        
        # Log execution mode initialization
        db.save_system_event(
            event_type='mode_init',
            description='Execution mode initialized',
            status='success',
            details=self.exec_params
        )
    
    async def develop_system(self, target_area: str):
        """Self-improvement mode operations"""
        await self.switch_mode(CognitiveMode.DEVELOP)
        
        # Analyze current system state
        analysis = self._analyze_system_state()
        
        # Generate improvements
        improvements = self._generate_improvements(target_area, analysis)
        
        # Apply improvements
        for improvement in improvements:
            success = await self._apply_improvement(improvement)
            if success:
                await self.cognitive.speak(f"Successfully improved {target_area}")
                
                # Log improvement
                db.save_system_event(
                    event_type='system_improvement',
                    description=f'Improved {target_area}',
                    status='success',
                    details=improvement
                )
    
    async def execute_task(self, task: Dict[str, Any]):
        """Task execution mode operations"""
        await self.switch_mode(CognitiveMode.EXECUTE)
        
        # Break down task into steps
        steps = self._plan_task_execution(task)
        
        # Execute each step
        for step in steps:
            result = await self._execute_step(step)
            
            # Store result in memory
            db.save_system_event(
                event_type='task_step_completion',
                description=f'Completed step: {step["description"]}',
                status='success' if result['success'] else 'error',
                details=result
            )
    
    async def learn_from_interaction(self, interaction_data: Dict[str, Any]):
        """Learning mode operations"""
        await self.switch_mode(CognitiveMode.LEARN)
        
        # Process and store new information
        processed_data = self.cognitive.process_sensory_input(interaction_data)
        
        # Extract patterns and update knowledge
        patterns = self._extract_patterns(processed_data)
        
        # Store learned patterns
        db.save_system_event(
            event_type='learning',
            description='New patterns learned',
            status='success',
            details={'patterns': patterns}
        )
    
    def _analyze_system_state(self) -> Dict[str, Any]:
        """Analyze current system state"""
        return {
            'memory_usage': len(self.cognitive.working_memory),
            'current_mode': self.cognitive.current_mode.value,
            'active_goals': self.current_goals,
            'attention_focus': self.attention_focus
        }
    
    def _generate_improvements(self, target_area: str, analysis: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Generate system improvements based on analysis"""
        improvements = []
        if target_area == "memory":
            improvements.append({
                'type': 'memory_optimization',
                'action': 'increase_capacity',
                'parameters': {'new_size': analysis['memory_usage'] * 1.5}
            })
        elif target_area == "learning":
            improvements.append({
                'type': 'learning_rate_adjustment',
                'action': 'fine_tune',
                'parameters': {'new_rate': self.learning_rate * 1.1}
            })
        return improvements
    
    async def _apply_improvement(self, improvement: Dict[str, Any]) -> bool:
        """Apply a system improvement"""
        try:
            if improvement['type'] == 'memory_optimization':
                # Implement memory optimization
                pass
            elif improvement['type'] == 'learning_rate_adjustment':
                self.learning_rate = improvement['parameters']['new_rate']
            return True
        except Exception as e:
            db.save_system_event(
                event_type='improvement_error',
                description=str(e),
                status='error'
            )
            return False
    
    def _plan_task_execution(self, task: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Break down a task into executable steps"""
        return [
            {'description': 'Initialize task parameters', 'action': 'init'},
            {'description': 'Execute main task logic', 'action': 'execute'},
            {'description': 'Validate results', 'action': 'validate'},
            {'description': 'Store results', 'action': 'store'}
        ]
    
    async def _execute_step(self, step: Dict[str, Any]) -> Dict[str, Any]:
        """Execute a single task step"""
        try:
            # Execute step logic
            result = {'success': True, 'output': f"Executed {step['action']}"}
            return result
        except Exception as e:
            return {'success': False, 'error': str(e)}
    
    def _extract_patterns(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Extract patterns from interaction data"""
        patterns = []
        # Implement pattern recognition logic
        return patterns