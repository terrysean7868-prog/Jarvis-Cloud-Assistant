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


_progressive_llm_update_running = False
_last_progressive_llm_update_report: dict | None = None


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
            print("[OK] Background Job Scheduler started")

    def stop(self):
        """Stop the background scheduler"""
        if self.is_running:
            self.scheduler.shutdown()
            self.is_running = False
            print("[OK] Background Job Scheduler stopped")

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
            print(f"[OK] Job '{job_id}' scheduled every {interval_seconds}s")
        except Exception as e:
            print(f"[ERR] Error adding job: {e}")

    def register_default_jobs(self):
        """Register all default background jobs"""
        from src.config import runtime_defaults as rd
        enable_git_sync = bool(rd.AUTO_GIT_SYNC)
        enable_db_maintenance = bool(rd.ENABLE_DB_MAINTENANCE)
        enable_web_training = bool(rd.ENABLE_WEB_TRAINING_JOB)
        enable_wiki_training = bool(rd.ENABLE_WIKI_TRAINING_JOB)
        enable_background_analysis = bool(rd.ENABLE_BACKGROUND_ANALYSIS_JOB)
        enable_local_reasoner_prewarm = bool(getattr(rd, "ENABLE_LOCAL_REASONER_PREWARM_JOB", False))
        enable_memory_optimization = bool(rd.ENABLE_MEMORY_OPTIMIZATION)
        enable_training_data_job = bool(rd.ENABLE_TRAINING_DATA_JOB)
        enable_progressive_llm_update_job = bool(getattr(rd, "ENABLE_PROGRESSIVE_LLM_UPDATE_JOB", False))

        # GitHub auto-sync every 5 minutes (off by default for hosted deploys)
        if enable_git_sync:
            self.add_job(
                auto_sync_github,
                interval_seconds=300,
                job_id="github_sync"
            )

        # Database cleanup every hour
        if enable_db_maintenance:
            self.add_job(
                cleanup_database,
                interval_seconds=3600,
                job_id="db_cleanup"
            )

        # Fetch training data every 24 hours (stub; off by default)
        if enable_training_data_job:
            self.add_job(
                update_training_data,
                interval_seconds=86400,
                job_id="training_data_update"
            )
        
        # Fetch web training data every 12 hours
        if enable_web_training:
            self.add_job(
                fetch_web_training_data,
                interval_seconds=43200,
                job_id="web_training_fetch"
            )

        # Fetch Wikipedia summaries (off by default; opt-in). Default cadence: weekly.
        if enable_wiki_training:
            try:
                interval_seconds = int(rd.WIKI_TRAINING_INTERVAL_SECONDS)
            except Exception:
                interval_seconds = 7 * 86400
            interval_seconds = max(3600, min(interval_seconds, 30 * 86400))
            self.add_job(
                fetch_wikipedia_training_data,
                interval_seconds=interval_seconds,
                job_id="wiki_training_fetch"
            )

        # Memory optimization every 6 hours (off by default; can be expensive)
        if enable_memory_optimization:
            self.add_job(
                optimize_memory,
                interval_seconds=21600,
                job_id="memory_optimization"
            )

        # Background analysis/enrichment for stored web knowledge.
        # Default cadence: every 30 minutes (override via env).
        if enable_background_analysis:
            try:
                interval_seconds = int(rd.BACKGROUND_ANALYSIS_INTERVAL_SECONDS)
            except Exception:
                interval_seconds = 1800
            interval_seconds = max(300, min(interval_seconds, 6 * 3600))
            self.add_job(
                background_analyze_web_training_data,
                interval_seconds=interval_seconds,
                job_id="background_web_analysis"
            )

        # Local reasoner prewarm (daily): analysis-first + web fetch to improve cold-start UX.
        if enable_local_reasoner_prewarm:
            try:
                interval_seconds = int(getattr(rd, "LOCAL_REASONER_PREWARM_INTERVAL_SECONDS", 86400))
            except Exception:
                interval_seconds = 86400
            interval_seconds = max(6 * 3600, min(interval_seconds, 7 * 86400))
            self.add_job(
                refresh_local_reasoner_prewarm,
                interval_seconds=interval_seconds,
                job_id="local_reasoner_prewarm"
            )

        # Progressive daily LLM/brain quality update (self-update pipeline).
        if enable_progressive_llm_update_job:
            try:
                interval_seconds = int(getattr(rd, "PROGRESSIVE_LLM_UPDATE_INTERVAL_SECONDS", 86400))
            except Exception:
                interval_seconds = 86400
            interval_seconds = max(6 * 3600, min(interval_seconds, 7 * 86400))
            self.add_job(
                progressive_llm_brain_update,
                interval_seconds=interval_seconds,
                job_id="progressive_llm_brain_update",
            )


