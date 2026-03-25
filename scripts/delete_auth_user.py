import argparse
import json
import os
from pathlib import Path

from pymongo import MongoClient

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    load_dotenv = None


def _norm_username(u: str) -> str:
    return (u or "").strip().lower()


def _delete_from_local_file(repo_root: Path, username: str) -> tuple[bool, str]:
    auth_file = repo_root / "data" / "auth_users.json"
    if not auth_file.exists():
        return False, "local file not found"

    try:
        data = json.loads(auth_file.read_text(encoding="utf-8"))
    except Exception as e:
        return False, f"failed to read local auth file: {e}"

    users = data.get("users") if isinstance(data, dict) else None
    if not isinstance(users, dict):
        return False, "local auth file has unexpected format"

    if username not in users:
        return False, "user not present in local file"

    users.pop(username, None)
    data["users"] = users
    auth_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return True, "deleted from local auth file"


def _delete_from_mongo(username: str, uri: str, db_name: str) -> tuple[bool, str, int]:
    # Do NOT print the URI (it may contain secrets)
    client = MongoClient(uri, serverSelectionTimeoutMS=8000)
    try:
        client.admin.command("ping")
    except Exception as e:
        return False, f"could not connect to MongoDB: {e}", 0

    col = client[db_name]["auth_users"]
    result = col.delete_one({"username": username})
    try:
        client.close()
    except Exception:
        pass
    return True, "deleted from MongoDB (auth_users)", int(getattr(result, "deleted_count", 0) or 0)


def main() -> int:
    if load_dotenv is not None:
        load_dotenv()

    parser = argparse.ArgumentParser(
        description="Delete a single voice-auth user (MongoDB auth_users, and optionally local data/auth_users.json)."
    )
    parser.add_argument("username", help="Username to delete")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Actually perform deletion (required).",
    )
    parser.add_argument(
        "--mongo",
        action="store_true",
        help="Delete from MongoDB only (skip local file).",
    )
    parser.add_argument(
        "--local",
        action="store_true",
        help="Delete from local file only (skip MongoDB).",
    )
    parser.add_argument(
        "--uri",
        default=os.getenv("MONGODB_URI"),
        help="MongoDB connection string (defaults from env: MONGODB_URI/MONGO_URI).",
    )
    parser.add_argument(
        "--db",
        default=os.getenv("MONGODB_DB_NAME", "jarvis_db"),
        help="Database name (defaults from env: MONGODB_DB_NAME).",
    )

    args = parser.parse_args()

    username = _norm_username(args.username)
    if not username:
        print("ERROR: username is required")
        return 2

    if not args.yes:
        print(f"REFUSING: This will DELETE user '{username}'.")
        print("Re-run with --yes to confirm.")
        return 3

    repo_root = Path(__file__).resolve().parents[1]

    delete_mongo = True
    delete_local = True
    if args.mongo and not args.local:
        delete_local = False
    if args.local and not args.mongo:
        delete_mongo = False

    if delete_mongo:
        if not args.uri:
            print("ERROR: MONGODB_URI (or MONGO_URI) is not set; cannot delete from MongoDB.")
        else:
            ok, msg, deleted = _delete_from_mongo(username, args.uri, args.db)
            if ok:
                if deleted:
                    print(f"MongoDB: {msg}")
                else:
                    print("MongoDB: user not found")
            else:
                print(f"MongoDB ERROR: {msg}")

    if delete_local:
        ok, msg = _delete_from_local_file(repo_root, username)
        if ok:
            print(f"Local: {msg}")
        else:
            print(f"Local: {msg}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
