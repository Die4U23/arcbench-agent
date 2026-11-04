"""Pack the v13 platform agent and reject packaged task knowledge.

The forbidden-word list lives only in this script. Importing the module does
not build an archive. ``scan_package_tree`` is the assertion helper.
"""
from __future__ import annotations

import hashlib
import json
import re
import tempfile
import zipfile
from pathlib import Path


# Case-sensitive whole word, so the English verb "keep" is allowed.
_KEEP_WORD = re.compile(r"\bKeep\b")
# Case-insensitive whole words. 携程 has no useful word boundary in Unicode
# ``\w``, so it is matched as a substring.
_OTHER_WORDS = re.compile(
    r"\b(?:bookstack|stackoverflow|ctrip|prestashop|ticketbooking|12306)\b",
    re.IGNORECASE,
)
_CTRIP_CN = "携程"

ROOT = Path(__file__).resolve().parent
PACKAGE = ROOT / "platform-agent"
ARCHIVE = ROOT / "platform-source-agent-v13.zip"
SKIP_PARTS = {"run-evidence", "application", ".git", ".env", "node_modules", ".arc", "__pycache__"}
REQUIRED_MEMBERS = (
    "main.py",
    "requirements.txt",
    "static-tools/interaction-scope.cjs",
    "agent/platform_generation.py",
    "agent/platform_inputs.py",
    "agent/platform_runtime_check.py",
    "agent/platform_scaffold.py",
    "agent/platform_seed.py",
    "agent/platform_settings.py",
    "agent/platform_contract.py",
    "agent/platform_static.py",
    "agent/platform_task_ir.py",
    "agent/platform_acceptance.py",
)


def _text_violations(relative: str, payload: bytes) -> list[str]:
    text = payload.decode("utf-8", errors="replace")
    found: list[str] = []
    if _KEEP_WORD.search(text):
        found.append(f"forbidden Keep: {relative}")
    match = _OTHER_WORDS.search(text)
    if match:
        found.append(f"forbidden word {match.group(0)}: {relative}")
    if _CTRIP_CN in text:
        found.append(f"forbidden word {_CTRIP_CN}: {relative}")
    return found


def scan_package_tree(root: Path) -> list[str]:
    """Return violations for a directory that will be zipped.

    An empty list means the tree has no ``public-inputs/``, no
    ``platform-registry.json``, and no forbidden task word in member text.
    """
    root = Path(root)
    if not root.is_dir():
        return [f"not a directory: {root}"]
    violations: list[str] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        parts = [part for part in relative.split("/") if part]
        if "public-inputs" in parts:
            violations.append(f"public-inputs/: {relative}")
        if path.name == "platform-registry.json":
            violations.append(f"platform-registry.json: {relative}")
        if path.is_file():
            violations.extend(_text_violations(relative, path.read_bytes()))
    return violations


def assert_package_tree(root: Path) -> None:
    """Raise AssertionError when ``scan_package_tree`` finds a violation."""
    violations = scan_package_tree(root)
    if violations:
        raise AssertionError("packaged task knowledge is present:\n" + "\n".join(violations))


def _skipped(relative: Path) -> bool:
    return any(part in SKIP_PARTS for part in relative.parts) or relative.name.startswith(".env")


def main() -> int:
    if ARCHIVE.exists():
        raise SystemExit("v13 exists")
    with zipfile.ZipFile(ARCHIVE, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zipped:
        for path in sorted(PACKAGE.rglob("*")):
            if not path.is_file() or path.suffix in {".pyc", ".pyo"}:
                continue
            relative = path.relative_to(PACKAGE)
            if _skipped(relative):
                continue
            zipped.write(path, relative.as_posix())
    try:
        with zipfile.ZipFile(ARCHIVE) as zipped:
            names = zipped.namelist()
            assert zipped.testzip() is None
            for required in REQUIRED_MEMBERS:
                assert required in names, required
            assert "platform-registry.json" not in {Path(name).name for name in names}
            assert not any(part == "public-inputs" for name in names for part in name.split("/"))
            assert not any(
                part in SKIP_PARTS or part.startswith(".env")
                for name in names for part in name.split("/")
            )
            requirements = zipped.read("requirements.txt").decode("utf-8")
            assert "playwright==" not in requirements
            generation = zipped.read("agent/platform_generation.py").decode("utf-8")
            assert "LOCATOR_WALKTHROUGH" in generation
            settings = zipped.read("agent/platform_settings.py").decode("utf-8")
            assert "runtime_rounds: int = 4" in settings and "runtime_seconds: int = 3_600" in settings
            assert "review_request_allowance: int = 4" in settings
            assert "return min(64, 8 + max(0, int(work_item_count)))" in generation
            runtime = zipped.read("agent/platform_runtime_check.py").decode("utf-8")
            assert "choose_playwright_package" in runtime and "chromium_headless_shell-" in runtime
            with tempfile.TemporaryDirectory() as temp:
                extracted = Path(temp) / "package"
                zipped.extractall(extracted)
                assert_package_tree(extracted)
    except Exception:
        ARCHIVE.unlink(missing_ok=True)
        raise
    manifest = dict(
        zip=str(ARCHIVE),
        sha256=hashlib.sha256(ARCHIVE.read_bytes()).hexdigest(),
        bytes=ARCHIVE.stat().st_size,
        members=len(names),
        source="local-runs/v13-20261104 without packaged task inputs",
        token_limit=(
            "clamp(1500000 + 30000*scenarios + 10000*work_items + 5000*entities, "
            "1500000, 6000000); ARC_TOKEN_LIMIT overrides via settings.token_limit"
        ),
        runtime_check=dict(rounds=4, seconds=3600, repair_requests=16,
                           review_requests_per_batch=4,
                           review_requests_total="min(64, 8+work items)",
                           disable_with="ARC_RUNTIME_CHECK=0"),
        model="deepseek-flash or deepseek-v4-flash (response alias deepseek-v4.1-flash-1017 accepted)",
        formal_status="UNRUN",
    )
    (ROOT / "platform-package-v13.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