def _db_available() -> bool:
    """Best-effort check for MongoDB availability."""
    try:
        db._ensure_connected()
    except Exception:
        return False
    return getattr(db, "db", None) is not None


# ===== Default Background Jobs =====

def auto_sync_github():
    """Auto-sync code changes to GitHub"""
    try:
        print(f"\n🔄 [AUTO-SYNC] Starting GitHub sync at {datetime.utcnow().isoformat()}")
        git_sync(repo_path=".")
        
        # Log the sync event
        if _db_available():
            db.save_system_event(
                event_type='auto_sync_github',
                description='Automatic GitHub sync completed',
                status='success',
                details={'timestamp': datetime.utcnow().isoformat()}
            )
        print("✅ [AUTO-SYNC] GitHub sync completed")
    except Exception as e:
        print(f"❌ [AUTO-SYNC] GitHub sync failed: {e}")
        if _db_available():
            db.save_system_event(
                event_type='auto_sync_error',
                description=f'GitHub auto-sync failed: {str(e)}',
                status='error'
            )


def cleanup_database():
    """Clean up old data from MongoDB"""
    try:
        print(f"\n🧹 [CLEANUP] Starting database cleanup at {datetime.utcnow().isoformat()}")

        if not _db_available():
            print("⚠️  [CLEANUP] MongoDB not connected; skipping")
            return
        
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
        if _db_available():
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
        
        if _db_available():
            db.save_system_event(
                event_type='training_data_update',
                description='Periodic training data update completed',
                status='success',
                details={'timestamp': datetime.utcnow().isoformat()}
            )
        print("✅ [TRAINING] Training data update completed")
    except Exception as e:
        print(f"❌ [TRAINING] Training data update failed: {e}")
        if _db_available():
            db.save_system_event(
                event_type='training_update_error',
                description=f'Training data update failed: {str(e)}',
                status='error'
            )


def optimize_memory():
    """Optimize memory and database indexes"""
    try:
        print(f"\n⚡ [OPTIMIZE] Starting memory optimization at {datetime.utcnow().isoformat()}")

        if not _db_available():
            print("⚠️  [OPTIMIZE] MongoDB not connected; skipping")
            return
        
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
        
        if _db_available():
            db.save_system_event(
                event_type='web_training_fetch',
                description='Fetched latest training data from web',
                status='success'
            )
    except Exception as e:
        print(f"❌ [WEB-TRAINING] Web training data fetch failed: {e}")


def fetch_wikipedia_training_data():
    """Seed compact Wikipedia summaries into web_training_data.

    This is a small, bounded knowledge cache to improve offline synthesis and context.
    It does NOT mirror full Wikipedia pages.

    Configuration:
    - Controlled via in-code defaults in src/config/runtime_defaults.py
    """
    try:
        print(f"\n📚 [WIKI-TRAINING] Starting Wikipedia training fetch at {datetime.utcnow().isoformat()}")

        from src.config import runtime_defaults as rd

        raw_topics = (rd.WIKI_TRAINING_TOPICS or "").strip()
        if raw_topics:
            topics = [t.strip() for t in raw_topics.split(",") if (t or "").strip()]
        else:
            topics = [
                "human psychology",
                "cognitive bias",
                "cognitive dissonance",
                "confirmation bias",
                "memory",
                "attention",
                "emotion",
                "motivation",
                "social psychology",
                "behavioral economics",
                "history of science",
            ]

        max_pages = int(rd.WIKI_TRAINING_MAX_PAGES)
        max_pages = max(1, min(max_pages, 5))

        lang = (rd.WIKI_TRAINING_LANG or "en").strip().lower() or "en"

        # Run async fetching (bounded)
        saved = asyncio.run(_fetch_wikipedia_training_data_async(topics[:30], lang=lang, max_pages=max_pages))

        print(f"✅ [WIKI-TRAINING] Wikipedia training fetch completed (saved: {saved})")
        if _db_available():
            db.save_system_event(
                event_type="wiki_training_fetch",
                description=f"Fetched Wikipedia summaries (saved: {saved})",
                status="success",
                details={"topics": topics[:30], "max_pages": max_pages, "lang": lang},
            )
    except Exception as e:
        print(f"❌ [WIKI-TRAINING] Wikipedia training fetch failed: {e}")
        if _db_available():
            db.save_system_event(
                event_type="wiki_training_error",
                description=f"Wikipedia training fetch failed: {str(e)}",
                status="error",
            )


