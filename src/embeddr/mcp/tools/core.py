import logging
from embeddr.core.event_bus import _EVENT_BUS
from datetime import datetime

from embeddr_core.plugin_interface import EmbeddrEvent

logger = logging.getLogger(__name__)

# CORE_ROUTES = {
#     "/": "Zen",
#     "/lotus": "Lotus",
# }


# def register_core_tools(mcp):
# @mcp.tool(name="ui.navigate", description="Navigate the Embeddr UI (client performs navigation).")
# def ui_navigate(route: str):
#     if route not in CORE_ROUTES:
#         return {"ok": False, "error": f"Route not allowed: {route}", "allowed": list(CORE_ROUTES.keys())}

#     _EVENT_BUS.publish(EmbeddrEvent(
#         event_type="ui:navigate",
#         source="lotus",
#         payload={"route": route,
#                  "timestamp": datetime.now().isoformat()},
#     ))

#     return {"ok": True, "navigate_to": route}

# @mcp.tool(name="ui.list_routes", description="List known core UI routes.")
# def ui_list_routes():
#     return {"ok": True, "routes": [{"route": r, "label": label} for r, label in CORE_ROUTES.items()]}

# @mcp.tool(name="ui.list_panels", description="List known UI panels.")
# def ui_list_panels():
#     return {"ok": True, "panels": []}  # Placeholder implementation

# @mcp.tool(name="ui.open_panel", description="Open a specific panel in the Embeddr UI (client performs navigation).")
# def ui_open_panel(panel: str):
#     _EVENT_BUS.publish(EmbeddrEvent(
#         event_type="ui:open_panel",
#         source="lotus",
#         payload={"panel": panel,
#                  "timestamp": datetime.now().isoformat()},
#     ))

#     return {"ok": True, "opened_panel": panel}
