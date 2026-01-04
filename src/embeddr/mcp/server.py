# Import tools to register them with the MCP instance
from embeddr.mcp.instance import mcp

# Export mcp for use in other modules (e.g. serve.py)
__all__ = ["mcp"]
