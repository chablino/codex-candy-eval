"""Launch an interactive Codex TUI with one CC Switch Codex provider."""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
import uuid
from collections.abc import Sequence

from cc_switch_config import CcSwitchConfigError
from codex_provider_mcp import ProviderMcpEditor, edit_provider_mcp, is_provider_mcp_edit
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
        "--edit-mcp",
        action="store_true",
        help="edit and save only this provider's MCP tables in VISUAL/EDITOR (default: nano/vi)",
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
    if arguments.edit_mcp and (arguments.codex_args or arguments.reset_plugin_state):
        parser.error("--edit-mcp cannot be combined with Codex arguments or --reset-plugin-state")
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


def _explicit_session_id(arguments: Sequence[str]) -> str | None:
    for index, argument in enumerate(arguments[:-1]):
        if argument not in {"resume", "fork"}:
            continue
        candidate = arguments[index + 1]
        try:
            return str(uuid.UUID(candidate))
        except (ValueError, AttributeError):
            continue
    return None


def _has_session_picker(arguments: Sequence[str]) -> bool:
    return any(argument in {"resume", "fork"} for argument in arguments)


def _save_mcp_changes(editor: ProviderMcpEditor) -> None:
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
    with edit_provider_mcp(selector, mcp_only=True) as editor:
        process = subprocess.Popen(
            [*command, str(editor.config_path)], env=editor.environment,
        )
        return_code = _wait_for_child(process)
        if return_code == 0:
            _save_mcp_changes(editor)
        return return_code


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parse_args(argv)
    try:
        if arguments.edit_mcp:
            return _edit_mcp_in_editor(arguments.cc_switch_config)
        executable = shutil.which("codex")
        if executable is None:
            print("ERROR: Codex CLI executable was not found in PATH", file=sys.stderr)
            return 2
        if is_provider_mcp_edit(arguments.codex_args):
            with edit_provider_mcp(arguments.cc_switch_config) as editor:
                process = subprocess.Popen(
                    [executable, *arguments.codex_args], env=editor.environment,
                )
                return_code = _wait_for_child(process)
                if return_code == 0:
                    _save_mcp_changes(editor)
                return return_code
        with use_codex_runtime(
            arguments.cc_switch_config,
            reset_plugin_state=arguments.reset_plugin_state,
        ) as runtime:
            for warning in runtime.warnings:
                print(f"WARNING: {warning}", file=sys.stderr)
            session_id = _explicit_session_id(arguments.codex_args)
            if session_id is not None:
                runtime.activate_thread_rollout(session_id)
            elif _has_session_picker(arguments.codex_args):
                runtime.activate_provider_rollouts()
            process = subprocess.Popen(
                [executable, *arguments.codex_args],
                env=runtime.environment,
            )
            return_code = _wait_for_child(process)
    except CcSwitchConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except OSError:
        print(
            "ERROR: failed to launch MCP editor" if arguments.edit_mcp
            else "ERROR: failed to launch Codex CLI", file=sys.stderr,
        )
        return 2

    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
