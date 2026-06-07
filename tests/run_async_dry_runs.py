import sys
import os
import asyncio
import json

# Ensure repository root is on sys.path for imports when running as a script
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.core.jarvis_brain import JarvisBrain
from src.core.llm_adapter import LLMAdapter
from src.core.executor import ActionExecutor


async def run():
    brain = JarvisBrain(LLMAdapter())
    executor = ActionExecutor(brain)

    print("\n--- Running finetune_model dry-run ---")
    actions = [{"type": "finetune_model", "dataset": "daily_dialog", "max_steps": 10}]
    res = await executor.process_actions(actions, user="tester")
    print(json.dumps(res, indent=2))

    print("\n--- Running ingest_hf_dataset dry-run ---")
    actions = [{"type": "ingest_hf_dataset", "dataset": "wikitext", "max_items": 5}]
    res = await executor.process_actions(actions, user="tester")
    print(json.dumps(res, indent=2))

if __name__ == '__main__':
    asyncio.run(run())
