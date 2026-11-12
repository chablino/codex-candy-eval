"""Persist per-provider Claude plugin switches for the Claude launcher."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any


STATE_DIRECTORY_NAME = ".cc-switch-tui"
OVERLAY_PREFIX = "codex-candy-eval-claude-"
_INSTALL_RECORDS = (
    ("installed_plugins.json", "installPath"),
    ("known_marketplaces.json", "installLocation"),
)
_RESET_HINT = "; use --reset-plugin-state to clear it"


class ClaudePluginStateError(RuntimeError):
    """Raised when launcher-owned Claude plugin state cannot be used safely."""


@dataclass(frozen=True)
class RuntimeSnapshot:
    plugins: Mapping[str, bool]
    # None means the install record could not be read, so removals are unknown.
    installed: frozenset[str] | None


def plugin_enabled(value: object) -> bool:
    """Match Claude Code: true or a version-constraint array enables a plugin."""
    return value is True or isinstance(value, list)


def enabled_view(plugins: Mapping[str, Any]) -> dict[str, bool]:
    return {
        plugin_id: plugin_enabled(value) for plugin_id, value in plugins.items()
    }


def effective_plugins(
    baseline: Mapping[str, Any], saved: Mapping[str, bool]
) -> dict[str, Any]:
    """Overlay saved switches on the live shared-settings baseline."""
    effective = deepcopy(dict(baseline))
    effective.update(saved)
    return effective


def installed_plugin_settings(
    plugins: Mapping[str, Any], installed: frozenset[str] | None
) -> dict[str, Any]:
    """Omit uninstalled plugins from window settings without erasing preferences.

    Claude can report a missing install even for a false enabledPlugins entry.
    True entries can trigger installation again. Installation must be explicit;
    neither kind of stale preference belongs in a new window's settings.
    An unreadable registry is not proof of an uninstall: refuse to launch rather
    than restore stale enables or silently discard the user's plugin settings.
    """
    if installed is None:
        raise ClaudePluginStateError(
            "Claude plugin installation record could not be read; "
            "plugin settings were not changed"
        )
    return {
        plugin_id: deepcopy(value)
        for plugin_id, value in plugins.items()
        if plugin_id in installed
    }


def read_installed_plugins(plugins_root: Path) -> frozenset[str] | None:
    """Return installed plugin IDs, or None when the record cannot be trusted."""
    try:
        payload = json.loads(
            (plugins_root / "installed_plugins.json").read_text(encoding="utf-8")
        )
    except FileNotFoundError:
        return frozenset()
    except (OSError, UnicodeDecodeError, ValueError, RecursionError):
        return None
    plugins = payload.get("plugins") if isinstance(payload, dict) else None
    if not isinstance(plugins, dict) or any(
        not isinstance(plugin_id, str)
        or not isinstance(records, list)
        or any(not isinstance(record, dict) for record in records)
        for plugin_id, records in plugins.items()
    ):
        return None
    return frozenset(
        plugin_id for plugin_id, records in plugins.items() if records
    )


def _ensure_private_directory(path: Path) -> None:
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.is_symlink() or not path.is_dir():
            raise OSError("state path is not a directory")
        path.chmod(0o700)
    except OSError:
        raise ClaudePluginStateError(
            "failed to prepare Claude plugin state"
        ) from None


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
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
        _fsync_directory(path.parent)
    except OSError:
        raise ClaudePluginStateError(
            "failed to write Claude plugin state"
        ) from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except OSError:
                pass


def _reject_json_constant(_constant: str) -> None:
    raise ValueError("non-standard JSON constant")


class ClaudePluginStateStore:
    """Persist launcher-owned plugin switches under a shared Claude home."""

    _VERSION = 1

    def __init__(self, shared_home: Path) -> None:
        self.shared_home = Path(shared_home)
        self.state_root = self.shared_home / STATE_DIRECTORY_NAME
        self.provider_directory = self.state_root / "provider-plugins"
        self.lock_directory = self.state_root / "locks"
        self.lock_path = self.lock_directory / "state.lock"

    def _sidecar_path(self, provider_id: str) -> Path:
        digest = hashlib.sha256(provider_id.encode()).hexdigest()
        return self.provider_directory / f"{digest}.json"

    @contextmanager
    def lock(self) -> Iterator[None]:
        # POSIX-only; importing it here keeps cc_switch_config importable on
        # Windows, where selector mode reports its own error.
        import fcntl

        for directory in (
            self.state_root,
            self.provider_directory,
            self.lock_directory,
        ):
            _ensure_private_directory(directory)
        descriptor = -1
        try:
            descriptor = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        except OSError:
            if descriptor >= 0:
                os.close(descriptor)
            raise ClaudePluginStateError(
                "failed to lock Claude plugin state"
            ) from None
        try:
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def _read_sidecar(
        self, path: Path, expected_provider_id: str | None
    ) -> tuple[str, dict[str, bool]]:
        payload: object = None
        unreadable = False
        try:
            if path.is_symlink() or not path.is_file():
                raise OSError("sidecar is not a regular file")
            payload = json.loads(
                path.read_text(encoding="utf-8"),
                parse_constant=_reject_json_constant,
            )
        except (OSError, UnicodeDecodeError, ValueError, RecursionError):
            unreadable = True

        if (
            unreadable
            or not isinstance(payload, dict)
            or set(payload) != {"version", "provider_id", "plugins"}
        ):
            raise ClaudePluginStateError(
                "invalid Claude provider plugin state" + _RESET_HINT
            )
        if type(payload["version"]) is not int or payload["version"] != self._VERSION:
            raise ClaudePluginStateError(
                "unknown Claude provider plugin state version" + _RESET_HINT
            )
        provider_id = payload["provider_id"]
        if (
            not isinstance(provider_id, str)
            or path != self._sidecar_path(provider_id)
            or (
                expected_provider_id is not None
                and provider_id != expected_provider_id
            )
        ):
            raise ClaudePluginStateError(
                "Claude provider plugin state ID mismatch" + _RESET_HINT
            )
        plugins = payload["plugins"]
        if not isinstance(plugins, dict) or any(
            not isinstance(plugin_id, str) or type(value) is not bool
            for plugin_id, value in plugins.items()
        ):
            raise ClaudePluginStateError(
                "invalid Claude provider plugin state" + _RESET_HINT
            )
        return provider_id, dict(plugins)

    def _write_sidecar(
        self, path: Path, provider_id: str, plugins: Mapping[str, bool]
    ) -> None:
        document = {
            "version": self._VERSION,
            "provider_id": provider_id,
            "plugins": dict(sorted(plugins.items())),
        }
        _atomic_write_text(
            path, json.dumps(document, ensure_ascii=False, indent=2) + "\n"
        )

    @staticmethod
    def _remove_file(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
            _fsync_directory(path.parent)
        except OSError:
            raise ClaudePluginStateError(
                "failed to remove Claude plugin state"
            ) from None

    def reset_provider(self, provider_id: str) -> None:
        with self.lock():
            self._remove_file(self._sidecar_path(provider_id))

    def load_provider_plugins(self, provider_id: str) -> dict[str, bool]:
        path = self._sidecar_path(provider_id)
        if not path.exists() and not path.is_symlink():
            return {}
        return self._read_sidecar(path, provider_id)[1]

    def apply_runtime_changes(
        self,
        provider_id: str,
        baseline: Mapping[str, Any],
        initial: RuntimeSnapshot,
        final: RuntimeSnapshot,
    ) -> None:
        """Merge one window's plugin changes into the latest saved state."""
        baseline_view = enabled_view(baseline)
        changed = {
            plugin_id
            for plugin_id in initial.plugins.keys() | final.plugins.keys()
            if initial.plugins.get(plugin_id, False)
            != final.plugins.get(plugin_id, False)
        }
        removed = (
            initial.installed - final.installed
            if initial.installed is not None and final.installed is not None
            else frozenset()
        )
        if not changed and not removed:
            return

        with self.lock():
            current_path = self._sidecar_path(provider_id)
            paths = (
                sorted(self.provider_directory.glob("*.json")) if removed else []
            )
            if current_path not in paths:
                paths.append(current_path)

            sidecars: dict[Path, tuple[str, dict[str, bool], dict[str, bool]]] = {}
            for path in paths:
                if path.exists() or path.is_symlink():
                    stored_id, plugins = self._read_sidecar(
                        path, provider_id if path == current_path else None
                    )
                else:
                    stored_id, plugins = provider_id, {}
                sidecars[path] = (stored_id, dict(plugins), plugins)

            for _, _, latest in sidecars.values():
                for plugin_id in removed:
                    # Uninstall drops saved enables everywhere; an explicit
                    # disable survives a later reinstall.
                    if latest.get(plugin_id) is True:
                        del latest[plugin_id]

            latest = sidecars[current_path][2]
            for plugin_id in changed:
                desired = final.plugins.get(plugin_id, False)
                # A stale window can still say enabled after another window
                # uninstalls the shared files. Never save that as a new enable.
                if final.installed is not None and plugin_id not in final.installed:
                    desired = False
                # Uninstall deletes the window's entry, so a baseline-enabled
                # plugin is saved as disabled instead of coming back.
                if desired == baseline_view.get(plugin_id, False):
                    latest.pop(plugin_id, None)
                else:
                    latest[plugin_id] = desired

            for path, (stored_id, original, updated) in sidecars.items():
                if updated == original:
                    continue
                if updated:
                    self._write_sidecar(path, stored_id, updated)
                else:
                    self._remove_file(path)


