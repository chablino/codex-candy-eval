import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

import cc_switch_config
from cc_switch_config import CcSwitchConfigError, SelectedProvider, load_provider


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


if __name__ == "__main__":
    unittest.main()
