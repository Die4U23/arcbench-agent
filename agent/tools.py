from __future__ import annotations

import json
import hashlib
import fnmatch
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from .command_output import compact_command_output


MAX_FILE_CHARS = 30_000
MAX_OUTPUT_CHARS = 12_000
MAX_BATCH_FILES = 20
MAX_BATCH_CONTENT_CHARS = 120_000
MAX_BATCH_OUTPUT_CHARS = 40_000
MAX_BATCH_SCRIPTS = 3
IGNORED_DIRS = {".git", ".arc", "node_modules", "dist", "build", ".venv", "venv"}
ALLOWED_PROJECT_SCRIPTS = {"build", "test", "lint", "typecheck", "check"}

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read one UTF-8 project file or a bounded line range. Use start_line/end_line to inspect stack-trace locations beyond a truncated batch read. Lines are one-based and inclusive.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer", "minimum": 1},
                    "end_line": {"type": "integer", "minimum": 1},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": (
                "List project files under a relative directory, excluding generated and hidden runtime data. "
                "Use when the project layout is unknown; batch related reads after locating paths."
            ),
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Relative directory; use '.' for project root."}},
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_files",
            "description": "Read several related UTF-8 project files in one tool call. Use this instead of repeated read_file calls when inspecting a feature.",
            "parameters": {
                "type": "object",
                "properties": {
                    "paths": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": MAX_BATCH_FILES}
                },
                "required": ["paths"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_text",
            "description": (
                "Search project text for a literal identifier, label, or route and return matching paths and lines. "
                "Use this to locate existing code before editing."
            ),
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_files",
            "description": (
                "Create several related UTF-8 project files in one tool call; small files may be replaced. "
                "Use replace_text to edit existing source files longer than 6,000 characters. "
                "All paths and size limits are checked before writing. "
                "Existing package.json dependencies are retained unless replaced with a newer version."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "files": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": MAX_BATCH_FILES,
                        "items": {
                            "type": "object",
                            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                            "required": ["path", "content"],
                            "additionalProperties": False,
                        },
                    }
                },
                "required": ["files"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "replace_text",
            "description": "Replace one uniquely matching text span in an existing UTF-8 file. Refuses missing or ambiguous matches; use write_files for new files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_text": {"type": "string"},
                    "new_text": {"type": "string"},
                },
                "required": ["path", "old_text", "new_text"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_project_script",
            "description": "Run an existing build, test, lint, typecheck, or check npm script in a project subdirectory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "directory": {"type": "string", "description": "Relative directory containing package.json."},
                    "script": {"type": "string", "enum": sorted(ALLOWED_PROJECT_SCRIPTS)},
                },
                "required": ["directory", "script"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_project_scripts",
            "description": (
                "Run up to three existing build, test, lint, typecheck, or check npm scripts in one call. "
                "Use the returned exit codes to verify changes; a model summary is not proof of passing checks."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "scripts": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": MAX_BATCH_SCRIPTS,
                        "items": {
                            "type": "object",
                            "properties": {
                                "directory": {"type": "string"},
                                "script": {"type": "string", "enum": sorted(ALLOWED_PROJECT_SCRIPTS)},
                            },
                            "required": ["directory", "script"],
                            "additionalProperties": False,
                        },
                    }
                },
                "required": ["scripts"],
                "additionalProperties": False,
            },
        },
    },
]