def _install_records(name: str, document: object) -> list[dict[str, Any]]:
    if not isinstance(document, dict):
        return []
    if name == "installed_plugins.json":
        plugins = document.get("plugins")
        entries = plugins.values() if isinstance(plugins, dict) else []
        return [
            record
            for entry in entries
            if isinstance(entry, list)
            for record in entry
            if isinstance(record, dict)
        ]
    return [record for record in document.values() if isinstance(record, dict)]


def _replace_json_if_unchanged(
    path: Path, document: object, before: os.stat_result
) -> bool:
    """Atomically replace path unless another process wrote it since before."""
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path: Path | None = Path(temporary_name)
    try:
        os.fchmod(descriptor, stat.S_IMODE(before.st_mode))
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as output:
            descriptor = -1
            output.write(json.dumps(document, ensure_ascii=False, indent=2))
            output.flush()
            os.fsync(output.fileno())
        current = path.stat()
        if (current.st_mtime_ns, current.st_size) != (
            before.st_mtime_ns,
            before.st_size,
        ):
            return False
        os.replace(temporary_path, path)
        temporary_path = None
        _fsync_directory(path.parent)
        return True
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def repair_install_paths(
    shared_home: Path,
    temporary_root: Path,
    *,
    active_overlay: Path | None = None,
) -> int:
    """Point plugin install records made inside launcher overlays at shared_home.

    Claude Code records absolute paths under its config directory, which is
    the overlay in launcher windows. With active_overlay only that window's
    records are rewritten. Without it, records of overlays that no longer
    exist are rewritten when the shared copy exists, which recovers windows
    that never finalized. Unreadable records are left alone.
    """
    roots = {str(temporary_root)}
    try:
        roots.add(str(temporary_root.resolve()))
    except (OSError, RuntimeError):
        pass

    def rewrite(value: str) -> str | None:
        for root in roots:
            prefix = root.rstrip(os.sep) + os.sep
            if not value.startswith(prefix):
                continue
            overlay_name, _, relative = value[len(prefix) :].partition(os.sep)
            if not overlay_name.startswith(OVERLAY_PREFIX) or not relative:
                return None
            if active_overlay is not None:
                if overlay_name != active_overlay.name:
                    return None
            elif os.path.lexists(os.path.join(root, overlay_name)):
                return None
            shared_path = shared_home / relative
            return str(shared_path) if shared_path.exists() else None
        return None

    rewritten = 0
    for name, key in _INSTALL_RECORDS:
        path = shared_home / "plugins" / name
        try:
            if path.is_symlink() or not path.is_file():
                continue
            before = path.stat()
            document = json.loads(path.read_text(encoding="utf-8"))
            count = 0
            for record in _install_records(name, document):
                value = record.get(key)
                replacement = rewrite(value) if isinstance(value, str) else None
                if replacement is not None:
                    record[key] = replacement
                    count += 1
            if count and _replace_json_if_unchanged(path, document, before):
                rewritten += count
        except (OSError, UnicodeDecodeError, ValueError, RecursionError):
            continue
    return rewritten
