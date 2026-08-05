import hashlib
import json
import os
import stat
import tempfile
import tomllib
import unittest
from copy import deepcopy
from pathlib import Path
from unittest import mock

import tomli_w

import codex_runtime
from cc_switch_config import CcSwitchConfigError, SelectedProvider
from codex_plugin_state import CANONICAL_SUPERPOWERS_ID
from codex_runtime import (
    DIRECTORY_SHARE_ALLOWLIST,
    FILE_SHARE_ALLOWLIST,
    CodexRuntime,
    materialize_codex_runtime,
)


PROVIDER_CONFIG = """\
model = "gpt-provider"
model_provider = "custom"

[model_providers.custom]
name = "Custom"
base_url = "https://provider.example/v1"
wire_api = "responses"
requires_openai_auth = true
"""
PROVIDER_SETTINGS = {
    "config": PROVIDER_CONFIG,
    "auth": {"OPENAI_API_KEY": "provider-secret"},
}


class CodexRuntimeMaterializationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.shared_home = self.root / "shared-codex"
        self.shared_home.mkdir()
        self.shared_sqlite_home = self.root / "shared-sqlite"
        self.shared_sqlite_home.mkdir()
        self.provider = SelectedProvider(
            "provider-id",
            "codex",
            "fengwind",
            deepcopy(PROVIDER_SETTINGS),
        )

    def environment(self, **extra):
        return {
            "CODEX_HOME": str(self.shared_home),
            "CODEX_SQLITE_HOME": str(self.shared_sqlite_home),
            "INHERITED_VALUE": "kept",
            **extra,
        }

    def install_plugin(
        self,
        plugin="demo",
        marketplace="test",
        version="1.0.0",
    ):
        manifest = (
            self.shared_home
            / "plugins"
            / "cache"
            / marketplace
            / plugin
            / version
            / ".codex-plugin"
            / "plugin.json"
        )
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            json.dumps({"name": plugin, "version": version}),
            encoding="utf-8",
        )
        return f"{plugin}@{marketplace}"

    def sidecar_path(self, provider_id="provider-id"):
        digest = hashlib.sha256(provider_id.encode()).hexdigest()
        return (
            self.shared_home
            / ".cc-switch-tui"
            / "provider-plugins"
            / f"{digest}.json"
        )

    def test_complete_runtime_is_isolated_and_shares_only_allowlist(self):
        shared_config = self.shared_home / "config.toml"
        shared_auth = self.shared_home / "auth.json"
        shared_config_text = """\
model = "app-selected"

[marketplaces.shared]
source = "shared-marketplace"

[plugins."shared-only@test"]
enabled = true
"""
        shared_config.write_text(shared_config_text, encoding="utf-8")
        shared_auth.write_text('{"token":"app-secret"}\n', encoding="utf-8")
        session = self.shared_home / "sessions" / "session.jsonl"
        session.parent.mkdir()
        session.write_text("shared-session\n", encoding="utf-8")
        history = self.shared_home / "history.jsonl"
        history.write_text("shared-history\n", encoding="utf-8")
        plugin_id = self.install_plugin()
        parent_environment = self.environment(OPENAI_API_KEY="parent-secret")

        with mock.patch.dict(os.environ, parent_environment, clear=True):
            with materialize_codex_runtime(self.provider, None) as runtime:
                runtime_home = runtime.home
                config_text = runtime.config_path.read_text(encoding="utf-8")
                config = tomllib.loads(config_text)
                selected = config["model_providers"][config["model_provider"]]

                self.assertIsInstance(runtime, CodexRuntime)
                self.assertEqual(runtime.config_path, runtime_home / "config.toml")
                self.assertNotEqual(runtime_home, self.shared_home)
                self.assertEqual(config["model"], "gpt-provider")
                self.assertNotIn("shared-only@test", config.get("plugins", {}))
                self.assertFalse(config["plugins"][plugin_id]["enabled"])
                self.assertEqual(
                    config["marketplaces"]["shared"]["source"],
                    "shared-marketplace",
                )
                self.assertFalse(selected["requires_openai_auth"])
                self.assertEqual(selected["env_key"], "OPENAI_API_KEY")
                self.assertNotIn("provider-secret", config_text)
                self.assertFalse((runtime_home / "auth.json").exists())
                self.assertEqual(stat.S_IMODE(runtime_home.stat().st_mode), 0o700)
                self.assertEqual(
                    stat.S_IMODE(runtime.config_path.stat().st_mode), 0o600
                )

                for name in DIRECTORY_SHARE_ALLOWLIST:
                    link = runtime_home / name
                    target = self.shared_home / name
                    self.assertTrue(link.is_symlink(), name)
                    self.assertEqual(link.resolve(), target.resolve())
                    self.assertTrue(target.is_dir())
                for name in FILE_SHARE_ALLOWLIST:
                    link = runtime_home / name
                    target = self.shared_home / name
                    self.assertTrue(link.is_symlink(), name)
                    self.assertEqual(
                        Path(os.readlink(link)).resolve(), target.resolve(), name
                    )

                self.assertEqual(
                    (runtime_home / "sessions" / "session.jsonl").read_text(),
                    "shared-session\n",
                )
                self.assertEqual(
                    (runtime_home / "history.jsonl").read_text(),
                    "shared-history\n",
                )
                self.assertFalse((runtime_home / "logs").exists())
                self.assertFalse((runtime_home / ".cc-switch-tui").exists())
                self.assertEqual(
                    runtime.environment["CODEX_HOME"], str(runtime_home)
                )
                self.assertEqual(
                    runtime.environment["CODEX_SQLITE_HOME"],
                    str(self.shared_sqlite_home.resolve()),
                )
                self.assertEqual(
                    runtime.environment["OPENAI_API_KEY"], "provider-secret"
                )
                self.assertEqual(
                    runtime.environment["CODEX_INTERNAL_ORIGINATOR_OVERRIDE"],
                    "codex-tui",
                )
                self.assertEqual(runtime.environment["INHERITED_VALUE"], "kept")
                self.assertNotIn("provider-secret", repr(runtime))
                self.assertEqual(dict(os.environ), parent_environment)

            self.assertFalse(runtime_home.exists())
            self.assertEqual(dict(os.environ), parent_environment)

        self.assertEqual(shared_config.read_text(), shared_config_text)
        self.assertEqual(shared_auth.read_text(), '{"token":"app-secret"}\n')
        self.assertEqual(session.read_text(), "shared-session\n")

    def test_common_config_merges_structurally_when_supplied(self):
        plugin_id = self.install_plugin()
        common = f'''\
model = "gpt-common"

[plugins."{plugin_id}"]
enabled = true
'''

        with mock.patch.dict(os.environ, self.environment(), clear=True):
            with materialize_codex_runtime(self.provider, common) as runtime:
                config = tomllib.loads(runtime.config_path.read_text())

        self.assertEqual(config["model"], "gpt-common")
        self.assertTrue(config["plugins"][plugin_id]["enabled"])

    def test_auth_transformation_targets_only_selected_model_provider(self):
        config = """\
model_provider = "selected provider"

[unrelated]
requires_openai_auth = true

[model_providers."selected provider"]
requires_openai_auth = true

[model_providers.other]
requires_openai_auth = true
"""
        provider = SelectedProvider(
            "id",
            "codex",
            "provider",
            {
                "config": config,
                "auth": {"OPENAI_API_KEY": "selected-secret"},
            },
        )

        with mock.patch.dict(os.environ, self.environment(), clear=True):
            with materialize_codex_runtime(provider, None) as runtime:
                parsed = tomllib.loads(runtime.config_path.read_text())

        self.assertTrue(parsed["unrelated"]["requires_openai_auth"])
        self.assertTrue(
            parsed["model_providers"]["other"]["requires_openai_auth"]
        )
        selected = parsed["model_providers"]["selected provider"]
        self.assertFalse(selected["requires_openai_auth"])
        self.assertEqual(selected["env_key"], "OPENAI_API_KEY")

    def test_missing_enabled_plugin_warns_without_downloading(self):
        document = tomllib.loads(PROVIDER_CONFIG)
        document["plugins"] = {
            CANONICAL_SUPERPOWERS_ID: {"enabled": True}
        }
        provider = SelectedProvider(
            "id",
            "codex",
            "provider",
            {
                "config": tomli_w.dumps(document),
                "auth": {"OPENAI_API_KEY": "provider-secret"},
            },
        )

        with mock.patch.dict(os.environ, self.environment(), clear=True):
            with materialize_codex_runtime(provider, None) as runtime:
                parsed = tomllib.loads(runtime.config_path.read_text())
                self.assertTrue(
                    parsed["plugins"][CANONICAL_SUPERPOWERS_ID]["enabled"]
                )
                self.assertEqual(len(runtime.warnings), 1)
                self.assertIn(CANONICAL_SUPERPOWERS_ID, runtime.warnings[0])

        self.assertFalse(
            (
                self.shared_home
                / "plugins"
                / "cache"
                / "openai-api-curated"
                / "superpowers"
            ).exists()
        )

    def test_reset_removes_malformed_provider_sidecar_before_loading(self):
        sidecar = self.sidecar_path()
        sidecar.parent.mkdir(parents=True)
        sidecar.write_text("not-json", encoding="utf-8")

        with mock.patch.dict(os.environ, self.environment(), clear=True):
            with materialize_codex_runtime(
                self.provider, None, reset_plugin_state=True
            ):
                self.assertFalse(sidecar.exists())

    def test_malformed_sidecar_fails_without_reset(self):
        sidecar = self.sidecar_path()
        sidecar.parent.mkdir(parents=True)
        sidecar.write_text("not-json", encoding="utf-8")

        with mock.patch.dict(os.environ, self.environment(), clear=True):
            with self.assertRaises(CcSwitchConfigError) as raised:
                with materialize_codex_runtime(self.provider, None):
                    pass

        self.assertNotIn("not-json", str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)
        self.assertIsNone(raised.exception.__context__)

    def test_invalid_provider_common_and_shared_toml_are_sanitized(self):
        cases = []
        bad_provider = SelectedProvider(
            "id",
            "codex",
            "provider",
            {
                "config": "secret-provider = [",
                "auth": {"OPENAI_API_KEY": "secret"},
            },
        )
        cases.append((bad_provider, None, None, "secret-provider"))
        cases.append((self.provider, "secret-common = [", None, "secret-common"))
        cases.append((self.provider, None, "secret-shared = [", "secret-shared"))

        for provider, common, shared, secret in cases:
            with self.subTest(secret=secret):
                shared_path = self.shared_home / "config.toml"
                if shared is None:
                    shared_path.unlink(missing_ok=True)
                else:
                    shared_path.write_text(shared, encoding="utf-8")
                with mock.patch.dict(
                    os.environ, self.environment(), clear=True
                ), mock.patch.object(
                    codex_runtime.tempfile, "TemporaryDirectory"
                ) as temporary_directory:
                    with self.assertRaises(CcSwitchConfigError) as raised:
                        with materialize_codex_runtime(provider, common):
                            pass

                self.assertNotIn(secret, str(raised.exception))
                self.assertIsNone(raised.exception.__cause__)
                self.assertIsNone(raised.exception.__context__)
                temporary_directory.assert_not_called()

    def test_unsupported_auth_and_config_shapes_fail_before_temp_home(self):
        cases = (
            {"config": PROVIDER_CONFIG, "auth": {}},
            {
                "config": PROVIDER_CONFIG,
                "auth": {"OPENAI_API_KEY": "secret", "EXTRA": "secret"},
            },
            {
                "config": PROVIDER_CONFIG,
                "auth": {"OPENAI_API_KEY": 7},
            },
            {
                "config": PROVIDER_CONFIG,
                "auth": {"OPENAI_API_KEY": "secret\0value"},
            },
            {
                "config": 'model_provider = ""\n',
                "auth": {"OPENAI_API_KEY": "secret"},
            },
            {
                "config": (
                    'model_provider = "custom"\n'
                    "[model_providers.custom]\n"
                    "requires_openai_auth = false\n"
                ),
                "auth": {"OPENAI_API_KEY": "secret"},
            },
            {
                "config": (
                    'model_provider = "custom"\n'
                    "[model_providers.custom]\n"
                    "requires_openai_auth = true\n"
                    'env_key = "OPENAI_API_KEY"\n'
                ),
                "auth": {"OPENAI_API_KEY": "secret"},
            },
        )

        for index, settings in enumerate(cases):
            provider = SelectedProvider("id", "codex", "provider", settings)
            with self.subTest(index=index), mock.patch.dict(
                os.environ, self.environment(), clear=True
            ), mock.patch.object(
                codex_runtime.tempfile, "TemporaryDirectory"
            ) as temporary_directory:
                with self.assertRaises(CcSwitchConfigError) as raised:
                    with materialize_codex_runtime(provider, None):
                        pass

            self.assertIn("single OPENAI_API_KEY", str(raised.exception))
            self.assertNotIn("secret", str(raised.exception))
            self.assertIsNone(raised.exception.__cause__)
            self.assertIsNone(raised.exception.__context__)
            temporary_directory.assert_not_called()

    def test_missing_or_wrong_type_shared_homes_are_rejected(self):
        regular_file = self.root / "regular-file"
        regular_file.write_text("state\n", encoding="utf-8")
        missing = self.root / "missing"
        cases = (
            ({"CODEX_HOME": str(missing)}, "CODEX_HOME"),
            ({"CODEX_HOME": str(regular_file)}, "CODEX_HOME"),
            ({"CODEX_HOME": ""}, "CODEX_HOME"),
            (
                {
                    "CODEX_HOME": str(self.shared_home),
                    "CODEX_SQLITE_HOME": str(missing),
                },
                "CODEX_SQLITE_HOME",
            ),
            (
                {
                    "CODEX_HOME": str(self.shared_home),
                    "CODEX_SQLITE_HOME": str(regular_file),
                },
                "CODEX_SQLITE_HOME",
            ),
            (
                {
                    "CODEX_HOME": str(self.shared_home),
                    "CODEX_SQLITE_HOME": "",
                },
                "CODEX_SQLITE_HOME",
            ),
        )

        for environment, label in cases:
            with self.subTest(environment=environment), mock.patch.dict(
                os.environ, environment, clear=True
            ):
                with self.assertRaisesRegex(CcSwitchConfigError, label):
                    with materialize_codex_runtime(self.provider, None):
                        pass

        self.assertFalse(missing.exists())

    def test_default_shared_and_sqlite_home_are_explicit(self):
        default_home = self.root / ".codex"
        default_home.mkdir()

        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            codex_runtime.Path, "home", return_value=self.root
        ):
            with materialize_codex_runtime(self.provider, None) as runtime:
                self.assertEqual(
                    runtime.environment["CODEX_SQLITE_HOME"],
                    str(default_home.resolve()),
                )
                self.assertNotEqual(runtime.home, default_home)

    def test_allowlist_type_mismatches_fail_and_unknown_paths_stay_isolated(self):
        cases = (
            (DIRECTORY_SHARE_ALLOWLIST[0], "file"),
            (FILE_SHARE_ALLOWLIST[0], "directory"),
        )

        for name, kind in cases:
            with self.subTest(name=name):
                path = self.shared_home / name
                if kind == "file":
                    path.write_text("wrong\n", encoding="utf-8")
                else:
                    path.mkdir()
                with mock.patch.dict(
                    os.environ, self.environment(), clear=True
                ):
                    with self.assertRaises(CcSwitchConfigError):
                        with materialize_codex_runtime(self.provider, None):
                            pass
                if path.is_dir():
                    path.rmdir()
                else:
                    path.unlink()

        unknown = self.shared_home / "unknown-state"
        unknown.write_text("shared\n", encoding="utf-8")
        with mock.patch.dict(os.environ, self.environment(), clear=True):
            with materialize_codex_runtime(self.provider, None) as runtime:
                self.assertFalse((runtime.home / unknown.name).exists())

    def test_explicit_originator_is_preserved(self):
        environment = self.environment(
            CODEX_INTERNAL_ORIGINATOR_OVERRIDE="custom-client"
        )

        with mock.patch.dict(os.environ, environment, clear=True):
            with materialize_codex_runtime(self.provider, None) as runtime:
                self.assertEqual(
                    runtime.environment["CODEX_INTERNAL_ORIGINATOR_OVERRIDE"],
                    "custom-client",
                )

    def test_concurrent_runtime_homes_are_unique_and_clean_independently(self):
        with mock.patch.dict(os.environ, self.environment(), clear=True):
            with materialize_codex_runtime(self.provider, None) as first:
                first_home = first.home
                with materialize_codex_runtime(self.provider, None) as second:
                    second_home = second.home
                    self.assertNotEqual(first_home, second_home)
                    self.assertTrue(first_home.is_dir())
                    self.assertTrue(second_home.is_dir())
                self.assertTrue(first_home.is_dir())
                self.assertFalse(second_home.exists())
        self.assertFalse(first_home.exists())

    def test_runtime_is_cleaned_when_body_raises(self):
        runtime_home = None

        with mock.patch.dict(os.environ, self.environment(), clear=True):
            with self.assertRaisesRegex(RuntimeError, "body failure"):
                with materialize_codex_runtime(self.provider, None) as runtime:
                    runtime_home = runtime.home
                    raise RuntimeError("body failure")

        self.assertIsNotNone(runtime_home)
        self.assertFalse(runtime_home.exists())

    def test_partial_runtime_is_removed_after_sanitized_setup_failure(self):
        created_home = None

        def fail_after_write(path, content):
            nonlocal created_home
            created_home = path.parent
            path.write_text(content, encoding="utf-8")
            raise OSError("setup-secret")

        with mock.patch.dict(
            os.environ, self.environment(), clear=True
        ), mock.patch.object(
            codex_runtime, "_write_private_file", side_effect=fail_after_write
        ):
            with self.assertRaises(CcSwitchConfigError) as raised:
                with materialize_codex_runtime(self.provider, None):
                    pass

        self.assertIsNotNone(created_home)
        self.assertFalse(created_home.exists())
        self.assertNotIn("setup-secret", str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)
        self.assertIsNone(raised.exception.__context__)

    def test_cleanup_only_failure_is_sanitized_without_exception_chain(self):
        real_cleanup = tempfile.TemporaryDirectory.cleanup

        def fail_after_cleanup(directory):
            real_cleanup(directory)
            raise OSError("cleanup-secret")

        with mock.patch.dict(
            os.environ, self.environment(), clear=True
        ), mock.patch.object(
            codex_runtime.tempfile.TemporaryDirectory,
            "cleanup",
            autospec=True,
            side_effect=fail_after_cleanup,
        ):
            with self.assertRaises(CcSwitchConfigError) as raised:
                with materialize_codex_runtime(self.provider, None):
                    pass

        self.assertNotIn("cleanup-secret", str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)
        self.assertIsNone(raised.exception.__context__)

    def test_body_exception_wins_when_cleanup_also_fails(self):
        body_error = KeyboardInterrupt("body interrupt")
        real_cleanup = tempfile.TemporaryDirectory.cleanup

        def fail_after_cleanup(directory):
            real_cleanup(directory)
            raise OSError("cleanup-secret")

        with mock.patch.dict(
            os.environ, self.environment(), clear=True
        ), mock.patch.object(
            codex_runtime.tempfile.TemporaryDirectory,
            "cleanup",
            autospec=True,
            side_effect=fail_after_cleanup,
        ):
            with self.assertRaises(KeyboardInterrupt) as raised:
                with materialize_codex_runtime(self.provider, None):
                    raise body_error

        self.assertIs(raised.exception, body_error)


if __name__ == "__main__":
    unittest.main()
