"""Reuse source only while its exact, complete tool payload is in context."""
from __future__ import annotations

import hashlib
import json
import os
from typing import Any


UNCHANGED = "[unchanged since earlier read in this conversation]"


def source_path(path: str) -> str:
    normalized = os.path.normpath(path.replace("\\", "/")).replace("\\", "/")
    return normalized.casefold() if os.name == "nt" else normalized


class ReadEvidence:
    def __init__(self) -> None:
        # Store the actual message, not only its ID: dropped messages must never
        # become valid evidence because Python reuses an object ID.
        self._entries: dict[tuple, tuple[str, dict[str, Any]]] = {}

    def clear(self) -> None:
        self._entries.clear()

    def invalidate(self, paths) -> None:
        changed = {source_path(path) for path in paths}
        for key in tuple(self._entries):
            if key[0] in changed:
                del self._entries[key]

    def retain(self, exchanges: list[list[dict[str, Any]]]) -> None:
        present = {id(message) for exchange in exchanges for message in exchange}
        self._entries = {key: entry for key, entry in self._entries.items()
                         if id(entry[1]) in present}

    @staticmethod
    def _single_key(arguments: dict[str, Any]) -> tuple | None:
        path = arguments.get("path")
        start, end = arguments.get("start_line", 1), arguments.get("end_line")
        if (not isinstance(path, str) or type(start) is not int or start < 1
                or (end is not None and (type(end) is not int or end < start))):
            return None
        return source_path(path), start, end

    @staticmethod
    def _batch(result: str) -> dict | None:
        try:
            payload = json.loads(result)
        except (TypeError, ValueError):
            return None
        if (not isinstance(payload, dict) or payload.get("truncated") is not False
                or not isinstance(payload.get("files"), list)):
            return None
        if not all(isinstance(file, dict) and isinstance(file.get("path"), str)
                   and isinstance(file.get("content"), str) for file in payload["files"]):
            return None
        return payload

    @staticmethod
    def _digest(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def _matches(self, key: tuple, content: str) -> bool:
        entry = self._entries.get(key)
        return entry is not None and entry[0] == self._digest(content)

    def filter(self, name: str, arguments: dict[str, Any], result: str) -> str:
        # Always called after the real tool read. Never bypass disk checks,
        # permission validation, file deletion, or an external source edit.
        if name == "read_file":
            key = self._single_key(arguments)
            if (key is not None and not result.endswith("\n...[truncated]")
                    and len(result) > len(UNCHANGED) and self._matches(key, result)):
                return UNCHANGED
        elif name == "read_files":
            payload = self._batch(result)
            if payload is not None:
                reused = False
                for file in payload["files"]:
                    if (len(file["content"]) > len(UNCHANGED)
                            and self._matches((source_path(file["path"]), 1, None), file["content"])):
                        file["content"] = UNCHANGED
                        reused = True
                if reused:
                    filtered = json.dumps(payload, ensure_ascii=False)
                    if len(filtered) < len(result):
                        return filtered
        return result

    def remember(self, name: str, arguments: dict[str, Any], message: dict[str, Any]) -> None:
        # Index only the final payload actually sent, after output limits. A
        # reference marker is not source evidence and cannot extend its lifetime.
        result = message["content"]
        if name == "read_file":
            key = self._single_key(arguments)
            if (key is not None and result != UNCHANGED
                    and not result.endswith("\n...[truncated]")):
                self._entries[key] = self._digest(result), message
        elif name == "read_files":
            payload = self._batch(result)
            if payload is not None:
                for file in payload["files"]:
                    if file["content"] != UNCHANGED:
                        key = source_path(file["path"]), 1, None
                        self._entries[key] = self._digest(file["content"]), message
