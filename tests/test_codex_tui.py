import contextlib
import io
from pathlib import Path
import subprocess
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
                "session-id",
                "--model",
                "gpt-5.6-sol",
            ]
        )

        self.assertEqual(arguments.cc_switch_config, "jianzhile")
        self.assertEqual(
            arguments.codex_args,
            ["resume", "session-id", "--model", "gpt-5.6-sol"],
        )

    def test_reset_plugin_state_is_launcher_owned_and_not_forwarded(self):
        arguments = codex_tui.parse_args(
            [
                "--cc-switch-config",
                "fengwind",
                "--reset-plugin-state",
                "--",
                "resume",
                "session-id",
            ]
        )

        self.assertTrue(arguments.reset_plugin_state)
        self.assertEqual(arguments.codex_args, ["resume", "session-id"])

    def test_no_forwarded_arguments_is_supported(self):
        arguments = codex_tui.parse_args(
            ["--cc-switch-config", "jianzhile"]
        )

        self.assertFalse(arguments.reset_plugin_state)
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

    def test_mcp_editor_flag_is_not_forwarded_and_rejects_other_actions(self):
        arguments = codex_tui.parse_args(["--cc-switch-config", "lanln", "--edit-mcp"])
        self.assertTrue(arguments.edit_mcp)
        self.assertEqual(arguments.codex_args, [])
        for extra in (["--", "mcp", "add", "dbx"], ["--reset-plugin-state"]):
            with self.subTest(extra=extra), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    codex_tui.parse_args(["--cc-switch-config", "lanln", "--edit-mcp", *extra])
                self.assertEqual(raised.exception.code, 2)

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

    def test_explicit_resume_and_fork_session_ids_are_detected(self):
        session_id = "01a00aaa-bae9-7541-b690-c6c6d193fe68"

        self.assertEqual(
            codex_tui._explicit_session_id(["resume", session_id]),
            session_id,
        )
        self.assertEqual(
            codex_tui._explicit_session_id(
                ["--model", "gpt-5.6-sol", "fork", session_id]
            ),
            session_id,
        )
        self.assertIsNone(
            codex_tui._explicit_session_id(["resume", "session-name"])
        )
        self.assertTrue(codex_tui._has_session_picker(["resume"]))
        self.assertTrue(codex_tui._has_session_picker(["fork", "name"]))
        self.assertFalse(codex_tui._has_session_picker(["--model", "gpt-5.6-sol"]))


