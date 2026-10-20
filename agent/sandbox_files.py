"""Bounded regular-file transfer across the generated-code container boundary.

No container-writable directory is reused directly by host Git or model tools.
These helpers assume one controller writer; exports are applied only after the
owned container has stopped. They are not a concurrent filesystem transaction.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
import stat
import tempfile
from collections.abc import Mapping

MAX_FILES = 5000
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024
_IGNORED_DIRS = {"node_modules", "__pycache__", ".venv", "venv"}
_METADATA = {".git", ".arc", ".hg", ".svn", ".gitmodules", ".gitattributes"}
_CREDENTIALS = {
    ".npmrc",
    ".pypirc",
    ".netrc",
    ".ssh",
    ".aws",
    ".azure",
    ".config",
    ".docker",
    "id_rsa",
    "id_ed25519",
    "id_ecdsa",
    "id_dsa",
    "credentials.json",
    "secrets.json",
}
_DEVICES = {"con", "prn", "aux", "nul"} | {
    f"{prefix}{number}"
    for prefix in ("com", "lpt")
    for number in [*range(1, 10), "¹", "²", "³"]
}


class UnsafeSandboxFile(ValueError):
    pass


def _excluded_part(part: str) -> bool:
    name = part.casefold()
    return (
        name in _IGNORED_DIRS | _METADATA | _CREDENTIALS
        or name.startswith(".env")
        or name.endswith((".pem", ".key", ".p12", ".pfx"))
    )


def validate_relative_path(value: str) -> PurePosixPath:
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or ":" in value
        or any(ord(ch) < 32 for ch in value)
    ):
        raise UnsafeSandboxFile("invalid sandbox path")
    parts = value.split("/")
    if len(value.encode("utf-8")) > 512 or len(parts) > 16:
        raise UnsafeSandboxFile("sandbox path length/depth limit exceeded")
    if any(
        not part
        or part in (".", "..")
        or part.endswith((".", " "))
        or part.split(".")[0].casefold() in _DEVICES
        or _excluded_part(part)
        for part in parts
    ):
        raise UnsafeSandboxFile(f"disallowed sandbox path: {value!r}")
    return PurePosixPath(value)


def validate_snapshot(files: Mapping[str, bytes]) -> None:
    if len(files) > MAX_FILES:
        raise UnsafeSandboxFile("sandbox file count limit exceeded")
    seen: set[str] = set()
    total = 0
    for name, data in files.items():
        validate_relative_path(name)
        # Prevent aliases on Windows and case-insensitive macOS filesystems.
        canonical = name.casefold()
        if canonical in seen:
            raise UnsafeSandboxFile("case-colliding sandbox paths")
        seen.add(canonical)
        if not isinstance(data, bytes) or len(data) > MAX_FILE_BYTES:
            raise UnsafeSandboxFile(f"sandbox file size/type limit exceeded: {name!r}")
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise UnsafeSandboxFile("sandbox total byte limit exceeded")
    for name in seen:
        parts = name.split("/")
        if any("/".join(parts[:index]) in seen for index in range(1, len(parts))):
            raise UnsafeSandboxFile("sandbox file/directory collision")


def snapshot_project(root: Path) -> dict[str, bytes]:
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise UnsafeSandboxFile("sandbox project must be a directory")
    files: dict[str, bytes] = {}
    total = 0
    directories = 0
    for current, dirs, names in os.walk(root, followlinks=False):
        directories += 1
        if directories > MAX_FILES * 2:
            raise UnsafeSandboxFile("sandbox directory count limit exceeded")
        for name in list(dirs):
            path = Path(current) / name
            if _excluded_part(name):
                dirs.remove(name)
            else:
                validate_relative_path(path.relative_to(root).as_posix())
                if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
                    raise UnsafeSandboxFile("sandbox directory symlinks are forbidden")
        for name in names:
            if _excluded_part(name):
                continue
            path = Path(current) / name
            relative = path.relative_to(root).as_posix()
            validate_relative_path(relative)
            info = path.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or getattr(info, "st_file_attributes", 0) & 0x400
            ):
                raise UnsafeSandboxFile(
                    f"sandbox input must be an unlinked regular file: {relative}"
                )
            if info.st_size > MAX_FILE_BYTES or len(files) >= MAX_FILES:
                raise UnsafeSandboxFile("sandbox input limits exceeded")
            flags = (
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
            )
            with os.fdopen(os.open(path, flags), "rb") as stream:
                opened = os.fstat(stream.fileno())
                if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                    raise UnsafeSandboxFile("sandbox input changed while opening")
                data = stream.read(MAX_FILE_BYTES + 1)
            if len(data) > MAX_FILE_BYTES:
                raise UnsafeSandboxFile("sandbox input grew past the file limit")
            total += len(data)
            if total > MAX_TOTAL_BYTES:
                raise UnsafeSandboxFile("sandbox input total limit exceeded")
            files[relative] = data
    validate_snapshot(files)
    return files


def _safe_target(root: Path, name: str) -> Path:
    relative = validate_relative_path(name)
    target = root.joinpath(*relative.parts)
    for path in (target, *target.parents):
        if path == root:
            break
        if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
            raise UnsafeSandboxFile("sandbox export must not traverse a host symlink")
        if path.exists():
            info = path.lstat()
            if path == target:
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_nlink != 1
                    or getattr(info, "st_file_attributes", 0) & 0x400
                ):
                    raise UnsafeSandboxFile(
                        "sandbox export cannot overwrite a special or linked file"
                    )
            elif not stat.S_ISDIR(info.st_mode):
                raise UnsafeSandboxFile("sandbox export parent is not a directory")
    return target


def apply_snapshot(
    root: Path, files: Mapping[str, bytes], *, before: Mapping[str, bytes]
) -> None:
    """Apply a bounded export only if the controller workspace has not changed."""
    validate_snapshot(files)
    validate_snapshot(before)
    root = root.resolve(strict=True)
    if snapshot_project(root) != dict(before):
        raise UnsafeSandboxFile("host project changed during sandbox execution")
    targets = {name: _safe_target(root, name) for name in set(files) | set(before)}
    staged: list[tuple[Path, Path]] = []
    try:
        for name, data in files.items():
            if before.get(name) == data:
                continue
            target = targets[name]
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                dir=target.parent, prefix=".sandbox-export-", delete=False
            ) as out:
                temporary = Path(out.name)
                staged.append((temporary, target))
                out.write(data)
            os.chmod(temporary, 0o644)
        for temporary, target in staged:
            os.replace(temporary, target)
        for name in set(before) - set(files):
            targets[name].unlink()
    finally:
        for temporary, _ in staged:
            temporary.unlink(missing_ok=True)
