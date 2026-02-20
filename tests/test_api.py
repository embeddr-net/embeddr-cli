"""
API integration tests for system endpoints.

Tests the /api/v1/system/ routes against an in-memory test engine
provisioned by the ``client`` fixture in conftest.py.
"""


def test_system_info(client):
    """GET /api/v1/system/info returns version and stats."""
    response = client.get("/api/v1/system/info")
    assert response.status_code == 200
    data = response.json()
    assert "version" in data
    assert "stats" in data
    assert "db" in data


def test_system_public(client):
    """GET /api/v1/system/public returns instance profile."""
    response = client.get("/api/v1/system/public")
    assert response.status_code == 200
    data = response.json()
    assert "dev_mode" in data


def test_404(client):
    """Unknown routes return 404."""
    response = client.get("/api/v1/nonexistent")
    assert response.status_code in (404, 405)
