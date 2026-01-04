"""Rebuild MongoDB text index for web_training_data.

Why:
- Background analysis adds compact fields: analysis_insight + analysis_tags.
- MongoDB allows only one text index per collection; updating the definition
  usually requires dropping and recreating the index.

Safety:
- This script does NOT delete any documents.
- It ONLY drops/recreates the text index named `web_training_text_idx`.
- Requires explicit confirmation via `--yes`.

Usage (PowerShell):
  python scripts\rebuild_web_training_text_index.py --yes

Env:
- Uses `MONGODB_URI` or `MONGO_URI`.
- Uses `MONGODB_DB_NAME` (default: jarvis_db)

Optional args:
  --db <name>          Override DB name
  --collection <name>  Override collection (default: web_training_data)
  --dry-run            Show actions without making changes
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

from pymongo import MongoClient

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    load_dotenv = None


INDEX_NAME = "web_training_text_idx"


def _print_indexes(col) -> None:
    try:
        idx = list(col.list_indexes())
    except Exception as e:
        print(f"ERROR: Could not list indexes: {e}")
        return

    print("Indexes:")
    for it in idx:
        name = it.get("name")
        keys = it.get("key")
        text_weights = it.get("weights")
        extra = ""
        if text_weights:
            extra = f" weights={list(text_weights.keys())}"
        print(f"- {name}: {keys}{extra}")


def main() -> int:
    if load_dotenv is not None:
        load_dotenv()

    ap = argparse.ArgumentParser(description="Rebuild web_training_data text index to include analysis fields")
    ap.add_argument("--yes", action="store_true", help="Confirm dropping and recreating the text index")
    ap.add_argument("--dry-run", action="store_true", help="Print what would happen; do not change DB")
    ap.add_argument(
        "--uri",
        default=os.getenv("MONGODB_URI") or os.getenv("MONGO_URI"),
        help="MongoDB connection string (defaults from env: MONGODB_URI/MONGO_URI).",
    )
    ap.add_argument("--db", default=os.getenv("MONGODB_DB_NAME", "jarvis_db"), help="MongoDB database name")
    ap.add_argument("--collection", default="web_training_data", help="Collection name")
    args = ap.parse_args()

    if not args.uri:
        print("ERROR: MONGODB_URI (or MONGO_URI) is not set.")
        return 2

    if not args.yes and not args.dry_run:
        print("Refusing to run without explicit confirmation.")
        print("Re-run with: --yes (or use --dry-run)")
        return 2

    client = MongoClient(args.uri, serverSelectionTimeoutMS=8000)
    try:
        client.admin.command("ping")
    except Exception as e:
        print(f"ERROR: Could not connect to MongoDB: {e}")
        return 3

    db = client[args.db]
    col = db[args.collection]

    print(f"Connected: db={args.db} collection={args.collection}")
    _print_indexes(col)

    # Define the desired text index.
    # NOTE: analysis_tags is an array; Mongo text index can include it.
    keys = [
        ("topic", "text"),
        ("title", "text"),
        ("snippet", "text"),
        ("summary", "text"),
        ("analysis_insight", "text"),
        ("analysis_tags", "text"),
    ]

    # If index exists, drop it.
    existing = set()
    try:
        existing = {it.get("name") for it in col.list_indexes()}
    except Exception:
        existing = set()

    will_drop = INDEX_NAME in existing

    print("\nPlan:")
    if will_drop:
        print(f"- Drop existing text index: {INDEX_NAME}")
    else:
        print(f"- No existing {INDEX_NAME} found; will create it")
    print(f"- Create text index: {INDEX_NAME} with fields {[k for k, _t in keys]}")

    if args.dry_run:
        print("\nDRY RUN: no changes made.")
        return 0

    # Execute.
    try:
        if will_drop:
            col.drop_index(INDEX_NAME)
            print(f"Dropped index: {INDEX_NAME}")
    except Exception as e:
        print(f"ERROR: Failed to drop index {INDEX_NAME}: {e}")
        return 4

    try:
        col.create_index(keys, name=INDEX_NAME, default_language="english")
        print(f"Created index: {INDEX_NAME}")
    except Exception as e:
        print(f"ERROR: Failed to create index {INDEX_NAME}: {e}")
        return 5

    done_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    print(f"\nDone at {done_at}")
    _print_indexes(col)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
