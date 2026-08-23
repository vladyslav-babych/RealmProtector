"""SQLite adapter for the objective scheduler's cross-guild scan."""

from __future__ import annotations

from src.realm_protector.infrastructure import document_store

_OBJECTIVES_NAMESPACE = "objectives"


def load_objectives() -> dict:
    return document_store.load_mapping(_OBJECTIVES_NAMESPACE)
