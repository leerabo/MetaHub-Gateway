"""Exercise relocated GPG artifacts and unsafe filesystem boundaries on Linux."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tool', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    assert os.name == 'posix'
    spec = importlib.util.spec_from_file_location('backup_portability_tool', args.tool)
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    checks = []
    with tempfile.TemporaryDirectory(prefix='metahub-backup-portability-') as temporary:
        root = Path(temporary)
        source = root / 'source'
        source.mkdir(mode=0o700)
        key = root / 'fixture.key'
        key.write_text(secrets.token_hex(32))
        key.chmod(0o400)
        prefix = source / '20261015T120000Z_0123456789ab'
        originals = {}
        for suffix in tool.SUFFIXES:
            artifact = prefix.with_name(prefix.name + '.' + suffix)
            payload = b'Independent acceptance artifact only: ' + suffix.encode()
            subprocess.run(['gpg', '--batch', '--yes', '--quiet', '--pinentry-mode', 'loopback', '--passphrase-file', str(key), '--symmetric', '--cipher-algo', 'AES256', '--output', str(artifact)], input=payload, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            originals[artifact.name] = (artifact.read_bytes(), payload)
        relocated = root / 'other-server'
        shutil.move(source, relocated)
        prefix = relocated / prefix.name
        manifest = prefix.with_name(prefix.name + '.sha256')
        def manifest_lines(absolute):
            return [hashlib.sha256(encrypted).hexdigest() + '  ' + ('/missing-old-server/backups/' if absolute else '') + name for name, (encrypted, _) in originals.items()]
        def write_manifest(lines):
            manifest.write_text('\n'.join(lines) + '\n')
        for absolute in (False, True):
            write_manifest(manifest_lines(absolute))
            assert tool.verify(prefix)['encrypted_artifacts'] == 4
            checks.append('relocated_legacy_absolute_manifest' if absolute else 'relocated_relative_manifest')
        for name, (_, expected) in originals.items():
            decrypted = subprocess.run(['gpg', '--batch', '--quiet', '--pinentry-mode', 'loopback', '--passphrase-file', str(key), '--decrypt', str(relocated / name)], check=True, capture_output=True).stdout
            assert decrypted == expected
        checks.append('all_four_relocated_gpg_artifacts_decrypt')
        def rejected(label):
            try:
                tool.verify(prefix)
            except (ValueError, FileNotFoundError):
                checks.append(label)
            else:
                raise AssertionError('Unsafe backup accepted')
        for name, (encrypted, _) in originals.items():
            path = relocated / name
            path.write_bytes(encrypted + b'changed')
            rejected('changed_' + name.rsplit('.', 2)[-2])
            path.write_bytes(encrypted)
        write_manifest(manifest_lines(True) + [manifest_lines(True)[0]])
        rejected('duplicate_manifest_entry')
        write_manifest(manifest_lines(True)[:-1])
        rejected('missing_manifest_entry')
        write_manifest(manifest_lines(True) + ['0' * 64 + '  /etc/passwd'])
        rejected('unexpected_host_path')
        write_manifest(manifest_lines(True))
        saved = manifest.read_bytes()
        target = root / 'unrelated-file'
        target.write_bytes(saved)
        manifest.unlink()
        manifest.symlink_to(target)
        rejected('manifest_symlink')
        manifest.unlink()
        manifest.write_bytes(saved)
        artifact = prefix.with_name(prefix.name + '.files.gpg')
        encrypted = artifact.read_bytes()
        target.write_bytes(encrypted)
        artifact.unlink()
        artifact.symlink_to(target)
        rejected('artifact_symlink')
        artifact.unlink()
        os.mkfifo(artifact)
        rejected('artifact_fifo_without_writer')
        artifact.unlink()
        artifact.write_bytes(encrypted)
        assert tool.verify(prefix)['status'] == 'verified'
    report = {'status': 'passed', 'cases': checks, 'case_count': len(checks), 'real_gpg_encryption_and_decryption': True, 'original_customer_data_accessed': False, 'real_postgresql_restore_executed': False, 'complete_backup_restore_verified': False}
    args.report.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report))


if __name__ == '__main__':
    main()
