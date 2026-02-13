"""
Worker WebSocket client — connects to the main embeddr instance and handles
registration, heartbeats, and job dispatch.
"""
from __future__ import annotations

import asyncio
import json
import logging
import platform
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from urllib.parse import urljoin

logger = logging.getLogger("embeddr.worker.client")

HEARTBEAT_INTERVAL = 15  # seconds
RECONNECT_DELAY = 5  # seconds
MAX_RECONNECT_DELAY = 60  # seconds


@dataclass
class WorkerConfig:
    """Configuration for a worker node."""
    main_url: str
    worker_key: str
    worker_name: str = ""
    tags: list[str] = field(default_factory=list)
    max_concurrent_jobs: int = 4

    def __post_init__(self):
        if not self.worker_name:
            self.worker_name = platform.node() or "worker"

    @classmethod
    def from_env(cls) -> "WorkerConfig":
        """Build config from environment variables set by the serve command."""
        main_url = os.environ.get("EMBEDDR_WORKER_MAIN_URL", "")
        worker_key = os.environ.get("EMBEDDR_WORKER_KEY", "")
        worker_name = os.environ.get("EMBEDDR_WORKER_NAME", "")
        tags_raw = os.environ.get("EMBEDDR_WORKER_TAGS", "")
        tags = [t.strip() for t in tags_raw.split(
            ",") if t.strip()] if tags_raw else []

        return cls(
            main_url=main_url,
            worker_key=worker_key,
            worker_name=worker_name,
            tags=tags,
        )


@dataclass
class WorkerHealth:
    """System health metrics for the worker."""
    active_jobs: int = 0
    queue_depth: int = 0
    cpu_usage: float = 0.0
    memory_usage: float = 0.0
    gpu_usage: float = 0.0

    def collect(self) -> dict[str, Any]:
        """Collect current system metrics."""
        data: dict[str, Any] = {
            "active_jobs": self.active_jobs,
            "queue_depth": self.queue_depth,
        }
        try:
            import psutil
            data["cpu_usage"] = psutil.cpu_percent(interval=0)
            mem = psutil.virtual_memory()
            data["memory_usage"] = mem.percent
        except ImportError:
            pass

        return data


@dataclass
class WorkerState:
    """Tracks the runtime state of the worker."""
    connected: bool = False
    registered: bool = False
    draining: bool = False
    active_jobs: dict[str, asyncio.Task] = field(default_factory=dict)
    capabilities: list[str] = field(default_factory=list)
    plugins: list[str] = field(default_factory=list)
    services: dict[str, dict[str, str]] = field(default_factory=dict)


