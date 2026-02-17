import math
import struct

from src.utils.voice_biometrics import validate_pcm16_audio_quality


def _pcm16_sine_bytes(sample_rate: int = 16000, seconds: float = 1.0, freq: float = 220.0, amp: float = 0.35) -> bytes:
    n = max(1, int(sample_rate * seconds))
    out = bytearray()
    for i in range(n):
        s = math.sin(2.0 * math.pi * freq * (i / float(sample_rate)))
        v = int(max(-1.0, min(1.0, s * amp)) * 32767)
        out.extend(struct.pack("<h", v))
    return bytes(out)


def test_validate_pcm16_audio_quality_empty_payload():
    ok, code, _ = validate_pcm16_audio_quality(b"", 16000)
    assert ok is False
    assert code == "audio_empty"


def test_validate_pcm16_audio_quality_too_short():
    # 100ms @ 16k = 1600 samples = 3200 bytes
    short_bytes = b"\x00\x00" * 1600
    ok, code, _ = validate_pcm16_audio_quality(short_bytes, 16000)
    assert ok is False
    assert code == "audio_too_short"


def test_validate_pcm16_audio_quality_silent_audio():
    silent = b"\x00\x00" * 16000  # 1s @ 16k
    ok, code, _ = validate_pcm16_audio_quality(silent, 16000)
    assert ok is False
    assert code == "audio_too_silent"


def test_validate_pcm16_audio_quality_valid_audio():
    audio = _pcm16_sine_bytes(sample_rate=16000, seconds=1.1, freq=220.0, amp=0.5)
    ok, code, msg = validate_pcm16_audio_quality(audio, 16000)
    assert ok is True
    assert code == ""
    assert msg == ""
