import asyncio
import logging
from typing import List, Dict, Any, Optional
from uuid import UUID
from sqlmodel import Session, select
from embeddr_core.models.artifact import Artifact
from embeddr.core.plugin_loader import _EVENT_BUS
from embeddr_core.plugin_interface import EmbeddrEvent

logger = logging.getLogger(__name__)


class IngestionService:
    def __init__(self, batch_size: int = 50, flush_interval: float = 1.0, queue_size: int = 1000):
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=queue_size)
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        self.is_running = False
        self.worker_task: Optional[asyncio.Task] = None
        self._engine = None

    @property
    def engine(self):
        if not self._engine:
            from embeddr.core.config import settings
            from sqlmodel import create_engine
            from sqlalchemy.pool import QueuePool

            connect_args = {
                "check_same_thread": False} if "sqlite" in settings.DATABASE_URL else {}

            # Dedicated pool with limited connections
            self._engine = create_engine(
                settings.DATABASE_URL,
                connect_args=connect_args,
                poolclass=QueuePool,
                pool_size=2,
                max_overflow=0
            )
        return self._engine

    async def start(self):
        if self.is_running:
            return

        # Ensure engine is ready
        _ = self.engine

        self.is_running = True
        self.worker_task = asyncio.create_task(self._worker())
        logger.info(
            f"IngestionService started (Batch: {self.batch_size}, Queue: {self.queue.maxsize})")

    async def stop(self):
        self.is_running = False
        if self.worker_task:
            self.worker_task.cancel()
            try:
                await self.worker_task
            except asyncio.CancelledError:
                pass

    async def ingest(self, item_data: Dict[str, Any]):
        """
        Submit an item for ingestion.
        Blocks if the ingestion queue is full (Backpressure).
        """
        await self.queue.put(item_data)

    async def _worker(self):
        buffer = []
        last_flush = asyncio.get_running_loop().time()

        while self.is_running:
            try:
                try:
                    # Wait for item with short timeout
                    item = await asyncio.wait_for(self.queue.get(), timeout=0.1)
                    buffer.append(item)
                except asyncio.TimeoutError:
                    pass
                except asyncio.CancelledError:
                    break

                now = asyncio.get_running_loop().time()
                is_batch_full = len(buffer) >= self.batch_size
                is_time_up = (
                    now - last_flush) >= self.flush_interval and len(buffer) > 0

                if is_batch_full or is_time_up:
                    await self._flush(buffer)
                    buffer = []
                    last_flush = now

            except Exception as e:
                logger.error(f"Error in ingestion worker: {e}", exc_info=True)
                await asyncio.sleep(1)

        # Flush remaining on stop
        if buffer:
            await self._flush(buffer)

    async def _flush(self, items: List[Dict]):
        if not items:
            return
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._flush_sync, items)

    def _flush_sync(self, items: List[Dict]):
        from uuid import uuid5, NAMESPACE_URL, UUID
        from embeddr_core.models.artifact_relation import ArtifactRelation

        try:
            with Session(self.engine) as session:
                # 1. Identify all URIs
                item_uris = {x.get('uri') for x in items if x.get('uri')}
                relation_uris = set()

                for item in items:
                    for rel in item.get('relations', []):
                        if rel.get('target_uri'):
                            relation_uris.add(rel['target_uri'])

                all_uris = item_uris | relation_uris

                # 2. Resolve existing UUIDs
                # Map URI -> UUID Object
                uri_to_id = {}
                if all_uris:
                    # Batch fetch existing
                    chunks = [list(all_uris)[i:i + 100]
                              for i in range(0, len(all_uris), 100)]
                    for chunk in chunks:
                        statement = select(Artifact.uri, Artifact.id).where(
                            Artifact.uri.in_(chunk))
                        results = session.exec(statement).all()
                        for u, i in results:
                            # Verify i is UUID object
                            if isinstance(i, str):
                                uri_to_id[u] = UUID(i)
                            else:
                                uri_to_id[u] = i

                to_insert_artifacts = []
                # 3. Process Artifacts
                for item in items:
                    uri = item.get('uri')
                    if not uri:
                        continue

                    # Deterministic ID generation if not exists
                    if uri not in uri_to_id:
                        # Check if scraper sent an ID
                        provided_id = item.get('id')
                        new_id_obj = None

                        if provided_id:
                            try:
                                new_id_obj = UUID(str(provided_id))
                            except ValueError:
                                logger.warning(
                                    f"Invalid UUID provided for {uri}: {provided_id}. Genererating new.")

                        if not new_id_obj:
                            new_id_obj = uuid5(NAMESPACE_URL, uri)

                        uri_to_id[uri] = new_id_obj

                        # Check if we already staged this artifact in this batch
                        if any(art.uri == uri for art in to_insert_artifacts):
                            continue  # Already added in this batch

                        art = Artifact(
                            id=new_id_obj,
                            type_name=item.get('type_name', 'artifact'),
                            base_type_name=item.get(
                                'base_type_name', 'artifact'),
                            uri=uri,
                            metadata_json=item.get('metadata_json', {}),
                            override_capabilities=item.get(
                                'override_capabilities', [])
                        )
                        # DEBUG: Verify ID type
                        if not isinstance(art.id, UUID):
                            logger.warning(
                                f"Artifact ID became {type(art.id)}: {art.id}. Forcing UUID.")
                            try:
                                art.id = UUID(str(art.id))
                            except Exception as e:
                                logger.error(f"Failed to force UUID: {e}")

                        to_insert_artifacts.append(art)

                # 4. Insert Artifacts
                if to_insert_artifacts:
                    for obj in to_insert_artifacts:
                        session.add(obj)
                    session.flush()

                # 5. Process Relations
                to_insert_relations = []
                for item in items:
                    source_uri = item.get('uri')
                    if not source_uri or source_uri not in uri_to_id:
                        continue

                    source_id = uri_to_id[source_uri]

                    for rel in item.get('relations', []):
                        target_uri = rel.get('target_uri')
                        if not target_uri:
                            continue

                        if target_uri in uri_to_id:
                            target_id = uri_to_id[target_uri]

                            rel_type = rel.get('relation_type', 'contains')

                            exists = session.exec(select(ArtifactRelation).where(
                                ArtifactRelation.source_id == source_id,
                                ArtifactRelation.target_id == target_id,
                                ArtifactRelation.relation_type == rel_type
                            )).first()

                            if not exists:
                                new_rel = ArtifactRelation(
                                    source_id=source_id,
                                    target_id=target_id,
                                    relation_type=rel_type,
                                    metadata_json=rel.get('metadata_json', {})
                                )
                                session.add(new_rel)

                session.commit()

                # Emit events
                if to_insert_artifacts:
                    for art in to_insert_artifacts:
                        payload = {
                            "id": str(art.id),
                            "uri": art.uri,
                            "type": art.type_name,
                            "metadata": art.metadata_json
                        }
                        _EVENT_BUS.publish(EmbeddrEvent(
                            event_type="artifact.created",
                            source="ingestion_service",
                            payload=payload
                        ))

                logger.info(
                    f"Ingested {len(to_insert_artifacts)} artifacts. (Batch: {len(items)})")
        except Exception as e:
            logger.error(f"Failed to flush batch on DB: {e}", exc_info=True)


# Global instance
ingestion_service = IngestionService()
