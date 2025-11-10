# llm_adapter.py
import os
import re
import json
import asyncio
import aiohttp
from datetime import datetime
from typing import Dict, List, Any, Optional
from dotenv import load_dotenv
from utils.db import db

load_dotenv()

class ResponseParser:
    @staticmethod
    def extract_actions(text: str) -> List[Dict[str, Any]]:
        """Extract action objects from response text"""
        actions = []
        try:
            # Look for JSON block at end of response
            matches = re.findall(r'```json\n(.*?)\n```', text, re.DOTALL)
            if matches:
                json_data = json.loads(matches[-1])
                if isinstance(json_data, dict) and 'actions' in json_data:
                    actions = json_data['actions']
            
            # Look for inline action objects
            action_matches = re.findall(r'{.*?}', text)
            for match in action_matches:
                try:
                    action = json.loads(match)
                    if 'type' in action and 'path' in action:
                        actions.append(action)
                except:
                    continue
                    
        except Exception as e:
            print(f"Error parsing actions: {e}")
        
        return actions

    @staticmethod
    def extract_intent(text: str) -> Optional[str]:
        """Extract intent from response text"""
        intent_patterns = {
            'code_update': r'update|modify|change|improve|fix|code',
            'system_command': r'execute|run|start|stop|restart',
            'git_operation': r'commit|push|pull|sync|merge',
            'query': r'what|how|why|when|where|who|explain',
            'task': r'create|add|remove|delete|move'
        }
        
        lower_text = text.lower()
        for intent, pattern in intent_patterns.items():
            if re.search(pattern, lower_text):
                return intent
        return 'general'

class LLMAdapter:
    """Enhanced LLM Adapter with multi-model support and advanced features"""

    def __init__(self):
        self.models = {
            'primary': {
                'name': os.getenv('PRIMARY_MODEL', 'gpt-4'),
                'api_key': os.getenv('PRIMARY_API_KEY'),
                'endpoint': os.getenv('PRIMARY_ENDPOINT', 'https://api.openai.com/v1/chat/completions')
            },
            'backup': {
                'name': os.getenv('BACKUP_MODEL', 'llama-3.1-8b-instant'),
                'api_key': os.getenv('BACKUP_API_KEY'),
                'endpoint': os.getenv('BACKUP_ENDPOINT', 'https://api.groq.com/openai/v1/chat/completions')
            }
        }
        
        self.session = None
        self.parser = ResponseParser()
        self.max_retries = 3
        self.timeout = aiohttp.ClientTimeout(total=30)

    async def _ensure_session(self):
        """Ensure aiohttp session exists"""
        if not self.session:
            self.session = aiohttp.ClientSession(timeout=self.timeout)

    async def _call_api(self, model_config: Dict[str, str], messages: List[Dict[str, str]], 
                       temperature: float = 0.7, max_tokens: int = 2048) -> Dict[str, Any]:
        """Make API call to LLM provider"""
        await self._ensure_session()
        
        headers = {
            "Authorization": f"Bearer {model_config['api_key']}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "model": model_config['name'],
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens
        }
        
        async with self.session.post(model_config['endpoint'], json=payload, headers=headers) as response:
            if response.status != 200:
                raise Exception(f"API call failed: {await response.text()}")
            return await response.json()

    async def generate_response(self, text: str, context: str = None, mode: str = "chat",
                              capabilities: List[str] = None) -> Dict[str, Any]:
        """Generate enhanced response with context awareness and capabilities"""
        
        # Prepare system message with available capabilities
        system_msg = (
            "You are JARVIS (Just A Rather Very Intelligent System), an advanced AI assistant. "
            "You have access to the following capabilities:\n"
            f"- {', '.join(capabilities if capabilities else ['basic_chat'])}\n\n"
            "You can execute actions by including a JSON block in your response:\n"
            "```json\n"
            "{'actions': [\n"
            "  {'type': 'code_change', 'path': 'file.py', 'content': '...'},\n"
            "  {'type': 'system_command', 'command': '...'},\n"
            "  {'type': 'git_operation', 'action': 'push', 'message': '...'}\n"
            "]}\n"
            "```\n"
        )

        messages = [
            {"role": "system", "content": system_msg}
        ]
        
        # Add context if available
        if context:
            messages.append({
                "role": "system",
                "content": f"Previous context:\n{context}"
            })
        
        messages.append({"role": "user", "content": text})
        
        # Try primary model first
        try:
            start_time = datetime.utcnow()
            response = await self._call_api(self.models['primary'], messages)
            
            response_text = response['choices'][0]['message']['content']
            actions = self.parser.extract_actions(response_text)
            intent = self.parser.extract_intent(response_text)
            
            # Log successful API call
            db.save_system_event(
                event_type='llm_call',
                description='Primary model API call',
                status='success',
                details={
                    'model': self.models['primary']['name'],
                    'duration': (datetime.utcnow() - start_time).total_seconds(),
                    'tokens': response.get('usage', {}).get('total_tokens', 0)
                }
            )
            
            return {
                'text': response_text,
                'actions': actions,
                'intent': intent
            }
            
        except Exception as e:
            # Log error and try backup model
            db.save_system_event(
                event_type='llm_error',
                description=f'Primary model failed: {str(e)}',
                status='error'
            )
            
            try:
                # Fallback to backup model
                response = await self._call_api(self.models['backup'], messages)
                response_text = response['choices'][0]['message']['content']
                
                return {
                    'text': response_text,
                    'actions': self.parser.extract_actions(response_text),
                    'intent': self.parser.extract_intent(response_text)
                }
                
            except Exception as backup_error:
                db.save_system_event(
                    event_type='llm_error',
                    description=f'Backup model failed: {str(backup_error)}',
                    status='error'
                )
                raise Exception("Both primary and backup LLM models failed")

    def parse_actions_from_text(self, text: str):
        """
        Extract { "actions": [...] } blocks from model output text safely.
        Example:
        text = "Here’s the update {\"actions\": [{\"type\":\"write\",\"path\":\"file.py\"}]}"
        """
        actions = []
        try:
            matches = re.findall(r"\{[\s\S]*?\"actions\"[\s\S]*?\}", text)
            for m in matches:
                try:
                    obj = json.loads(m)
                    if isinstance(obj, dict) and "actions" in obj:
                        actions.extend(obj["actions"])
                except json.JSONDecodeError:
                    continue
        except Exception as e:
            print(f"[LLM] parse_actions_from_text error: {e}")
        return actions
