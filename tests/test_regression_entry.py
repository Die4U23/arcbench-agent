from pathlib import Path
import tempfile
import unittest

from agent.regression_entry import browser_entry, entry_adapter
from agent.ticketbooking_contract import DOM_ADAPTER


class RegressionEntryTests(unittest.TestCase):
    def test_html_selects_actual_entry_despite_test_and_multiple_component_files(self):
        with tempfile.TemporaryDirectory() as directory:
            frontend = Path(directory)
            (frontend / 'src').mkdir()
            for name in ('App.test.jsx', 'App.jsx', 'App.tsx'):
                (frontend / 'src' / name).write_text('throw new Error("not the browser entry");')
            entry = frontend / 'src/client.tsx'
            entry.write_text("createRoot(document.getElementById('application')).render(<SessionProvider><App /></SessionProvider>);")
            (frontend / 'index.html').write_text('<div id="application"></div><script type="module" src="/src/client.tsx?v=1"></script>')
            self.assertEqual(browser_entry(frontend), (entry, 'application'))
            fixture = entry_adapter(DOM_ADAPTER, frontend)
            self.assertIn('await import("../src/client.tsx")', fixture)
            self.assertNotIn('App.test.jsx', fixture)
            self.assertNotIn('render(<BrowserRouter><App/></BrowserRouter>)', fixture)

    def test_rejects_test_remote_missing_and_escaping_module_entries(self):
        for entry in ('/src/App.test.jsx', 'https://example.com/main.jsx', '/src/missing.jsx', '../outside.jsx'):
            with self.subTest(entry=entry), tempfile.TemporaryDirectory() as directory:
                frontend = Path(directory)
                (frontend / 'src').mkdir()
                (frontend / 'src/App.test.jsx').write_text("getElementById('root')")
                (frontend / 'index.html').write_text(f'<script type="module" src="{entry}"></script>')
                with self.assertRaises(ValueError):
                    browser_entry(frontend)

    def test_missing_or_ambiguous_browser_entry_fails_before_running_a_fixture(self):
        for html in ('<div id="root"></div>', '<script type="module" src="/src/main.jsx"></script>' * 2):
            with tempfile.TemporaryDirectory() as directory:
                frontend = Path(directory)
                (frontend / 'index.html').write_text(html)
                with self.assertRaisesRegex(ValueError, 'exactly one'):
                    browser_entry(frontend)


if __name__ == '__main__':
    unittest.main()
