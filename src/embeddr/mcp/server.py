# Import tools to register them with the MCP instance
from embeddr.mcp.instance import mcp
import embeddr.mcp.tools.library
import embeddr.mcp.tools.collections
import embeddr.mcp.tools.search
import embeddr.mcp.tools.workflows

# Export mcp for use in other modules (e.g. serve.py)
__all__ = ["mcp"]
