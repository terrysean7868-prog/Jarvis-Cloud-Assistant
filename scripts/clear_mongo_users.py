import argparse
import os
import json
from pathlib import Path

from pymongo import MongoClient

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    load_dotenv = None


def main() -> int:
    # Load .env so local runs behave like the app (and like Render env vars).
    if load_dotenv is not None:
        load_dotenv()

    parser = argparse.ArgumentParser(
        description="Delete ALL Jarvis voice-auth users from MongoDB collection auth_users."
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Actually perform deletion (required).",
    )
    parser.add_argument(
        "--uri",
        default=os.getenv("MONGODB_URI") or os.getenv("MONGO_URI"),
        help="MongoDB connection string (defaults from env: MONGODB_URI/MONGO_URI).",
    )
    parser.add_argument(
        "--db",
        default=os.getenv("MONGODB_DB_NAME", "jarvis_db"),
        help="Database name (defaults from env: MONGODB_DB_NAME).",
    )
    parser.add_argument(
        "--local",
        action="store_true",
        help="Also clear local auth files (data/auth_users.json and data/auth_pending_queue.json).",
    )
    args = parser.parse_args()

    if not args.yes:
        print("REFUSING: This will DELETE ALL users from MongoDB collection 'auth_users'.")
        print("Re-run with --yes to confirm.")
        return 3

    if args.local:
        root = Path(__file__).resolve().parents[1]
        auth_file = root / "data" / "auth_users.json"
        pending_file = root / "data" / "auth_pending_queue.json"
        try:
            auth_file.parent.mkdir(parents=True, exist_ok=True)
            auth_file.write_text(json.dumps({"users": {}}, indent=2), encoding="utf-8")
            print(f"Local cleared: {auth_file}")
        except Exception as e:
            print(f"WARN: Could not clear local auth file: {e}")
        try:
            pending_file.parent.mkdir(parents=True, exist_ok=True)
            pending_file.write_text(json.dumps({"pending": []}, indent=2), encoding="utf-8")
            print(f"Local cleared: {pending_file}")
        except Exception as e:
            print(f"WARN: Could not clear local pending queue file: {e}")

    if not args.uri:
        print("NOTE: MONGODB_URI (or MONGO_URI) is not set; skipping MongoDB deletion.")
        return 0 if args.local else 2

    # Do NOT print the URI (it may contain secrets)
    client = MongoClient(args.uri, serverSelectionTimeoutMS=8000)
    try:
        client.admin.command("ping")
    except Exception as e:
        print(f"ERROR: Could not connect to MongoDB: {e}")
        return 4

    db = client[args.db]
    col = db["auth_users"]

    before = col.count_documents({})
    result = col.delete_many({})
    after = col.count_documents({})

    print(f"MongoDB DB: {args.db}")
    print("Collection: auth_users")
    print(f"Users before: {before}")
    print(f"Deleted: {result.deleted_count}")
    print(f"Users after: {after}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
