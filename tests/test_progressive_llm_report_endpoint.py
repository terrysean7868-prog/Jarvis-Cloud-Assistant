import pytest
from fastapi.testclient import TestClient

from apps.web.app import app


@pytest.mark.parametrize("path", ["/api/admin/updates/progressive-report"])
def test_progressive_report_requires_admin(path):
    client = TestClient(app)
    response = client.get(path, params={"session_id": "invalid-session"})
    assert response.status_code in (401, 403)
