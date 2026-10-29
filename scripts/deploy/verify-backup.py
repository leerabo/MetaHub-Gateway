"""Verify all encrypted backup artifacts after relocation, without trusting manifest paths."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys

SUFFIXES = ('database.gpg', 'configuration.gpg', 'files.gpg', 'verification.gpg')


def open_regular(path: Path):
    # Do not follow the manifest or artifact symlinks into arbitrary host files.
    if path.is_symlink():
        raise ValueError('Backup symlinks are forbidden.')
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError('Backup artifact must be a regular file.')
        return os.fdopen(descriptor, 'rb')
    except BaseException:
        os.close(descriptor)
        raise


def verify(prefix: Path) -> dict:
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', prefix.name):
        raise ValueError('Invalid backup prefix.')
    expected = {prefix.name + '.' + suffix for suffix in SUFFIXES}
    with open_regular(prefix.with_name(prefix.name + '.sha256')) as source:
        raw = source.read(65537)
    if len(raw) > 65536:
        raise ValueError('Oversized backup checksum manifest.')
    checksums = {}
    for line in raw.decode('ascii').splitlines():
        match = re.fullmatch(r'([0-9a-f]{64}) [ *](.+)', line)
        if match is None:
            raise ValueError('Malformed backup checksum manifest.')
        digest, recorded_path = match.groups()
        # Legacy manifests contain absolute POSIX paths. Never open those paths;
        # verify exactly the four sibling files supplied by the operator instead.
        filename = recorded_path.rsplit('/', 1)[-1]
        if filename not in expected or filename in checksums:
            raise ValueError('Unexpected or duplicate backup artifact.')
        checksums[filename] = digest
    if set(checksums) != expected:
        raise ValueError('Incomplete backup checksum manifest.')
    total = 0
    for filename, expected_digest in checksums.items():
        with open_regular(prefix.parent / filename) as source:
            actual_digest = hashlib.file_digest(source, 'sha256').hexdigest()
            total += os.fstat(source.fileno()).st_size
        if actual_digest != expected_digest:
            raise ValueError('Backup artifact checksum differs.')
    return {'status': 'verified', 'encrypted_artifacts': len(expected), 'encrypted_bytes': total}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prefix', type=Path, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(verify(args.prefix)))
        return 0
    except Exception as error:
        # File paths and raw errors can expose host configuration; use fixed diagnostics.
        print(json.dumps({'status': 'failed', 'phase': 'backup_checksums', 'exception_type': type(error).__name__}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
