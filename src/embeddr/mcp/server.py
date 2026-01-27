from embeddr_core.plugin_interface import PluginIntent
from embeddr.core.plugin_loader import get_plugins_by_intent
from embeddr.mcp.tools.collections import register_collection_tools
from embeddr.mcp.tools.library import register_library_resources
import logging

logger = logging.getLogger(__name__)

__all__ = ["register_plugin_tools", "register_core_tools"]


def register_plugin_tools(mcp):
    """
    Iterate over loaded plugins and register their MCP tools.
    """
    plugins = get_plugins_by_intent(PluginIntent.REGISTER_MCP_TOOL)
    logger.info(f"Registering MCP tools from {len(plugins)} plugins...")

    for p in plugins:
        try:
            tools = p.register_mcp_tools()
            for tool_def in tools:
                name = tool_def.get("name")
                desc = tool_def.get("description")
                handler = tool_def.get("handler")

                if not name or not handler:
                    continue

                # Use FastMCP decorator programmatically
                # This registers the tool with the internal registry
                mcp.tool(name=name, description=desc)(handler)
                logger.debug(f"Registered plugin tool: {name} from {p.name}")
        except Exception as e:
            logger.error(f"Failed to register tools for plugin {p.name}: {e}")


def register_core_tools(mcp) -> None:
    register_library_resources(mcp)
    register_collection_tools(mcp)
