import time
import requests
import httpx
import asyncio
import mimetypes
from typing import Dict, Any, Optional

from embeddr.core.config import settings


class ComfyClient:
    def __init__(self, url: str = None):
        self.url = (url or settings.COMFYUI_URL).rstrip("/")

    def is_available(self) -> bool:
        try:
            requests.get(f"{self.url}/system_stats", timeout=2)
            return True
        except Exception as e:
            print(f"Error checking availability: {e}")
            return False

    def queue_prompt(
        self, workflow_graph: Dict[str, Any], client_id: Optional[str] = None
    ) -> str:
        p = {"prompt": workflow_graph}
        if client_id:
            p["client_id"] = client_id

        resp = requests.post(f"{self.url}/prompt", json=p)
        resp.raise_for_status()
        return resp.json()["prompt_id"]

    def get_history(self, prompt_id: str) -> Dict[str, Any]:
        resp = requests.get(f"{self.url}/history/{prompt_id}")
        resp.raise_for_status()
        return resp.json()

    def wait_for_completion(
        self, prompt_id: str, timeout: int = 300
    ) -> Optional[Dict[str, Any]]:
        """Wait for the workflow to complete and return the history entry."""
        start = time.time()
        while time.time() - start < timeout:
            try:
                history = self.get_history(prompt_id)
                if prompt_id in history:
                    return history[prompt_id]
            except Exception as e:
                print(f"Error getting history: {e}")
                pass
            time.sleep(1)
        return None

    def upload_image(
        self, image_data: bytes, filename: str, overwrite: bool = False
    ) -> Dict[str, Any]:
        """
        Upload an image to ComfyUI.
        """
        content_type, _ = mimetypes.guess_type(filename)
        if content_type is None:
            content_type = "application/octet-stream"

        files = {"image": (filename, image_data, content_type)}
        data = {"overwrite": "true" if overwrite else "false"}

        resp = requests.post(
            f"{self.url}/upload/image", files=files, data=data, timeout=60
        )
        resp.raise_for_status()
        return resp.json()


class AsyncComfyClient:
    def __init__(self, url: str = None):
        self.url = (url or settings.COMFYUI_URL).rstrip("/")
        self.client = httpx.AsyncClient(base_url=self.url, timeout=60)

    async def close(self):
        await self.client.aclose()

    async def is_available(self) -> bool:
        try:
            await self.client.get("/system_stats", timeout=2)
            return True
        except Exception as e:
            print(f"Error checking availability: {e}")
            return False

    async def queue_prompt(
        self, workflow_graph: Dict[str, Any], client_id: Optional[str] = None
    ) -> str:
        p = {"prompt": workflow_graph}
        if client_id:
            p["client_id"] = client_id

        resp = await self.client.post("/prompt", json=p)
        resp.raise_for_status()
        return resp.json()["prompt_id"]

    async def get_history(self, prompt_id: str) -> Dict[str, Any]:
        resp = await self.client.get(f"/history/{prompt_id}")
        resp.raise_for_status()
        return resp.json()

    async def get_object_info(self) -> Dict[str, Any]:
        """Get object info (node definitions) from ComfyUI."""
        resp = await self.client.get("/object_info")
        resp.raise_for_status()
        return resp.json()

    async def wait_for_completion(
        self, prompt_id: str, timeout: int = 300
    ) -> Optional[Dict[str, Any]]:
        """Wait for the workflow to complete and return the history entry."""
        start = time.time()
        while time.time() - start < timeout:
            try:
                history = await self.get_history(prompt_id)
                if prompt_id in history:
                    return history[prompt_id]
            except Exception as e:
                print(f"Error getting history: {e}")
                pass
            await asyncio.sleep(1)
        return None

    async def upload_image(
        self, image_data: bytes, filename: str, overwrite: bool = False
    ) -> Dict[str, Any]:
        """
        Upload an image to ComfyUI.
        """
        content_type, _ = mimetypes.guess_type(filename)
        if content_type is None:
            content_type = "application/octet-stream"

        files = {"image": (filename, image_data, content_type)}
        data = {"overwrite": "true" if overwrite else "false"}

        resp = await self.client.post("/upload/image", files=files, data=data)
        resp.raise_for_status()
        return resp.json()
