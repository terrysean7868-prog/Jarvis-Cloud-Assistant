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
    
    # Database settings (defaults to local if not configured)
    MONGODB_URI = env.get_str('MONGODB_URI', 'mongodb://localhost:27017/jarvis')
    MONGODB_DB = env.get_str('MONGODB_DB_NAME', 'jarvis_db')
    SQLITE_PATH = DATA_DIR / 'jarvis_memory.db'
    
    # LLM Configuration
    # Primary: OpenAI (ChatGPT). Fallback: Groq.
    _PRIMARY_DEFAULT_ENDPOINT = 'https://api.openai.com/v1/chat/completions'
    _PRIMARY_DEFAULT_MODEL = 'gpt-4o'
    _PRIMARY_API_KEY = llm_secrets().primary_api_key
    LLM_CONFIG = {
        'primary': {
            'name': env.get_str('PRIMARY_MODEL', _PRIMARY_DEFAULT_MODEL),
            'api_key': _PRIMARY_API_KEY,
            'endpoint': env.get_str('PRIMARY_ENDPOINT', _PRIMARY_DEFAULT_ENDPOINT)
        },
        'backup': {
            'name': env.get_str('BACKUP_MODEL', 'llama-3.1-8b-instant'),
            'api_key': llm_secrets().backup_api_key,
            'endpoint': env.get_str('BACKUP_ENDPOINT', 'https://api.groq.com/openai/v1/chat/completions')
        }
    }
    
    # Voice Recognition Settings
    VOICE_CONFIG = {
        'offline_model': env.get_str('VOSK_MODEL', 'vosk-model-small-en-us'),
        'language': env.get_str('VOICE_LANGUAGE', 'en-US'),
        'use_vad': True,
        'vad_sensitivity': env.get_float('VAD_SENSITIVITY', 3.0),
        'silence_duration': env.get_float('SILENCE_DURATION', 0.5)
    }
    
    # Git Configuration
    GIT_CONFIG = {
        'repository': env.get('GITHUB_REPO'),
        'username': env.get('GITHUB_USERNAME'),
        'token': env.get('GITHUB_TOKEN'),
        'ssh_key': env.get('SSH_KEY'),
        'branch': env.get_str('GIT_BRANCH', 'main'),
        'auto_sync': env.get_bool('GIT_AUTO_SYNC', True)
    }
    
    # System Settings
    SYSTEM_CONFIG = {
        'debug_mode': env.get_bool('DEBUG', False),
        'log_level': env.get_str('LOG_LEVEL', 'INFO'),
        'auto_update': env.get_bool('AUTO_UPDATE', True),
        'max_retries': env.get_int('MAX_RETRIES', 3),
        'timeout': env.get_int('TIMEOUT', 30)
    }
    
    # Frontend Configuration
    FRONTEND_CONFIG = {
        'port': env.get_int('PORT', 3000),
        'host': env.get_str('HOST', 'localhost'),
        'api_base_url': env.get_str('API_BASE_URL', '/api'),
        'ws_base_url': env.get_str('WS_BASE_URL', '/ws')
    }
    
    # Security Configuration
    SECURITY_CONFIG = {
        'allowed_origins': env.get_str('ALLOWED_ORIGINS', '*').split(','),
        'jwt_secret': env.get_str('JWT_SECRET', 'your-secret-key'),
        'jwt_algorithm': env.get_str('JWT_ALGORITHM', 'HS256'),
        'token_expire_minutes': env.get_int('TOKEN_EXPIRE_MINUTES', 1440)
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
    
    ALLOWED_PATHS = [p.strip() for p in env.get_str('ALLOWED_PATHS', ','.join(DEFAULT_ALLOWED_PATHS)).split(',')]
    
    @classmethod
    def validate(cls, strict=False) -> bool:
        """Validate configuration settings. In dev mode, missing optional fields only warn.
        
        Args:
            strict (bool): If True, raise error on missing critical fields.
                          If False (default), only warn in debug mode.
        """
        required_settings = [
            ('MONGODB_URI', cls.MONGODB_URI),
            ('LLM_API_KEY', cls.LLM_CONFIG['primary']['api_key']),
            ('GITHUB_REPO', cls.GIT_CONFIG['repository'])
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