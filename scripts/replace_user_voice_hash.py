import argparse
import os
import sys
from datetime import datetime, UTC
from pathlib import Path

from pymongo import MongoClient

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    load_dotenv = None

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.utils.voice_auth import _hash_normalized_text, _normalize_voice_hash, _norm_text


def _is_local_mongo_uri(uri: str) -> bool:
    raw = (uri or "").strip().lower()
    if not raw:
        return False
    return (
        "localhost" in raw
        or "127.0.0.1" in raw
        or "::1" in raw
    )


def _replace_local_file(repo_root: Path, username: str, voice_hash: str, voice_text: str | None) -> tuple[bool, str]:
    import json

    auth_file = repo_root / "data" / "auth_users.json"
    if not auth_file.exists():
        return False, f"local auth file not found: {auth_file}"

    try:
        data = json.loads(auth_file.read_text(encoding="utf-8"))
    except Exception as e:
        return False, f"failed reading local auth file: {e}"

    users = data.get("users") if isinstance(data, dict) else None
    if not isinstance(users, dict):
        return False, "local auth file has unexpected format"

    user = users.get(username)
    if not isinstance(user, dict):
        return False, f"user '{username}' not found in local auth file"

    now = datetime.now(UTC).isoformat()
    user["voice_hash"] = voice_hash
    user["voice_samples"] = [{"hash": voice_hash, "text": _norm_text(voice_text or "") or None, "created_at": now}]
    user["updated_at"] = now
    users[username] = user
    data["users"] = users

    auth_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return True, f"updated local auth file for user '{username}'"


def _replace_mongo(uri: str, db_name: str, username: str, voice_hash: str, voice_text: str | None) -> tuple[bool, str]:
    client = MongoClient(uri, serverSelectionTimeoutMS=8000)
    try:
        client.admin.command("ping")
    except Exception as e:
        return False, f"could not connect to MongoDB: {e}"

    col = client[db_name]["auth_users"]
    user_doc = col.find_one({"username": username})
    if not user_doc:
        try:
            client.close()
        except Exception:
            pass
        return False, f"user '{username}' not found in MongoDB db={db_name}"

    now = datetime.now(UTC).isoformat()
    update_doc = {
        "voice_hash": voice_hash,
        "voice_samples": [{"hash": voice_hash, "text": _norm_text(voice_text or "") or None, "created_at": now}],
        "updated_at": now,
    }
    col.update_one({"username": username}, {"$set": update_doc})

    try:
        client.close()
    except Exception:
        pass

    return True, f"updated MongoDB auth_users for user '{username}' in db={db_name}"


def main() -> int:
    if load_dotenv is not None:
        load_dotenv()

    parser = argparse.ArgumentParser(
        description=(
            "Replace a user's voice hash and remove old hashes by keeping a single voice sample. "
            "Safe-by-default: dry-run unless --yes; refuses non-local Mongo URI unless explicitly allowed."
        )
    )
    parser.add_argument("username", help="Target username")
    parser.add_argument(
        "--voice-text",
        default="",
        help="Spoken phrase text (will be normalized and SHA-256 hashed).",
    )
    parser.add_argument(
        "--voice-hash",
        default="",
        help="Optional precomputed voice hash (hex/base64/sha256:... supported). If omitted, derived from --voice-text.",
    )
    parser.add_argument(
        "--target",
        choices=["mongo", "local", "both"],
        default="mongo",
        help="Where to apply update (default: mongo).",
    )
    parser.add_argument(
        "--uri",
        default=os.getenv("MONGODB_URI") or "",
        help="MongoDB URI (defaults to env MONGODB_URI/MONGO_URI).",
    )
    parser.add_argument(
        "--db",
        default=os.getenv("MONGODB_DB_NAME", "jarvis_db"),
        help="MongoDB DB name (default from env MONGODB_DB_NAME or jarvis_db).",
    )
    parser.add_argument(
        "--allow-nonlocal-uri",
        action="store_true",
        help="Allow updates on non-local MongoDB URIs (disabled by default to protect live data).",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Actually apply changes. Without this, script runs in dry-run mode.",
    )

    args = parser.parse_args()

    username = (args.username or "").strip().lower()
    if not username:
        print("ERROR: username is required")
        return 2

    normalized_hash = _normalize_voice_hash(args.voice_hash)
    if not normalized_hash:
        normalized_hash = _hash_normalized_text(args.voice_text)

    if not normalized_hash:
        print("ERROR: provide --voice-text or --voice-hash")
        return 2

    if len(normalized_hash) != 64 or any(ch not in "0123456789abcdef" for ch in normalized_hash):
        print("ERROR: resulting hash is not canonical 64-char lowercase hex")
        return 2

    print("Planned update:")
    print(f"- username: {username}")
    print(f"- hash_prefix: {normalized_hash[:12]}...")
    print("- voice_samples: will be replaced with exactly one sample")
    print(f"- target: {args.target}")

    if not args.yes:
        print("DRY-RUN only. Re-run with --yes to apply.")
        return 0

    repo_root = REPO_ROOT

    if args.target in ("mongo", "both"):
        uri = (args.uri or "").strip()
        if not uri:
            print("MongoDB ERROR: URI not set. Provide --uri or set MONGODB_URI.")
            return 3
        if (not args.allow_nonlocal_uri) and (not _is_local_mongo_uri(uri)):
            print("MongoDB ERROR: non-local URI blocked. Use --allow-nonlocal-uri only if you are sure.")
            return 3
        ok, msg = _replace_mongo(uri, args.db, username, normalized_hash, args.voice_text)
        print(("MongoDB: " if ok else "MongoDB ERROR: ") + msg)
        if not ok:
            return 4

    if args.target in ("local", "both"):
        ok, msg = _replace_local_file(repo_root, username, normalized_hash, args.voice_text)
        print(("Local: " if ok else "Local ERROR: ") + msg)
        if not ok:
            return 5

    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
