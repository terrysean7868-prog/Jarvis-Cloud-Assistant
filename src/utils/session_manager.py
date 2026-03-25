"""
Session Management and Persistence
Handles user sessions with browser storage, auto-reconnect on reload, and expiry management
"""

import os
import json
import os
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, Any, Tuple

logger = logging.getLogger(__name__)

# Session storage paths
SESSIONS_DIR = Path("data/sessions")
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
ACTIVE_SESSIONS_FILE = SESSIONS_DIR / "active_sessions.json"


class SessionManager:
    """Manages user sessions across web and Telegram with persistence"""
    
    def __init__(self):
        self.sessions = self._load_sessions()
        self.session_timeout = 86400  # 24 hours for web sessions
        self.telegram_timeout = 2592000  # 30 days for Telegram
    
    def _load_sessions(self) -> Dict[str, Dict[str, Any]]:
        """Load active sessions from disk"""
        if ACTIVE_SESSIONS_FILE.exists():
            try:
                with open(ACTIVE_SESSIONS_FILE, 'r') as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"Failed to load sessions: {e}")
        return {}
    
    def _save_sessions(self):
        """Persist sessions to disk"""
        try:
            with open(ACTIVE_SESSIONS_FILE, 'w') as f:
                json.dump(self.sessions, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save sessions: {e}")
    
    def create_session(self, user_id: str, username: str, platform: str = "web",
                      role: str = "user", **extra_data) -> Tuple[str, Dict]:
        """
        Create a new session
        platform: "web" or "telegram"
        Returns: (session_id, session_data)
        """
        import secrets
        session_id = secrets.token_urlsafe(32)
        
        timeout = self.telegram_timeout if platform == "telegram" else self.session_timeout
        expires_at = (datetime.utcnow() + timedelta(seconds=timeout)).isoformat()
        
        session_data = {
            "session_id": session_id,
            "user_id": user_id,
            "username": username,
            "platform": platform,
            "role": role,
            "created_at": datetime.utcnow().isoformat(),
            "last_activity": datetime.utcnow().isoformat(),
            "expires_at": expires_at,
            "ip_address": extra_data.get("ip_address"),
            "user_agent": extra_data.get("user_agent"),
            **extra_data
        }
        
        self.sessions[session_id] = session_data
        self._save_sessions()
        
        return session_id, session_data
    
    def validate_session(self, session_id: str, update_activity: bool = True) -> Tuple[bool, Optional[str]]:
        """
        Validate a session and optionally update its last activity time
        Returns: (is_valid, username)
        """
        if session_id not in self.sessions:
            return False, None
        
        session = self.sessions[session_id]
        expires_at = datetime.fromisoformat(session.get("expires_at", ""))
        
        # Check if expired
        if datetime.utcnow() > expires_at:
            del self.sessions[session_id]
            self._save_sessions()
            return False, None
        
        # Update activity timestamp
        if update_activity:
            session["last_activity"] = datetime.utcnow().isoformat()
            self._save_sessions()
        
        return True, session.get("username")
    
    def get_session_info(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Get full session information if valid"""
        is_valid, username = self.validate_session(session_id, update_activity=False)
        if is_valid and session_id in self.sessions:
            return self.sessions[session_id].copy()
        return None
    
    def extend_session(self, session_id: str, additional_hours: int = 24) -> bool:
        """Extend session expiry time on reload"""
        if session_id not in self.sessions:
            return False
        
        session = self.sessions[session_id]
        timeout = self.telegram_timeout if session.get("platform") == "telegram" else self.session_timeout
        
        # Extend by the specified hours or use default timeout
        new_expires = (datetime.utcnow() + timedelta(seconds=timeout)).isoformat()
        session["expires_at"] = new_expires
        session["last_activity"] = datetime.utcnow().isoformat()
        
        self._save_sessions()
        return True
    
    def invalidate_session(self, session_id: str) -> bool:
        """Logout and remove a session"""
        if session_id in self.sessions:
            del self.sessions[session_id]
            self._save_sessions()
            return True
        return False
    
    def get_user_sessions(self, username: str, platform: Optional[str] = None) -> list:
        """Get all active sessions for a user"""
        user_sessions = []
        for session_id, session_data in self.sessions.items():
            if session_data.get("username") == username:
                if platform is None or session_data.get("platform") == platform:
                    user_sessions.append(session_data)
        return user_sessions
    
    def cleanup_expired_sessions(self) -> int:
        """Remove all expired sessions and return count"""
        current_time = datetime.utcnow()
        expired_sessions = []
        
        for session_id, session_data in self.sessions.items():
            expires_at = datetime.fromisoformat(session_data.get("expires_at", ""))
            if current_time > expires_at:
                expired_sessions.append(session_id)
        
        for session_id in expired_sessions:
            del self.sessions[session_id]
        
        if expired_sessions:
            self._save_sessions()
            logger.info(f"Cleaned up {len(expired_sessions)} expired sessions")
        
        return len(expired_sessions)
    
    def get_session_stats(self) -> Dict[str, Any]:
        """Get session statistics"""
        web_sessions = sum(1 for s in self.sessions.values() if s.get("platform") == "web")
        telegram_sessions = sum(1 for s in self.sessions.values() if s.get("platform") == "telegram")
        
        return {
            "total_sessions": len(self.sessions),
            "web_sessions": web_sessions,
            "telegram_sessions": telegram_sessions,
            "active_users": len(set(s.get("username") for s in self.sessions.values())),
            "last_cleanup": datetime.utcnow().isoformat()
        }


# Global instance
session_manager = SessionManager()


# Background task to cleanup expired sessions periodically
_cleanup_scheduler = None

def start_session_cleanup_task():
    """Start background task to clean expired sessions"""
    global _cleanup_scheduler

    # Desktop optimization: allow disabling this background scheduler.
    # Default is enabled for server deployments.
    flag = "true"
    if flag in {"0", "false", "no", "off"}:
        return
    
    if _cleanup_scheduler is not None:
        return  # Already running
    
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        
        _cleanup_scheduler = BackgroundScheduler()
        _cleanup_scheduler.add_job(session_manager.cleanup_expired_sessions, 'interval', hours=1, id='cleanup_sessions')
        _cleanup_scheduler.start()
        logger.info("Session cleanup task started (runs every 1 hour)")
    except Exception as e:
        logger.error(f"Failed to start session cleanup: {e}")
