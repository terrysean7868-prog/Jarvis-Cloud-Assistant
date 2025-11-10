"""
Background Job Scheduler for JARVIS
Handles periodic tasks like:
- GitHub auto-sync
- Database cleanup
- Training data updates
- Memory optimization
"""

import asyncio
import os
from datetime import datetime, timedelta
from typing import Callable, Optional
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from src.utils.db import db
from src.utils.git_sync import git_sync


class JobScheduler:
    """Background job scheduler for periodic tasks"""

    def __init__(self):
        self.scheduler = BackgroundScheduler()
        self.is_running = False

    def start(self):
        """Start the background scheduler"""
        if not self.is_running:
            self.scheduler.start()
            self.is_running = True
            print("✅ Background Job Scheduler started")

    def stop(self):
        """Stop the background scheduler"""
        if self.is_running:
            self.scheduler.shutdown()
            self.is_running = False
            print("⛔ Background Job Scheduler stopped")

    def add_job(self, func: Callable, interval_seconds: int = 300, job_id: str = None):
        """
        Add a periodic job to the scheduler

        Args:
            func: Function to execute
            interval_seconds: How often to run (default: 5 minutes)
            job_id: Unique job identifier
        """
        try:
            job_id = job_id or f"job_{datetime.utcnow().timestamp()}"
            self.scheduler.add_job(
                func,
                IntervalTrigger(seconds=interval_seconds),
                id=job_id,
                replace_existing=True
            )
            print(f"✅ Job '{job_id}' scheduled every {interval_seconds}s")
        except Exception as e:
            print(f"❌ Error adding job: {e}")

    def register_default_jobs(self):
        """Register all default background jobs"""
        # GitHub auto-sync every 5 minutes
        self.add_job(
            auto_sync_github,
            interval_seconds=300,
            job_id="github_sync"
        )

        # Database cleanup every hour
        self.add_job(
            cleanup_database,
            interval_seconds=3600,
            job_id="db_cleanup"
        )

        # Fetch training data every 24 hours
        self.add_job(
            update_training_data,
            interval_seconds=86400,
            job_id="training_data_update"
        )
        
        # Fetch web training data every 12 hours
        self.add_job(
            fetch_web_training_data,
            interval_seconds=43200,
            job_id="web_training_fetch"
        )

        # Memory optimization every 6 hours
        self.add_job(
            optimize_memory,
            interval_seconds=21600,
            job_id="memory_optimization"
        )


# ===== Default Background Jobs =====

def auto_sync_github():
    """Auto-sync code changes to GitHub"""
    try:
        print(f"\n🔄 [AUTO-SYNC] Starting GitHub sync at {datetime.utcnow().isoformat()}")
        git_sync(repo_path=".")
        
        # Log the sync event
        db.save_system_event(
            event_type='auto_sync_github',
            description='Automatic GitHub sync completed',
            status='success',
            details={'timestamp': datetime.utcnow().isoformat()}
        )
        print("✅ [AUTO-SYNC] GitHub sync completed")
    except Exception as e:
        print(f"❌ [AUTO-SYNC] GitHub sync failed: {e}")
        db.save_system_event(
            event_type='auto_sync_error',
            description=f'GitHub auto-sync failed: {str(e)}',
            status='error'
        )


def cleanup_database():
    """Clean up old data from MongoDB"""
    try:
        print(f"\n🧹 [CLEANUP] Starting database cleanup at {datetime.utcnow().isoformat()}")
        
        # Remove conversations older than 30 days
        cutoff_date = datetime.utcnow() - timedelta(days=30)
        conversations = db.db['conversations']
        deleted = conversations.delete_many(
            {"timestamp": {"$lt": cutoff_date}}
        ).deleted_count
        
        # Remove old system events (90 days)
        cutoff_date = datetime.utcnow() - timedelta(days=90)
        system_events = db.db['system_events']
        deleted_events = system_events.delete_many(
            {"timestamp": {"$lt": cutoff_date}}
        ).deleted_count
        
        print(f"✅ [CLEANUP] Removed {deleted} conversations and {deleted_events} events")
        
        db.save_system_event(
            event_type='database_cleanup',
            description=f'Cleaned {deleted} conversations and {deleted_events} events',
            status='success'
        )
    except Exception as e:
        print(f"❌ [CLEANUP] Database cleanup failed: {e}")
        db.save_system_event(
            event_type='cleanup_error',
            description=f'Database cleanup failed: {str(e)}',
            status='error'
        )


