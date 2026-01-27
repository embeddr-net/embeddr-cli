from typing import Dict, List, Any, Callable
import time
import logging
import asyncio
import inspect
from embeddr_core.plugin_interface import EventBus, EmbeddrEvent

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
                            # Schedule async callbacks on the running loop
                            loop = asyncio.get_running_loop()
                            loop.create_task(res)
                        except RuntimeError:
                            # No running loop (e.g. called from synchronous script context, or uvicorn threaded context)
                            # Create a new loop or run locally?
                            # If we are in a thread without a loop, we can't easily "create_task".
                            # But if this is called from an async endpoint, get_running_loop SHOULD works.
                            # If it's called from a sync endpoint in FastAPI, it runs in a threadpool.

                            # Fallback: Create a Fire-and-Forget task via run_coroutine_threadsafe if we can find a main loop,
                            # or just run_until_complete if we don't care about blocking (we do).

                            # Hack: Just run it synchronously if we can't find a loop? No, that blocks.
                            # Better: Print a clearer warning and hint at using background tasks.

                            # Actually, for the "Async event handler called outside of event loop" error:
                            # This usually happens when `publish` is called from standard def (sync) code,
                            # AND there is no *active* loop in that specific thread.

                            if hasattr(asyncio, 'to_thread'):
                                # If we are in a sync thread, maybe we can run it?
                                # But `res` is a coroutine object. We need an event loop to run it.

                                # Try to get a new loop for this thread?
                                try:
                                    logger.warning(
                                        f"Creating ephemeral loop for {event.event_type}")
                                    asyncio.run(res)
                                except Exception as loop_err:
                                    logger.error(
                                        f"Failed to run ephemeral loop: {loop_err}")
                            else:
                                logger.warning(
                                    f"Async event handler {callback} called outside of event loop for {event.event_type}")

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
                            logger.warning(
                                f"Async wildcard handler called outside loop")
                except Exception as e:
                    logger.error(
                        f"Error in wildcard subscriber for {event.event_type}: {e}")

    def emit(self, event_type: str, payload: Any = None, source: str = "system") -> None:
        logger.info("BUS EMIT %s source=%s payload=%s",
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
