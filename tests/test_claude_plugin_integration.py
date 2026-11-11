import json
import os
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from cc_switch_config import SelectedProvider, materialize_claude_profile
from claude_plugin_state import ClaudePluginStateStore


PLUGIN_ID = "demo@localmkt"
PROVIDER_ID = "claude-integration-provider"


class ClaudePluginCliIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.executable = shutil.which("cometix") or shutil.which("claude")
        if self.executable is None:
            self.skipTest("Claude Code executable (cometix or claude) not found")
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.shared_home = self.root / "shared-claude"
        self.shared_home.mkdir()
        self.project = self.root / "project"
        self.project.mkdir()
        self.marketplace = self.root / "marketplace"
        self._create_marketplace()

        environment = mock.patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        for key in list(os.environ):
            if key.startswith("CLAUDE_CODE_") or key in (
                "CLAUDECODE",
                "CLAUDE_SECURESTORAGE_CONFIG_DIR",
            ):
                del os.environ[key]
        os.environ.update(
            {
                "CLAUDE_CONFIG_DIR": str(self.shared_home),
                "CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL": "1",
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                "DISABLE_AUTOUPDATER": "1",
            }
        )

        self._claude(dict(os.environ), "plugin", "marketplace", "add", str(self.marketplace))
        self._claude(dict(os.environ), "plugin", "install", PLUGIN_ID, "--scope", "user")
        self.provider = SelectedProvider(
            PROVIDER_ID,
            "claude",
            "integration",
            {
                "env": {
                    "ANTHROPIC_BASE_URL": "http://127.0.0.1:9",
                    "ANTHROPIC_API_KEY": "integration-placeholder",
                },
                "enabledPlugins": {PLUGIN_ID: True},
            },
        )
        self.store = ClaudePluginStateStore(self.shared_home)

    def _create_marketplace(self):
        manifest = self.marketplace / ".claude-plugin" / "marketplace.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(
            json.dumps(
                {
                    "name": "localmkt",
                    "owner": {"name": "test"},
                    "plugins": [
                        {
                            "name": "demo",
                            "source": "./plugins/demo",
                            "description": "demo",
                        }
                    ],
                }
            ),
            "utf-8",
        )
        plugin = self.marketplace / "plugins" / "demo"
        (plugin / ".claude-plugin").mkdir(parents=True)
        (plugin / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"name": "demo", "version": "1.0.0", "description": "demo"}),
            "utf-8",
        )
        (plugin / "skills" / "hello").mkdir(parents=True)
        (plugin / "skills" / "hello" / "SKILL.md").write_text(
            "---\nname: hello\ndescription: say hello\n---\nhello\n", "utf-8"
        )

    def _claude(self, environment, *arguments):
        completed = subprocess.run(
            [self.executable, *arguments],
            cwd=self.project,
            env=environment,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(
            completed.returncode, 0, completed.stdout + completed.stderr
        )
        return completed.stdout

    def _window_claude(self, runtime, *arguments):
        return self._claude(
            runtime.environment,
            "--setting-sources",
            "user,project,local",
            "--settings",
            str(runtime.settings_path),
            *arguments,
        )

    def _enabled(self, runtime):
        listed = json.loads(self._window_claude(runtime, "plugin", "list", "--json"))
        return {plugin["id"]: plugin.get("enabled") for plugin in listed}.get(
            PLUGIN_ID
        )

    def _install_paths(self):
        record = json.loads(
            (self.shared_home / "plugins" / "installed_plugins.json").read_text(
                "utf-8"
            )
        )
        return [entry["installPath"] for entry in record["plugins"][PLUGIN_ID]]

    def test_disable_in_a_window_is_saved_for_that_provider_only(self):
        shared_settings = (self.shared_home / "settings.json").read_text("utf-8")

        with materialize_claude_profile(self.provider) as runtime:
            self.assertTrue(self._enabled(runtime))
            self._window_claude(runtime, "plugin", "disable", PLUGIN_ID)
            self.assertFalse(self._enabled(runtime))

        self.assertEqual(
            (self.shared_home / "settings.json").read_text("utf-8"), shared_settings
        )
        self.assertEqual(
            self.store.load_provider_plugins(PROVIDER_ID), {PLUGIN_ID: False}
        )
        with materialize_claude_profile(self.provider) as runtime:
            self.assertFalse(self._enabled(runtime))
        other = replace(self.provider, provider_id="other-integration-provider")
        with materialize_claude_profile(other) as runtime:
            self.assertTrue(self._enabled(runtime))

    def test_plugin_installed_in_a_window_is_recorded_in_shared_home(self):
        with materialize_claude_profile(self.provider) as runtime:
            self._window_claude(runtime, "plugin", "uninstall", PLUGIN_ID)
            self._window_claude(
                runtime, "plugin", "install", PLUGIN_ID, "--scope", "user"
            )
            self.assertTrue(
                all(
                    path.startswith(str(runtime.config_home) + os.sep)
                    for path in self._install_paths()
                )
            )

        paths = self._install_paths()
        self.assertTrue(paths)
        for path in paths:
            self.assertTrue(path.startswith(str(self.shared_home) + os.sep))
            self.assertTrue(Path(path).is_dir())
        self.assertEqual(self.store.load_provider_plugins(PROVIDER_ID), {})


if __name__ == "__main__":
    unittest.main()
