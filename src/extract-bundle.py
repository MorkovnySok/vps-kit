#!/usr/bin/env python3
"""Safe streaming extraction of a CI bundle; compatible with Python >= 3.10."""
from pathlib import Path, PurePosixPath
import shutil
import sys
import tarfile

if len(sys.argv) != 2:
    raise SystemExit('usage: extract-bundle.py DESTINATION')
root = Path(sys.argv[1]).resolve()
root.mkdir(parents=True, exist_ok=True)
MAX_FILES = 25000
MAX_TOTAL = 2 * 1024 * 1024 * 1024
count = total = 0
directory_modes = {}


def make_directories(path):
    # The receiver uses umask 077. Record implicit parents too, so bind-mounted
    # directories remain traversable by container users after extraction.
    for parent in [*reversed(path.parents), path]:
        if parent != root and parent.is_relative_to(root) and not parent.exists():
            parent.mkdir()
            directory_modes[parent] = 0o755

with tarfile.open(fileobj=sys.stdin.buffer, mode='r|gz') as archive:
    for item in archive:
        name = PurePosixPath(item.name)
        parts = name.parts
        if (name.is_absolute() or '..' in parts or
                any(part in ('.git', '.ssh') or part == '.env' or part.startswith('.env.') for part in parts)):
            raise SystemExit(f'unsafe archive path: {item.name!r}')
        if not (item.isfile() or item.isdir()):
            raise SystemExit(f'unsupported archive entry (symlinks and devices rejected): {item.name!r}')
        count += 1
        total += item.size
        if count > MAX_FILES or total > MAX_TOTAL:
            raise SystemExit('bundle exceeds safety limit')
        dest = root.joinpath(*parts).resolve()
        if not dest.is_relative_to(root):
            raise SystemExit(f'archive path escapes extraction root: {item.name!r}')
        if item.isdir():
            make_directories(dest)
            if dest != root:
                directory_modes[dest] = item.mode & 0o777
        else:
            make_directories(dest.parent)
            source = archive.extractfile(item)
            if source is None:
                raise SystemExit(f'could not extract {item.name!r}')
            with source, dest.open('xb') as output:
                shutil.copyfileobj(source, output, length=1024*1024)
            dest.chmod(item.mode & 0o777)  # executable bits are useful for Docker builds
for directory in sorted(directory_modes, key=lambda p: len(p.parts), reverse=True):
    directory.chmod(directory_modes[directory])
print(f'extracted {count} entries, {total} bytes', file=sys.stderr)
