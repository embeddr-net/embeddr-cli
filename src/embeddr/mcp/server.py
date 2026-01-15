# Import tools to register them with the MCP instance
from embeddr.mcp.instance import mcp
import embeddr.mcp.tools.library
import embeddr.mcp.tools.collections
# import embeddr.mcp.tools.search
# import embeddr.mcp.tools.workflows
from embeddr_core.plugin_interface import PluginIntent
from embeddr.core.plugin_loader import get_plugins_by_intent
import logging

logger = logging.getLogger(__name__)

# Export mcp for use in other modules (e.g. serve.py)
__all__ = ["mcp", "register_plugin_tools"]


def register_plugin_tools():
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
