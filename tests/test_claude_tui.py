import importlib.util
import contextlib
import io
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import claude_tui
from cc_switch_config import CcSwitchConfigError


class LauncherApiTests(unittest.TestCase):
    def test_launcher_module_exists(self):
        self.assertIsNotNone(importlib.util.find_spec("claude_tui"))

    def test_argument_parser_is_public(self):
        self.assertTrue(hasattr(claude_tui, "parse_args"))

    def test_main_is_public(self):
        self.assertTrue(hasattr(claude_tui, "main"))


class ParserTests(unittest.TestCase):
    def test_selector_is_required(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                claude_tui.parse_args([])

        self.assertEqual(raised.exception.code, 2)

    def test_separator_is_removed_and_arguments_keep_order(self):
        arguments = claude_tui.parse_args(
            [
                "--cc-switch-config",
                "anyrouter",
                "--",
                "--resume",
                "session-id",
                "--model",
                "sonnet",
                "--effort",
                "high",
            ]
        )

        self.assertEqual(arguments.cc_switch_config, "anyrouter")
        self.assertEqual(
            arguments.claude_args,
            [
                "--resume",
                "session-id",
                "--model",
                "sonnet",
                "--effort",
                "high",
            ],
        )

    def test_no_forwarded_arguments_is_supported(self):
        arguments = claude_tui.parse_args(["--cc-switch-config", "provider-id"])

        self.assertEqual(arguments.claude_args, [])

    def test_forwarded_claude_arguments_keep_order_without_separator(self):
        arguments = claude_tui.parse_args(
            [
                "--cc-switch-config",
                "provider-id",
                "--continue",
                "--verbose",
            ]
        )

        self.assertEqual(arguments.claude_args, ["--continue", "--verbose"])

    def test_option_values_keep_order_without_separator(self):
        arguments = claude_tui.parse_args(
            [
                "--cc-switch-config",
                "provider-id",
                "--resume",
                "session-id",
                "--model",
                "sonnet",
            ]
        )

        self.assertEqual(
            arguments.claude_args,
            ["--resume", "session-id", "--model", "sonnet"],
        )

    def test_forwarded_launcher_owned_settings_forms_are_rejected(self):
        cases = (
            ["--settings", "/tmp/other.json"],
            ["--settings=/tmp/other.json"],
            ["--setting-sources", "user"],
            ["--setting-sources=user"],
        )
        for claude_args in cases:
            with self.subTest(claude_args=claude_args), contextlib.redirect_stderr(
                io.StringIO()
            ):
                with self.assertRaises(SystemExit):
                    claude_tui.parse_args(
                        ["--cc-switch-config", "anyrouter", "--", *claude_args]
                    )


class MainTests(unittest.TestCase):
    def setUp(self):
        self.environment = {
            "CLAUDE_CONFIG_DIR": "/Users/chablin/.claude",
            "ANTHROPIC_BASE_URL": "https://provider.example/v1",
            "ANTHROPIC_AUTH_TOKEN": "provider-secret",
        }
        self.settings_path = Path("/private/tmp/claude-settings/settings.json")
        self.runtime = SimpleNamespace(
            provider_id="provider-id",
            provider_name="anyrouter",
            settings_path=self.settings_path,
            environment=self.environment,
        )

    def runtime_context(self):
        manager = mock.MagicMock()
        manager.__enter__.return_value = self.runtime
        manager.__exit__.return_value = False
        return manager

    def test_launches_resume_with_selected_settings_and_returns_child_code(self):
        manager = self.runtime_context()
        completed = mock.Mock(returncode=17)
        forwarded = ["--resume", "session-id", "--model", "sonnet"]

        with mock.patch.object(
            claude_tui.shutil, "which", return_value="/usr/local/bin/claude"
        ) as which, mock.patch.object(
            claude_tui, "use_claude_profile", return_value=manager
        ) as use_profile, mock.patch.object(
            claude_tui.subprocess, "run", return_value=completed
        ) as run:
            result = claude_tui.main(
                ["--cc-switch-config", "anyrouter", "--", *forwarded]
            )

        self.assertEqual(result, 17)
        which.assert_called_once_with("claude")
        use_profile.assert_called_once_with("anyrouter")
        run.assert_called_once_with(
            [
                "/usr/local/bin/claude",
                "--setting-sources",
                "project,local",
                "--settings",
                str(self.settings_path),
                *forwarded,
            ],
            env=self.environment,
        )
        self.assertNotIn("provider-secret", run.call_args.args[0])
        manager.__enter__.assert_called_once_with()
        manager.__exit__.assert_called_once()

    def test_launches_without_forwarded_arguments(self):
        manager = self.runtime_context()

        with mock.patch.object(
            claude_tui.shutil, "which", return_value="/bin/claude"
        ), mock.patch.object(
            claude_tui, "use_claude_profile", return_value=manager
        ), mock.patch.object(
            claude_tui.subprocess,
            "run",
            return_value=mock.Mock(returncode=0),
        ) as run:
            result = claude_tui.main(["--cc-switch-config", "provider-id"])

        self.assertEqual(result, 0)
        run.assert_called_once_with(
            [
                "/bin/claude",
                "--setting-sources",
                "project,local",
                "--settings",
                str(self.settings_path),
            ],
            env=self.environment,
        )

    def test_missing_claude_returns_two_before_provider_selection(self):
        stderr = io.StringIO()

        with mock.patch.object(
            claude_tui.shutil, "which", return_value=None
        ), mock.patch.object(
            claude_tui, "use_claude_profile"
        ) as use_profile, mock.patch.object(
            claude_tui.subprocess, "run"
        ) as run, contextlib.redirect_stderr(stderr):
            result = claude_tui.main(["--cc-switch-config", "anyrouter"])

        self.assertEqual(result, 2)
        self.assertIn("Claude Code", stderr.getvalue())
        use_profile.assert_not_called()
        run.assert_not_called()

    def test_configuration_error_returns_two_before_child_launch(self):
        token = "provider-error-secret"
        manager = self.runtime_context()
        manager.__enter__.side_effect = CcSwitchConfigError(
            "failed to load selected provider"
        )
        stderr = io.StringIO()

        with mock.patch.object(
            claude_tui.shutil, "which", return_value="/bin/claude"
        ), mock.patch.object(
            claude_tui, "use_claude_profile", return_value=manager
        ), mock.patch.object(
            claude_tui.subprocess, "run"
        ) as run, contextlib.redirect_stderr(stderr):
            result = claude_tui.main(["--cc-switch-config", "anyrouter"])

        self.assertEqual(result, 2)
        self.assertIn("failed to load selected provider", stderr.getvalue())
        self.assertNotIn(token, stderr.getvalue())
        run.assert_not_called()

    def test_cleanup_error_returns_two(self):
        manager = self.runtime_context()
        manager.__exit__.side_effect = CcSwitchConfigError(
            "failed to clean up selected Claude profile"
        )
        stderr = io.StringIO()

        with mock.patch.object(
            claude_tui.shutil, "which", return_value="/bin/claude"
        ), mock.patch.object(
            claude_tui, "use_claude_profile", return_value=manager
        ), mock.patch.object(
            claude_tui.subprocess,
            "run",
            return_value=mock.Mock(returncode=0),
        ), contextlib.redirect_stderr(stderr):
            result = claude_tui.main(["--cc-switch-config", "anyrouter"])

        self.assertEqual(result, 2)
        self.assertIn("clean up", stderr.getvalue())

    def test_executable_launch_error_is_sanitized_and_returns_two(self):
        token = "launch-error-secret"
        manager = self.runtime_context()
        stderr = io.StringIO()

        with mock.patch.object(
            claude_tui.shutil, "which", return_value="/bin/claude"
        ), mock.patch.object(
            claude_tui, "use_claude_profile", return_value=manager
        ), mock.patch.object(
            claude_tui.subprocess, "run", side_effect=OSError(token)
        ), contextlib.redirect_stderr(stderr):
            result = claude_tui.main(["--cc-switch-config", "anyrouter"])

        self.assertEqual(result, 2)
        self.assertIn("failed to launch Claude Code", stderr.getvalue())
        self.assertNotIn(token, stderr.getvalue())
        manager.__exit__.assert_called_once()

    def test_launcher_owned_argument_stops_before_all_runtime_side_effects(self):
        with mock.patch.object(claude_tui.shutil, "which") as which, mock.patch.object(
            claude_tui, "use_claude_profile"
        ) as use_profile, mock.patch.object(
            claude_tui.subprocess, "run"
        ) as run, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                claude_tui.main(
                    [
                        "--cc-switch-config",
                        "anyrouter",
                        "--",
                        "--settings=/tmp/other.json",
                    ]
                )

        self.assertEqual(raised.exception.code, 2)
        which.assert_not_called()
        use_profile.assert_not_called()
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
