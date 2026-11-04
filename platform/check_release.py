"""Verify the frozen source manifest and optionally create a portable ZIP."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import tempfile
import zipfile

from prepare_platform_package_v13 import REQUIRED_MEMBERS, assert_package_tree


ROOT = Path(__file__).resolve().parent
PACKAGE = ROOT / "platform-agent"


def generated_artifact(path: Path) -> bool:
    relative = path.relative_to(PACKAGE)
    return ("__pycache__" in relative.parts or path.suffix in {".pyc", ".pyo"}
            or relative.as_posix().startswith((
                "arcbench-agent-runtime/build/",
                "arcbench-agent-runtime/src/arcbench_agent_runtime.egg-info/")))


def verify() -> dict:
    manifest = json.loads((ROOT / "freeze-v13.json").read_text(encoding="utf-8"))
    expected = manifest["files"]
    paths = {
        path.relative_to(PACKAGE).as_posix(): path
        for path in PACKAGE.rglob("*")
        if path.is_file() and not generated_artifact(path)
    }
    if set(paths) != set(expected):
        raise ValueError(f"Package members changed: {sorted(set(paths) ^ set(expected))}")
    if not set(REQUIRED_MEMBERS) <= set(paths):
        raise ValueError("Required package entry is absent")
    # Scan an exact member copy so import caches do not become package inputs.
    with tempfile.TemporaryDirectory() as temporary:
        copied = Path(temporary)
        for name, path in paths.items():
            raw = path.read_bytes()
            if {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)} != expected[name]:
                raise ValueError(f"Frozen source differs: {name}")
            target = copied / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
        assert_package_tree(copied)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Create a new ZIP without overwriting an existing file")
    args = parser.parse_args()
    manifest = verify()
    result = {"version": manifest["version"], "members": len(manifest["files"]),
              "source_verified": True, "formal_status": manifest["formal_status"]}
    if args.output:
        # STORE removes compression-library variation; member bytes equal the
        # frozen manifest, while ZIP timestamps and compression are canonical.
        with zipfile.ZipFile(args.output, "x", compression=zipfile.ZIP_STORED) as archive:
            for name in sorted(manifest["files"]):
                item = zipfile.ZipInfo(name, date_time=(2026, 11, 4, 0, 0, 0))
                item.create_system = 3
                item.external_attr = 0o100644 << 16
                archive.writestr(item, (PACKAGE / name).read_bytes())
        with zipfile.ZipFile(args.output) as archive:
            if archive.testzip() is not None or set(archive.namelist()) != set(manifest["files"]):
                raise ValueError("ZIP round-trip failed")
            for name, record in manifest["files"].items():
                if hashlib.sha256(archive.read(name)).hexdigest() != record["sha256"]:
                    raise ValueError(f"ZIP member changed: {name}")
        result.update(zip=str(args.output), sha256=hashlib.sha256(args.output.read_bytes()).hexdigest())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
