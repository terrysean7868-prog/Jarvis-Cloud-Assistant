import argparse
import os


def _apply_local_overrides(host: str, port: int) -> None:
    # Local launcher no longer mutates process env; defaults are handled in code.
    _ = (host, port)


def main() -> int:
    ap = argparse.ArgumentParser(description="Run Jarvis backend locally")
    ap.add_argument("--host", default="127.0.0.1", help="Host to bind")
    ap.add_argument("--port", type=int, default=18001, help="Port to bind")
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
