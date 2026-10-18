import contextlib
import io
import json
import os
import shutil
import shlex
import sqlite3
import stat
import subprocess
import sys
import tempfile
import tomllib
import unittest
from contextlib import closing
from copy import deepcopy
from pathlib import Path
from unittest import mock

import tomli_w

import codex_provider_mcp
import codex_tui
from cc_switch_config import CcSwitchConfigError, load_provider
from codex_provider_mcp import edit_provider_mcp, is_provider_mcp_edit
from codex_runtime import use_codex_runtime


CONFIG = '''# provider comment retained by native Codex
model = "gpt-test"
model_provider = "custom"

[model_providers.custom]
name = "Custom"
base_url = "https://provider.example/v1"
wire_api = "responses"
requires_openai_auth = true

[plugins."demo@test"]
enabled = false
'''


class ProviderMcpTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.db_path = self.root / "cc-switch.db"
        self.settings = {
            "config": CONFIG,
            "auth": {"OPENAI_API_KEY": "private-test-key"},
            "extra": {"preserve": True},
        }
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                "CREATE TABLE providers (id TEXT, app_type TEXT, name TEXT, "
                "settings_config TEXT, meta TEXT)"
            )
            for provider_id in ("one-id", "two-id"):
                connection.execute(
                    "INSERT INTO providers VALUES (?, 'codex', ?, ?, ?)",
                    (provider_id, provider_id[:-3], json.dumps(self.settings),
                     '{"commonConfigEnabled":true}'),
                )
            connection.execute("CREATE TABLE settings (key TEXT, value TEXT)")
            connection.execute(
                "INSERT INTO settings VALUES ('common_config_codex', ?)",
                ('[mcp_servers.shared]\ncommand = "shared-command"\n',),
            )
        self.global_home = self.root / "global-codex"
        self.global_home.mkdir()
        self.global_config = self.global_home / "config.toml"
        self.global_config.write_text('[mcp_servers.global]\ncommand = "global-command"\n')
        self.global_auth = self.global_home / "auth.json"
        self.global_auth.write_text('"global-auth-sentinel"')
        self.sessions = self.global_home / "sessions"
        self.sessions.mkdir()
        (self.sessions / "session.jsonl").write_text("session-sentinel\n")
        self.addCleanup(mock.patch.stopall)
        mock.patch.dict(os.environ, {
            "CODEX_HOME": str(self.global_home),
            "CODEX_SQLITE_HOME": str(self.global_home),
        }).start()

    def read_settings(self, selector="one"):
        return load_provider("codex", selector, self.db_path).settings

    def write_settings(self, settings):
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                "UPDATE providers SET settings_config = ? WHERE id = 'one-id'",
                (json.dumps(settings),),
            )

    def set_servers(self, editor, servers):
        document = tomllib.loads(editor.config_path.read_text())
        if servers:
            document["mcp_servers"] = servers
        else:
            document.pop("mcp_servers", None)
        editor.config_path.write_text(tomli_w.dumps(document))

    def test_add_is_persistent_and_scoped_and_backup_is_private(self):
        with edit_provider_mcp("one", db_path=self.db_path) as editor:
            runtime_home = editor.config_path.parent
            self.assertEqual(stat.S_IMODE(editor.config_path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(runtime_home.stat().st_mode), 0o700)
            self.assertNotEqual(editor.environment["CODEX_HOME"], str(self.global_home))
            self.assertEqual(editor.environment["CODEX_SQLITE_HOME"], str(runtime_home))
            self.assertFalse((runtime_home / "auth.json").exists())
            initial = tomllib.loads(editor.config_path.read_text())
            self.assertNotIn("mcp_servers", initial)  # no Common/global MCP inheritance
            self.set_servers(editor, {"dbx": {"command": "/test/dbx-mcp"}})
            backup = editor.save()
        self.assertFalse(runtime_home.exists())
        self.assertEqual(backup.parent, self.root / "backups" / "codex-tui-mcp")
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(backup.parent.stat().st_mode), 0o700)
        saved_backup = json.loads(backup.read_text())
        self.assertEqual(json.loads(saved_backup["settings_config"]), self.settings)
        saved = self.read_settings()
        document = tomllib.loads(saved["config"])
        self.assertEqual(document.pop("mcp_servers"), {"dbx": {"command": "/test/dbx-mcp"}})
        self.assertEqual(document, tomllib.loads(CONFIG))
        self.assertEqual(saved["auth"], self.settings["auth"])
        self.assertEqual(saved["extra"], self.settings["extra"])
        self.assertEqual(self.read_settings("two"), self.settings)
        self.assertEqual(self.global_config.read_text(), '[mcp_servers.global]\ncommand = "global-command"\n')
        self.assertEqual(self.global_auth.read_text(), '"global-auth-sentinel"')
        self.assertEqual((self.sessions / "session.jsonl").read_text(), "session-sentinel\n")

    def test_delete_last_mcp_removes_only_its_provider_declaration(self):
        with edit_provider_mcp("one", db_path=self.db_path) as editor:
            self.set_servers(editor, {"dbx": {"command": "dbx-mcp"}})
            editor.save()
        with edit_provider_mcp("one", db_path=self.db_path) as editor:
            self.set_servers(editor, {})
            editor.save()
        self.assertNotIn("mcp_servers", tomllib.loads(self.read_settings()["config"]))
        self.assertEqual(self.read_settings("two"), self.settings)
        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertEqual(connection.execute("SELECT value FROM settings").fetchone()[0],
                             '[mcp_servers.shared]\ncommand = "shared-command"\n')

    def test_no_change_does_not_write_or_back_up(self):
        with edit_provider_mcp("one", db_path=self.db_path) as editor:
            self.assertIsNone(editor.save())
        self.assertFalse((self.root / "backups").exists())
        self.assertEqual(self.read_settings(), self.settings)

    def test_text_editor_shows_only_mcp_and_supports_add_then_delete(self):
        with edit_provider_mcp("one", db_path=self.db_path, mcp_only=True) as editor:
            document = tomllib.loads(editor.config_path.read_text())
            self.assertEqual(document, {"mcp_servers": {}})
            self.assertNotIn("private-test-key", editor.config_path.read_text())
            editor.config_path.write_text('[mcp_servers.dbx]\ncommand = "/test/dbx-mcp"\n')
            editor.save()
        with edit_provider_mcp("one", db_path=self.db_path, mcp_only=True) as editor:
            self.assertEqual(tomllib.loads(editor.config_path.read_text()), {
                "mcp_servers": {"dbx": {"command": "/test/dbx-mcp"}},
            })
            editor.config_path.write_text("")
            editor.save()
        self.assertEqual(tomllib.loads(self.read_settings()["config"]), tomllib.loads(CONFIG))
        self.assertEqual(self.read_settings()["auth"], self.settings["auth"])
        self.assertEqual(self.read_settings("two"), self.settings)

    def test_text_editor_rejects_non_mcp_settings_without_saving(self):
        with edit_provider_mcp("one", db_path=self.db_path, mcp_only=True) as editor:
            editor.config_path.write_text('model = "wrong-editor"\n[mcp_servers.dbx]\ncommand = "dbx"\n')
            with self.assertRaisesRegex(CcSwitchConfigError, "only mcp_servers"):
                editor.save()
        self.assertEqual(self.read_settings(), self.settings)

    def test_editor_process_save_is_loaded_by_next_provider_runtime(self):
        script = self.root / "test editor.py"
        script.write_text(
            "import sys\nfrom pathlib import Path\n"
            'Path(sys.argv[1]).write_text(\'[mcp_servers.dbx]\\ncommand = "/test/dbx-mcp"\\n\')\n'
        )
        def editor_context(selector, **kwargs):
            return edit_provider_mcp(selector, db_path=self.db_path, **kwargs)
        with mock.patch.dict(os.environ, {
            "VISUAL": f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}",
        }), mock.patch.object(
            codex_tui, "edit_provider_mcp", side_effect=editor_context,
        ), contextlib.redirect_stdout(io.StringIO()):
            result = codex_tui.main(["--cc-switch-config", "one", "--edit-mcp"])
        self.assertEqual(result, 0)
        for name in ("one", "two"):
            with use_codex_runtime(name, db_path=self.db_path) as runtime:
                servers = tomllib.loads(runtime.config_path.read_text())["mcp_servers"]
                self.assertEqual("dbx" in servers, name == "one")
                self.assertIn("shared", servers)  # Common Config still applies at launch
                self.assertNotIn("global", servers)
                self.assertEqual(runtime.environment["OPENAI_API_KEY"], "private-test-key")

    def test_concurrent_model_auth_and_other_mcp_edits_are_preserved(self):
        with edit_provider_mcp("one", db_path=self.db_path) as editor:
            self.set_servers(editor, {"dbx": {"command": "dbx-mcp"}})
            concurrent = deepcopy(self.settings)
            document = tomllib.loads(CONFIG)
            document["model"] = "concurrent-model"
            document["mcp_servers"] = {"other": {"command": "other-mcp"}}
            concurrent["config"] = tomli_w.dumps(document)
            concurrent["auth"] = {"OPENAI_API_KEY": "concurrent-key"}
            self.write_settings(concurrent)
            backup = editor.save()
        saved = self.read_settings()
        document = tomllib.loads(saved["config"])
        self.assertEqual(document["model"], "concurrent-model")
        self.assertEqual(saved["auth"], concurrent["auth"])
        self.assertEqual(document["mcp_servers"], {
            "dbx": {"command": "dbx-mcp"}, "other": {"command": "other-mcp"},
        })
        self.assertEqual(json.loads(json.loads(backup.read_text())["settings_config"]), concurrent)

    def test_same_server_concurrent_change_is_rejected(self):
        with edit_provider_mcp("one", db_path=self.db_path) as first, edit_provider_mcp(
            "one", db_path=self.db_path
        ) as second:
            self.set_servers(first, {"dbx": {"command": "first-mcp"}})
            self.set_servers(second, {"dbx": {"command": "second-mcp"}})
            first.save()
            with self.assertRaisesRegex(CcSwitchConfigError, "concurrently"):
                second.save()
        self.assertEqual(tomllib.loads(self.read_settings()["config"])["mcp_servers"],
                         {"dbx": {"command": "first-mcp"}})
        self.assertEqual(len(list((self.root / "backups" / "codex-tui-mcp").iterdir())), 1)

    def test_identical_concurrent_change_needs_no_second_write(self):
        with edit_provider_mcp("one", db_path=self.db_path) as first, edit_provider_mcp(
            "one", db_path=self.db_path
        ) as second:
            self.set_servers(first, {"dbx": {"command": "same-mcp"}})
            self.set_servers(second, {"dbx": {"command": "same-mcp"}})
            first.save()
            self.assertIsNone(second.save())

    def test_unrelated_native_changes_are_not_saved(self):
        with edit_provider_mcp("one", db_path=self.db_path) as editor:
            document = tomllib.loads(CONFIG)
            document["model"] = "do-not-persist"
            document["mcp_servers"] = {"dbx": {"command": "dbx-mcp"}}
            editor.config_path.write_text(tomli_w.dumps(document))
            editor.save()
        self.assertEqual(tomllib.loads(self.read_settings()["config"])["model"], "gpt-test")

    def test_malformed_final_config_is_rejected_without_saving(self):
        with edit_provider_mcp("one", db_path=self.db_path) as editor:
            editor.config_path.write_text('private-test-key = [')
            with self.assertRaises(CcSwitchConfigError) as raised:
                editor.save()
        self.assertNotIn("private-test-key", str(raised.exception))
        self.assertEqual(self.read_settings(), self.settings)

    def test_backup_failure_prevents_database_update(self):
        with edit_provider_mcp("one", db_path=self.db_path) as editor:
            self.set_servers(editor, {"dbx": {"command": "dbx-mcp"}})
            with mock.patch.object(codex_provider_mcp, "_write_private_file",
                                   side_effect=OSError("private-test-key")):
                with self.assertRaises(CcSwitchConfigError) as raised:
                    editor.save()
        self.assertNotIn("private-test-key", str(raised.exception))
        self.assertEqual(self.read_settings(), self.settings)

    def test_deleted_provider_is_not_recreated(self):
        with edit_provider_mcp("one", db_path=self.db_path) as editor:
            self.set_servers(editor, {"dbx": {"command": "dbx-mcp"}})
            with closing(sqlite3.connect(self.db_path)) as connection, connection:
                connection.execute("DELETE FROM providers WHERE id = 'one-id'")
            with self.assertRaisesRegex(CcSwitchConfigError, "no longer unique"):
                editor.save()
        self.assertEqual(self.read_settings("two"), self.settings)

    @unittest.skipUnless(shutil.which("codex"), "native Codex CLI is not installed")
    def test_real_native_add_and_remove_preserve_provider_and_comments(self):
        for command in (["mcp", "add", "dbx", "--", "/test/dbx-mcp"],
                        ["mcp", "remove", "dbx"]):
            with edit_provider_mcp("one", db_path=self.db_path) as editor:
                completed = subprocess.run(
                    [shutil.which("codex"), *command], env=editor.environment,
                    capture_output=True, text=True, timeout=15,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                editor.save()
            saved_config = self.read_settings()["config"]
            self.assertIn("# provider comment retained by native Codex", saved_config)
            self.assertEqual(self.read_settings()["auth"], self.settings["auth"])
            self.assertEqual(self.read_settings("two"), self.settings)
        self.assertNotIn("mcp_servers", tomllib.loads(saved_config))


class CommandTests(unittest.TestCase):
    def test_edit_is_recognized_without_scanning_server_command_payload(self):
        self.assertTrue(is_provider_mcp_edit(["mcp", "add", "dbx", "--", "dbx-mcp"]))
        self.assertTrue(is_provider_mcp_edit(["mcp", "remove", "dbx"]))
        for arguments in ([], ["mcp"], ["mcp", "list"], ["mcp", "get", "dbx"],
                          ["exec", "mcp add dbx"], ["resume", "mcp", "add"]):
            self.assertFalse(is_provider_mcp_edit(arguments))


if __name__ == "__main__":
    unittest.main()
