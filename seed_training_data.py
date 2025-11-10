"""
Database initialization and training data seeding script.
Seeds JARVIS bot with intent examples and conversational patterns.
"""

import asyncio
from datetime import datetime
from src.utils.db import db
from data.training_data import (
    TRAINING_INTENTS,
    CONVERSATIONAL_PATTERNS,
    CONTEXTUAL_RESPONSES,
    PERSONALITY_TRAITS,
)


def seed_training_data():
    """
    Seed the MongoDB database with training data for the bot.
    Creates collections for intents, patterns, and personality config.
    """
    try:
        # Access MongoDB collections
        intents_collection = db.db['training_intents']
        patterns_collection = db.db['training_patterns']
        config_collection = db.db['bot_config']
        
        # Clear existing data (optional - comment out to preserve existing data)
        # intents_collection.delete_many({})
        # patterns_collection.delete_many({})
        
        print("🤖 Seeding JARVIS training data...")
        
        # Insert intents
        intent_count = 0
        for intent_name, intent_data in TRAINING_INTENTS.items():
            document = {
                "name": intent_name,
                "examples": intent_data.get("examples", []),
                "responses": intent_data.get("responses", []),
                "created_at": datetime.utcnow(),
                "updated_at": datetime.utcnow(),
                "usage_count": 0,
                "confidence": 0.85,
            }
            
            # Use replace_one to avoid duplicates
            result = intents_collection.replace_one(
                {"name": intent_name},
                document,
                upsert=True
            )
            
            if result.upserted_id:
                intent_count += 1
                print(f"  ✓ Created intent: {intent_name}")
            else:
                print(f"  ✓ Updated intent: {intent_name}")
        
        print(f"\n✅ Seeded {intent_count} intents successfully")
        
        # Insert conversational patterns
        pattern_count = 0
        for pattern in CONVERSATIONAL_PATTERNS:
            document = {
                "pattern": pattern["pattern"],
                "response_template": pattern["response"],
                "created_at": datetime.utcnow(),
                "usage_count": 0,
            }
            
            result = patterns_collection.insert_one(document)
            pattern_count += 1
            print(f"  ✓ Added pattern: {pattern['pattern']}")
        
        print(f"\n✅ Seeded {pattern_count} conversational patterns")
        
        # Store bot configuration
        config_document = {
            "bot_name": "JARVIS",
            "personality": PERSONALITY_TRAITS,
            "contextual_responses": CONTEXTUAL_RESPONSES,
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
            "version": "1.0",
        }
        
        config_collection.replace_one(
            {"bot_name": "JARVIS"},
            config_document,
            upsert=True
        )
        
        print(f"✅ Bot configuration stored")
        
        # Log the seeding event
        db.save_system_event(
            event_type='training_data_seeded',
            description=f'Seeded {intent_count} intents and {pattern_count} patterns',
            status='success',
            details={
                'intents_count': intent_count,
                'patterns_count': pattern_count,
                'timestamp': datetime.utcnow().isoformat()
            }
        )
        
        print("\n🎉 Training data seeding complete!")
        
    except Exception as e:
        print(f"❌ Error seeding training data: {str(e)}")
        db.save_system_event(
            event_type='training_data_seed_error',
            description=f'Failed to seed training data: {str(e)}',
            status='error'
        )
        raise


def get_training_intent(intent_name: str) -> dict:
    """Retrieve a training intent from the database."""
    intents_collection = db.db['training_intents']
    return intents_collection.find_one({"name": intent_name}) or {}


def get_all_training_intents() -> list:
    """Retrieve all training intents from the database."""
    intents_collection = db.db['training_intents']
    return list(intents_collection.find({}))


def update_intent_usage(intent_name: str):
    """Update usage count for a training intent."""
    intents_collection = db.db['training_intents']
    intents_collection.update_one(
        {"name": intent_name},
        {"$inc": {"usage_count": 1}}
    )


if __name__ == "__main__":
    # Run seeding when script is executed directly
    try:
        seed_training_data()
    except Exception as e:
        print(f"Fatal error: {e}")
        exit(1)
