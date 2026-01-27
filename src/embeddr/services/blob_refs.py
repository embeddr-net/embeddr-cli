from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Dict, Any
from urllib.parse import urlparse

from embeddr_core.models.artifact_blob import ArtifactBlob


@dataclass
class BlobRef:
    provider: str
    bucket: Optional[str]
    key: Optional[str]
    path: Optional[str]
    etag: Optional[str]
    content_type: Optional[str]
    size: Optional[int]
    artifact_id: Optional[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provider": self.provider,
            "bucket": self.bucket,
            "key": self.key,
            "path": self.path,
            "etag": self.etag,
            "contentType": self.content_type,
            "size": self.size,
            "artifact_id": self.artifact_id,
        }


def _parse_s3_path(storage_path: str) -> tuple[Optional[str], Optional[str]]:
    if not storage_path:
        return None, None
    if storage_path.startswith("s3://"):
        parsed = urlparse(storage_path)
        bucket = parsed.netloc
        key = parsed.path.lstrip("/") if parsed.path else None
        return bucket, key
    return None, None


def blob_ref_from_blob(
    blob: ArtifactBlob,
    artifact_id: Optional[str] = None,
) -> BlobRef:
    bucket, key = _parse_s3_path(blob.path)
    return BlobRef(
        provider=blob.storage_backend,
        bucket=bucket,
        key=key,
        path=blob.path,
        etag=blob.sha256,
        content_type=blob.content_type,
        size=blob.size,
        artifact_id=artifact_id,
    )


def blob_ref_storage_path(blob_ref: BlobRef) -> Optional[str]:
    if blob_ref.path:
        return blob_ref.path
    if blob_ref.provider == "s3" and blob_ref.bucket and blob_ref.key:
        return f"s3://{blob_ref.bucket}/{blob_ref.key}"
    return None
