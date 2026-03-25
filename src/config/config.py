# config.py
from pathlib import Path
from typing import Dict, Any
from dotenv import load_dotenv

from src.config import env
from src.config.secrets import llm_secrets

load_dotenv()

class Config:
    """Central configuration management for Jarvis"""
    
    # Base paths
    ROOT_DIR = Path(__file__).parent
    MODULES_DIR = ROOT_DIR / 'modules'
    DATA_DIR = ROOT_DIR / 'data'
    MODELS_DIR = ROOT_DIR / 'models'
    
    # Ensure directories exist
    for dir_path in [DATA_DIR, MODELS_DIR]:
        dir_path.mkdir(exist_ok=True)
    
    # Database settings (whitelist env)
    MONGODB_URI = env.get_str('MONGODB_URI', 'mongodb://localhost:27017/jarvis')
    MONGODB_DB = env.get_str('MONGODB_DB_NAME', 'jarvis_db')
    USE_DATABASE_FOR_TRAINING = True
    SQLITE_PATH = DATA_DIR / 'jarvis_memory.db'
    
    # LLM Configuration
    # Primary: OpenAI (ChatGPT). Fallback: Groq.
    _PRIMARY_DEFAULT_ENDPOINT = 'https://api.openai.com/v1/chat/completions'
    _PRIMARY_DEFAULT_MODEL = 'gpt-4o'
    _PRIMARY_API_KEY = llm_secrets().primary_api_key
    LLM_CONFIG = {
        'primary': {
            'name': _PRIMARY_DEFAULT_MODEL,
            'api_key': _PRIMARY_API_KEY,
            'endpoint': _PRIMARY_DEFAULT_ENDPOINT,
        },
        'backup': {
            'name': 'llama-3.1-8b-instant',
            'api_key': llm_secrets().backup_api_key,
            'endpoint': 'https://api.groq.com/openai/v1/chat/completions',
        }
    }
    
    # Voice Recognition Settings
    VOICE_CONFIG = {
        'offline_model': 'vosk-model-small-en-us',
        'language': 'en-US',
        'use_vad': True,
        'vad_sensitivity': 3.0,
        'silence_duration': 0.5,
        'voice_max_samples': env.get_int('VOICE_MAX_SAMPLES', 5),
        'voice_text_similarity_threshold': env.get_float('VOICE_TEXT_SIMILARITY_THRESHOLD', 0.85),
    }
    
    # Git Configuration
    GIT_CONFIG = {
        'repository': '',
        'username': '',
        'token': '',
        'ssh_key': '',
        'branch': 'main',
        'auto_sync': True,
    }
    
    # System Settings
    SYSTEM_CONFIG = {
        'debug_mode': False,
        'log_level': 'INFO',
        'auto_update': True,
        'max_retries': 2,
        'timeout': 10,
    }
    
    # Frontend Configuration
    FRONTEND_CONFIG = {
        'port': 3000,
        'host': 'localhost',
        'api_base_url': '/api',
        'ws_base_url': '/ws',
    }
    
    # Security Configuration
    SECURITY_CONFIG = {
        'allowed_origins': ['*'],
        'jwt_secret': env.get_str('JARVIS_JWT_SECRET', ''),
        'jwt_issuer': env.get_str('JARVIS_JWT_ISSUER', 'jarvis'),
        'jwt_algorithm': 'HS256',
        'token_expire_minutes': 1440,
    }
    
    # Module Configurations
    DEFAULT_ALLOWED_PATHS = [
        'modules',
        'utils',
        'frontend/src',
        'app.py',
        'jarvis_brain.py',
        'llm_adapter.py',
        'executor.py',
        'git_sync.py',
        'run_jarvis.py',
        'config.py',
        'requirements.txt',
        'requirements',
        'README.md'
    ]
    
    ALLOWED_PATHS = [
        p.strip()
        for p in env.get_str('JARVIS_ALLOWED_PATHS', ','.join(DEFAULT_ALLOWED_PATHS)).split(',')
        if p.strip()
    ]
    
    @classmethod
    def validate(cls, strict=False) -> bool:
        """Validate configuration settings. In dev mode, missing optional fields only warn.
        
        Args:
            strict (bool): If True, raise error on missing critical fields.
                          If False (default), only warn in debug mode.
        """
        required_settings = [
            ('MONGODB_URI', cls.MONGODB_URI),
            ('LLM_API_KEY', cls.LLM_CONFIG['primary']['api_key'] or cls.LLM_CONFIG['backup']['api_key']),
        ]
        
        missing = [key for key, value in required_settings if not value]
        
        if missing:
            msg = f"Missing configuration: {', '.join(missing)}"
            if cls.SYSTEM_CONFIG['debug_mode']:
                print(f"[WARN] {msg}")
            if strict:
                raise ValueError(msg)
        
        return True
    
    @classmethod
    def get_all(cls) -> Dict[str, Any]:
        """Get all configuration settings as a dictionary"""
        return {
            'database': {
                'mongodb_uri': cls.MONGODB_URI,
                'mongodb_db': cls.MONGODB_DB,
                'sqlite_path': str(cls.SQLITE_PATH)
            },
            'llm': cls.LLM_CONFIG,
            'voice': cls.VOICE_CONFIG,
            'git': cls.GIT_CONFIG,
            'system': cls.SYSTEM_CONFIG,
            'frontend': cls.FRONTEND_CONFIG,
            'security': cls.SECURITY_CONFIG,
            'paths': {
                'root': str(cls.ROOT_DIR),
                'modules': str(cls.MODULES_DIR),
                'data': str(cls.DATA_DIR),
                'models': str(cls.MODELS_DIR)
            }
        }