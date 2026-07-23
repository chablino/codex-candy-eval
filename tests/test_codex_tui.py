import contextlib
import io
import unittest
from types import SimpleNamespace
from unittest import mock

import codex_tui
from cc_switch_config import CcSwitchConfigError


class ParserTests(unittest.TestCase):
    def test_selector_is_required(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                codex_tui.parse_args([])

        self.assertEqual(raised.exception.code, 2)

    def test_separator_is_removed_and_codex_arguments_keep_order(self):
        arguments = codex_tui.parse_args(
            [
                "--cc-switch-config",
                "jianzhile",
                "--",
                "resume",
                "019f8353-4724-7ec2-8f1b-f101458b5151",
                "--model",
                "gpt-5.6-sol",
            ]
        )

        self.assertEqual(arguments.cc_switch_config, "jianzhile")
        self.assertEqual(
            arguments.codex_args,
            [
                "resume",
                "019f8353-4724-7ec2-8f1b-f101458b5151",
                "--model",
                "gpt-5.6-sol",
            ],
        )

    def test_no_forwarded_arguments_is_supported(self):
        arguments = codex_tui.parse_args(
            ["--cc-switch-config", "jianzhile"]
        )

        self.assertEqual(arguments.codex_args, [])

    def test_non_profile_codex_overrides_are_allowed(self):
        arguments = codex_tui.parse_args(
            [
                "--cc-switch-config",
                "jianzhile",
                "--",
                "-c",
                "model_reasoning_effort=high",
                "--model",
                "gpt-5.6-sol",
            ]
        )

        self.assertEqual(
            arguments.codex_args,
            [
                "-c",
                "model_reasoning_effort=high",
                "--model",
                "gpt-5.6-sol",
            ],
        )

    def test_forwarded_profile_forms_are_rejected(self):
        cases = (
            ["--profile", "other"],
            ["--profile=other"],
            ["-p", "other"],
            ["-pother"],
        )

        for codex_args in cases:
            with self.subTest(codex_args=codex_args), contextlib.redirect_stderr(
                io.StringIO()
            ):
                with self.assertRaises(SystemExit) as raised:
                    codex_tui.parse_args(
                        ["--cc-switch-config", "jianzhile", "--", *codex_args]
                    )

            self.assertEqual(raised.exception.code, 2)


class MainTests(unittest.TestCase):
    def setUp(self):
        self.environment = {"OPENAI_API_KEY": "provider-secret"}
        self.runtime = SimpleNamespace(
            provider_id="provider-id",
            provider_name="jianzhile",
            profile_name="cc-switch-random",
            environment=self.environment,
            config_overrides=("legacy-auth-override",),
        )

    def runtime_context(self):
        manager = mock.MagicMock()
        manager.__enter__.return_value = self.runtime
        manager.__exit__.return_value = False
        return manager

    def test_launches_resume_with_inherited_terminal_and_returns_child_code(self):
        manager = self.runtime_context()
        completed = mock.Mock(returncode=17)
        forwarded = [
            "resume",
            "session-id",
            "-c",
            "model_reasoning_effort=high",
            "--model",
            "gpt-5.6-sol",
        ]

        with mock.patch.object(
            codex_tui.shutil, "which", return_value="/usr/local/bin/codex"
        ) as which, mock.patch.object(
            codex_tui, "use_codex_profile", return_value=manager
        ) as use_profile, mock.patch.object(
            codex_tui.subprocess, "run", return_value=completed
        ) as run:
            result = codex_tui.main(
                ["--cc-switch-config", "jianzhile", "--", *forwarded]
            )

        self.assertEqual(result, 17)
        which.assert_called_once_with("codex")
        use_profile.assert_called_once_with("jianzhile")
        run.assert_called_once_with(
            [
                "/usr/local/bin/codex",
                "--profile",
                "cc-switch-random",
                *forwarded,
            ],
            env=self.environment,
        )
        self.assertNotIn("provider-secret", run.call_args.args[0])
        self.assertEqual(
            run.call_args.kwargs["env"]["OPENAI_API_KEY"], "provider-secret"
        )
        manager.__enter__.assert_called_once_with()
        manager.__exit__.assert_called_once()

    def test_launches_codex_without_extra_arguments(self):
        manager = self.runtime_context()

        with mock.patch.object(
            codex_tui.shutil, "which", return_value="/bin/codex"
        ), mock.patch.object(
            codex_tui, "use_codex_profile", return_value=manager
        ), mock.patch.object(
            codex_tui.subprocess,
            "run",
            return_value=mock.Mock(returncode=0),
        ) as run:
            result = codex_tui.main(
                ["--cc-switch-config", "provider-id"]
            )

        self.assertEqual(result, 0)
        run.assert_called_once_with(
            ["/bin/codex", "--profile", "cc-switch-random"],
            env=self.environment,
        )

    def test_missing_codex_returns_two_before_provider_selection(self):
        stderr = io.StringIO()

        with mock.patch.object(
            codex_tui.shutil, "which", return_value=None
        ), mock.patch.object(codex_tui, "use_codex_profile") as use_profile, mock.patch.object(
            codex_tui.subprocess, "run"
        ) as run, contextlib.redirect_stderr(stderr):
            result = codex_tui.main(
                ["--cc-switch-config", "jianzhile"]
            )

        self.assertEqual(result, 2)
        self.assertIn("Codex CLI", stderr.getvalue())
        use_profile.assert_not_called()
        run.assert_not_called()

    def test_configuration_error_returns_two_before_child_launch(self):
        token = "provider-error-secret"
        manager = self.runtime_context()
        manager.__enter__.side_effect = CcSwitchConfigError(
            "failed to load provider"
        )
        stderr = io.StringIO()

        with mock.patch.object(
            codex_tui.shutil, "which", return_value="/bin/codex"
        ), mock.patch.object(
            codex_tui, "use_codex_profile", return_value=manager
        ), mock.patch.object(
            codex_tui.subprocess, "run"
        ) as run, contextlib.redirect_stderr(stderr):
            result = codex_tui.main(
                ["--cc-switch-config", "jianzhile"]
            )

        self.assertEqual(result, 2)
        self.assertIn("failed to load provider", stderr.getvalue())
        self.assertNotIn(token, stderr.getvalue())
        run.assert_not_called()

    def test_profile_cleanup_error_returns_two(self):
        manager = self.runtime_context()
        manager.__exit__.side_effect = CcSwitchConfigError(
            "failed to clean up selected Codex profile"
        )
        stderr = io.StringIO()

        with mock.patch.object(
            codex_tui.shutil, "which", return_value="/bin/codex"
        ), mock.patch.object(
            codex_tui, "use_codex_profile", return_value=manager
        ), mock.patch.object(
            codex_tui.subprocess,
            "run",
            return_value=mock.Mock(returncode=0),
        ), contextlib.redirect_stderr(stderr):
            result = codex_tui.main(
                ["--cc-switch-config", "jianzhile"]
            )

        self.assertEqual(result, 2)
        self.assertIn("clean up", stderr.getvalue())

    def test_executable_launch_error_is_sanitized_and_returns_two(self):
        token = "launch-error-secret"
        manager = self.runtime_context()
        stderr = io.StringIO()

        with mock.patch.object(
            codex_tui.shutil, "which", return_value="/bin/codex"
        ), mock.patch.object(
            codex_tui, "use_codex_profile", return_value=manager
        ), mock.patch.object(
            codex_tui.subprocess, "run", side_effect=OSError(token)
        ), contextlib.redirect_stderr(stderr):
            result = codex_tui.main(
                ["--cc-switch-config", "jianzhile"]
            )

        self.assertEqual(result, 2)
        self.assertIn("failed to launch Codex CLI", stderr.getvalue())
        self.assertNotIn(token, stderr.getvalue())
        manager.__exit__.assert_called_once()

    def test_profile_conflict_stops_before_all_runtime_side_effects(self):
        with mock.patch.object(codex_tui.shutil, "which") as which, mock.patch.object(
            codex_tui, "use_codex_profile"
        ) as use_profile, mock.patch.object(
            codex_tui.subprocess, "run"
        ) as run, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                codex_tui.main(
                    [
                        "--cc-switch-config",
                        "jianzhile",
                        "--",
                        "resume",
                        "--profile=other",
                    ]
                )

        self.assertEqual(raised.exception.code, 2)
        which.assert_not_called()
        use_profile.assert_not_called()
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
