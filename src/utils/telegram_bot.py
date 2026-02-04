"""
Telegram Bot Integration for JARVIS
Handles voice registration, login, and chat interactions via Telegram
"""

import os
import logging
from typing import Optional, Dict, Any
from datetime import datetime, timedelta
from dotenv import load_dotenv

from src.config.secrets import telegram_secrets
import json
from pathlib import Path

load_dotenv()
logger = logging.getLogger(__name__)

# Telegram Bot Configuration
_tg = telegram_secrets()
TELEGRAM_TOKEN = _tg.token
TELEGRAM_CHAT_ID = _tg.chat_id

# Session storage for Telegram users
TELEGRAM_SESSIONS_FILE = Path("data/telegram_sessions.json")
TELEGRAM_SESSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)


class TelegramBotManager:
    """Manages Telegram bot interactions with authentication and session management"""
    
    def __init__(self):
        self.token = TELEGRAM_TOKEN
        self.chat_id = TELEGRAM_CHAT_ID
        self.sessions = self._load_sessions()
        self.pending_voice = {}  # user_id -> awaiting_voice_sample
        self.pending_registrations = {}  # user_id -> {username, password, action}
    
    def _load_sessions(self) -> Dict:
        """Load existing Telegram user sessions from file"""
        if TELEGRAM_SESSIONS_FILE.exists():
            try:
                with open(TELEGRAM_SESSIONS_FILE, 'r') as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"Failed to load Telegram sessions: {e}")
        return {}
    
    def _save_sessions(self):
        """Persist Telegram user sessions to file"""
        try:
            with open(TELEGRAM_SESSIONS_FILE, 'w') as f:
                json.dump(self.sessions, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save Telegram sessions: {e}")
    
    def start_registration(self, user_id: str, username: str) -> Dict[str, Any]:
        """Start voice registration process from Telegram"""
        self.pending_registrations[user_id] = {
            "username": username,
            "action": "register",
            "started_at": datetime.utcnow().isoformat()
        }
        return {
            "status": "awaiting_voice",
            "message": f"Please send a voice message for registration of user '{username}'",
            "next_step": "send_voice_sample"
        }
    
    def process_voice_sample(self, user_id: str, voice_file_id: str, voice_bytes: bytes) -> Dict[str, Any]:
        """Process voice sample from Telegram for registration/login"""
        if user_id not in self.pending_registrations:
            return {
                "status": "error",
                "message": "No registration in progress. Use /register <username> first"
            }
        
        reg_data = self.pending_registrations[user_id]
        
        # Convert voice bytes to hash (in production, use proper voice matching)
        import hashlib
        voice_hash = hashlib.sha256(voice_bytes).hexdigest()
        
        return {
            "status": "voice_received",
            "message": "Voice sample received. Proceed with password setup.",
            "voice_sample_hash": voice_hash,
            "next_step": "confirm_password"
        }
    
    def complete_registration(self, user_id: str, voice_hash: str, password: str, 
                             username: str, role: str = "user") -> Dict[str, Any]:
        """Complete registration with voice and password"""
        if user_id not in self.pending_registrations:
            return {"status": "error", "message": "Invalid registration session"}
        
        # Create session for Telegram user
        session_token = self._create_session_token()
        
        self.sessions[user_id] = {
            "username": username,
            "role": role,
            "session_id": session_token,
            "registered_at": datetime.utcnow().isoformat(),
            "last_activity": datetime.utcnow().isoformat(),
            "expires_at": (datetime.utcnow() + timedelta(days=30)).isoformat()
        }
        
        self._save_sessions()
        del self.pending_registrations[user_id]
        
        return {
            "status": "registered",
            "message": f"User '{username}' registered successfully!",
            "session_id": session_token,
            "username": username
        }
    
    def telegram_login(self, user_id: str, username: str, voice_hash: str) -> Dict[str, Any]:
        """Handle Telegram user login with voice"""
        # Validate voice sample (in production, compare with stored sample)
        session_token = self._create_session_token()
        
        self.sessions[user_id] = {
            "username": username,
            "session_id": session_token,
            "logged_in_at": datetime.utcnow().isoformat(),
            "last_activity": datetime.utcnow().isoformat(),
            "expires_at": (datetime.utcnow() + timedelta(days=1)).isoformat()  # Shorter expiry for login
        }
        
        self._save_sessions()
        
        return {
            "status": "logged_in",
            "message": f"Welcome back, {username}!",
            "session_id": session_token,
            "username": username
        }
    
    def validate_telegram_session(self, user_id: str) -> tuple[bool, Optional[str]]:
        """Check if Telegram user has valid session"""
        if user_id not in self.sessions:
            return False, None
        
        session = self.sessions[user_id]
        expires_at = datetime.fromisoformat(session.get("expires_at", ""))
        
        if datetime.utcnow() > expires_at:
            # Session expired
            del self.sessions[user_id]
            self._save_sessions()
            return False, None
        
        # Update last activity
        session["last_activity"] = datetime.utcnow().isoformat()
        self._save_sessions()
        
        return True, session.get("username")
    
    def logout_telegram_user(self, user_id: str) -> bool:
        """Logout a Telegram user and invalidate session"""
        if user_id in self.sessions:
            del self.sessions[user_id]
            self._save_sessions()
            return True
        return False
    
    def get_user_info(self, user_id: str) -> Optional[Dict[str, Any]]:
        """Get Telegram user information if authenticated"""
        is_valid, username = self.validate_telegram_session(user_id)
        if is_valid:
            return {
                "user_id": user_id,
                "username": username,
                "session_info": self.sessions.get(user_id)
            }
        return None
    
    def _create_session_token(self) -> str:
        """Generate a secure session token"""
        import secrets
        return secrets.token_urlsafe(32)
    
    def cleanup_expired_sessions(self):
        """Remove expired sessions"""
        current_time = datetime.utcnow()
        expired_users = []
        
        for user_id, session in self.sessions.items():
            expires_at = datetime.fromisoformat(session.get("expires_at", ""))
            if current_time > expires_at:
                expired_users.append(user_id)
        
        for user_id in expired_users:
            del self.sessions[user_id]
        
        if expired_users:
            self._save_sessions()
            logger.info(f"Cleaned up {len(expired_users)} expired Telegram sessions")


# Global instance
telegram_bot = TelegramBotManager()
