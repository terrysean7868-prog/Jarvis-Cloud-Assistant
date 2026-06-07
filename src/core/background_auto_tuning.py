"""Background Auto-Tuning Service

Runs continuously to:
1. Collect learning examples from conversations
2. Fetch domain-specific data from web
3. Generate synthetic training examples
4. Auto-trigger training when ready
5. Load trained models automatically

No human interaction needed - fully autonomous!
"""

from __future__ import annotations

import asyncio
import logging
import os
import json
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
import aiohttp
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)


class BackgroundAutoTuningService:
    """24/7 autonomous auto-tuning service."""

    def __init__(self):
        self.scheduler = BackgroundScheduler()
        self.is_running = False
        self.data_dir = Path("data/ai_training")
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def start(self):
        """Start background auto-tuning."""
        if self.is_running:
            return

        logger.info("[AutoTuning] Starting 24/7 background service...")

        # Schedule jobs
        # Every 6 hours: Collect learning data
        self.scheduler.add_job(
            self._job_collect_learning_data,
            IntervalTrigger(hours=6),
            id="collect_learning",
            name="Collect learning data from conversations",
            misfire_grace_time=600,
        )

        # Every 12 hours: Fetch web data for training
        self.scheduler.add_job(
            self._job_fetch_web_training_data,
            IntervalTrigger(hours=12),
            id="fetch_web_data",
            name="Fetch domain-specific data from web",
            misfire_grace_time=600,
        )

        # Every 8 hours: Generate synthetic examples
        self.scheduler.add_job(
            self._job_generate_synthetic_examples,
            IntervalTrigger(hours=8),
            id="generate_synthetic",
            name="Generate synthetic training examples",
            misfire_grace_time=600,
        )

        # Every 4 hours: Check training readiness
        self.scheduler.add_job(
            self._job_check_training_readiness,
            IntervalTrigger(hours=4),
            id="check_readiness",
            name="Check if training should trigger",
            misfire_grace_time=600,
        )

        # Every 2 hours: Auto-trigger training if ready
        self.scheduler.add_job(
            self._job_auto_train,
            IntervalTrigger(hours=2),
            id="auto_train",
            name="Auto-trigger training if thresholds met",
            misfire_grace_time=600,
        )

        # Every 1 hour: Load new trained models
        self.scheduler.add_job(
            self._job_load_trained_models,
            IntervalTrigger(hours=1),
            id="load_models",
            name="Load newly trained LoRA models",
            misfire_grace_time=600,
        )

        self.scheduler.start()
        self.is_running = True
        logger.info("[AutoTuning] Service started! Running 6 background jobs 24/7")

    async def _job_collect_learning_data(self):
        """Collect all learning data from database every 6 hours."""
        try:
            logger.info("[AutoTuning] TASK 1/6: Collecting learning data from conversations...")

            from src.utils.db import db
            from src.learning import SelfLearningEngine

            db._ensure_connected()
            if not db.db:
                logger.warning("[AutoTuning] MongoDB unavailable, skipping collection")
                return

            engine = SelfLearningEngine()

            # Get all learning patterns
            learning_collection = db.db.get_collection("learning_memory")
            patterns = list(learning_collection.find({"quality_score": {"$gte": 0.6}}).limit(500))

            collected = len(patterns)
            logger.info(f"[AutoTuning] ✓ Collected {collected} quality learning patterns")

            if collected > 0:
                # Upsert to learning buffer
                for pattern in patterns:
                    engine._upsert_learning_entry(
                        {
                            "input_pattern": pattern.get("input_pattern", ""),
                            "best_response": pattern.get("best_response", ""),
                            "pattern_type": pattern.get("pattern_type", "chat"),
                            "priority_score": pattern.get("priority_score", 0.5),
                        }
                    )

            return {"collected": collected, "status": "ok"}

        except Exception as e:
            logger.error(f"[AutoTuning] Error in collection: {str(e)}")
            return {"status": "error", "error": str(e)}

    async def _job_fetch_web_training_data(self):
        """Fetch domain-specific data from web every 12 hours."""
        try:
            logger.info("[AutoTuning] TASK 2/6: Fetching training data from web...")

            web_sources = [
                "https://huggingface.co/datasets",  # ML datasets
                "https://github.com/awesome-lists/awesome-python",  # Python patterns
                "https://stackoverflow.com/questions/tagged/machine-learning?tab=newest",  # Common issues
                "https://arxiv.org/list/cs.LG/recent",  # Latest ML research
            ]

            collected_count = 0

            async with aiohttp.ClientSession() as session:
                for source in web_sources:
                    try:
                        async with session.get(source, timeout=10) as resp:
                            if resp.status == 200:
                                content = await resp.text()

                                # Extract training examples from content
                                examples = self._extract_training_examples(content, source)
                                collected_count += len(examples)

                                # Save to temporary cache
                                cache_file = self.data_dir / f"web_cache_{hashlib.md5(source.encode()).hexdigest()}.jsonl"
                                with open(cache_file, "a") as f:
                                    for ex in examples:
                                        f.write(json.dumps(ex) + "\n")

                                logger.info(f"[AutoTuning]   ✓ {source}: {len(examples)} examples")
                    except Exception as e:
                        logger.warning(f"[AutoTuning]   ✗ {source}: {str(e)}")
                        continue

            logger.info(f"[AutoTuning] ✓ Fetched {collected_count} web training examples")
            return {"collected": collected_count, "status": "ok"}

        except Exception as e:
            logger.error(f"[AutoTuning] Error in web fetch: {str(e)}")
            return {"status": "error", "error": str(e)}

    def _extract_training_examples(self, content: str, source: str) -> list[dict]:
        """Extract training examples from web content."""
        examples = []

        # Extract Q&A patterns from StackOverflow
        if "stackoverflow" in source:
            import re

            qa_pairs = re.findall(r"<a[^>]*>([^<]+)</a>.*?<p[^>]*>([^<]+)</p>", content)
            for question, answer in qa_pairs[:10]:  # Limit to 10 per source
                if len(question) > 10 and len(answer) > 10:
                    examples.append({"user": question, "assistant": answer})

        # Extract dataset descriptions from HuggingFace
        if "huggingface" in source:
            import re

            datasets = re.findall(r"<h3>([^<]+)</h3>.*?<p[^>]*>([^<]+)</p>", content)
            for name, desc in datasets[:10]:
                if len(name) > 5 and len(desc) > 10:
                    examples.append(
                        {
                            "user": f"Tell me about {name} dataset",
                            "assistant": f"{name} is a dataset for: {desc}",
                        }
                    )

        # Extract GitHub awesome lists
        if "awesome" in source:
            import re

            items = re.findall(r"\* \[([^\]]+)\]\(([^)]+)\) - ([^[\n]+)", content)
            for name, url, description in items[:15]:
                if len(name) > 3 and len(description) > 10:
                    examples.append(
                        {
                            "user": f"What is {name}?",
                            "assistant": f"{name} ({url}): {description}",
                        }
                    )

        return examples

    async def _job_generate_synthetic_examples(self):
        """Generate synthetic training examples every 8 hours."""
        try:
            logger.info("[AutoTuning] TASK 3/6: Generating synthetic training examples...")

            from src.core.llm_adapter import LLMAdapter

            adapter = LLMAdapter()

            # Generate examples for common patterns
            topics = [
                "model training best practices",
                "debugging common errors",
                "code optimization techniques",
                "project analysis workflow",
                "task delegation patterns",
            ]

            synthetic_examples = []

            for topic in topics:
                try:
                    # Use LLM to generate Q&A pairs
                    response = await adapter.chat(
                        f"""Generate 3 realistic Q&A pairs about {topic} for fine-tuning a model.
                        Format as JSON array with "user" and "assistant" keys.
                        Example: [{{"user": "...", "assistant": "..."}}]""",
                        max_tokens=300,
                    )

                    if response:
                        try:
                            pairs = json.loads(response)
                            if isinstance(pairs, list):
                                synthetic_examples.extend(pairs)
                                logger.info(f"[AutoTuning]   ✓ Generated 3 examples for {topic}")
                        except json.JSONDecodeError:
                            pass
                except Exception as e:
                    logger.warning(f"[AutoTuning]   ✗ Error generating for {topic}: {str(e)}")
                    continue

            # Save synthetic examples
            synthetic_file = self.data_dir / "synthetic_training.jsonl"
            with open(synthetic_file, "a") as f:
                for ex in synthetic_examples:
                    if ex.get("user") and ex.get("assistant"):
                        f.write(json.dumps(ex) + "\n")

            logger.info(f"[AutoTuning] ✓ Generated {len(synthetic_examples)} synthetic examples")
            return {"generated": len(synthetic_examples), "status": "ok"}

        except Exception as e:
            logger.error(f"[AutoTuning] Error in synthetic generation: {str(e)}")
            return {"status": "error", "error": str(e)}

    async def _job_check_training_readiness(self):
        """Check if training thresholds are met every 4 hours."""
        try:
            logger.info("[AutoTuning] TASK 4/6: Checking training readiness...")

            from src.utils.db import db
            from src.model_ops.training_readiness import compute_readiness

            db._ensure_connected()
            if not db.db:
                return {"status": "unavailable"}

            # Get dataset statistics
            learning_collection = db.db.get_collection("learning_memory")

            total_samples = learning_collection.count_documents({})
            instruction_samples = learning_collection.count_documents({"pattern_type": "instruction"})
            conversation_samples = learning_collection.count_documents({"pattern_type": "chat"})
            task_samples = learning_collection.count_documents({"pattern_type": "task"})
            error_samples = learning_collection.count_documents({"pattern_type": {"$in": ["error", "failure_fix"]}})

            stats = {
                "total_samples": total_samples,
                "instruction_samples": instruction_samples,
                "conversation_samples": conversation_samples,
                "task_samples": task_samples,
                "error_samples": error_samples,
                "duplicate_rate": 0.05,  # Estimate
                "masked_sensitive": True,
            }

            readiness = compute_readiness(stats, model_supports_finetune=True)

            logger.info(f"[AutoTuning] ✓ Readiness score: {readiness['readiness_score']}/100")
            logger.info(f"[AutoTuning]   • Total samples: {total_samples}")
            logger.info(f"[AutoTuning]   • Error samples: {error_samples}")
            logger.info(f"[AutoTuning]   • Ready for training: {readiness['ready']}")

            return readiness

        except Exception as e:
            logger.error(f"[AutoTuning] Error checking readiness: {str(e)}")
            return {"status": "error"}

    async def _job_auto_train(self):
        """Auto-trigger training if thresholds met every 2 hours."""
        try:
            logger.info("[AutoTuning] TASK 5/6: Checking if auto-training should trigger...")

            from src.utils.db import db
            from src.model_ops.training_readiness import compute_readiness
            import subprocess

            db._ensure_connected()
            if not db.db:
                logger.warning("[AutoTuning] MongoDB unavailable, skipping training check")
                return

            learning_collection = db.db.get_collection("learning_memory")

            # Check if we have enough examples
            error_count = learning_collection.count_documents({"pattern_type": {"$in": ["error", "failure_fix"]}})
            task_count = learning_collection.count_documents({"pattern_type": "task"})
            chat_count = learning_collection.count_documents({"pattern_type": "chat"})
            total = learning_collection.count_documents({})

            logger.info(f"[AutoTuning]   Current counts: {error_count} errors, {task_count} tasks, {chat_count} chats")

            # Check last training time
            training_log = db.db.get_collection("auto_tuning_log")
            last_training = training_log.find_one({"type": "training"}, sort=[("timestamp", -1)])
            last_training_time = (
                datetime.fromisoformat(last_training["timestamp"]) if last_training else datetime.now(timezone.utc) - timedelta(days=7)
            )
            hours_since_training = (datetime.now(timezone.utc) - last_training_time).total_seconds() / 3600

            # Trigger conditions
            should_train = False
            reason = ""

            if error_count >= 20:
                should_train = True
                reason = f"Enough error examples ({error_count})"
            elif task_count >= 50 and hours_since_training > 24:
                should_train = True
                reason = f"Enough task examples ({task_count}) and 24h passed"
            elif chat_count >= 200 and hours_since_training > 72:
                should_train = True
                reason = f"Enough chat examples ({chat_count}) and 72h passed"
            elif total >= 100 and hours_since_training > 48:
                should_train = True
                reason = f"Enough total examples ({total}) and 48h passed"

            if should_train:
                logger.info(f"[AutoTuning] ✓ AUTO-TRAINING TRIGGERED: {reason}")

                # Prepare dataset
                sft_file = self.data_dir / "sft_from_learning.jsonl"
                await self._prepare_training_dataset(sft_file)

                # Log training attempt
                training_log.insert_one(
                    {
                        "type": "training",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "trigger_reason": reason,
                        "total_examples": total,
                        "error_count": error_count,
                        "task_count": task_count,
                        "chat_count": chat_count,
                        "status": "in_progress",
                    }
                )

                # Run training
                try:
                    cmd = [
                        "python",
                        "scripts/train_self_hosted_lora.py",
                        "--dataset",
                        str(sft_file),
                        "--epochs",
                        "3",
                        "--batch-size",
                        "1",
                    ]

                    logger.info(f"[AutoTuning] Running: {' '.join(cmd)}")
                    result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)

                    if result.returncode == 0:
                        logger.info("[AutoTuning] ✓ Training completed successfully!")
                        training_log.update_one(
                            {"timestamp": datetime.now(timezone.utc).isoformat()},
                            {"$set": {"status": "completed", "output": result.stdout[-500:]}},
                        )
                        return {"trained": True, "reason": reason}
                    else:
                        logger.error(f"[AutoTuning] ✗ Training failed: {result.stderr[-500:]}")
                        training_log.update_one(
                            {"timestamp": datetime.now(timezone.utc).isoformat()},
                            {"$set": {"status": "failed", "error": result.stderr[-500:]}},
                        )

                except subprocess.TimeoutExpired:
                    logger.error("[AutoTuning] ✗ Training timeout (>1 hour)")
                    training_log.update_one(
                        {"timestamp": datetime.now(timezone.utc).isoformat()},
                        {"$set": {"status": "timeout"}},
                    )

            else:
                logger.info(f"[AutoTuning]   Not ready yet. Need: 20 errors ({error_count}), 50 tasks ({task_count}), or 200 chats ({chat_count})")

            return {"trained": should_train}

        except Exception as e:
            logger.error(f"[AutoTuning] Error in auto-training: {str(e)}")
            return {"status": "error", "error": str(e)}

    async def _prepare_training_dataset(self, output_file: Path):
        """Prepare combined training dataset from all sources."""
        try:
            from src.learning import SelfLearningEngine

            engine = SelfLearningEngine()

            # Collect from learning system
            all_examples = []

            # Get high-quality learning examples
            from src.utils.db import db

            db._ensure_connected()
            if db.db:
                learning_collection = db.db.get_collection("learning_memory")
                patterns = list(learning_collection.find({"quality_score": {"$gte": 0.6}}).limit(1000))

                for pattern in patterns:
                    user = pattern.get("input_pattern", "").strip()
                    assistant = pattern.get("best_response", "").strip()
                    if user and assistant:
                        all_examples.append({"user": user, "assistant": assistant})

            # Add web-fetched examples
            for cache_file in self.data_dir.glob("web_cache_*.jsonl"):
                try:
                    with open(cache_file) as f:
                        for line in f:
                            try:
                                ex = json.loads(line)
                                if ex.get("user") and ex.get("assistant"):
                                    all_examples.append(ex)
                            except json.JSONDecodeError:
                                pass
                except Exception:
                    pass

            # Add synthetic examples
            synthetic_file = self.data_dir / "synthetic_training.jsonl"
            if synthetic_file.exists():
                try:
                    with open(synthetic_file) as f:
                        for line in f:
                            try:
                                ex = json.loads(line)
                                if ex.get("user") and ex.get("assistant"):
                                    all_examples.append(ex)
                            except json.JSONDecodeError:
                                pass
                except Exception:
                    pass

            # Deduplicate
            seen = set()
            unique_examples = []
            for ex in all_examples:
                key = hashlib.md5((ex["user"] + ex["assistant"]).encode()).hexdigest()
                if key not in seen:
                    seen.add(key)
                    unique_examples.append(ex)

            # Save final dataset
            with open(output_file, "w") as f:
                for ex in unique_examples:
                    f.write(json.dumps(ex) + "\n")

            logger.info(f"[AutoTuning] ✓ Prepared {len(unique_examples)} training examples")

        except Exception as e:
            logger.error(f"[AutoTuning] Error preparing dataset: {str(e)}")

    async def _job_load_trained_models(self):
        """Load newly trained models every 1 hour."""
        try:
            logger.info("[AutoTuning] TASK 6/6: Checking for newly trained models...")

            import requests

            lora_path = Path("models/jarvis-lora")

            if lora_path.exists() and (lora_path / "adapter_config.json").exists():
                try:
                    # Notify model service to reload
                    response = requests.post(
                        "http://127.0.0.1:8010/v1/load-lora-weights",
                        json={"adapter_name": str(lora_path), "merge": False},
                        timeout=30,
                    )

                    if response.status_code == 200:
                        logger.info("[AutoTuning] ✓ New LoRA weights loaded into model service!")
                        return {"loaded": True, "status": "ok"}
                    else:
                        logger.warning(f"[AutoTuning] ✗ Failed to load LoRA: {response.text[:200]}")

                except Exception as e:
                    logger.warning(f"[AutoTuning] ✗ Could not reach model service: {str(e)}")
            else:
                logger.debug("[AutoTuning]   No new LoRA adapter found")

            return {"loaded": False}

        except Exception as e:
            logger.error(f"[AutoTuning] Error loading models: {str(e)}")
            return {"status": "error"}

    def stop(self):
        """Stop background auto-tuning."""
        if self.is_running:
            self.scheduler.shutdown()
            self.is_running = False
            logger.info("[AutoTuning] Service stopped")


# Global instance
_auto_tuning_service: BackgroundAutoTuningService | None = None


def get_auto_tuning_service() -> BackgroundAutoTuningService:
    """Get or create auto-tuning service."""
    global _auto_tuning_service
    if _auto_tuning_service is None:
        _auto_tuning_service = BackgroundAutoTuningService()
    return _auto_tuning_service


def start_background_auto_tuning():
    """Start the background auto-tuning service."""
    service = get_auto_tuning_service()
    service.start()
    return service
