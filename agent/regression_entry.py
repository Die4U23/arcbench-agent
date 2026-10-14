"""Mount the generated browser entry without guessing components or providers."""
from __future__ import annotations

from html.parser import HTMLParser
import json
from pathlib import Path
import re
from urllib.parse import urlsplit


class _ModuleScripts(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.sources: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "script" and values.get("type") == "module" and values.get("src"):
            self.sources.append(values["src"])


def browser_entry(frontend: Path) -> tuple[Path, str]:
    """Index.html is authoritative; canonical main files only support absent HTML."""
    index = frontend / "index.html"
    if index.is_file():
        parser = _ModuleScripts()
        parser.feed(index.read_text(encoding="utf-8"))
        if len(parser.sources) != 1:
            raise ValueError("Regression needs exactly one local module script in frontend/index.html")
        url = urlsplit(parser.sources[0])
        if url.scheme or url.netloc:
            raise ValueError("Regression browser entry must be a local frontend file")
        entry = frontend / url.path.lstrip("/")
    else:
        entries = [frontend / "src" / ("main" + suffix) for suffix in (".jsx", ".tsx", ".js", ".ts")]
        entries = [path for path in entries if path.is_file()]
        if len(entries) != 1:
            raise ValueError("Regression needs the real frontend/index.html module entry or one src/main file")
        entry = entries[0]
    if (not entry.resolve().is_relative_to(frontend.resolve()) or not entry.is_file()
            or entry.suffix not in {".jsx", ".tsx", ".js", ".ts"}
            or re.search(r"\.(test|spec)\.", entry.name)
            or {"node_modules", ".arc"}.intersection(entry.relative_to(frontend).parts)):
        raise ValueError("Regression browser entry must be an existing application module, not a test or dependency")
    source = entry.read_text(encoding="utf-8")
    roots = re.findall(r"\bgetElementById\(\s*['\"]([^'\"]+)['\"]\s*\)", source)
    if len(set(roots)) != 1:
        raise ValueError("Regression needs one explicit getElementById mount target in the browser entry")
    return entry, roots[0]


def entry_adapter(adapter: str, frontend: Path) -> str:
    entry, root_id = browser_entry(frontend)
    # Intercept only the browser's root. Testing Library's separate createRoot
    # calls still use React DOM itself, so assertions observe real components.
    capture = """
import {vi} from 'vitest';
const entryCapture = vi.hoisted(() => ({tree: undefined as any, renders: 0}));
vi.mock('react-dom/client', async (original) => {
  const actual: any = await original();
  const createRoot = (container: any, ...options: any[]) => {
    if (container?.id !== __ROOT_ID__) return actual.createRoot(container, ...options);
    return {render(tree: any) {entryCapture.tree=tree;entryCapture.renders++;}, unmount() {}};
  };
  return {...actual, createRoot, default: {...actual.default, createRoot}};
});
""".replace("__ROOT_ID__", json.dumps(root_id))
    bootstrap = """
const entryRoot = document.createElement('div');
entryRoot.id = __ROOT_ID__;
document.body.appendChild(entryRoot);
await import(__ENTRY__);
entryRoot.remove();
function renderApplication() {
  if (entryCapture.renders !== 1 || entryCapture.tree === undefined)
    throw new Error('Regression browser entry must render one real application tree');
  render(entryCapture.tree);
}

""".replace("__ROOT_ID__", json.dumps(root_id)).replace(
        "__ENTRY__", json.dumps("../" + entry.relative_to(frontend).as_posix()))
    adapter = adapter.replace("import {BrowserRouter} from 'react-router-dom';", capture)
    adapter = adapter.replace("import App from '../src/App.jsx';", "import '@testing-library/jest-dom/vitest';")
    adapter = adapter.replace("class Locator {", bootstrap + "class Locator {")
    adapter = adapter.replace("render(<BrowserRouter><App/></BrowserRouter>);", "renderApplication();")
    # Browser reload preserves history.state (including Router journey state).
    # A new goto destroys it. Treating both alike invents a booking failure.
    return adapter.replace("async reload() {await this.goto(window.location.href);}", """async reload() {
    await settleDocumentWork();cleanup();renderApplication();await settleDocumentWork();
  }""")
