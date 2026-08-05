import hashlib
import json
import os
import stat
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

import tomli_w

import codex_plugin_state
from codex_plugin_state import (
    CANONICAL_SUPERPOWERS_ID,
    CodexPluginStateError,
    ComposedConfig,
    PluginStateStore,
    RuntimeSnapshot,
    compose_effective_config,
    deep_merge,
    plugin_flags,
    scan_plugin_inventory,
)


class VendoredTomliWTests(unittest.TestCase):
    def test_nested_plugin_config_round_trips(self):
        repository_root = Path(__file__).resolve().parents[1]
        document = {
            "model": "gpt-test",
            "plugins": {
                "superpowers@openai-api-curated": {"enabled": True},
            },
        }

        rendered = tomli_w.dumps(document)

        self.assertEqual(
            Path(tomli_w.__file__).resolve().parent,
            repository_root / "tomli_w",
        )
        self.assertEqual(tomllib.loads(rendered), document)


class EffectiveConfigTests(unittest.TestCase):
    def test_deep_merge_recurses_mappings_and_replaces_arrays_and_scalars(self):
        base = {"table": {"left": 1, "items": [1]}, "mode": "provider"}
        common = {"table": {"right": 2, "items": [2]}, "mode": {"nested": True}}

        self.assertEqual(
            deep_merge(base, common),
            {
                "table": {"left": 1, "right": 2, "items": [2]},
                "mode": {"nested": True},
            },
        )
        self.assertEqual(base["table"], {"left": 1, "items": [1]})
        self.assertEqual(common["table"], {"right": 2, "items": [2]})

    def test_common_is_applied_only_when_enabled(self):
        provider = {
            "model": "provider",
            "plugins": {"demo@test": {"enabled": False}},
        }
        common = {
            "model": "common",
            "plugins": {"demo@test": {"enabled": True}},
        }

        disabled = compose_effective_config(provider, common, False, {}, set(), {})
        enabled = compose_effective_config(provider, common, True, {}, set(), {})

        self.assertIsInstance(disabled, ComposedConfig)
        self.assertEqual(disabled.document["model"], "provider")
        self.assertEqual(enabled.document["model"], "common")
        self.assertFalse(disabled.baseline_plugins["demo@test"])
        self.assertTrue(enabled.baseline_plugins["demo@test"])

    def test_canonical_superpowers_id_wins_and_legacy_is_removed(self):
        result = compose_effective_config(
            {
                "plugins": {
                    "superpowers@openai-curated": {"enabled": False},
                    CANONICAL_SUPERPOWERS_ID: {"enabled": True},
                }
            },
            None,
            False,
            {},
            {CANONICAL_SUPERPOWERS_ID},
            {},
        )

        self.assertNotIn("superpowers@openai-curated", result.document["plugins"])
        self.assertTrue(plugin_flags(result.document)[CANONICAL_SUPERPOWERS_ID])
        self.assertTrue(result.baseline_plugins[CANONICAL_SUPERPOWERS_ID])

    def test_inventory_is_disabled_by_default_then_sidecar_wins(self):
        initial = compose_effective_config({}, None, False, {}, {"demo@test"}, {})
        overridden = compose_effective_config(
            {}, None, False, {}, {"demo@test"}, {"demo@test": True}
        )

        self.assertEqual(plugin_flags(initial.document), {"demo@test": False})
        self.assertEqual(plugin_flags(overridden.document), {"demo@test": True})
        self.assertEqual(initial.baseline_plugins, {})

    def test_plugin_without_enabled_defaults_to_true(self):
        result = compose_effective_config(
            {"plugins": {"demo@test": {"custom": "value"}}},
            None,
            False,
            {},
            {"demo@test"},
            {},
        )

        self.assertEqual(plugin_flags(result.document), {"demo@test": True})
        self.assertEqual(result.document["plugins"]["demo@test"]["custom"], "value")

    def test_invalid_plugin_and_marketplace_shapes_are_rejected(self):
        cases = (
            {"plugins": []},
            {"plugins": {"demo@test": True}},
            {"plugins": {"demo@test": {"enabled": "true"}}},
            {"marketplaces": []},
        )

        for provider in cases:
            with self.subTest(provider=provider):
                with self.assertRaises(CodexPluginStateError):
                    compose_effective_config(
                        provider, None, False, {}, set(), {}
                    )

    def test_launcher_marketplaces_override_provider_and_common(self):
        result = compose_effective_config(
            {"marketplaces": {"shared": {"source": "provider"}}},
            {"marketplaces": {"shared": {"source": "common"}}},
            True,
            {"shared": {"source": "launcher"}, "extra": {"source": "state"}},
            set(),
            {},
        )

        self.assertEqual(
            result.document["marketplaces"],
            {
                "shared": {"source": "launcher"},
                "extra": {"source": "state"},
            },
        )

    def test_missing_enabled_plugin_warns_once_without_changing_declaration(self):
        result = compose_effective_config(
            {"plugins": {"missing@test": {"enabled": True, "option": "kept"}}},
            None,
            False,
            {},
            set(),
            {"missing@test": True},
        )

        self.assertEqual(plugin_flags(result.document), {"missing@test": True})
        self.assertEqual(result.document["plugins"]["missing@test"]["option"], "kept")
        self.assertEqual(len(result.warnings), 1)
        self.assertIn("missing@test", result.warnings[0])


class PluginInventoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.plugins_root = Path(self.directory.name) / "plugins"

    def install(
        self,
        marketplace,
        plugin,
        version="1.2.3",
        *,
        manifest_name=None,
        raw_manifest=None,
    ):
        manifest_path = (
            self.plugins_root
            / "cache"
            / marketplace
            / plugin
            / version
            / ".codex-plugin"
            / "plugin.json"
        )
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        if raw_manifest is None:
            raw_manifest = json.dumps(
                {"name": manifest_name or plugin, "version": version}
            )
        manifest_path.write_text(raw_manifest, encoding="utf-8")

    def test_inventory_contains_only_valid_matching_manifests(self):
        self.install("test", "demo")
        self.install("test", "mismatch", manifest_name="other")
        self.install("test", "broken", raw_manifest="not-json")
        self.install(
            "test",
            "constant",
            raw_manifest='{"name":"constant","value":NaN}',
        )

        self.assertEqual(scan_plugin_inventory(self.plugins_root), {"demo@test"})

    def test_legacy_superpowers_cache_is_ignored_and_canonical_is_discovered(self):
        self.install("openai-curated", "superpowers")
        self.install("openai-api-curated", "superpowers")

        self.assertEqual(
            scan_plugin_inventory(self.plugins_root),
            frozenset({CANONICAL_SUPERPOWERS_ID}),
        )

    def test_unsafe_marketplace_and_plugin_segments_are_ignored(self):
        self.install("bad@marketplace", "demo")
        self.install("test", "bad@plugin")

        self.assertEqual(scan_plugin_inventory(self.plugins_root), frozenset())


class PluginStateStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.shared_home = Path(self.directory.name) / "shared-home"
        self.shared_home.mkdir()
        self.state = PluginStateStore(self.shared_home)

    def sidecar_path(self, provider_id):
        digest = hashlib.sha256(provider_id.encode()).hexdigest()
        return (
            self.shared_home
            / ".cc-switch-tui"
            / "provider-plugins"
            / f"{digest}.json"
        )

    def write_sidecar(self, provider_id, document):
        path = self.sidecar_path(provider_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        content = document if isinstance(document, str) else json.dumps(document)
        path.write_text(content, encoding="utf-8")
        return path

    @staticmethod
    def snapshot(plugins, inventory, marketplaces=None):
        return RuntimeSnapshot(
            plugins=plugins,
            marketplaces=marketplaces or {},
            inventory=frozenset(inventory),
        )

    def enable_plugin(self, provider_id, plugin_id="demo@test"):
        initial = self.snapshot({plugin_id: False}, {plugin_id})
        final = self.snapshot({plugin_id: True}, {plugin_id})
        self.state.apply_runtime_changes(
            provider_id,
            {plugin_id: False},
            initial,
            final,
        )

    def test_sidecar_uses_provider_hash_and_normalizes_legacy_superpowers(self):
        provider_id = "provider/with unsafe path text"
        path = self.write_sidecar(
            provider_id,
            {
                "version": 1,
                "provider_id": provider_id,
                "plugins": {"superpowers@openai-curated": False},
            },
        )

        loaded = self.state.load_provider_plugins(provider_id)

        self.assertEqual(loaded, {CANONICAL_SUPERPOWERS_ID: False})
        self.assertEqual(path, self.sidecar_path(provider_id))
        self.assertNotIn(provider_id, path.name)

    def test_reset_removes_malformed_sidecar_without_parsing_it(self):
        path = self.write_sidecar("provider", "not-json")

        self.state.reset_provider("provider")

        self.assertFalse(path.exists())
        self.assertEqual(self.state.load_provider_plugins("provider"), {})

    def test_malformed_unknown_and_mismatched_sidecars_are_rejected(self):
        cases = (
            ("malformed", "not-json"),
            (
                "unknown",
                {"version": 2, "provider_id": "unknown", "plugins": {}},
            ),
            (
                "expected",
                {"version": 1, "provider_id": "different", "plugins": {}},
            ),
        )

        for provider_id, document in cases:
            with self.subTest(provider_id=provider_id):
                self.write_sidecar(provider_id, document)
                with self.assertRaises(CodexPluginStateError):
                    self.state.load_provider_plugins(provider_id)

    def test_state_permissions_and_atomic_writes_are_private(self):
        self.enable_plugin("provider")
        state_root = self.shared_home / ".cc-switch-tui"

        for directory in (
            state_root,
            state_root / "provider-plugins",
            state_root / "locks",
        ):
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        for path in state_root.rglob("*"):
            if path.is_file():
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                self.assertFalse(path.name.endswith(".tmp"))

    def test_runtime_deltas_merge_with_latest_sidecar(self):
        first = RuntimeSnapshot(
            plugins={"alpha@test": False, "beta@test": False},
            marketplaces={},
            inventory=frozenset({"alpha@test", "beta@test"}),
        )
        self.state.apply_runtime_changes(
            "provider",
            {"alpha@test": False, "beta@test": False},
            first,
            RuntimeSnapshot(
                plugins={"alpha@test": True, "beta@test": False},
                marketplaces={},
                inventory=first.inventory,
            ),
        )
        self.state.apply_runtime_changes(
            "provider",
            {"alpha@test": False, "beta@test": False},
            first,
            RuntimeSnapshot(
                plugins={"alpha@test": False, "beta@test": True},
                marketplaces={},
                inventory=first.inventory,
            ),
        )

        self.assertEqual(
            self.state.load_provider_plugins("provider"),
            {"alpha@test": True, "beta@test": True},
        )

    def test_unchanged_old_snapshot_performs_no_atomic_write(self):
        self.enable_plugin("provider")
        old = self.snapshot({"demo@test": False}, {"demo@test"})

        with mock.patch.object(
            codex_plugin_state, "_atomic_write_text"
        ) as atomic_write:
            self.state.apply_runtime_changes(
                "provider", {"demo@test": False}, old, old
            )

        atomic_write.assert_not_called()
        self.assertEqual(
            self.state.load_provider_plugins("provider"), {"demo@test": True}
        )

    def test_same_plugin_last_actual_synchronization_wins(self):
        disabled = self.snapshot({"demo@test": False}, {"demo@test"})
        enabled = self.snapshot({"demo@test": True}, {"demo@test"})

        self.state.apply_runtime_changes(
            "provider", {"demo@test": False}, disabled, enabled
        )
        self.state.apply_runtime_changes(
            "provider", {"demo@test": False}, enabled, disabled
        )

        self.assertEqual(self.state.load_provider_plugins("provider"), {})
        self.assertFalse(self.sidecar_path("provider").exists())

    def test_inventory_removal_clears_plugin_from_every_provider(self):
        self.enable_plugin("first")
        self.enable_plugin("second")
        initial = self.snapshot({"demo@test": True}, {"demo@test"})
        final = self.snapshot({"demo@test": True}, set())

        self.state.apply_runtime_changes(
            "first", {"demo@test": False}, initial, final
        )

        self.assertEqual(self.state.load_provider_plugins("first"), {})
        self.assertEqual(self.state.load_provider_plugins("second"), {})

    def test_marketplace_state_initializes_once_and_overrides_provider(self):
        initial = self.state.load_or_initialize_marketplaces(
            {"shared": {"source": "shared"}},
            {
                "shared": {"source": "provider"},
                "provider-only": {"source": "first"},
            },
        )
        loaded_again = self.state.load_or_initialize_marketplaces(
            {"shared": {"source": "changed-shared"}},
            {
                "shared": {"source": "changed-provider"},
                "later": {"source": "provider"},
            },
        )

        self.assertEqual(
            initial,
            {
                "shared": {"source": "provider"},
                "provider-only": {"source": "first"},
            },
        )
        self.assertEqual(loaded_again["shared"], {"source": "provider"})
        self.assertEqual(loaded_again["provider-only"], {"source": "first"})
        self.assertEqual(loaded_again["later"], {"source": "provider"})

    def test_concurrent_marketplace_deltas_merge_different_keys(self):
        self.state.load_or_initialize_marketplaces({}, {})
        initial = self.snapshot(
            {},
            set(),
            {"alpha": {"source": "old"}, "beta": {"source": "old"}},
        )
        self.state.apply_runtime_changes(
            "provider",
            {},
            initial,
            self.snapshot(
                {},
                set(),
                {"alpha": {"source": "new"}, "beta": {"source": "old"}},
            ),
        )
        self.state.apply_runtime_changes(
            "provider",
            {},
            initial,
            self.snapshot(
                {},
                set(),
                {"alpha": {"source": "old"}, "beta": {"source": "new"}},
            ),
        )

        self.assertEqual(
            self.state.load_or_initialize_marketplaces({}, initial.marketplaces),
            {"alpha": {"source": "new"}, "beta": {"source": "new"}},
        )

    def test_removing_provider_only_marketplace_does_not_claim_ownership(self):
        self.state.load_or_initialize_marketplaces({}, {})
        baseline = {"provider-only": {"source": "provider"}}

        self.state.apply_runtime_changes(
            "provider",
            {},
            self.snapshot({}, set(), baseline),
            self.snapshot({}, set(), {}),
        )

        self.assertEqual(
            self.state.load_or_initialize_marketplaces({}, baseline), baseline
        )


if __name__ == "__main__":
    unittest.main()
