"""Static import closure of the platform generation entry for packaging."""
from __future__ import annotations

import ast
import json
from pathlib import Path


PLATFORM_ROOT = Path(__file__).resolve().parents[1]
AGENT_DIR = PLATFORM_ROOT / "agent"
ENTRY_FILES = (
    AGENT_DIR / "platform_generation.py",
    PLATFORM_ROOT / "main.py",
)


def agent_module_names() -> dict[str, Path]:
    modules = {}
    for path in sorted(AGENT_DIR.glob("*.py")):
        if path.name == "__init__.py":
            continue
        modules[f"agent.{path.stem}"] = path
    return modules


def _package_parts(path: Path) -> tuple[str, ...]:
    try:
        relative = path.resolve().relative_to(AGENT_DIR.resolve())
    except ValueError:
        return ()
    if relative.name == "__init__.py":
        return ("agent", *relative.parts[:-1])
    return ("agent", *relative.parts[:-1]) if relative.parts[:-1] else ("agent",)


def resolve_agent_modules(node: ast.AST, path: Path, known: set[str]) -> list[str]:
    found: list[str] = []
    if isinstance(node, ast.Import):
        for alias in node.names:
            if alias.name in known:
                found.append(alias.name)
            elif alias.name.startswith("agent."):
                root = alias.name
                while root not in known and root.startswith("agent."):
                    root = root.rsplit(".", 1)[0]
                if root in known:
                    found.append(root)
    elif isinstance(node, ast.ImportFrom):
        if node.level == 0:
            module = node.module or ""
            candidates = []
            if module in known:
                candidates.append(module)
            elif module == "agent" or module.startswith("agent."):
                candidates.append(module)
                for alias in node.names:
                    if alias.name != "*":
                        candidates.append(f"{module}.{alias.name}")
            found.extend(name for name in candidates if name in known)
        else:
            parts = list(_package_parts(path))
            up = node.level - 1
            if up > len(parts):
                return []
            if up:
                parts = parts[:-up]
            prefix = ".".join(parts)
            if node.module:
                candidates = [".".join(filter(None, (prefix, node.module)))]
            else:
                candidates = [
                    ".".join(filter(None, (prefix, alias.name)))
                    for alias in node.names if alias.name != "*"
                ]
            found.extend(name for name in candidates if name in known)
    return found


def classify_imports(path: Path, known: set[str]) -> tuple[set[str], set[str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    top: set[str] = set()
    lazy: set[str] = set()

    def visit(node: ast.AST, inside_function: bool) -> None:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            target = lazy if inside_function else top
            target.update(resolve_agent_modules(node, path, known))
            return
        nested = inside_function or isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        for child in ast.iter_child_nodes(node):
            visit(child, nested)

    for node in tree.body:
        visit(node, False)
    return top, lazy


def closure(seeds: set[str], follow: dict[str, set[str]]) -> set[str]:
    seen: set[str] = set()
    stack = list(seeds)
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        seen.add(name)
        stack.extend(follow.get(name, ()))
    return seen


def compute_closure() -> dict[str, list[str]]:
    modules = agent_module_names()
    known = set(modules)
    top_imports: dict[str, set[str]] = {name: set() for name in known}
    lazy_imports: dict[str, set[str]] = {name: set() for name in known}
    for name, path in modules.items():
        top_imports[name], lazy_imports[name] = classify_imports(path, known)

    seeds: set[str] = set()
    for entry in ENTRY_FILES:
        top, _lazy = classify_imports(entry, known)
        seeds.update(top)
        if entry.parent == AGENT_DIR:
            seeds.add(f"agent.{entry.stem}")

    required = closure(seeds, top_imports)
    combined = {name: top_imports[name] | lazy_imports[name] for name in known}
    lazy_seeds = set()
    for name in required:
        lazy_seeds.update(lazy_imports.get(name, ()))
    lazy_only = closure(lazy_seeds, combined) - required
    unused = known - required - lazy_only
    return {
        "required": sorted(required),
        "lazy_only": sorted(lazy_only),
        "unused": sorted(unused),
    }


def main() -> int:
    print(json.dumps(compute_closure(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
