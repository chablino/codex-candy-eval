"""Launch an interactive Codex TUI with one CC Switch Codex provider."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from collections.abc import Sequence

from cc_switch_config import CcSwitchConfigError, use_codex_profile


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cc-switch-config",
        required=True,
        help="CC Switch Codex provider display name or ID",
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
            "the launcher supplies its temporary profile"
        )
    return arguments


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parse_args(argv)
    executable = shutil.which("codex")
    if executable is None:
        print("ERROR: Codex CLI executable was not found in PATH", file=sys.stderr)
        return 2

    try:
        with use_codex_profile(arguments.cc_switch_config) as runtime:
            override_arguments = [
                argument
                for override in runtime.config_overrides
                for argument in ("-c", override)
            ]
            process = subprocess.run(
                [
                    executable,
                    "--profile",
                    runtime.profile_name,
                    *override_arguments,
                    *arguments.codex_args,
                ],
                env=runtime.environment,
            )
    except CcSwitchConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except OSError:
        print("ERROR: failed to launch Codex CLI", file=sys.stderr)
        return 2

    return process.returncode


if __name__ == "__main__":
    raise SystemExit(main())
