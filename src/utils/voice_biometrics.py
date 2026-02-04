"""src/utils/voice_biometrics.py

Lightweight text-dependent speaker verification.

This is intentionally simple (MFCC statistics + cosine similarity) to avoid
large runtime dependencies (e.g., torch). It works best when users always
say the same short passphrase for enrollment/login/commands.

Configuration:
- Controlled via in-code defaults in src/config/runtime_defaults.py
"""

from __future__ import annotations

import base64
from datetime import datetime
from typing import Iterable, Optional, Tuple

import numpy as np

from src.config import runtime_defaults as rd


VOICE_BIOMETRICS_ENABLED = bool(rd.VOICE_BIOMETRICS_ENABLED)
VOICE_BIOMETRICS_THRESHOLD = float(rd.VOICE_BIOMETRICS_THRESHOLD)
VOICE_BIOMETRICS_MAX_EMBEDS = int(rd.VOICE_BIOMETRICS_MAX_EMBEDS)


def _decode_pcm16_b64(audio_b64: str) -> bytes:
    if not audio_b64:
        return b""
    return base64.b64decode(audio_b64)


def _pcm16le_bytes_to_float32(audio_bytes: bytes) -> np.ndarray:
    if not audio_bytes:
        return np.zeros((0,), dtype=np.float32)
    pcm = np.frombuffer(audio_bytes, dtype="<i2").astype(np.float32)
    if pcm.size == 0:
        return np.zeros((0,), dtype=np.float32)
    pcm /= 32768.0
    return pcm


def _resample_linear(x: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    if x is None or x.size == 0:
        return np.zeros((0,), dtype=np.float32)
    if src_rate == dst_rate:
        return x.astype(np.float32, copy=False)
    if src_rate <= 0 or dst_rate <= 0:
        return x.astype(np.float32, copy=False)
    ratio = float(dst_rate) / float(src_rate)
    n_out = max(1, int(round(x.size * ratio)))
    idx = np.linspace(0, x.size - 1, num=n_out, dtype=np.float32)
    x0 = np.floor(idx).astype(np.int64)
    x1 = np.minimum(x0 + 1, x.size - 1)
    w = idx - x0.astype(np.float32)
    out = (1.0 - w) * x[x0] + w * x[x1]
    return out.astype(np.float32, copy=False)


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    if a is None or b is None:
        return 0.0
    if a.size == 0 or b.size == 0:
        return 0.0
    denom = (np.linalg.norm(a) * np.linalg.norm(b))
    if denom <= 0:
        return 0.0
    return float(np.dot(a, b) / denom)


def _vad_mask(x: np.ndarray, frame: int = 400, hop: int = 160, rms_thresh: float = 0.012) -> np.ndarray:
    """Very small energy-based VAD mask over frames."""
    if x.size < frame:
        return np.ones((0,), dtype=bool)
    n = 1 + (x.size - frame) // hop
    mask = np.zeros((n,), dtype=bool)
    for i in range(n):
        s = i * hop
        f = x[s : s + frame]
        rms = float(np.sqrt(np.mean(f * f) + 1e-12))
        mask[i] = rms >= rms_thresh
    return mask


def compute_embedding_from_pcm16(audio_bytes: bytes, sample_rate_hz: int) -> Optional[np.ndarray]:
    """Return a stable vector (float32) or None if audio unusable."""
    try:
        import python_speech_features  # type: ignore
    except Exception:
        # Dependency missing; treat as unavailable.
        return None

    x = _pcm16le_bytes_to_float32(audio_bytes)
    if x.size == 0:
        return None

    # Normalize and resample to 16k.
    sr = int(sample_rate_hz or 16000)
    x = _resample_linear(x, sr, 16000)
    if x.size < 16000 // 4:
        return None

    # Trim leading/trailing silence a bit (energy-based).
    frame = 400  # 25ms @ 16k
    hop = 160    # 10ms @ 16k
    vad = _vad_mask(x, frame=frame, hop=hop, rms_thresh=0.010)
    if vad.size >= 8 and np.any(vad):
        first = int(np.argmax(vad))
        last = int(len(vad) - 1 - np.argmax(vad[::-1]))
        start = first * hop
        end = min(x.size, last * hop + frame)
        if end > start:
            x = x[start:end]

    if x.size < 16000 // 3:
        return None

    # MFCCs
    try:
        mfcc = python_speech_features.mfcc(
            signal=x,
            samplerate=16000,
            winlen=0.025,
            winstep=0.010,
            numcep=13,
            nfilt=26,
            nfft=512,
            preemph=0.97,
            appendEnergy=True,
        )
    except Exception:
        return None

    if mfcc is None or len(mfcc) < 8:
        return None

    mfcc = np.asarray(mfcc, dtype=np.float32)
    mu = np.mean(mfcc, axis=0)
    sd = np.std(mfcc, axis=0)
    emb = np.concatenate([mu, sd], axis=0).astype(np.float32)

    # L2 normalize
    n = float(np.linalg.norm(emb))
    if n <= 0:
        return None
    emb = emb / n
    return emb


def to_jsonable_embedding(vec: np.ndarray) -> list[float]:
    return [float(x) for x in np.asarray(vec, dtype=np.float32).tolist()]


def best_similarity(sample_vec: np.ndarray, stored_vectors: Iterable[Iterable[float]]) -> float:
    best = 0.0
    for v in stored_vectors or []:
        try:
            arr = np.asarray(list(v), dtype=np.float32)
            if arr.size != sample_vec.size:
                continue
            score = _cosine(sample_vec, arr)
            if score > best:
                best = score
        except Exception:
            continue
    return float(best)


def should_accept(sample_vec: np.ndarray, stored_vectors: Iterable[Iterable[float]], threshold: float = VOICE_BIOMETRICS_THRESHOLD) -> Tuple[bool, float]:
    score = best_similarity(sample_vec, stored_vectors)
    return (score >= float(threshold)), float(score)


def now_iso() -> str:
    return datetime.utcnow().isoformat()
