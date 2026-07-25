"""Launch interactive Claude Code with one CC Switch Claude provider."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from collections.abc import Sequence

from cc_switch_config import CcSwitchConfigError, use_claude_profile


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cc-switch-config",
        required=True,
        help="CC Switch Claude provider display name or ID",
    )
    return parser


def _is_launcher_owned_argument(argument: str) -> bool:
    return (
        argument == "--settings"
        or argument.startswith("--settings=")
        or argument == "--setting-sources"
        or argument.startswith("--setting-sources=")
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = _build_parser()
    arguments, forwarded_unknown = parser.parse_known_args(argv)
    arguments.claude_args = forwarded_unknown
    if arguments.claude_args[:1] == ["--"]:
        arguments.claude_args = arguments.claude_args[1:]
    if any(
        _is_launcher_owned_argument(argument)
        for argument in arguments.claude_args
    ):
        parser.error(
            "forwarded Claude arguments cannot include --settings or "
            "--setting-sources; the launcher supplies both options"
        )
    return arguments


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parse_args(argv)
    executable = shutil.which("claude")
    if executable is None:
        print(
            "ERROR: Claude Code executable was not found in PATH",
            file=sys.stderr,
        )
        return 2

    try:
        with use_claude_profile(arguments.cc_switch_config) as runtime:
            process = subprocess.run(
                [
                    executable,
                    "--setting-sources",
                    "project,local",
                    "--settings",
                    str(runtime.settings_path),
                    *arguments.claude_args,
                ],
                env=runtime.environment,
            )
    except CcSwitchConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except OSError:
        print("ERROR: failed to launch Claude Code", file=sys.stderr)
        return 2

    return process.returncode


if __name__ == "__main__":
    raise SystemExit(main())
