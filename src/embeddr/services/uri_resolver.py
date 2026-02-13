"""Centralised URI resolution for artifact storage paths.

Every place that needs to turn an ``Artifact.uri`` (or preview/ingest URI)
into a concrete filesystem path MUST use :func:`resolve_uri` instead of
doing ad-hoc ``uri.replace("file://", "")`` stripping.

Supported schemes
-----------------
* ``workspace://…``  – path relative to the *data directory* of the current
  workspace (``EMBEDDR_DATA_DIR``).  Portable across machines / workspace
  moves.
* ``file://…``       – absolute filesystem path (legacy, still supported
  for reading; new writes should prefer ``workspace://``).
* Raw absolute paths  (``/home/…``) – treated the same as ``file://``.
* ``s3://…``, ``http(s)://…``, ``internal://…``, ``virtual://…``,
  ``stash://…``, ``embeddr:///…`` – returned unchanged (handled elsewhere).

Writing helpers
---------------
:func:`make_workspace_uri` converts an absolute path into a ``workspace://``
URI if it falls inside the current data directory.

Migration
---------
:func:`relink_absolute_uris` rewrites stale absolute paths in the DB when a
workspace has been moved.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger("embeddr.services.uri_resolver")

# Schemes that this module does NOT resolve (they're handled elsewhere).
_PASSTHROUGH_SCHEMES = ("s3://", "http://", "https://", "internal://",
                        "virtual://", "stash://", "embeddr:///")


# ---------------------------------------------------------------------------
# Read: resolve a stored URI to an absolute path
# ---------------------------------------------------------------------------

def resolve_uri(uri: Optional[str], *, data_dir: Optional[str] = None) -> Optional[str]:
    """Return an absolute filesystem path for *uri*, or *None* for
    non-filesystem schemes (s3, http, internal, …).

    Parameters
    ----------
    uri:
        The raw URI string from ``Artifact.uri``, ``ArtifactPreview.uri``, etc.
    data_dir:
        Override for ``EMBEDDR_DATA_DIR``.  When *None* the env-var is read.
    """
    if not uri:
        return None

    # Pass through non-filesystem schemes unchanged
    for scheme in _PASSTHROUGH_SCHEMES:
        if uri.startswith(scheme):
            return None  # caller should handle via blob_registry / redirect

    # workspace:// → relative to data_dir
    if uri.startswith("workspace://"):
        rel = uri[len("workspace://"):]
        root = _get_data_dir(data_dir)
        if root is None:
            logger.warning(
                "Cannot resolve workspace:// URI – EMBEDDR_DATA_DIR not set: %s", uri)
            return None
        return str(Path(root) / rel)

    # file:// → strip scheme
    if uri.startswith("file://"):
        return uri[7:]

    # Raw absolute path
    if os.path.isabs(uri):
        return uri

    # Relative path (rare) — resolve against data_dir
    root = _get_data_dir(data_dir)
    if root:
        candidate = str(Path(root) / uri)
        if os.path.exists(candidate):
            return candidate

    # Fallback: return as-is and let the caller deal with it
    return uri


def is_local_uri(uri: Optional[str]) -> bool:
    """Return *True* if this URI points to a local filesystem path."""
    if not uri:
        return False
    for scheme in _PASSTHROUGH_SCHEMES:
        if uri.startswith(scheme):
            return False
    return True


# ---------------------------------------------------------------------------
# Write: create a workspace:// URI from an absolute path
# ---------------------------------------------------------------------------

def make_workspace_uri(absolute_path: str, *, data_dir: Optional[str] = None) -> str:
    """Convert an absolute path to a ``workspace://`` URI if it falls inside
    the workspace data directory.  Otherwise return the path unchanged.

    Parameters
    ----------
    absolute_path:
        An absolute filesystem path (e.g. from ``BlobRef.path``).
    data_dir:
        Override for ``EMBEDDR_DATA_DIR``.
    """
    root = _get_data_dir(data_dir)
    if not root:
        return absolute_path

    root_str = str(Path(root).resolve())
    abs_str = str(Path(absolute_path).resolve())

    if abs_str.startswith(root_str + os.sep) or abs_str.startswith(root_str + "/"):
        rel = abs_str[len(root_str):].lstrip(os.sep).lstrip("/")
        return f"workspace://{rel}"

    return absolute_path


# ---------------------------------------------------------------------------
# Migration: relink stale absolute URIs
# ---------------------------------------------------------------------------

def relink_absolute_uris(
    *,
    old_prefix: str,
    data_dir: Optional[str] = None,
    dry_run: bool = True,
) -> dict:
    """Rewrite absolute URIs that start with *old_prefix* to ``workspace://``
    URIs relative to the current data directory.

    Operates on the ``artifact``, ``artifactpreview``, and ``artifactingest``
    tables.

    Returns a summary dict with counts of affected rows.
    """
    root = _get_data_dir(data_dir)
    if not root:
        raise RuntimeError(
            "EMBEDDR_DATA_DIR not set — cannot compute workspace-relative paths")

    root_str = str(Path(root).resolve())
    # Normalise old_prefix: strip trailing slash, resolve /.embeddr if needed
    old_data_dir = old_prefix.rstrip("/")

    from sqlmodel import Session, text
    from embeddr.db.session import get_engine

    results = {"artifact": 0, "artifactpreview": 0, "artifactingest": 0}

    # Build search prefixes: both raw absolute and file:// prefixed
    prefixes = [old_data_dir, f"file://{old_data_dir}"]

    def _strip_prefix(uri: str) -> str:
        """Strip whichever prefix matches and return the relative path."""
        if uri.startswith(f"file://{old_data_dir}"):
            return uri[len(f"file://{old_data_dir}"):].lstrip("/")
        return uri[len(old_data_dir):].lstrip("/")

    with Session(get_engine()) as session:
        # --- artifact.uri ---
        rows = session.exec(
            text("SELECT id, uri FROM artifact WHERE uri LIKE :p1 OR uri LIKE :p2")
            .bindparams(p1=f"{old_data_dir}%", p2=f"file://{old_data_dir}%")
        ).all()
        for row_id, uri in rows:
            rel = _strip_prefix(uri)
            new_uri = f"workspace://{rel}"
            if not dry_run:
                session.exec(
                    text("UPDATE artifact SET uri = :new_uri WHERE id = :id")
                    .bindparams(new_uri=new_uri, id=row_id)
                )
            results["artifact"] += 1
            logger.info("  artifact %s: %s → %s",
                        row_id, uri[:60], new_uri[:60])

        # --- artifactpreview.uri ---
        rows = session.exec(
            text("SELECT id, uri FROM artifactpreview WHERE uri LIKE :p1 OR uri LIKE :p2")
            .bindparams(p1=f"{old_data_dir}%", p2=f"file://{old_data_dir}%")
        ).all()
        for row_id, uri in rows:
            rel = _strip_prefix(uri)
            new_uri = f"workspace://{rel}"
            if not dry_run:
                session.exec(
                    text("UPDATE artifactpreview SET uri = :new_uri WHERE id = :id")
                    .bindparams(new_uri=new_uri, id=row_id)
                )
            results["artifactpreview"] += 1
            logger.info("  preview %s: %s → %s",
                        row_id, uri[:60], new_uri[:60])

        # --- artifactingest.storage_path ---
        rows = session.exec(
            text("SELECT id, storage_path FROM artifactingest WHERE storage_path LIKE :p1 OR storage_path LIKE :p2")
            .bindparams(p1=f"{old_data_dir}%", p2=f"file://{old_data_dir}%")
        ).all()
        for row_id, uri in rows:
            rel = _strip_prefix(uri)
            new_uri = f"workspace://{rel}"
            if not dry_run:
                session.exec(
                    text(
                        "UPDATE artifactingest SET storage_path = :new_uri WHERE id = :id")
                    .bindparams(new_uri=new_uri, id=row_id)
                )
            results["artifactingest"] += 1
            logger.info("  ingest %s: %s → %s", row_id, uri[:60], new_uri[:60])

        if not dry_run:
            session.commit()

    return results


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_data_dir(override: Optional[str] = None) -> Optional[str]:
    if override:
        return override
    return os.environ.get("EMBEDDR_DATA_DIR")
