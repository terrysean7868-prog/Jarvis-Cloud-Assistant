import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    load_dotenv = None

from src.config import runtime_defaults as rd
from src.config.secrets import llm_secrets


def _fetch_json(url: str, api_key: str, timeout: int) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {api_key}"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "ignore"))


def _check_provider(
    *,
    name: str,
    api_key: str | None,
    endpoint: str,
    model: str,
    timeout: int,
    show_models: bool,
) -> tuple[bool, str]:
    if not api_key:
        return False, f"{name}: KEY_MISSING"

    models_url = endpoint.rstrip("/")
    if models_url.endswith("/chat/completions"):
        models_url = models_url[: -len("/chat/completions")]
    models_url = models_url + "/models"

    try:
        data = _fetch_json(models_url, api_key, timeout)
        model_ids = sorted(
            [
                m.get("id", "")
                for m in data.get("data", [])
                if isinstance(m, dict) and m.get("id")
            ]
        )
        has_configured_model = model in model_ids
        summary = (
            f"{name}: OK | endpoint={models_url} | configured_model={model} "
            f"| model_available={str(has_configured_model).lower()} | model_count={len(model_ids)}"
        )
        if show_models:
            preview = ", ".join(model_ids[:20]) if model_ids else "(none)"
            summary += f"\n{name}: MODELS: {preview}"
        return True, summary
    except Exception as e:
        return False, f"{name}: FAIL | endpoint={models_url} | error={e}"


def main() -> int:
    if load_dotenv is not None:
        load_dotenv()

    parser = argparse.ArgumentParser(
        description="Manual one-shot check for configured LLM providers (primary + backup)."
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=20,
        help="HTTP timeout in seconds (default: 20)",
    )
    parser.add_argument(
        "--show-models",
        action="store_true",
        help="Print first 20 model IDs from each provider.",
    )
    args = parser.parse_args()

    secrets = llm_secrets()

    print("LLM provider config:")
    print(f"- primary_model={rd.PRIMARY_MODEL}")
    print(f"- primary_endpoint={rd.PRIMARY_ENDPOINT}")
    print(f"- backup_model={rd.BACKUP_MODEL}")
    print(f"- backup_endpoint={rd.BACKUP_ENDPOINT}")
    print(f"- primary_key_present={str(bool(secrets.primary_api_key)).lower()}")
    print(f"- backup_key_present={str(bool(secrets.backup_api_key)).lower()}")
    print()

    ok_primary, out_primary = _check_provider(
        name="PRIMARY",
        api_key=secrets.primary_api_key,
        endpoint=rd.PRIMARY_ENDPOINT,
        model=rd.PRIMARY_MODEL,
        timeout=args.timeout,
        show_models=args.show_models,
    )
    print(out_primary)

    ok_backup, out_backup = _check_provider(
        name="BACKUP",
        api_key=secrets.backup_api_key,
        endpoint=rd.BACKUP_ENDPOINT,
        model=rd.BACKUP_MODEL,
        timeout=args.timeout,
        show_models=args.show_models,
    )
    print(out_backup)

    # Exit non-zero if backup is not healthy, since that's usually the risky surprise.
    if ok_primary and ok_backup:
        print("\nRESULT: BOTH_PROVIDERS_OK")
        return 0

    print("\nRESULT: PROVIDER_CHECK_FAILED")
    if not ok_primary and not ok_backup:
        print("Neither provider is healthy.")
        return 2
    if not ok_primary:
        print("Primary provider failed.")
        return 3
    print("Backup provider failed.")
    return 4


if __name__ == "__main__":
    raise SystemExit(main())
