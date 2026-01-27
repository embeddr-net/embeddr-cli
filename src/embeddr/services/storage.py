from __future__ import annotations

from typing import Optional, Dict, Any

from fastapi import UploadFile
from fastapi.responses import Response
from sqlmodel import Session

from embeddr_core.models.artifact import Artifact
from embeddr_core.models.artifact_blob import ArtifactBlob
from embeddr.services.blob_refs import BlobRef
from embeddr.services.blob_registry import (
    get_default_provider_name,
    get_provider,
    resolve_response,
    resolve_blob,
)


class StorageService:
    def store_upload(
        self,
        session: Session,
        artifact: Artifact,
        upload_file: UploadFile,
        filename: str,
        content_type: Optional[str],
        backend_hint: Optional[str] = None,
        confirmed: bool = False,
    ) -> BlobRef:
        provider_name = (
            backend_hint or get_default_provider_name() or "local").lower()
        provider = get_provider(provider_name)
        if not provider:
            raise RuntimeError(f"blob_provider_not_registered:{provider_name}")

        return provider.put_upload(
            artifact_id=str(artifact.id),
            filename=filename,
            content_type=content_type,
            upload_file=upload_file,
            confirmed=confirmed,
        )

    def get_content_response(
        self,
        session: Session,
        blob: ArtifactBlob,
        artifact: Optional[Artifact] = None,
        purpose: str = "original",
    ) -> Optional[Response]:
        blob_ref = BlobRef(
            provider=blob.storage_backend,
            bucket=None,
            key=None,
            path=blob.path,
            etag=blob.sha256,
            content_type=blob.content_type,
            size=blob.size,
            artifact_id=str(blob.artifact_id),
        )
        return resolve_response(blob_ref, purpose)

    def resolve_blob(self, blob: ArtifactBlob, purpose: str = "original") -> Dict[str, Any]:
        blob_ref = BlobRef(
            provider=blob.storage_backend,
            bucket=None,
            key=None,
            path=blob.path,
            etag=blob.sha256,
            content_type=blob.content_type,
            size=blob.size,
            artifact_id=str(blob.artifact_id),
        )
        return resolve_blob(blob_ref, purpose)


storage_service = StorageService()
