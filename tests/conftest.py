import sys
import gc
import asyncio
from pathlib import Path

# Ensure repo root is on sys.path so tests can import `src` and `app` reliably.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def pytest_sessionfinish(session, exitstatus):
    try:
        from src.utils.db import db

        db.close()
    except Exception:
        pass

    # Test-only hygiene: close any leaked aiohttp sessions so CI logs stay clean.
    try:
        import aiohttp

        leaked = []
        for obj in gc.get_objects():
            try:
                if isinstance(obj, aiohttp.ClientSession) and (not obj.closed):
                    leaked.append(obj)
            except Exception:
                continue

        if leaked:
            loop = asyncio.new_event_loop()
            try:
                asyncio.set_event_loop(loop)
                for s in leaked:
                    try:
                        loop.run_until_complete(s.close())
                    except Exception:
                        pass
            finally:
                try:
                    loop.run_until_complete(loop.shutdown_asyncgens())
                except Exception:
                    pass
                loop.close()
                asyncio.set_event_loop(None)
    except Exception:
        pass
