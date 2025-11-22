
# src/utils/voice_auth.py
"""
Robust voice-based authentication system for Jarvis.

Provides:
- register_user(username, voice_sample_hash, password=None, role='user')
- authenticate_by_voice(username, voice_sample_hash, password=None) -> (True, session_id) or (False, error)
- validate_session(session_id) -> (is_valid, username_or_none)
- logout(session_id) -> bool
- is_admin(username) -> bool

Storage: a JSON file at data/auth_users.json (project-local). Sessions kept in-memory with safe random tokens.
This implementation is intentionally simple and file-based for portability. For production,
replace voice hash comparison with real voice biometrics / external service.
"""

import os
import json
import hashlib
import secrets
import logging
from pathlib import Path
from datetime import datetime, timedelta
from typing import Tuple, Optional

logger = logging.getLogger("jarvis.voice_auth")
logger.setLevel(logging.INFO)

# Configuration
PROJECT_ROOT = Path(__file__).resolve().parents[2]  # go up to project root
AUTH_FILE = PROJECT_ROOT / "data" / "auth_users.json"
SESSION_DURATION = timedelta(hours=8)  # sessions valid for 8 hours

def _ensure_auth_file():
    if not AUTH_FILE.parent.exists():
        AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not AUTH_FILE.exists():
        AUTH_FILE.write_text(json.dumps({"users":{}}, indent=2))

def _hash_password(password: str, salt: Optional[str] = None) -> Tuple[str,str]:
    """Return (salt, hashed). If salt not provided, generate one."""
    if salt is None:
        salt = secrets.token_hex(8)
    hashed = hashlib.sha256((salt + password).encode("utf-8")).hexdigest()
    return salt, hashed

class VoiceAuth:
    def __init__(self):
        _ensure_auth_file()
        self._load()
        self.active_sessions = {}  # session_id -> {"username", "expires_at"}

    def _load(self):
        try:
            with open(AUTH_FILE, "r", encoding="utf-8") as fh:
                self.auth_data = json.load(fh)
            if "users" not in self.auth_data:
                self.auth_data["users"] = {}
        except Exception:
            self.auth_data = {"users": {}}
            self._save()

    def _save(self):
        try:
            with open(AUTH_FILE, "w", encoding="utf-8") as fh:
                json.dump(self.auth_data, fh, indent=2)
        except Exception as e:
            logger.exception("Failed to save auth data: %s", e)

    def register_user(self, username: str, voice_sample_hash: str, password: Optional[str]=None, role: str="user") -> dict:
        """
        Register a new user.
        role: 'user' or 'admin'
        Returns dict status message.
        """
        username = username.strip().lower()
        if not username:
            return {"status":"error","message":"Username required"}
        if username in self.auth_data.get("users", {}):
            return {"status":"error","message":"User already exists"}
        user = {"voice_hash": voice_sample_hash, "role": role, "created_at": datetime.utcnow().isoformat()}
        if password:
            salt, hashed = _hash_password(password)
            user["password_salt"] = salt
            user["password_hash"] = hashed
        self.auth_data.setdefault("users", {})[username] = user
        self._save()
        logger.info("Registered user '%s' with role '%s'", username, role)
        return {"status":"success","message":"User registered", "username": username, "role": role}

    def _compare_voice_hashes(self, stored_hash: str, provided_hash: str, threshold: float = 0.9) -> bool:
        """
        Compare stored and provided voice hash. This is a placeholder for
        a real voice biometric comparison. For now use exact match or prefix match.
        """
        if not stored_hash or not provided_hash:
            return False
        if stored_hash == provided_hash:
            return True
        # allow small differences: check prefix equality
        return stored_hash.startswith(provided_hash) or provided_hash.startswith(stored_hash)

    def authenticate_by_voice(self, username: str, voice_sample_hash: str, password: Optional[str]=None) -> Tuple[bool, str]:
        """
        Authenticate by voice (and optionally password). Returns (True, session_id) or (False, error_message)
        """
        username_l = (username or "").strip().lower()
        if username_l not in self.auth_data.get("users", {}):
            return False, "User not found"
        user = self.auth_data["users"][username_l]
        # If user has password, verify if provided
        if "password_hash" in user:
            if not password:
                return False, "Password required for this account"
            salt = user.get("password_salt")
            _, hashed = _hash_password(password, salt=salt)
            if hashed != user.get("password_hash"):
                return False, "Password incorrect"
        # Verify voice sample
        if not self._compare_voice_hashes(user.get("voice_hash",""), voice_sample_hash):
            return False, "Voice sample did not match"
        # Create session
        session_id = self._create_session(username_l)
        # Update last login
        user["last_login"] = datetime.utcnow().isoformat()
        self._save()
        return True, session_id

    def _create_session(self, username: str) -> str:
        sid = secrets.token_urlsafe(32)
        expires_at = (datetime.utcnow() + SESSION_DURATION).isoformat()
        self.active_sessions[sid] = {"username": username, "expires_at": expires_at, "created_at": datetime.utcnow().isoformat()}
        return sid

    def validate_session(self, session_id: str) -> Tuple[bool, Optional[str]]:
        if not session_id:
            return False, None
        s = self.active_sessions.get(session_id)
        if not s:
            return False, None
        expires = datetime.fromisoformat(s["expires_at"])
        if datetime.utcnow() > expires:
            # expired
            del self.active_sessions[session_id]
            return False, None
        return True, s["username"]

    def logout(self, session_id: str) -> bool:
        if session_id in self.active_sessions:
            del self.active_sessions[session_id]
            return True
        return False

    def is_admin(self, username: str) -> bool:
        if not username:
            return False
        u = self.auth_data.get("users", {}).get(username.lower())
        return bool(u and u.get("role") == "admin")

    def get_user(self, username: str) -> Optional[dict]:
        return self.auth_data.get("users", {}).get((username or "").lower())

    def cleanup_expired_sessions(self):
        now = datetime.utcnow()
        expired = [sid for sid,s in list(self.active_sessions.items()) if datetime.fromisoformat(s["expires_at"]) < now]
        for sid in expired:
            del self.active_sessions[sid]

# Create global instance
voice_auth = VoiceAuth()
