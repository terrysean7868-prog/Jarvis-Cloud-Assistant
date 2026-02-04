from pymongo import MongoClient, ASCENDING, DESCENDING, IndexModel
from datetime import datetime
import os
import sqlite3
from pathlib import Path
from dotenv import load_dotenv
from src.config import env
import json
from bson import ObjectId
from urllib.parse import quote_plus, urlparse
import threading
import time

load_dotenv()

class JSONEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, ObjectId):
            return str(o)
        if isinstance(o, datetime):
            return o.isoformat()
        return super().default(o)

class Database:
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(Database, cls).__new__(cls)
            cls._instance._initialized = False
            cls._instance.client = None
            cls._instance.db = None
            cls._instance._error = None
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        
        # Lazy-load database; don't fail on startup
        self._initialized = True
        # Don't connect until first database call
        self._reconnect_thread = None
        self._reconnect_lock = threading.Lock()
        self._stop_reconnect = False
    
    def _ensure_connected(self):
        """Ensure database is connected before use."""
        if self.client is None:
            # Try a non-raising connect first
            self._connect(raise_on_fail=False)
            if self.client is None:
                # start background reconnect attempts
                self._start_reconnect_thread()
    
    def _connect(self, raise_on_fail=True):
        """Connect to MongoDB. If raise_on_fail is False, failures won't raise but will set client to None."""
        if self.client is not None:
            return  # Already connected

        uri = env.get('MONGODB_URI') or env.get('MONGO_URI')
        if not uri:
            msg = (
                "MONGODB_URI not set in environment. "
                "Set MONGODB_URI=mongodb://localhost:27017/jarvis or your MongoDB Atlas URI"
            )
            if raise_on_fail:
                raise ValueError(msg)
            else:
                self._error = msg
                return

        try:
            # Handle MongoDB URI with special characters
            if 'mongodb+srv://' in uri:
                # Split the URI into parts
                prefix = 'mongodb+srv://'
                rest = uri.replace(prefix, '')

                # Find the position of the last @ before the hostname
                last_at = rest.rindex('@')

                # Split credentials and host info
                credentials = rest[:last_at]
                host_part = rest[last_at + 1:]

                # Find username and password
                username_end = credentials.find(':')
                username = credentials[:username_end]
                password = credentials[username_end + 1:]

                # Reconstruct URI with escaped characters
                self.uri = f"{prefix}{quote_plus(username)}:{quote_plus(password)}@{host_part}"
            else:
                self.uri = uri

            # Initialize MongoDB connection
            self.client = MongoClient(self.uri, serverSelectionTimeoutMS=5000)
            self.client.admin.command('ping')
            self.db = self.client[env.get_str('MONGODB_DB_NAME', 'jarvis_db')]
            self._setup_collections()
            print("[DB] SUCCESS - Connected to MongoDB")

        except Exception as e:
            self._error = str(e)
            print(f"[DB] ERROR: Failed to connect to MongoDB: {str(e)[:200]}")
            print(f"[DB] Make sure MongoDB is running locally or set MONGODB_URI to your Atlas cluster")
            # cleanup partial state
            self.client = None
            self.db = None
            if raise_on_fail:
                raise
            
    def _setup_collections(self):
        """Setup collections with proper indexes and schemas"""
        # Chat History Collection
        chat_history = self.db.chat_history
        chat_history.create_indexes([
            IndexModel([("timestamp", DESCENDING)]),
            IndexModel([("user_id", ASCENDING)]),
            IndexModel([("session_id", ASCENDING)]),
            IndexModel([("intent", ASCENDING)])
        ])
        
        # System Events Collection
        system_events = self.db.system_events
        system_events.create_indexes([
            IndexModel([("timestamp", DESCENDING)]),
            IndexModel([("event_type", ASCENDING)]),
            IndexModel([("status", ASCENDING)])
        ])
        
        # Voice Commands Collection
        voice_commands = self.db.voice_commands
        voice_commands.create_indexes([
            IndexModel([("timestamp", DESCENDING)]),
            IndexModel([("command_type", ASCENDING)]),
            IndexModel([("status", ASCENDING)])
        ])
        
        # Module Changes Collection
        module_changes = self.db.module_changes
        module_changes.create_indexes([
            IndexModel([("timestamp", DESCENDING)]),
            IndexModel([("module_name", ASCENDING)]),
            IndexModel([("change_type", ASCENDING)])
        ])
        
        # Git Operations Collection
        git_operations = self.db.git_operations
        git_operations.create_indexes([
            IndexModel([("timestamp", DESCENDING)]),
            IndexModel([("commit_hash", ASCENDING)]),
            IndexModel([("status", ASCENDING)])
        ])

        # Learning Examples Collection (RAG-lite training store)
        learning_examples = self.db.learning_examples
        learning_examples.create_indexes([
            IndexModel([("timestamp", DESCENDING)]),
            IndexModel([("user_id", ASCENDING)]),
            IndexModel([("last_used", DESCENDING)]),
            IndexModel([("usage_count", DESCENDING)]),
        ])
        # Optional text index for search (MongoDB will allow only one text index per collection)
        try:
            learning_examples.create_index(
                [("prompt", "text"), ("completion", "text"), ("tags", "text")],
                name="learning_examples_text_idx",
                default_language="english",
            )
        except Exception:
            # Some Mongo configs may reject text index creation; we'll fall back to regex search.
            pass

        # Web Training Data (short summaries + sources)
        web_training_data = self.db.web_training_data
        web_training_data.create_indexes([
            IndexModel([("fetched_at", DESCENDING)]),
            IndexModel([("topic", ASCENDING)]),
            IndexModel([("url", ASCENDING)]),
        ])
        try:
            web_training_data.create_index(
                [
                    ("topic", "text"),
                    ("title", "text"),
                    ("snippet", "text"),
                    ("summary", "text"),
                    # Optional enrichment fields (added by background analysis)
                    ("analysis_insight", "text"),
                    ("analysis_tags", "text"),
                ],
                name="web_training_text_idx",
                default_language="english",
            )
        except Exception:
            pass
        # end _setup_collections

    def _start_reconnect_thread(self):
        """Start background thread to attempt reconnection if not already running."""
        with self._reconnect_lock:
            if getattr(self, '_reconnect_thread', None) and self._reconnect_thread.is_alive():
                return
            self._stop_reconnect = False
            t = threading.Thread(target=self._reconnect_loop, daemon=True)
            self._reconnect_thread = t
            t.start()

    def _reconnect_loop(self):
        """Background loop that periodically attempts to connect to MongoDB."""
        attempt = 0
        while not self._stop_reconnect and (self.client is None):
            try:
                attempt += 1
                print(f"[DB] Reconnect attempt {attempt}...")
                self._connect(raise_on_fail=False)
                if self.client:
                    print("[DB] Reconnected to MongoDB")
                    break
            except Exception:
                pass
            # backoff with cap
            sleep_sec = min(10 + attempt * 5, 60)
            time.sleep(sleep_sec)

    def stop_reconnect(self):
        """Stop the reconnect background thread (used in shutdown/tests)."""
        self._stop_reconnect = True
        try:
            if self._reconnect_thread:
                self._reconnect_thread.join(timeout=1)
        except Exception:
            pass
    
    def save_chat(self, user_input, bot_response, session_id=None, intent=None, context=None):
        """
        Save chat interaction to MongoDB
        
        Args:
            user_input (str): User's input text or command
            bot_response (str): Jarvis's response
            session_id (str): Unique session identifier
            intent (str): Classified intent of user's input
            context (dict): Additional context about the interaction
        """
        self._ensure_connected()
        if self.db is None:
            return None
        collection = self.db.chat_history
        doc = {
            'timestamp': datetime.utcnow(),
            'session_id': session_id or ObjectId(),
            'user_input': user_input,
            'bot_response': bot_response,
            'intent': intent,
            'context': context or {},
            'metadata': {
                'input_type': 'voice' if user_input.startswith('Voice: ') else 'text',
                'response_type': 'error' if 'error' in bot_response.lower() else 'success'
            }
        }
        return collection.insert_one(doc)

    def save_system_event(self, event_type, description, status, details=None):
        """
        Save system events to MongoDB
        
        Args:
            event_type (str): Type of event (update/restart/error)
            description (str): Event description
            status (str): Event status (success/error/pending)
            details (dict): Additional event details
        """
        self._ensure_connected()
        if self.db is None:
            return None
        collection = self.db.system_events
        doc = {
            'timestamp': datetime.utcnow(),
            'event_type': event_type,
            'description': description,
            'status': status,
            'details': details or {},
            'metadata': {
                'hostname': env.get_str('COMPUTERNAME', 'unknown'),
                'environment': env.get_str('ENVIRONMENT', 'development')
            }
        }
        return collection.insert_one(doc)

    # =========================================================
    # Learning Examples (persistent training store)
    # =========================================================
    def save_learning_example(self, user_id: str, prompt: str, completion: str, meta: dict | None = None, tags: list[str] | None = None):
        """Persist a prompt/completion pair for later retrieval.

        This is NOT model fine-tuning; it is a RAG-lite store used to improve continuity.
        """
        self._ensure_connected()
        if self.db is None:
            return None

        user_id = (user_id or "default").strip().lower()
        prompt = (prompt or "").strip()
        completion = (completion or "").strip()

        if not prompt or not completion:
            return None

        collection = self.db.learning_examples
        doc = {
            "timestamp": datetime.utcnow(),
            "user_id": user_id,
            "prompt": prompt,
            "completion": completion,
            "meta": meta or {},
            "tags": tags or [],
            "usage_count": 0,
            "last_used": None,
        }
        return collection.insert_one(doc)

    def search_learning_examples(self, query: str, user_id: str | None = None, limit: int = 3):
        """Search learning examples by query.

        Prefers MongoDB text search when the text index exists; falls back to regex.
        Returns a list of dicts with ObjectIds converted to strings.
        """
        self._ensure_connected()
        if self.db is None:
            return []

        q = (query or "").strip()
        if not q:
            return []

        collection = self.db.learning_examples
        base_filter = {}
        if user_id:
            base_filter["user_id"] = (user_id or "").strip().lower()

        # Try text search
        try:
            cursor = (
                collection.find({**base_filter, "$text": {"$search": q}}, {"score": {"$meta": "textScore"}})
                .sort([("score", {"$meta": "textScore"}), ("timestamp", DESCENDING)])
                .limit(int(limit))
            )
            results = list(cursor)
        except Exception:
            # Fallback regex search (case-insensitive) across prompt/completion
            try:
                import re

                safe = re.escape(q)
                regex = {"$regex": safe, "$options": "i"}
                cursor = (
                    collection.find({
                        **base_filter,
                        "$or": [{"prompt": regex}, {"completion": regex}, {"tags": regex}],
                    })
                    .sort("timestamp", DESCENDING)
                    .limit(int(limit))
                )
                results = list(cursor)
            except Exception:
                results = []

        # Touch usage counters best-effort
        try:
            ids = [r.get("_id") for r in results if r.get("_id")]
            if ids:
                collection.update_many(
                    {"_id": {"$in": ids}},
                    {"$inc": {"usage_count": 1}, "$set": {"last_used": datetime.utcnow()}},
                )
        except Exception:
            pass

        # Normalize ids
        for r in results:
            try:
                r["_id"] = str(r.get("_id"))
            except Exception:
                pass
        return results

    def get_learning_stats(self, user_id: str | None = None):
        self._ensure_connected()
        if self.db is None:
            return {"count": 0}
        collection = self.db.learning_examples
        query = {"user_id": (user_id or "").strip().lower()} if user_id else {}
        return {"count": collection.count_documents(query)}

    def delete_learning_examples(self, user_id: str | None = None):
        self._ensure_connected()
        if self.db is None:
            return {"deleted": 0}
        collection = self.db.learning_examples
        query = {"user_id": (user_id or "").strip().lower()} if user_id else {}
        res = collection.delete_many(query)
        return {"deleted": int(res.deleted_count)}

    # =========================================================
    # Web Training Data (internet summaries)
    # =========================================================
    def save_web_training_item(self, topic: str, title: str | None, snippet: str | None, summary: str | None, url: str | None, source: str = "web"):
        """Store a web-derived training item.

        Stores only short summaries/snippets + metadata (no full-page mirroring).
        """
        self._ensure_connected()
        if self.db is None:
            return None

        from src.core.web_training_schema import normalize_web_training_item

        doc = normalize_web_training_item(
            topic=topic,
            title=title,
            snippet=snippet,
            summary=summary,
            url=url,
            source=source,
        )
        if not doc:
            return None

        collection = self.db.web_training_data
        # Upsert to avoid duplicates by (topic,url)
        return collection.update_one({"topic": doc["topic"], "url": doc["url"]}, {"$set": doc}, upsert=True)

    def search_web_training(self, query: str, limit: int = 3):
        """Search web_training_data by query and return best-effort relevant items."""
        self._ensure_connected()
        if self.db is None:
            return []
        q = (query or "").strip()
        if not q:
            return []

        collection = self.db.web_training_data
        limit = max(1, min(int(limit), 10))

        try:
            cursor = (
                collection.find({"$text": {"$search": q}}, {"score": {"$meta": "textScore"}})
                .sort([("score", {"$meta": "textScore"}), ("fetched_at", DESCENDING)])
                .limit(limit)
            )
            items = list(cursor)
        except Exception:
            try:
                import re

                safe = re.escape(q)
                regex = {"$regex": safe, "$options": "i"}
                cursor = (
                    collection.find(
                        {
                            "$or": [
                                {"topic": regex},
                                {"title": regex},
                                {"snippet": regex},
                                {"summary": regex},
                                {"analysis_insight": regex},
                                {"analysis_tags": regex},
                            ]
                        }
                    )
                    .sort("fetched_at", DESCENDING)
                    .limit(limit)
                )
                items = list(cursor)
            except Exception:
                items = []

        for it in items:
            try:
                it["_id"] = str(it.get("_id"))
            except Exception:
                pass
        return items

    def save_voice_command(self, command_text, command_type, status, result=None):
        """
        Save voice command details to MongoDB
        
        Args:
            command_text (str): Raw voice command text
            command_type (str): Type of command (system/module/git)
            status (str): Command execution status
            result (dict): Command execution result
        """
        collection = self.db.voice_commands
        doc = {
            'timestamp': datetime.utcnow(),
            'command_text': command_text,
            'command_type': command_type,
            'status': status,
            'result': result or {},
            'metadata': {
                'confidence_score': result.get('confidence', 1.0) if result else 1.0,
                'execution_time': result.get('execution_time') if result else None
            }
        }
        return collection.insert_one(doc)

    def save_module_change(self, module_name, change_type, content, author='Jarvis'):
        """
        Save module modifications to MongoDB
        
        Args:
            module_name (str): Name of the module
            change_type (str): Type of change (create/update/delete)
            content (str): Module content or changes
            author (str): Change author
        """
        collection = self.db.module_changes
        doc = {
            'timestamp': datetime.utcnow(),
            'module_name': module_name,
            'change_type': change_type,
            'content': content,
            'author': author,
            'metadata': {
                'file_path': f"modules/{module_name}.py",
                'lines_changed': len(content.splitlines())
            }
        }
        return collection.insert_one(doc)

    def save_git_sync(self, commit_hash, message, status, details=None):
        """
        Save git sync operations to MongoDB
        
        Args:
            commit_hash (str): Git commit hash
            message (str): Commit message
            status (str): Operation status
            details (dict): Additional operation details
        """
        collection = self.db.git_operations
        doc = {
            'timestamp': datetime.utcnow(),
            'commit_hash': commit_hash,
            'message': message,
            'status': status,
            'details': details or {},
            'metadata': {
                'branch': env.get_str('GIT_BRANCH', 'main'),
                'repository': env.get_str('GITHUB_REPO', 'unknown')
            }
        }
        return collection.insert_one(doc)
        
    def get_chat_history(self, session_id=None, limit=10, skip=0):
        """
        Get chat history with optional session filtering
        
        Args:
            session_id (str): Optional session ID to filter by
            limit (int): Maximum number of records to return
            skip (int): Number of records to skip (for pagination)
        """
        query = {'session_id': session_id} if session_id else {}
        return list(self.db.chat_history
                   .find(query)
                   .sort('timestamp', DESCENDING)
                   .skip(skip)
                   .limit(limit))

    def get_system_events(self, event_type=None, status=None, start_date=None, limit=10):
        """
        Get system events with optional filters
        
        Args:
            event_type (str): Optional event type filter
            status (str): Optional status filter
            start_date (datetime): Optional start date filter
            limit (int): Maximum number of records to return
        """
        query = {}
        if event_type:
            query['event_type'] = event_type
        if status:
            query['status'] = status
        if start_date:
            query['timestamp'] = {'$gte': start_date}
            
        return list(self.db.system_events
                   .find(query)
                   .sort('timestamp', DESCENDING)
                   .limit(limit))

    def get_voice_commands(self, command_type=None, status=None, limit=10):
        """
        Get voice command history with optional filters
        
        Args:
            command_type (str): Optional command type filter
            status (str): Optional status filter
            limit (int): Maximum number of records to return
        """
        query = {}
        if command_type:
            query['command_type'] = command_type
        if status:
            query['status'] = status
            
        return list(self.db.voice_commands
                   .find(query)
                   .sort('timestamp', DESCENDING)
                   .limit(limit))

    def get_module_changes(self, module_name=None, change_type=None, limit=10):
        """
        Get module change history with optional filters
        
        Args:
            module_name (str): Optional module name filter
            change_type (str): Optional change type filter
            limit (int): Maximum number of records to return
        """
        query = {}
        if module_name:
            query['module_name'] = module_name
        if change_type:
            query['change_type'] = change_type
            
        return list(self.db.module_changes
                   .find(query)
                   .sort('timestamp', DESCENDING)
                   .limit(limit))

    def get_git_operations(self, status=None, start_date=None, limit=10):
        """
        Get git operation history with optional filters
        
        Args:
            status (str): Optional status filter
            start_date (datetime): Optional start date filter
            limit (int): Maximum number of records to return
        """
        query = {}
        if status:
            query['status'] = status
        if start_date:
            query['timestamp'] = {'$gte': start_date}
            
        return list(self.db.git_operations
                   .find(query)
                   .sort('timestamp', DESCENDING)
                   .limit(limit))

    def get_stats(self):
        """Get system statistics"""
        return {
            'total_chats': self.db.chat_history.count_documents({}),
            'total_voice_commands': self.db.voice_commands.count_documents({}),
            'successful_git_ops': self.db.git_operations.count_documents({'status': 'success'}),
            'failed_git_ops': self.db.git_operations.count_documents({'status': 'error'}),
            'module_updates': self.db.module_changes.count_documents({'change_type': 'update'}),
            'system_errors': self.db.system_events.count_documents({'status': 'error'})
        }

    def to_json(self, data):
        """Convert MongoDB document to JSON string"""
        return json.dumps(data, cls=JSONEncoder)

# Create singleton instance
db = Database()
