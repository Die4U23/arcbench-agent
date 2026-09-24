from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any


MAX_FILE_CHARS = 30_000
MAX_OUTPUT_CHARS = 12_000
IGNORED_DIRS = {".git", ".arc", "node_modules", "dist", "build", ".venv", "venv"}
ALLOWED_PROJECT_SCRIPTS = {"build", "test", "lint", "typecheck", "check"}

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List project files under a relative directory, excluding generated and hidden runtime data.",
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
            "name": "read_file",
            "description": "Read a UTF-8 text file from the target project.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_text",
            "description": "Search project text files for a literal string and return matching paths and lines.",
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
            "name": "write_file",
            "description": "Create or replace a UTF-8 text file in the target project. Do not write to .git, .arc, or dependency directories.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
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
]


class ProjectTools:
    def __init__(self, project_dir: Path, timeout_seconds: int = 120) -> None:
        self.project_dir = project_dir.resolve()
        self.timeout_seconds = timeout_seconds
        self.written_paths: list[str] = []

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

    def read_file(self, path: str) -> str:
        target = self._resolve(path)
        if not target.is_file():
            raise FileNotFoundError(f"File not found: {path}")
        content = target.read_text(encoding="utf-8")
        if len(content) > MAX_FILE_CHARS:
            return content[:MAX_FILE_CHARS] + "\n...[truncated]"
        return content

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
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")
        relative = target.relative_to(self.project_dir).as_posix()
        self.written_paths.append(relative)
        return f"Wrote {relative} ({len(content)} characters)"

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
        npm = shutil.which("npm") or shutil.which("npm.cmd")
        if not npm:
            raise RuntimeError("npm was not found on PATH")
        command = [npm, "run", script]
        kwargs: dict[str, Any] = {
            "cwd": package_dir,
            "capture_output": True,
            "text": True,
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
        if len(combined) > MAX_OUTPUT_CHARS:
            combined = combined[: MAX_OUTPUT_CHARS // 2] + "\n...[middle truncated]\n" + combined[-MAX_OUTPUT_CHARS // 2 :]
        return json.dumps(
            {
                "command": f"npm run {script}",
                "directory": directory,
                "exit_code": result.returncode,
                "output": combined,
            },
            ensure_ascii=False,
        )

    def call(self, name: str, arguments: dict[str, Any]) -> str:
        handlers = {
            "list_files": self.list_files,
            "read_file": self.read_file,
            "search_text": self.search_text,
            "write_file": self.write_file,
            "run_project_script": self.run_project_script,
        }
        handler = handlers.get(name)
        if handler is None:
            raise ValueError(f"Unknown tool: {name}")
        return handler(**arguments)
