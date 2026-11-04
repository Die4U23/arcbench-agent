"""Frozen payload checks after local SDK installation."""
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import check_release


class ReleaseIntegrityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.package = self.root / "platform-agent"
        shutil.copytree(check_release.PACKAGE, self.package,
                        ignore=shutil.ignore_patterns("__pycache__", "build", "*.egg-info"))
        shutil.copyfile(check_release.ROOT / "release-v14.json", self.root / "release-v14.json")
        for attribute, value in (("ROOT", self.root), ("PACKAGE", self.package)):
            context = patch.object(check_release, attribute, value)
            context.start()
            self.addCleanup(context.stop)

    def test_sdk_install_metadata_does_not_change_the_frozen_payload(self):
        for name in ("arcbench-agent-runtime/build/lib/arcbench_agent_runtime/runtime.py",
                     "arcbench-agent-runtime/src/arcbench_agent_runtime.egg-info/PKG-INFO"):
            target = self.package / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("installation artifact\n", encoding="utf-8")
        self.assertEqual(len(check_release.verify()["files"]), 44)

    def test_an_unlisted_application_source_is_still_rejected(self):
        (self.package / "agent" / "unexpected.py").write_text("pass\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Package members changed"):
            check_release.verify()

    def test_changed_original_source_is_still_rejected(self):
        (self.package / "main.py").write_bytes(b"raise SystemExit(0)\n")
        with self.assertRaisesRegex(ValueError, "Frozen source differs: main.py"):
            check_release.verify()


if __name__ == "__main__":
    unittest.main()
