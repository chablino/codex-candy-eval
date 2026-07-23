import json
import os
import signal
import sqlite3
import stat
import tempfile
import unittest
from contextlib import closing, contextmanager
from pathlib import Path
from unittest import mock

import cc_switch_config
from cc_switch_config import (
    CcSwitchConfigError,
    CodexProfileRuntime,
    ProviderRuntime,
    SelectedProvider,
    _sigterm_as_system_exit,
    _write_private_file,
    load_provider,
    materialize_codex_profile,
    materialize_provider,
    redact_text,
    use_codex_profile,
    use_provider,
)


CODEX_SETTINGS = {
    "config": 'model = "gpt-test"\n',
    "auth": {"OPENAI_API_KEY": "codex-test-secret"},
}
CLAUDE_SETTINGS = {
    "env": {"ANTHROPIC_AUTH_TOKEN": "claude-test-secret"},
    "model": "sonnet",
}


class ProviderDatabase:
    def __init__(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "cc-switch.db"

    def create_schema(self, schema=None):
        schema = schema or """
            CREATE TABLE providers (
                id TEXT NOT NULL,
                app_type TEXT NOT NULL,
                name TEXT NOT NULL,
                settings_config TEXT NOT NULL
            )
        """
        with closing(sqlite3.connect(self.path)) as connection:
            with connection:
                connection.execute(schema)

    def insert(self, provider_id, app_type, name, settings):
        payload = settings if isinstance(settings, str) else json.dumps(settings)
        with closing(sqlite3.connect(self.path)) as connection:
            with connection:
                connection.execute(
                    "INSERT INTO providers "
                    "(id, app_type, name, settings_config) VALUES (?, ?, ?, ?)",
                    (provider_id, app_type, name, payload),
                )

    def close(self):
        self.directory.cleanup()


class RecordingConnection:
    def __init__(self, connection):
        self.connection = connection
        self.statements = []
        self.closed = False

    def execute(self, statement, parameters=()):
        self.statements.append(statement)
        return self.connection.execute(statement, parameters)

    def close(self):
        self.closed = True
        self.connection.close()


class LoadProviderTests(unittest.TestCase):
    def setUp(self):
        self.database = ProviderDatabase()
        self.addCleanup(self.database.close)
        self.database.create_schema()

    def recreate_schema(self, schema):
        self.database.path.unlink()
        self.database.create_schema(schema)

    def test_id_wins_over_name(self):
        self.database.insert("target", "codex", "first", CODEX_SETTINGS)
        self.database.insert("other", "codex", "target", CODEX_SETTINGS)

        selected = load_provider("codex", "target", self.database.path)

        self.assertEqual((selected.provider_id, selected.name), ("target", "first"))

    def test_unique_name_is_scoped_to_app_type(self):
        self.database.insert("codex-id", "codex", "anyrouter", CODEX_SETTINGS)
        self.database.insert("claude-id", "claude", "anyrouter", CLAUDE_SETTINGS)

        selected = load_provider("codex", "anyrouter", self.database.path)

        self.assertEqual(selected.provider_id, "codex-id")
        self.assertEqual(selected.app_type, "codex")
        self.assertEqual(selected.settings, CODEX_SETTINGS)

    def test_selected_provider_repr_hides_settings(self):
        self.database.insert("codex-id", "codex", "provider", CODEX_SETTINGS)

        selected = load_provider("codex", "provider", self.database.path)

        self.assertIsInstance(selected, SelectedProvider)
        self.assertNotIn("codex-test-secret", repr(selected))

    def test_duplicate_name_lists_ids_without_settings(self):
        self.database.insert("second", "codex", "same", CODEX_SETTINGS)
        self.database.insert("first", "codex", "same", CODEX_SETTINGS)

        with self.assertRaises(CcSwitchConfigError) as raised:
            load_provider("codex", "same", self.database.path)

        message = str(raised.exception)
        self.assertIn("first", message)
        self.assertIn("second", message)
        self.assertLess(message.index("first"), message.index("second"))
        self.assertNotIn("codex-test-secret", message)

    def test_missing_name_lists_only_requested_app_metadata(self):
        self.database.insert("codex-b", "codex", "Beta", CODEX_SETTINGS)
        self.database.insert("codex-a", "codex", "Alpha", CODEX_SETTINGS)
        self.database.insert("claude-id", "claude", "Hidden", CLAUDE_SETTINGS)

        with self.assertRaises(CcSwitchConfigError) as raised:
            load_provider("codex", "missing", self.database.path)

        message = str(raised.exception)
        self.assertIn("Alpha (codex-a)", message)
        self.assertIn("Beta (codex-b)", message)
        self.assertLess(message.index("Alpha"), message.index("Beta"))
        self.assertNotIn("Hidden", message)
        self.assertNotIn("claude-id", message)
        self.assertNotIn("codex-test-secret", message)

    def test_empty_app_list_reports_none(self):
        with self.assertRaises(CcSwitchConfigError) as raised:
            load_provider("claude", "missing", self.database.path)

        self.assertIn("available names/IDs: none", str(raised.exception))

    def test_duplicate_and_missing_paths_never_query_settings(self):
        self.database.insert("first", "codex", "same", CODEX_SETTINGS)
        self.database.insert("second", "codex", "same", CODEX_SETTINGS)
        real_connect = sqlite3.connect

        for selector in ("same", "missing"):
            with self.subTest(selector=selector):
                connections = []

                def recording_connect(*args, **kwargs):
                    connection = RecordingConnection(real_connect(*args, **kwargs))
                    connections.append(connection)
                    return connection

                with mock.patch.object(
                    cc_switch_config.sqlite3, "connect", recording_connect
                ):
                    with self.assertRaises(CcSwitchConfigError):
                        load_provider("codex", selector, self.database.path)

                selects = [
                    statement
                    for connection in connections
                    for statement in connection.statements
                    if statement.lstrip().upper().startswith("SELECT")
                ]
                self.assertTrue(selects)
                self.assertTrue(
                    all("settings_config" not in statement for statement in selects)
                )
                self.assertTrue(all(connection.closed for connection in connections))

    def test_success_uses_two_read_only_connections_and_closes_them(self):
        self.database.insert("codex-id", "codex", "provider", CODEX_SETTINGS)
        real_connect = sqlite3.connect
        calls = []
        connections = []

        def recording_connect(*args, **kwargs):
            calls.append((args, kwargs))
            connection = RecordingConnection(real_connect(*args, **kwargs))
            connections.append(connection)
            return connection

        with mock.patch.object(cc_switch_config.sqlite3, "connect", recording_connect):
            load_provider("codex", "provider", self.database.path)

        self.assertEqual(len(calls), 2)
        self.assertTrue(all(args[0].startswith("file:") for args, _ in calls))
        self.assertTrue(all("mode=ro" in args[0] for args, _ in calls))
        self.assertTrue(all(kwargs == {"uri": True} for _, kwargs in calls))
        self.assertTrue(all(connection.closed for connection in connections))

    def test_missing_database_is_not_created(self):
        missing = Path(self.database.directory.name) / "missing.db"

        with self.assertRaisesRegex(CcSwitchConfigError, "not a file"):
            load_provider("codex", "anything", missing)

        self.assertFalse(missing.exists())

    def test_missing_providers_table_is_reported(self):
        self.recreate_schema("CREATE TABLE unrelated (id TEXT)")

        with self.assertRaisesRegex(CcSwitchConfigError, "providers table"):
            load_provider("codex", "anything", self.database.path)

    def test_missing_required_columns_are_reported(self):
        self.recreate_schema("CREATE TABLE providers (id TEXT, name TEXT)")

        with self.assertRaises(CcSwitchConfigError) as raised:
            load_provider("codex", "anything", self.database.path)

        message = str(raised.exception)
        self.assertIn("app_type", message)
        self.assertIn("settings_config", message)

    def test_invalid_json_is_sanitized_without_exception_chain(self):
        payload = "not-json-codex-test-secret"
        self.database.insert("bad", "codex", "bad", payload)

        with self.assertRaises(CcSwitchConfigError) as raised:
            load_provider("codex", "bad", self.database.path)

        self.assertIn("JSON object", str(raised.exception))
        self.assertNotIn(payload, str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)
        self.assertIsNone(raised.exception.__context__)

    def test_nonstandard_json_constants_are_rejected(self):
        for index, constant in enumerate(("NaN", "Infinity", "-Infinity")):
            with self.subTest(constant=constant):
                provider_id = f"bad-{index}"
                payload = (
                    '{"config":"model=x","auth":{},"value":' + constant + "}"
                )
                self.database.insert(provider_id, "codex", provider_id, payload)
                with self.assertRaises(CcSwitchConfigError):
                    load_provider("codex", provider_id, self.database.path)

    def test_payload_must_be_text_containing_an_object(self):
        with closing(sqlite3.connect(self.database.path)) as connection:
            with connection:
                connection.execute(
                    "INSERT INTO providers VALUES (?, ?, ?, ?)",
                    ("binary", "claude", "binary", sqlite3.Binary(b"{}")),
                )
        self.database.insert("array", "claude", "array", '["secret-value"]')

        for selector in ("binary", "array"):
            with self.subTest(selector=selector):
                with self.assertRaisesRegex(CcSwitchConfigError, "JSON object"):
                    load_provider("claude", selector, self.database.path)

    def test_codex_config_and_auth_shapes_are_validated_without_leakage(self):
        secret = "shape-secret-value"
        cases = (
            ("bad-config", {"config": {"token": secret}, "auth": {}}, "config"),
            ("bad-auth", {"config": "model=x", "auth": secret}, "auth"),
        )
        for provider_id, settings, field in cases:
            with self.subTest(provider_id=provider_id):
                self.database.insert(provider_id, "codex", provider_id, settings)
                with self.assertRaises(CcSwitchConfigError) as raised:
                    load_provider("codex", provider_id, self.database.path)
                self.assertIn(field, str(raised.exception))
                self.assertNotIn(secret, str(raised.exception))

    def test_claude_env_must_be_an_object_when_present(self):
        secret = "claude-shape-secret"
        self.database.insert("bad-env", "claude", "bad-env", {"env": secret})

        with self.assertRaises(CcSwitchConfigError) as raised:
            load_provider("claude", "bad-env", self.database.path)

        self.assertIn("env", str(raised.exception))
        self.assertNotIn(secret, str(raised.exception))

    def test_claude_env_may_be_absent(self):
        self.database.insert("claude-id", "claude", "provider", {"model": "sonnet"})

        selected = load_provider("claude", "provider", self.database.path)

        self.assertEqual(selected.settings, {"model": "sonnet"})

    def test_non_text_metadata_is_rejected_without_reading_payload(self):
        secret = "metadata-secret-value"
        with closing(sqlite3.connect(self.database.path)) as connection:
            with connection:
                connection.execute(
                    "INSERT INTO providers VALUES (?, ?, ?, ?)",
                    (
                        "binary-name",
                        "codex",
                        sqlite3.Binary(b"bad-name"),
                        json.dumps({"config": secret, "auth": {}}),
                    ),
                )

        with self.assertRaises(CcSwitchConfigError) as raised:
            load_provider("codex", "binary-name", self.database.path)

        self.assertIn("metadata", str(raised.exception))
        self.assertNotIn(secret, str(raised.exception))

    def test_unsupported_app_type_is_rejected_before_database_access(self):
        with mock.patch.object(cc_switch_config.sqlite3, "connect") as connect:
            with self.assertRaisesRegex(CcSwitchConfigError, "unsupported app type"):
                load_provider("opencode", "anything", self.database.path)

        connect.assert_not_called()


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)

    def test_codex_runtime_files_modes_and_environment(self):
        provider = SelectedProvider("id", "codex", "anyrouter", CODEX_SETTINGS)

        with mock.patch.dict(
            os.environ, {"CC_SWITCH_TEST_BASE": "inherited"}, clear=True
        ):
            with materialize_provider(provider) as runtime:
                root = Path(runtime.environment["CODEX_HOME"])
                self.assertEqual(
                    (root / "config.toml").read_text(encoding="utf-8"),
                    CODEX_SETTINGS["config"],
                )
                self.assertEqual(
                    json.loads((root / "auth.json").read_text(encoding="utf-8")),
                    CODEX_SETTINGS["auth"],
                )
                self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
                self.assertEqual(
                    stat.S_IMODE((root / "config.toml").stat().st_mode), 0o600
                )
                self.assertEqual(
                    stat.S_IMODE((root / "auth.json").stat().st_mode), 0o600
                )
                self.assertEqual(
                    runtime.environment["CC_SWITCH_TEST_BASE"], "inherited"
                )
                self.assertIn("codex-test-secret", runtime.secrets)
            self.assertFalse(root.exists())
            self.assertEqual(
                dict(os.environ), {"CC_SWITCH_TEST_BASE": "inherited"}
            )

    def test_claude_runtime_writes_complete_settings_and_inherits_environment(self):
        provider = SelectedProvider("id", "claude", "anyrouter", CLAUDE_SETTINGS)

        with mock.patch.dict(os.environ, {"CC_SWITCH_TEST_BASE": "inherited"}, clear=True):
            with materialize_provider(provider) as runtime:
                root = Path(runtime.environment["CLAUDE_CONFIG_DIR"])
                settings_path = root / "settings.json"
                self.assertEqual(
                    json.loads(settings_path.read_text(encoding="utf-8")),
                    CLAUDE_SETTINGS,
                )
                self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE(settings_path.stat().st_mode), 0o600)
                self.assertEqual(
                    runtime.environment["CC_SWITCH_TEST_BASE"], "inherited"
                )
                self.assertIn("claude-test-secret", runtime.secrets)
            self.assertFalse(root.exists())

    def test_runtime_does_not_mutate_parent_environment(self):
        provider = SelectedProvider("id", "codex", "provider", CODEX_SETTINGS)
        before = dict(os.environ)

        with materialize_provider(provider) as runtime:
            self.assertNotEqual(runtime.environment.get("CODEX_HOME"), before.get("CODEX_HOME"))
            self.assertEqual(dict(os.environ), before)

        self.assertEqual(dict(os.environ), before)

    def test_runtime_is_cleaned_when_body_raises(self):
        provider = SelectedProvider("id", "claude", "provider", {"env": {}})
        root = None

        with self.assertRaisesRegex(RuntimeError, "body failure"):
            with materialize_provider(provider) as runtime:
                root = Path(runtime.environment["CLAUDE_CONFIG_DIR"])
                raise RuntimeError("body failure")

        self.assertIsNotNone(root)
        self.assertFalse(root.exists())

    def test_setup_failure_is_sanitized_and_does_not_yield(self):
        token = "setup-failure-secret-token"
        provider = SelectedProvider(
            "id", "codex", "provider", {"config": "model=x", "auth": {"token": token}}
        )
        yielded = False

        with mock.patch.object(
            cc_switch_config.os,
            "open",
            side_effect=OSError(f"cannot write {token}"),
        ):
            with self.assertRaises(CcSwitchConfigError) as raised:
                with materialize_provider(provider):
                    yielded = True

        self.assertFalse(yielded)
        self.assertNotIn(token, str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)
        self.assertIsNone(raised.exception.__context__)

    def test_body_exception_wins_when_cleanup_also_fails(self):
        provider = SelectedProvider("id", "claude", "provider", {"env": {}})
        body_error = KeyboardInterrupt("body interrupt")
        cleanup_error = OSError("cleanup failure")
        real_cleanup = cc_switch_config.tempfile.TemporaryDirectory.cleanup

        def failing_cleanup(temporary_directory):
            real_cleanup(temporary_directory)
            raise cleanup_error

        with mock.patch.object(
            cc_switch_config.tempfile.TemporaryDirectory,
            "cleanup",
            autospec=True,
            side_effect=failing_cleanup,
        ) as cleanup:
            with self.assertRaises(KeyboardInterrupt) as raised:
                with materialize_provider(provider):
                    raise body_error

        self.assertIs(raised.exception, body_error)
        cleanup.assert_called_once()

    def test_cleanup_only_failure_is_sanitized_without_exception_chain(self):
        token = "cleanup-failure-secret-token"
        provider = SelectedProvider(
            "id", "claude", "provider", {"env": {"TOKEN": token}}
        )
        real_cleanup = cc_switch_config.tempfile.TemporaryDirectory.cleanup

        def failing_cleanup(temporary_directory):
            real_cleanup(temporary_directory)
            raise OSError(f"cleanup failed with {token}")

        with mock.patch.object(
            cc_switch_config.tempfile.TemporaryDirectory,
            "cleanup",
            autospec=True,
            side_effect=failing_cleanup,
        ):
            with self.assertRaises(CcSwitchConfigError) as raised:
                with materialize_provider(provider):
                    pass

        self.assertNotIn(token, str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)
        self.assertIsNone(raised.exception.__context__)

    def test_private_file_descriptor_closes_when_fchmod_fails(self):
        path = Path(self.temp_dir.name) / "fchmod-failure"
        real_open = cc_switch_config.os.open
        descriptors = []

        def recording_open(*args, **kwargs):
            descriptor = real_open(*args, **kwargs)
            descriptors.append(descriptor)
            return descriptor

        with mock.patch.object(
            cc_switch_config.os, "open", side_effect=recording_open
        ), mock.patch.object(
            cc_switch_config.os, "fchmod", side_effect=OSError("fchmod failure")
        ):
            with self.assertRaises(OSError):
                _write_private_file(path, "content")

        with self.assertRaises(OSError):
            cc_switch_config.os.fstat(descriptors[0])

    def test_private_file_descriptor_closes_when_write_fails(self):
        path = Path(self.temp_dir.name) / "write-failure"
        real_open = cc_switch_config.os.open
        descriptors = []

        def recording_open(*args, **kwargs):
            descriptor = real_open(*args, **kwargs)
            descriptors.append(descriptor)
            return descriptor

        with mock.patch.object(
            cc_switch_config.os, "open", side_effect=recording_open
        ):
            with self.assertRaises(TypeError):
                _write_private_file(path, object())

        with self.assertRaises(OSError):
            cc_switch_config.os.fstat(descriptors[0])

    def test_redaction_handles_overlap_multiline_and_marker(self):
        self.assertEqual(
            redact_text("x acted y", ("act", "acted")), "x <redacted> y"
        )
        self.assertEqual(
            redact_text("before line-one\nline-two after", (" line-one\nline-two ",)),
            "before<redacted>after",
        )
        self.assertEqual(
            redact_text("top-secret", ("acted", "top-secret")), "<redacted>"
        )
        self.assertEqual(redact_text("unchanged", ("",)), "unchanged")

    def test_runtime_repr_hides_secrets_and_redact_method_works(self):
        token = "runtime-secret-token"
        runtime = ProviderRuntime(
            "id", "provider", {"CODEX_HOME": "/private/runtime"}, (token,)
        )

        self.assertNotIn(token, repr(runtime))
        self.assertEqual(runtime.redact(f"error: {token}"), "error: <redacted>")

    def test_sigterm_context_restores_previous_handler_and_raises_exit_code(self):
        previous = object()
        installed = []

        def record_signal(signum, handler):
            installed.append((signum, handler))

        with mock.patch.object(
            cc_switch_config.signal, "getsignal", return_value=previous
        ), mock.patch.object(cc_switch_config.signal, "signal", side_effect=record_signal):
            with self.assertRaises(SystemExit) as raised:
                with _sigterm_as_system_exit():
                    installed[0][1](signal.SIGTERM, None)

        self.assertEqual(raised.exception.code, 128 + signal.SIGTERM)
        self.assertEqual(installed[0][0], signal.SIGTERM)
        self.assertEqual(installed[1], (signal.SIGTERM, previous))

    def test_use_provider_installs_sigterm_handler_before_materialization(self):
        provider = SelectedProvider("id", "claude", "provider", {"env": {}})
        previous = object()
        current_handler = {"value": previous}

        def fake_signal(_signum, handler):
            current_handler["value"] = handler

        @contextmanager
        def interrupt_during_materialization(_provider):
            current_handler["value"](signal.SIGTERM, None)
            yield  # pragma: no cover

        with mock.patch.object(
            cc_switch_config, "load_provider", return_value=provider
        ) as load, mock.patch.object(
            cc_switch_config, "materialize_provider", interrupt_during_materialization
        ), mock.patch.object(
            cc_switch_config.signal, "getsignal", return_value=previous
        ), mock.patch.object(
            cc_switch_config.signal, "signal", side_effect=fake_signal
        ):
            with self.assertRaises(SystemExit) as raised:
                with use_provider("claude", "provider", Path("unused.db")):
                    pass

        self.assertEqual(raised.exception.code, 128 + signal.SIGTERM)
        self.assertIs(current_handler["value"], previous)
        load.assert_called_once_with("claude", "provider", Path("unused.db"))

    def test_use_provider_sigterm_cleans_runtime(self):
        provider = SelectedProvider("id", "claude", "provider", {"env": {}})
        previous = signal.getsignal(signal.SIGTERM)
        root = None

        with mock.patch.object(
            cc_switch_config, "load_provider", return_value=provider
        ):
            with self.assertRaises(SystemExit) as raised:
                with use_provider("claude", "provider") as runtime:
                    root = Path(runtime.environment["CLAUDE_CONFIG_DIR"])
                    handler = signal.getsignal(signal.SIGTERM)
                    handler(signal.SIGTERM, None)

        self.assertEqual(raised.exception.code, 128 + signal.SIGTERM)
        self.assertIsNotNone(root)
        self.assertFalse(root.exists())
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous)

    def test_use_provider_rejects_windows_before_database_access(self):
        with mock.patch.object(cc_switch_config.os, "name", "nt"), mock.patch.object(
            cc_switch_config, "load_provider"
        ) as load:
            with self.assertRaisesRegex(CcSwitchConfigError, "macOS/Linux"):
                with use_provider("codex", "provider", Path("never.db")):
                    pass

        load.assert_not_called()


class CodexProfileRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.codex_home = self.root / "codex-home"
        self.codex_home.mkdir()
        self.provider = SelectedProvider(
            "codex-id", "codex", "jianzhile", CODEX_SETTINGS
        )

    def profile_paths(self):
        return list(self.codex_home.glob("cc-switch-*.config.toml"))

    def test_profile_uses_shared_home_and_child_only_environment(self):
        default_config = self.codex_home / "config.toml"
        default_auth = self.codex_home / "auth.json"
        session = self.codex_home / "sessions" / "shared-session.jsonl"
        default_config.write_text("default-config\n", encoding="utf-8")
        default_auth.write_text("default-auth\n", encoding="utf-8")
        session.parent.mkdir()
        session.write_text("shared-session\n", encoding="utf-8")
        parent_environment = {
            "CODEX_HOME": str(self.codex_home),
            "CODEX_SQLITE_HOME": str(self.root / "sqlite-state"),
            "OPENAI_API_KEY": "parent-secret",
            "INHERITED_VALUE": "kept",
        }

        with mock.patch.dict(os.environ, parent_environment, clear=True):
            with materialize_codex_profile(self.provider) as runtime:
                profile_path = self.codex_home / (
                    f"{runtime.profile_name}.config.toml"
                )

                self.assertIsInstance(runtime, CodexProfileRuntime)
                self.assertRegex(runtime.profile_name, r"^cc-switch-[0-9a-f]{32}$")
                self.assertEqual(
                    profile_path.read_text(encoding="utf-8"),
                    CODEX_SETTINGS["config"],
                )
                self.assertEqual(stat.S_IMODE(profile_path.stat().st_mode), 0o600)
                self.assertEqual(
                    runtime.environment["CODEX_HOME"], str(self.codex_home)
                )
                self.assertEqual(
                    runtime.environment["CODEX_SQLITE_HOME"],
                    str(self.root / "sqlite-state"),
                )
                self.assertEqual(runtime.environment["INHERITED_VALUE"], "kept")
                self.assertEqual(
                    runtime.environment["OPENAI_API_KEY"], "codex-test-secret"
                )
                self.assertEqual(
                    runtime.environment["CODEX_INTERNAL_ORIGINATOR_OVERRIDE"],
                    "codex-tui",
                )
                self.assertEqual(dict(os.environ), parent_environment)
                self.assertNotIn("codex-test-secret", repr(runtime))

            self.assertFalse(profile_path.exists())
            self.assertEqual(dict(os.environ), parent_environment)

        self.assertEqual(default_config.read_text(encoding="utf-8"), "default-config\n")
        self.assertEqual(default_auth.read_text(encoding="utf-8"), "default-auth\n")
        self.assertEqual(session.read_text(encoding="utf-8"), "shared-session\n")

    def test_explicit_originator_is_preserved(self):
        environment = {
            "CODEX_HOME": str(self.codex_home),
            "CODEX_INTERNAL_ORIGINATOR_OVERRIDE": "custom-client",
        }

        with mock.patch.dict(os.environ, environment, clear=True):
            with materialize_codex_profile(self.provider) as runtime:
                self.assertEqual(
                    runtime.environment["CODEX_INTERNAL_ORIGINATOR_OVERRIDE"],
                    "custom-client",
                )

    def test_default_codex_home_is_shared_and_made_explicit_for_child(self):
        default_home = self.root / ".codex"
        default_home.mkdir()

        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            cc_switch_config.Path, "home", return_value=self.root
        ):
            with materialize_codex_profile(self.provider) as runtime:
                profile_path = default_home / f"{runtime.profile_name}.config.toml"
                self.assertTrue(profile_path.is_file())
                self.assertEqual(runtime.environment["CODEX_HOME"], str(default_home))

        self.assertFalse(profile_path.exists())

    def test_concurrent_profiles_are_unique_and_independently_cleaned(self):
        with mock.patch.dict(
            os.environ, {"CODEX_HOME": str(self.codex_home)}, clear=True
        ):
            with materialize_codex_profile(self.provider) as first:
                first_path = self.codex_home / f"{first.profile_name}.config.toml"
                with materialize_codex_profile(self.provider) as second:
                    second_path = self.codex_home / f"{second.profile_name}.config.toml"
                    self.assertNotEqual(first.profile_name, second.profile_name)
                    self.assertTrue(first_path.is_file())
                    self.assertTrue(second_path.is_file())

                self.assertTrue(first_path.is_file())
                self.assertFalse(second_path.exists())

        self.assertFalse(first_path.exists())
        self.assertEqual(self.profile_paths(), [])

    def test_profile_is_cleaned_when_body_raises(self):
        profile_path = None

        with mock.patch.dict(
            os.environ, {"CODEX_HOME": str(self.codex_home)}, clear=True
        ):
            with self.assertRaisesRegex(RuntimeError, "body failure"):
                with materialize_codex_profile(self.provider) as runtime:
                    profile_path = self.codex_home / (
                        f"{runtime.profile_name}.config.toml"
                    )
                    raise RuntimeError("body failure")

        self.assertIsNotNone(profile_path)
        self.assertFalse(profile_path.exists())

    def test_invalid_auth_is_rejected_before_profile_creation(self):
        invalid_auth_values = (
            {1: "value"},
            {"": "value"},
            {"BAD=KEY": "value"},
            {"BAD\0KEY": "value"},
            {"TOKEN": 1},
            {"TOKEN": "secret\0value"},
        )

        with mock.patch.dict(
            os.environ, {"CODEX_HOME": str(self.codex_home)}, clear=True
        ):
            for index, auth in enumerate(invalid_auth_values):
                with self.subTest(index=index):
                    provider = SelectedProvider(
                        "id", "codex", "provider", {"config": "model=x", "auth": auth}
                    )
                    with self.assertRaises(CcSwitchConfigError) as raised:
                        with materialize_codex_profile(provider):
                            pass

                    self.assertIn("auth", str(raised.exception))
                    self.assertNotIn("secret", str(raised.exception))
                    self.assertIsNone(raised.exception.__cause__)
                    self.assertIsNone(raised.exception.__context__)
                    self.assertEqual(self.profile_paths(), [])

    def test_invalid_codex_home_is_rejected_without_creating_it(self):
        missing = self.root / "missing-home"
        regular_file = self.root / "not-a-directory"
        regular_file.write_text("state\n", encoding="utf-8")

        for invalid_home in (missing, regular_file):
            with self.subTest(path=invalid_home), mock.patch.dict(
                os.environ, {"CODEX_HOME": str(invalid_home)}, clear=True
            ):
                with self.assertRaises(CcSwitchConfigError) as raised:
                    with materialize_codex_profile(self.provider):
                        pass

                self.assertIn("CODEX_HOME", str(raised.exception))
                self.assertIsNone(raised.exception.__cause__)
                self.assertIsNone(raised.exception.__context__)

        self.assertFalse(missing.exists())
        self.assertTrue(regular_file.is_file())

    def test_partial_profile_is_removed_after_sanitized_setup_failure(self):
        token = "profile-setup-secret"

        def fail_after_write(path, content):
            path.write_text(content, encoding="utf-8")
            raise OSError(token)

        with mock.patch.dict(
            os.environ, {"CODEX_HOME": str(self.codex_home)}, clear=True
        ), mock.patch.object(
            cc_switch_config, "_write_private_file", side_effect=fail_after_write
        ):
            with self.assertRaises(CcSwitchConfigError) as raised:
                with materialize_codex_profile(self.provider):
                    pass

        self.assertNotIn(token, str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)
        self.assertIsNone(raised.exception.__context__)
        self.assertEqual(self.profile_paths(), [])

    def test_cleanup_only_failure_is_sanitized_without_exception_chain(self):
        token = "profile-cleanup-secret"
        real_unlink = Path.unlink

        def fail_after_unlink(path, *args, **kwargs):
            real_unlink(path, *args, **kwargs)
            raise OSError(token)

        with mock.patch.dict(
            os.environ, {"CODEX_HOME": str(self.codex_home)}, clear=True
        ), mock.patch.object(
            cc_switch_config.Path,
            "unlink",
            autospec=True,
            side_effect=fail_after_unlink,
        ):
            with self.assertRaises(CcSwitchConfigError) as raised:
                with materialize_codex_profile(self.provider):
                    pass

        self.assertNotIn(token, str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)
        self.assertIsNone(raised.exception.__context__)
        self.assertEqual(self.profile_paths(), [])

    def test_body_exception_wins_when_profile_cleanup_also_fails(self):
        body_error = KeyboardInterrupt("body interrupt")
        real_unlink = Path.unlink

        def fail_after_unlink(path, *args, **kwargs):
            real_unlink(path, *args, **kwargs)
            raise OSError("cleanup failure")

        with mock.patch.dict(
            os.environ, {"CODEX_HOME": str(self.codex_home)}, clear=True
        ), mock.patch.object(
            cc_switch_config.Path,
            "unlink",
            autospec=True,
            side_effect=fail_after_unlink,
        ):
            with self.assertRaises(KeyboardInterrupt) as raised:
                with materialize_codex_profile(self.provider):
                    raise body_error

        self.assertIs(raised.exception, body_error)
        self.assertEqual(self.profile_paths(), [])

    def test_use_codex_profile_selects_codex_and_sigterm_cleans_profile(self):
        previous = signal.getsignal(signal.SIGTERM)
        profile_path = None
        database_path = self.root / "unused.db"

        with mock.patch.dict(
            os.environ, {"CODEX_HOME": str(self.codex_home)}, clear=True
        ), mock.patch.object(
            cc_switch_config, "load_provider", return_value=self.provider
        ) as load:
            with self.assertRaises(SystemExit) as raised:
                with use_codex_profile("jianzhile", database_path) as runtime:
                    profile_path = self.codex_home / (
                        f"{runtime.profile_name}.config.toml"
                    )
                    handler = signal.getsignal(signal.SIGTERM)
                    handler(signal.SIGTERM, None)

        self.assertEqual(raised.exception.code, 128 + signal.SIGTERM)
        self.assertIsNotNone(profile_path)
        self.assertFalse(profile_path.exists())
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous)
        load.assert_called_once_with("codex", "jianzhile", database_path)

    def test_use_codex_profile_rejects_windows_before_database_access(self):
        with mock.patch.object(cc_switch_config.os, "name", "nt"), mock.patch.object(
            cc_switch_config, "load_provider"
        ) as load:
            with self.assertRaisesRegex(CcSwitchConfigError, "macOS/Linux"):
                with use_codex_profile("provider", Path("never.db")):
                    pass

        load.assert_not_called()




if __name__ == "__main__":
    unittest.main()
