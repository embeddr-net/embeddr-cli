"""
Plugin test helpers for Embeddr.

Provides fixtures and utilities for testing plugins in isolation.
Plugin authors can use these to test their actions, capabilities,
and lifecycle hooks without running the full server.

Usage in a test file::

    from tests.plugin_test_helpers import (
        make_plugin_context,
        assert_action_result,
        assert_capabilities_valid,
    )

    def test_my_action():
        plugin = MyPlugin()
        ctx = make_plugin_context()
        result = plugin.execute("my_action", "test-exec-id", {"key": "val"}, ctx)
        assert_action_result(result)
"""

from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock
from uuid import uuid4

from embeddr_core.plugin_interface import (
    ActionPlugin,
    EmbeddrPlugin,
    PluginContext,
)
from embeddr_core.models.lotus import LotusCapability


# ---------------------------------------------------------------------------
# PluginContext factory
# ---------------------------------------------------------------------------

def make_plugin_context(
    *,
    config: Optional[Dict[str, Any]] = None,
    bus: Optional[Any] = None,
    lotus_responses: Optional[Dict[str, Any]] = None,
    db_session_factory: Optional[Any] = None,
) -> PluginContext:
    """
    Create a lightweight PluginContext for testing.

    Args:
        config: Plugin config dict (returned by context.get_config()).
        bus: Event bus mock. Defaults to a MagicMock.
        lotus_responses: Dict mapping cap_id → return value for lotus_invoke().
        db_session_factory: DB session factory. Defaults to None (no DB).

    Returns:
        A PluginContext suitable for passing to plugin methods.
    """
    mock_bus = bus or MagicMock()
    _lotus_responses = lotus_responses or {}

    mock_lotus = MagicMock()
    mock_lotus.invoke_action = MagicMock(
        side_effect=lambda cap_id, inputs: _lotus_responses.get(
            cap_id, {"ok": True}
        )
    )

    return PluginContext(
        bus=mock_bus,
        config=config or {},
        lotus=mock_lotus,
        db_session_factory=db_session_factory,
        resources=MagicMock(),
    )


# ---------------------------------------------------------------------------
# Action execution helpers
# ---------------------------------------------------------------------------

def execute_action(
    plugin: EmbeddrPlugin,
    action_name: str,
    inputs: Optional[Dict[str, Any]] = None,
    *,
    context: Optional[PluginContext] = None,
    execution_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Execute a plugin action and return the result.

    Handles both ActionPlugin (preferred) and manual-dispatch plugins.
    """
    ctx = context or make_plugin_context()
    exec_id = execution_id or str(uuid4())
    return plugin.execute(action_name, exec_id, inputs or {}, ctx)


def assert_action_result(
    result: Dict[str, Any],
    *,
    ok: Optional[bool] = True,
    has_keys: Optional[List[str]] = None,
) -> None:
    """
    Assert common properties of an action result.

    Args:
        result: The dict returned by plugin.execute().
        ok: Expected value of result["ok"]. Set to None to skip.
        has_keys: List of keys that must be present in the result.
    """
    assert isinstance(result, dict), f"Expected dict result, got {type(result)}"
    if ok is not None:
        assert result.get("ok") is ok, (
            f"Expected ok={ok}, got ok={result.get('ok')}"
            f" — error: {result.get('error', 'none')}"
        )
    if has_keys:
        for key in has_keys:
            assert key in result, f"Missing key '{key}' in result: {list(result.keys())}"


# ---------------------------------------------------------------------------
# Capability validation
# ---------------------------------------------------------------------------

def assert_capabilities_valid(
    capabilities: List[LotusCapability],
    *,
    plugin_name: Optional[str] = None,
    min_count: int = 1,
) -> None:
    """
    Validate that a plugin's registered capabilities are well-formed.

    Checks:
    - At least min_count capabilities registered
    - Each has an id and kind
    - Each has a title
    - Plugin name matches if specified
    """
    assert len(capabilities) >= min_count, (
        f"Expected >= {min_count} capabilities, got {len(capabilities)}"
    )

    for cap in capabilities:
        assert cap.id, f"Capability missing id: {cap}"
        assert cap.kind, f"Capability {cap.id} missing kind"
        assert cap.title, f"Capability {cap.id} missing title"

        if plugin_name and cap.plugin:
            assert cap.plugin == plugin_name, (
                f"Capability {cap.id} has plugin={cap.plugin}, "
                f"expected {plugin_name}"
            )


def assert_action_capabilities_match_methods(plugin: ActionPlugin) -> None:
    """
    Validate that an ActionPlugin's @action methods have corresponding
    Lotus capabilities registered.
    """
    caps = plugin.register_lotus()
    cap_ids = {cap.id for cap in caps}

    # Get all @action-decorated method names
    action_names = set()
    for _, method in plugin._iter_action_methods():
        action_name = getattr(method, "_embeddr_action_name", None)
        if action_name:
            action_names.add(action_name)

    # Each action should have a corresponding capability
    # (capabilities may have prefixes like "plugin_name.action_name")
    for action_name in action_names:
        matching = [
            cid for cid in cap_ids
            if action_name in cid or cid.endswith(f".{action_name}")
        ]
        assert matching, (
            f"Action '{action_name}' has no matching capability. "
            f"Registered cap IDs: {sorted(cap_ids)}"
        )


# ---------------------------------------------------------------------------
# Lifecycle helpers
# ---------------------------------------------------------------------------

def run_plugin_lifecycle(
    plugin: EmbeddrPlugin,
    *,
    context: Optional[PluginContext] = None,
) -> PluginContext:
    """
    Run on_load() and on_startup() lifecycle hooks.
    Returns the context used (for inspection).
    """
    ctx = context or make_plugin_context()
    plugin.on_load(ctx)
    plugin.on_startup(ctx)
    return ctx
