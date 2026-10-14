"""Retain verified generated source within one run, without reverting runtime data."""
from __future__ import annotations

import json
import os
from pathlib import Path

from .verify import CheckResult
from .browser_contract import BROWSER_CHECK

PUBLIC_CHECK = "public interaction regression (reconstructed jsdom, not Stage 3)"
EXCLUDED = {".git", ".arc", "node_modules", "dist", "build", ".venv", "venv", "requirements",
            "data", "storage", "uploads", "logs", "coverage"}
SUFFIXES = {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".css", ".html", ".json",
            ".yaml", ".yml", ".svg", ".png", ".jpg", ".webp", ".md"}
MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024


def public_checks_ready(project_checks: tuple[CheckResult, ...], structure_passed: bool) -> bool:
    """A failing self-authored test must not hide the fixed behavioral oracle."""
    return structure_passed and (all(check.passed for check in project_checks) or any(
        check.name == "frontend: npm run build" and check.passed for check in project_checks))


class CandidateCheckpoint:
    def __init__(self, project: Path):
        self.project = project.resolve()
        self.files: dict[str, bytes] | None = None
        self.passing: frozenset[str] = frozenset()
        self.case_ids: frozenset[str] = frozenset()

    def _files(self) -> dict[str, bytes]:
        files = {}
        size = 0
        for current, directories, names in os.walk(self.project):
            base = Path(current)
            directories[:] = sorted(name for name in directories
                if name.casefold() not in EXCLUDED and not name.startswith(".")
                and (base / name).resolve() == base / name)
            for name in sorted(names):
                path = base / name
                if name.startswith(".") or path.suffix.casefold() not in SUFFIXES:
                    continue
                if name.casefold() in {"credentials.json", "secrets.json"}:
                    continue
                if name.casefold().startswith("requirements."):
                    continue
                # Runtime JSON databases can live outside a data/ directory.
                # Snapshot manifests/configuration, never arbitrary account data.
                if path.suffix.casefold() == ".json" and not (
                    name in {"package.json", "package-lock.json", "tsconfig.json", "jsconfig.json"}
                    or name.endswith(".config.json")):
                    continue
                if path.resolve() != path:
                    raise ValueError(f"Cannot checkpoint linked source: {path}")
                size += path.stat().st_size
                if size > MAX_SNAPSHOT_BYTES:
                    raise ValueError("Generated source exceeds the bounded checkpoint size")
                files[path.relative_to(self.project).as_posix()] = path.read_bytes()
        return files

    def consider(self, checks: tuple[CheckResult, ...], *, final: bool = False,
                 allow_restore: bool = True, require_comparable: bool = False) -> str:
        public = next((check for check in checks if check.name == PUBLIC_CHECK), None)
        build_ok = any(check.name == "frontend: npm run build" and check.passed for check in checks)
        layout_ok = any(check.name == "web template structure" and check.passed for check in checks)
        statuses = dict(public.case_statuses) if public is not None else {}
        browser = next((check for check in checks if check.name == BROWSER_CHECK), None)
        if statuses and browser is not None:
            statuses.update(browser.case_statuses)
        comparable = bool(statuses) and (not self.case_ids or statuses.keys() == self.case_ids)
        passing = frozenset(title for title, status in statuses.items() if status == "passed")
        lost = self.passing - passing if comparable else frozenset()
        if self.files is not None and (lost or not build_ok or not layout_ok
                or ((final or require_comparable) and not comparable)):
            # A diagnostic may catch a migration between related edits. Keep
            # the healthy pointer without undoing the unfinished source.
            if not allow_restore:
                return ""
            current = self._files()
            if current == self.files:
                return ""
            # Validate the entire restore before any mutation. Never follow a link
            # or remove a runtime database, requirement, protected test or dependency.
            for name in current.keys() | self.files.keys():
                target = self.project / name
                if target.resolve() != target:
                    raise ValueError(f"Cannot restore linked source: {name}")
            for name in current.keys() - self.files.keys():
                (self.project / name).unlink()
            for name, content in self.files.items():
                path = self.project / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            reason = ", ".join(sorted(lost)) if lost else "build/layout failed or latest behavioral result was unavailable"
            note = (f"Restored the last candidate preserving {len(self.passing)}/{len(self.case_ids)} local case passes. "
                    f"Rejected regression: {reason}. Reread changed source before another focused repair. "
                    "This is local regression evidence, not formal Stage 3 acceptance.")
            self._record(note)
            return note
        if build_ok and layout_ok and comparable:
            self.files = self._files()
            self.passing, self.case_ids = passing, frozenset(statuses)
            self._record("Retained complete local behavioral checkpoint; formal acceptance pending.")
        return ""

    def _record(self, note: str) -> None:
        path = self.project / ".arc/candidate-checkpoint.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"local_passed": len(self.passing), "local_total": len(self.case_ids),
            "passed_cases": sorted(self.passing), "source_files": sorted(self.files or {}),
            "note": note, "formal_acceptance": "pending"}, indent=2), encoding="utf-8")
