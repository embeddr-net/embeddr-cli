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
                    "expose": {"lotus": True, "api": True, "cli": False},
                },
            )
        )


def _core_capabilities() -> list[LotusCapability]:
    return [
        LotusCapability(
            id="embeddr-core.transport.policy",
            kind=LotusKind.config,
            title="Transport Policy",
            description="Control which transports can invoke which capabilities.",
            plugin="embeddr-core",
            tags=["core", "transport", "policy", "config"],
            data={
                "type": "config",
                "plugin": "embeddr-core",
                "scope": "global",
                "input": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "enabled": {"type": "boolean", "default": False},
                            "default_allow": {
                                "type": "object",
                                "properties": {
                                    "api": {"type": "boolean", "default": False},
                                    "lotus": {"type": "boolean", "default": False},
                                    "cli": {"type": "boolean", "default": False},
                                },
                                "additionalProperties": {"type": "boolean"},
                            },
                            "plugin_allow": {
                                "type": "object",
                                "additionalProperties": {
                                    "type": "object",
                                    "additionalProperties": {"type": "boolean"},
                                },
                            },
                            "capability_allow": {
                                "type": "object",
                                "additionalProperties": {
                                    "type": "object",
                                    "additionalProperties": {"type": "boolean"},
                                },
                            },
                        },
                        "required": [],
                    },
                    "defaults": {
                        "enabled": False,
                        "default_allow": {
                            "api": False,
                            "lotus": False,
                            "cli": False,
                        },
                        "plugin_allow": {},
                        "capability_allow": {},
                    },
                    "ui": {
                        "order": [
                            "enabled",
                            "default_allow",
                            "plugin_allow",
                            "capability_allow",
                        ],
                        "widgets": {
                            "enabled": "checkbox",
                            "default_allow": "json",
                            "plugin_allow": "json",
                            "capability_allow": "json",
                        },
                    },
                },
            },
        ),
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
    ]
