"""Launch interactive Claude Code with one CC Switch Claude provider."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from collections.abc import Sequence

from cc_switch_config import CcSwitchConfigError, use_claude_profile

# cometix (@cometix/claude-code, the Node.js build) is preferred; the official
# claude is used only when no cometix command is on PATH.
_CLAUDE_EXECUTABLES = ("cometix", "claude")


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


def _has_effort_argument(arguments: Sequence[str]) -> bool:
    return any(
        argument == "--effort" or argument.startswith("--effort=")
        for argument in arguments
    )


def _find_claude_executable() -> str | None:
    for name in _CLAUDE_EXECUTABLES:
        executable = shutil.which(name)
        if executable is not None:
            return executable
    return None


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parse_args(argv)
    executable = _find_claude_executable()
    if executable is None:
        print(
            "ERROR: Claude Code executable (cometix or claude) "
            "was not found in PATH",
            file=sys.stderr,
        )
        return 2

    try:
        with use_claude_profile(arguments.cc_switch_config) as runtime:
            effort_arguments = (
                ["--effort", runtime.default_effort]
                if runtime.default_effort is not None
                and not _has_effort_argument(arguments.claude_args)
                else []
            )
            process = subprocess.run(
                [
                    executable,
                    "--setting-sources",
                    "project,local",
                    "--settings",
                    str(runtime.settings_path),
                    *effort_arguments,
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
