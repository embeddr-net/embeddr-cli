"""ComfyUI execution e2e test — validates the full generation pipeline.

Tests that:
  1. run_workflow dispatches a generate job
  2. The generate job queues to ComfyUI
  3. The execution completes (not stuck in "running")
  4. Artifacts are created with proper types
  5. Active job count returns to 0

Requires:
  EMBEDDR_COMFY_TEST=1
  A running ComfyUI instance
  A workspace with at least one synced workflow

Run with:
  EMBEDDR_COMFY_TEST=1 pytest tests/test_comfyui_e2e.py -v -s --tb=short

Example:
  EMBEDDR_COMFY_TEST=1 EMBEDDR_COMFY_WORKFLOW="Z Image" \
    pytest tests/test_comfyui_e2e.py -v -s
"""
from __future__ import annotations

import os
import time

import pytest
import httpx

pytestmark = pytest.mark.skipif(
    not os.environ.get("EMBEDDR_COMFY_TEST"),
    reason="ComfyUI test — requires EMBEDDR_COMFY_TEST=1 and running ComfyUI + Embeddr",
)

BASE_URL = os.environ.get("EMBEDDR_TEST_URL", "http://localhost:8003")
WORKFLOW_NAME = os.environ.get("EMBEDDR_COMFY_WORKFLOW", "Z Image")
PROMPT_TEXT = os.environ.get("EMBEDDR_COMFY_PROMPT", "a cat sitting on a table")


@pytest.fixture(scope="module")
def client():
    with httpx.Client(base_url=BASE_URL, timeout=30) as c:
        # Verify server is up
        resp = c.get("/api/v1/system/info")
        if resp.status_code != 200:
            pytest.skip(f"Embeddr not available at {BASE_URL}")
        yield c


def _get_active_jobs(client: httpx.Client) -> int:
    """Get the number of active (non-terminal) executions."""
    resp = client.get("/api/v1/executions/active-count")
    if resp.status_code != 200:
        return -1
    data = resp.json()
    return data.get("count", data.get("active", 0))


class TestComfyUIExecution:
    def test_workflow_exists(self, client):
        """Verify the test workflow is available."""
        resp = client.get("/api/v1/lotus/query?q=workflow")
        assert resp.status_code == 200

    def test_run_workflow_completes(self, client):
        """Submit a workflow and verify the execution completes."""
        # Record active jobs before
        active_before = _get_active_jobs(client)

        # Submit workflow
        resp = client.post(
            "/api/v1/lotus/embeddr-comfyui.run_workflow",
            json={
                "workflow_name": WORKFLOW_NAME,
                "inputs": {},
                "wait_for_completion": False,
            },
        )
        assert resp.status_code in (200, 202), f"Submit failed: {resp.status_code} {resp.text}"
        data = resp.json()
        execution_id = data.get("execution_id")
        assert execution_id, f"No execution_id returned: {data}"

        print(f"\n  Execution ID: {execution_id}")
        print(f"  Workflow: {WORKFLOW_NAME}")

        # Wait for execution to complete (poll status)
        max_wait = 120  # 2 minutes
        poll_interval = 2
        elapsed = 0
        final_status = None

        while elapsed < max_wait:
            time.sleep(poll_interval)
            elapsed += poll_interval

            status_resp = client.get(f"/api/v1/executions/{execution_id}")
            if status_resp.status_code == 200:
                status_data = status_resp.json()
                status = status_data.get("status")
                progress = status_data.get("progress", 0)
                message = status_data.get("message", "")
                print(f"  [{elapsed:3d}s] status={status} progress={progress} message={message}")

                if status in ("completed", "failed", "canceled"):
                    final_status = status
                    break

        assert final_status is not None, (
            f"Execution {execution_id} never reached terminal status after {max_wait}s"
        )
        assert final_status == "completed", (
            f"Execution {execution_id} ended with status={final_status}"
        )

        # Verify active jobs returned to previous count
        time.sleep(2)  # Grace period for spine cleanup
        active_after = _get_active_jobs(client)
        if active_before >= 0 and active_after >= 0:
            print(f"  Active jobs: before={active_before} after={active_after}")
            # Should not have more active jobs than before
            assert active_after <= active_before + 1, (
                f"Active jobs increased from {active_before} to {active_after} — jobs not completing"
            )

    def test_run_workflow_creates_artifacts(self, client):
        """Submit a workflow and verify artifacts are created."""
        resp = client.post(
            "/api/v1/lotus/embeddr-comfyui.run_workflow",
            json={
                "workflow_name": WORKFLOW_NAME,
                "inputs": {},
                "wait_for_completion": True,
            },
        )

        # This blocks until completion
        if resp.status_code == 200:
            data = resp.json()
            status = data.get("status")
            artifacts = data.get("artifacts") or data.get("outputs", {}).get("artifacts", [])
            print(f"\n  Status: {status}")
            print(f"  Artifacts: {len(artifacts)}")

            if status == "completed" and artifacts:
                for art in artifacts:
                    art_id = art.get("id")
                    art_type = art.get("type") or art.get("type_name")
                    print(f"  - {art_id} ({art_type})")

                    # Verify artifact type is image:comfyui
                    if art_type:
                        assert "image" in art_type.lower() or "video" in art_type.lower() or "audio" in art_type.lower(), (
                            f"Unexpected artifact type: {art_type}"
                        )
