"""Read and validate provider settings from the CC Switch App database."""

from __future__ import annotations

import json
import os
import re
import signal
import sqlite3
import tempfile
import tomllib
from collections.abc import Callable, Iterator, Mapping, Sequence
from copy import deepcopy
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

import tomli_w

from provider_settings import (
    claude_connection_env,
    claude_provider_settings,
    claude_shared_settings,
    codex_settings,
)
from claude_overlay import (
    GLOBAL_CONFIG_NAME,
    ClaudeOverlayError,
    link_shared_entries,
    reclaim_overlay_entries,
)
from claude_plugin_state import (
    OVERLAY_PREFIX,
    ClaudePluginStateError,
    ClaudePluginStateStore,
    RuntimeSnapshot,
    effective_plugins,
    enabled_view,
    installed_plugin_settings,
    read_installed_plugins,
    repair_install_paths,
)


AppType = Literal["codex", "claude"]

DEFAULT_DB_PATH = Path.home() / ".cc-switch" / "cc-switch.db"
CLAUDE_LIVE_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
CLAUDE_DEFAULT_HOME = Path.home() / ".claude"
CLAUDE_DEFAULT_GLOBAL_CONFIG = Path.home() / ".claude.json"
REQUIRED_PROVIDER_COLUMNS = frozenset(
    {"id", "app_type", "name", "settings_config", "meta"}
)


class CcSwitchConfigError(RuntimeError):
    """Raised when CC Switch provider settings cannot be safely loaded."""


@dataclass(frozen=True)
class SelectedProvider:
    provider_id: str
    app_type: AppType
    name: str
    settings: dict[str, Any] = field(repr=False)
    meta: dict[str, Any] = field(default_factory=dict, repr=False)
    shared_settings: dict[str, Any] | None = field(default=None, repr=False)


@dataclass(frozen=True)
class ProviderRuntime:
    provider_id: str
    provider_name: str
    environment: Mapping[str, str] = field(repr=False)
    secrets: tuple[str, ...] = field(repr=False)
    default_effort: str | None = None

    def redact(self, text: str) -> str:
        return redact_text(text, self.secrets)


@dataclass(frozen=True)
class ClaudeProfileRuntime:
    provider_id: str
    provider_name: str
    settings_path: Path
    config_home: Path
    environment: Mapping[str, str] = field(repr=False)
    default_effort: str | None = None
    mcp_config_path: Path | None = None


def _open_read_only(db_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)


def _resolve_database_path(db_path: str | Path) -> Path:
    try:
        resolved_path = Path(db_path).expanduser().resolve()
        is_file = resolved_path.is_file()
    except OSError:
        raise CcSwitchConfigError("failed to inspect CC Switch database path") from None
    if not is_file:
        raise CcSwitchConfigError(
            f"CC Switch database path is not a file: {resolved_path}"
        )
    return resolved_path


def _read_provider_metadata(
    app_type: AppType, db_path: Path
) -> list[tuple[str, str]]:
    connection: sqlite3.Connection | None = None
    try:
        connection = _open_read_only(db_path)
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(providers)")
        }
        if not columns:
            raise CcSwitchConfigError(
                "CC Switch database does not contain a providers table"
            )

        missing_columns = REQUIRED_PROVIDER_COLUMNS - columns
        if missing_columns:
            missing = ", ".join(sorted(missing_columns))
            raise CcSwitchConfigError(
                f"CC Switch providers table is missing required columns: {missing}"
            )

        rows = list(
            connection.execute(
                """
                SELECT id, name
                FROM providers
                WHERE app_type = ?
                ORDER BY name, id
                """,
                (app_type,),
            )
        )
        if any(
            not isinstance(provider_id, str) or not isinstance(name, str)
            for provider_id, name in rows
        ):
            raise CcSwitchConfigError(
                "CC Switch provider metadata id and name must be text"
            )
        return rows
    except CcSwitchConfigError:
        raise
    except sqlite3.Error:
        raise CcSwitchConfigError("failed to read CC Switch database") from None
    finally:
        if connection is not None:
            connection.close()


