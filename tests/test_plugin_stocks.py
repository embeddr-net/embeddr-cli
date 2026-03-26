"""
Exemplar plugin tests — embeddr-stocks.

Demonstrates how to test an ActionPlugin:
1. Capability registration validation
2. Action dispatch with mocked dependencies
3. Input validation (Pydantic models)
4. Error handling (on_action_error)
5. Lifecycle hooks

Use this as a template for testing your own plugins.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Set up the stocks plugin as an importable package
_PLUGINS_DIR = (
    Path(__file__).resolve().parents[2]
    / "embeddr-plugins"
    / "plugins"
    / "examples"
)
_STOCKS_DIR = _PLUGINS_DIR / "embeddr-stocks"

# Register the plugin as a proper package so relative imports work
import importlib.util

_spec = importlib.util.spec_from_file_location(
    "embeddr_plugins.embeddr_stocks",
    _STOCKS_DIR / "plugin.py",
    submodule_search_locations=[str(_STOCKS_DIR)],
)

# Pre-register the package namespace and src submodule
sys.path.insert(0, str(_STOCKS_DIR))
sys.modules.setdefault("embeddr_plugins", type(sys)("embeddr_plugins"))
sys.modules["embeddr_plugins"].__path__ = [str(_PLUGINS_DIR)]  # type: ignore[attr-defined]

# Now import the plugin's src modules first
sys.modules["embeddr_plugins.embeddr_stocks"] = type(sys)("embeddr_plugins.embeddr_stocks")
sys.modules["embeddr_plugins.embeddr_stocks"].__path__ = [str(_STOCKS_DIR)]  # type: ignore[attr-defined]

# Import the src subpackage
_src_dir = _STOCKS_DIR / "src"
for mod_file in sorted(_src_dir.glob("*.py")):
    mod_name = f"embeddr_plugins.embeddr_stocks.src.{mod_file.stem}"
    _mspec = importlib.util.spec_from_file_location(mod_name, mod_file)
    if _mspec and _mspec.loader:
        _mod = importlib.util.module_from_spec(_mspec)
        sys.modules[mod_name] = _mod
        _mspec.loader.exec_module(_mod)

# Now we can import the plugin
_plugin_spec = importlib.util.spec_from_file_location(
    "embeddr_plugins.embeddr_stocks.plugin",
    _STOCKS_DIR / "plugin.py",
)
assert _plugin_spec and _plugin_spec.loader
_plugin_mod = importlib.util.module_from_spec(_plugin_spec)
sys.modules["embeddr_plugins.embeddr_stocks.plugin"] = _plugin_mod
_plugin_spec.loader.exec_module(_plugin_mod)

StocksPlugin = _plugin_mod.StocksPlugin

from plugin_test_helpers import (
    assert_action_result,
    assert_capabilities_valid,
    assert_action_capabilities_match_methods,
    execute_action,
    make_plugin_context,
    run_plugin_lifecycle,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

CSV_SEARCH_RESPONSE = """\
Symbol,Name,Exchange
AAPL.US,Apple Inc,NASDAQ
AMZN.US,Amazon.com Inc,NASDAQ
"""

CSV_QUOTE_RESPONSE = """\
Date,Open,High,Low,Close,Volume
2026-03-24,170.00,175.50,169.25,174.80,42000000
"""

CSV_TIMESERIES_RESPONSE = """\
Date,Open,High,Low,Close,Volume
2026-03-20,168.00,170.00,167.00,169.50,38000000
2026-03-21,169.50,172.00,168.50,171.00,40000000
2026-03-22,171.00,173.00,170.00,172.50,39000000
2026-03-23,172.50,174.00,171.50,173.80,41000000
2026-03-24,173.80,175.50,172.80,174.80,42000000
"""


@pytest.fixture
def plugin():
    """Fresh StocksPlugin instance."""
    return StocksPlugin()


@pytest.fixture
def context():
    """
    PluginContext with http.fetch_text stubbed to return CSV data.
    The stocks plugin uses lotus_invoke("http.fetch_text", ...) for all HTTP.
    """
    return make_plugin_context(
        lotus_responses={
            "http.fetch_text": {
                "ok": True,
                "text": CSV_SEARCH_RESPONSE,
                "status_code": 200,
            },
        },
    )


# ---------------------------------------------------------------------------
# Capability registration
# ---------------------------------------------------------------------------

class TestCapabilityRegistration:
    def test_registers_capabilities(self, plugin):
        caps = plugin.register_lotus()
        assert_capabilities_valid(caps, plugin_name="embeddr-stocks", min_count=2)

    def test_has_config_capability(self, plugin):
        caps = plugin.register_lotus()
        config_caps = [c for c in caps if c.kind == "config"]
        assert len(config_caps) >= 1, "Should register at least one config capability"

    def test_action_methods_have_capabilities(self, plugin):
        assert_action_capabilities_match_methods(plugin)

    def test_registers_zen_panels(self, plugin):
        panels = plugin.register_zen()
        assert len(panels) == 2
        names = {p.name for p in panels}
        assert "stocks-tracker" in names
        assert "stocks-chart" in names


# ---------------------------------------------------------------------------
# Action: search
# ---------------------------------------------------------------------------

class TestSearchAction:
    def test_search_returns_results(self, plugin, context):
        result = execute_action(
            plugin, "search",
            {"query": "AAPL", "limit": 10},
            context=context,
        )
        assert_action_result(result, ok=True, has_keys=["count", "items"])
        assert result["count"] > 0

    def test_search_items_have_required_fields(self, plugin, context):
        result = execute_action(
            plugin, "search",
            {"query": "AAPL", "limit": 5},
            context=context,
        )
        for item in result["items"]:
            assert "id" in item
            assert "title" in item
            assert "symbol" in item

    def test_search_empty_response(self, plugin):
        ctx = make_plugin_context(
            lotus_responses={
                "http.fetch_text": {"ok": True, "text": "", "status_code": 200},
            },
        )
        result = execute_action(
            plugin, "search",
            {"query": "NONEXISTENT", "limit": 10},
            context=ctx,
        )
        # Plugin returns ok=True with empty items when no matches
        assert_action_result(result, ok=True, has_keys=["count"])

    def test_search_http_failure_returns_gracefully(self, plugin):
        ctx = make_plugin_context(
            lotus_responses={
                "http.fetch_text": {"ok": False, "error": "timeout"},
            },
        )
        result = execute_action(
            plugin, "search",
            {"query": "AAPL", "limit": 10},
            context=ctx,
        )
        # Plugin handles HTTP failures gracefully — returns ok=True with empty results
        assert_action_result(result, ok=True, has_keys=["count"])


# ---------------------------------------------------------------------------
# Action: quote
# ---------------------------------------------------------------------------

class TestQuoteAction:
    def test_quote_returns_data(self, plugin):
        ctx = make_plugin_context(
            lotus_responses={
                "http.fetch_text": {
                    "ok": True,
                    "text": CSV_QUOTE_RESPONSE,
                    "status_code": 200,
                },
            },
        )
        result = execute_action(
            plugin, "quote",
            {"symbol": "AAPL"},
            context=ctx,
        )
        assert_action_result(result, ok=True, has_keys=["quote"])
        assert result["quote"]["symbol"] is not None

    def test_quote_empty_response(self, plugin):
        ctx = make_plugin_context(
            lotus_responses={
                "http.fetch_text": {"ok": True, "text": "", "status_code": 200},
            },
        )
        result = execute_action(
            plugin, "quote",
            {"symbol": "INVALID"},
            context=ctx,
        )
        assert_action_result(result, ok=False)


# ---------------------------------------------------------------------------
# Action: timeseries
# ---------------------------------------------------------------------------

class TestTimeseriesAction:
    def test_timeseries_returns_points(self, plugin):
        ctx = make_plugin_context(
            lotus_responses={
                "http.fetch_text": {
                    "ok": True,
                    "text": CSV_TIMESERIES_RESPONSE,
                    "status_code": 200,
                },
            },
        )
        result = execute_action(
            plugin, "timeseries",
            {"symbol": "AAPL", "limit": 100},
            context=ctx,
        )
        assert_action_result(result, ok=True, has_keys=["points", "symbol"])

    def test_timeseries_points_have_ohlcv(self, plugin):
        ctx = make_plugin_context(
            lotus_responses={
                "http.fetch_text": {
                    "ok": True,
                    "text": CSV_TIMESERIES_RESPONSE,
                    "status_code": 200,
                },
            },
        )
        result = execute_action(
            plugin, "timeseries",
            {"symbol": "AAPL", "limit": 100},
            context=ctx,
        )
        for point in result["points"]:
            assert "t" in point
            assert "open" in point
            assert "close" in point


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

class TestErrorHandling:
    def test_unknown_action_raises(self, plugin, context):
        with pytest.raises(NotImplementedError):
            execute_action(plugin, "nonexistent_action", {}, context=context)

    def test_on_action_error_returns_typed_search_response(self, plugin):
        """on_action_error should return a search-specific error response."""
        error = ValueError("test error")
        result = plugin.on_action_error(
            action_name="search",
            error=error,
            inputs={"query": "test"},
        )
        assert_action_result(result, ok=False, has_keys=["error"])
        assert "test error" in result["error"]

    def test_on_action_error_returns_typed_quote_response(self, plugin):
        """on_action_error should return a quote-specific error response."""
        error = ValueError("network timeout")
        result = plugin.on_action_error(
            action_name="quote",
            error=error,
            inputs={"symbol": "AAPL"},
        )
        assert_action_result(result, ok=False, has_keys=["error"])


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

class TestLifecycle:
    def test_lifecycle_hooks_dont_crash(self, plugin):
        """on_load and on_startup should run without error."""
        ctx = run_plugin_lifecycle(plugin)
        assert ctx is not None

    def test_plugin_metadata(self, plugin):
        assert plugin.name == "embeddr-stocks"
        assert plugin.version == "1.0.0"
