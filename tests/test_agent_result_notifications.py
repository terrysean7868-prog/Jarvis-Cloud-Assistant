import json

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

    # Force deterministic JWT secret for sessions.
    jarvis_app.auth_tokens.secret = "test-secret"
    jarvis_app.auth_tokens.issuer = "jarvis"

    return TestClient(jarvis_app.app)


def test_agent_job_results_published_to_notifications_ws():
    import app as jarvis_app

    with _make_client() as client:
        # Create an authenticated session (admin is easiest for dispatch routing).
        session_id = jarvis_app.auth_tokens.issue(username="zz_test_admin_unique", role="admin")

        # Connect notifications websocket.
        with client.websocket_connect(f"/ws/notifications?session_id={session_id}") as ws_notifications:
            ack = ws_notifications.receive_json()
            assert ack.get("type") == "ack"

            # Connect a fake agent.
            with client.websocket_connect("/ws/agent") as ws_agent:
                ws_agent.send_text(
                    json.dumps(
                        {
                            "type": "auth",
                            "device_id": "primary",
                            "secret": jarvis_app.AGENT_SHARED_SECRET,
                            "capabilities": {
                                # open_url requires allow_execute_command in /api/device/dispatch.
                                "allow_execute_command": True,
                                "allow_app_control": False,
                                "allow_screen": False,
                                "allow_self_update": False,
                                "allow_file_ops": False,
                            },
                        }
                    )
                )
                agent_ack = ws_agent.receive_json()
                assert agent_ack.get("type") == "ack"

                # Dispatch an action to the connected agent.
                resp = client.post(
                    "/api/device/dispatch",
                    json={
                        "session_id": session_id,
                        "device_id": "primary",
                        "actions": [{"type": "open_url", "url": "https://example.com"}],
                        "source_text": "test dispatch",
                    },
                )
                assert resp.status_code == 200, f"dispatch failed: {resp.status_code} {resp.text}"
                job = resp.json()["job"]
                job_id = job["job_id"]

                # Agent receives the job.
                incoming = ws_agent.receive_json()
                assert incoming.get("type") == "job"
                assert incoming.get("job_id") == job_id

                # Agent sends results.
                ws_agent.send_text(
                    json.dumps(
                        {
                            "type": "result",
                            "device_id": "primary",
                            "job_id": job_id,
                            "results": [{"status": "success", "action_type": "open_url"}],
                            "completed_at": "now",
                        }
                    )
                )

                # User should receive a notification.
                msg = ws_notifications.receive_json()
                assert msg.get("type") == "device_job_result"
                assert msg.get("job_id") == job_id
                assert msg.get("device_id") == "primary"
                assert isinstance(msg.get("results"), list)