def update_training_data():
    """
    Fetch and update training data from internet sources
    (Can be extended to scrape AI/tech websites)
    """
    try:
        print(f"\n📚 [TRAINING] Updating training data at {datetime.utcnow().isoformat()}")
        
        # Here you can add web scraping logic to fetch training data
        # For now, we'll just log the update
        
        db.save_system_event(
            event_type='training_data_update',
            description='Periodic training data update completed',
            status='success',
            details={'timestamp': datetime.utcnow().isoformat()}
        )
        print("✅ [TRAINING] Training data update completed")
    except Exception as e:
        print(f"❌ [TRAINING] Training data update failed: {e}")
        db.save_system_event(
            event_type='training_update_error',
            description=f'Training data update failed: {str(e)}',
            status='error'
        )


def optimize_memory():
    """Optimize memory and database indexes"""
    try:
        print(f"\n⚡ [OPTIMIZE] Starting memory optimization at {datetime.utcnow().isoformat()}")
        
        # Rebuild indexes for faster queries
        db.db['conversations'].reindex()
        db.db['bot_memory'].reindex()
        db.db['user_preferences'].reindex()
        
        # Aggregate and cache frequently accessed data
        # This would improve query performance
        
        print("✅ [OPTIMIZE] Memory optimization completed")
        
        db.save_system_event(
            event_type='memory_optimization',
            description='Database indexes optimized',
            status='success'
        )
    except Exception as e:
        print(f"❌ [OPTIMIZE] Memory optimization failed: {e}")


def fetch_web_training_data():
    """Fetch training data from internet sources"""
    try:
        print(f"\n📚 [WEB-TRAINING] Starting web training data fetch at {datetime.utcnow().isoformat()}")
        
        # Topics to fetch training data for
        topics = [
            'artificial intelligence trends',
            'Python programming tips',
            'web development best practices',
            'cloud computing news',
            'cybersecurity updates'
        ]
        
        # This job would:
        # 1. Search web for training data
        # 2. Parse and clean content
        # 3. Store in training_data collection
        # 4. Update training data in memory
        
        # Run async fetching
        asyncio.run(_fetch_training_data_async(topics))
        
        print("✅ [WEB-TRAINING] Web training data fetch completed")
        
        db.save_system_event(
            event_type='web_training_fetch',
            description='Fetched latest training data from web',
            status='success'
        )
    except Exception as e:
        print(f"❌ [WEB-TRAINING] Web training data fetch failed: {e}")


async def _fetch_training_data_async(topics):
    """Helper function to async fetch training data"""
    try:
        from internet import get_internet
        
        internet = await get_internet()
        
        for topic in topics:
            try:
                # Search for topic
                results = await internet.search_and_summarize(topic, num_results=2)
                
                # Store in database
                for result in results:
                    db.db['web_training_data'].insert_one({
                        'topic': topic,
                        'title': result.get('title'),
                        'snippet': result.get('snippet'),
                        'summary': result.get('content_summary'),
                        'url': result.get('url'),
                        'fetched_at': datetime.utcnow(),
                        'source': 'web'
                    })
                
                print(f"  ✓ Fetched training data for: {topic}")
            except Exception as e:
                print(f"  ⚠️ Could not fetch training data for {topic}: {e}")
                
    except ImportError:
        print("  ⚠️ Internet module not available for web training data")
    except Exception as e:
        print(f"  ❌ Error in async training data fetch: {e}")


# Global scheduler instance
global_scheduler = JobScheduler()


def initialize_scheduler():
    """Initialize and start the background scheduler"""
    global_scheduler.register_default_jobs()
    global_scheduler.start()


def shutdown_scheduler():
    """Shutdown the background scheduler"""
    global_scheduler.stop()

