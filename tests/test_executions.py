from fastapi.testclient import TestClient
from sqlmodel import Session, select
from embeddr.commands.serve import create_app
from embeddr.db.session import get_engine
from embeddr_core.models.artifact_execution import ArtifactExecution
from embeddr.core.plugin_loader import load_python_plugins
from pathlib import Path
from uuid import uuid4
import time
import os


def test_execution_lifecycle():
    # 1. Setup App & Plugins
    app = create_app()
    client = TestClient(app)

    # Load plugins
    plugin_path = Path("/home/user/git/embeddr-net/embeddr-plugins")
    load_python_plugins(plugin_path)

    # 2. Create Execution (Action: generate_thumbnail)
    # We use a random Artifact ID, so we expect it to fail inside the plugin logic
    # but the execution framework should handle the failure gracefully.
    dummy_id = str(uuid4())

    response = client.post("/api/v2/executions/", json={
        "plugin_name": "embeddr-thumbnailer",
        "action_name": "generate_thumbnail",
        "inputs": {"artifact_id": dummy_id, "preview_size": 50},
    })

    assert response.status_code == 200, f"Response: {response.text}"
    data = response.json()
    assert data["status"] == "pending"
    assert data["plugin_name"] == "embeddr-thumbnailer"

    execution_id = data["id"]

    # 3. Check result
    # TestClient runs background tasks immediately after the request handler returns in valid cases,
    # but sometimes async/sync bridge might delay.

    # Poll for status update
    for _ in range(5):
        resp = client.get(f"/api/v2/executions/{execution_id}")
        d = resp.json()
        if d["status"] != "pending" and d["status"] != "running":
            break
        time.sleep(0.5)

    final_resp = client.get(f"/api/v2/executions/{execution_id}")
    final_data = final_resp.json()

    # It should have failed because artifact doesn't exist
    assert final_data["status"] == "failed"
    assert "not found" in (final_data.get("error") or "")

    print("Execution test passed!")


if __name__ == "__main__":
    test_execution_lifecycle()
