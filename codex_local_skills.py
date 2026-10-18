"""Apply optional machine-local skill exclusions to launcher runtimes."""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

from cc_switch_config import CcSwitchConfigError


LOCAL_SKILL_POLICY_PATH = Path(".cc-switch-tui") / "local-skills.toml"
_POLICY_ERROR = "local Codex skill policy is invalid"


def apply_local_skill_policy(
    document: Mapping[str, Any], shared_home: Path
) -> tuple[dict[str, Any], dict[str, str]]:
    """Disable explicit local skill paths without changing plugin switches.

    Policy paths need not exist: they must stay disabled if an installer recreates
    an archived skill. The policy is local to a shared home, never a repo default.
    """
    result = deepcopy(dict(document))
    path = shared_home / LOCAL_SKILL_POLICY_PATH
    if not path.exists() and not path.is_symlink():
        return result, {}
    try:
        if path.is_symlink() or not path.is_file():
            raise ValueError("policy must be a regular file")
        policy = tomllib.loads(path.read_text(encoding="utf-8"))
        if set(policy) - {"disabled_skills", "skip_hyperframes_auto_install"}:
            raise ValueError("unknown policy field")
        disabled = policy.get("disabled_skills", [])
        skip_install = policy.get("skip_hyperframes_auto_install", False)
        if not isinstance(disabled, list) or not isinstance(skip_install, bool):
            raise ValueError("invalid policy type")
        disabled_paths: dict[str, str] = {}
        for item in disabled:
            if (
                not isinstance(item, str)
                or "\0" in item
                or not Path(item).is_absolute()
                or Path(item).name != "SKILL.md"
            ):
                raise ValueError("skill paths must be absolute SKILL.md paths")
            disabled_paths[str(Path(item).resolve())] = item

        if disabled_paths:
            skills = result.setdefault("skills", {})
            if not isinstance(skills, dict):
                raise ValueError("skills must be a table")
            entries = skills.setdefault("config", [])
            if not isinstance(entries, list):
                raise ValueError("skill config must be an array")
            seen = set()
            for entry in entries:
                if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                    raise ValueError("invalid skill entry")
                canonical = str(Path(entry["path"]).expanduser().resolve())
                if canonical in disabled_paths:
                    entry["enabled"] = False
                    seen.add(canonical)
            entries.extend(
                {"path": original, "enabled": False}
                for canonical, original in disabled_paths.items()
                if canonical not in seen
            )

        environment = {}
        if skip_install:
            environment["HYPERFRAMES_SKIP_SKILLS"] = "1"
            shell_policy = result.setdefault("shell_environment_policy", {})
            if not isinstance(shell_policy, dict):
                raise ValueError("shell environment policy must be a table")
            overrides = shell_policy.setdefault("set", {})
            if not isinstance(overrides, dict):
                raise ValueError("shell environment overrides must be a table")
            # Set it both for Codex and its tools, including inherit = "none".
            overrides.update(environment)
        return result, environment
    except (OSError, UnicodeError, ValueError, RuntimeError, RecursionError):
        raise CcSwitchConfigError(_POLICY_ERROR) from None
