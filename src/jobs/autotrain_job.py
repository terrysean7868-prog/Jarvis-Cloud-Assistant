import subprocess
import sys
from datetime import datetime
from pathlib import Path
from src.utils.db import db

def run_auto_training_job():
    """
    Background job to build datasets from recent voice/chat logs 
    and kick off the fine-tuning script.
    """
    try:
        print(f"\n🚀 [AUTOTRAIN] Starting continuous self-training at {datetime.utcnow().isoformat()}")
        
        # 1. Build local datasets from DB (including voice logs stored in learning_memory)
        root_dir = Path(__file__).resolve().parents[2]
        ds_dir = root_dir / "data" / "ai_training" / "datasets"
        
        try:
            from src.ai_training.dataset_builder import build_datasets
            print("  -> Exporting latest local voice & chat logs to dataset...")
            build_datasets(db, output_dir=ds_dir)
        except Exception as e:
            print(f"  ⚠️ Could not build local dataset: {e}")
            print("  -> Proceeding with HuggingFace dataset only...")
        
        # 2. Trigger the fine-tuning script in a subprocess
        script_path = root_dir / "scripts" / "train_model_job.py"
        
        if not script_path.exists():
            print("  ❌ Training script not found at:", script_path)
            return

        print("  -> Initiating HuggingFace data merge and model fine-tuning...")
        # Run the training script in a completely detached process to avoid memory 
        # bloat in the main Render.com worker thread.
        process = subprocess.Popen(
            [sys.executable, str(script_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True
        )
        
        print(f"  ✅ [AUTOTRAIN] Training job dispatched with PID {process.pid}. It will run in the background.")
        
        if getattr(db, "db", None) is not None:
            db.save_system_event(
                event_type='auto_training_started',
                description='Dispatched background fine-tuning job using HuggingFace and local voice datasets',
                status='success'
            )
            
    except Exception as e:
        print(f"❌ [AUTOTRAIN] Auto-training failed to start: {e}")
