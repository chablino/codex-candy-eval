import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

import tomli_w

from cc_switch_config import CcSwitchConfigError
from codex_local_skills import LOCAL_SKILL_POLICY_PATH, apply_local_skill_policy


class LocalSkillPolicyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.policy_path = self.root / LOCAL_SKILL_POLICY_PATH
        self.skill = str(self.root / "personal" / "hyperframes" / "SKILL.md")

    def write_policy(self, policy):
        self.policy_path.parent.mkdir(parents=True, exist_ok=True)
        self.policy_path.write_text(tomli_w.dumps(policy), encoding="utf-8")

    def test_missing_policy_does_not_change_existing_configuration(self):
        original = {"plugins": {"hyperframes@test": {"enabled": True}}}

        document, environment = apply_local_skill_policy(original, self.root)

        self.assertEqual(document, original)
        self.assertIsNot(document, original)
        self.assertEqual(environment, {})
        self.assertFalse(self.policy_path.parent.exists())

    def test_only_selected_standalone_paths_are_disabled(self):
        plugin_skill = str(self.root / "plugins" / "hyperframes" / "SKILL.md")
        other_skill = str(self.root / "personal" / "other" / "SKILL.md")
        original = {
            "plugins": {"hyperframes@test": {"enabled": True}},
            "skills": {"config": [
                {"path": self.skill, "enabled": True},
                {"path": plugin_skill, "enabled": True},
                {"path": other_skill, "enabled": True},
            ]},
        }
        before = deepcopy(original)
        self.write_policy({"disabled_skills": [self.skill]})

        document, environment = apply_local_skill_policy(original, self.root)

        self.assertEqual(document["skills"]["config"], [
            {"path": self.skill, "enabled": False},
            {"path": plugin_skill, "enabled": True},
            {"path": other_skill, "enabled": True},
        ])
        self.assertEqual(document["plugins"], original["plugins"])
        self.assertEqual(original, before)
        self.assertEqual(environment, {})

    def test_absent_skills_stay_excluded_after_an_installer_recreates_them(self):
        self.write_policy({"disabled_skills": [self.skill, self.skill]})
        before, _ = apply_local_skill_policy({}, self.root)
        skill = Path(self.skill)
        skill.parent.mkdir(parents=True)
        skill.write_text("reinstalled skill", encoding="utf-8")

        after, _ = apply_local_skill_policy(before, self.root)

        self.assertEqual(after, before)
        self.assertEqual(after["skills"]["config"], [
            {"path": self.skill, "enabled": False}
        ])

    def test_skip_install_reaches_tools_even_with_no_inherited_environment(self):
        self.write_policy({"skip_hyperframes_auto_install": True})
        original = {"shell_environment_policy": {
            "inherit": "none", "set": {"KEPT": "value", "HYPERFRAMES_SKIP_SKILLS": "0"}
        }}

        document, environment = apply_local_skill_policy(original, self.root)

        self.assertEqual(environment, {"HYPERFRAMES_SKIP_SKILLS": "1"})
        self.assertEqual(document["shell_environment_policy"], {
            "inherit": "none", "set": {"KEPT": "value", "HYPERFRAMES_SKIP_SKILLS": "1"}
        })
        self.assertNotIn("skills", document)

    def test_malformed_policy_is_sanitized(self):
        cases = (
            {"disabled_skills": "secret-invalid"},
            {"disabled_skills": [True]},
            {"disabled_skills": ["secret-relative/SKILL.md"]},
            {"disabled_skills": [str(self.root / "not-a-skill")]},
            {"skip_hyperframes_auto_install": "secret-invalid"},
            {"unknown": "secret-invalid"},
        )
        for policy in cases:
            with self.subTest(policy=policy):
                self.write_policy(policy)
                with self.assertRaises(CcSwitchConfigError) as raised:
                    apply_local_skill_policy({}, self.root)
                self.assertEqual(str(raised.exception), "local Codex skill policy is invalid")
                self.assertIsNone(raised.exception.__cause__)

    def test_symlink_policy_is_rejected(self):
        self.policy_path.parent.mkdir(parents=True)
        target = self.root / "elsewhere.toml"
        target.write_text("", encoding="utf-8")
        self.policy_path.symlink_to(target)

        with self.assertRaises(CcSwitchConfigError):
            apply_local_skill_policy({}, self.root)
