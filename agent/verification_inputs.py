"""Conservative bounded observation of verification inputs."""
import hashlib
import os
import time
from pathlib import Path


def verification_input_fingerprint(project: Path) -> str | None:
    """Observe local check inputs without reading secrets or hashing dependencies."""
    digest = hashlib.sha256()
    content_suffixes = {'.js', '.jsx', '.mjs', '.cjs', '.ts', '.tsx', '.py',
                        '.css', '.html', '.sh', '.ps1', '.yaml', '.yml'}
    manifests = {'package.json', 'package-lock.json', 'npm-shrinkwrap.json',
                 'tsconfig.json', 'jsconfig.json'}
    root = project.resolve()
    content_bytes = 0
    started = time.monotonic()
    observed_nodes = 0
    def raise_walk_error(error):
        raise error
    try:
        for current, directories, names in os.walk(root, followlinks=False, onerror=raise_walk_error):
            observed_nodes += 1 + len(directories) + len(names)
            if observed_nodes > 50_000 or time.monotonic() - started > 2:
                return None
            base = Path(current)
            directories[:] = sorted(name for name in directories if name != '.git')
            # External directory links can hide changed dependency/test inputs.
            if any((base / name).resolve() != base / name for name in directories):
                return None
            for name in sorted(names):
                if time.monotonic() - started > 2:
                    return None
                path = base / name
                resolved = path.resolve()
                if not resolved.is_relative_to(root):
                    return None
                relative = path.relative_to(root)
                stat = path.stat()
                digest.update(repr((relative.as_posix(), stat.st_size, stat.st_mtime_ns,
                                    stat.st_ctime_ns, stat.st_ino)).encode())
                # Dependency installs and runtime/secret files are observed by
                # metadata. Hash source and check scripts too, including .arc.
                if ('node_modules' not in relative.parts and not name.startswith('.')
                        and (path.suffix.lower() in content_suffixes or name in manifests)):
                    content_bytes += stat.st_size
                    if content_bytes > 16 * 1024 * 1024:
                        return None
                    digest.update(hashlib.sha256(path.read_bytes()).digest())
        return digest.hexdigest()
    except (OSError, ValueError):
        return None

