from typing import Dict, List, Any, Callable
import time
import logging
import asyncio
import inspect
from embeddr_core.plugin_interface import EventBus, EmbeddrEvent
from embeddr_core.async_compat import run_sync

logger = logging.getLogger(__name__)


class SimpleEventBus(EventBus):
    def __init__(self):
        self._subscribers: Dict[str, List[Callable[[EmbeddrEvent], None]]] = {}

    def subscribe(self, event_type: str, callback: Callable[[EmbeddrEvent], None]) -> None:
        if event_type not in self._subscribers:
            self._subscribers[event_type] = []
        self._subscribers[event_type].append(callback)
        logger.debug(f"Subscribed to {event_type}")

    def publish(self, event: EmbeddrEvent) -> None:
        if event.timestamp == 0.0:
            event.timestamp = time.time()

        logger.debug(
            f"Publishing event: {event.event_type} from {event.source}")

        # Notify specific subscribers
        if event.event_type in self._subscribers:
            for callback in self._subscribers[event.event_type]:
                try:
                    res = callback(event)
                    if inspect.isawaitable(res):
                        try:
                            loop = asyncio.get_running_loop()
                            loop.create_task(res)
                        except RuntimeError:
                            try:
                                run_sync(res)
                            except Exception as sync_err:
                                logger.error(
                                    f"Failed to run async handler for {event.event_type}: {sync_err}")

                except Exception as e:
                    logger.error(
                        f"Error in subscriber for {event.event_type}: {e}")

        # Notify wildcard subscribers
        if "*" in self._subscribers:
            for callback in self._subscribers["*"]:
                try:
                    res = callback(event)
                    if inspect.isawaitable(res):
                        try:
                            loop = asyncio.get_running_loop()
                            loop.create_task(res)
                        except RuntimeError:
                            try:
                                run_sync(res)
                            except Exception as sync_err:
                                logger.error(
                                    f"Failed to run async wildcard handler for {event.event_type}: {sync_err}")
                except Exception as e:
                    logger.error(
                        f"Error in wildcard subscriber for {event.event_type}: {e}")

    def emit(self, event_type: str, payload: Any = None, source: str = "system") -> None:
        logger.debug("BUS EMIT %s source=%s payload=%s",
                     event_type, source, payload)

        event = EmbeddrEvent(
            event_type=event_type,
            source=source,
            payload=payload or {},
            timestamp=time.time(),
        )
        self.publish(event)


# Global Event Bus Instance
_EVENT_BUS = SimpleEventBus()
