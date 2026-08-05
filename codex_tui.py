"""Launch an interactive Codex TUI with one CC Switch Codex provider."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from collections.abc import Sequence

from cc_switch_config import CcSwitchConfigError
from codex_runtime import use_codex_runtime


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cc-switch-config",
        required=True,
        help="CC Switch Codex provider display name or ID",
    )
    parser.add_argument(
        "--reset-plugin-state",
        action="store_true",
        help="clear launcher-owned plugin switches for the selected provider before launch",
    )
    parser.add_argument(
        "codex_args",
        nargs=argparse.REMAINDER,
        help="arguments after -- are forwarded to Codex",
    )
    return parser


def _is_profile_argument(argument: str) -> bool:
    return (
        argument == "--profile"
        or argument.startswith("--profile=")
        or argument == "-p"
        or (argument.startswith("-p") and len(argument) > 2)
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = _build_parser()
    arguments = parser.parse_args(argv)
    if arguments.codex_args[:1] == ["--"]:
        arguments.codex_args = arguments.codex_args[1:]
    if any(_is_profile_argument(argument) for argument in arguments.codex_args):
        parser.error(
            "forwarded Codex arguments cannot include --profile or -p; "
            "the launcher supplies a complete temporary configuration"
        )
    return arguments


def _wait_for_child(process: subprocess.Popen) -> int:
    try:
        return process.wait()
    except BaseException:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        raise


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parse_args(argv)
    executable = shutil.which("codex")
    if executable is None:
        print("ERROR: Codex CLI executable was not found in PATH", file=sys.stderr)
        return 2

    try:
        with use_codex_runtime(
            arguments.cc_switch_config,
            reset_plugin_state=arguments.reset_plugin_state,
        ) as runtime:
            for warning in runtime.warnings:
                print(f"WARNING: {warning}", file=sys.stderr)
            process = subprocess.Popen(
                [executable, *arguments.codex_args],
                env=runtime.environment,
            )
            return_code = _wait_for_child(process)
    except CcSwitchConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except OSError:
        print("ERROR: failed to launch Codex CLI", file=sys.stderr)
        return 2

    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