def _select_row(
    app_type: AppType,
    selector: str,
    rows: list[tuple[str, str]],
) -> tuple[str, str]:
    matches = [row for row in rows if row[0] == selector]
    if not matches:
        matches = [row for row in rows if row[1] == selector]

    if len(matches) > 1:
        matching_ids = ", ".join(sorted(row[0] for row in matches))
        raise CcSwitchConfigError(
            f"multiple {app_type} providers match selector; "
            f"matching IDs: {matching_ids}"
        )
    if matches:
        return matches[0]

    available = ", ".join(
        f"{name} ({provider_id})" for provider_id, name in rows
    )
    if not available:
        available = "none"
    raise CcSwitchConfigError(
        f"no {app_type} provider matches {selector!r}; "
        f"available names/IDs: {available}"
    )


def _read_provider_payload(
    app_type: AppType,
    provider_id: str,
    name: str,
    db_path: Path,
) -> tuple[object, object]:
    connection: sqlite3.Connection | None = None
    try:
        connection = _open_read_only(db_path)
        rows = list(
            connection.execute(
                """
                SELECT settings_config, meta
                FROM providers
                WHERE app_type = ? AND id = ? AND name = ?
                """,
                (app_type, provider_id, name),
            )
        )
        if len(rows) != 1:
            raise CcSwitchConfigError(
                f"selected {app_type} provider {provider_id!r} is no longer unique"
            )
        return rows[0]
    except CcSwitchConfigError:
        raise
    except sqlite3.Error:
        raise CcSwitchConfigError("failed to read CC Switch database") from None
    finally:
        if connection is not None:
            connection.close()


def _reject_json_constant(_constant: str) -> None:
    raise ValueError("non-standard JSON constant")


def _parse_settings(
    app_type: AppType, provider_id: str, payload: object
) -> dict[str, Any]:
    if not isinstance(payload, str):
        raise CcSwitchConfigError(
            f"provider {provider_id!r} settings_config must contain a JSON object"
        )

    try:
        settings = json.loads(payload, parse_constant=_reject_json_constant)
    except (json.JSONDecodeError, RecursionError, ValueError):
        # Construct the public error after leaving the except block so the
        # parser exception is not retained in its traceback/context chain.
        settings = None

    if settings is None:
        raise CcSwitchConfigError(
            f"provider {provider_id!r} settings_config must contain a JSON object"
        )

    if not isinstance(settings, dict):
        raise CcSwitchConfigError(
            f"provider {provider_id!r} settings_config must contain a JSON object"
        )

    if app_type == "codex":
        if not isinstance(settings.get("config"), str):
            raise CcSwitchConfigError(
                f"Codex provider {provider_id!r} settings_config "
                "requires config as text"
            )
        if not isinstance(settings.get("auth"), dict):
            raise CcSwitchConfigError(
                f"Codex provider {provider_id!r} settings_config "
                "requires auth as an object"
            )
    elif "env" in settings and not isinstance(settings["env"], dict):
        raise CcSwitchConfigError(
            f"Claude provider {provider_id!r} settings_config env must be an object"
        )

    return settings


def _parse_provider_meta(
    app_type: AppType, provider_id: str, payload: object
) -> dict[str, Any]:
    if not isinstance(payload, str):
        raise CcSwitchConfigError(
            f"provider {provider_id!r} metadata must contain a JSON object"
        )
    try:
        meta = json.loads(payload, parse_constant=_reject_json_constant)
    except (json.JSONDecodeError, RecursionError, ValueError):
        meta = None
    if not isinstance(meta, dict):
        raise CcSwitchConfigError(
            f"provider {provider_id!r} metadata must contain a JSON object"
        )
    return meta


