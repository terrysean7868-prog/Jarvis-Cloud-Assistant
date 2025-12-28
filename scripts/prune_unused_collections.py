import argparse
import os
from typing import Iterable

from pymongo import MongoClient

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    load_dotenv = None


# Collections currently used by this codebase (Jarvis-Cloud-Assistant)
# NOTE: Keeping a collection is always safer than dropping it.
USED_COLLECTIONS = {
    # Voice auth + tokens
    "auth_users",
    "auth_revoked_tokens",

    # Core telemetry
    "chat_history",
    "system_events",
    "voice_commands",
    "module_changes",
    "git_operations",

    # RAG-lite stores
    "learning_examples",
    "web_training_data",

    # Memory subsystem
    "bot_memory",
    "conversations",
    "user_preferences",
    "conversation_context",
}


def _is_system_collection(name: str) -> bool:
    return name.startswith("system.")


def _print_list(title: str, items: Iterable[str]):
    items = list(items)
    print(title)
    if not items:
        print("  (none)")
        return
    for x in items:
        print(f"  - {x}")


def main() -> int:
    if load_dotenv is not None:
        load_dotenv()

    parser = argparse.ArgumentParser(
        description=(
            "Drop MongoDB collections in jarvis_db that are not used by this assistant. "
            "Default is dry-run; add --yes to actually drop."
        )
    )
    parser.add_argument("--yes", action="store_true", help="Actually drop collections (required to make changes).")
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
        "--keep",
        action="append",
        default=[],
        help="Additional collection name to keep (can be provided multiple times).",
    )
    args = parser.parse_args()

    if not args.uri:
        print("ERROR: MONGODB_URI (or MONGO_URI) is not set.")
        return 2

    keep = set(USED_COLLECTIONS)
    keep.update([k.strip() for k in (args.keep or []) if (k or "").strip()])

    client = MongoClient(args.uri, serverSelectionTimeoutMS=8000)
    try:
        client.admin.command("ping")
    except Exception as e:
        print(f"ERROR: Could not connect to MongoDB: {e}")
        return 3

    db = client[args.db]
    collections = sorted(db.list_collection_names())

    # Candidate drops: non-system collections not in keep list
    candidates = [c for c in collections if (not _is_system_collection(c)) and (c not in keep)]

    print(f"MongoDB DB: {args.db}")
    _print_list("Collections present:", collections)
    _print_list("Keeping (used by assistant):", sorted(keep))
    _print_list("Unused candidates to drop:", candidates)

    if not candidates:
        print("Nothing to drop.")
        return 0

    if not args.yes:
        print("DRY-RUN ONLY. Re-run with --yes to actually drop the unused collections above.")
        return 0

    dropped = []
    for name in candidates:
        try:
            db.drop_collection(name)
            dropped.append(name)
        except Exception as e:
            print(f"WARN: Failed to drop '{name}': {e}")

    _print_list("Dropped:", dropped)
    remaining = sorted(db.list_collection_names())
    _print_list("Remaining:", remaining)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
