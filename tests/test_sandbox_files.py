from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

from agent import sandbox_files as files


class SandboxFilesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_source_round_trip_preserves_metadata_and_excludes_credentials(self):
        (self.root / ".git").mkdir()
        (self.root / ".git/config").write_text("trusted")
        (self.root / ".env").write_text("FAKE_SENTINEL=never-export")
        (self.root / "app.js").write_text("old")
        before = files.snapshot_project(self.root)
        self.assertEqual(before, {"app.js": b"old"})
        files.apply_snapshot(self.root, {"src/app.js": b"new"}, before=before)
        self.assertFalse((self.root / "app.js").exists())
        self.assertEqual((self.root / "src/app.js").read_bytes(), b"new")
        self.assertEqual((self.root / ".git/config").read_text(), "trusted")
        self.assertIn("FAKE_SENTINEL", (self.root / ".env").read_text())

    def test_malicious_paths_are_rejected_before_writing(self):
        for name in (
            "../escape",
            "/abs",
            "a//b",
            "a/./b",
            r"a\b",
            "x:y",
            ".git/hooks/x",
            "x/.GiT/config",
            ".npmrc",
            ".env.production",
            "a/CON.txt",
            "a.",
            "a ",
        ):
            with self.subTest(name=name), self.assertRaises(files.UnsafeSandboxFile):
                files.apply_snapshot(self.root, {name: b"bad"}, before={})
        self.assertEqual(list(self.root.iterdir()), [])

    def test_case_and_file_directory_collisions_are_rejected(self):
        for payload in ({"A": b"a", "a": b"b"}, {"a": b"a", "a/b": b"b"}):
            with self.assertRaises(files.UnsafeSandboxFile):
                files.validate_snapshot(payload)

    def test_changes_on_host_abort_export(self):
        (self.root / "a").write_bytes(b"first")
        before = files.snapshot_project(self.root)
        (self.root / "a").write_bytes(b"edited")
        with self.assertRaises(files.UnsafeSandboxFile):
            files.apply_snapshot(self.root, {"a": b"container"}, before=before)
        self.assertEqual((self.root / "a").read_bytes(), b"edited")

    @unittest.skipUnless(os.name == "posix", "link fixture requires POSIX")
    def test_symlinks_and_hardlinks_never_cross_boundary(self):
        outside = self.root / "outside"
        outside.write_bytes(b"private")
        project = self.root / "project"
        project.mkdir()
        (project / "link").symlink_to(outside)
        with self.assertRaises(files.UnsafeSandboxFile):
            files.snapshot_project(project)
        (project / "link").unlink()
        os.link(outside, project / "link")
        with self.assertRaises(files.UnsafeSandboxFile):
            files.snapshot_project(project)

    def test_empty_deep_directories_are_bounded_before_traversal(self):
        nested = self.root
        for _ in range(17):
            nested = nested / "d"
            nested.mkdir()
        with self.assertRaises(files.UnsafeSandboxFile):
            files.snapshot_project(self.root)

    def test_byte_and_count_limits(self):
        with patch.object(files, "MAX_FILE_BYTES", 2):
            with self.assertRaises(files.UnsafeSandboxFile):
                files.validate_snapshot({"a": b"abc"})
        with patch.object(files, "MAX_TOTAL_BYTES", 2):
            with self.assertRaises(files.UnsafeSandboxFile):
                files.validate_snapshot({"a": b"ab", "b": b"c"})
        with patch.object(files, "MAX_FILES", 1):
            with self.assertRaises(files.UnsafeSandboxFile):
                files.validate_snapshot({"a": b"a", "b": b"b"})


if __name__ == "__main__":
    unittest.main()
