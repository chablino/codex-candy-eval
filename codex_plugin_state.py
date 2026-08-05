"""Compose Codex plugin configuration without mutating shared state."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any


CANONICAL_SUPERPOWERS_ID = "superpowers@openai-api-curated"
LEGACY_SUPERPOWERS_ID = "superpowers@openai-curated"


class CodexPluginStateError(RuntimeError):
    """Raised when launcher-owned Codex plugin state is invalid."""


@dataclass(frozen=True)
class ComposedConfig:
    document: dict[str, Any] = field(repr=False)
    baseline_plugins: dict[str, bool]
    warnings: tuple[str, ...]


def deep_merge(
    base: Mapping[str, Any], overlay: Mapping[str, Any]
) -> dict[str, Any]:
    """Recursively merge mappings while replacing all other values."""
    merged = deepcopy(dict(base))
    for key, value in overlay.items():
        current = merged.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            merged[key] = deep_merge(current, value)
        else:
            merged[key] = deepcopy(value)
    return merged


def normalize_plugin_id(plugin_id: str) -> str:
    return (
        CANONICAL_SUPERPOWERS_ID
        if plugin_id == LEGACY_SUPERPOWERS_ID
        else plugin_id
    )


def _mapping_table(document: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = document.get(key, {})
    if not isinstance(value, Mapping):
        raise CodexPluginStateError(f"Codex {key} must be a table")
    return value


def _normalized_plugin_entries(
    document: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    plugins = _mapping_table(document, "plugins")
    canonical_is_explicit = CANONICAL_SUPERPOWERS_ID in plugins
    normalized: dict[str, dict[str, Any]] = {}

    for plugin_id, entry in plugins.items():
        if not isinstance(plugin_id, str) or not isinstance(entry, Mapping):
            raise CodexPluginStateError("each Codex plugin must be a table")
        enabled = entry.get("enabled", True)
        if not isinstance(enabled, bool):
            raise CodexPluginStateError("Codex plugin enabled must be boolean")
        if plugin_id == LEGACY_SUPERPOWERS_ID and canonical_is_explicit:
            continue
        normalized[normalize_plugin_id(plugin_id)] = deepcopy(dict(entry))

    return normalized


def _normalized_plugin_overrides(
    overrides: Mapping[str, bool],
) -> dict[str, bool]:
    canonical_is_explicit = CANONICAL_SUPERPOWERS_ID in overrides
    normalized: dict[str, bool] = {}
    for plugin_id, enabled in overrides.items():
        if not isinstance(plugin_id, str) or not isinstance(enabled, bool):
            raise CodexPluginStateError(
                "provider plugin overrides must map plugin IDs to booleans"
            )
        if plugin_id == LEGACY_SUPERPOWERS_ID and canonical_is_explicit:
            continue
        normalized[normalize_plugin_id(plugin_id)] = enabled
    return normalized


def plugin_flags(document: Mapping[str, Any]) -> dict[str, bool]:
    """Return normalized plugin enablement, defaulting omitted flags to true."""
    return {
        plugin_id: entry.get("enabled", True)
        for plugin_id, entry in _normalized_plugin_entries(document).items()
    }


def compose_effective_config(
    provider: Mapping[str, Any],
    common: Mapping[str, Any] | None,
    common_enabled: bool,
    launcher_marketplaces: Mapping[str, Any],
    inventory: set[str] | frozenset[str],
    provider_plugins: Mapping[str, bool],
) -> ComposedConfig:
    """Build one provider's complete config and its plugin baseline."""
    document = deepcopy(dict(provider))
    if common_enabled and common is not None:
        document = deep_merge(document, common)

    plugins = _normalized_plugin_entries(document)
    baseline_plugins = {
        plugin_id: entry.get("enabled", True)
        for plugin_id, entry in plugins.items()
    }

    marketplaces = _mapping_table(document, "marketplaces")
    if not isinstance(launcher_marketplaces, Mapping):
        raise CodexPluginStateError("launcher marketplaces must be a table")
    if marketplaces or launcher_marketplaces:
        document["marketplaces"] = deep_merge(
            marketplaces, launcher_marketplaces
        )

    normalized_inventory: set[str] = set()
    for plugin_id in inventory:
        if not isinstance(plugin_id, str):
            raise CodexPluginStateError("plugin inventory IDs must be text")
        normalized_inventory.add(normalize_plugin_id(plugin_id))

    for plugin_id in sorted(normalized_inventory):
        plugins.setdefault(plugin_id, {"enabled": False})

    for plugin_id, enabled in _normalized_plugin_overrides(
        provider_plugins
    ).items():
        entry = plugins.setdefault(plugin_id, {})
        entry["enabled"] = enabled

    if plugins or "plugins" in document:
        document["plugins"] = plugins

    enabled_missing = sorted(
        plugin_id
        for plugin_id, enabled in plugin_flags(document).items()
        if enabled and plugin_id not in normalized_inventory
    )
    warnings = tuple(
        f"configured plugin {plugin_id!r} is enabled but is not installed"
        for plugin_id in enabled_missing
    )
    return ComposedConfig(document, baseline_plugins, warnings)
