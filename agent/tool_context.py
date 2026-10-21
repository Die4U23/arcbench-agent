"""Bounded, read-only evidence that avoids discovery and diagnostic round trips."""
from __future__ import annotations

import json
import re


def initial_file_inventory(tools) -> str:
    """Only disclose names allowed by the existing project path policy."""
    paths = []
    size = 0
    try:
        listing = tools.list_files(".")
        for path in listing.splitlines():
            if path.startswith(("...[", "(no files")):
                continue
            try:
                tools._resolve(path)
            except (ValueError, OSError):
                continue
            if len(paths) >= 80 or size + len(path) > 4000:
                break
            paths.append(path)
            size += len(path) + 1
    except (AttributeError, ValueError, OSError):
        return ""
    if not paths:
        return ""
    return ("\n\nInitial project file inventory (bounded snapshot; not file contents):\n"
            + json.dumps(paths, ensure_ascii=False)
            + "\nStart with a batch read of the relevant known files; no root listing is needed "
              "for these paths. Refresh discovery when new or unlisted files are relevant.")


def failure_source_context(name: str, result: str, tools) -> str:
    """Quote at most two existing stack locations; never infer or edit a repair."""
    if name not in {"run_project_script", "run_project_scripts"}:
        return result
    try:
        payload = json.loads(result)
    except (ValueError, TypeError):
        return result
    checks = payload.get("results", []) if isinstance(payload, dict) and name == "run_project_scripts" else [payload]
    if not isinstance(checks, list):
        return result
    contexts = []
    seen = set()
    for check in checks:
        if not isinstance(check, dict) or type(check.get("exit_code")) is not int or check["exit_code"] == 0:
            continue
        # Only absolute paths under the trusted Docker mount are interpretable.
        # ProjectTools still enforces traversal, protected directories and keys.
        for match in re.finditer(r"/work/([^\s():]+):(\d+)(?::\d+)?", str(check.get("output", ""))):
            path, line = match[1], int(match[2])
            if (path, line) in seen or line < 1:
                continue
            seen.add((path, line))
            start, end = max(1, line - 3), line + 3
            try:
                content = tools.read_file(path, start_line=start, end_line=end)
            except (ValueError, OSError, UnicodeError):
                continue
            if not content:
                continue
            contexts.append({"path": path, "start_line": start, "failure_line": line,
                             "content": content[:1600]})
            if len(contexts) == 2:
                break
        if len(contexts) == 2:
            break
    if not contexts:
        return result
    payload["failure_source_context"] = contexts
    payload["failure_source_note"] = ("Exact current source at failing stack locations, not a proposed fix. "
        "Compare the failing assertion, the called function's return contract, and its caller before editing. "
        "Preserve protected assertions; a failing caller does not imply the shared function is wrong.")
    enriched = json.dumps(payload, ensure_ascii=False)
    return enriched if len(enriched) <= 40000 else result
