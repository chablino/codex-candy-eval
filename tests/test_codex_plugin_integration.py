import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import tomllib
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from codex_plugin_state import PluginStateStore, scan_plugin_inventory
from codex_runtime import use_codex_runtime


PLUGIN_ID = "demo@test-marketplace"
PROVIDER_ID = "provider-integration-id"
OTHER_PROVIDER_ID = "other-provider-integration-id"
DEFAULT_PLUGIN_IDS = {
    "superpowers@openai-api-curated",
    "documents@openai-primary-runtime",
    "pdf@openai-primary-runtime",
    "presentations@openai-primary-runtime",
    "template-creator@openai-primary-runtime",
    "spreadsheets@openai-primary-runtime",
    "visualize@openai-bundled",
    "browser@openai-bundled",
}


class CodexPluginCliIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.shared_home = self.root / "shared-codex"
        self.shared_home.mkdir()
        self.sqlite_home = self.root / "shared-sqlite"
        self.sqlite_home.mkdir()
        self.marketplace = self.root / "marketplace"
        self.database = self.root / "cc-switch.db"
        self.codex = shutil.which("codex")

        self._create_curated_marketplace()
        self._create_marketplace()
        self._create_database()

    def _create_curated_marketplace(self):
        root = self.shared_home / ".tmp" / "plugins"
        marketplace_manifest = root / ".agents" / "plugins" / "marketplace.json"
        marketplace_manifest.parent.mkdir(parents=True)
        marketplace_manifest.write_text(
            json.dumps(
                {
                    "name": "openai-curated",
                    "interface": {"displayName": "Codex official"},
                    "plugins": [
                        {
                            "name": "superpowers",
                            "source": {
                                "source": "local",
                                "path": "./plugins/superpowers",
                            },
                            "policy": {
                                "installation": "AVAILABLE",
                                "authentication": "ON_INSTALL",
                            },
                            "category": "Developer Tools",
                        }
                    ],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        api_marketplace_manifest = (
            root / ".agents" / "plugins" / "api_marketplace.json"
        )
        api_marketplace_manifest.write_text(
            json.dumps(
                {
                    "name": "openai-api-curated",
                    "interface": {"displayName": "Codex official"},
                    "plugins": [
                        {
                            "name": "superpowers",
                            "source": {
                                "source": "local",
                                "path": "./plugins/superpowers",
                            },
                            "policy": {
                                "installation": "AVAILABLE",
                                "authentication": "ON_INSTALL",
                                "products": ["CODEX"],
                            },
                            "category": "Developer Tools",
                        }
                    ],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        plugin_root = root / "plugins" / "superpowers"
        plugin_manifest = plugin_root / ".codex-plugin" / "plugin.json"
        plugin_manifest.parent.mkdir(parents=True)
        plugin_manifest.write_text(
            json.dumps(
                {
                    "name": "superpowers",
                    "version": "1.0.0",
                    "description": "Isolated API marketplace fixture",
                    "skills": "./skills/",
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        skill = plugin_root / "skills" / "fixture" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text(
            """\
---
name: fixture
description: Isolated API marketplace fixture.
---

# Fixture
""",
            encoding="utf-8",
        )
    def _create_marketplace(self):
        manifest = {
            "name": "test-marketplace",
            "interface": {"displayName": "Test Marketplace"},
            "plugins": [
                {
                    "name": "demo",
                    "source": {
                        "source": "local",
                        "path": "./plugins/demo",
                    },
                    "policy": {
                        "installation": "AVAILABLE",
                        "authentication": "ON_INSTALL",
                    },
                    "category": "Engineering",
                }
            ],
        }
        marketplace_manifest = (
            self.marketplace / ".agents" / "plugins" / "marketplace.json"
        )
        marketplace_manifest.parent.mkdir(parents=True)
        marketplace_manifest.write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )

        plugin_root = self.marketplace / "plugins" / "demo"
        plugin_manifest = plugin_root / ".codex-plugin" / "plugin.json"
        plugin_manifest.parent.mkdir(parents=True)
        plugin_manifest.write_text(
            json.dumps(
                {
                    "name": "demo",
                    "version": "1.0.0",
                    "description": "Local integration-test plugin",
                    "skills": "./skills/",
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        skill = plugin_root / "skills" / "demo" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text(
            """\
---
name: demo
description: Local integration-test skill.
---

# Demo

Use this skill only for the isolated plugin integration test.
""",
            encoding="utf-8",
        )

    def _create_database(self):
        provider_config = f'''\
model = "gpt-test"
model_provider = "custom"

[model_providers.custom]
name = "Custom"
base_url = "https://provider.invalid/v1"
wire_api = "responses"
requires_openai_auth = true

[marketplaces.test-marketplace]
source_type = "local"
source = {json.dumps(str(self.marketplace))}
'''
        settings = json.dumps(
            {
                "config": provider_config,
                "auth": {"OPENAI_API_KEY": "integration-test-secret"},
            }
        )
        meta = json.dumps({"commonConfigEnabled": False})
        with closing(sqlite3.connect(self.database)) as connection:
            with connection:
                connection.execute(
                    """
                    CREATE TABLE providers (
                        id TEXT NOT NULL,
                        app_type TEXT NOT NULL,
                        name TEXT NOT NULL,
                        settings_config TEXT NOT NULL,
                        meta TEXT NOT NULL DEFAULT '{}'
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE settings (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO providers
                        (id, app_type, name, settings_config, meta)
                    VALUES (?, 'codex', 'integration-provider', ?, ?)
                    """,
                    (PROVIDER_ID, settings, meta),
                )
                connection.execute(
                    """
                    INSERT INTO providers
                        (id, app_type, name, settings_config, meta)
                    VALUES (?, 'codex', 'other-integration-provider', ?, ?)
                    """,
                    (OTHER_PROVIDER_ID, settings, meta),
                )
                connection.execute(
                    "INSERT INTO settings (key, value) VALUES (?, ?)",
                    (
                        "common_config_codex",
                        '[plugins."demo@test-marketplace"]\nenabled = false\n',
                    ),
                )

    def _install_cached_plugin(self, plugin_id):
        plugin, marketplace = plugin_id.rsplit("@", 1)
        manifest = (
            self.shared_home
            / "plugins"
            / "cache"
            / marketplace
            / plugin
            / "1.0.0"
            / ".codex-plugin"
            / "plugin.json"
        )
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            json.dumps({"name": plugin, "version": "1.0.0"}) + "\n",
            encoding="utf-8",
        )

    def _database_rows(self):
        with closing(sqlite3.connect(self.database)) as connection:
            return (
                list(connection.execute("SELECT * FROM providers")),
                list(connection.execute("SELECT * FROM settings")),
            )

    def _plugin_command(self, runtime, *arguments):
        result = subprocess.run(
            [self.codex, "plugin", *arguments],
            cwd=self.root,
            env=runtime.environment,
            text=True,
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            self.fail(
                f"Codex plugin command did not return JSON: {result.stdout!r}"
            )

    def _marketplace_command(self, runtime):
        result = subprocess.run(
            [self.codex, "plugin", "marketplace", "list", "--json"],
            cwd=self.root,
            env=runtime.environment,
            text=True,
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            self.fail(
                "Codex marketplace command did not return JSON: "
                f"{result.stdout!r}"
            )

    @staticmethod
    def _serialized_json_contains(document, *values):
        serialized = json.dumps(document, sort_keys=True)
        return all(value in serialized for value in values)

    def test_plugin_install_enable_and_global_remove_lifecycle(self):
        if self.codex is None:
            self.skipTest("Codex CLI executable is unavailable")
        probe_environment = os.environ.copy()
        probe_environment["CODEX_HOME"] = str(self.shared_home)
        probe = subprocess.run(
            [self.codex, "plugin", "--help"],
            env=probe_environment,
            text=True,
            capture_output=True,
            timeout=10,
        )
        if probe.returncode != 0:
            self.skipTest("installed Codex CLI does not support plugins")

        database_bytes = self.database.read_bytes()
        database_rows = self._database_rows()
        environment = {
            "CODEX_HOME": str(self.shared_home),
            "CODEX_SQLITE_HOME": str(self.sqlite_home),
        }
        state = PluginStateStore(self.shared_home)

        with mock.patch.dict(os.environ, environment, clear=False):
            with use_codex_runtime(
                "integration-provider", db_path=self.database
            ) as runtime:
                self.assertEqual(
                    json.loads((runtime.home / "auth.json").read_text()),
                    {"OPENAI_API_KEY": "integration-test-secret"},
                )
                marketplaces = self._marketplace_command(runtime)
                names = {
                    entry["name"] for entry in marketplaces["marketplaces"]
                }
                self.assertIn("openai-api-curated", names)
                self.assertNotIn("openai-curated", names)
                available = self._plugin_command(
                    runtime,
                    "list",
                    "--marketplace",
                    "test-marketplace",
                    "--available",
                    "--json",
                )
                self.assertTrue(
                    self._serialized_json_contains(
                        available, "demo", "test-marketplace"
                    ),
                    available,
                )

                added = self._plugin_command(
                    runtime, "add", PLUGIN_ID, "--json"
                )
                self.assertTrue(
                    self._serialized_json_contains(added, "demo"), added
                )
                self.assertEqual(
                    scan_plugin_inventory(self.shared_home / "plugins"),
                    {PLUGIN_ID},
                )
                config = tomllib.loads(runtime.config_path.read_text())
                self.assertTrue(config["plugins"][PLUGIN_ID]["enabled"])

        self.assertEqual(
            state.load_provider_plugins(PROVIDER_ID), {PLUGIN_ID: True}
        )

        with mock.patch.dict(os.environ, environment, clear=False):
            with use_codex_runtime(
                OTHER_PROVIDER_ID, db_path=self.database
            ) as runtime:
                config = tomllib.loads(runtime.config_path.read_text())
                self.assertFalse(config["plugins"][PLUGIN_ID]["enabled"])
                self.assertEqual(
                    scan_plugin_inventory(self.shared_home / "plugins"),
                    {PLUGIN_ID},
                )

        self.assertEqual(state.load_provider_plugins(OTHER_PROVIDER_ID), {})

        with mock.patch.dict(os.environ, environment, clear=False):
            with use_codex_runtime(PROVIDER_ID, db_path=self.database) as runtime:
                config = tomllib.loads(runtime.config_path.read_text())
                self.assertTrue(config["plugins"][PLUGIN_ID]["enabled"])
                installed = self._plugin_command(runtime, "list", "--json")
                self.assertTrue(
                    self._serialized_json_contains(
                        installed, "demo", "test-marketplace"
                    ),
                    installed,
                )

                removed = self._plugin_command(
                    runtime, "remove", PLUGIN_ID, "--json"
                )
                self.assertTrue(
                    self._serialized_json_contains(removed, "demo"), removed
                )
                self.assertEqual(
                    scan_plugin_inventory(self.shared_home / "plugins"), set()
                )

        self.assertEqual(state.load_provider_plugins(PROVIDER_ID), {})
        sidecar = (
            self.shared_home
            / ".cc-switch-tui"
            / "provider-plugins"
            / f"{hashlib.sha256(PROVIDER_ID.encode()).hexdigest()}.json"
        )
        self.assertFalse(sidecar.exists())
        self.assertEqual(self.database.read_bytes(), database_bytes)
        self.assertEqual(self._database_rows(), database_rows)

    def test_installed_project_defaults_are_enabled_without_database_writes(self):
        if self.codex is None:
            self.skipTest("Codex CLI executable is unavailable")
        probe_environment = os.environ.copy()
        probe_environment["CODEX_HOME"] = str(self.shared_home)
        probe = subprocess.run(
            [self.codex, "plugin", "--help"],
            env=probe_environment,
            text=True,
            capture_output=True,
            timeout=10,
        )
        if probe.returncode != 0:
            self.skipTest("installed Codex CLI does not support plugins")

        for plugin_id in DEFAULT_PLUGIN_IDS:
            self._install_cached_plugin(plugin_id)

        database_bytes = self.database.read_bytes()
        database_rows = self._database_rows()
        environment = {
            "CODEX_HOME": str(self.shared_home),
            "CODEX_SQLITE_HOME": str(self.sqlite_home),
        }

        with mock.patch.dict(os.environ, environment, clear=False):
            with use_codex_runtime(
                "integration-provider", db_path=self.database
            ) as runtime:
                config = tomllib.loads(runtime.config_path.read_text())
                self.assertEqual(
                    {
                        plugin_id
                        for plugin_id, entry in config["plugins"].items()
                        if entry["enabled"]
                    },
                    DEFAULT_PLUGIN_IDS,
                )
                installed = self._plugin_command(
                    runtime,
                    "list",
                    "--marketplace",
                    "openai-api-curated",
                    "--json",
                )
                self.assertTrue(
                    self._serialized_json_contains(
                        installed, "superpowers", "installed"
                    ),
                    installed,
                )

        self.assertEqual(self.database.read_bytes(), database_bytes)
        self.assertEqual(self._database_rows(), database_rows)


if __name__ == "__main__":
    unittest.main()
