"""Tests for SimpleEventBus — subscribe, publish, emit, wildcards, error isolation."""

from embeddr.core.event_bus import SimpleEventBus
from embeddr_core.plugin_interface import EmbeddrEvent


def _make_event(event_type="test.event", payload=None, source="test"):
    return EmbeddrEvent(event_type=event_type, source=source, payload=payload or {})


class TestSubscribeAndPublish:
    def test_subscribe_and_publish(self, event_bus: SimpleEventBus):
        received = []
        event_bus.subscribe("test.event", lambda e: received.append(e))
        event_bus.publish(_make_event())
        assert len(received) == 1
        assert received[0].event_type == "test.event"

    def test_emit_convenience(self, event_bus: SimpleEventBus):
        received = []
        event_bus.subscribe("file.uploaded", lambda e: received.append(e))
        event_bus.emit("file.uploaded", payload={"name": "photo.jpg"}, source="uploader")
        assert len(received) == 1
        assert received[0].payload["name"] == "photo.jpg"

    def test_multiple_subscribers(self, event_bus: SimpleEventBus):
        a, b = [], []
        event_bus.subscribe("x", lambda e: a.append(1))
        event_bus.subscribe("x", lambda e: b.append(1))
        event_bus.publish(_make_event("x"))
        assert len(a) == 1 and len(b) == 1


class TestWildcardSubscriber:
    def test_wildcard_receives_all(self, event_bus: SimpleEventBus):
        all_events = []
        event_bus.subscribe("*", lambda e: all_events.append(e))
        event_bus.publish(_make_event("alpha"))
        event_bus.publish(_make_event("beta"))
        assert len(all_events) == 2

    def test_wildcard_and_specific_both_fire(self, event_bus: SimpleEventBus):
        wildcard, specific = [], []
        event_bus.subscribe("*", lambda e: wildcard.append(1))
        event_bus.subscribe("target", lambda e: specific.append(1))
        event_bus.publish(_make_event("target"))
        assert len(wildcard) == 1 and len(specific) == 1


class TestErrorIsolation:
    def test_error_in_subscriber_doesnt_break_others(self, event_bus: SimpleEventBus):
        results = []
        event_bus.subscribe("x", lambda e: results.append("before"))
        event_bus.subscribe("x", lambda e: (_ for _ in ()).throw(RuntimeError("boom")))
        event_bus.subscribe("x", lambda e: results.append("after"))
        event_bus.publish(_make_event("x"))
        # "after" should still fire despite the error in the middle subscriber
        assert "before" in results and "after" in results


class TestPublishWithNoSubscribers:
    def test_publish_with_no_subscribers(self, event_bus: SimpleEventBus):
        event_bus.publish(_make_event("nobody.listening"))  # should not raise

    def test_emit_with_no_subscribers(self, event_bus: SimpleEventBus):
        event_bus.emit("also.nobody")  # should not raise
