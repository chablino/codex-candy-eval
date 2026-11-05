"""Launch interactive Claude Code with one CC Switch Claude provider."""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
from collections.abc import Sequence

from cc_switch_config import CcSwitchConfigError, use_claude_profile
from claude_provider_mcp import ClaudeProviderMcpEditor, edit_claude_provider_mcp

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
    parser.add_argument(
        "--edit-mcp",
        action="store_true",
        help="edit and save only this provider's MCP servers in VISUAL/EDITOR (default: nano/vi)",
    )
    return parser


def _is_launcher_owned_argument(argument: str) -> bool:
    return (
        argument == "--settings"
        or argument.startswith("--settings=")
        or argument == "--setting-sources"
        or argument.startswith("--setting-sources=")
        or argument == "--mcp-config"
        or argument.startswith("--mcp-config=")
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = _build_parser()
    arguments, forwarded_unknown = parser.parse_known_args(argv)
    arguments.claude_args = forwarded_unknown
    if arguments.claude_args[:1] == ["--"]:
        arguments.claude_args = arguments.claude_args[1:]
    if arguments.edit_mcp and arguments.claude_args:
        parser.error("--edit-mcp cannot be combined with Claude arguments")
    if any(
        _is_launcher_owned_argument(argument)
        for argument in arguments.claude_args
    ):
        parser.error(
            "forwarded Claude arguments cannot include --settings, "
            "--setting-sources or --mcp-config; the launcher supplies them"
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


def _save_mcp_changes(editor: ClaudeProviderMcpEditor) -> None:
    backup = editor.save()
    if backup is not None:
        print(f"MCP 已保存到 provider：{editor.provider.name}")
        print(f"修改前备份：{backup}")


def _edit_mcp_in_editor(selector: str) -> int:
    configured_editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if configured_editor:
        try:
            command = shlex.split(configured_editor)
        except ValueError:
            raise CcSwitchConfigError("VISUAL/EDITOR is invalid") from None
    else:
        executable = shutil.which("nano") or shutil.which("vi")
        command = [executable] if executable else []
    if not command:
        raise CcSwitchConfigError("no text editor found; set VISUAL or EDITOR")
    with edit_claude_provider_mcp(selector) as editor:
        process = subprocess.run([*command, str(editor.config_path)], env=editor.environment)
        if process.returncode == 0:
            _save_mcp_changes(editor)
        return process.returncode


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parse_args(argv)
    if arguments.edit_mcp:
        try:
            return _edit_mcp_in_editor(arguments.cc_switch_config)
        except CcSwitchConfigError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        except OSError:
            print("ERROR: failed to launch MCP editor", file=sys.stderr)
            return 2
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
            # --mcp-config is variadic: use the = form so it cannot swallow
            # the arguments that follow it.
            mcp_arguments = (
                [f"--mcp-config={runtime.mcp_config_path}"]
                if runtime.mcp_config_path is not None
                else []
            )
            process = subprocess.run(
                [
                    executable,
                    "--setting-sources",
                    "project,local",
                    "--settings",
                    str(runtime.settings_path),
                    *mcp_arguments,
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
