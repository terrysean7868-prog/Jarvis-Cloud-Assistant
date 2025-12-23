
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
import threading
import time
from pathlib import Path
from src.utils.db import db
from datetime import datetime, timedelta
from typing import Tuple, Optional

logger = logging.getLogger("jarvis.voice_auth")
logger.setLevel(logging.INFO)

# Configuration
PROJECT_ROOT = Path(__file__).resolve().parents[2]  # go up to project root
AUTH_FILE = PROJECT_ROOT / "data" / "auth_users.json"
SESSION_DURATION = timedelta(hours=8)  # sessions valid for 8 hours
AUTH_USE_DB = os.getenv("AUTH_USE_DB", "true").lower() in ("1", "true", "yes")
PENDING_QUEUE_FILE = PROJECT_ROOT / "data" / "auth_pending_queue.json"
QUEUE_FLUSH_INTERVAL = int(os.getenv("AUTH_QUEUE_FLUSH_INTERVAL", "10"))  # seconds
VOICE_HASH_PREFIX_MATCH = os.getenv("VOICE_HASH_PREFIX_MATCH", "false").lower() in ("1", "true", "yes")

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
        # pending registrations queued while DB is unavailable
        self._pending_lock = threading.Lock()
        self._load_pending_queue()
        self._stop_queue_thread = False
        self._queue_thread = threading.Thread(target=self._pending_worker, daemon=True)
        self._queue_thread.start()

    def _load(self):
        # Load from MongoDB when AUTH_USE_DB is enabled. Do not read/write local file when DB is primary.
        if AUTH_USE_DB:
            try:
                db._ensure_connected()
                users = list(db.db.auth_users.find({}))
                # Convert list of docs into dict keyed by username
                self.auth_data = {"users": {}}
                for u in users:
                    uname = (u.get("username") or "").lower()
                    if uname:
                        u.pop("_id", None)
                        self.auth_data["users"][uname] = u
                return
            except Exception:
                # DB not available - log and FALLBACK to local file so auth still works
                logger.warning("AUTH_USE_DB enabled but MongoDB not available; falling back to local auth file")
                # try to load local file as a fallback so previously-registered users remain available
                try:
                    with open(AUTH_FILE, "r", encoding="utf-8") as fh:
                        self.auth_data = json.load(fh)
                    if "users" not in self.auth_data:
                        self.auth_data["users"] = {}
                    return
                except Exception:
                    logger.exception("Failed to load fallback local auth file; starting with empty auth store")
                    self.auth_data = {"users": {}}
                    return

        # Fallback behavior: read local file (when AUTH_USE_DB is disabled)
        try:
            with open(AUTH_FILE, "r", encoding="utf-8") as fh:
                self.auth_data = json.load(fh)
            if "users" not in self.auth_data:
                self.auth_data["users"] = {}
        except Exception:
            self.auth_data = {"users": {}}
            self._save()

    def _save(self):
        # If using DB as primary store, persist only to MongoDB.
        if AUTH_USE_DB:
            try:
                db._ensure_connected()
                users_col = db.db.auth_users
                # Ensure username index
                users_col.create_index([("username", 1)], unique=True)
                for uname, udata in self.auth_data.get("users", {}).items():
                    doc = dict(udata)
                    doc["username"] = uname
                    users_col.update_one({"username": uname}, {"$set": doc}, upsert=True)
                return
            except Exception as e:
                # DB not available - log and FALLBACK to local file so auth updates aren't lost
                logger.exception("Failed to persist auth data to MongoDB: %s; falling back to local file", e)
                try:
                    with open(AUTH_FILE, "w", encoding="utf-8") as fh:
                        json.dump(self.auth_data, fh, indent=2)
                    return
                except Exception:
                    logger.exception("Failed to persist auth data to local file after DB failure")
                    # raise original exception to notify caller if desired
                    raise

        # If DB is not primary, save to local file
        try:
            with open(AUTH_FILE, "w", encoding="utf-8") as fh:
                json.dump(self.auth_data, fh, indent=2)
        except Exception as e:
            logger.exception("Failed to save auth data to file: %s", e)

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
        # Add to in-memory store immediately (so auth attempts can work locally once flushed)
        self.auth_data.setdefault("users", {})[username] = user
        # If configured to use DB as primary store, attempt to persist; if DB unavailable, queue for later
        if AUTH_USE_DB:
            try:
                # Ensure DB connection is attempted
                db._ensure_connected()
                # Wait briefly for background connection to complete
                if db.db is None:
                    # Give background reconnect thread a moment to connect
                    for attempt in range(3):
                        time.sleep(0.5)
                        if db.db is not None:
                            break
                # Check if DB is now connected
                if db.db is None:
                    raise RuntimeError('MongoDB not yet connected')
                users_col = db.db.auth_users
                users_col.create_index([("username", 1)], unique=True)
                doc = dict(user)
                doc["username"] = username
                users_col.update_one({"username": username}, {"$set": doc}, upsert=True)
                logger.info("Registered user '%s' persisted to MongoDB", username)
                return {"status":"success","message":"User registered", "username": username, "role": role}
            except Exception as e:
                # DB unavailable — queue the registration and continue
                logger.warning(f"MongoDB unavailable for registration of user '{username}': {e}")
                self._enqueue_pending({"username": username, "user": user})
                # Persist pending queue to disk
                self._persist_pending_queue()
                logger.info(f"Queued registration for user '{username}' - will flush when DB available")
                return {"status":"queued", "message":"Database unavailable; registration queued", "username": username}

        # Fallback/local save when DB not used
        try:
            self._save()
            logger.info("Registered user '%s' saved locally", username)
            return {"status":"success","message":"User registered (local)", "username": username, "role": role}
        except Exception as e:
            logger.exception("Failed to save user locally: %s", e)
            self.auth_data.get("users", {}).pop(username, None)
            return {"status":"error", "message": "Failed to save user locally"}

    def _compare_voice_hashes(self, stored_hash: str, provided_hash: str, threshold: float = 0.9) -> bool:
        """
        Compare stored and provided voice hash. This is a placeholder for
        a real voice biometric comparison.

        Default: exact match only.
        Optional: prefix match can be enabled for legacy behavior via VOICE_HASH_PREFIX_MATCH=true.
        """
        if not stored_hash or not provided_hash:
            return False
        if stored_hash == provided_hash:
            return True
        if VOICE_HASH_PREFIX_MATCH:
            return stored_hash.startswith(provided_hash) or provided_hash.startswith(stored_hash)
        return False

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

    def get_role(self, username: str) -> str:
        u = self.get_user(username) or {}
        r = (u.get("role") or "user").strip().lower()
        return r if r in ("user", "admin") else "user"

    def update_user(self, username: str, new_username: Optional[str] = None, new_role: Optional[str] = None) -> dict:
        """Update an existing user's username and/or role.

        Notes:
        - For JWT sessions, existing tokens keep the old username/role until re-login.
        - For local/dev in-memory sessions, we update active session username references.
        """
        old_uname = (username or "").strip().lower()
        if not old_uname:
            return {"status": "error", "message": "Username required"}

        users = self.auth_data.setdefault("users", {})
        if old_uname not in users:
            return {"status": "error", "message": "User not found"}

        target_uname = (new_username or "").strip().lower() if new_username else old_uname
        if new_username:
            if not target_uname:
                return {"status": "error", "message": "New username required"}
            if target_uname != old_uname and target_uname in users:
                return {"status": "error", "message": "New username already exists"}

        role = None
        if new_role is not None:
            role = (new_role or "").strip().lower()
            if role not in ("user", "admin"):
                return {"status": "error", "message": "Invalid role"}

        # Update user record in memory
        user_doc = dict(users[old_uname])
        if role is not None:
            user_doc["role"] = role
        user_doc["updated_at"] = datetime.utcnow().isoformat()

        # Rename key if needed
        if target_uname != old_uname:
            users.pop(old_uname, None)
            users[target_uname] = user_doc

            # Update in-memory sessions (legacy local sessions)
            for sid, s in list(self.active_sessions.items()):
                try:
                    if (s.get("username") or "").lower() == old_uname:
                        s["username"] = target_uname
                except Exception:
                    continue
        else:
            users[old_uname] = user_doc

        # Persist
        try:
            if AUTH_USE_DB:
                db._ensure_connected()
                if db.db is not None:
                    col = db.db.auth_users
                    col.create_index([("username", 1)], unique=True)
                    # If username changed, create new doc then delete old.
                    if target_uname != old_uname:
                        new_doc = dict(user_doc)
                        new_doc["username"] = target_uname
                        col.update_one({"username": target_uname}, {"$set": new_doc}, upsert=True)
                        col.delete_one({"username": old_uname})
                    else:
                        col.update_one({"username": old_uname}, {"$set": user_doc}, upsert=True)

                    return {
                        "status": "success",
                        "message": "User updated",
                        "username": target_uname,
                        "role": user_doc.get("role", "user"),
                        "note": "Existing JWT sessions require re-login" if True else "",
                    }

                # DB configured but unavailable -> fallback to local file persistence
                self._save()
                return {
                    "status": "success",
                    "message": "User updated (local fallback)",
                    "username": target_uname,
                    "role": user_doc.get("role", "user"),
                }

            # Local file store
            self._save()
            return {
                "status": "success",
                "message": "User updated",
                "username": target_uname,
                "role": user_doc.get("role", "user"),
            }
        except Exception as e:
            logger.exception("Failed to update user: %s", e)
            return {"status": "error", "message": "Failed to persist user update"}

    def cleanup_expired_sessions(self):
        now = datetime.utcnow()
        expired = [sid for sid,s in list(self.active_sessions.items()) if datetime.fromisoformat(s["expires_at"]) < now]
        for sid in expired:
            del self.active_sessions[sid]

    # Pending queue helpers
    def _load_pending_queue(self):
        try:
            if not PENDING_QUEUE_FILE.parent.exists():
                PENDING_QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
            if PENDING_QUEUE_FILE.exists():
                with open(PENDING_QUEUE_FILE, 'r', encoding='utf-8') as fh:
                    self._pending_queue = json.load(fh)
            else:
                self._pending_queue = []
        except Exception:
            self._pending_queue = []

    def _persist_pending_queue(self):
        try:
            with open(PENDING_QUEUE_FILE, 'w', encoding='utf-8') as fh:
                json.dump(self._pending_queue, fh, indent=2)
        except Exception:
            logger.exception("Failed to persist pending auth queue")

    def _enqueue_pending(self, item: dict):
        with self._pending_lock:
            self._pending_queue.append(item)

    def _dequeue_pending(self):
        with self._pending_lock:
            if not self._pending_queue:
                return None
            item = self._pending_queue.pop(0)
            return item

    def _pending_worker(self):
        """Background worker that flushes pending registrations to DB when available."""
        while not getattr(self, '_stop_queue_thread', False):
            try:
                # Try to flush while DB is available
                if getattr(db, 'db', None) is None:
                    # Ensure DB reconnect process is running
                    db._ensure_connected()
                if getattr(db, 'db', None):
                    # flush all queued items
                    flushed_any = False
                    while True:
                        item = None
                        with self._pending_lock:
                            if self._pending_queue:
                                item = self._pending_queue.pop(0)
                        if not item:
                            break
                        try:
                            uname = item.get('username')
                            udoc = dict(item.get('user') or {})
                            udoc['username'] = uname
                            users_col = db.db.auth_users
                            users_col.create_index([('username', 1)], unique=True)
                            users_col.update_one({'username': uname}, {'$set': udoc}, upsert=True)
                            logger.info("Flushed pending registration for '%s' to MongoDB", uname)
                            flushed_any = True
                        except Exception:
                            # Put back at front and stop trying for now
                            with self._pending_lock:
                                self._pending_queue.insert(0, item)
                            break
                    if flushed_any:
                        # persist queue state
                        self._persist_pending_queue()
                # sleep before next attempt
            except Exception:
                logger.debug("Pending worker encountered an error; will retry")
            time.sleep(QUEUE_FLUSH_INTERVAL)

    def stop(self):
        """Stop background threads (used in shutdown/tests)."""
        self._stop_queue_thread = True
        try:
            if self._queue_thread:
                self._queue_thread.join(timeout=1)
        except Exception:
            pass

# Create global instance
voice_auth = VoiceAuth()
