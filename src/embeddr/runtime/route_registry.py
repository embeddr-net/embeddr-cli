from __future__ import annotations

from typing import Any, List, Sequence, Tuple


RouteRegistration = Tuple[Any, str, List[str], Sequence[Any]]


def build_route_registrations(api_dependencies: Sequence[Any]) -> List[RouteRegistration]:
    from embeddr.api.v1 import actions as actions_v2
    from embeddr.api.v1 import graph as graph_v1
    from embeddr.api.v1 import artifacts as artifacts_v2
    from embeddr.api.v1 import collections as collections_v2
    from embeddr.api.v1 import config as config_v2
    from embeddr.api.v1 import config_api as lotus_config
    from embeddr.api.v1 import executions as executions_v2
    from embeddr.api.v1 import lotus as lotus_v2
    from embeddr.api.v1 import lotus_invoke_routes
    from embeddr.api.v1 import maintenance as maintenance_v2
    from embeddr.api.v1 import panels as panels_v1
    from embeddr.api.v1 import plugins as plugins_v2
    from embeddr.api.v1 import resources as resources_v2
    from embeddr.api.v1 import security as security_router
    from embeddr.api.v1 import system as system_v2
    from embeddr.api.v1 import system_public as system_public_v2
    from embeddr.api.v1 import themes as themes_v2
    from embeddr.api.v1 import workers as workers_v1
    from embeddr.api.v1 import workflows as workflows_v2

    # Canonical public API base is /api.
    # /api/v1 mounts are compatibility aliases for existing clients.
    return [
        (artifacts_v2.router, "/api/artifacts",
         ["artifacts"], api_dependencies),
        (artifacts_v2.router, "/api/v1/artifacts",
         ["artifacts"], api_dependencies),
        (graph_v1.router, "/api/graph", ["graph"], api_dependencies),
        (graph_v1.router, "/api/v1/graph", ["graph"], api_dependencies),
        (plugins_v2.router, "/api/plugins", ["plugins"], api_dependencies),
        (plugins_v2.router, "/api/v1/plugins", ["plugins"], api_dependencies),
        (system_v2.router, "/api/system", ["system"], api_dependencies),
        (system_v2.router, "/api/v1/system", ["system"], api_dependencies),
        (system_public_v2.router, "/api", ["system"], []),
        (system_public_v2.router, "/api/v1", ["system"], []),
        (collections_v2.router, "/api/collections",
         ["collections"], api_dependencies),
        (collections_v2.router, "/api/v1/collections",
         ["collections"], api_dependencies),
        (executions_v2.router, "/api/executions",
         ["executions"], api_dependencies),
        (executions_v2.router, "/api/v1/executions",
         ["executions"], api_dependencies),
        (workflows_v2.router, "/api/workflows",
         ["workflows"], api_dependencies),
        (workflows_v2.router, "/api/v1/workflows",
         ["workflows"], api_dependencies),
        (config_v2.router, "/api/config", ["config"], api_dependencies),
        (config_v2.router, "/api/v1/config", ["config"], api_dependencies),
        (maintenance_v2.router, "/api/maintenance",
         ["maintenance"], api_dependencies),
        (maintenance_v2.router, "/api/v1/maintenance",
         ["maintenance"], api_dependencies),
        (actions_v2.router, "/api/actions", ["actions"], api_dependencies),
        (actions_v2.router, "/api/v1/actions", ["actions"], api_dependencies),
        (resources_v2.router, "/api/resources",
         ["resources"], api_dependencies),
        (resources_v2.router, "/api/v1/resources",
         ["resources"], api_dependencies),
        (security_router.router, "/api/security", ["security"], []),
        (security_router.router, "/api/v1/security", ["security"], []),
        (themes_v2.router, "/api/themes", ["themes"], []),
        (themes_v2.router, "/api/v1/themes", ["themes"], []),
        (workers_v1.router, "/api/v1/workers", ["workers"], []),
        (panels_v1.router, "/api/panels", ["panels"], api_dependencies),
        (panels_v1.router, "/api/v1/panels", ["panels"], api_dependencies),
        (lotus_v2.router, "/api/lotus", ["lotus"], api_dependencies),
        (lotus_v2.router, "/api/v1/lotus", ["lotus"], api_dependencies),
        (lotus_config.router, "/api/lotus",
         ["lotus", "config"], api_dependencies),
        (lotus_config.router, "/api/v1/lotus",
         ["lotus", "config"], api_dependencies),
        (lotus_invoke_routes.router, "/api/lotus",
         ["lotus"], api_dependencies),
        (lotus_invoke_routes.router, "/api/v1/lotus",
         ["lotus"], api_dependencies),
    ]
