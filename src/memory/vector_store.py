from __future__ import annotations

import math
import re
from typing import Any

from src.config import runtime_defaults as rd


try:
    import chromadb  # type: ignore
except Exception:
    chromadb = None


class VectorStore:
    """Optional ChromaDB-backed semantic store with in-memory fallback."""

    def __init__(self, collection_name: str = "jarvis_memory"):
        self.collection_name = collection_name
        self._fallback: list[dict[str, Any]] = []
        self._collection = None

        if chromadb is not None and bool(getattr(rd, "ENABLE_CHROMADB", True)):
            try:
                client = chromadb.PersistentClient(path=str(getattr(rd, "CHROMADB_PATH", "data/chromadb")))
                self._collection = client.get_or_create_collection(name=collection_name)
            except Exception:
                self._collection = None

    @staticmethod
    def _tokenize(text: str) -> set[str]:
        return set(re.findall(r"[a-z0-9]+", (text or "").lower()))

    @staticmethod
    def _jaccard(a: set[str], b: set[str]) -> float:
        if not a or not b:
            return 0.0
        inter = len(a.intersection(b))
        union = len(a.union(b))
        if union == 0:
            return 0.0
        return inter / union

    def add(self, item_id: str, text: str, metadata: dict[str, Any] | None = None) -> None:
        metadata = metadata or {}
        if self._collection is not None:
            try:
                self._collection.upsert(
                    ids=[item_id],
                    documents=[text],
                    metadatas=[metadata],
                )
                return
            except Exception:
                pass

        self._fallback.append({"id": item_id, "text": text, "metadata": metadata})

    def query(self, text: str, k: int = 5) -> list[dict[str, Any]]:
        if self._collection is not None:
            try:
                res = self._collection.query(query_texts=[text], n_results=max(1, k))
                docs = (res.get("documents") or [[]])[0]
                ids = (res.get("ids") or [[]])[0]
                metas = (res.get("metadatas") or [[]])[0]
                out = []
                for idx, doc in enumerate(docs):
                    out.append(
                        {
                            "id": ids[idx] if idx < len(ids) else f"item-{idx}",
                            "text": doc,
                            "metadata": metas[idx] if idx < len(metas) else {},
                            "score": None,
                        }
                    )
                return out
            except Exception:
                pass

        q = self._tokenize(text)
        scored = []
        for row in self._fallback:
            score = self._jaccard(q, self._tokenize(str(row.get("text") or "")))
            if score <= 0:
                continue
            scored.append({**row, "score": float(score)})

        scored.sort(key=lambda x: x.get("score") or -math.inf, reverse=True)
        return scored[: max(1, k)]
