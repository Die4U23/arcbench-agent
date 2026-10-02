from __future__ import annotations

import json
import html
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from .command_output import compact_command_output


OUTPUT_LIMIT = 10_000
TIMEOUT_SECONDS = 120
IGNORED_DIRS = {".git", ".arc", "node_modules", "dist", "build", ".venv", "venv"}


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    exit_code: int | None
    output: str


@dataclass(frozen=True)
class VerificationResult:
    passed: bool
    checks: tuple[CheckResult, ...]
    ready_for_evaluation: bool = False

    def summary(self) -> str:
        if not self.checks:
            return "No supported build or test scripts were found."
        parts = []
        for check in self.checks:
            status = "PASSED" if check.passed else "FAILED"
            parts.append(f"{check.name}: {status} (exit={check.exit_code})\n{check.output}")
        return "\n\n".join(parts)


def _truncate(text: str) -> str:
    return compact_command_output(text, OUTPUT_LIMIT)


def _run_npm_script(package_dir: Path, script: str) -> CheckResult:
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if not npm:
        return CheckResult(f"{package_dir.name}: npm run {script}", False, None, "npm was not found on PATH")
    command = [npm, "run", script]
    kwargs: dict[str, Any] = {
        "cwd": package_dir,
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": TIMEOUT_SECONDS,
        "check": False,
    }
    try:
        if os.name == "nt" and npm.lower().endswith((".cmd", ".bat")):
            # Only package scripts selected from a fixed allowlist are run here.
            result = subprocess.run(f'"{npm}" run {script}', shell=True, **kwargs)
        else:
            result = subprocess.run(command, **kwargs)
        output = _truncate(((result.stdout or "") + "\n" + (result.stderr or "")).strip())
        return CheckResult(
            name=f"{package_dir.name}: npm run {script}",
            passed=result.returncode == 0,
            exit_code=result.returncode,
            output=output,
        )
    except subprocess.TimeoutExpired as exc:
        output = _truncate(str(exc.stdout or "") + "\n" + str(exc.stderr or ""))
        return CheckResult(f"{package_dir.name}: npm run {script}", False, None, f"Timed out after {TIMEOUT_SECONDS}s\n{output}")


def verify_project(project_dir: Path, *, include_root: bool = True) -> VerificationResult:
    checks: list[CheckResult] = []
    package_files: list[Path] = []
    for current, dirs, files in os.walk(project_dir):
        dirs[:] = sorted(d for d in dirs if d not in IGNORED_DIRS and not d.startswith("."))
        relative = Path(current).relative_to(project_dir)
        if len(relative.parts) > 2:
            dirs[:] = []
        if "package.json" in files:
            package_files.append(Path(current) / "package.json")
    npm_scripts: list[tuple[Path, str]] = []
    for package_file in sorted(package_files):
        if not include_root and package_file.parent == project_dir:
            continue
        try:
            package = json.loads(package_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            checks.append(CheckResult(str(package_file.relative_to(project_dir)), False, None, f"Invalid package.json: {exc}"))
            continue
        scripts = package.get("scripts", {})
        if not isinstance(scripts, dict):
            continue
        for script in ("build", "test"):
            if script in scripts:
                npm_scripts.append((package_file.parent, script))
    for package_dir, script in npm_scripts:
        checks.append(_run_npm_script(package_dir, script))
    if not checks:
        return VerificationResult(False, (CheckResult("project scripts", False, None, "No package.json build/test scripts found"),))
    return VerificationResult(all(check.passed for check in checks), tuple(checks))


def verify_web_template_structure(project_dir: Path) -> VerificationResult:
    required_dirs = ("frontend", "backend")
    missing = [name for name in required_dirs if not (project_dir / name).is_dir()]
    problems: list[str] = []
    if missing:
        problems.append(
            "missing required root-level directories: " + ", ".join(f"{name}/" for name in missing)
        )

    frontend_manifest_path = project_dir / "frontend" / "package.json"
    if not frontend_manifest_path.is_file():
        problems.append("frontend/package.json is missing")
    else:
        try:
            frontend_manifest = json.loads(frontend_manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            problems.append(f"frontend/package.json is invalid: {exc}")
        else:
            scripts = frontend_manifest.get("scripts", {}) if isinstance(frontend_manifest, dict) else None
            if not isinstance(scripts, dict) or not isinstance(scripts.get("build"), str) or not scripts["build"].strip():
                problems.append("frontend/package.json must define a non-empty scripts.build command")

    passed = not problems
    output = (
        "Required Web layout is present and the frontend defines its build command."
        if passed
        else "Web template is not ready: " + "; ".join(problems)
        + ". Put the app in frontend/, backend code in backend/, and make the frontend buildable independently."
    )
    return VerificationResult(
        passed,
        (CheckResult("web template structure", passed, 0 if passed else 1, output),),
    )


def prepare_web_template_structure(project_dir: Path) -> None:
    """Create the runner-required top-level Web directories before generation."""
    project_dir.mkdir(parents=True, exist_ok=True)
    for name in ("frontend", "backend"):
        directory = project_dir / name
        if directory.exists() and not directory.is_dir():
            raise RuntimeError(f"Required Web directory path is occupied by a file: {name}/")
        directory.mkdir(exist_ok=True)


def verify_demo_page(project_dir: Path, expected_heading: str) -> VerificationResult:
    page = project_dir / "index.html"
    if not page.is_file():
        return VerificationResult(False, (CheckResult("demo page", False, None, "index.html was not generated"),))
    content = page.read_text(encoding="utf-8")
    passed = html.escape(expected_heading) in content and "<html" in content.lower() and "</html>" in content.lower()
    output = "Generated page contains the expected heading." if passed else "Generated page is missing expected HTML content."
    return VerificationResult(passed, (CheckResult("demo page", passed, 0 if passed else 1, output),))
