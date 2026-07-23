import io
import subprocess
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from types import SimpleNamespace
from unittest import mock

import cc_switch_config
import codex_candy_eval


class CodexCandyTests(unittest.TestCase):
    def test_parser_accepts_selector(self):
        args = codex_candy_eval.parse_args(
            ["--cc-switch-config", "anyrouter", "-m", "gpt-test", "-n", "2"]
        )

        self.assertEqual(
            (args.cc_switch_config, args.model, args.tests),
            ("anyrouter", "gpt-test", 2),
        )

    def test_subprocess_receives_runtime_and_redacts_error(self):
        environment = {"CODEX_HOME": "/private/runtime"}
        with mock.patch.object(
            codex_candy_eval, "resolve_codex_executable", return_value="codex"
        ), mock.patch.object(
            codex_candy_eval.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 1, "", "secret-token"),
        ) as run:
            with self.assertRaisesRegex(RuntimeError, "<redacted>"):
                codex_candy_eval.run_codex(
                    None,
                    "medium",
                    environment=environment,
                    redact=lambda text: text.replace("secret-token", "<redacted>"),
                )

        self.assertIs(run.call_args.kwargs["env"], environment)

    def test_error_uses_redacted_stdout_when_stderr_is_whitespace(self):
        with mock.patch.object(
            codex_candy_eval, "resolve_codex_executable", return_value="codex"
        ), mock.patch.object(
            codex_candy_eval.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(
                [], 1, "stdout-secret-token", " \n"
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "stdout-<redacted>"):
                codex_candy_eval.run_codex(
                    None,
                    "medium",
                    redact=lambda text: text.replace("secret-token", "<redacted>"),
                )

    def test_selector_enters_one_provider_context_for_all_runs(self):
        runtime = SimpleNamespace(
            environment={"CODEX_HOME": "/private/runtime"},
            redact=lambda text: text.replace("secret", "<redacted>"),
        )
        context_calls = []
        run_calls = []

        @contextmanager
        def fake_use_provider(app_type, selector, db_path=cc_switch_config.DEFAULT_DB_PATH):
            context_calls.append((app_type, selector, db_path))
            yield runtime

        def fake_run_codex(model, effort, *, environment=None, redact=None):
            run_calls.append((model, effort, environment, redact))
            return ("answer 21", 1, 2, 3)

        output = io.StringIO()
        with mock.patch.object(
            cc_switch_config, "use_provider", side_effect=fake_use_provider
        ), mock.patch.object(
            codex_candy_eval, "run_codex", side_effect=fake_run_codex
        ), mock.patch.object(codex_candy_eval, "setup_console", return_value=False), redirect_stdout(
            output
        ):
            result = codex_candy_eval.main(
                ["--cc-switch-config", "anyrouter", "-m", "gpt-test", "-n", "2"]
            )

        self.assertEqual(result, 0)
        self.assertEqual(context_calls, [("codex", "anyrouter", cc_switch_config.DEFAULT_DB_PATH)])
        self.assertEqual(len(run_calls), 2)
        self.assertTrue(all(call[2] is runtime.environment for call in run_calls))
        self.assertTrue(all(call[3] is runtime.redact for call in run_calls))

    def test_no_selector_does_not_load_cc_switch_provider(self):
        with mock.patch.object(codex_candy_eval, "setup_console", return_value=False), mock.patch.object(
            codex_candy_eval, "run_codex", return_value=("answer 21", 1, 2, 3)
        ), mock.patch.object(cc_switch_config, "load_provider") as load, redirect_stdout(
            io.StringIO()
        ):
            result = codex_candy_eval.main(["-n", "1"])

        self.assertEqual(result, 0)
        load.assert_not_called()

    def test_configuration_error_stops_before_first_request(self):
        with mock.patch.object(
            cc_switch_config,
            "use_provider",
            side_effect=cc_switch_config.CcSwitchConfigError("bad selector"),
        ), mock.patch.object(codex_candy_eval, "setup_console", return_value=False), mock.patch.object(
            codex_candy_eval, "run_codex"
        ) as run, redirect_stderr(io.StringIO()) as errors:
            result = codex_candy_eval.main(
                ["--cc-switch-config", "missing", "-n", "1"]
            )

        self.assertEqual(result, 2)
        self.assertIn("bad selector", errors.getvalue())
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
