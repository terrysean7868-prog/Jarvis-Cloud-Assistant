# llm_adapter.py
import os
import re
import json
import asyncio
import aiohttp
import random
from datetime import datetime
from typing import Dict, List, Any, Optional
from dotenv import load_dotenv
from utils.db import db

load_dotenv()

try:
    from training_data import TRAINING_INTENTS, PERSONALITY_TRAITS
except ImportError:
    TRAINING_INTENTS = {}
    PERSONALITY_TRAITS = {"humor": 0.4, "formality": 0.7}

# Internet access for real-time data
try:
    from internet import get_internet, close_internet
    INTERNET_AVAILABLE = True
except ImportError:
    INTERNET_AVAILABLE = False
    print("⚠️ Internet module not available - install BeautifulSoup4 and aiohttp")

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
        """Extract intent from response text and training data"""
        # Check against training intents first
        if TRAINING_INTENTS:
            text_lower = text.lower()
            for intent_name, intent_data in TRAINING_INTENTS.items():
                examples = intent_data.get("examples", [])
                for example in examples:
                    if example.lower() in text_lower:
                        return intent_name
        
        # Fallback to pattern matching
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

    @staticmethod
    def get_training_response(intent: str) -> Optional[str]:
        """Get a response from training data for a detected intent"""
        if not TRAINING_INTENTS or intent not in TRAINING_INTENTS:
            return None
        
        responses = TRAINING_INTENTS[intent].get("responses", [])
        if responses:
            return random.choice(responses)
        return None

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
        
        # Quick intent detection and training response for speed
        intent = self.parser.extract_intent(text)
        training_response = self.parser.get_training_response(intent)
        
        # Use training response for simple intents (60% faster)
        if training_response and random.random() < 0.7:  # Increased from 0.6
            actions = self.parser.extract_actions(text)
            return {
                'text': training_response,
                'actions': actions,
                'intent': intent,
                'source': 'training_data',
                'latency': 'fast'
            }
        
        # Prepare system message with available capabilities
        system_msg = (
            "You are JARVIS (Just A Rather Very Intelligent System), an advanced AI assistant. "
            "You have access to the following capabilities:\n"
            f"- {', '.join(capabilities if capabilities else ['basic_chat'])}\n\n"
            "Keep responses concise (1-2 sentences max). "
            "You can execute actions by including a JSON block in your response:\n"
            "```json\n"
            "{'actions': [\n"
            "  {'type': 'open_url', 'url_name': 'youtube'},\n"
            "  {'type': 'search', 'query': 'search term'}\n"
            "]}\n"
            "```\n"
        )

        messages = [
            {"role": "system", "content": system_msg}
        ]
        
        # Add context if available (but keep it minimal for speed)
        if context:
            messages.append({
                "role": "system",
                "content": f"Context: {context[:200]}"  # Limit context to 200 chars
            })
        
        messages.append({"role": "user", "content": text})
        
        # Try primary model with timeout for faster responses
        try:
            start_time = datetime.utcnow()
            # Use shorter max_tokens for faster generation (reduced from 4096 to 256)
            response = await self._call_api(
                self.models['primary'],
                messages,
                max_tokens=256  # Limit to 1-2 sentence responses
            )
            
            response_text = response['choices'][0]['message']['content']
            actions = self.parser.extract_actions(response_text)
            latency = (datetime.utcnow() - start_time).total_seconds()
            
            return {
                'text': response_text,
                'actions': actions,
                'intent': intent,
                'source': 'llm_primary',
                'latency': f"{latency:.2f}s"
            }
            
        except Exception as e:
            # Fallback to training response on LLM failure
            print(f"LLM error (using fallback): {e}")
            fallback_response = "I'm processing that. One moment please."
            return {
                'text': fallback_response,
                'actions': [],
                'intent': intent,
                'source': 'fallback',
                'latency': 'instant'
            }

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
        text = "Here's the update {\"actions\": [{\"type\":\"write\",\"path\":\"file.py\"}]}"
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
    
    async def enhance_with_internet_data(self, text: str) -> str:
        """
        Enhance response with real-time internet data
        
        Args:
            text: User query
            
        Returns:
            Enhanced response with internet data if available
        """
        if not INTERNET_AVAILABLE:
            return text
        
        try:
            # Detect if query requires internet data
            internet_keywords = [
                'what is', 'who is', 'when was', 'where is', 'latest',
                'current', 'today', 'news', 'weather', 'stock', 'price',
                'search', 'find', 'research', 'how to', 'tutorial'
            ]
            
            query_lower = text.lower()
            needs_internet = any(keyword in query_lower for keyword in internet_keywords)
            
            if not needs_internet:
                return text
            
            # Get internet data
            print(f"🌐 Fetching internet data for: {text}")
            internet = await get_internet()
            
            # Perform web search and summarization
            results = await internet.search_and_summarize(text, num_results=2)
            
            if not results:
                return text
            
            # Build enhanced context
            enhanced_text = f"{text}\n\n[Web Search Results]:\n"
            for i, result in enumerate(results, 1):
                enhanced_text += f"{i}. {result.get('title', 'No title')}\n"
                snippet = result.get('content_summary') or result.get('snippet', '')
                enhanced_text += f"   {snippet[:200]}...\n"
            
            return enhanced_text
            
        except Exception as e:
            print(f"⚠️ Internet enhancement failed: {str(e)}")
            return text
    
    async def search_and_answer(self, question: str) -> Dict[str, Any]:
        """
        Search the web and generate an answer
        
        Args:
            question: Question to answer
            
        Returns:
            Dictionary with answer and sources
        """
        if not INTERNET_AVAILABLE:
            return {
                'text': 'Internet access not available',
                'sources': [],
                'source': 'none'
            }
        
        try:
            print(f"🔍 Searching for: {question}")
            internet = await get_internet()
            
            # Get answer from web
            answer = await internet.answer_question(question)
            
            # Get sources
            sources = await internet.search(question, num_results=3)
            
            return {
                'text': answer or 'Could not find answer',
                'sources': sources,
                'source': 'internet_search'
            }
            
        except Exception as e:
            print(f"❌ Search and answer failed: {str(e)}")
            return {
                'text': f'Error searching: {str(e)}',
                'sources': [],
                'source': 'error'
            }
