from pathlib import Path
import sys
import tempfile
import unittest

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))

from prepare_platform_package_v13 import assert_package_tree, scan_package_tree


class PackageScanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)

    def write(self, relative: str, text: str) -> Path:
        path = self.path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_clean_tree_allows_the_verb_keep(self):
        self.write("agent/notes.py", "Please keep the ledger path.\nKeeping notes is fine.\n")
        self.write("requirements.txt", "PyYAML==6.0.2\n")
        self.assertEqual(scan_package_tree(self.path), [])
        assert_package_tree(self.path)

    def test_product_name_keep_fails(self):
        self.write("agent/notes.py", "Keep the catalog title.\n")
        found = scan_package_tree(self.path)
        self.assertTrue(any(item.startswith("forbidden Keep:") for item in found), found)
        with self.assertRaises(AssertionError):
            assert_package_tree(self.path)

    def test_case_insensitive_words_and_cjk_fail(self):
        samples = (
            "BookStack",
            "stackoverflow",
            "CTRIP",
            "PrestaShop",
            "TicketBooking",
            "12306",
            "携程",
        )
        for word in samples:
            with self.subTest(word=word):
                path = self.write("agent/sample.txt", f"prefix {word} suffix\n")
                found = scan_package_tree(self.path)
                self.assertTrue(found, word)
                path.unlink()

    def test_words_inside_larger_tokens_do_not_fail(self):
        self.write("agent/sample.txt", "bookstackapp keeping 123065 ticketbookingtool\n")
        self.assertEqual(scan_package_tree(self.path), [])

    def test_public_inputs_directory_fails(self):
        packaged = self.path / "public-inputs"
        packaged.mkdir()
        (packaged / "readme.txt").write_text("clean notes\n", encoding="utf-8")
        found = scan_package_tree(self.path)
        self.assertTrue(any(item.startswith("public-inputs/") for item in found), found)
        with self.assertRaises(AssertionError):
            assert_package_tree(self.path)

    def test_platform_registry_fails(self):
        self.write("nested/platform-registry.json", "{}\n")
        found = scan_package_tree(self.path)
        self.assertTrue(any(item.startswith("platform-registry.json:") for item in found), found)


if __name__ == "__main__":
    unittest.main()
