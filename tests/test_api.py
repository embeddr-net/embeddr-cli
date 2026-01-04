def test_system_status(client):
    response = client.get("/api/v1/system/status")
    assert response.status_code == 200
    data = response.json()
    assert "mcp" in data
    assert "comfy" in data
    assert "docs" in data


def test_system_info(client):
    response = client.get("/api/v1/system/info")
    assert response.status_code == 200
    data = response.json()
    assert "version" in data
    assert "stats" in data


def test_404(client):
    response = client.get("/api/v1/nonexistent")
    assert response.status_code == 404
