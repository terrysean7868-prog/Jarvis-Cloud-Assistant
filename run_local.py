import argparse
import os


def _apply_local_overrides(host: str, port: int) -> None:
    base_url = f"http://{host}:{port}"

    # Force safe local defaults. (This script is explicitly for local runs.)
    os.environ["JARVIS_CLOUD_MODE"] = "false"
    os.environ.setdefault("JARVIS_ENABLE_PC_AGENT", "true")

    # Local dev should not require MongoDB unless explicitly enabled.
    os.environ.setdefault("AUTH_USE_DB", "false")

    # Help desktop + agent discovery by providing consistent URLs.
    os.environ.setdefault("JARVIS_PUBLIC_SERVER_URL", base_url)
    os.environ.setdefault("JARVIS_DESKTOP_API_URL", base_url)


def main() -> int:
    ap = argparse.ArgumentParser(description="Run Jarvis backend locally")
    ap.add_argument("--host", default=os.getenv("HOST", "127.0.0.1"), help="Host to bind")
    ap.add_argument("--port", type=int, default=int(os.getenv("PORT", "18001")), help="Port to bind")
    ap.add_argument("--reload", action="store_true", help="Enable auto-reload (dev)")
    ap.add_argument(
        "--respect-env",
        action="store_true",
        help="Do not override local defaults; use existing environment variables as-is.",
    )
    args = ap.parse_args()

    if not args.respect_env:
        _apply_local_overrides(host=str(args.host), port=int(args.port))

    try:
        import uvicorn  # type: ignore
    except Exception as e:
        print(f"ERROR: uvicorn is not installed: {e}")
        return 1

    uvicorn.run("app:app", host=str(args.host), port=int(args.port), reload=bool(args.reload), log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
