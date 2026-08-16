"""Build a complete, temporary Codex home for one CC Switch provider."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import tomllib
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomli_w

from cc_switch_config import (
    DEFAULT_DB_PATH,
    CcSwitchConfigError,
    SelectedProvider,
    _sigterm_as_system_exit,
    _write_private_file,
    load_common_config,
    load_provider,
)
from codex_plugin_state import (
    CANONICAL_SUPERPOWERS_ID,
    LEGACY_SUPERPOWERS_ID,
    PluginStateStore,
    RuntimeSnapshot,
    compose_effective_config,
    deep_merge,
    normalize_plugin_id,
    plugin_flags,
    scan_plugin_inventory,
)


DIRECTORY_SHARE_ALLOWLIST = (
    "sessions",
    "archived_sessions",
    "shell_snapshots",
    "memories",
    "skills",
    "rules",
    "vendor_imports",
    "plugins",
    ".tmp",
)
FILE_SHARE_ALLOWLIST = (
    "history.jsonl",
    "session_index.jsonl",
    "AGENTS.md",
    "AGENTS.override.md",
)
_MATERIALIZED_DIRECTORY_ALLOWLIST = (
    "sessions",
    "archived_sessions",
)

_CODEX_API_KEY_ERROR = (
    "Codex TUI launcher supports only adaptable CC Switch providers with a "
    "single OPENAI_API_KEY"
)
_DEFAULT_PLUGIN_CONFIG_PATH = Path(__file__).with_name(
    "codex_plugin_defaults.toml"
)
_RUNTIME_HOME_PREFIX = "codex-candy-eval-codex-"
_STATE_DB_PATTERN = "state_*.sqlite"


@dataclass(frozen=True)
class CodexRuntime:
    provider_id: str
    provider_name: str
    model_provider: str
    home: Path
    config_path: Path
    environment: Mapping[str, str] = field(repr=False)
    warnings: tuple[str, ...]
    shared_home: Path = field(repr=False)
    shared_sqlite_home: Path = field(repr=False)

    def activate_thread_rollout(self, thread_id: str) -> bool:
        """Map one persisted thread to this runtime before resume/fork."""
        return (
            _activate_runtime_rollout_paths(
                self.shared_sqlite_home,
                self.shared_home,
                self.home,
                (thread_id,),
            )
            > 0
        )

    def activate_provider_rollouts(self) -> int:
        """Map picker-visible provider threads into this runtime home."""
        return _activate_runtime_rollout_paths(
            self.shared_sqlite_home,
            self.shared_home,
            self.home,
            (),
            model_provider=self.model_provider,
        )


@dataclass
class _PreparedCodexRuntime:
    temporary_directory: tempfile.TemporaryDirectory[str]
    runtime: CodexRuntime
    state_store: PluginStateStore
    baseline_plugins: dict[str, bool]
    initial_snapshot: RuntimeSnapshot
    shared_plugins: Path
    shared_home: Path
    shared_sqlite_home: Path


def _parse_toml(payload: object) -> dict[str, Any]:
    parsed: dict[str, Any] | None = None
    if isinstance(payload, str):
        try:
            parsed = tomllib.loads(payload)
        except (tomllib.TOMLDecodeError, RecursionError):
            pass
    if parsed is None:
        raise CcSwitchConfigError("selected Codex configuration is invalid")
    return parsed


def _existing_directory(
    environment: Mapping[str, str], key: str, default: Path
) -> Path:
    configured = environment.get(key)
    inspection_failed = False
    resolved: Path | None = None
    try:
        if configured is None:
            candidate = default
        elif configured:
            candidate = Path(configured).expanduser()
        else:
            candidate = None
        if candidate is not None:
            resolved = candidate.resolve()
            if not resolved.is_dir():
                resolved = None
    except (OSError, RuntimeError):
        inspection_failed = True

    if inspection_failed or resolved is None:
        raise CcSwitchConfigError(f"{key} must refer to an existing directory")
    return resolved


def _read_shared_config(shared_home: Path) -> dict[str, Any]:
    path = shared_home / "config.toml"
    if not path.exists() and not path.is_symlink():
        return {}
    payload = ""
    read_failed = False
    try:
        if path.is_symlink() or not path.is_file():
            raise OSError("shared config is not a regular file")
        payload = path.read_text(encoding="utf-8")
    except OSError:
        read_failed = True
    if read_failed:
        raise CcSwitchConfigError("shared Codex configuration is invalid")

    parsed: dict[str, Any] | None = None
    try:
        parsed = tomllib.loads(payload)
    except (tomllib.TOMLDecodeError, RecursionError):
        pass
    if parsed is None:
        raise CcSwitchConfigError("shared Codex configuration is invalid")
    return parsed


def _load_default_plugin_config() -> dict[str, bool]:
    path = _DEFAULT_PLUGIN_CONFIG_PATH
    try:
        if path.is_symlink() or not path.is_file():
            raise OSError("default plugin config is not a regular file")
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError, RecursionError):
        raise CcSwitchConfigError(
            "Codex default plugin configuration is invalid"
        ) from None

    if set(document) != {"plugins"}:
        raise CcSwitchConfigError(
            "Codex default plugin configuration is invalid"
        )
    plugins = document["plugins"]
    if not isinstance(plugins, Mapping):
        raise CcSwitchConfigError(
            "Codex default plugin configuration is invalid"
        )

    defaults: dict[str, bool] = {}
    canonical_is_explicit = CANONICAL_SUPERPOWERS_ID in plugins
    for plugin_id, entry in plugins.items():
        if (
            not isinstance(plugin_id, str)
            or not plugin_id
            or "\0" in plugin_id
            or not isinstance(entry, Mapping)
            or set(entry) != {"enabled"}
            or not isinstance(entry["enabled"], bool)
        ):
            raise CcSwitchConfigError(
                "Codex default plugin configuration is invalid"
            )
        if plugin_id == LEGACY_SUPERPOWERS_ID and canonical_is_explicit:
            continue
        defaults[normalize_plugin_id(plugin_id)] = entry["enabled"]
    return defaults


def _marketplaces(document: Mapping[str, Any]) -> dict[str, Any]:
    marketplaces = document.get("marketplaces", {})
    if not isinstance(marketplaces, Mapping):
        raise CcSwitchConfigError("Codex marketplaces configuration is invalid")
    if any(not isinstance(key, str) for key in marketplaces):
        raise CcSwitchConfigError("Codex marketplaces configuration is invalid")
    return deepcopy(dict(marketplaces))


def _codex_auth_parts(
    document: Mapping[str, Any], auth: object
) -> tuple[dict[str, Any], dict[str, str], dict[str, str]]:
    if (
        not isinstance(auth, dict)
        or set(auth) != {"OPENAI_API_KEY"}
        or not isinstance(auth.get("OPENAI_API_KEY"), str)
        or "\0" in auth["OPENAI_API_KEY"]
    ):
        raise CcSwitchConfigError(_CODEX_API_KEY_ERROR)

    provider_id = document.get("model_provider")
    providers = document.get("model_providers")
    provider_config = (
        providers.get(provider_id)
        if isinstance(provider_id, str) and isinstance(providers, Mapping)
        else None
    )
    if (
        not isinstance(provider_id, str)
        or not provider_id.strip()
        or not isinstance(provider_config, Mapping)
        or provider_config.get("requires_openai_auth") is not True
        or "env_key" in provider_config
    ):
        raise CcSwitchConfigError(_CODEX_API_KEY_ERROR)

    transformed = deepcopy(dict(document))
    selected = transformed["model_providers"][provider_id]
    selected["requires_openai_auth"] = False
    selected["env_key"] = "OPENAI_API_KEY"
    api_key = auth["OPENAI_API_KEY"]
    return (
        transformed,
        {"OPENAI_API_KEY": api_key},
        {"OPENAI_API_KEY": api_key},
    )


def _prepare_shared_targets(shared_home: Path) -> dict[str, Path]:
    targets: dict[str, Path] = {}
    for name in DIRECTORY_SHARE_ALLOWLIST:
        target = shared_home / name
        invalid = False
        try:
            if target.is_symlink():
                raise OSError("shared directory is a symlink")
            if target.exists():
                if not target.is_dir():
                    raise OSError("shared directory has wrong type")
            else:
                target.mkdir(mode=0o700)
                target.chmod(0o700)
        except OSError:
            invalid = True
        if invalid:
            raise CcSwitchConfigError(
                "shared Codex state has an incompatible path"
            )
        targets[name] = target

    for name in FILE_SHARE_ALLOWLIST:
        target = shared_home / name
        invalid = False
        try:
            if target.is_symlink() or (target.exists() and not target.is_file()):
                raise OSError("shared file has wrong type")
        except OSError:
            invalid = True
        if invalid:
            raise CcSwitchConfigError(
                "shared Codex state has an incompatible path"
            )
        targets[name] = target
    return targets


def _link_shared_targets(runtime_home: Path, targets: Mapping[str, Path]) -> None:
    for name in (*DIRECTORY_SHARE_ALLOWLIST, *FILE_SHARE_ALLOWLIST):
        if name in _MATERIALIZED_DIRECTORY_ALLOWLIST:
            continue
        failed = False
        try:
            (runtime_home / name).symlink_to(
                targets[name], target_is_directory=name in DIRECTORY_SHARE_ALLOWLIST
            )
        except OSError:
            failed = True
        if failed:
            raise CcSwitchConfigError("failed to link shared Codex state")


def _materialize_shared_tree(runtime_path: Path, shared_path: Path) -> None:
    """Expose session files under the runtime home without a sessions symlink.

    Codex 0.147.0 validates rollout paths lexically against CODEX_HOME during
    an in-process fork. Hard-linking existing files keeps the shared state
    cheap while ensuring active rollouts are addressed by runtime-local paths.
    """
    try:
        runtime_path.mkdir(mode=0o700)
        for source in shared_path.rglob("*"):
            relative = source.relative_to(shared_path)
            target = runtime_path / relative
            if source.is_symlink():
                raise OSError("shared session tree contains a symlink")
            if source.is_dir():
                target.mkdir(mode=0o700)
                continue
            if not source.is_file():
                raise OSError("shared session tree contains an invalid entry")
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                os.link(source, target)
            except OSError:
                shutil.copy2(source, target)
    except OSError:
        raise CcSwitchConfigError(
            "failed to materialize shared Codex sessions"
        ) from None


def _synchronize_materialized_tree(runtime_path: Path, shared_path: Path) -> None:
    """Publish files created while Codex used the runtime-local session tree."""
    try:
        for source in runtime_path.rglob("*"):
            relative = source.relative_to(runtime_path)
            target = shared_path / relative
            if source.is_symlink():
                raise OSError("runtime session tree contains a symlink")
            if source.is_dir():
                target.mkdir(mode=0o700, parents=True, exist_ok=True)
                continue
            if not source.is_file():
                raise OSError("runtime session tree contains an invalid entry")
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if target.exists() and os.path.samefile(source, target):
                continue
            if target.exists():
                shutil.copy2(source, target)
                continue
            try:
                os.link(source, target)
            except OSError:
                shutil.copy2(source, target)
    except OSError:
        raise CcSwitchConfigError(
            "failed to synchronize shared Codex sessions"
        ) from None


def _runtime_rollout_path(
    thread_id: object,
    rollout_path: object,
    runtime_home: Path,
    shared_home: Path,
) -> Path | None:
    if (
        not isinstance(thread_id, str)
        or not thread_id
        or not isinstance(rollout_path, str)
        or not rollout_path
    ):
        return None

    source = Path(rollout_path)
    for name in _MATERIALIZED_DIRECTORY_ALLOWLIST:
        shared_root = shared_home / name
        runtime_root = runtime_home / name
        try:
            resolved_shared_root = shared_root.resolve(strict=True)
            relative = source.resolve(strict=True).relative_to(
                resolved_shared_root
            )
            if not relative.parts or any(
                part in ("", ".", "..") for part in relative.parts
            ):
                continue
            target = runtime_root / relative
            if target.is_symlink():
                continue
            resolved_runtime_root = runtime_root.resolve(strict=True)
            resolved_target = target.resolve(strict=True)
            resolved_target.relative_to(resolved_runtime_root)
            if (
                not resolved_target.is_file()
                or not resolved_target.name.endswith(
                    f"-{thread_id}.jsonl"
                )
            ):
                continue
            with resolved_target.open(encoding="utf-8") as rollout:
                first_event = json.loads(rollout.readline())
            if (
                not isinstance(first_event, dict)
                or first_event.get("type") != "session_meta"
                or not isinstance(first_event.get("payload"), dict)
                or first_event["payload"].get("id") != thread_id
            ):
                continue
            return resolved_target
        except (
            json.JSONDecodeError,
            OSError,
            RuntimeError,
            UnicodeError,
            ValueError,
        ):
            continue
    return None


def _activate_runtime_rollout_paths(
    sqlite_home: Path,
    shared_home: Path,
    runtime_home: Path,
    thread_ids: tuple[str, ...],
    *,
    model_provider: str | None = None,
    timeout: float = 10,
) -> int:
    valid_ids = tuple(
        dict.fromkeys(
            thread_id
            for thread_id in thread_ids
            if isinstance(thread_id, str) and thread_id
        )
    )
    if not valid_ids and not model_provider:
        return 0

    try:
        candidates = []
        for path in sqlite_home.glob(_STATE_DB_PATTERN):
            suffix = path.stem.removeprefix("state_")
            if suffix.isdigit():
                candidates.append((int(suffix), path))
    except OSError:
        raise CcSwitchConfigError(
            "failed to activate Codex session state"
        ) from None
    if not candidates:
        return 0

    database_path = max(candidates)[1]
    connection: sqlite3.Connection | None = None
    try:
        if database_path.is_symlink() or not database_path.is_file():
            return 0
        connection = sqlite3.connect(database_path, timeout=timeout)
        connection.execute(
            f"PRAGMA busy_timeout = {max(1, int(timeout * 1000))}"
        )
        if valid_ids:
            placeholders = ",".join("?" for _ in valid_ids)
            rows = list(
                connection.execute(
                    f"""
                    SELECT id, rollout_path
                    FROM threads
                    WHERE id IN ({placeholders})
                    """,
                    valid_ids,
                )
            )
        else:
            rows = list(
                connection.execute(
                    """
                    SELECT id, rollout_path
                    FROM threads
                    WHERE model_provider = ?
                    """,
                    (model_provider,),
                )
            )
        updates: list[tuple[str, str, str]] = []
        for thread_id, rollout_path in rows:
            target = _runtime_rollout_path(
                thread_id,
                rollout_path,
                runtime_home,
                shared_home,
            )
            if target is not None and str(target) != rollout_path:
                updates.append((str(target), thread_id, rollout_path))
        activated = 0
        if updates:
            with connection:
                for target, thread_id, rollout_path in updates:
                    cursor = connection.execute(
                        """
                        UPDATE threads
                        SET rollout_path = ?
                        WHERE id = ? AND rollout_path = ?
                        """,
                        (target, thread_id, rollout_path),
                    )
                    activated += cursor.rowcount
        return activated
    except (OSError, sqlite3.Error):
        raise CcSwitchConfigError(
            "failed to activate Codex session state"
        ) from None
    finally:
        if connection is not None:
            connection.close()


def _shared_rollout_path(
    thread_id: object,
    rollout_path: object,
    shared_home: Path,
) -> Path | None:
    if (
        not isinstance(thread_id, str)
        or not thread_id
        or not isinstance(rollout_path, str)
        or not rollout_path
    ):
        return None

    source = Path(rollout_path)
    parts = source.parts
    runtime_index: int | None = None
    for index, part in enumerate(parts):
        if part.startswith(_RUNTIME_HOME_PREFIX):
            runtime_index = index
            break
    if (
        runtime_index is None
        or runtime_index + 2 >= len(parts)
        or parts[runtime_index + 1] not in _MATERIALIZED_DIRECTORY_ALLOWLIST
    ):
        return None

    directory_name = parts[runtime_index + 1]
    relative_parts = parts[runtime_index + 2 :]
    if not relative_parts or any(
        part in ("", ".", "..") for part in relative_parts
    ):
        return None
    filename = relative_parts[-1]
    if not filename.endswith(f"-{thread_id}.jsonl"):
        return None

    shared_directory = shared_home / directory_name
    target = shared_directory.joinpath(*relative_parts)
    try:
        if target.is_symlink():
            return None
        resolved_sessions = shared_directory.resolve()
        resolved_target = target.resolve(strict=True)
        resolved_target.relative_to(resolved_sessions)
        if not resolved_target.is_file():
            return None
        with resolved_target.open(encoding="utf-8") as rollout:
            first_event = json.loads(rollout.readline())
        if (
            not isinstance(first_event, dict)
            or first_event.get("type") != "session_meta"
            or not isinstance(first_event.get("payload"), dict)
            or first_event["payload"].get("id") != thread_id
        ):
            return None
    except (
        json.JSONDecodeError,
        OSError,
        RuntimeError,
        UnicodeError,
        ValueError,
    ):
        return None
    return resolved_target


def _repair_launcher_rollout_paths(
    sqlite_home: Path,
    shared_home: Path,
) -> int:
    """Rewrite launcher-temporary rollout paths to their shared session paths."""
    repaired = 0
    try:
        candidates = []
        for path in sqlite_home.glob(_STATE_DB_PATTERN):
            suffix = path.stem.removeprefix("state_")
            if suffix.isdigit():
                candidates.append((int(suffix), path))
    except OSError:
        raise CcSwitchConfigError(
            "failed to repair shared Codex session state"
        ) from None

    database_paths = [max(candidates)[1]] if candidates else []

    for database_path in database_paths:
        connection: sqlite3.Connection | None = None
        try:
            if database_path.is_symlink() or not database_path.is_file():
                continue
            connection = sqlite3.connect(database_path, timeout=10)
            connection.execute("PRAGMA busy_timeout = 10000")
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(threads)")
            }
            if not {"id", "rollout_path"}.issubset(columns):
                continue
            rows = list(
                connection.execute(
                    """
                    SELECT id, rollout_path
                    FROM threads
                    WHERE rollout_path LIKE ?
                    """,
                    (f"%/{_RUNTIME_HOME_PREFIX}%/%",),
                )
            )
            repairs: list[tuple[str, str, str]] = []
            for thread_id, rollout_path in rows:
                target = _shared_rollout_path(
                    thread_id,
                    rollout_path,
                    shared_home,
                )
                if target is not None:
                    repairs.append((str(target), thread_id, rollout_path))
            if repairs:
                with connection:
                    for target, thread_id, rollout_path in repairs:
                        cursor = connection.execute(
                            """
                            UPDATE threads
                            SET rollout_path = ?
                            WHERE id = ? AND rollout_path = ?
                            """,
                            (target, thread_id, rollout_path),
                        )
                        repaired += cursor.rowcount
        except (OSError, sqlite3.Error):
            raise CcSwitchConfigError(
                "failed to repair shared Codex session state"
            ) from None
        finally:
            if connection is not None:
                connection.close()
    return repaired


def _cleanup_temporary_directory(
    temporary_directory: tempfile.TemporaryDirectory[str],
) -> bool:
    try:
        temporary_directory.cleanup()
    except BaseException:
        return False
    return True


def _prepare_codex_runtime(
    provider: SelectedProvider,
    common_config: str | None,
    *,
    reset_plugin_state: bool = False,
) -> _PreparedCodexRuntime:
    temporary_directory: tempfile.TemporaryDirectory[str] | None = None
    runtime: CodexRuntime | None = None
    prepared: _PreparedCodexRuntime | None = None
    setup_error: BaseException | None = None

    try:
        if provider.app_type != "codex":
            raise CcSwitchConfigError(_CODEX_API_KEY_ERROR)

        environment = os.environ.copy()
        shared_home = _existing_directory(
            environment, "CODEX_HOME", Path.home() / ".codex"
        )
        shared_sqlite_home = _existing_directory(
            environment,
            "CODEX_SQLITE_HOME",
            shared_home,
        )

        provider_document = _parse_toml(provider.settings.get("config"))
        common_document = (
            _parse_toml(common_config) if common_config is not None else None
        )
        shared_document = _read_shared_config(shared_home)
        default_plugins = _load_default_plugin_config()
        baseline_document = (
            deep_merge(provider_document, common_document)
            if common_document is not None
            else deepcopy(provider_document)
        )

        targets = _prepare_shared_targets(shared_home)
        shared_plugins = targets["plugins"]
        state_store = PluginStateStore(shared_home)
        if reset_plugin_state:
            state_store.reset_provider(provider.provider_id)
        provider_plugins = state_store.load_provider_plugins(
            provider.provider_id
        )
        launcher_marketplaces = state_store.load_or_initialize_marketplaces(
            _marketplaces(shared_document),
            _marketplaces(baseline_document),
        )
        inventory = scan_plugin_inventory(shared_plugins)
        composed = compose_effective_config(
            provider_document,
            common_document,
            common_document is not None,
            launcher_marketplaces,
            inventory,
            provider_plugins,
            default_plugins=default_plugins,
        )
        final_document, auth_environment, auth_document = _codex_auth_parts(
            composed.document, provider.settings.get("auth")
        )
        final_document.pop("sqlite_home", None)
        rendered = tomli_w.dumps(final_document)
        rendered_auth = json.dumps(auth_document, separators=(",", ":")) + "\n"
        serialized_document = tomllib.loads(rendered)
        if serialized_document != final_document:
            raise CcSwitchConfigError("selected Codex configuration is invalid")
        _repair_launcher_rollout_paths(shared_sqlite_home, shared_home)

        temporary_directory = tempfile.TemporaryDirectory(
            prefix="codex-candy-eval-codex-"
        )
        runtime_home = Path(temporary_directory.name)
        runtime_home.chmod(0o700)
        for name in _MATERIALIZED_DIRECTORY_ALLOWLIST:
            _materialize_shared_tree(runtime_home / name, targets[name])
        _link_shared_targets(runtime_home, targets)
        config_path = runtime_home / "config.toml"
        _write_private_file(config_path, rendered)
        _write_private_file(runtime_home / "auth.json", rendered_auth)

        environment["CODEX_HOME"] = str(runtime_home)
        environment["CODEX_SQLITE_HOME"] = str(shared_sqlite_home)
        environment.update(auth_environment)
        environment.setdefault("CODEX_INTERNAL_ORIGINATOR_OVERRIDE", "codex-tui")
        runtime = CodexRuntime(
            provider_id=provider.provider_id,
            provider_name=provider.name,
            model_provider=final_document["model_provider"],
            home=runtime_home,
            config_path=config_path,
            environment=environment,
            warnings=composed.warnings,
            shared_home=shared_home,
            shared_sqlite_home=shared_sqlite_home,
        )
        initial_snapshot = RuntimeSnapshot(
            plugins=plugin_flags(serialized_document),
            marketplaces=_marketplaces(serialized_document),
            inventory=inventory,
        )
        prepared = _PreparedCodexRuntime(
            temporary_directory=temporary_directory,
            runtime=runtime,
            state_store=state_store,
            baseline_plugins=composed.baseline_plugins,
            initial_snapshot=initial_snapshot,
            shared_plugins=shared_plugins,
            shared_home=shared_home,
            shared_sqlite_home=shared_sqlite_home,
        )
    except BaseException as exc:
        setup_error = exc

    if setup_error is not None:
        if temporary_directory is not None:
            _cleanup_temporary_directory(temporary_directory)
        if isinstance(setup_error, (KeyboardInterrupt, SystemExit)):
            raise setup_error
        if isinstance(setup_error, CcSwitchConfigError):
            raise setup_error
        raise CcSwitchConfigError(
            "failed to materialize selected Codex runtime"
        )

    assert temporary_directory is not None
    assert runtime is not None
    assert prepared is not None
    return prepared


def _synchronize_runtime(prepared: _PreparedCodexRuntime) -> None:
    final_document = _parse_toml(
        prepared.runtime.config_path.read_text(encoding="utf-8")
    )
    final_snapshot = RuntimeSnapshot(
        plugins=plugin_flags(final_document),
        marketplaces=_marketplaces(final_document),
        inventory=scan_plugin_inventory(prepared.shared_plugins),
    )
    prepared.state_store.apply_runtime_changes(
        prepared.runtime.provider_id,
        prepared.baseline_plugins,
        prepared.initial_snapshot,
        final_snapshot,
    )


def _synchronize_runtime_safely(prepared: _PreparedCodexRuntime) -> bool:
    try:
        _synchronize_runtime(prepared)
    except BaseException:
        return False
    return True


def _repair_runtime_rollout_paths_safely(
    prepared: _PreparedCodexRuntime,
) -> bool:
    try:
        _repair_launcher_rollout_paths(
            prepared.shared_sqlite_home,
            prepared.shared_home,
        )
    except BaseException:
        return False
    return True


def _synchronize_runtime_sessions_safely(
    prepared: _PreparedCodexRuntime,
) -> bool:
    try:
        for name in _MATERIALIZED_DIRECTORY_ALLOWLIST:
            _synchronize_materialized_tree(
                prepared.runtime.home / name,
                prepared.shared_home / name,
            )
    except BaseException:
        return False
    return True


@contextmanager
def materialize_codex_runtime(
    provider: SelectedProvider,
    common_config: str | None,
    *,
    reset_plugin_state: bool = False,
) -> Iterator[CodexRuntime]:
    """Materialize one complete Codex configuration in a temporary home."""
    prepared = _prepare_codex_runtime(
        provider,
        common_config,
        reset_plugin_state=reset_plugin_state,
    )
    body_failed = False
    try:
        yield prepared.runtime
    except BaseException:
        body_failed = True
        raise
    finally:
        sync_succeeded = _synchronize_runtime_safely(prepared)
        sessions_synchronized = _synchronize_runtime_sessions_safely(prepared)
        repair_succeeded = _repair_runtime_rollout_paths_safely(prepared)
        cleanup_succeeded = _cleanup_temporary_directory(
            prepared.temporary_directory
        )
        if not body_failed and (
            not sync_succeeded
            or not sessions_synchronized
            or not repair_succeeded
            or not cleanup_succeeded
        ):
            raise CcSwitchConfigError(
                "failed to finalize selected Codex runtime"
            )


@contextmanager
def use_codex_runtime(
    selector: str,
    *,
    reset_plugin_state: bool = False,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> Iterator[CodexRuntime]:
    """Select one Codex provider and materialize its complete runtime."""
    if os.name == "nt":
        raise CcSwitchConfigError(
            "--cc-switch-config is currently supported only on macOS/Linux"
        )

    provider = load_provider("codex", selector, db_path)
    common_config = (
        load_common_config("codex", db_path)
        if provider.meta.get("commonConfigEnabled") is True
        else None
    )
    with _sigterm_as_system_exit():
        with materialize_codex_runtime(
            provider,
            common_config,
            reset_plugin_state=reset_plugin_state,
        ) as runtime:
            yield runtime
