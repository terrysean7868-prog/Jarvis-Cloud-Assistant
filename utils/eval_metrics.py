# src/utils/eval_metrics.py
import os
import math
import json
from typing import List, Tuple

# Optional libraries
try:
    from nltk.translate.bleu_score import sentence_bleu, SmoothingFunction  # pyright: ignore[reportMissingImports]
except Exception:
    sentence_bleu = None

try:
    # Simple ROUGE-L by longest common subsequence
    pass
except Exception:
    pass

try:
    from sklearn.metrics.pairwise import cosine_similarity  # pyright: ignore[reportMissingImports]
    import numpy as np  # pyright: ignore[reportMissingImports]
except Exception:
    cosine_similarity = None
    np = None

# Optional embedding model
EMBED_MODEL = os.getenv("EMBED_MODEL", "all-MiniLM-L6-v2")

try:
    from sentence_transformers import SentenceTransformer  # pyright: ignore[reportMissingImports]
    _embed_model = SentenceTransformer(EMBED_MODEL)
except Exception:
    _embed_model = None

# ---------------------
# BLEU wrapper
# ---------------------

def compute_bleu(reference: str, candidate: str) -> float:
    """Return BLEU-4 score (0-1). If nltk not installed, return -1."""
    if sentence_bleu is None:
        return -1.0
    ref_tokens = reference.split()
    cand_tokens = candidate.split()
    smoothie = SmoothingFunction().method4
    try:
        score = sentence_bleu([ref_tokens], cand_tokens, smoothing_function=smoothie)
        return float(score)
    except Exception:
        return 0.0

# ---------------------
# ROUGE-L (simple LCS based)
# ---------------------

def _lcs(a: List[str], b: List[str]) -> int:
    m = len(a)
    n = len(b)
    dp = [[0]*(n+1) for _ in range(m+1)]
    for i in range(m-1, -1, -1):
        for j in range(n-1, -1, -1):
            if a[i] == b[j]:
                dp[i][j] = 1 + dp[i+1][j+1]
            else:
                dp[i][j] = max(dp[i+1][j], dp[i][j+1])
    return dp[0][0]


def compute_rouge_l(reference: str, candidate: str) -> float:
    r_tokens = reference.split()
    c_tokens = candidate.split()
    if not r_tokens or not c_tokens:
        return 0.0
    lcs_len = _lcs(r_tokens, c_tokens)
    prec = lcs_len / len(c_tokens)
    rec = lcs_len / len(r_tokens)
    if prec + rec == 0:
        return 0.0
    beta = 1.2
    score = (1 + beta**2) * prec * rec / (rec + beta**2 * prec)
    return float(score)

# ---------------------
# Embedding similarity
# ---------------------

def compute_embedding_similarity(reference: str, candidate: str) -> float:
    """Return cosine similarity between embeddings (0-1). If model not available return -1"""
    if _embed_model is None or cosine_similarity is None:
        return -1.0
    try:
        ref_emb = _embed_model.encode([reference], convert_to_numpy=True)
        cand_emb = _embed_model.encode([candidate], convert_to_numpy=True)
        sim = float(cosine_similarity(ref_emb, cand_emb)[0, 0])
        # scale from -1..1 to 0..1
        return (sim + 1.0) / 2.0
    except Exception:
        return -1.0

# ---------------------
# Convenience scorer for a batch of examples
# ---------------------

def score_examples(examples: List[Tuple[str, str]]) -> List[dict]:
    """Each example is (reference, candidate). Returns per-example metrics."""
    out = []
    for ref, cand in examples:
        bleu = compute_bleu(ref, cand)
        rouge = compute_rouge_l(ref, cand)
        emb = compute_embedding_similarity(ref, cand)
        out.append({"bleu": bleu, "rouge_l": rouge, "embed_sim": emb})
    return out
