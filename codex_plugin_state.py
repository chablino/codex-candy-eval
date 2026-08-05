"""Compose Codex plugin configuration without mutating shared state."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
import tomllib
from collections.abc import Mapping
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomli_w


CANONICAL_SUPERPOWERS_ID = "superpowers@openai-api-curated"
LEGACY_SUPERPOWERS_ID = "superpowers@openai-curated"


class CodexPluginStateError(RuntimeError):
    """Raised when launcher-owned Codex plugin state is invalid."""


@dataclass(frozen=True)
class ComposedConfig:
    document: dict[str, Any] = field(repr=False)
    baseline_plugins: dict[str, bool]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class RuntimeSnapshot:
    plugins: dict[str, bool]
    marketplaces: dict[str, Any] = field(repr=False)
    inventory: frozenset[str]


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


def _safe_path_segment(segment: str) -> bool:
    return (
        bool(segment)
        and segment not in (".", "..")
        and "@" not in segment
        and "/" not in segment
        and "\0" not in segment
    )


def _directories(path: Path) -> tuple[Path, ...]:
    try:
        return tuple(path.iterdir())
    except OSError:
        return ()


def scan_plugin_inventory(plugins_root: Path) -> frozenset[str]:
    """Discover installed plugins from validated cache manifests."""
    cache = plugins_root / "cache"
    if not cache.is_dir() or cache.is_symlink():
        return frozenset()

    installed: set[str] = set()
    for marketplace in _directories(cache):
        if (
            not marketplace.is_dir()
            or marketplace.is_symlink()
            or not _safe_path_segment(marketplace.name)
        ):
            continue
        for plugin in _directories(marketplace):
            if (
                not plugin.is_dir()
                or plugin.is_symlink()
                or not _safe_path_segment(plugin.name)
            ):
                continue
            if (
                marketplace.name == "openai-curated"
                and plugin.name == "superpowers"
            ):
                continue
            for version in _directories(plugin):
                if (
                    not version.is_dir()
                    or version.is_symlink()
                    or not _safe_path_segment(version.name)
                ):
                    continue
                manifest_path = version / ".codex-plugin" / "plugin.json"
                try:
                    if manifest_path.is_symlink() or not manifest_path.is_file():
                        continue
                    manifest = json.loads(
                        manifest_path.read_text(encoding="utf-8"),
                        parse_constant=_reject_json_constant,
                    )
                except (OSError, json.JSONDecodeError, RecursionError, ValueError):
                    continue
                if (
                    isinstance(manifest, dict)
                    and manifest.get("name") == plugin.name
                ):
                    installed.add(
                        normalize_plugin_id(
                            f"{plugin.name}@{marketplace.name}"
                        )
                    )
                    break
    return frozenset(installed)


def _ensure_private_directory(path: Path) -> None:
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.is_symlink() or not path.is_dir():
            raise OSError("state path is not a directory")
        path.chmod(0o700)
    except OSError:
        raise CodexPluginStateError("failed to prepare Codex plugin state") from None


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write_text(path: Path, content: str) -> None:
    descriptor = -1
    temporary_path: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary_path = Path(temporary_name)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as output:
            descriptor = -1
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
        path.chmod(0o600)
        _fsync_directory(path.parent)
    except OSError:
        raise CodexPluginStateError("failed to write Codex plugin state") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except OSError:
                pass


def _remove_file(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
        _fsync_directory(path.parent)
    except OSError:
        raise CodexPluginStateError("failed to remove Codex plugin state") from None


def _reject_json_constant(_constant: str) -> None:
    raise ValueError("non-standard JSON constant")


def _normalized_inventory(inventory: frozenset[str]) -> frozenset[str]:
    normalized: set[str] = set()
    for plugin_id in inventory:
        if not isinstance(plugin_id, str):
            raise CodexPluginStateError("plugin inventory IDs must be text")
        normalized.add(normalize_plugin_id(plugin_id))
    return frozenset(normalized)


def _marketplace_mapping(
    marketplaces: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(marketplaces, Mapping):
        raise CodexPluginStateError("Codex marketplaces must be a table")
    if any(not isinstance(key, str) for key in marketplaces):
        raise CodexPluginStateError("Codex marketplace names must be text")
    return deepcopy(dict(marketplaces))


class PluginStateStore:
    """Persist launcher-owned plugin switches under a shared Codex home."""

    _VERSION = 1

    def __init__(self, shared_home: Path) -> None:
        self.shared_home = Path(shared_home)
        self.state_root = self.shared_home / ".cc-switch-tui"
        self.provider_directory = self.state_root / "provider-plugins"
        self.lock_directory = self.state_root / "locks"
        self.lock_path = self.lock_directory / "state.lock"
        self.marketplaces_path = self.state_root / "marketplaces.toml"

    @staticmethod
    def _provider_digest(provider_id: str) -> str:
        if not isinstance(provider_id, str):
            raise CodexPluginStateError("provider ID must be text")
        return hashlib.sha256(provider_id.encode()).hexdigest()

    def _sidecar_path(self, provider_id: str) -> Path:
        return self.provider_directory / f"{self._provider_digest(provider_id)}.json"

    def _ensure_layout(self) -> None:
        _ensure_private_directory(self.state_root)
        _ensure_private_directory(self.provider_directory)
        _ensure_private_directory(self.lock_directory)

    @contextmanager
    def _lock(self):
        self._ensure_layout()
        descriptor = -1
        try:
            descriptor = os.open(
                self.lock_path,
                os.O_RDWR | os.O_CREAT,
                0o600,
            )
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        except CodexPluginStateError:
            raise
        except OSError:
            raise CodexPluginStateError("failed to lock Codex plugin state") from None
        finally:
            if descriptor >= 0:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)

    def _read_sidecar(
        self, path: Path, expected_provider_id: str | None = None
    ) -> tuple[str, dict[str, bool]]:
        try:
            if path.is_symlink() or not path.is_file():
                raise OSError("sidecar is not a regular file")
            payload = json.loads(
                path.read_text(encoding="utf-8"),
                parse_constant=_reject_json_constant,
            )
        except (OSError, json.JSONDecodeError, RecursionError, ValueError):
            raise CodexPluginStateError("invalid Codex provider plugin state") from None

        if not isinstance(payload, dict) or set(payload) != {
            "version",
            "provider_id",
            "plugins",
        }:
            raise CodexPluginStateError("invalid Codex provider plugin state")
        if type(payload["version"]) is not int or payload["version"] != self._VERSION:
            raise CodexPluginStateError("unknown Codex provider plugin state version")
        provider_id = payload["provider_id"]
        if not isinstance(provider_id, str):
            raise CodexPluginStateError("invalid Codex provider plugin state")
        if expected_provider_id is not None and provider_id != expected_provider_id:
            raise CodexPluginStateError("Codex provider plugin state ID mismatch")
        if path.name != f"{self._provider_digest(provider_id)}.json":
            raise CodexPluginStateError("Codex provider plugin state ID mismatch")
        plugins = payload["plugins"]
        if not isinstance(plugins, Mapping):
            raise CodexPluginStateError("invalid Codex provider plugin state")
        return provider_id, _normalized_plugin_overrides(plugins)

    def _write_sidecar(
        self, path: Path, provider_id: str, plugins: Mapping[str, bool]
    ) -> None:
        document = {
            "version": self._VERSION,
            "provider_id": provider_id,
            "plugins": dict(sorted(plugins.items())),
        }
        _atomic_write_text(
            path,
            json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        )

    def reset_provider(self, provider_id: str) -> None:
        path = self._sidecar_path(provider_id)
        with self._lock():
            if path.exists() or path.is_symlink():
                _remove_file(path)

    def load_provider_plugins(self, provider_id: str) -> dict[str, bool]:
        path = self._sidecar_path(provider_id)
        if not path.exists() and not path.is_symlink():
            return {}
        _, plugins = self._read_sidecar(path, provider_id)
        return plugins

    def _read_marketplaces(self) -> dict[str, Any] | None:
        path = self.marketplaces_path
        if not path.exists() and not path.is_symlink():
            return None
        try:
            if path.is_symlink() or not path.is_file():
                raise OSError("marketplace state is not a regular file")
            document = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError, RecursionError):
            raise CodexPluginStateError("invalid Codex marketplace state") from None
        if set(document) != {"marketplaces"}:
            raise CodexPluginStateError("invalid Codex marketplace state")
        return _marketplace_mapping(document["marketplaces"])

    def _write_marketplaces(self, marketplaces: Mapping[str, Any]) -> None:
        try:
            rendered = tomli_w.dumps(
                {"marketplaces": _marketplace_mapping(marketplaces)}
            )
        except (TypeError, ValueError):
            raise CodexPluginStateError("invalid Codex marketplace state") from None
        _atomic_write_text(self.marketplaces_path, rendered)

    def load_or_initialize_marketplaces(
        self,
        shared_marketplaces: Mapping[str, Any],
        provider_marketplaces: Mapping[str, Any],
    ) -> dict[str, Any]:
        shared = _marketplace_mapping(shared_marketplaces)
        provider = _marketplace_mapping(provider_marketplaces)
        with self._lock():
            stored = self._read_marketplaces()
            if stored is None:
                stored = deep_merge(shared, provider)
                self._write_marketplaces(stored)
            return deep_merge(provider, stored)

    def apply_runtime_changes(
        self,
        provider_id: str,
        baseline_plugins: Mapping[str, bool],
        initial: RuntimeSnapshot,
        final: RuntimeSnapshot,
    ) -> None:
        baseline = _normalized_plugin_overrides(baseline_plugins)
        initial_plugins = _normalized_plugin_overrides(initial.plugins)
        final_plugins = _normalized_plugin_overrides(final.plugins)
        initial_inventory = _normalized_inventory(initial.inventory)
        final_inventory = _normalized_inventory(final.inventory)
        initial_marketplaces = _marketplace_mapping(initial.marketplaces)
        final_marketplaces = _marketplace_mapping(final.marketplaces)

        changed_plugins = {
            plugin_id
            for plugin_id in initial_plugins.keys() | final_plugins.keys()
            if initial_plugins.get(plugin_id, False)
            != final_plugins.get(plugin_id, False)
        }
        removed_inventory = initial_inventory - final_inventory
        missing = object()
        changed_marketplaces = {
            key
            for key in initial_marketplaces.keys() | final_marketplaces.keys()
            if initial_marketplaces.get(key, missing)
            != final_marketplaces.get(key, missing)
        }
        if not changed_plugins and not removed_inventory and not changed_marketplaces:
            return

        with self._lock():
            sidecars: dict[Path, tuple[str, dict[str, bool], dict[str, bool]]] = {}
            current_path = self._sidecar_path(provider_id)

            if removed_inventory:
                for path in sorted(self.provider_directory.glob("*.json")):
                    stored_provider, plugins = self._read_sidecar(path)
                    sidecars[path] = (
                        stored_provider,
                        deepcopy(plugins),
                        plugins,
                    )

            if changed_plugins and current_path not in sidecars:
                if current_path.exists() or current_path.is_symlink():
                    stored_provider, plugins = self._read_sidecar(
                        current_path, provider_id
                    )
                else:
                    stored_provider, plugins = provider_id, {}
                sidecars[current_path] = (
                    stored_provider,
                    deepcopy(plugins),
                    plugins,
                )

            if changed_plugins:
                stored_provider, original, latest = sidecars[current_path]
                for plugin_id in changed_plugins:
                    if plugin_id in removed_inventory:
                        latest.pop(plugin_id, None)
                        continue
                    desired = final_plugins.get(plugin_id, False)
                    if desired == baseline.get(plugin_id, False):
                        latest.pop(plugin_id, None)
                    else:
                        latest[plugin_id] = desired
                sidecars[current_path] = (stored_provider, original, latest)

            if removed_inventory:
                for path, (stored_provider, original, latest) in sidecars.items():
                    for plugin_id in removed_inventory:
                        latest.pop(plugin_id, None)
                    sidecars[path] = (stored_provider, original, latest)

            for path, (stored_provider, original, latest) in sidecars.items():
                if latest == original:
                    continue
                if latest:
                    self._write_sidecar(path, stored_provider, latest)
                else:
                    _remove_file(path)

            if changed_marketplaces:
                stored_marketplaces = self._read_marketplaces()
                if stored_marketplaces is None:
                    stored_marketplaces = {}
                updated_marketplaces = deepcopy(stored_marketplaces)
                for key in changed_marketplaces:
                    if key in final_marketplaces:
                        updated_marketplaces[key] = deepcopy(
                            final_marketplaces[key]
                        )
                    elif key in updated_marketplaces:
                        del updated_marketplaces[key]
                if updated_marketplaces != stored_marketplaces:
                    self._write_marketplaces(updated_marketplaces)