class MainTests(unittest.TestCase):
    def setUp(self):
        self.environment = {
            "CODEX_HOME": "/private/tmp/runtime-home",
            "OPENAI_API_KEY": "provider-secret",
        }
        self.runtime = SimpleNamespace(
            provider_id="provider-id",
            provider_name="jianzhile",
            environment=self.environment,
            warnings=("configured plugin 'missing@test' is not installed",),
            activate_thread_rollout=mock.Mock(),
            activate_provider_rollouts=mock.Mock(),
        )

    def runtime_context(self):
        manager = mock.MagicMock()
        manager.__enter__.return_value = self.runtime
        manager.__exit__.return_value = False
        return manager

    @staticmethod
    def process(returncode=0):
        process = mock.Mock()
        process.wait.return_value = returncode
        return process

    def test_launches_resume_without_profile_and_returns_child_code(self):
        manager = self.runtime_context()
        process = self.process(17)
        forwarded = [
            "resume",
            "session-id",
            "--model",
            "gpt-5.6-sol",
        ]
        stderr = io.StringIO()

        with mock.patch.object(
            codex_tui.shutil, "which", return_value="/usr/local/bin/codex"
        ) as which, mock.patch.object(
            codex_tui, "use_codex_runtime", return_value=manager
        ) as use_runtime, mock.patch.object(
            codex_tui.subprocess, "Popen", return_value=process
        ) as popen, contextlib.redirect_stderr(stderr):
            result = codex_tui.main(
                ["--cc-switch-config", "jianzhile", "--", *forwarded]
            )

        self.assertEqual(result, 17)
        which.assert_called_once_with("codex")
        use_runtime.assert_called_once_with(
            "jianzhile", reset_plugin_state=False
        )
        popen.assert_called_once_with(
            ["/usr/local/bin/codex", *forwarded],
            env=self.environment,
        )
        process.wait.assert_called_once_with()
        self.assertNotIn("--profile", popen.call_args.args[0])
        self.assertNotIn("provider-secret", popen.call_args.args[0])
        self.assertIn("WARNING:", stderr.getvalue())
        self.assertIn("missing@test", stderr.getvalue())
        manager.__exit__.assert_called_once()

    def test_reset_flag_is_passed_only_to_runtime(self):
        manager = self.runtime_context()
        process = self.process()

        with mock.patch.object(
            codex_tui.shutil, "which", return_value="/bin/codex"
        ), mock.patch.object(
            codex_tui, "use_codex_runtime", return_value=manager
        ) as use_runtime, mock.patch.object(
            codex_tui.subprocess, "Popen", return_value=process
        ) as popen, contextlib.redirect_stderr(io.StringIO()):
            result = codex_tui.main(
                [
                    "--cc-switch-config",
                    "fengwind",
                    "--reset-plugin-state",
                    "--",
                    "resume",
                ]
            )

        self.assertEqual(result, 0)
        use_runtime.assert_called_once_with(
            "fengwind", reset_plugin_state=True
        )
        popen.assert_called_once_with(
            ["/bin/codex", "resume"], env=self.environment
        )
        self.runtime.activate_provider_rollouts.assert_called_once_with()

    def test_native_mcp_edits_save_only_after_success_without_tui_runtime(self):
        for native_code in (0, 2):
            with self.subTest(native_code=native_code):
                editor = SimpleNamespace(
                    environment={"CODEX_HOME": "/private/tmp/mcp-only"},
                    provider=SimpleNamespace(name="jianzhile"),
                    save=mock.Mock(return_value="/test/backup.json"),
                )
                manager = mock.MagicMock()
                manager.__enter__.return_value = editor
                manager.__exit__.return_value = False
                forwarded = ["mcp", "add", "dbx", "--", "/test/dbx-mcp"]
                stdout = io.StringIO()
                with mock.patch.object(
                    codex_tui.shutil, "which", return_value="/bin/codex"
                ), mock.patch.object(
                    codex_tui, "edit_provider_mcp", return_value=manager
                ) as edit, mock.patch.object(
                    codex_tui, "use_codex_runtime"
                ) as use_runtime, mock.patch.object(
                    codex_tui.subprocess, "Popen", return_value=self.process(native_code)
                ) as popen, contextlib.redirect_stdout(stdout):
                    result = codex_tui.main([
                        "--cc-switch-config", "jianzhile", "--", *forwarded
                    ])
                self.assertEqual(result, native_code)
                edit.assert_called_once_with("jianzhile")
                use_runtime.assert_not_called()
                popen.assert_called_once_with(["/bin/codex", *forwarded], env=editor.environment)
                if native_code == 0:
                    editor.save.assert_called_once_with()
                    self.assertIn("jianzhile", stdout.getvalue())
                    self.assertIn("backup.json", stdout.getvalue())
                else:
                    editor.save.assert_not_called()
                    self.assertEqual(stdout.getvalue(), "")

    def test_native_mcp_save_failure_returns_error_and_cleans_temporary_home(self):
        editor = SimpleNamespace(
            environment={},
            save=mock.Mock(side_effect=CcSwitchConfigError("concurrent MCP edit")),
        )
        manager = mock.MagicMock()
        manager.__enter__.return_value = editor
        manager.__exit__.return_value = False
        stderr = io.StringIO()
        with mock.patch.object(
            codex_tui.shutil, "which", return_value="/bin/codex"
        ), mock.patch.object(
            codex_tui, "edit_provider_mcp", return_value=manager
        ), mock.patch.object(
            codex_tui.subprocess, "Popen", return_value=self.process()
        ), contextlib.redirect_stderr(stderr):
            result = codex_tui.main([
                "--cc-switch-config", "jianzhile", "--", "mcp", "remove", "dbx"
            ])
        self.assertEqual(result, 2)
        self.assertIn("concurrent MCP edit", stderr.getvalue())
        manager.__exit__.assert_called_once()

    def test_mcp_editor_uses_editor_arguments_and_saves_only_after_success(self):
        for editor_code in (0, 1):
            with self.subTest(editor_code=editor_code):
                editor = SimpleNamespace(
                    config_path=Path("/private/tmp/mcp-only/config.toml"),
                    environment={"CODEX_HOME": "/private/tmp/mcp-only"},
                    provider=SimpleNamespace(name="lanln"),
                    save=mock.Mock(return_value="/test/backup.json"),
                )
                manager = mock.MagicMock()
                manager.__enter__.return_value = editor
                manager.__exit__.return_value = False
                with mock.patch.dict(codex_tui.os.environ, {
                    "VISUAL": 'test-editor --wait "argument with spaces"',
                }), mock.patch.object(
                    codex_tui.shutil, "which"
                ) as which, mock.patch.object(
                    codex_tui, "edit_provider_mcp", return_value=manager
                ) as edit, mock.patch.object(
                    codex_tui, "use_codex_runtime"
                ) as use_runtime, mock.patch.object(
                    codex_tui.subprocess, "Popen", return_value=self.process(editor_code)
                ) as popen, contextlib.redirect_stdout(io.StringIO()):
                    result = codex_tui.main(["--cc-switch-config", "lanln", "--edit-mcp"])
                self.assertEqual(result, editor_code)
                which.assert_not_called()
                use_runtime.assert_not_called()
                edit.assert_called_once_with("lanln", mcp_only=True)
                popen.assert_called_once_with([
                    "test-editor", "--wait", "argument with spaces", str(editor.config_path),
                ], env=editor.environment)
                if editor_code == 0:
                    editor.save.assert_called_once_with()
                else:
                    editor.save.assert_not_called()

    def test_invalid_editor_setting_stops_before_creating_config(self):
        stderr = io.StringIO()
        with mock.patch.dict(codex_tui.os.environ, {"VISUAL": 'invalid-"editor'}), mock.patch.object(
            codex_tui, "edit_provider_mcp"
        ) as edit, contextlib.redirect_stderr(stderr):
            result = codex_tui.main(["--cc-switch-config", "lanln", "--edit-mcp"])
        self.assertEqual(result, 2)
        self.assertIn("VISUAL/EDITOR is invalid", stderr.getvalue())
        edit.assert_not_called()

    def test_explicit_session_is_activated_before_launch(self):
        manager = self.runtime_context()
        process = self.process()
        session_id = "01a00aaa-bae9-7541-b690-c6c6d193fe68"

        with mock.patch.object(
            codex_tui.shutil, "which", return_value="/bin/codex"
        ), mock.patch.object(
            codex_tui, "use_codex_runtime", return_value=manager
        ), mock.patch.object(
            codex_tui.subprocess, "Popen", return_value=process
        ) as popen, contextlib.redirect_stderr(io.StringIO()):
            result = codex_tui.main(
                [
                    "--cc-switch-config",
                    "fengwind",
                    "--",
                    "fork",
                    session_id,
                ]
            )

        self.assertEqual(result, 0)
        self.runtime.activate_thread_rollout.assert_called_once_with(session_id)
        popen.assert_called_once_with(
            ["/bin/codex", "fork", session_id], env=self.environment
        )

    def test_launches_codex_without_extra_arguments(self):
        manager = self.runtime_context()
        process = self.process()

        with mock.patch.object(
            codex_tui.shutil, "which", return_value="/bin/codex"
        ), mock.patch.object(
            codex_tui, "use_codex_runtime", return_value=manager
        ), mock.patch.object(
            codex_tui.subprocess, "Popen", return_value=process
        ) as popen, contextlib.redirect_stderr(io.StringIO()):
            result = codex_tui.main(["--cc-switch-config", "provider-id"])

        self.assertEqual(result, 0)
        popen.assert_called_once_with(["/bin/codex"], env=self.environment)

    def test_missing_codex_returns_two_before_provider_selection(self):
        stderr = io.StringIO()

        with mock.patch.object(
            codex_tui.shutil, "which", return_value=None
        ), mock.patch.object(
            codex_tui, "use_codex_runtime"
        ) as use_runtime, mock.patch.object(
            codex_tui.subprocess, "Popen"
        ) as popen, contextlib.redirect_stderr(stderr):
            result = codex_tui.main(["--cc-switch-config", "jianzhile"])

        self.assertEqual(result, 2)
        self.assertIn("Codex CLI", stderr.getvalue())
        use_runtime.assert_not_called()
        popen.assert_not_called()

    def test_configuration_error_returns_two_before_child_launch(self):
        manager = self.runtime_context()
        manager.__enter__.side_effect = CcSwitchConfigError(
            "failed to load provider"
        )
        stderr = io.StringIO()

        with mock.patch.object(
            codex_tui.shutil, "which", return_value="/bin/codex"
        ), mock.patch.object(
            codex_tui, "use_codex_runtime", return_value=manager
        ), mock.patch.object(
            codex_tui.subprocess, "Popen"
        ) as popen, contextlib.redirect_stderr(stderr):
            result = codex_tui.main(["--cc-switch-config", "jianzhile"])

        self.assertEqual(result, 2)
        self.assertIn("failed to load provider", stderr.getvalue())
        popen.assert_not_called()

    def test_finalization_error_returns_two(self):
        manager = self.runtime_context()
        manager.__exit__.side_effect = CcSwitchConfigError(
            "failed to finalize selected Codex runtime"
        )
        stderr = io.StringIO()

        with mock.patch.object(
            codex_tui.shutil, "which", return_value="/bin/codex"
        ), mock.patch.object(
            codex_tui, "use_codex_runtime", return_value=manager
        ), mock.patch.object(
            codex_tui.subprocess, "Popen", return_value=self.process()
        ), contextlib.redirect_stderr(stderr):
            result = codex_tui.main(["--cc-switch-config", "jianzhile"])

        self.assertEqual(result, 2)
        self.assertIn("finalize", stderr.getvalue())

    def test_executable_launch_error_is_sanitized_and_returns_two(self):
        manager = self.runtime_context()
        stderr = io.StringIO()

        with mock.patch.object(
            codex_tui.shutil, "which", return_value="/bin/codex"
        ), mock.patch.object(
            codex_tui, "use_codex_runtime", return_value=manager
        ), mock.patch.object(
            codex_tui.subprocess,
            "Popen",
            side_effect=OSError("launch-error-secret"),
        ), contextlib.redirect_stderr(stderr):
            result = codex_tui.main(["--cc-switch-config", "jianzhile"])

        self.assertEqual(result, 2)
        self.assertIn("failed to launch Codex CLI", stderr.getvalue())
        self.assertNotIn("launch-error-secret", stderr.getvalue())
        manager.__exit__.assert_called_once()

    def test_keyboard_interrupt_stops_child_before_runtime_finalizes(self):
        events = []

        @contextlib.contextmanager
        def runtime_context(*_args, **_kwargs):
            events.append("enter-runtime")
            try:
                yield self.runtime
            finally:
                events.append("exit-runtime")

        process = mock.Mock()
        process.poll.return_value = None

        def wait(timeout=None):
            if timeout is None:
                events.append("wait-child")
                raise KeyboardInterrupt("interrupt")
            events.append(f"wait-child-timeout-{timeout}")
            return -15

        process.wait.side_effect = wait
        process.terminate.side_effect = lambda: events.append("terminate-child")

        with mock.patch.object(
            codex_tui.shutil, "which", return_value="/bin/codex"
        ), mock.patch.object(
            codex_tui, "use_codex_runtime", side_effect=runtime_context
        ), mock.patch.object(
            codex_tui.subprocess, "Popen", return_value=process
        ), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                codex_tui.main(["--cc-switch-config", "jianzhile"])

        self.assertEqual(
            events,
            [
                "enter-runtime",
                "wait-child",
                "terminate-child",
                "wait-child-timeout-5",
                "exit-runtime",
            ],
        )

    def test_timeout_while_stopping_child_escalates_to_kill(self):
        process = mock.Mock()
        process.poll.return_value = None
        process.wait.side_effect = (
            KeyboardInterrupt("interrupt"),
            subprocess.TimeoutExpired("codex", 5),
            -9,
        )

        with self.assertRaises(KeyboardInterrupt):
            codex_tui._wait_for_child(process)

        process.terminate.assert_called_once_with()
        process.kill.assert_called_once_with()
        self.assertEqual(
            process.wait.call_args_list,
            [mock.call(), mock.call(timeout=5), mock.call()],
        )

    def test_profile_conflict_stops_before_all_runtime_side_effects(self):
        with mock.patch.object(codex_tui.shutil, "which") as which, mock.patch.object(
            codex_tui, "use_codex_runtime"
        ) as use_runtime, mock.patch.object(
            codex_tui.subprocess, "Popen"
        ) as popen, contextlib.redirect_stderr(io.StringIO()):
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
        use_runtime.assert_not_called()
        popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
