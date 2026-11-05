import contextlib
import io
import json
import sqlite3
import stat
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import claude_tui
from cc_switch_config import CcSwitchConfigError, load_provider
from claude_provider_mcp import edit_claude_provider_mcp

SETTINGS = {
    "env": {
        "ANTHROPIC_BASE_URL": "https://provider.example/v1",
        "ANTHROPIC_AUTH_TOKEN": "claude-test-secret",
    },
    "model": "sonnet",
    "theme": "light",
}


class ClaudeProviderMcpTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.db_path = self.root / "cc-switch.db"
        with contextlib.closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                "CREATE TABLE providers (id TEXT, app_type TEXT, name TEXT, "
                "settings_config TEXT, meta TEXT)"
            )
            for provider_id in ("one-id", "two-id"):
                connection.execute(
                    "INSERT INTO providers VALUES (?, 'claude', ?, ?, ?)",
                    (provider_id, provider_id[:-3], json.dumps(SETTINGS),
                     '{"commonConfigEnabled":false}'),
                )

    def read_settings(self, selector="one"):
        return load_provider("claude", selector, self.db_path).settings

    def write_settings(self, settings):
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                "UPDATE providers SET settings_config = ? WHERE id = 'one-id'",
                (json.dumps(settings),),
            )

    def set_servers(self, editor, servers):
        editor.config_path.write_text(json.dumps(servers), encoding="utf-8")

    def test_add_is_persistent_and_scoped_and_backup_is_private(self):
        with edit_claude_provider_mcp("one", db_path=self.db_path) as editor:
            self.assertEqual(stat.S_IMODE(editor.config_path.stat().st_mode), 0o600)
            self.assertEqual(
                stat.S_IMODE(editor.config_path.parent.stat().st_mode), 0o700
            )
            initial = json.loads(editor.config_path.read_text(encoding="utf-8"))
            self.assertEqual(initial, {})
            self.set_servers(editor, {"dbx": {"command": "/test/dbx-mcp"}})
            backup = editor.save()

        saved = self.read_settings()
        self.assertEqual(
            saved.pop("mcpServers"), {"dbx": {"command": "/test/dbx-mcp"}}
        )
        self.assertEqual(saved, SETTINGS)
        self.assertEqual(self.read_settings("two"), SETTINGS)
        self.assertEqual(backup.parent, self.root / "backups" / "claude-tui-mcp")
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(backup.parent.stat().st_mode), 0o700)
        saved_backup = json.loads(backup.read_text(encoding="utf-8"))
        self.assertEqual(saved_backup["app_type"], "claude")
        self.assertEqual(json.loads(saved_backup["settings_config"]), SETTINGS)

    def test_delete_last_mcp_removes_the_key(self):
        with edit_claude_provider_mcp("one", db_path=self.db_path) as editor:
            self.set_servers(editor, {"dbx": {"command": "/test/dbx-mcp"}})
            editor.save()
        with edit_claude_provider_mcp("one", db_path=self.db_path) as editor:
            self.assertEqual(
                json.loads(editor.config_path.read_text(encoding="utf-8")),
                {"dbx": {"command": "/test/dbx-mcp"}},
            )
            self.set_servers(editor, {})
            editor.save()
        self.assertNotIn("mcpServers", self.read_settings())

    def test_no_change_does_not_write_or_back_up(self):
        with edit_claude_provider_mcp("one", db_path=self.db_path) as editor:
            self.assertIsNone(editor.save())
        self.assertFalse((self.root / "backups").exists())
        self.assertEqual(self.read_settings(), SETTINGS)

    def test_invalid_document_is_rejected_without_writing(self):
        for text in ("not json", '["dbx"]', '{"dbx": "/test/dbx-mcp"}'):
            with self.subTest(text=text):
                with edit_claude_provider_mcp("one", db_path=self.db_path) as editor:
                    editor.config_path.write_text(text, encoding="utf-8")
                    with self.assertRaisesRegex(CcSwitchConfigError, "MCP|mcpServers"):
                        editor.save()
                self.assertEqual(self.read_settings(), SETTINGS)

    def test_concurrent_same_server_edit_is_rejected(self):
        with edit_claude_provider_mcp("one", db_path=self.db_path) as editor:
            self.set_servers(editor, {"dbx": {"command": "/test/dbx-mcp"}})
            editor.save()
        with edit_claude_provider_mcp("one", db_path=self.db_path) as editor:
            self.set_servers(editor, {"dbx": {"command": "/test/other"}})
            updated = self.read_settings()
            updated["mcpServers"]["dbx"] = {"command": "/test/concurrent"}
            self.write_settings(updated)
            with self.assertRaisesRegex(CcSwitchConfigError, "concurrently"):
                editor.save()
        self.assertEqual(
            self.read_settings()["mcpServers"], {"dbx": {"command": "/test/concurrent"}}
        )

    def test_concurrent_unrelated_edit_is_retained(self):
        with edit_claude_provider_mcp("one", db_path=self.db_path) as editor:
            self.set_servers(editor, {"dbx": {"command": "/test/dbx-mcp"}})
            updated = self.read_settings()
            updated["model"] = "opus"
            self.write_settings(updated)
            editor.save()
        saved = self.read_settings()
        self.assertEqual(saved["model"], "opus")
        self.assertEqual(saved["mcpServers"], {"dbx": {"command": "/test/dbx-mcp"}})


class EditMcpFlagTests(unittest.TestCase):
    def test_edit_mcp_rejects_forwarded_arguments(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                claude_tui.parse_args(
                    ["--cc-switch-config", "anyrouter", "--edit-mcp", "--", "--resume"]
                )
        self.assertEqual(raised.exception.code, 2)

    def test_editor_runs_and_saves_only_after_success(self):
        for editor_code in (0, 1):
            with self.subTest(editor_code=editor_code):
                editor = SimpleNamespace(
                    config_path=Path("/private/tmp/claude-mcp/mcp.json"),
                    environment={"HOME": "/private/tmp"},
                    provider=SimpleNamespace(name="anyrouter"),
                    save=mock.Mock(return_value="/test/backup.json"),
                )
                manager = mock.MagicMock()
                manager.__enter__.return_value = editor
                manager.__exit__.return_value = False
                process = mock.Mock(returncode=editor_code)
                with mock.patch.dict(
                    claude_tui.os.environ, {"VISUAL": "test-editor --wait"}
                ), mock.patch.object(
                    claude_tui, "edit_claude_provider_mcp", return_value=manager
                ) as edit, mock.patch.object(
                    claude_tui.subprocess, "run", return_value=process
                ) as run, contextlib.redirect_stdout(io.StringIO()):
                    result = claude_tui.main(
                        ["--cc-switch-config", "anyrouter", "--edit-mcp"]
                    )
                self.assertEqual(result, editor_code)
                edit.assert_called_once_with("anyrouter")
                run.assert_called_once_with(
                    ["test-editor", "--wait", str(editor.config_path)],
                    env=editor.environment,
                )
                if editor_code == 0:
                    editor.save.assert_called_once_with()
                else:
                    editor.save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
