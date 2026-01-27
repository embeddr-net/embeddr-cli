from __future__ import annotations

from typing import Dict, List
from embeddr_core.models.lotus import LotusCapability, LotusKind
from embeddr.core.plugin_loader import get_lotus_registry

CORE_ROUTES: Dict[str, str] = {
    "/": "Zen",
    "/lotus": "Lotus",
    "/lotus-playground": "Lotus Playground",
}


def register_core_lotus_capabilities() -> None:
    reg = get_lotus_registry()

    for cap in _core_capabilities():
        reg.register(cap)

    # NOTE: Can likely be removed?
    for route, label in CORE_ROUTES.items():
        reg.register(
            LotusCapability(
                id=f"nav:{route}",
                kind=LotusKind.nav,
                title=f"Go to {label}",
                description=f"Navigate to {route}",
                plugin="core",
                tags=["core", "nav"],
                data={
                    "type": "nav",
                    "route": route,
                    "expose": {"lotus": True, "api": True, "mcp": False, "cli": False},
                },
            )
        )


def _core_capabilities() -> list[LotusCapability]:
    return [
        LotusCapability(
            id="embeddr-core.ingest.pipeline",
            kind=LotusKind.config,
            title="Ingestion Pipeline",
            description="Configure the default ingestion pipeline automation.",
            plugin="embeddr-core",
            tags=["core", "ingest", "configuration"],
            data={
                "type": "config",
                "plugin": "embeddr-core",
                "scope": "global",
                "input": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "pipeline_id": {"type": ["string", "null"]},
                            "automation_id": {"type": ["string", "null"]},
                        },
                        "required": [],
                    },
                    "defaults": {
                        "pipeline_id": None,
                    },
                    "ui": {
                        "order": ["pipeline_id", "automation_id"],
                        "widgets": {
                            "pipeline_id": "text",
                            "automation_id": "text",
                        },
                    },
                },
            },
        ),
        LotusCapability(
            id="embeddr-core.mcp.transport",
            kind=LotusKind.config,
            title="MCP Transport",
            description="Configure MCP transport (embedded vs plugin).",
            plugin="embeddr-core",
            tags=["core", "mcp", "configuration"],
            data={
                "type": "config",
                "plugin": "embeddr-core",
                "scope": "global",
                "input": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "enabled": {"type": "boolean", "default": True},
                            "transport": {
                                "type": "string",
                                "enum": ["embedded", "plugin", "disabled"],
                                "default": "embedded",
                            },
                        },
                        "required": [],
                    },
                    "defaults": {
                        "enabled": True,
                        "transport": "embedded",
                    },
                    "ui": {
                        "order": ["enabled", "transport"],
                        "widgets": {
                            "enabled": "boolean",
                            "transport": "select",
                        },
                        "options": {
                            "transport": [
                                {"label": "Embedded", "value": "embedded"},
                                {"label": "Plugin", "value": "plugin"},
                                {"label": "Disabled", "value": "disabled"},
                            ]
                        },
                    },
                },
            },
        ),
        # LotusCapability(
        #     id="ui.list_routes",
        #     kind=LotusKind.action,
        #     title="List Core UI Routes",
        #     description="List known core UI routes.",
        #     plugin="core",
        #     tags=["core", "ui"],
        #     data={
        #         "type": "action",
        #         "plugin": "core",
        #         "action": "ui.list_routes",
        #         "expose": {"lotus": True, "api": True, "mcp": True, "cli": False},
        #         "input": {
        #             "schema": {
        #                 "type": "object",
        #                 "properties": {},
        #                 "required": [],
        #             },
        #         },
        #         "exec": {"mode": "sync"},
        #     },
        # ),
        # LotusCapability(
        #     id="ui.navigate",
        #     kind=LotusKind.action,
        #     title="Navigate UI",
        #     description="Navigate the Embeddr UI (client performs navigation).",
        #     plugin="core",
        #     tags=["core", "ui"],
        #     data={
        #         "type": "action",
        #         "plugin": "core",
        #         "action": "ui.navigate",
        #         "expose": {"lotus": True, "api": True, "mcp": True, "cli": False},
        #         "input": {
        #             "schema": {
        #                 "type": "object",
        #                 "properties": {
        #                     "route": {"type": "string", "enum": list(CORE_ROUTES.keys())}
        #                 },
        #                 "required": ["route"],
        #             },
        #         },
        #         "exec": {"mode": "sync"},
        #     },
        # ),
    ]
