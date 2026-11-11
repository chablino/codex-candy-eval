import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path

from claude_overlay import (
    PRECREATED_DIRECTORIES,
    PRECREATED_FILES,
    ClaudeOverlayError,
    link_shared_entries,
    reclaim_overlay_entries,
)


class LinkTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.shared = self.root / "shared"
        self.shared.mkdir()
        self.overlay = self.root / "overlay"
        self.overlay.mkdir()
        self.global_config = self.root / "global.json"

    def link(self):
        link_shared_entries(self.overlay, self.shared, self.global_config)

    def test_existing_entries_are_linked_by_name(self):
        (self.shared / "projects").mkdir()
        (self.shared / "projects" / "p.jsonl").write_text("p", "utf-8")
        (self.shared / "keybindings.json").write_text("{}", "utf-8")
        (self.shared / "custom-dir").mkdir()
        elsewhere = self.root / "dotfiles-claude.md"
        elsewhere.write_text("dotfiles", "utf-8")
        (self.shared / "CLAUDE.md").symlink_to(elsewhere)

        self.link()

        for name in ("projects", "keybindings.json", "custom-dir", "CLAUDE.md"):
            with self.subTest(name=name):
                self.assertTrue((self.overlay / name).is_symlink())
                self.assertEqual(
                    os.readlink(self.overlay / name), str(self.shared / name)
                )
        self.assertEqual(
            (self.overlay / "CLAUDE.md").read_text("utf-8"), "dotfiles"
        )

    def test_missing_known_state_is_created_or_linked_ahead(self):
        self.link()

        for name in PRECREATED_DIRECTORIES:
            with self.subTest(name=name):
                self.assertTrue((self.shared / name).is_dir())
                self.assertEqual(
                    stat.S_IMODE((self.shared / name).stat().st_mode), 0o700
                )
                self.assertTrue((self.overlay / name).is_symlink())
        for name in PRECREATED_FILES:
            with self.subTest(name=name):
                self.assertTrue((self.overlay / name).is_symlink())
                self.assertFalse((self.shared / name).exists())

        (self.overlay / "history.jsonl").write_text("line\n", "utf-8")
        self.assertEqual(
            (self.shared / "history.jsonl").read_text("utf-8"), "line\n"
        )

    def test_global_config_backs_claude_json(self):
        (self.shared / ".claude.json").write_text("shared-copy", "utf-8")

        self.link()

        self.assertEqual(
            os.readlink(self.overlay / ".claude.json"), str(self.global_config)
        )

    def test_launcher_owned_and_transient_entries_are_not_linked(self):
        for name in (
            "settings.json",
            "settings.json.bak",
            "provider-settings.json",
            "mcp.json",
            ".claude.json.tmp.123.abc",
            "draft.tmp",
        ):
            (self.shared / name).write_text("x", "utf-8")
        (self.shared / ".cc-switch-tui").mkdir()
        (self.shared / ".claude.json.lock").mkdir()

        self.link()

        for name in (
            "settings.json",
            "settings.json.bak",
            "provider-settings.json",
            "mcp.json",
            ".claude.json.tmp.123.abc",
            "draft.tmp",
            ".cc-switch-tui",
            ".claude.json.lock",
        ):
            with self.subTest(name=name):
                self.assertFalse(os.path.lexists(self.overlay / name))

    def test_removing_overlay_keeps_shared_state(self):
        (self.shared / "projects").mkdir()
        kept = self.shared / "projects" / "keep.jsonl"
        kept.write_text("keep", "utf-8")
        self.global_config.write_text("{}", "utf-8")

        self.link()
        shutil.rmtree(self.overlay)

        self.assertEqual(kept.read_text("utf-8"), "keep")
        self.assertEqual(self.global_config.read_text("utf-8"), "{}")

    def test_link_failure_is_reported(self):
        (self.overlay / "projects").mkdir()

        with self.assertRaises(ClaudeOverlayError):
            self.link()


class ReclaimTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.shared = self.root / "shared"
        self.shared.mkdir()
        self.overlay = self.root / "overlay"
        self.overlay.mkdir()

    def test_new_entries_move_to_shared_home(self):
        (self.overlay / "statsig").mkdir()
        (self.overlay / "statsig" / "id").write_text("kept", "utf-8")
        (self.overlay / "new.json").write_text("{}", "utf-8")

        reclaim_overlay_entries(self.overlay, self.shared)

        self.assertEqual(
            (self.shared / "statsig" / "id").read_text("utf-8"), "kept"
        )
        self.assertEqual((self.shared / "new.json").read_text("utf-8"), "{}")
        self.assertFalse((self.overlay / "statsig").exists())

    def test_shared_copy_wins_and_owned_entries_stay(self):
        (self.shared / "conflict.json").write_text("shared", "utf-8")
        (self.overlay / "conflict.json").write_text("overlay", "utf-8")
        (self.overlay / "settings.json").write_text("{}", "utf-8")
        (self.overlay / "provider-settings.json").write_text("{}", "utf-8")
        (self.overlay / ".claude.json.lock").mkdir()
        (self.overlay / ".claude.json.tmp.1.abc").write_text("t", "utf-8")
        (self.overlay / "history.jsonl").symlink_to(self.shared / "history.jsonl")

        reclaim_overlay_entries(self.overlay, self.shared)

        self.assertEqual(
            (self.shared / "conflict.json").read_text("utf-8"), "shared"
        )
        for name in (
            "settings.json",
            "provider-settings.json",
            ".claude.json.lock",
            ".claude.json.tmp.1.abc",
            "history.jsonl",
        ):
            with self.subTest(name=name):
                self.assertFalse(os.path.lexists(self.shared / name))
        self.assertTrue((self.overlay / "history.jsonl").is_symlink())


if __name__ == "__main__":
    unittest.main()