def load_provider(
    app_type: AppType,
    selector: str,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> SelectedProvider:
    """Load one provider, preferring an exact ID over an exact display name."""
    if app_type not in ("codex", "claude"):
        raise CcSwitchConfigError(f"unsupported app type: {app_type!r}")

    resolved_path = _resolve_database_path(db_path)

    rows = _read_provider_metadata(app_type, resolved_path)
    provider_id, name = _select_row(app_type, selector, rows)
    settings_payload, meta_payload = _read_provider_payload(
        app_type, provider_id, name, resolved_path
    )
    settings = _parse_settings(app_type, provider_id, settings_payload)
    meta = _parse_provider_meta(app_type, provider_id, meta_payload)
    return SelectedProvider(provider_id, app_type, name, settings, meta)


def read_codex_live_config(shared_home: Path) -> dict[str, Any]:
    path = shared_home / "config.toml"
    payload = ""
    failed = False
    try:
        payload = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        pass
    except (OSError, UnicodeError):
        failed = True
    if not failed:
        try:
            return tomllib.loads(payload)
        except (tomllib.TOMLDecodeError, RecursionError):
            pass
    raise CcSwitchConfigError("shared Codex configuration is invalid")


def apply_codex_live_config(provider: SelectedProvider) -> SelectedProvider:
    shared_home = Path(
        os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))
    ).expanduser()
    shared = read_codex_live_config(shared_home)
    settings = deepcopy(provider.settings)
    failed = False
    try:
        settings["config"] = tomli_w.dumps(
            codex_settings(tomllib.loads(settings["config"]), shared)
        )
    except (ValueError, TypeError, RecursionError):
        failed = True
    if failed:
        raise CcSwitchConfigError("selected Codex configuration is invalid")
    return replace(provider, settings=settings)


def _json_deep_merge(
    base: Mapping[str, Any], overlay: Mapping[str, Any]
) -> dict[str, Any]:
    """Recursively merge JSON objects; arrays and scalars are replaced."""
    merged = deepcopy(dict(base))
    for key, value in overlay.items():
        current = merged.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            merged[key] = _json_deep_merge(current, value)
        else:
            merged[key] = deepcopy(value)
    return merged


def _parse_claude_settings(payload: str, label: str) -> dict[str, Any]:
    if not payload.strip():
        return {}
    try:
        settings = json.loads(payload, parse_constant=_reject_json_constant)
    except (json.JSONDecodeError, RecursionError, ValueError):
        settings = None
    if not isinstance(settings, dict) or (
        "env" in settings and not isinstance(settings["env"], dict)
    ):
        raise CcSwitchConfigError(f"{label} must contain a JSON object")
    return settings


_CLAUDE_EFFORT_ENV_KEY = "CLAUDE_CODE_EFFORT_LEVEL"
_CLAUDE_LAUNCH_EFFORTS = ("low", "medium", "high", "xhigh", "max")


def apply_claude_live_config(provider: SelectedProvider) -> SelectedProvider:
    """Use current shared settings plus only this provider's connection fields."""
    configured_home = os.environ.get("CLAUDE_CONFIG_DIR")
    path = (
        Path(configured_home).expanduser() / "settings.json"
        if configured_home else CLAUDE_LIVE_SETTINGS_PATH
    )
    payload = ""
    failed = False
    try:
        payload = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        pass
    except (OSError, UnicodeError):
        failed = True
    if failed:
        raise CcSwitchConfigError("failed to read Claude settings file")
    live = _parse_claude_settings(payload, "Claude settings file")
    shared = claude_shared_settings(live)
    selected = claude_provider_settings(provider.settings)
    settings = _json_deep_merge(shared, selected)
    # MCP definitions are indivisible: replacing a server must not inherit the
    # other server's headers or credentials.
    if "mcpServers" in selected:
        base = shared.get("mcpServers", {})
        servers = selected["mcpServers"]
        if not isinstance(base, dict) or not isinstance(servers, dict):
            raise CcSwitchConfigError("Claude mcpServers must be an object")
        settings["mcpServers"] = {**deepcopy(base), **deepcopy(servers)}
    return replace(provider, settings=settings, shared_settings=shared)


