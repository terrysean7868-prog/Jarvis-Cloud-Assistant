"""
Memory System for JARVIS Bot
Stores and retrieves conversations, user preferences, and contextual data
from MongoDB for persistent learning and personalization.
"""

from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional
from utils.db import db
import json
from bson import ObjectId


class BotMemory:
    """
    Manages bot memory including:
    - Conversation history
    - User preferences
    - Context and state
    - Learning patterns
    """

    def __init__(self, user_id: str = "default"):
        self.user_id = user_id
        self.memory_collection = db.db['bot_memory']
        self.conversations_collection = db.db['conversations']
        self.user_prefs_collection = db.db['user_preferences']
        self.context_collection = db.db['conversation_context']
        self._ensure_indexes()

    def _ensure_indexes(self):
        """Create necessary indexes for fast queries"""
        try:
            self.memory_collection.create_index("user_id")
            self.conversations_collection.create_index([("user_id", 1), ("timestamp", -1)])
            self.user_prefs_collection.create_index("user_id", unique=True)
            self.context_collection.create_index([("user_id", 1), ("session_id", 1)])
        except Exception as e:
            print(f"Index creation note: {e}")

    def save_conversation(self, user_input: str, bot_response: str, intent: str = None) -> bool:
        """
        Save a conversation turn to MongoDB.
        Stores both input and response for learning and context.
        """
        try:
            conversation = {
                "user_id": self.user_id,
                "user_input": user_input,
                "bot_response": bot_response,
                "intent": intent,
                "timestamp": datetime.utcnow(),
                "session_id": self._get_current_session_id(),
                "feedback": None,  # User can rate this later
                "useful": None,
            }
            result = self.conversations_collection.insert_one(conversation)
            return bool(result.inserted_id)
        except Exception as e:
            print(f"Error saving conversation: {e}")
            return False

    def get_recent_context(self, limit: int = 5) -> List[Dict[str, Any]]:
        """
        Retrieve recent conversation history for context.
        Used to provide continuity in conversation.
        """
        try:
            conversations = list(
                self.conversations_collection.find(
                    {"user_id": self.user_id}
                )
                .sort("timestamp", -1)
                .limit(limit)
            )
            # Convert ObjectId to string for JSON serialization
            for conv in conversations:
                conv['_id'] = str(conv['_id'])
                conv['timestamp'] = conv['timestamp'].isoformat()
            return conversations[::-1]  # Return in chronological order
        except Exception as e:
            print(f"Error retrieving context: {e}")
            return []

    def save_user_preference(self, key: str, value: Any) -> bool:
        """
        Save user preferences (response style, favorite sites, etc.)
        """
        try:
            self.user_prefs_collection.update_one(
                {"user_id": self.user_id},
                {
                    "$set": {
                        f"preferences.{key}": value,
                        "updated_at": datetime.utcnow()
                    }
                },
                upsert=True
            )
            return True
        except Exception as e:
            print(f"Error saving preference: {e}")
            return False

    def get_user_preferences(self) -> Dict[str, Any]:
        """Retrieve all user preferences."""
        try:
            prefs = self.user_prefs_collection.find_one(
                {"user_id": self.user_id}
            )
            return prefs.get("preferences", {}) if prefs else {}
        except Exception as e:
            print(f"Error retrieving preferences: {e}")
            return {}

    def save_memory_fact(self, key: str, value: Any, category: str = "general") -> bool:
        """
        Save a specific memory fact (e.g., user name, preferred language, etc.)
        """
        try:
            self.memory_collection.update_one(
                {"user_id": self.user_id, "key": key},
                {
                    "$set": {
                        "value": value,
                        "category": category,
                        "updated_at": datetime.utcnow(),
                        "frequency": 1
                    },
                    "$inc": {"access_count": 0}
                },
                upsert=True
            )
            return True
        except Exception as e:
            print(f"Error saving memory fact: {e}")
            return False

    def retrieve_memory_fact(self, key: str) -> Optional[Any]:
        """Retrieve a specific memory fact and increment access count."""
        try:
            result = self.memory_collection.find_one_and_update(
                {"user_id": self.user_id, "key": key},
                {
                    "$inc": {"access_count": 1},
                    "$set": {"last_accessed": datetime.utcnow()}
                }
            )
            return result.get("value") if result else None
        except Exception as e:
            print(f"Error retrieving memory fact: {e}")
            return None

    def get_memory_summary(self) -> Dict[str, Any]:
        """Get a summary of all stored memory for context."""
        try:
            memory_facts = list(
                self.memory_collection.find(
                    {"user_id": self.user_id}
                ).sort("access_count", -1).limit(10)
            )
            summary = {}
            for fact in memory_facts:
                summary[fact['key']] = fact.get('value')
            return summary
        except Exception as e:
            print(f"Error getting memory summary: {e}")
            return {}

    def cleanup_old_conversations(self, days: int = 30) -> int:
        """
        Remove conversations older than specified days.
        Keeps database optimized and respects data privacy.
        """
        try:
            cutoff_date = datetime.utcnow() - timedelta(days=days)
            result = self.conversations_collection.delete_many(
                {
                    "user_id": self.user_id,
                    "timestamp": {"$lt": cutoff_date}
                }
            )
            return result.deleted_count
        except Exception as e:
            print(f"Error cleaning up conversations: {e}")
            return 0

    def get_conversation_stats(self) -> Dict[str, Any]:
        """Get statistics about conversations with this user."""
        try:
            total = self.conversations_collection.count_documents(
                {"user_id": self.user_id}
            )
            
            # Get most common intents
            pipeline = [
                {"$match": {"user_id": self.user_id, "intent": {"$ne": None}}},
                {"$group": {"_id": "$intent", "count": {"$sum": 1}}},
                {"$sort": {"count": -1}},
                {"$limit": 5}
            ]
            top_intents = list(self.conversations_collection.aggregate(pipeline))
            
            return {
                "total_conversations": total,
                "top_intents": top_intents,
                "last_conversation": self.conversations_collection.find_one(
                    {"user_id": self.user_id},
                    sort=[("timestamp", -1)]
                )
            }
        except Exception as e:
            print(f"Error getting conversation stats: {e}")
            return {}

    def _get_current_session_id(self) -> str:
        """Get or create current session ID."""
        try:
            session = self.context_collection.find_one(
                {"user_id": self.user_id, "active": True}
            )
            if session:
                return str(session['session_id'])
            
            # Create new session
            new_session = {
                "user_id": self.user_id,
                "session_id": str(ObjectId()),
                "start_time": datetime.utcnow(),
                "active": True
            }
            result = self.context_collection.insert_one(new_session)
            return str(new_session['session_id'])
        except Exception as e:
            print(f"Error managing session: {e}")
            return str(ObjectId())

    def end_session(self) -> bool:
        """End current session."""
        try:
            self.context_collection.update_many(
                {"user_id": self.user_id, "active": True},
                {"$set": {"active": False, "end_time": datetime.utcnow()}}
            )
            return True
        except Exception as e:
            print(f"Error ending session: {e}")
            return False

    def get_contextual_memory(self) -> str:
        """
        Get formatted context for use in LLM prompts.
        Includes recent conversations and key memory facts.
        """
        try:
            context_parts = []
            
            # Add user preferences
            prefs = self.get_user_preferences()
            if prefs:
                context_parts.append(f"User preferences: {json.dumps(prefs)}")
            
            # Add recent conversations
            recent = self.get_recent_context(limit=3)
            if recent:
                context_parts.append("Recent context:")
                for conv in recent:
                    context_parts.append(f"  User: {conv['user_input'][:100]}")
                    context_parts.append(f"  JARVIS: {conv['bot_response'][:100]}")
            
            # Add key memory facts
            memory = self.get_memory_summary()
            if memory:
                context_parts.append(f"Remembered facts: {json.dumps(memory)}")
            
            return "\n".join(context_parts)
        except Exception as e:
            print(f"Error formatting contextual memory: {e}")
            return ""
