"""Read and validate provider settings from the CC Switch App database."""

from __future__ import annotations

import json
import os
import re
import signal
import sqlite3
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal


AppType = Literal["codex", "claude"]

DEFAULT_DB_PATH = Path.home() / ".cc-switch" / "cc-switch.db"
REQUIRED_PROVIDER_COLUMNS = frozenset(
    {"id", "app_type", "name", "settings_config"}
)


class CcSwitchConfigError(RuntimeError):
    """Raised when CC Switch provider settings cannot be safely loaded."""


@dataclass(frozen=True)
class SelectedProvider:
    provider_id: str
    app_type: AppType
    name: str
    settings: dict[str, Any] = field(repr=False)


@dataclass(frozen=True)
class ProviderRuntime:
    provider_id: str
    provider_name: str
    environment: Mapping[str, str] = field(repr=False)
    secrets: tuple[str, ...] = field(repr=False)

    def redact(self, text: str) -> str:
        return redact_text(text, self.secrets)


def _open_read_only(db_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)


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
) -> object:
    connection: sqlite3.Connection | None = None
    try:
        connection = _open_read_only(db_path)
        rows = list(
            connection.execute(
                """
                SELECT settings_config
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
        return rows[0][0]
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


def load_provider(
    app_type: AppType,
    selector: str,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> SelectedProvider:
    """Load one provider, preferring an exact ID over an exact display name."""
    if app_type not in ("codex", "claude"):
        raise CcSwitchConfigError(f"unsupported app type: {app_type!r}")

    try:
        resolved_path = Path(db_path).expanduser().resolve()
        is_file = resolved_path.is_file()
    except OSError:
        raise CcSwitchConfigError("failed to inspect CC Switch database path") from None
    if not is_file:
        raise CcSwitchConfigError(
            f"CC Switch database path is not a file: {resolved_path}"
        )

    rows = _read_provider_metadata(app_type, resolved_path)
    provider_id, name = _select_row(app_type, selector, rows)
    payload = _read_provider_payload(
        app_type, provider_id, name, resolved_path
    )
    settings = _parse_settings(app_type, provider_id, payload)
    return SelectedProvider(provider_id, app_type, name, settings)


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
            _write_private_file(
                root / "settings.json", _json_document(provider.settings)
            )
            environment_key = "CLAUDE_CONFIG_DIR"
            secret_source = provider.settings.get("env", {})
        else:
            raise ValueError("unsupported provider app type")

        environment = os.environ.copy()
        environment[environment_key] = str(root)
        runtime = ProviderRuntime(
            provider_id=provider.provider_id,
            provider_name=provider.name,
            environment=environment,
            secrets=_collect_secrets(secret_source),
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
    with _sigterm_as_system_exit():
        with materialize_provider(provider) as runtime:
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
