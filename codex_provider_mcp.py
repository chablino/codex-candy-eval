"""Persist explicit native MCP edits in one CC Switch provider record."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import tomllib
import uuid
from collections.abc import Iterator, Mapping, Sequence
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
    _parse_settings,
    _resolve_database_path,
    _sigterm_as_system_exit,
    _write_private_file,
    load_provider,
)


_MISSING = object()


def is_provider_mcp_edit(arguments: Sequence[str]) -> bool:
    """Recognize the supported native command spelling, before its payload."""
    return len(arguments) >= 2 and arguments[0] == "mcp" and arguments[1] in {
        "add", "remove"
    }


def _document(text: str) -> dict[str, Any]:
    try:
        document = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, RecursionError):
        raise CcSwitchConfigError("provider MCP configuration is invalid") from None
    servers = document.get("mcp_servers", {})
    if not isinstance(servers, dict) or any(
        not isinstance(server, dict) for server in servers.values()
    ):
        raise CcSwitchConfigError("provider mcp_servers must contain server tables")
    return document


@dataclass(frozen=True)
class ProviderMcpEditor:
    provider: SelectedProvider = field(repr=False)
    db_path: Path
    config_path: Path
    environment: Mapping[str, str] = field(repr=False)
    mcp_only: bool = False

    def save(self) -> Path | None:
        """Save only changed MCP entries, retaining concurrent unrelated edits."""
        try:
            final_text = self.config_path.read_text(encoding="utf-8")
            initial = _document(self.provider.settings["config"])
            final = _document(final_text)
            if self.mcp_only and set(final) - {"mcp_servers"}:
                raise CcSwitchConfigError(
                    "the MCP editor accepts only mcp_servers tables; changes were not saved"
                )
            before = initial.get("mcp_servers", {})
            after = final.get("mcp_servers", {})
            changed = {
                name for name in before.keys() | after.keys()
                if before.get(name, _MISSING) != after.get(name, _MISSING)
            }
            if not changed:
                return None
            return self._save_changes(initial, final, final_text, changed)
        except (OSError, UnicodeError, sqlite3.Error):
            raise CcSwitchConfigError(
                "failed to save provider MCP changes; CC Switch was not updated"
            ) from None

    def _save_changes(
        self,
        initial: dict[str, Any],
        final: dict[str, Any],
        final_text: str,
        changed: set[str],
    ) -> Path | None:
        connection = sqlite3.connect(f"{self.db_path.as_uri()}?mode=rw", uri=True)
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = list(connection.execute(
                "SELECT name, settings_config, meta FROM providers "
                "WHERE app_type = 'codex' AND id = ?",
                (self.provider.provider_id,),
            ))
            if len(rows) != 1:
                raise CcSwitchConfigError(
                    "selected Codex provider is no longer unique; MCP changes were not saved"
                )
            name, payload, meta = rows[0]
            settings = _parse_settings("codex", self.provider.provider_id, payload)
            latest = _document(settings["config"])
            latest_servers = latest.get("mcp_servers", {})
            before = initial.get("mcp_servers", {})
            after = final.get("mcp_servers", {})
            for server in changed:
                current = latest_servers.get(server, _MISSING)
                if current != before.get(server, _MISSING) and current != after.get(
                    server, _MISSING
                ):
                    raise CcSwitchConfigError(
                        "the same provider MCP was changed concurrently; "
                        "changes were not saved, please retry"
                    )
            updated_servers = deepcopy(latest_servers)
            for server in changed:
                if server in after:
                    updated_servers[server] = after[server]
                else:
                    updated_servers.pop(server, None)
            if updated_servers == latest_servers:
                return None
            updated = deepcopy(latest)
            if updated_servers:
                updated["mcp_servers"] = updated_servers
            else:
                updated.pop("mcp_servers", None)

            # Native Codex preserves TOML comments and layout. Keep its output
            # when there are no concurrent or unrelated configuration changes.
            settings["config"] = (
                final_text
                if settings["config"] == self.provider.settings["config"]
                and final == updated
                else tomli_w.dumps(updated)
            )
            backup = self._backup(name, payload, meta)
            cursor = connection.execute(
                "UPDATE providers SET settings_config = ? "
                "WHERE app_type = 'codex' AND id = ? AND settings_config = ?",
                (
                    json.dumps(settings, ensure_ascii=False),
                    self.provider.provider_id,
                    payload,
                ),
            )
            if cursor.rowcount != 1:
                raise CcSwitchConfigError("provider MCP changes could not be saved uniquely")
            connection.commit()
            return backup
        finally:
            connection.close()

    def _backup(self, name: str, payload: str, meta: str) -> Path:
        directory = self.db_path.parent / "backups" / "codex-tui-mcp"
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        if directory.is_symlink() or not directory.is_dir():
            raise CcSwitchConfigError("provider MCP backup directory is invalid")
        directory.chmod(0o700)
        backup = directory / f"{uuid.uuid4()}.json"
        _write_private_file(backup, json.dumps({
            "provider_id": self.provider.provider_id,
            "app_type": "codex",
            "name": name,
            "settings_config": payload,
            "meta": meta,
        }, ensure_ascii=False, indent=2) + "\n")
        return backup


@contextmanager
def edit_provider_mcp(
    selector: str, *, db_path: str | Path = DEFAULT_DB_PATH, mcp_only: bool = False,
) -> Iterator[ProviderMcpEditor]:
    """Give native Codex a private provider config for an explicit MCP edit.

    The caller saves only after the native command succeeds. Common Config,
    global config, plugin sidecars, auth and session state are not copied here.
    """
    if os.name == "nt":
        raise CcSwitchConfigError(
            "--cc-switch-config is currently supported only on macOS/Linux"
        )
    resolved = _resolve_database_path(db_path)
    provider = load_provider("codex", selector, resolved)
    initial = _document(provider.settings["config"])
    with _sigterm_as_system_exit(), tempfile.TemporaryDirectory(
        prefix="codex-candy-eval-mcp-"
    ) as temporary:
        home = Path(temporary)
        home.chmod(0o700)
        config_path = home / "config.toml"
        content = provider.settings["config"]
        if mcp_only:
            content = (
                "# 这里只编辑当前 provider 的 MCP。添加表格以新增，删除表格以移除。\n"
                "# 保存并退出编辑器后生效；留空表示删除这份 provider 的所有 MCP。\n"
                "# 例如：\n# [mcp_servers.dbx]\n"
                '# command = "/absolute/path/to/dbx-mcp"\n\n'
                + tomli_w.dumps({"mcp_servers": initial.get("mcp_servers", {})})
            )
        _write_private_file(config_path, content)
        environment = os.environ.copy()
        environment["CODEX_HOME"] = str(home)
        environment["CODEX_SQLITE_HOME"] = str(home)
        yield ProviderMcpEditor(provider, resolved, config_path, environment, mcp_only)
