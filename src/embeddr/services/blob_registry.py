from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Dict, Optional, Protocol, Any

from fastapi.responses import Response

from embeddr.services.blob_refs import BlobRef


class BlobProvider(Protocol):
    name: str

    def put_upload(
        self,
        *,
        artifact_id: str,
        filename: str,
        content_type: Optional[str],
        upload_file: Any,
        confirmed: bool,
        key_hint: Optional[str] = None,
    ) -> BlobRef:
        ...

    def stat(self, blob_ref: BlobRef) -> Optional[Dict[str, Any]]:
        ...

    def delete(self, blob_ref: BlobRef) -> bool:
        ...


class BlobResolver(Protocol):
    name: str

    def resolve(self, blob_ref: BlobRef, purpose: str) -> Dict[str, Any]:
        ...

    def resolve_response(self, blob_ref: BlobRef, purpose: str) -> Optional[Response]:
        ...


@dataclass
class RegistryState:
    providers: Dict[str, BlobProvider]
    resolvers: Dict[str, BlobResolver]
    provider_resolvers: Dict[str, str]
    default_provider: Optional[str] = None
    default_resolver: Optional[str] = None


_registry = RegistryState(providers={}, resolvers={}, provider_resolvers={})


def _hydrate_defaults_from_config() -> None:
    if _registry.default_provider and _registry.default_resolver:
        return
    try:
        from sqlmodel import Session
        from embeddr.db.session import get_engine
        from embeddr_core.services.config_service import resolve_plugin_config

        with Session(get_engine()) as session:
            cfg = resolve_plugin_config(
                session=session,
                plugin_name="embeddr-core",
                config_id="embeddr-core.blob.defaults",
            ) or {}

        provider = cfg.get("default_provider")
        resolver = cfg.get("default_resolver")

        if provider and provider in _registry.providers:
            _registry.default_provider = provider
        if resolver and resolver in _registry.resolvers:
            _registry.default_resolver = resolver
    except Exception:
        return


def register_provider(provider: BlobProvider) -> None:
    _registry.providers[provider.name] = provider
    if not _registry.default_provider:
        _registry.default_provider = provider.name


def register_resolver(
    resolver_name: str,
    resolver: BlobResolver,
    providers: Optional[list[str]] = None,
) -> None:
    _registry.resolvers[resolver_name] = resolver
    if providers:
        for provider_name in providers:
            _registry.provider_resolvers[provider_name] = resolver_name
    if not _registry.default_resolver:
        _registry.default_resolver = resolver_name


def set_default_provider(name: str) -> None:
    _registry.default_provider = name


def set_default_resolver(name: str) -> None:
    _registry.default_resolver = name


def get_default_resolver_name() -> Optional[str]:
    _hydrate_defaults_from_config()
    return _registry.default_resolver


def get_default_provider_name() -> Optional[str]:
    _hydrate_defaults_from_config()
    if _registry.default_provider:
        return _registry.default_provider
    env_default = os.environ.get("EMBEDDR_BLOB_PROVIDER")
    if env_default and env_default in _registry.providers:
        _registry.default_provider = env_default
    return _registry.default_provider


def get_provider(name: Optional[str]) -> Optional[BlobProvider]:
    if not name:
        return None
    return _registry.providers.get(name)


def get_resolver(resolver_name: Optional[str]) -> Optional[BlobResolver]:
    if not resolver_name:
        return None
    return _registry.resolvers.get(resolver_name)


def get_resolver_for_provider(provider_name: Optional[str]) -> Optional[BlobResolver]:
    if not provider_name:
        return None
    resolver_name = _registry.provider_resolvers.get(provider_name)
    if resolver_name:
        return _registry.resolvers.get(resolver_name)
    if _registry.default_resolver:
        return _registry.resolvers.get(_registry.default_resolver)
    return None


def list_providers() -> Dict[str, BlobProvider]:
    return dict(_registry.providers)


def list_resolvers() -> Dict[str, BlobResolver]:
    return dict(_registry.resolvers)


def list_provider_resolvers() -> Dict[str, str]:
    return dict(_registry.provider_resolvers)


def resolve_blob(
    blob_ref: BlobRef,
    purpose: str,
) -> Dict[str, Any]:
    resolver = get_resolver_for_provider(blob_ref.provider)
    if not resolver:
        return {
            "ok": False,
            "error": f"resolver_not_found:{blob_ref.provider}",
        }
    return resolver.resolve(blob_ref, purpose)


def resolve_response(blob_ref: BlobRef, purpose: str) -> Optional[Response]:
    resolver = get_resolver_for_provider(blob_ref.provider)
    if not resolver:
        return None
    return resolver.resolve_response(blob_ref, purpose)
