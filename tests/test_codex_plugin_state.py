import tomllib
import unittest
from pathlib import Path

import tomli_w

from codex_plugin_state import (
    CANONICAL_SUPERPOWERS_ID,
    CodexPluginStateError,
    ComposedConfig,
    compose_effective_config,
    deep_merge,
    plugin_flags,
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


if __name__ == "__main__":
    unittest.main()
