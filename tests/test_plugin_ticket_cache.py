from __future__ import annotations

from embeddr.services import plugin_ticket_cache as tickets


def _clear_tickets() -> None:
    with tickets._LOCK:
        tickets._TICKETS.clear()


def test_issue_and_read_ticket():
    _clear_tickets()
    token = tickets.issue_plugin_ticket(
        "plugin.example",
        {"foo": "bar"},
        ttl_seconds=120,
    )
    assert token

    payload = tickets.read_plugin_ticket("plugin.example", token)
    assert payload == {"foo": "bar"}


def test_namespace_mismatch_returns_none():
    _clear_tickets()
    token = tickets.issue_plugin_ticket(
        "plugin.alpha",
        {"foo": "bar"},
        ttl_seconds=120,
    )
    assert token

    payload = tickets.read_plugin_ticket("plugin.beta", token)
    assert payload is None


def test_expired_ticket_returns_none(monkeypatch):
    _clear_tickets()
    current = [1000.0]

    monkeypatch.setattr(tickets.time, "time", lambda: current[0])
    token = tickets.issue_plugin_ticket(
        "plugin.example", {"x": 1}, ttl_seconds=60)
    assert token

    current[0] = 1100.0
    payload = tickets.read_plugin_ticket("plugin.example", token)
    assert payload is None
