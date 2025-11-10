# config.py
import os
from pathlib import Path
from typing import Dict, Any
from dotenv import load_dotenv

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
    
    # Database settings
    MONGODB_URI = os.getenv('MONGODB_URI')
    MONGODB_DB = os.getenv('MONGODB_DB_NAME', 'jarvis_db')
    SQLITE_PATH = DATA_DIR / 'jarvis_memory.db'
    
    # LLM Configuration
    LLM_CONFIG = {
        'primary': {
            'name': os.getenv('PRIMARY_MODEL', 'gpt-4'),
            'api_key': os.getenv('PRIMARY_API_KEY'),
            'endpoint': os.getenv('PRIMARY_ENDPOINT', 'https://api.openai.com/v1/chat/completions')
        },
        'backup': {
            'name': os.getenv('BACKUP_MODEL', 'llama-3.1-8b-instant'),
            'api_key': os.getenv('BACKUP_API_KEY', os.getenv('GROQ_API_KEY')),
            'endpoint': os.getenv('BACKUP_ENDPOINT', 'https://api.groq.com/openai/v1/chat/completions')
        }
    }
    
    # Voice Recognition Settings
    VOICE_CONFIG = {
        'offline_model': os.getenv('VOSK_MODEL', 'vosk-model-small-en-us'),
        'language': os.getenv('VOICE_LANGUAGE', 'en-US'),
        'use_vad': True,
        'vad_sensitivity': float(os.getenv('VAD_SENSITIVITY', '3')),
        'silence_duration': float(os.getenv('SILENCE_DURATION', '0.5'))
    }
    
    # Git Configuration
    GIT_CONFIG = {
        'repository': os.getenv('GITHUB_REPO'),
        'username': os.getenv('GITHUB_USERNAME'),
        'token': os.getenv('GITHUB_TOKEN'),
        'ssh_key': os.getenv('SSH_KEY'),
        'branch': os.getenv('GIT_BRANCH', 'main'),
        'auto_sync': os.getenv('GIT_AUTO_SYNC', 'true').lower() == 'true'
    }
    
    # System Settings
    SYSTEM_CONFIG = {
        'debug_mode': os.getenv('DEBUG', 'false').lower() == 'true',
        'log_level': os.getenv('LOG_LEVEL', 'INFO'),
        'auto_update': os.getenv('AUTO_UPDATE', 'true').lower() == 'true',
        'max_retries': int(os.getenv('MAX_RETRIES', '3')),
        'timeout': int(os.getenv('TIMEOUT', '30'))
    }
    
    # Frontend Configuration
    FRONTEND_CONFIG = {
        'port': int(os.getenv('PORT', '3000')),
        'host': os.getenv('HOST', 'localhost'),
        'api_base_url': os.getenv('API_BASE_URL', '/api'),
        'ws_base_url': os.getenv('WS_BASE_URL', '/ws')
    }
    
    # Security Configuration
    SECURITY_CONFIG = {
        'allowed_origins': os.getenv('ALLOWED_ORIGINS', '*').split(','),
        'jwt_secret': os.getenv('JWT_SECRET', 'your-secret-key'),
        'jwt_algorithm': os.getenv('JWT_ALGORITHM', 'HS256'),
        'token_expire_minutes': int(os.getenv('TOKEN_EXPIRE_MINUTES', '1440'))
    }
    
    # Module Configurations
    DEFAULT_ALLOWED_PATHS = [
        'modules',
        'utils',
        'jarvis-frontend/src',
        'app.py',
        'jarvis_brain.py',
        'llm_adapter.py',
        'executor.py',
        'git_sync.py',
        'run_jarvis.py',
        'config.py',
        'requirements.txt',
        'README.md'
    ]
    
    ALLOWED_PATHS = [p.strip() for p in os.getenv('ALLOWED_PATHS', ','.join(DEFAULT_ALLOWED_PATHS)).split(',')]
    
    @classmethod
    def validate(cls) -> bool:
        """Validate required configuration settings"""
        required_settings = [
            ('MONGODB_URI', cls.MONGODB_URI),
            ('PRIMARY_API_KEY', cls.LLM_CONFIG['primary']['api_key']),
            ('GITHUB_REPO', cls.GIT_CONFIG['repository'])
        ]
        
        missing = [key for key, value in required_settings if not value]
        
        if missing:
            raise ValueError(f"Missing required configuration: {', '.join(missing)}")
        
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