def background_analyze_web_training_data():
    """Enrich stored web_training_data items with compact tags/insights.

    This is a lightweight, non-LLM job meant to improve retrieval quality and
    reduce response time. It writes only small derived fields:
    - analysis_tags: list[str]
    - analysis_insight: str
    - analysis_at: datetime
    - analysis_version: int

    Configuration:
    - Controlled via in-code defaults in src/config/runtime_defaults.py
    """
    try:
        print(f"\n🧠 [BG-ANALYSIS] Starting web knowledge analysis at {datetime.utcnow().isoformat()}")

        if not _db_available():
            print("⚠️  [BG-ANALYSIS] MongoDB not connected; skipping")
            return

        from src.config import runtime_defaults as rd

        batch = int(rd.BACKGROUND_ANALYSIS_BATCH)
        batch = max(1, min(batch, 200))

        from src.core.background_analysis import analyze_web_training_item
        from src.core.web_training_schema import WEB_TRAINING_DOC_TYPE, WEB_TRAINING_SCHEMA_VERSION, jarvis_identity

        col = db.db.web_training_data
        # Analyze docs missing analysis or with old version.
        query = {"$or": [{"analysis_at": {"$exists": False}}, {"analysis_version": {"$ne": 1}}]}
        items = list(col.find(query).sort("fetched_at", -1).limit(batch))

        updated = 0
        ident = jarvis_identity()
        for it in items:
            try:
                topic = (it.get("topic") or "").strip()
                title = (it.get("title") or "").strip()
                snippet = (it.get("snippet") or "").strip()
                summary = (it.get("summary") or "").strip()
                if not (topic and (summary or snippet or title)):
                    continue

                payload = analyze_web_training_item(topic=topic, title=title, snippet=snippet, summary=summary)
                payload = payload or {}
                payload["analysis_at"] = datetime.utcnow()
                payload["analysis_version"] = 1

                # Backfill canonical schema/identity fields for older documents.
                payload.setdefault("doc_type", WEB_TRAINING_DOC_TYPE)
                payload.setdefault("schema_version", WEB_TRAINING_SCHEMA_VERSION)
                payload.setdefault("producer_assistant_id", ident.get("assistant_id", "jarvis"))
                payload.setdefault("producer_instance_id", ident.get("instance_id", "local"))
                payload["updated_at"] = datetime.utcnow()

                col.update_one({"_id": it.get("_id")}, {"$set": payload})
                updated += 1
            except Exception:
                continue

        print(f"✅ [BG-ANALYSIS] Updated {updated} items")
        db.save_system_event(
            event_type="background_web_analysis",
            description=f"Enriched web_training_data items (updated: {updated})",
            status="success",
            details={"updated": updated, "batch": batch},
        )
    except Exception as e:
        print(f"❌ [BG-ANALYSIS] Failed: {e}")
        if _db_available():
            db.save_system_event(
                event_type="background_web_analysis_error",
                description=f"Background web analysis failed: {str(e)}",
                status="error",
            )


def refresh_local_reasoner_prewarm():
    """Daily prewarm for local reasoner with analysis-first strategy."""
    try:
        print(f"\n🧩 [PREWARM] Starting local reasoner prewarm at {datetime.utcnow().isoformat()}")

        from src.config import runtime_defaults as rd
        from src.core.local_reasoner_prewarm import prewarm_local_reasoner_from_web

        # Systematic pipeline: analyze existing web corpus first, then fetch fresh pages.
        if bool(getattr(rd, "LOCAL_REASONER_PREWARM_ANALYSIS_FIRST", True)):
            try:
                background_analyze_web_training_data()
            except Exception:
                pass

        max_q = max(1, min(int(getattr(rd, "LOCAL_REASONER_PREWARM_MAX_QUERIES", 8)), 20))
        per_q = max(1, min(int(getattr(rd, "LOCAL_REASONER_PREWARM_RESULTS_PER_QUERY", 4)), 8))

        report = asyncio.run(
            prewarm_local_reasoner_from_web(max_queries=max_q, results_per_query=per_q)
        )

        print(
            "✅ [PREWARM] Local reasoner prewarm completed "
            f"(queries={report.get('queries')}, results={report.get('results_seen')}, aliases={report.get('aliases_added')})"
        )
        if _db_available():
            db.save_system_event(
                event_type="local_reasoner_prewarm",
                description="Local reasoner prewarm refresh completed",
                status="success",
                details=report,
            )
    except Exception as e:
        print(f"❌ [PREWARM] Local reasoner prewarm failed: {e}")
        if _db_available():
            db.save_system_event(
                event_type="local_reasoner_prewarm_error",
                description=f"Local reasoner prewarm failed: {str(e)}",
                status="error",
            )


