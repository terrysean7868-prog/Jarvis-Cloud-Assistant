import json

import pytest
from starlette.testclient import TestClient


def _make_client():
    # Import lazily so we can patch globals before startup runs.
    import app as jarvis_app

    # Prevent startup from doing network/background work during tests.
    jarvis_app.ENABLE_SCHEDULER = False
    try:
        jarvis_app.start_session_cleanup_task = lambda: None
    except Exception:
        pass
    try:
        jarvis_app.database._ensure_connected = lambda: None
    except Exception:
        pass

    return TestClient(jarvis_app.app)


def test_ws_agent_invalid_json_returns_error_payload():
    with _make_client() as client:
        with client.websocket_connect("/ws/agent") as ws:
            ws.send_text("not-json")
            msg = ws.receive_json()
            assert msg["type"] == "error"
            assert msg["reason"] == "invalid_json"


def test_ws_agent_wrong_token_type_returns_invalid_agent_token():
    # Use a valid JWT signature but wrong typ to ensure the server returns a helpful reason.
    import app as jarvis_app
    from jose import jwt

    jarvis_app.ENABLE_SCHEDULER = False
    jarvis_app.start_session_cleanup_task = lambda: None
    jarvis_app.database._ensure_connected = lambda: None

    # Force a deterministic secret for this test.
    jarvis_app.auth_tokens.secret = "test-secret"
    jarvis_app.auth_tokens.issuer = "jarvis"

    bad_payload = {"iss": "jarvis", "sub": "user", "role": "user", "typ": "session"}
    bad_token = jwt.encode(bad_payload, jarvis_app.auth_tokens.secret, algorithm="HS256")

    with TestClient(jarvis_app.app) as client:
        with client.websocket_connect("/ws/agent") as ws:
            ws.send_text(
                json.dumps(
                    {
                        "type": "auth",
                        "token": bad_token,
                        "device_id": "avadh",
                        "capabilities": {},
                    }
                )
            )
            msg = ws.receive_json()
            assert msg["type"] == "error"
            assert msg["reason"] == "invalid_agent_token"
