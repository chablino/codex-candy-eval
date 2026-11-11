import json
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from claude_plugin_state import (
    ClaudePluginStateError,
    ClaudePluginStateStore,
    RuntimeSnapshot,
    effective_plugins,
    enabled_view,
    read_installed_plugins,
)


PLUGIN = "superpowers@claude-plugins-official"
OTHER = "frontend-design@claude-plugins-official"
BOTH = frozenset({PLUGIN, OTHER})


class ImportTests(unittest.TestCase):
    def test_module_imports_where_fcntl_is_missing(self):
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; sys.modules['fcntl'] = None; import claude_plugin_state",
            ],
            cwd=Path(__file__).resolve().parent.parent,
            capture_output=True,
            text=True,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)


class PluginViewTests(unittest.TestCase):
    def test_enabled_view_matches_claude_code(self):
        self.assertEqual(
            enabled_view(
                {"a": True, "b": False, "c": ["^1.0"], "d": "yes", "e": None}
            ),
            {"a": True, "b": False, "c": True, "d": False, "e": False},
        )

    def test_saved_switches_override_baseline_and_keep_other_values(self):
        baseline = {PLUGIN: True, "pinned@m": ["^1.0"]}

        effective = effective_plugins(baseline, {PLUGIN: False, OTHER: True})

        self.assertEqual(
            effective, {PLUGIN: False, "pinned@m": ["^1.0"], OTHER: True}
        )
        self.assertEqual(baseline, {PLUGIN: True, "pinned@m": ["^1.0"]})


class InstalledPluginsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.plugins_root = Path(directory.name)
        self.record = self.plugins_root / "installed_plugins.json"

    def test_missing_record_means_nothing_installed(self):
        self.assertEqual(read_installed_plugins(self.plugins_root), frozenset())

    def test_record_keys_are_installed_ids(self):
        self.record.write_text(
            json.dumps({"version": 2, "plugins": {PLUGIN: [{}], OTHER: [{}]}}),
            "utf-8",
        )

        self.assertEqual(read_installed_plugins(self.plugins_root), BOTH)

    def test_unreadable_record_is_unknown(self):
        for payload in ("{", "[]", '{"plugins": []}'):
            with self.subTest(payload=payload):
                self.record.write_text(payload, "utf-8")

                self.assertIsNone(read_installed_plugins(self.plugins_root))


class StoreTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name)
        self.store = ClaudePluginStateStore(self.home)

    def sync(self, provider_id, baseline, initial, final, installed=(BOTH, BOTH)):
        self.store.apply_runtime_changes(
            provider_id,
            baseline,
            RuntimeSnapshot(initial, installed[0]),
            RuntimeSnapshot(final, installed[1]),
        )

    def write_raw(self, provider_id, text):
        path = self.store._sidecar_path(provider_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, "utf-8")

    def test_missing_sidecar_loads_empty(self):
        self.assertEqual(self.store.load_provider_plugins("provider-a"), {})

    def test_disabling_baseline_plugin_saves_sparse_false(self):
        baseline = {PLUGIN: True, OTHER: True}

        self.sync("provider-a", baseline, baseline, {PLUGIN: False, OTHER: True})

        self.assertEqual(
            self.store.load_provider_plugins("provider-a"), {PLUGIN: False}
        )
        path = self.store._sidecar_path("provider-a")
        self.assertEqual(
            json.loads(path.read_text("utf-8")),
            {"version": 1, "provider_id": "provider-a", "plugins": {PLUGIN: False}},
        )
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)

    def test_returning_to_baseline_removes_sidecar(self):
        baseline = {PLUGIN: True}
        self.sync("provider-a", baseline, baseline, {PLUGIN: False})

        self.sync("provider-a", baseline, {PLUGIN: False}, {PLUGIN: True})

        self.assertFalse(self.store._sidecar_path("provider-a").exists())
        self.assertEqual(self.store.load_provider_plugins("provider-a"), {})

    def test_unchanged_window_writes_nothing(self):
        self.sync("provider-a", {PLUGIN: True}, {PLUGIN: True}, {PLUGIN: True})

        self.assertFalse(self.store.state_root.exists())

    def test_uninstall_clears_saved_enables_everywhere_and_keeps_disables(self):
        self.sync("provider-b", {}, {}, {PLUGIN: True})
        self.sync("provider-c", {PLUGIN: True}, {PLUGIN: True}, {PLUGIN: False})

        self.sync("provider-a", {}, {}, {}, installed=(BOTH, frozenset({OTHER})))

        self.assertEqual(self.store.load_provider_plugins("provider-b"), {})
        self.assertFalse(self.store._sidecar_path("provider-b").exists())
        self.assertEqual(
            self.store.load_provider_plugins("provider-c"), {PLUGIN: False}
        )
        self.assertEqual(self.store.load_provider_plugins("provider-a"), {})

    def test_uninstalling_baseline_plugin_saves_disable_for_current_provider(self):
        self.sync(
            "provider-a",
            {PLUGIN: True},
            {PLUGIN: True},
            {},
            installed=(frozenset({PLUGIN}), frozenset()),
        )

        self.assertEqual(
            self.store.load_provider_plugins("provider-a"), {PLUGIN: False}
        )

    def test_unknown_install_record_skips_uninstall_cleanup(self):
        self.sync("provider-b", {}, {}, {PLUGIN: True})

        for installed in ((BOTH, None), (None, frozenset())):
            with self.subTest(installed=installed):
                self.sync("provider-a", {}, {}, {}, installed=installed)

                self.assertEqual(
                    self.store.load_provider_plugins("provider-b"), {PLUGIN: True}
                )

    def test_concurrent_windows_keep_both_changes(self):
        baseline = {PLUGIN: True, OTHER: True}

        self.sync("provider-a", baseline, baseline, {PLUGIN: False, OTHER: True})
        self.sync("provider-a", baseline, baseline, {PLUGIN: True, OTHER: False})
        self.sync("provider-a", baseline, baseline, baseline)

        self.assertEqual(
            self.store.load_provider_plugins("provider-a"),
            {PLUGIN: False, OTHER: False},
        )

    def test_invalid_sidecars_are_rejected_with_reset_hint(self):
        cases = (
            "{",
            '{"version": 2, "provider_id": "provider-a", "plugins": {}}',
            '{"version": 1, "provider_id": "provider-a", "plugins": {"x@m": "yes"}}',
            '{"version": 1, "provider_id": "provider-b", "plugins": {}}',
            '{"version": 1, "provider_id": "provider-a", "plugins": {}, "extra": 1}',
        )
        for text in cases:
            with self.subTest(text=text):
                self.write_raw("provider-a", text)

                with self.assertRaises(ClaudePluginStateError) as raised:
                    self.store.load_provider_plugins("provider-a")

                self.assertIn("--reset-plugin-state", str(raised.exception))

    def test_reset_removes_corrupt_sidecar(self):
        self.write_raw("provider-a", "{")

        self.store.reset_provider("provider-a")

        self.assertEqual(self.store.load_provider_plugins("provider-a"), {})


if __name__ == "__main__":
    unittest.main()
