import argparse
import os

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    load_dotenv = None

from src.utils.db import db


EXAMPLES = [
    {
        "prompt": "open youtube",
        "completion": '{"text":"Opening YouTube.","actions":[{"type":"open_url","url":"https://www.youtube.com"}]}' ,
        "tags": ["voice", "open_url"],
    },
    {
        "prompt": "open gmail",
        "completion": '{"text":"Opening Gmail.","actions":[{"type":"open_url","url":"https://mail.google.com"}]}' ,
        "tags": ["voice", "open_url"],
    },
    {
        "prompt": "search for best python async patterns",
        "completion": '{"text":"Searching the web for best Python async patterns.","actions":[{"type":"web_search","query":"best Python async patterns","num_results":5}]}' ,
        "tags": ["voice", "web_search"],
    },
    {
        "prompt": "write a professional email to HR asking for leave next friday",
        "completion": '{"text":"Sure — I can draft that email.","actions":[{"type":"generate_email","recipient":"HR","subject":"Leave request for next Friday","body_prompt":"Request leave for next Friday, briefly explain it\u2019s for a personal commitment, offer to ensure coverage, and ask for approval.","tone":"professional"}]}' ,
        "tags": ["voice", "email"],
    },
    {
        "prompt": "create a task to deploy this app to render",
        "completion": '{"text":"I\u2019ll create a deployment checklist task.","actions":[{"type":"create_task","description":"Deploy Jarvis Cloud Assistant to Render","steps":["Set Render env vars (MONGODB_URI, JARVIS_CLOUD_MODE, JARVIS_JWT_SECRET)","Deploy and confirm /health returns ok","Register user and validate /api/chat works"],"priority":4}]}' ,
        "tags": ["tasks", "render"],
    },
    {
        "prompt": "what can you do",
        "completion": '{"text":"I can help with web actions (open sites, search), drafting emails, creating tasks, and in local mode I can queue PC actions through the PC agent (with approval).","actions":[]}',
        "tags": ["help"],
    },
    {
        "prompt": "delete the .env file",
        "completion": '{"text":"I can\u2019t help with deleting or exposing secrets like .env. If you want, I can help rotate credentials and update deployment env vars safely.","actions":[]}',
        "tags": ["security", "refusal"],
    },
    {
        "prompt": "run a command to format python code",
        "completion": '{"text":"If you approve, I can run a formatter. Tell me which tool you prefer (black/ruff) and which folder to target.","actions":[]}',
        "tags": ["safety", "execute_command"],
    },
]


def main() -> int:
    if load_dotenv is not None:
        load_dotenv()

    parser = argparse.ArgumentParser(description="Seed the learning_examples store with helpful voice-style examples.")
    parser.add_argument("--user", default=os.getenv("JARVIS_SEED_USER", "default"), help="User id to seed (defaults to 'default').")
    parser.add_argument("--yes", action="store_true", help="Actually perform inserts.")
    args = parser.parse_args()

    if not args.yes:
        print("REFUSING: This will INSERT learning examples into MongoDB.")
        print("Re-run with --yes to confirm.")
        return 3

    # Ensure DB connection
    try:
        db._connect(raise_on_fail=True)
    except Exception as e:
        print(f"ERROR: Could not connect to MongoDB (check MONGODB_URI): {e}")
        return 4

    user_id = (args.user or "default").strip().lower()
    inserted = 0
    for ex in EXAMPLES:
        res = db.save_learning_example(
            user_id=user_id,
            prompt=ex["prompt"],
            completion=ex["completion"],
            meta={"source": "seed_learning_examples"},
            tags=ex.get("tags") or [],
        )
        if res is not None:
            inserted += 1

    print(f"Seeded learning_examples for user_id='{user_id}': {inserted} inserted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
