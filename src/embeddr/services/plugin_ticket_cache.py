from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

_DEFAULT_TTL_SECONDS = 900


@dataclass
class _TicketRecord:
    namespace: str
    payload: Any
    expires_at: float


_TICKETS: Dict[str, _TicketRecord] = {}
_LOCK = threading.Lock()


def _cleanup_expired_locked(now: Optional[float] = None) -> None:
    now_ts = now if now is not None else time.time()
    expired = [token for token, rec in _TICKETS.items()
               if rec.expires_at <= now_ts]
    for token in expired:
        _TICKETS.pop(token, None)


def issue_plugin_ticket(
    namespace: str,
    payload: Any,
    *,
    ttl_seconds: int = _DEFAULT_TTL_SECONDS,
) -> Optional[str]:
    clean_namespace = str(namespace or "").strip()
    if not clean_namespace:
        return None
    if payload is None:
        return None

    token = f"ptk_{secrets.token_urlsafe(32)}"
    expires_at = time.time() + max(30, int(ttl_seconds or _DEFAULT_TTL_SECONDS))

    with _LOCK:
        _cleanup_expired_locked()
        _TICKETS[token] = _TicketRecord(
            namespace=clean_namespace,
            payload=payload,
            expires_at=expires_at,
        )

    return token


def read_plugin_ticket(namespace: str, token: str) -> Any:
    clean_namespace = str(namespace or "").strip()
    clean_token = str(token or "").strip()
    if not clean_namespace or not clean_token:
        return None

    with _LOCK:
        _cleanup_expired_locked()
        record = _TICKETS.get(clean_token)

    if not record:
        return None
    if record.namespace != clean_namespace:
        return None
    return record.payload
