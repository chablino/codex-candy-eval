"""Give one Claude window its own config directory that shares state by link."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from claude_plugin_state import STATE_DIRECTORY_NAME


GLOBAL_CONFIG_NAME = ".claude.json"
# Files the launcher writes into each window's directory.
LAUNCHER_FILES = frozenset({"settings.json", "provider-settings.json", "mcp.json"})
_UNSHARED_ENTRIES = LAUNCHER_FILES | {
    "settings.json.bak",
    GLOBAL_CONFIG_NAME,
    STATE_DIRECTORY_NAME,
}
# Linked before Claude Code first creates them, so first writes land in the
# shared home instead of the temporary directory.
PRECREATED_DIRECTORIES = (
    "agents",
    "backups",
    "cache",
    "commands",
    "file-history",
    "paste-cache",
    "plans",
    "plugins",
    "projects",
    "session-env",
    "sessions",
    "shell-snapshots",
    "skills",
    "todos",
)
PRECREATED_FILES = ("CLAUDE.md", "history.jsonl")


class ClaudeOverlayError(RuntimeError):
    """Raised when a window's Claude directory cannot share state safely."""


def _transient(name: str) -> bool:
    # Lock directories and atomic-write temporaries belong to one process.
    return name.endswith((".lock", ".tmp")) or ".tmp." in name


def link_shared_entries(
    overlay: Path, shared_home: Path, global_config: Path
) -> None:
    """Link shared Claude state into overlay; global_config backs .claude.json."""
    try:
        for name in PRECREATED_DIRECTORIES:
            target = shared_home / name
            if not os.path.lexists(target):
                target.mkdir(mode=0o700)
        names = {
            name
            for name in os.listdir(shared_home)
            if name not in _UNSHARED_ENTRIES and not _transient(name)
        }
        names.update(PRECREATED_DIRECTORIES, PRECREATED_FILES)
        for name in sorted(names):
            (overlay / name).symlink_to(shared_home / name)
        (overlay / GLOBAL_CONFIG_NAME).symlink_to(global_config)
    except OSError:
        raise ClaudeOverlayError("failed to link shared Claude state") from None


def reclaim_overlay_entries(overlay: Path, shared_home: Path) -> None:
    """Move entries Claude Code created inside overlay into the shared home.

    An entry that already exists in the shared home wins; the overlay copy is
    dropped with the temporary directory.
    """
    failed = False
    try:
        names = sorted(os.listdir(overlay))
    except OSError:
        raise ClaudeOverlayError("failed to keep new Claude state") from None
    for name in names:
        source = overlay / name
        if name in LAUNCHER_FILES or _transient(name) or source.is_symlink():
            continue
        target = shared_home / name
        try:
            if not os.path.lexists(target):
                shutil.move(source, target)
        except OSError:
            failed = True
    if failed:
        raise ClaudeOverlayError("failed to keep new Claude state")