def progressive_llm_brain_update():
    """Run incremental daily self-update on LLM + brain files.

    This uses the same guarded update pipeline used by admin operations:
    - code generation
    - validation gate
    - backup + rollback
    - audit trail
    """
    global _progressive_llm_update_running
    global _last_progressive_llm_update_report
    if _progressive_llm_update_running:
        print("⚠️  [LLM-PROGRESSIVE] Previous run still in progress; skipping")
        return

    _progressive_llm_update_running = True
    try:
        from src.config import runtime_defaults as rd
        from src.utils.self_update import self_update_file

        started_at = datetime.utcnow().isoformat()
        print(f"\n🧠 [LLM-PROGRESSIVE] Starting daily LLM/brain update at {started_at}")

        actor = str(getattr(rd, "PROGRESSIVE_LLM_UPDATE_ACTOR", "scheduler") or "scheduler").strip() or "scheduler"
        description = str(getattr(rd, "PROGRESSIVE_LLM_UPDATE_DESCRIPTION", "") or "").strip()
        if not description:
            description = (
                "Apply a small, safe, incremental improvement to reasoning quality and "
                "response consistency while preserving APIs and existing behavior."
            )

        targets_raw = str(getattr(rd, "PROGRESSIVE_LLM_UPDATE_TARGET_FILES_CSV", "") or "").strip()
        if targets_raw:
            targets = [x.strip() for x in targets_raw.split(",") if (x or "").strip()]
        else:
            targets = ["src/core/llm_adapter.py", "src/core/jarvis_brain.py"]

        dry_run = bool(getattr(rd, "PROGRESSIVE_LLM_UPDATE_DRY_RUN", False))
        auto_install_deps = bool(getattr(rd, "PROGRESSIVE_LLM_UPDATE_AUTO_INSTALL_DEPS", False))

        results = []
        success_count = 0
        for target in targets:
            try:
                res = self_update_file(
                    description,
                    target,
                    actor=actor,
                    auto_install_deps=auto_install_deps,
                    dry_run=dry_run,
                )
            except Exception as e:
                res = {"status": "error", "message": str(e), "path": target}

            results.append({"target": target, "result": res})
            if isinstance(res, dict) and res.get("status") == "success":
                success_count += 1

        status_text = "success" if success_count == len(targets) else ("partial" if success_count > 0 else "error")
        changed_files: list[str] = []
        for item in results:
            try:
                r = item.get("result") if isinstance(item, dict) else {}
                if not isinstance(r, dict):
                    continue
                if r.get("status") != "success":
                    continue
                p = (r.get("path") or item.get("target") or "").strip()
                if p and p not in changed_files:
                    changed_files.append(p)
            except Exception:
                continue

        _last_progressive_llm_update_report = {
            "event_type": "progressive_llm_update",
            "started_at": started_at,
            "completed_at": datetime.utcnow().isoformat(),
            "status": status_text,
            "targets": targets,
            "changed_files": changed_files,
            "dry_run": dry_run,
            "auto_install_deps": auto_install_deps,
            "actor": actor,
            "results": results,
        }
        print(
            "✅ [LLM-PROGRESSIVE] Completed"
            if status_text == "success"
            else "⚠️  [LLM-PROGRESSIVE] Completed with issues"
        )

        if _db_available():
            db.save_system_event(
                event_type="progressive_llm_update",
                description=(
                    f"Daily LLM/brain progressive update finished: {success_count}/{len(targets)} targets updated"
                ),
                status=status_text,
                details={
                    "targets": targets,
                    "changed_files": changed_files,
                    "started_at": started_at,
                    "completed_at": _last_progressive_llm_update_report.get("completed_at"),
                    "dry_run": dry_run,
                    "auto_install_deps": auto_install_deps,
                    "actor": actor,
                    "results": results,
                },
            )
    except Exception as e:
        print(f"❌ [LLM-PROGRESSIVE] Failed: {e}")
        _last_progressive_llm_update_report = {
            "event_type": "progressive_llm_update_error",
            "started_at": datetime.utcnow().isoformat(),
            "completed_at": datetime.utcnow().isoformat(),
            "status": "error",
            "targets": [],
            "changed_files": [],
            "results": [],
            "error": str(e),
        }
        if _db_available():
            db.save_system_event(
                event_type="progressive_llm_update_error",
                description=f"Daily LLM/brain progressive update failed: {str(e)}",
                status="error",
            )
    finally:
        _progressive_llm_update_running = False


