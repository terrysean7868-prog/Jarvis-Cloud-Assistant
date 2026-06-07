"""Lightweight HuggingFace helpers used by executor for dataset ingestion.

Functions here avoid heavy dependencies and prefer the existing internet layer.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List

try:
    from ..internet.internet import get_internet
    INTERNET_AVAILABLE = True
except Exception:
    get_internet = None
    INTERNET_AVAILABLE = False

from .db import db


async def fetch_and_seed_hf_dataset(dataset_query: str, max_items: int = 20) -> Dict[str, Any]:
    """Fetch a HuggingFace dataset page summary (or dataset URL) and persist compact items to DB.

    This is intentionally lightweight: it scrapes the dataset landing page via the internet module
    and saves a handful of references as training seeds. For full dataset downloads, use the
    dedicated training scripts which leverage `datasets`/`transformers`.
    """
    q = str(dataset_query or "").strip()
    out = {"status": "error", "saved": 0, "details": []}
    if not q:
        out["message"] = "dataset_query required"
        return out

    # Accept full URLs or dataset ids like "huggingface/dataset-name" or "dataset-name"
    url = None
    if q.startswith("http://") or q.startswith("https://"):
        url = q
    else:
        # Try to form a huggingface dataset URL
        ds = q
        if "/datasets/" in q:
            url = q
        else:
            ds = q.split()[-1]
            url = f"https://huggingface.co/datasets/{ds}"

    if not INTERNET_AVAILABLE or not get_internet:
        out["message"] = "Internet module not available"
        return out

    try:
        internet = await get_internet()
        page = await internet.fetch_webpage(url, include_content=True)
    except Exception as e:
        out["message"] = f"failed to fetch dataset page: {e}"
        # Attempt to close internet to avoid unclosed sessions
        try:
            from ..internet.internet import close_internet
            await close_internet()
        except Exception:
            pass
        return out

    if not page or not isinstance(page, dict):
        out["message"] = f"failed to fetch dataset page or empty result (status unknown): {url}"
        try:
            from ..internet.internet import close_internet
            await close_internet()
        except Exception:
            pass
        return out

    title = page.get("title") or url
    summary = (page.get("summary") or "")[:2000]

    saved = 0
    try:
        # Save a compact reference as dataset_seed
        db.save_web_training_item(
            topic=ds,
            title=title,
            snippet=(summary or title)[:500],
            summary=summary,
            url=url,
            source="huggingface_seed",
        )
        saved += 1
    except Exception as e:
        out["details"].append({"url": url, "status": "error", "message": str(e)})
    finally:
        # Ensure internet session is closed to avoid aiohttp warnings
        try:
            from ..internet.internet import close_internet
            await close_internet()
        except Exception:
            pass

    out["status"] = "success" if saved else "error"
    out["saved"] = saved
    out["details"].append({"url": url, "status": "saved" if saved else "error"})
    return out
