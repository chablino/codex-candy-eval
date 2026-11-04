"""Read and validate provider settings from the CC Switch App database."""

from __future__ import annotations

import json
import os
import re
import signal
import sqlite3
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from copy import deepcopy
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal


AppType = Literal["codex", "claude"]

DEFAULT_DB_PATH = Path.home() / ".cc-switch" / "cc-switch.db"
CLAUDE_LIVE_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
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
    environment: Mapping[str, str] = field(repr=False)
    default_effort: str | None = None


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
    enabled = meta.get("commonConfigEnabled")
    if enabled is not None and not isinstance(enabled, bool):
        app_label = "Codex" if app_type == "codex" else "Claude"
        raise CcSwitchConfigError(
            f"{app_label} provider {provider_id!r} metadata "
            "commonConfigEnabled must be boolean"
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


def load_common_config(
    app_type: AppType, db_path: str | Path = DEFAULT_DB_PATH
) -> str:
    """Read the selected app's Common Config without mutating CC Switch."""
    if app_type not in ("codex", "claude"):
        raise CcSwitchConfigError(
            f"unsupported Common Config app type: {app_type!r}"
        )

    app_label = "Codex" if app_type == "codex" else "Claude"
    resolved_path = _resolve_database_path(db_path)
    connection: sqlite3.Connection | None = None
    rows: list[tuple[object, ...]] = []
    read_failed = False
    try:
        connection = _open_read_only(resolved_path)
        rows = list(
            connection.execute(
                "SELECT value FROM settings WHERE key = ?",
                (f"common_config_{app_type}",),
            )
        )
    except sqlite3.Error:
        read_failed = True
    finally:
        if connection is not None:
            connection.close()

    if read_failed:
        raise CcSwitchConfigError(f"failed to read {app_label} Common Config")
    if len(rows) != 1 or not isinstance(rows[0][0], str):
        raise CcSwitchConfigError(f"{app_label} Common Config is missing or invalid")
    return rows[0][0]


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


def apply_claude_common_config(
    provider: SelectedProvider, db_path: str | Path = DEFAULT_DB_PATH
) -> SelectedProvider:
    """Overlay Claude Common Config when the provider enables it in CC Switch.

    Like the CC Switch App, the provider is the base and Common Config is
    deep-merged over it, so Common Config wins on conflicting values.
    """
    if provider.meta.get("commonConfigEnabled") is not True:
        return provider
    common = _parse_claude_settings(
        load_common_config("claude", db_path), "Claude Common Config"
    )
    return replace(
        provider, settings=_json_deep_merge(provider.settings, common)
    )


_CLAUDE_EFFORT_ENV_KEY = "CLAUDE_CODE_EFFORT_LEVEL"
_CLAUDE_LAUNCH_EFFORTS = ("low", "medium", "high", "xhigh", "max")


def _load_claude_live_effort() -> str | None:
    payload = ""
    read_failed = False
    try:
        payload = CLAUDE_LIVE_SETTINGS_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        pass
    except (OSError, UnicodeDecodeError):
        read_failed = True

    if read_failed:
        raise CcSwitchConfigError("failed to read Claude settings file")
    settings = _parse_claude_settings(payload, "Claude settings file")
    value = settings.get("env", {}).get(_CLAUDE_EFFORT_ENV_KEY)
    return value if isinstance(value, str) and value.strip() else None


def apply_claude_live_effort(provider: SelectedProvider) -> SelectedProvider:
    """Prefer the effort level in Claude's live settings file.

    CC Switch treats CLAUDE_CODE_EFFORT_LEVEL as a global setting: its editor
    writes the value to the live file and leaves any copy in the provider row
    as it was, so the row and Common Config are only fallbacks.
    """
    value = _load_claude_live_effort()
    if value is None:
        return provider
    return replace(
        provider,
        settings=_json_deep_merge(
            provider.settings, {"env": {_CLAUDE_EFFORT_ENV_KEY: value}}
        ),
    )


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
_CLAUDE_PARENT_AUTH_KEYS = (
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
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


def _prepare_claude_profile(
    provider: SelectedProvider,
) -> tuple[tempfile.TemporaryDirectory[str], ClaudeProfileRuntime]:
    temporary_directory: tempfile.TemporaryDirectory[str] | None = None
    runtime: ClaudeProfileRuntime | None = None
    setup_error: BaseException | None = None

    try:
        profile_settings, provider_environment = _claude_profile_parts(provider)
        # provider_environment is profile_settings["env"], so the level leaves
        # both the settings file and the child environment.
        default_effort = _take_claude_default_effort(profile_settings)
        environment = os.environ.copy()
        for key in _CLAUDE_PARENT_AUTH_KEYS:
            environment.pop(key, None)
        environment.update(provider_environment)

        temporary_directory = tempfile.TemporaryDirectory(
            prefix="codex-candy-eval-claude-"
        )
        root = Path(temporary_directory.name)
        root.chmod(0o700)
        settings_path = root / "settings.json"
        _write_private_file(settings_path, _json_document(profile_settings))
        runtime = ClaudeProfileRuntime(
            provider_id=provider.provider_id,
            provider_name=provider.name,
            settings_path=settings_path,
            environment=environment,
            default_effort=default_effort,
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
            "failed to materialize selected Claude profile"
        )

    assert temporary_directory is not None
    assert runtime is not None
    return temporary_directory, runtime


@contextmanager
def materialize_claude_profile(
    provider: SelectedProvider,
) -> Iterator[ClaudeProfileRuntime]:
    """Overlay one Claude provider without replacing shared Claude state."""
    temporary_directory, runtime = _prepare_claude_profile(provider)
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
                "failed to clean up selected Claude profile"
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
        provider = apply_claude_live_effort(
            apply_claude_common_config(provider, db_path)
        )
    with _sigterm_as_system_exit():
        with materialize_provider(provider) as runtime:
            yield runtime


@contextmanager
def use_claude_profile(
    selector: str,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> Iterator[ClaudeProfileRuntime]:
    """Select one Claude provider while preserving shared Claude state."""
    if os.name == "nt":
        raise CcSwitchConfigError(
            "--cc-switch-config is currently supported only on macOS/Linux"
        )

    provider = apply_claude_live_effort(
        apply_claude_common_config(
            load_provider("claude", selector, db_path), db_path
        )
    )
    with _sigterm_as_system_exit():
        with materialize_claude_profile(provider) as runtime:
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
