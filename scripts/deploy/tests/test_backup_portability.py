import hashlib
import importlib.util
import os
from pathlib import Path
import shutil

import pytest

spec = importlib.util.spec_from_file_location('backup_checksums', Path(__file__).parents[1] / 'verify-backup.py')
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)


def make_backup(directory, *, absolute):
    directory.mkdir()
    prefix = directory / '20261015T120000Z_0123456789ab'
    lines = []
    for suffix in backup.SUFFIXES:
        filename = prefix.name + '.' + suffix
        payload = ('independent encrypted fixture ' + suffix).encode()
        (directory / filename).write_bytes(payload)
        recorded = '/old-server/private/backups/' + filename if absolute else filename
        lines.append(hashlib.sha256(payload).hexdigest() + '  ' + recorded)
    prefix.with_name(prefix.name + '.sha256').write_text('\n'.join(lines) + '\n')
    return prefix


@pytest.mark.parametrize('absolute', [False, True])
def test_all_artifacts_verify_after_moving_backup(tmp_path, absolute):
    original = make_backup(tmp_path / 'source', absolute=absolute)
    relocated = tmp_path / 'another-server'
    shutil.move(str(original.parent), relocated)
    assert not original.parent.exists()
    result = backup.verify(relocated / original.name)
    assert result['status'] == 'verified'
    assert result['encrypted_artifacts'] == 4


@pytest.mark.parametrize('suffix', backup.SUFFIXES)
def test_changed_or_missing_artifact_cannot_pass(tmp_path, suffix):
    prefix = make_backup(tmp_path / 'source', absolute=True)
    artifact = prefix.with_name(prefix.name + '.' + suffix)
    artifact.write_bytes(b'changed')
    with pytest.raises(ValueError):
        backup.verify(prefix)
    artifact.unlink()
    with pytest.raises(FileNotFoundError):
        backup.verify(prefix)


@pytest.mark.parametrize('change', ['omit', 'duplicate', 'extra', 'malformed', 'oversized', 'wrong-prefix'])
def test_manifest_requires_exactly_four_unique_expected_artifacts(tmp_path, change):
    prefix = make_backup(tmp_path / 'source', absolute=False)
    manifest = prefix.with_name(prefix.name + '.sha256')
    lines = manifest.read_text().splitlines()
    if change == 'omit': lines.pop()
    elif change == 'duplicate': lines.append(lines[0])
    elif change == 'extra': lines.append('0' * 64 + '  /etc/passwd')
    elif change == 'malformed': lines[0] = 'not a checksum'
    elif change == 'oversized': lines = ['x' * 65537]
    elif change == 'wrong-prefix': lines[0] = lines[0].replace(prefix.name, 'other-backup')
    manifest.write_text('\n'.join(lines) + '\n')
    with pytest.raises(ValueError):
        backup.verify(prefix)


@pytest.mark.parametrize('manifest_link', [True, False])
def test_symlink_is_rejected_before_following_target(tmp_path, manifest_link):
    prefix = make_backup(tmp_path / 'source', absolute=False)
    selected = prefix.with_name(prefix.name + ('.sha256' if manifest_link else '.files.gpg'))
    target = tmp_path / 'unrelated-secret'
    target.write_bytes(selected.read_bytes())
    selected.unlink()
    try:
        selected.symlink_to(target)
    except OSError:
        pytest.skip('Host does not permit creating symlinks')
    with pytest.raises(ValueError):
        backup.verify(prefix)
    assert target.is_file()


@pytest.mark.skipif(os.name != 'posix', reason='FIFO requires Linux/POSIX')
def test_fifo_is_rejected_without_waiting_for_a_writer(tmp_path):
    prefix = make_backup(tmp_path / 'source', absolute=False)
    selected = prefix.with_name(prefix.name + '.database.gpg')
    selected.unlink()
    os.mkfifo(selected)
    with pytest.raises(ValueError):
        backup.verify(prefix)