class ProjectTools:
    def __init__(self, project_dir: Path, timeout_seconds: int = 120) -> None:
        self.project_dir = project_dir.resolve()
        self.timeout_seconds = timeout_seconds
        self.written_paths: list[str] = []
        self.changed_paths: list[str] = []

    def _resolve(self, raw_path: str, *, allow_root: bool = False) -> Path:
        candidate = Path(raw_path)
        if candidate.is_absolute():
            raise ValueError("Absolute paths are not allowed")
        resolved = (self.project_dir / candidate).resolve()
        try:
            resolved.relative_to(self.project_dir)
        except ValueError as exc:
            raise ValueError("Path escapes the target project") from exc
        relative = resolved.relative_to(self.project_dir)
        if any(part in IGNORED_DIRS for part in relative.parts):
            raise ValueError("Access to runtime, dependency, or generated directories is not allowed")
        sensitive_names = {"id_rsa", "id_ed25519", "credentials.json", "secrets.json"}
        for part in relative.parts:
            lowered = part.lower()
            if lowered.startswith(".env") or lowered in sensitive_names or lowered.endswith((".pem", ".key", ".p12", ".pfx")):
                raise ValueError("Access to credential or private-key files is not allowed")
        if not allow_root and resolved == self.project_dir:
            raise ValueError("A file path is required")
        return resolved

    def list_files(self, path: str) -> str:
        root = self._resolve(path, allow_root=True)
        if not root.exists() or not root.is_dir():
            raise FileNotFoundError(f"Directory not found: {path}")
        items: list[str] = []
        for current, dirs, files in os.walk(root):
            dirs[:] = sorted(d for d in dirs if d not in IGNORED_DIRS and not d.startswith("."))
            base = Path(current)
            for filename in sorted(files):
                full = base / filename
                items.append(full.relative_to(self.project_dir).as_posix())
                if len(items) >= 300:
                    return "\n".join(items) + "\n...[truncated at 300 files]"
        return "\n".join(items) or "(no files found)"

    def read_file(self, path: str, start_line: int = 1, end_line: int | None = None) -> str:
        if start_line < 1 or (end_line is not None and end_line < start_line):
            raise ValueError("Invalid one-based line range")
        target = self._resolve(path)
        if not target.is_file():
            raise FileNotFoundError(f"File not found: {path}")
        content = target.read_text(encoding="utf-8")
        if start_line != 1 or end_line is not None:
            content = "\n".join(content.splitlines()[start_line - 1:end_line])
        if len(content) > MAX_FILE_CHARS:
            return content[:MAX_FILE_CHARS] + "\n...[truncated]"
        return content

    def read_files(self, paths: list[str]) -> str:
        if not isinstance(paths, list) or not 1 <= len(paths) <= MAX_BATCH_FILES:
            raise ValueError(f"read_files accepts between 1 and {MAX_BATCH_FILES} paths")
        resolved: list[tuple[str, Path]] = []
        seen: set[str] = set()
        for raw_path in paths:
            target = self._resolve(raw_path)
            relative = target.relative_to(self.project_dir).as_posix()
            if relative in seen:
                raise ValueError(f"Duplicate path in read_files: {relative}")
            seen.add(relative)
            if not target.is_file():
                raise FileNotFoundError(f"File not found: {relative}")
            resolved.append((relative, target))

        files: list[dict[str, str]] = []
        read_limit = min(MAX_BATCH_OUTPUT_CHARS, max(1_000, int(os.environ.get("ARCBENCH_MAX_READ_OUTPUT_CHARS", MAX_BATCH_OUTPUT_CHARS))))
        remaining = read_limit
        truncated = False
        for relative, target in resolved:
            if remaining <= 0:
                truncated = True
                break
            content = target.read_text(encoding="utf-8")
            if len(content) > MAX_FILE_CHARS:
                truncated = True
                content = content[:MAX_FILE_CHARS] + "\n...[file truncated]"
            if len(content) > remaining:
                truncated = True
                marker = "\n...[batch output limit reached]"
                content = content[: max(0, remaining - len(marker))] + marker[:remaining]
            files.append({"path": relative, "content": content})
            remaining -= len(content)
            if truncated or remaining <= 0:
                break
        truncated = truncated or len(files) < len(resolved)
        payload = {"files": files, "truncated": truncated}
        encoded = json.dumps(payload, ensure_ascii=False)
        while len(encoded) > read_limit and files:
            payload["truncated"] = True
            last = files[-1]
            original = last["content"]
            marker = "\n...[batch output limit reached]"
            low, high = 0, len(original)
            best: str | None = None
            while low <= high:
                middle = (low + high) // 2
                candidate = original[:middle] + (marker if middle < len(original) else "")
                last["content"] = candidate
                candidate_json = json.dumps(payload, ensure_ascii=False)
                if len(candidate_json) <= read_limit:
                    best = candidate
                    encoded = candidate_json
                    low = middle + 1
                else:
                    high = middle - 1
            if best is None:
                files.pop()
                encoded = json.dumps(payload, ensure_ascii=False)
            else:
                last["content"] = best
                encoded = json.dumps(payload, ensure_ascii=False)
                break
        return encoded

    def search_text(self, query: str) -> str:
        needle = query.strip()
        if not needle:
            raise ValueError("Search query must not be empty")
        matches: list[str] = []
        for current, dirs, files in os.walk(self.project_dir):
            dirs[:] = sorted(d for d in dirs if d not in IGNORED_DIRS and not d.startswith("."))
            for filename in files:
                path = Path(current) / filename
                try:
                    self._resolve(path.relative_to(self.project_dir).as_posix())
                except ValueError:
                    continue
                try:
                    lines = path.read_text(encoding="utf-8").splitlines()
                except (UnicodeDecodeError, OSError):
                    continue
                for number, line in enumerate(lines, 1):
                    if needle.casefold() in line.casefold():
                        matches.append(f"{path.relative_to(self.project_dir).as_posix()}:{number}: {line[:300]}")
                        if len(matches) >= 100:
                            return "\n".join(matches) + "\n...[truncated at 100 matches]"
        return "\n".join(matches) or "(no matches)"

    def write_file(self, path: str, content: str) -> str:
        target = self._resolve(path)
        changed = not target.is_file() or target.read_text(encoding="utf-8") != content
        target.parent.mkdir(parents=True, exist_ok=True)
        if changed:
            target.write_text(content, encoding="utf-8", newline="\n")
        relative = target.relative_to(self.project_dir).as_posix()
        self.written_paths.append(relative)
        if changed:
            self.changed_paths.append(relative)
        return f"Wrote {relative} ({len(content)} characters)"

    def write_files(self, files: list[dict[str, str]]) -> str:
        if not isinstance(files, list) or not 1 <= len(files) <= MAX_BATCH_FILES:
            raise ValueError(f"write_files accepts between 1 and {MAX_BATCH_FILES} files")
        prepared: list[tuple[str, Path, str]] = []
        seen: set[str] = set()
        total_chars = 0
        for item in files:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not isinstance(item.get("content"), str):
                raise ValueError("Each write_files item must contain string path and content fields")
            target = self._resolve(item["path"])
            relative = target.relative_to(self.project_dir).as_posix()
            if relative in seen:
                raise ValueError(f"Duplicate path in write_files: {relative}")
            seen.add(relative)
            content = item["content"]
            if relative == "arcbench-visual-acceptance.json" and target.is_file():
                try:
                    previous = json.loads(target.read_text(encoding="utf-8"))
                    replacement = json.loads(content)
                except json.JSONDecodeError:
                    pass
                else:
                    if isinstance(previous, dict) and isinstance(replacement, dict) and previous.get("version") == replacement.get("version") == 1:
                        for field, key in (("cases", "reference"), ("flows", "name")):
                            old_items, new_items = previous.get(field), replacement.get(field)
                            if isinstance(old_items, list) and isinstance(new_items, list):
                                merged = {item[key]: item for item in old_items if isinstance(item, dict) and isinstance(item.get(key), str)}
                                merged.update({item[key]: item for item in new_items if isinstance(item, dict) and isinstance(item.get(key), str)})
                                replacement[field] = list(merged.values())
                        content = json.dumps(replacement, ensure_ascii=False, indent=2) + "\n"
            if target.is_file() and target.suffix in {".js", ".jsx", ".ts", ".tsx"}:
                original = target.read_text(encoding="utf-8")
                if len(original) > 6_000 and original != content:
                    raise ValueError(
                        f"Existing source file requires replace_text for focused edits: {relative}"
                    )
            if target.name == "package.json" and target.is_file():
                try:
                    previous = json.loads(target.read_text(encoding="utf-8"))
                    replacement = json.loads(content)
                except json.JSONDecodeError:
                    pass
                else:
                    if isinstance(previous, dict) and isinstance(replacement, dict):
                        for field in ("dependencies", "devDependencies"):
                            old_items = previous.get(field)
                            new_items = replacement.get(field)
                            if isinstance(old_items, dict):
                                replacement[field] = {**old_items, **(new_items if isinstance(new_items, dict) else {})}
                        content = json.dumps(replacement, ensure_ascii=False, indent=2) + "\n"
            if len(content) > MAX_FILE_CHARS:
                raise ValueError(f"File exceeds {MAX_FILE_CHARS} characters: {relative}")
            total_chars += len(content)
            if total_chars > MAX_BATCH_CONTENT_CHARS:
                raise ValueError(f"write_files batch exceeds {MAX_BATCH_CONTENT_CHARS} characters")
            prepared.append((relative, target, content))

        for relative, target, content in prepared:
            changed = not target.is_file() or target.read_text(encoding="utf-8") != content
            target.parent.mkdir(parents=True, exist_ok=True)
            if changed:
                target.write_text(content, encoding="utf-8", newline="\n")
                self.changed_paths.append(relative)
        self.written_paths.extend(relative for relative, _, _ in prepared)
        return json.dumps(
            {"written": [{"path": relative, "characters": len(content)} for relative, _, content in prepared]},
            ensure_ascii=False,
        )

    def replace_text(self, path: str, old_text: str, new_text: str) -> str:
        if not isinstance(old_text, str) or not isinstance(new_text, str) or not old_text:
            raise ValueError("replace_text requires nonempty old_text and string new_text")
        target = self._resolve(path)
        original = target.read_text(encoding="utf-8")
        matches = original.count(old_text)
        if matches != 1:
            raise ValueError(f"replace_text requires one exact match in {path}; found {matches}")
        updated = original.replace(old_text, new_text, 1)
        if updated != original:
            file_descriptor, temporary_path = tempfile.mkstemp(prefix=".arc-replace-", dir=target.parent)
            try:
                with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n") as stream:
                    stream.write(updated)
                os.replace(temporary_path, target)
            finally:
                if os.path.exists(temporary_path):
                    os.unlink(temporary_path)
        relative = target.relative_to(self.project_dir).as_posix()
        self.written_paths.append(relative)
        if updated != original:
            self.changed_paths.append(relative)
        return json.dumps({
            "path": relative,
            "old_chars": len(old_text),
            "new_chars": len(new_text),
            "sha256": hashlib.sha256(updated.encode("utf-8")).hexdigest(),
        }, ensure_ascii=False)

    def ensure_dependencies(self) -> None:
        manifests = []
        for folder in (".", "frontend", "backend"):
            directory = self._resolve(folder, allow_root=True)
            manifest = directory / "package.json"
            if manifest.is_file():
                package = json.loads(manifest.read_text(encoding="utf-8"))
                manifests.append((directory, package))
        dependencies = {
            str(directory.relative_to(self.project_dir)): {
                key: package.get(key, {}) for key in ("dependencies", "devDependencies", "optionalDependencies", "workspaces")
            } for directory, package in manifests
        }
        fingerprint = hashlib.sha256(json.dumps(dependencies, sort_keys=True).encode()).hexdigest()
        if fingerprint == getattr(self, "_installed_dependencies", None):
            return
        targets = [directory for directory, package in manifests if any(package.get(key) for key in ("dependencies", "devDependencies", "optionalDependencies"))]
        root_package = next((package for directory, package in manifests if directory == self.project_dir), {})
        workspaces = root_package.get("workspaces", [])
        if isinstance(workspaces, dict):
            workspaces = workspaces.get("packages", [])
        if targets and isinstance(workspaces, list) and workspaces:
            targets = [self.project_dir, *[
                directory for directory in targets if directory != self.project_dir
                and not any(isinstance(pattern, str) and fnmatch.fnmatchcase(
                    directory.relative_to(self.project_dir).as_posix(), pattern.removeprefix("./").rstrip("/"))
                    for pattern in workspaces)
            ]]
        npm = shutil.which("npm") or shutil.which("npm.cmd")
        if targets and not npm:
            raise RuntimeError("npm was not found on PATH")
        for directory in targets:
            command: Any = [npm, "install", "--no-audit", "--no-fund"]
            use_shell = os.name == "nt" and npm.lower().endswith((".cmd", ".bat"))
            if use_shell:
                command = f'"{npm}" install --no-audit --no-fund'
            result = subprocess.run(command, cwd=directory, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=self.timeout_seconds, check=False, shell=use_shell)
            if result.returncode:
                output = compact_command_output((result.stdout or "") + (result.stderr or ""), MAX_OUTPUT_CHARS)
                raise RuntimeError(f"Dependency installation failed under {directory.name}:\n{output}")
        self._installed_dependencies = fingerprint

    def run_project_script(self, directory: str, script: str) -> str:
        if script not in ALLOWED_PROJECT_SCRIPTS:
            raise ValueError(f"Script is not allowed: {script}")
        package_dir = self._resolve(directory, allow_root=True)
        package_json = package_dir / "package.json"
        if not package_json.is_file():
            raise FileNotFoundError(f"package.json not found under {directory}")
        package = json.loads(package_json.read_text(encoding="utf-8"))
        scripts = package.get("scripts", {})
        if script not in scripts:
            raise ValueError(f"package.json does not define script `{script}`")
        if (
            package_dir == self.project_dir
            and (self.project_dir / "frontend").is_dir()
            and script in {"build", "test"}
        ):
            frontend_manifest = self.project_dir / "frontend" / "package.json"
            if not frontend_manifest.is_file():
                raise FileNotFoundError(
                    "frontend/package.json is missing; create a frontend build manifest "
                    "before running root build or test scripts"
                )
            if script == "test" and "backend/tests" in str(scripts[script]):
                test_dir = self.project_dir / "backend" / "tests"
                if not test_dir.is_dir() or not any(
                    path.is_file() and path.suffix in {".js", ".mjs", ".cjs"}
                    for path in test_dir.rglob("*")
                ):
                    raise FileNotFoundError(
                        "backend/tests has no executable JavaScript tests; create real backend tests "
                        "before running the root test script"
                    )
        self.ensure_dependencies()
        npm = shutil.which("npm") or shutil.which("npm.cmd")
        if not npm:
            raise RuntimeError("npm was not found on PATH")
        command = [npm, "run", script]
        kwargs: dict[str, Any] = {
            "cwd": package_dir,
            "capture_output": True,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "timeout": self.timeout_seconds,
            "check": False,
        }
        # Windows command shims require a shell. The script name is strictly allowlisted above.
        if os.name == "nt" and npm.lower().endswith((".cmd", ".bat")):
            command_text = f'"{npm}" run {script}'
            result = subprocess.run(command_text, shell=True, **kwargs)
        else:
            result = subprocess.run(command, **kwargs)
        stdout = result.stdout or ""
        stderr = result.stderr or ""
        combined = (stdout + ("\n" if stdout and stderr else "") + stderr).strip()
        combined = compact_command_output(combined, MAX_OUTPUT_CHARS)
        return json.dumps(
            {
                "command": f"npm run {script}",
                "directory": directory,
                "exit_code": result.returncode,
                "output": combined,
            },
            ensure_ascii=False,
        )

    def run_project_scripts(self, scripts: list[dict[str, str]]) -> str:
        if not isinstance(scripts, list) or not 1 <= len(scripts) <= MAX_BATCH_SCRIPTS:
            raise ValueError(f"run_project_scripts accepts between 1 and {MAX_BATCH_SCRIPTS} scripts")
        results: list[dict[str, Any]] = []
        for item in scripts:
            if not isinstance(item, dict) or not isinstance(item.get("directory"), str) or not isinstance(item.get("script"), str):
                raise ValueError("Each script must contain string directory and script fields")
            try:
                results.append(json.loads(self.run_project_script(item["directory"], item["script"])))
            except Exception as exc:
                results.append({
                    "directory": item["directory"],
                    "command": f"npm run {item['script']}",
                    "exit_code": 1,
                    "error": f"{type(exc).__name__}: {exc}",
                })
        return json.dumps({"results": results}, ensure_ascii=False)

    def call(self, name: str, arguments: dict[str, Any]) -> str:
        handlers = {
            "list_files": self.list_files,
            "read_file": self.read_file,
            "read_files": self.read_files,
            "search_text": self.search_text,
            "write_file": self.write_file,
            "write_files": self.write_files,
            "replace_text": self.replace_text,
            "run_project_script": self.run_project_script,
            "run_project_scripts": self.run_project_scripts,
        }
        handler = handlers.get(name)
        if handler is None:
            raise ValueError(f"Unknown tool: {name}")
        return handler(**arguments)
