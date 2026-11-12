"""Regression coverage for live settings, provider isolation and frozen snippets."""

import json
import os
import sqlite3
import tomllib
import unittest
from copy import deepcopy
from pathlib import Path
from unittest import mock

import tomli_w

import cc_switch_config
from cc_switch_config import CcSwitchConfigError, use_claude_profile, use_provider
from codex_runtime import use_codex_runtime
from tests.test_cc_switch_config import ProviderDatabase, CLAUDE_PROFILE_SETTINGS, CODEX_SETTINGS


class LiveConfigTests(unittest.TestCase):
    def setUp(self):
        self.db = ProviderDatabase()
        self.addCleanup(self.db.close)
        self.db.create_schema()
        self.root = self.db.path.parent
        self.claude_home = self.root / "claude"
        self.codex_home = self.root / "codex"
        self.claude_home.mkdir()
        self.codex_home.mkdir()
        self.claude_path = self.claude_home / "settings.json"
        self.codex_path = self.codex_home / "config.toml"
        patcher = mock.patch.dict(os.environ, {
            "CLAUDE_CONFIG_DIR": str(self.claude_home),
            "CODEX_HOME": str(self.codex_home),
        }, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def claude(self, settings=None, flag=True):
        self.db.insert("claude-id", "claude", "selected", settings or CLAUDE_PROFILE_SETTINGS,
                       {"commonConfigEnabled": flag})

    def codex(self, settings=None, flag=True):
        self.db.insert("codex-id", "codex", "selected", settings or CODEX_SETTINGS,
                       {"commonConfigEnabled": flag})

    def write_claude(self, document):
        self.claude_path.write_text(json.dumps(document))

    def install_claude_plugins(self, *plugin_ids):
        root = self.claude_home / "plugins"
        root.mkdir(exist_ok=True)
        (root / "installed_plugins.json").write_text(json.dumps({
            "version": 2,
            "plugins": {plugin_id: [{"scope": "user"}] for plugin_id in plugin_ids},
        }))

    def test_claude_reads_live_globals_but_uses_only_selected_credentials_and_model(self):
        self.install_claude_plugins("superpowers@market", "design@market")
        row = deepcopy(CLAUDE_PROFILE_SETTINGS)
        row["enabledPlugins"] = {"superpowers@market": True, "old@market": True}
        row["hooks"] = {"old": "stale"}
        row["env"]["CLAUDE_CODE_EFFORT_LEVEL"] = "low"
        row["env"]["CLAUDE_CODE_DISABLE_ARTIFACT"] = "1"
        self.claude(row)
        self.db.set_setting("common_config_claude", json.dumps({
            "enabledPlugins": {"superpowers@market": True}, "model": "obsolete",
            "env": {"DISABLE_AUTOUPDATER": "0"},
        }))
        live = {
            "env": {"DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_EFFORT_LEVEL": "xhigh",
                    "ANTHROPIC_AUTH_TOKEN": "wrong-secret", "ANTHROPIC_BASE_URL": "wrong-url",
                    "ANTHROPIC_DEFAULT_OPUS_MODEL": "wrong-model", "AWS_SECRET_ACCESS_KEY": "wrong-secret",
                    "CLAUDE_CODE_OAUTH_TOKEN": "wrong-secret", "CLAUDE_CODE_USE_BEDROCK": "1",
                    "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "123"},
            "model": "wrong-model", "apiKeyHelper": "wrong-helper",
            "enabledPlugins": {"superpowers@market": False, "design@market": True},
            "hooks": {"SessionStart": []}, "permissions": {"deny": ["Bash(rm *)"]},
            "statusLine": {"type": "command", "command": "status"},
        }
        self.write_claude(live)
        original_db = self.db.path.read_bytes()
        original_live = self.claude_path.read_bytes()
        with use_claude_profile("selected", self.db.path) as runtime:
            user = json.loads((runtime.config_home / "settings.json").read_text())
            selected = json.loads(runtime.settings_path.read_text())
            self.assertEqual(selected["model"], row["model"])
            self.assertEqual(selected["env"]["ANTHROPIC_AUTH_TOKEN"], "claude-profile-secret")
            self.assertEqual(selected["env"]["CLAUDE_CODE_DISABLE_ARTIFACT"], "1")
            self.assertEqual(user["enabledPlugins"], live["enabledPlugins"])
            for key in ("hooks", "permissions", "statusLine"):
                self.assertEqual(user[key], live[key])
                self.assertNotIn(key, selected)  # Remains in the user settings layer.
            self.assertNotIn("wrong-", json.dumps(user) + json.dumps(selected))
            self.assertNotIn("AWS_SECRET_ACCESS_KEY", runtime.environment)
            self.assertNotIn("CLAUDE_CODE_USE_BEDROCK", runtime.environment)
            self.assertNotIn("CLAUDE_CODE_MAX_CONTEXT_TOKENS", runtime.environment)
            self.assertEqual(runtime.environment["DISABLE_AUTOUPDATER"], "1")
            self.assertEqual(runtime.default_effort, "xhigh")
        self.assertEqual(self.db.path.read_bytes(), original_db)
        self.assertEqual(self.claude_path.read_bytes(), original_live)

    def test_claude_next_launch_reads_global_changes_and_deleted_plugins_stay_deleted(self):
        self.install_claude_plugins("new@market")
        self.claude()
        self.write_claude({"enabledPlugins": {"new@market": True}})
        with use_claude_profile("selected", self.db.path) as runtime:
            self.assertEqual(json.loads((runtime.config_home / "settings.json").read_text())[
                "enabledPlugins"], {"new@market": True})
        self.write_claude({})
        with use_claude_profile("selected", self.db.path) as runtime:
            self.assertEqual(json.loads((runtime.config_home / "settings.json").read_text())[
                "enabledPlugins"], {})

    def test_claude_global_mcp_is_linked_and_provider_server_replaces_whole_definition(self):
        row = deepcopy(CLAUDE_PROFILE_SETTINGS)
        row["mcpServers"] = {"same": {"command": "provider-mcp"}}
        self.claude(row)
        self.write_claude({"mcpServers": {
            "same": {"url": "https://global", "headers": {"Authorization": "global-secret"}},
            "global": {"command": "global-mcp"},
        }})
        global_config = self.claude_home / ".claude.json"
        global_config.write_text('{"mcpServers":{"native":{"command":"native-mcp"}}}')
        with use_claude_profile("selected", self.db.path) as runtime:
            servers = json.loads(runtime.mcp_config_path.read_text())["mcpServers"]
            self.assertEqual(servers["same"], {"command": "provider-mcp"})
            self.assertIn("global", servers)
            self.assertEqual((runtime.config_home / ".claude.json").resolve(), global_config.resolve())

    def test_claude_live_plugin_baseline_still_saves_per_provider_changes(self):
        self.install_claude_plugins("design@market")
        self.claude()
        self.db.insert("other", "claude", "other", CLAUDE_PROFILE_SETTINGS)
        self.write_claude({"enabledPlugins": {"design@market": True}})
        with use_claude_profile("selected", self.db.path) as runtime:
            path = runtime.config_home / "settings.json"
            document = json.loads(path.read_text())
            document["enabledPlugins"]["design@market"] = False
            path.write_text(json.dumps(document))
        for provider, expected in (("selected", False), ("other", True)):
            with use_claude_profile(provider, self.db.path) as runtime:
                self.assertIs(json.loads((runtime.config_home / "settings.json").read_text())[
                    "enabledPlugins"]["design@market"], expected)

    def test_claude_default_path_and_custom_home_are_respected(self):
        self.claude()
        fallback = self.root / "default-settings.json"
        fallback.write_text('{"env":{"SHARED":"default"}}')
        self.write_claude({"env": {"SHARED": "custom"}})
        with mock.patch.object(cc_switch_config, "CLAUDE_LIVE_SETTINGS_PATH", fallback):
            selected = cc_switch_config.load_provider("claude", "selected", self.db.path)
            self.assertEqual(cc_switch_config.apply_claude_live_config(selected).settings[
                "env"]["SHARED"], "custom")
            os.environ.pop("CLAUDE_CONFIG_DIR")
            self.assertEqual(cc_switch_config.apply_claude_live_config(selected).settings[
                "env"]["SHARED"], "default")

    def test_claude_eval_uses_same_live_settings_and_effort(self):
        self.claude()
        self.write_claude({"env": {"CLAUDE_CODE_EFFORT_LEVEL": "max", "DISABLE_AUTOUPDATER": "1"},
                           "enabledPlugins": {"superpowers@market": False}})
        with use_provider("claude", "selected", self.db.path) as runtime:
            settings = json.loads((Path(runtime.environment["CLAUDE_CONFIG_DIR"]) / "settings.json").read_text())
            self.assertFalse(settings["enabledPlugins"]["superpowers@market"])
            self.assertEqual(runtime.default_effort, "max")
            self.assertEqual(runtime.environment["DISABLE_AUTOUPDATER"], "1")

    def test_claude_missing_live_file_does_not_resurrect_old_globals(self):
        self.claude()
        with use_claude_profile("selected", self.db.path) as runtime:
            self.assertEqual(json.loads((runtime.config_home / "settings.json").read_text()),
                             {"enabledPlugins": {}})
            self.assertIsNone(runtime.default_effort)

    def test_claude_corrupt_install_record_does_not_restore_stale_plugins(self):
        self.claude()
        self.write_claude({"enabledPlugins": {"removed@market": True}})
        root = self.claude_home / "plugins"
        root.mkdir()
        (root / "installed_plugins.json").write_text("broken private-value")
        original = self.claude_path.read_bytes()
        with self.assertRaisesRegex(CcSwitchConfigError, "installation record") as raised:
            with use_claude_profile("selected", self.db.path):
                self.fail("invalid installation state must not launch")
        self.assertNotIn("private-value", str(raised.exception))
        self.assertEqual(self.claude_path.read_bytes(), original)

    def test_claude_ignores_missing_malformed_snippets_and_obsolete_flags(self):
        for flag in (True, False, "obsolete", None):
            for snippet in (None, "secret-invalid-json", '{"enabledPlugins":{"old@market":true}}'):
                with self.subTest(flag=flag, snippet=snippet):
                    with sqlite3.connect(self.db.path) as connection:
                        connection.execute("delete from providers")
                    self.claude(flag=flag)
                    if snippet is not None:
                        self.db.set_setting("common_config_claude", snippet)
                    with use_claude_profile("selected", self.db.path) as runtime:
                        self.assertEqual(json.loads((runtime.config_home / "settings.json").read_text())[
                            "enabledPlugins"], {})

    def test_claude_invalid_live_settings_fail_before_launch_without_secrets(self):
        self.claude()
        for text in ('["private-value"]', '{"env":"private-value"}', 'private-value',
                     '{"enabledPlugins":[]}'):
            self.claude_path.write_text(text)
            with self.subTest(text=text), self.assertRaises(CcSwitchConfigError) as raised:
                with use_claude_profile("selected", self.db.path):
                    self.fail("invalid configuration must not launch")
            self.assertNotIn("private-value", str(raised.exception))

    def test_unreadable_live_files_fail_without_secret_content(self):
        self.claude()
        self.codex()
        for path, launch in (
            (self.claude_path, lambda: use_claude_profile("selected", self.db.path)),
            (self.codex_path, lambda: use_codex_runtime("selected", db_path=self.db.path)),
        ):
            path.write_bytes(b"private-value\xff")
            with self.subTest(path=path), self.assertRaises(CcSwitchConfigError) as raised:
                with launch():
                    self.fail("unreadable configuration must not launch")
            self.assertNotIn("private-value", str(raised.exception))
            self.assertIsNone(raised.exception.__context__)

    def test_codex_live_globals_and_mcp_do_not_leak_active_provider(self):
        row = deepcopy(CODEX_SETTINGS)
        document = tomllib.loads(row["config"])
        document.update({"plugins": {"stale@test": {"enabled": True}},
                         "mcp_servers": {"same": {"command": "provider-mcp"}},
                         "model_context_window": 123456})
        document["agents"] = {"default_subagent_model": "provider-mini", "max_threads": 99}
        row["config"] = tomli_w.dumps(document)
        self.codex(row)
        self.db.set_setting("common_config_codex", "secret invalid TOML")
        live = {
            "model": "wrong-model", "model_provider": "wrong-provider",
            "openai_base_url": "https://wrong-route", "experimental_bearer_token": "wrong-secret",
            "model_catalog_json": "/wrong/catalog.json", "profile": "wrong-profile",
            "profiles": {"wrong-profile": {"model": "wrong-model"}},
            "model_providers": {"custom": {"base_url": "https://wrong-route", "experimental_bearer_token": "wrong-secret"}},
            "plugins": {"shared@test": {"enabled": False}},
            "mcp_servers": {"same": {"url": "https://global", "http_headers": {"Authorization": "wrong-secret"}},
                            "global": {"command": "global-mcp"}},
            "agents": {"max_threads": 6, "default_subagent_model": "wrong-model"},
            "memories": {"extract_model": "wrong-model", "enabled": True},
            "tui": {"status_line": ["model-name"]},
            "approval_policy": "on-request", "features": {"multi_agent": True},
        }
        self.codex_path.write_text(tomli_w.dumps(live))
        original = self.codex_path.read_bytes()
        original_db = self.db.path.read_bytes()
        with use_codex_runtime("selected", db_path=self.db.path) as runtime:
            text = runtime.config_path.read_text()
            final = tomllib.loads(text)
            self.assertNotIn("wrong-", text)
            self.assertEqual(final["model"], "gpt-test")
            self.assertEqual(final["model_context_window"], 123456)
            self.assertEqual(final["agents"], {"max_threads": 6, "default_subagent_model": "provider-mini"})
            self.assertEqual(final["memories"], {"enabled": True})
            self.assertEqual(final["mcp_servers"]["same"], {"command": "provider-mcp"})
            self.assertIn("global", final["mcp_servers"])
            self.assertNotIn("stale@test", final["plugins"])
            self.assertFalse(final["plugins"]["shared@test"]["enabled"])
            for key in ("tui", "features", "approval_policy"):
                self.assertEqual(final[key], live[key])
            self.assertEqual(runtime.environment["OPENAI_API_KEY"], "codex-test-secret")
        self.assertEqual(self.codex_path.read_bytes(), original)
        self.assertEqual(self.db.path.read_bytes(), original_db)

    def test_codex_global_mcp_edits_apply_to_next_launch_without_edit_mcp(self):
        self.codex(flag=False)
        for command in ("first", "second"):
            self.codex_path.write_text(tomli_w.dumps({"mcp_servers": {"global": {"command": command}}}))
            with use_codex_runtime("selected", db_path=self.db.path) as runtime:
                self.assertEqual(tomllib.loads(runtime.config_path.read_text())["mcp_servers"]["global"]["command"], command)

    def test_codex_eval_uses_live_shared_settings_and_selected_route(self):
        self.codex()
        self.codex_path.write_text(tomli_w.dumps({
            "model": "wrong-model", "model_provider": "wrong-provider",
            "model_providers": {"custom": {"base_url": "wrong-url"}},
            "mcp_servers": {"global": {"command": "shared-mcp"}},
            "features": {"multi_agent": True},
        }))
        with use_provider("codex", "selected", self.db.path) as runtime:
            path = Path(runtime.environment["CODEX_HOME"]) / "config.toml"
            settings = tomllib.loads(path.read_text())
            self.assertEqual(settings["model"], "gpt-test")
            self.assertEqual(settings["model_providers"]["custom"]["base_url"],
                             "https://provider.example/v1")
            self.assertTrue(settings["features"]["multi_agent"])
            self.assertEqual(settings["mcp_servers"]["global"]["command"], "shared-mcp")

    def test_reading_symlinked_codex_dotfile_does_not_modify_it(self):
        self.codex()
        target = self.root / "dotfile.toml"
        target.write_text('[features]\nmulti_agent = true\n')
        self.codex_path.symlink_to(target)
        with use_codex_runtime("selected", db_path=self.db.path) as runtime:
            self.assertTrue(tomllib.loads(runtime.config_path.read_text())["features"]["multi_agent"])
        self.assertTrue(self.codex_path.is_symlink())
        self.assertEqual(target.read_text(), '[features]\nmulti_agent = true\n')


if __name__ == "__main__":
    unittest.main()
