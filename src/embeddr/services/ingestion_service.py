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
                # 1. Pre-calculate IDs for all items to safely check existence
                uri_to_uuid = {}
                for item in items:
                    u = item.get('uri')
                    if not u:
                        continue

                    if item.get('id'):
                        try:
                            uid = UUID(str(item.get('id')))
                        except:
                            uid = uuid5(NAMESPACE_URL, u)
                    else:
                        uid = uuid5(NAMESPACE_URL, u)

                    uri_to_uuid[u] = uid

                # Also collect target URIs for relations
                relation_target_uris = set()
                for item in items:
                    for rel in item.get('relations', []):
                        if rel.get('target_uri'):
                            relation_target_uris.add(rel['target_uri'])

                # 2. Check which IDs already exist in DB
                all_ids = list(uri_to_uuid.values())
                existing_ids = set()
                if all_ids:
                    id_chunks = [all_ids[i:i + 100]
                                 for i in range(0, len(all_ids), 100)]
                    for chunk in id_chunks:
                        # Only select IDs that exist
                        found = session.exec(select(Artifact.id).where(
                            Artifact.id.in_(chunk))).all()
                        existing_ids.update(found)

                # 3. Resolve existing relation targets (by URI or just assume they might not exist yet?)
                # Actually, we need to map relation target URIs to IDs too.
                # If they are NOT in the items list, we need to fetch them or generate IDs.
                # For simplicity, let's treat relation targets same as items loop logic:
                # But we might need to lookup their IDs from DB if they exist.

                # Let's do a loose check for target URIs in DB to get their IDs
                if relation_target_uris:
                    chunks = [list(relation_target_uris)[i:i + 100]
                              for i in range(0, len(relation_target_uris), 100)]
                    for chunk in chunks:
                        res = session.exec(select(Artifact.uri, Artifact.id).where(
                            Artifact.uri.in_(chunk))).all()
                        for u, i in res:
                            if u not in uri_to_uuid:
                                uri_to_uuid[u] = i if isinstance(
                                    i, UUID) else UUID(str(i))

                to_insert_artifacts = []
                processed_uris = set()

                # 4. Process Artifacts to Insert
                for item in items:
                    uri = item.get('uri')
                    if not uri:
                        continue

                    if uri in processed_uris:
                        continue  # Dedup batch

                    uid = uri_to_uuid.get(uri)
                    if not uid:
                        # Should be impossible given step 1, but safe fallback
                        uid = uuid5(NAMESPACE_URL, uri)
                        uri_to_uuid[uri] = uid

                    # If ID not in existing_ids, we insert
                    if uid not in existing_ids:
                        art = Artifact(
                            id=uid,
                            type_name=item.get('type_name', 'artifact'),
                            base_type_name=item.get(
                                'base_type_name', 'artifact'),
                            uri=uri,
                            metadata_json=item.get('metadata_json', {}),
                            override_capabilities=item.get(
                                'override_capabilities', []),
                            owner_user_id=item.get('owner_user_id'),
                            owner_operator_id=item.get('owner_operator_id'),
                        )
                        to_insert_artifacts.append(art)
                        existing_ids.add(uid)  # Mark processed

                    processed_uris.add(uri)

                # 5. Insert Artifacts
                if to_insert_artifacts:
                    for obj in to_insert_artifacts:
                        session.add(obj)
                    session.flush()

                # 6. Process Relations
                to_insert_relations = []
                for item in items:
                    source_uri = item.get('uri')
                    if not source_uri or source_uri not in uri_to_uuid:
                        continue

                    source_id = uri_to_uuid[source_uri]

                    for rel in item.get('relations', []):
                        target_uri = rel.get('target_uri')
                        if not target_uri:
                            continue

                        # We need an ID for target.
                        target_id = uri_to_uuid.get(target_uri)
                        if not target_id:
                            # Generate if missing (Relation to external/unseen artifact)
                            target_id = uuid5(NAMESPACE_URL, target_uri)
                            # Note: If this target artifact doesn't exist, relation might fail FK?
                            # ArtifactRelation typically requires foreign key to Artifact.
                            # If we haven't inserted the target, this will fail.
                            # So we should only add relations where target is known/inserted.
                            continue

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
