from pymongo import MongoClient, ASCENDING, DESCENDING, IndexModel
from datetime import datetime
import os
import sqlite3
from pathlib import Path
from dotenv import load_dotenv
import json
from bson import ObjectId
from urllib.parse import quote_plus, urlparse

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
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
            
        uri = os.getenv('MONGODB_URI') or os.getenv('MONGO_URI')
        if not uri:
            raise ValueError("MongoDB URI not found in environment variables")
            
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
                print("MongoDB URI successfully parsed and escaped")
            else:
                self.uri = uri
                
            # Initialize MongoDB connection
            self.client = MongoClient(self.uri, serverSelectionTimeoutMS=5000)
            self.client.admin.command('ping')
            self.db = self.client[os.getenv('MONGODB_DB_NAME', 'jarvis_db')]
            self._initialized = True
            self._setup_collections()
            
            print("Successfully connected to MongoDB")
            
        except Exception as e:
            print(f"MongoDB connection error: {str(e)}")
            # Fallback to local storage if MongoDB connection fails
            self._setup_local_fallback()
            raise ConnectionError(f'MongoDB connection failed: {e}')
            
    def _setup_local_fallback(self):
        """Setup local SQLite database as fallback"""
        try:
            db_path = Path("jarvis_local.db")
            self.local_db = sqlite3.connect(str(db_path))
            cursor = self.local_db.cursor()
            
            # Create tables for essential data
            cursor.executescript("""
                CREATE TABLE IF NOT EXISTS chat_history (
                    id INTEGER PRIMARY KEY,
                    user_id TEXT,
                    message TEXT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                );
                
                CREATE TABLE IF NOT EXISTS system_events (
                    id INTEGER PRIMARY KEY,
                    event_type TEXT,
                    description TEXT,
                    status TEXT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                );
                
                CREATE INDEX IF NOT EXISTS idx_chat_timestamp 
                ON chat_history(timestamp DESC);
                
                CREATE INDEX IF NOT EXISTS idx_events_type 
                ON system_events(event_type);
            """)
            
            self.local_db.commit()
            print("Local SQLite database initialized as fallback")
            
        except Exception as e:
            print(f"Failed to setup local fallback: {e}")
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
        collection = self.db.system_events
        doc = {
            'timestamp': datetime.utcnow(),
            'event_type': event_type,
            'description': description,
            'status': status,
            'details': details or {},
            'metadata': {
                'hostname': os.environ.get('COMPUTERNAME', 'unknown'),
                'environment': os.environ.get('ENVIRONMENT', 'development')
            }
        }
        return collection.insert_one(doc)

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
                'branch': os.environ.get('GIT_BRANCH', 'main'),
                'repository': os.environ.get('GITHUB_REPO', 'unknown')
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