def _take_claude_default_effort(settings: dict[str, Any]) -> str | None:
    """Move a configured effort level out of env so /effort can change it.

    Claude Code lets CLAUDE_CODE_EFFORT_LEVEL override effort for the whole
    session, while --effort only sets the starting level. Values that --effort
    does not accept, such as auto or unset, stay in env unchanged.
    """
    environment = settings.get("env")
    if not isinstance(environment, dict):
        return None
    value = environment.get(_CLAUDE_EFFORT_ENV_KEY)
    if not isinstance(value, str):
        return None
    level = value.strip().lower()
    if level not in _CLAUDE_LAUNCH_EFFORTS:
        return None
    del environment[_CLAUDE_EFFORT_ENV_KEY]
    return level


def _write_private_file(path: Path, content: str) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as output:
            descriptor = -1
            output.write(content)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _collect_secrets(value: object) -> tuple[str, ...]:
    secrets: set[str] = set()

    def collect(item: object) -> None:
        if isinstance(item, str):
            if item:
                secrets.add(item)
        elif isinstance(item, Mapping):
            for child in item.values():
                collect(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                collect(child)

    collect(value)
    return tuple(sorted(secrets, key=lambda secret: (-len(secret), secret)))


def _json_document(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def _cleanup_temporary_directory(
    temporary_directory: tempfile.TemporaryDirectory[str],
) -> bool:
    try:
        temporary_directory.cleanup()
    except BaseException:
        return False
    return True


def _prepare_provider_runtime(
    provider: SelectedProvider,
) -> tuple[tempfile.TemporaryDirectory[str], ProviderRuntime]:
    temporary_directory: tempfile.TemporaryDirectory[str] | None = None
    setup_error: BaseException | None = None
    runtime: ProviderRuntime | None = None
    default_effort: str | None = None

    try:
        temporary_directory = tempfile.TemporaryDirectory(
            prefix="codex-candy-eval-"
        )
        root = Path(temporary_directory.name)
        root.chmod(0o700)

        if provider.app_type == "codex":
            _write_private_file(root / "config.toml", provider.settings["config"])
            _write_private_file(
                root / "auth.json", _json_document(provider.settings["auth"])
            )
            environment_key = "CODEX_HOME"
            secret_source = provider.settings["auth"]
        elif provider.app_type == "claude":
            settings = deepcopy(provider.settings)
            default_effort = _take_claude_default_effort(settings)
            _write_private_file(root / "settings.json", _json_document(settings))
            environment_key = "CLAUDE_CONFIG_DIR"
            secret_source = settings.get("env", {})
        else:
            raise ValueError("unsupported provider app type")

        environment = os.environ.copy()
        if provider.app_type == "claude":
            for key in tuple(environment):
                if claude_connection_env(key):
                    environment.pop(key, None)
            environment.update(settings.get("env", {}))
        environment[environment_key] = str(root)
        runtime = ProviderRuntime(
            provider_id=provider.provider_id,
            provider_name=provider.name,
            environment=environment,
            secrets=_collect_secrets(secret_source),
            default_effort=default_effort,
        )
    except BaseException as exc:
        setup_error = exc

    if setup_error is not None:
        if temporary_directory is not None:
            _cleanup_temporary_directory(temporary_directory)
        if isinstance(setup_error, (KeyboardInterrupt, SystemExit)):
            raise setup_error
        # This is intentionally outside the except block, so setup details do
        # not remain in the public exception context.
        raise CcSwitchConfigError(
            "failed to materialize selected provider runtime"
        )

    assert temporary_directory is not None
    assert runtime is not None
    return temporary_directory, runtime


@contextmanager
def materialize_provider(provider: SelectedProvider) -> Iterator[ProviderRuntime]:
    """Materialize provider files in a private temporary directory."""
    temporary_directory, runtime = _prepare_provider_runtime(provider)
    body_failed = False
    try:
        yield runtime
    except BaseException:
        body_failed = True
        raise
    finally:
        cleanup_succeeded = _cleanup_temporary_directory(temporary_directory)
        if not body_failed and not cleanup_succeeded:
            raise CcSwitchConfigError(
                "failed to clean up selected provider runtime"
            )


_CLAUDE_PROFILE_ERROR = (
    "Claude TUI launcher supports only CC Switch proxy profiles with "
    "ANTHROPIC_BASE_URL and exactly one API credential"
)


def _claude_profile_parts(
    provider: SelectedProvider,
) -> tuple[dict[str, Any], dict[str, str]]:
    if provider.app_type != "claude":
        raise CcSwitchConfigError(_CLAUDE_PROFILE_ERROR)

    settings = deepcopy(provider.settings)
    provider_environment = settings.get("env")
    if not isinstance(provider_environment, dict):
        raise CcSwitchConfigError(_CLAUDE_PROFILE_ERROR)

    for key, value in provider_environment.items():
        if (
            not isinstance(key, str)
            or not key
            or "=" in key
            or "\0" in key
            or not isinstance(value, str)
            or "\0" in value
        ):
            raise CcSwitchConfigError(_CLAUDE_PROFILE_ERROR)

    base_url = provider_environment.get("ANTHROPIC_BASE_URL")
    if not isinstance(base_url, str) or not base_url.strip():
        raise CcSwitchConfigError(_CLAUDE_PROFILE_ERROR)

    credential_names = ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY")
    present_credentials = [
        name for name in credential_names if name in provider_environment
    ]
    if len(present_credentials) != 1:
        raise CcSwitchConfigError(_CLAUDE_PROFILE_ERROR)
    if not provider_environment[present_credentials[0]].strip():
        raise CcSwitchConfigError(_CLAUDE_PROFILE_ERROR)
    if "CLAUDE_CONFIG_DIR" in provider_environment:
        raise CcSwitchConfigError(_CLAUDE_PROFILE_ERROR)

    return settings, provider_environment


_SECURE_STORAGE_ENV_KEY = "CLAUDE_SECURESTORAGE_CONFIG_DIR"


@dataclass(frozen=True)
class _PreparedClaudeProfile:
    temporary_directory: tempfile.TemporaryDirectory[str]
    runtime: ClaudeProfileRuntime
    shared_home: Path
    state_store: ClaudePluginStateStore
    baseline_plugins: dict[str, Any]
    initial_snapshot: RuntimeSnapshot


def _claude_shared_home(configured: str | None) -> tuple[Path, Path]:
    """Return the shared Claude home and the global config Claude pairs with it."""
    failed = False
    home = global_config = Path()
    try:
        if configured:
            home = Path(configured).expanduser().absolute()
            global_config = home / GLOBAL_CONFIG_NAME
            failed = not home.is_dir()
        else:
            home = CLAUDE_DEFAULT_HOME
            global_config = CLAUDE_DEFAULT_GLOBAL_CONFIG
            home.mkdir(mode=0o700, exist_ok=True)
    except OSError:
        failed = True
    if failed:
        raise CcSwitchConfigError(
            "Claude config directory (CLAUDE_CONFIG_DIR or ~/.claude) "
            "must be a directory"
        )
    return home, global_config


def _read_window_plugins(settings_path: Path) -> dict[str, bool] | None:
    """Read a window's plugin switches, or None when they cannot be trusted."""
    try:
        document = json.loads(
            settings_path.read_text(encoding="utf-8"),
            parse_constant=_reject_json_constant,
        )
    except (OSError, UnicodeDecodeError, ValueError, RecursionError):
        return None
    plugins = (
        document.get("enabledPlugins", {}) if isinstance(document, dict) else None
    )
    if not isinstance(plugins, dict):
        return None
    return enabled_view(plugins)


def _prepare_claude_profile(
    provider: SelectedProvider, *, reset_plugin_state: bool = False
) -> _PreparedClaudeProfile:
    temporary_directory: tempfile.TemporaryDirectory[str] | None = None
    prepared: _PreparedClaudeProfile | None = None
    setup_error: BaseException | None = None

    try:
        profile_settings, provider_environment = _claude_profile_parts(provider)
        # provider_environment is profile_settings["env"], so the level leaves
        # both the settings file and the child environment.
        default_effort = _take_claude_default_effort(profile_settings)
        # Plugin switches live in the window's own settings so /plugin can
        # change them; flag settings would override every /plugin change.
        baseline_plugins = profile_settings.pop("enabledPlugins", {})
        if not isinstance(baseline_plugins, dict) or any(
            not isinstance(plugin_id, str) for plugin_id in baseline_plugins
        ):
            raise CcSwitchConfigError("provider enabledPlugins must be an object")
        mcp_servers = profile_settings.get("mcpServers")
        if mcp_servers is not None and (
            not isinstance(mcp_servers, dict)
            or any(not isinstance(server, dict) for server in mcp_servers.values())
        ):
            raise CcSwitchConfigError(
                "provider mcpServers must contain server objects"
            )

        configured_home = os.environ.get("CLAUDE_CONFIG_DIR")
        shared_home, global_config = _claude_shared_home(configured_home)
        environment = os.environ.copy()
        for key in tuple(environment):
            if claude_connection_env(key):
                environment.pop(key, None)
        environment.update(provider_environment)
        if _SECURE_STORAGE_ENV_KEY not in environment:
            # Keep keychain entry names: Claude Code adds no suffix for an
            # empty value and otherwise hashes the configured directory.
            environment[_SECURE_STORAGE_ENV_KEY] = configured_home or ""

        state_store = ClaudePluginStateStore(shared_home)
        if reset_plugin_state:
            state_store.reset_provider(provider.provider_id)
        effective = effective_plugins(
            baseline_plugins,
            state_store.load_provider_plugins(provider.provider_id),
        )
        try:
            with state_store.lock():
                repair_install_paths(shared_home, Path(tempfile.gettempdir()))
        except ClaudePluginStateError:
            pass  # Best effort: stale records only affect which plugins load.
        installed = read_installed_plugins(shared_home / "plugins")
        effective = installed_plugin_settings(effective, installed)

        temporary_directory = tempfile.TemporaryDirectory(prefix=OVERLAY_PREFIX)
        root = Path(temporary_directory.name)
        root.chmod(0o700)
        link_shared_entries(root, shared_home, global_config)
        user_settings = deepcopy(provider.shared_settings or {})
        user_settings.pop("mcpServers", None)
        if default_effort is not None:
            user_settings.get("env", {}).pop(_CLAUDE_EFFORT_ENV_KEY, None)
        user_settings["enabledPlugins"] = effective
        _write_private_file(root / "settings.json", _json_document(user_settings))
        if provider.shared_settings is not None:
            profile_settings = claude_provider_settings(profile_settings)
        settings_path = root / "provider-settings.json"
        _write_private_file(settings_path, _json_document(profile_settings))
        # Claude Code ignores mcpServers in --settings files, so provider MCP
        # servers travel in a separate file injected via --mcp-config.
        mcp_config_path: Path | None = None
        if mcp_servers:
            mcp_config_path = root / "mcp.json"
            _write_private_file(
                mcp_config_path, _json_document({"mcpServers": mcp_servers})
            )
        environment["CLAUDE_CONFIG_DIR"] = str(root)
        runtime = ClaudeProfileRuntime(
            provider_id=provider.provider_id,
            provider_name=provider.name,
            settings_path=settings_path,
            config_home=root,
            environment=environment,
            default_effort=default_effort,
            mcp_config_path=mcp_config_path,
        )
        prepared = _PreparedClaudeProfile(
            temporary_directory=temporary_directory,
            runtime=runtime,
            shared_home=shared_home,
            state_store=state_store,
            baseline_plugins=baseline_plugins,
            initial_snapshot=RuntimeSnapshot(
                enabled_view(effective),
                installed,
            ),
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
        if isinstance(setup_error, (ClaudePluginStateError, ClaudeOverlayError)):
            # Their messages are fixed text, so they are safe to show.
            raise CcSwitchConfigError(str(setup_error))
        raise CcSwitchConfigError(
            "failed to materialize selected Claude profile"
        )

    assert prepared is not None
    return prepared


def _synchronize_claude_plugins(prepared: _PreparedClaudeProfile) -> None:
    final_plugins = _read_window_plugins(
        prepared.runtime.config_home / "settings.json"
    )
    if final_plugins is None:
        raise CcSwitchConfigError("Claude plugin switches could not be read")
    prepared.state_store.apply_runtime_changes(
        prepared.runtime.provider_id,
        prepared.baseline_plugins,
        prepared.initial_snapshot,
        RuntimeSnapshot(
            final_plugins, read_installed_plugins(prepared.shared_home / "plugins")
        ),
    )


def _repair_claude_install_paths(prepared: _PreparedClaudeProfile) -> None:
    with prepared.state_store.lock():
        repair_install_paths(
            prepared.shared_home,
            Path(tempfile.gettempdir()),
            active_overlay=prepared.runtime.config_home,
        )


def _reclaim_claude_window(prepared: _PreparedClaudeProfile) -> None:
    reclaim_overlay_entries(prepared.runtime.config_home, prepared.shared_home)


def _finalize_step(
    step: Callable[[_PreparedClaudeProfile], None],
    prepared: _PreparedClaudeProfile,
) -> bool:
    try:
        step(prepared)
    except BaseException:
        return False
    return True


@contextmanager
def materialize_claude_profile(
    provider: SelectedProvider, *, reset_plugin_state: bool = False
) -> Iterator[ClaudeProfileRuntime]:
    """Run one Claude provider in its own config directory over shared state."""
    prepared = _prepare_claude_profile(
        provider, reset_plugin_state=reset_plugin_state
    )
    body_failed = False
    try:
        yield prepared.runtime
    except BaseException:
        body_failed = True
        raise
    finally:
        finalized = all(
            [
                _finalize_step(_synchronize_claude_plugins, prepared),
                _finalize_step(_repair_claude_install_paths, prepared),
                _finalize_step(_reclaim_claude_window, prepared),
            ]
        )
        cleanup_succeeded = _cleanup_temporary_directory(
            prepared.temporary_directory
        )
        if not body_failed and not (finalized and cleanup_succeeded):
            raise CcSwitchConfigError(
                "failed to finalize selected Claude profile"
            )


@contextmanager
def _sigterm_as_system_exit() -> Iterator[None]:
    previous_handler = signal.getsignal(signal.SIGTERM)

    def handle_sigterm(signum: int, _frame: object) -> None:
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, handle_sigterm)
    body_failed = False
    try:
        yield
    except BaseException:
        body_failed = True
        raise
    finally:
        try:
            signal.signal(signal.SIGTERM, previous_handler)
        except BaseException:
            if not body_failed:
                raise


@contextmanager
def use_provider(
    app_type: AppType,
    selector: str,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> Iterator[ProviderRuntime]:
    """Select one provider and expose its isolated runtime for one process."""
    if os.name == "nt":
        raise CcSwitchConfigError(
            "--cc-switch-config is currently supported only on macOS/Linux"
        )

    provider = load_provider(app_type, selector, db_path)
    if app_type == "claude":
        provider = apply_claude_live_config(provider)
    else:
        provider = apply_codex_live_config(provider)
    with _sigterm_as_system_exit():
        with materialize_provider(provider) as runtime:
            yield runtime


@contextmanager
def use_claude_profile(
    selector: str,
    db_path: str | Path = DEFAULT_DB_PATH,
    *,
    reset_plugin_state: bool = False,
) -> Iterator[ClaudeProfileRuntime]:
    """Select one Claude provider while preserving shared Claude state."""
    if os.name == "nt":
        raise CcSwitchConfigError(
            "--cc-switch-config is currently supported only on macOS/Linux"
        )

    provider = apply_claude_live_config(load_provider("claude", selector, db_path))
    with _sigterm_as_system_exit():
        with materialize_claude_profile(
            provider, reset_plugin_state=reset_plugin_state
        ) as runtime:
            yield runtime


def redact_text(text: str, secrets: Sequence[str]) -> str:
    """Replace all configured secrets with one deterministic marker."""
    ordered_secrets = sorted(
        {secret for secret in secrets if secret},
        key=lambda secret: (-len(secret), secret),
    )
    if not ordered_secrets:
        return text

    pattern = re.compile("|".join(re.escape(secret) for secret in ordered_secrets))
    return pattern.sub(lambda _match: "<redacted>", text)
