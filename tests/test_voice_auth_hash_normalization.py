import base64

from src.utils.voice_auth import voice_auth


def test_voice_hash_sha256_prefix_matches_hex():
    raw_hex = "a" * 64
    assert voice_auth._compare_voice_hashes(raw_hex, f"sha256:{raw_hex}")


def test_voice_hash_base64_digest_matches_hex():
    hex_digest = "00" * 32
    b64_digest = base64.b64encode(bytes.fromhex(hex_digest)).decode("ascii")
    assert voice_auth._compare_voice_hashes(hex_digest, b64_digest)


def test_authenticate_accepts_text_only_when_registered_with_text():
    username = "hash_norm_test_user"

    reg = voice_auth.register_user(username, "", password=None, role="user", voice_sample_text="Open Jarvis now")
    assert reg.get("status") in {"success", "queued"}

    ok, sid_or_error = voice_auth.authenticate_by_voice(username, "", password=None, voice_sample_text="open   jarvis now")
    assert ok, sid_or_error
