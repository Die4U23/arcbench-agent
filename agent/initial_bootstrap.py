"""Cheap, conservative diagnostics while a new Web scaffold is incomplete.

This is a scheduling guard, never an acceptance check. Unknown toolchains and
custom entry configurations continue to the normal verifier.
"""
from __future__ import annotations

from html.parser import HTMLParser
import json
from pathlib import Path
import shlex
from urllib.parse import unquote, urlsplit


class _ModuleScripts(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.sources: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "script" and values.get("type") == "module" and values.get("src"):
            self.sources.append(values["src"])


def _inside(root: Path, path: Path) -> bool:
    return path.resolve().is_relative_to(root.resolve())


def initial_bootstrap_gaps(root: Path) -> tuple[str, ...]:
    """Report only definite missing inputs; do not execute scripts or change files."""
    gaps: list[str] = []
    manifests: dict[str, dict] = {}
    for folder in ("frontend", "backend", ""):
        manifest = root / folder / "package.json"
        if not _inside(root, manifest):
            continue
        if not manifest.is_file():
            if folder == "frontend":
                gaps.append("frontend/package.json is missing")
            continue
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            gaps.append(f"{manifest.relative_to(root).as_posix()} is not readable JSON")
            continue
        if not isinstance(data, dict):
            gaps.append(f"{manifest.relative_to(root).as_posix()} must be a JSON object")
            continue
        scripts = data.get("scripts", {})
        scripts = scripts if isinstance(scripts, dict) else {}
        manifests[folder] = scripts
        if folder == "frontend" and (not isinstance(scripts.get("build"), str) or not scripts["build"].strip()):
            gaps.append("frontend/package.json scripts.build is missing")
        for name in ("build", "test", "start"):
            script = scripts.get(name)
            if not isinstance(script, str):
                continue
            # Only a direct node file command is unambiguous. Shell pipelines,
            # flags, package runners and pre-hooks may create their own inputs.
            try:
                words = shlex.split(script)
            except ValueError:
                continue
            if (len(words) == 2 and words[0] == "node" and Path(words[1]).suffix in {".js", ".mjs", ".cjs"}
                    and not scripts.get("pre" + name)):
                target = manifest.parent / words[1]
                if _inside(root, target) and not target.is_file():
                    gaps.append(f"{manifest.relative_to(root).as_posix()} scripts.{name} references missing {target.relative_to(root).as_posix()}")

    frontend = root / "frontend"
    scripts = manifests.get("frontend", {})
    html = frontend / "index.html"
    # An explicit config may choose an alternate input. Do not guess it.
    has_config = any(frontend.glob("vite.config.*"))
    if scripts.get("build") == "vite build" and not scripts.get("prebuild") and not has_config:
        if not html.is_file():
            gaps.append("frontend/index.html is missing for the declared vite build")
        elif html.is_file() and _inside(root, html):
            parser = _ModuleScripts()
            try:
                parser.feed(html.read_text(encoding="utf-8"))
            except (OSError, UnicodeError):
                return tuple(gaps)
            for source in parser.sources:
                url = urlsplit(source)
                if url.scheme or url.netloc or not url.path:
                    continue
                target = frontend / unquote(url.path).lstrip("/")
                if _inside(root, target) and not target.is_file():
                    gaps.append(f"frontend/index.html references missing {target.relative_to(root).as_posix()}")
    return tuple(gaps)