def get_progressive_llm_update_report() -> dict:
    """Return latest progressive LLM update report.

    Preference order:
    1) In-memory last report from this process
    2) Latest system_events entry from DB
    """
    global _last_progressive_llm_update_report

    if isinstance(_last_progressive_llm_update_report, dict) and _last_progressive_llm_update_report:
        return {
            "status": "success",
            "source": "memory",
            "report": _last_progressive_llm_update_report,
        }

    if _db_available():
        try:
            col = db.db.system_events
            doc = col.find_one(
                {"event_type": {"$in": ["progressive_llm_update", "progressive_llm_update_error"]}},
                sort=[("timestamp", -1)],
            )
            if doc:
                details = doc.get("details") if isinstance(doc.get("details"), dict) else {}
                report = {
                    "event_type": doc.get("event_type"),
                    "status": doc.get("status") or details.get("status") or "unknown",
                    "started_at": details.get("started_at"),
                    "completed_at": details.get("completed_at") or (
                        doc.get("timestamp").isoformat() if hasattr(doc.get("timestamp"), "isoformat") else doc.get("timestamp")
                    ),
                    "targets": details.get("targets") or [],
                    "changed_files": details.get("changed_files") or [],
                    "dry_run": details.get("dry_run"),
                    "auto_install_deps": details.get("auto_install_deps"),
                    "actor": details.get("actor"),
                    "results": details.get("results") or [],
                    "description": doc.get("description"),
                }
                return {"status": "success", "source": "db", "report": report}
        except Exception as e:
            return {"status": "error", "message": str(e), "report": None}

    return {
        "status": "success",
        "source": "none",
        "report": None,
        "message": "No progressive LLM update report yet",
    }


async def _fetch_wikipedia_training_data_async(topics, *, lang: str = "en", max_pages: int = 2) -> int:
    """Helper: fetch Wikipedia summaries and store compact items to MongoDB."""
    try:
        # Ensure DB connection (best-effort)
        try:
            db._ensure_connected()
        except Exception:
            pass
        if getattr(db, "db", None) is None:
            print("  ⚠️ MongoDB not connected; skipping Wikipedia training store")
            return 0

        from src.internet.wikipedia_client import wikipedia_topic_summaries

        saved = 0
        for topic in topics or []:
            t = (topic or "").strip()
            if not t:
                continue
            try:
                summaries = await wikipedia_topic_summaries(t, lang=lang, max_pages=max_pages)
                for s in summaries:
                    try:
                        snippet = (s.description or "").strip()
                        if snippet and s.extract:
                            snippet = (snippet + ": " + s.extract).strip()
                        else:
                            snippet = s.extract

                        db.save_web_training_item(
                            topic=t,
                            title=s.title,
                            snippet=(snippet or "")[:500],
                            summary=(s.extract or "")[:1200],
                            url=s.url,
                            source="wikipedia_api",
                        )
                        saved += 1
                    except Exception:
                        pass
                print(f"  ✓ Wikipedia: {t} ({len(summaries)} pages)")
            except Exception as e:
                print(f"  ⚠️ Wikipedia fetch failed for '{t}': {e}")
                continue
        return saved
    except Exception:
        return 0


async def _fetch_training_data_async(topics):
    """Helper function to async fetch training data"""
    try:
        from src.internet.internet import get_internet
        
        internet = await get_internet()

        # Ensure DB connection (best-effort)
        try:
            db._ensure_connected()
        except Exception:
            pass
        if getattr(db, "db", None) is None:
            print("  ⚠️ MongoDB not connected; skipping web training data store")
            return
        
        for topic in topics:
            try:
                # Search for topic
                results = await internet.search_and_summarize(topic, num_results=2)
                
                # Store in database
                for result in results:
                    try:
                        db.save_web_training_item(
                            topic=topic,
                            title=result.get('title'),
                            snippet=result.get('snippet'),
                            summary=(result.get('content_summary') or result.get('summary')),
                            url=result.get('url'),
                            source='web',
                        )
                    except Exception:
                        # Ignore per-item failures
                        pass
                
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

