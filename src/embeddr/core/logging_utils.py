import logging
import os
from collections import deque

log_capture_deque = deque(maxlen=1000)


class DequeHandler(logging.Handler):
    def emit(self, record):
        # Filter out the log polling requests to reduce noise
        if record.name == "uvicorn.access":
            return

        # Filter out docket and mcp logs
        if record.name.startswith("docket") or record.name.startswith("mcp"):
            return

        log_entry = self.format(record)
        log_capture_deque.append(log_entry)


def setup_log_capture():
    root_logger = logging.getLogger()
    uv_logger = logging.getLogger("uvicorn.error")
    access_logger = logging.getLogger("uvicorn.access")

    deque_handler = DequeHandler()
    deque_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    )

    root_logger.addHandler(deque_handler)
    uv_logger.addHandler(deque_handler)
    access_logger.addHandler(deque_handler)


def setup_logging(verbose: bool | None = None):
    if verbose is None:
        verbose = os.environ.get("EMBEDDR_VERBOSE", "false").lower() == "true"

    logging.basicConfig(level=logging.INFO if verbose else logging.WARNING)

    logging.getLogger("embeddr.local").setLevel(logging.INFO)
    logging.getLogger("embeddr").setLevel(
        logging.INFO if verbose else logging.WARNING)
    logging.getLogger("embeddr_core").setLevel(
        logging.INFO if verbose else logging.WARNING)
    logging.getLogger("embeddr_plugins").setLevel(
        logging.INFO if verbose else logging.WARNING)

    logging.getLogger("docket").setLevel(logging.WARNING)
    logging.getLogger("mcp").setLevel(
        logging.WARNING if not verbose else logging.INFO)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("watchfiles").setLevel(logging.WARNING)
    setup_log_capture()


def get_logs(limit: int = 100, include_filter: str = None):
    logs = list(log_capture_deque)
    if include_filter:
        logs = [log for log in logs if include_filter in log]
    return logs[-limit:]
