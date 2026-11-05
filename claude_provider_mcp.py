"""Persist MCP edits in one CC Switch Claude provider record."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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


def _servers(document: Mapping[str, Any]) -> dict[str, Any]:
    servers = document.get("mcpServers", {})
    if not isinstance(servers, dict) or any(
        not isinstance(server, dict) for server in servers.values()
    ):
        raise CcSwitchConfigError("provider mcpServers must contain server objects")
    return servers


def _edited_servers(text: str) -> dict[str, Any]:
    try:
        document = json.loads(text) if text.strip() else {}
    except json.JSONDecodeError:
        raise CcSwitchConfigError(
            "the MCP editor accepts only a JSON object of servers; changes were not saved"
        ) from None
    if not isinstance(document, dict) or any(
        not isinstance(server, dict) for server in document.values()
    ):
        raise CcSwitchConfigError(
            "the MCP editor accepts only a JSON object of servers; changes were not saved"
        )
    return document


@dataclass(frozen=True)
class ClaudeProviderMcpEditor:
    provider: SelectedProvider = field(repr=False)
    db_path: Path
    config_path: Path
    environment: Mapping[str, str] = field(repr=False)

    def save(self) -> Path | None:
        """Save only changed MCP entries, retaining concurrent unrelated edits."""
        try:
            before = _servers(self.provider.settings)
            after = _edited_servers(self.config_path.read_text(encoding="utf-8"))
            changed = {
                name
                for name in before.keys() | after.keys()
                if before.get(name, _MISSING) != after.get(name, _MISSING)
            }
            if not changed:
                return None
            return self._save_changes(before, after, changed)
        except (OSError, UnicodeError, sqlite3.Error):
            raise CcSwitchConfigError(
                "failed to save provider MCP changes; CC Switch was not updated"
            ) from None

    def _save_changes(
        self,
        before: Mapping[str, Any],
        after: Mapping[str, Any],
        changed: set[str],
    ) -> Path | None:
        connection = sqlite3.connect(f"{self.db_path.as_uri()}?mode=rw", uri=True)
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = list(
                connection.execute(
                    "SELECT name, settings_config, meta FROM providers "
                    "WHERE app_type = 'claude' AND id = ?",
                    (self.provider.provider_id,),
                )
            )
            if len(rows) != 1:
                raise CcSwitchConfigError(
                    "selected Claude provider is no longer unique; MCP changes were not saved"
                )
            name, payload, meta = rows[0]
            latest = _parse_settings("claude", self.provider.provider_id, payload)
            latest_servers = _servers(latest)
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
                updated["mcpServers"] = updated_servers
            else:
                updated.pop("mcpServers", None)

            backup = self._backup(name, payload, meta)
            cursor = connection.execute(
                "UPDATE providers SET settings_config = ? "
                "WHERE app_type = 'claude' AND id = ? AND settings_config = ?",
                (
                    json.dumps(updated, ensure_ascii=False),
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
        directory = self.db_path.parent / "backups" / "claude-tui-mcp"
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        if directory.is_symlink() or not directory.is_dir():
            raise CcSwitchConfigError("provider MCP backup directory is invalid")
        directory.chmod(0o700)
        backup = directory / f"{uuid.uuid4()}.json"
        _write_private_file(
            backup,
            json.dumps(
                {
                    "provider_id": self.provider.provider_id,
                    "app_type": "claude",
                    "name": name,
                    "settings_config": payload,
                    "meta": meta,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
        )
        return backup


@contextmanager
def edit_claude_provider_mcp(
    selector: str, *, db_path: str | Path = DEFAULT_DB_PATH
) -> Iterator[ClaudeProviderMcpEditor]:
    """Open one Claude provider's MCP servers as a private JSON document.

    The file holds only the mcpServers object; saving it back merges just the
    changed servers into the provider record. The caller saves only after the
    editor exits successfully.
    """
    if os.name == "nt":
        raise CcSwitchConfigError(
            "--cc-switch-config is currently supported only on macOS/Linux"
        )
    resolved = _resolve_database_path(db_path)
    provider = load_provider("claude", selector, resolved)
    with _sigterm_as_system_exit(), tempfile.TemporaryDirectory(
        prefix="codex-candy-eval-claude-mcp-"
    ) as temporary:
        root = Path(temporary)
        root.chmod(0o700)
        config_path = root / "mcp.json"
        _write_private_file(
            config_path,
            json.dumps(_servers(provider.settings), ensure_ascii=False, indent=2) + "\n",
        )
        yield ClaudeProviderMcpEditor(
            provider, resolved, config_path, os.environ.copy()
        )
