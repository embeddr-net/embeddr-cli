from typing import List, Optional
from uuid import UUID
import logging
from sqlmodel import Session, select
from embeddr_core.models.config import AutoAnalysisConfig
from embeddr_core.models.collection import CollectionItem

logger = logging.getLogger(__name__)


class AnalysisDispatcher:
    def __init__(self, session: Session):
        self.session = session

    def should_run_analysis(self, artifact_id: UUID, plugin_name: str, default_enabled: bool = True) -> bool:
        """
        Determines if an analysis plugin should run for a given artifact.
        """
        # ... logic ...
        return self._should_run_recursive(artifact_id, plugin_name, default_enabled)

    def _should_run_recursive(self, artifact_id: UUID, plugin_name: str, default_enabled: bool = True) -> bool:
        """
        Determines if an analysis plugin should run for a given artifact.

        Priority:
        1. Collection-specific configs (if artifact is in collections).
           If ANY collection disables it -> Disabled.
           If ALL collections with config enable it -> Enabled.
        2. Global config.
        3. Default (plugin default).
        """

        # 1. Find artifact collections
        # We need to know which collections this artifact is part of.
        # This usually happens right after creation/upload, so it might not be in a collection yet,
        # unless it was uploaded *into* a collection.
        if artifact_id:
            collection_links = self.session.exec(
                select(CollectionItem).where(
                    CollectionItem.artifact_id == artifact_id)
            ).all()

            collection_ids = [c.collection_id for c in collection_links]

            # 2. Check Collection Configs
            if collection_ids:
                # Get configs for these collections AND for this plugin
                col_configs = self.session.exec(
                    select(AutoAnalysisConfig).where(
                        AutoAnalysisConfig.scope == "collection",
                        AutoAnalysisConfig.scope_id.in_(collection_ids),
                        AutoAnalysisConfig.plugin_name == plugin_name
                    )
                ).all()

                if col_configs:
                    # If we have any collection-specific configs

                    # Logic: If any collection explicitly DISABLES it, we respect that specificity.
                    for cfg in col_configs:
                        if not cfg.enabled:
                            logger.debug(
                                f"Analysis {plugin_name} disabled by collection {cfg.scope_id}")
                            return False

                    # If we found configs and none disabled it (meaning they all enabled it), we return True.
                    # Assuming the existence of a config implies intent.
                    return True

        # 3. Check Global Config
        global_config = self.session.exec(
            select(AutoAnalysisConfig).where(
                AutoAnalysisConfig.scope == "global",
                AutoAnalysisConfig.plugin_name == plugin_name
            )
        ).first()

        if global_config:
            return global_config.enabled

        return default_enabled

    def get_priority(self, plugin_name: str, default_priority: int = 0) -> int:
        """
        Get priority for a plugin/capability from global config.
        """
        global_config = self.session.exec(
            select(AutoAnalysisConfig).where(
                AutoAnalysisConfig.scope == "global",
                AutoAnalysisConfig.plugin_name == plugin_name
            )
        ).first()

        if global_config:
            return global_config.priority
        return default_priority