class WorkerClient:
    """
    WebSocket client that connects a worker to the main embeddr instance.

    Handles:
    - Registration with the main instance
    - Periodic heartbeats  
    - Job dispatch and result reporting
    - Automatic reconnection
    """

    def __init__(
        self,
        config: WorkerConfig,
        job_handler: Optional[Callable] = None,
    ):
        self.config = config
        self.job_handler = job_handler
        self.state = WorkerState()
        self.health = WorkerHealth()
        self._ws = None
        self._running = False
        self._reconnect_delay = RECONNECT_DELAY

    def _ws_url(self) -> str:
        """Build the WebSocket URL for connecting to the main instance."""
        base = self.config.main_url.rstrip("/")
        # Convert http(s) to ws(s)
        if base.startswith("https://"):
            base = "wss://" + base[8:]
        elif base.startswith("http://"):
            base = "ws://" + base[7:]
        return f"{base}/api/v1/workers/connect"

    def _headers(self) -> dict[str, str]:
        """Auth headers for the WebSocket connection."""
        return {"X-API-Key": self.config.worker_key}

    async def start(self) -> None:
        """Start the worker client loop (connect, register, heartbeat)."""
        self._running = True
        logger.info(
            "Worker '%s' starting, connecting to %s",
            self.config.worker_name,
            self.config.main_url,
        )

        while self._running:
            try:
                await self._connect_and_run()
            except Exception as e:
                logger.error("Worker connection error: %s", e)

            if not self._running:
                break

            # Exponential backoff on reconnect
            delay = min(self._reconnect_delay, MAX_RECONNECT_DELAY)
            logger.info("Reconnecting in %ds...", delay)
            await asyncio.sleep(delay)
            self._reconnect_delay = min(
                self._reconnect_delay * 2, MAX_RECONNECT_DELAY
            )

    async def stop(self) -> None:
        """Gracefully stop the worker client."""
        self._running = False
        self.state.connected = False
        self.state.registered = False

        # Cancel active jobs
        for job_id, task in self.state.active_jobs.items():
            logger.info("Cancelling job %s", job_id)
            task.cancel()
        self.state.active_jobs.clear()

        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass

        logger.info("Worker '%s' stopped", self.config.worker_name)

    async def _connect_and_run(self) -> None:
        """Connect to the main instance and run the message loop."""
        try:
            import websockets
        except ImportError:
            logger.error(
                "websockets package required for worker mode. "
                "Install with: pip install websockets"
            )
            self._running = False
            return

        ws_url = self._ws_url()
        logger.info("Connecting to %s", ws_url)

        async with websockets.connect(
            ws_url,
            additional_headers=self._headers(),
            ping_interval=20,
            ping_timeout=10,
        ) as ws:
            self._ws = ws
            self.state.connected = True
            self._reconnect_delay = RECONNECT_DELAY  # Reset on successful connect
            logger.info("Connected to main instance")

            # Register ourselves
            await self._send_registration()

            # Start heartbeat task
            heartbeat_task = asyncio.create_task(self._heartbeat_loop())

            try:
                async for message in ws:
                    await self._handle_message(message)
            finally:
                heartbeat_task.cancel()
                self.state.connected = False
                self.state.registered = False

    async def _send_registration(self) -> None:
        """Send registration message to the main instance."""
        system_info = {
            "hostname": platform.node(),
            "platform": platform.system(),
            "python_version": platform.python_version(),
            "cpu_count": os.cpu_count() or 1,
        }

        # Try to get GPU info
        try:
            import subprocess
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=name,memory.total",
                    "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                system_info["gpu"] = result.stdout.strip().split("\n")[0]
        except Exception:
            pass

        # Try to get total memory
        try:
            import psutil
            mem = psutil.virtual_memory()
            system_info["memory_gb"] = round(mem.total / (1024 ** 3), 1)
        except ImportError:
            pass

        # Build the worker's own HTTP URL so the main instance can proxy calls
        host = os.environ.get("EMBEDDR_HOST", "127.0.0.1")
        port = os.environ.get("EMBEDDR_PORT", "8003")
        http_url = f"http://{host}:{port}"

        msg = {
            "type": "worker:register",
            "data": {
                "name": self.config.worker_name,
                "tags": self.config.tags,
                "plugins": self.state.plugins,
                "capabilities": self.state.capabilities,
                "services": self.state.services,
                "max_concurrent_jobs": self.config.max_concurrent_jobs,
                "system": system_info,
                "http_url": http_url,
            },
        }
        await self._send(msg)
        logger.info("Registration sent (http_url=%s)", http_url)

    async def _heartbeat_loop(self) -> None:
        """Send periodic heartbeats to the main instance."""
        while self._running and self.state.connected:
            try:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                metrics = self.health.collect()
                metrics["active_jobs"] = len(self.state.active_jobs)
                await self._send({
                    "type": "worker:heartbeat",
                    "data": metrics,
                })
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Heartbeat error: %s", e)

    async def _handle_message(self, raw: str) -> None:
        """Handle an incoming message from the main instance."""
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Received invalid JSON: %s", raw[:200])
            return

        msg_type = msg.get("type", "")
        data = msg.get("data", {})

        if msg_type == "worker:registered":
            self.state.registered = True
            worker_id = data.get("worker_id", "unknown")
            logger.info(
                "Registered with main instance (worker_id=%s)",
                worker_id,
            )

            # Log instance banner / MOTD if provided
            instance = data.get("instance")
            if instance:
                inst_name = instance.get("name", "Embeddr")
                inst_ver = instance.get("version", "?")
                motd = instance.get("motd")
                banner_text = instance.get("banner_text")
                banner_enabled = instance.get("banner_enabled", False)
                desc = instance.get("description")

                logger.info("╔══════════════════════════════════════════════╗")
                logger.info("║  Connected to: %-28s ║", inst_name)
                logger.info("║  Version:      %-28s ║", inst_ver)
                if desc:
                    # Truncate long descriptions for the banner
                    short_desc = desc[:28] if len(
                        desc) <= 28 else desc[:25] + "..."
                    logger.info("║  %-42s  ║", short_desc)
                logger.info("╚══════════════════════════════════════════════╝")
                if motd:
                    logger.info("MOTD: %s", motd)
                if banner_enabled and banner_text:
                    logger.info("Banner [%s]: %s", instance.get(
                        "banner_type", "info"), banner_text)

        elif msg_type == "worker:job":
            await self._handle_job(data)

        elif msg_type == "worker:cancel":
            job_id = data.get("job_id")
            if job_id and job_id in self.state.active_jobs:
                self.state.active_jobs[job_id].cancel()
                logger.info("Cancelled job %s", job_id)

        elif msg_type == "worker:drain":
            self.state.draining = True
            logger.info("Entering drain mode — no new jobs will be accepted")

        elif msg_type == "error":
            logger.error("Error from main: %s", data.get("message", "unknown"))

        else:
            logger.debug("Unhandled message type: %s", msg_type)

    async def _handle_job(self, data: dict) -> None:
        """Handle an incoming job dispatch."""
        job_id = data.get("job_id")
        if not job_id:
            logger.warning("Received job without job_id")
            return

        if self.state.draining:
            await self._send({
                "type": "worker:reject",
                "data": {"job_id": job_id, "reason": "draining"},
            })
            return

        if len(self.state.active_jobs) >= self.config.max_concurrent_jobs:
            await self._send({
                "type": "worker:reject",
                "data": {"job_id": job_id, "reason": "at_capacity"},
            })
            return

        logger.info("Accepting job %s (capability=%s)",
                    job_id, data.get("capability"))

        task = asyncio.create_task(self._run_job(job_id, data))
        self.state.active_jobs[job_id] = task

    async def _run_job(self, job_id: str, data: dict) -> None:
        """Execute a job and report results."""
        try:
            await self._send({
                "type": "worker:progress",
                "data": {"job_id": job_id, "progress": 0.0, "stage": "starting"},
            })

            if self.job_handler:
                result = await self.job_handler(job_id, data, self._report_progress)
            else:
                logger.warning(
                    "No job handler registered, failing job %s", job_id)
                result = None

            if result is not None:
                await self._send({
                    "type": "worker:complete",
                    "data": {"job_id": job_id, "output": result},
                })
            else:
                await self._send({
                    "type": "worker:error",
                    "data": {"job_id": job_id, "error": "No job handler or handler returned None"},
                })

        except asyncio.CancelledError:
            await self._send({
                "type": "worker:error",
                "data": {"job_id": job_id, "error": "Job cancelled"},
            })
        except Exception as e:
            logger.error("Job %s failed: %s", job_id, e)
            await self._send({
                "type": "worker:error",
                "data": {"job_id": job_id, "error": str(e)},
            })
        finally:
            self.state.active_jobs.pop(job_id, None)

    async def _report_progress(self, job_id: str, progress: float, stage: str = "") -> None:
        """Report job progress back to the main instance."""
        await self._send({
            "type": "worker:progress",
            "data": {"job_id": job_id, "progress": progress, "stage": stage},
        })

    async def _send(self, msg: dict) -> None:
        """Send a JSON message over the WebSocket."""
        if self._ws:
            try:
                await self._ws.send(json.dumps(msg))
            except Exception as e:
                logger.warning("Failed to send message: %s", e)
