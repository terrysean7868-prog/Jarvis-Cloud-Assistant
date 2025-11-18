# src/utils/voice_auth.py
"""
Voice-based authentication system for Jarvis
Uses voice recognition to authenticate users
"""
import os
import hashlib
import json
import logging
from pathlib import Path
from typing import Dict, Optional, Tuple
from datetime import datetime, timedelta

logger = logging.getLogger("jarvis.voice_auth")

# Voice authentication storage
AUTH_FILE = Path(__file__).parent.parent.parent / "data" / "voice_auth.json"
AUTH_FILE.parent.mkdir(exist_ok=True)

# Session management
SESSION_DURATION = timedelta(hours=24)  # 24 hour sessions


class VoiceAuth:
    """Voice-based authentication manager"""
    
    def __init__(self):
        self.auth_data = self._load_auth_data()
        self.active_sessions = {}  # session_id -> {user, expires_at, voice_hash}
    
    def _load_auth_data(self) -> Dict:
        """Load authentication data from file"""
        if AUTH_FILE.exists():
            try:
                with open(AUTH_FILE, 'r') as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Failed to load auth data: {e}")
        return {"users": {}, "voice_samples": {}}
    
    def _save_auth_data(self):
        """Save authentication data to file"""
        try:
            with open(AUTH_FILE, 'w') as f:
                json.dump(self.auth_data, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save auth data: {e}")
    
    def create_voice_hash(self, audio_data: bytes) -> str:
        """Create hash from voice audio data"""
        return hashlib.sha256(audio_data).hexdigest()
    
    def register_user(self, username: str, voice_sample_hash: str, password: Optional[str] = None) -> Dict:
        """
        Register a new user with voice authentication
        
        Args:
            username: Username
            voice_sample_hash: Hash of voice sample
            password: Optional backup password
        """
        if username in self.auth_data["users"]:
            return {
                "status": "error",
                "message": "User already exists"
            }
        
        self.auth_data["users"][username] = {
            "voice_hash": voice_sample_hash,
            "password_hash": hashlib.sha256(password.encode()).hexdigest() if password else None,
            "created_at": datetime.now().isoformat(),
            "last_login": None
        }
        
        self._save_auth_data()
        
        return {
            "status": "success",
            "message": f"User {username} registered successfully"
        }
    
    def authenticate_by_voice(self, username: str, voice_sample_hash: str) -> Tuple[bool, Optional[str]]:
        """
        Authenticate user by voice
        
        Returns:
            (is_authenticated, session_id or error_message)
        """
        if username not in self.auth_data["users"]:
            return False, "User not found"
        
        stored_hash = self.auth_data["users"][username].get("voice_hash")
        
        # Simple hash comparison (in production, use more sophisticated voice matching)
        # For now, we'll use a similarity threshold
        if stored_hash and self._compare_voice_hashes(stored_hash, voice_sample_hash):
            # Create session
            session_id = self._create_session(username)
            self.auth_data["users"][username]["last_login"] = datetime.now().isoformat()
            self._save_auth_data()
            
            return True, session_id
        
        return False, "Voice authentication failed"
    
    def _compare_voice_hashes(self, stored_hash: str, provided_hash: str, threshold: float = 0.8) -> bool:
        """
        Compare voice hashes with similarity threshold
        In production, use proper voice recognition/verification
        """
        # For now, simple exact match (in production, use voice verification API)
        # This is a placeholder - real implementation would use voice biometrics
        return stored_hash == provided_hash
    
    def _create_session(self, username: str) -> str:
        """Create authentication session"""
        import secrets
        session_id = secrets.token_urlsafe(32)
        expires_at = datetime.now() + SESSION_DURATION
        
        self.active_sessions[session_id] = {
            "username": username,
            "expires_at": expires_at.isoformat(),
            "created_at": datetime.now().isoformat()
        }
        
        return session_id
    
    def validate_session(self, session_id: str) -> Tuple[bool, Optional[str]]:
        """
        Validate session token
        
        Returns:
            (is_valid, username or None)
        """
        if session_id not in self.active_sessions:
            return False, None
        
        session = self.active_sessions[session_id]
        expires_at = datetime.fromisoformat(session["expires_at"])
        
        if datetime.now() > expires_at:
            # Session expired
            del self.active_sessions[session_id]
            return False, None
        
        return True, session["username"]
    
    def logout(self, session_id: str) -> bool:
        """Logout and invalidate session"""
        if session_id in self.active_sessions:
            del self.active_sessions[session_id]
            return True
        return False
    
    def get_user_info(self, username: str) -> Optional[Dict]:
        """Get user information"""
        return self.auth_data["users"].get(username)
    
    def update_voice_sample(self, username: str, voice_sample_hash: str) -> bool:
        """Update user's voice sample"""
        if username not in self.auth_data["users"]:
            return False
        
        self.auth_data["users"][username]["voice_hash"] = voice_sample_hash
        self._save_auth_data()
        return True
    
    def cleanup_expired_sessions(self):
        """Remove expired sessions"""
        now = datetime.now()
        expired = [
            sid for sid, session in self.active_sessions.items()
            if datetime.fromisoformat(session["expires_at"]) < now
        ]
        for sid in expired:
            del self.active_sessions[sid]


# Global instance
voice_auth = VoiceAuth()

# Cleanup expired sessions periodically
import atexit
atexit.register(voice_auth.cleanup_expired_sessions)

