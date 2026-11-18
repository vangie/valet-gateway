#!/usr/bin/env python3
"""Build portable gateway packages and verifiable, independently versioned receipts."""
import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[1]
VERSION = '0.1.0'
HERMES = {'__init__.py', 'adapter.py', 'passport_auth.py', 'passport_ima.py',
          'passport_protocol.py', 'passport_speech.py', 'passport_talk.py',
          'plugin.yaml', 'README.md', 'README.zh_CN.md'}


def tarball(target, prefix, sources):
    with target.open('xb') as out:
        with gzip.GzipFile(fileobj=out, filename='', mode='wb', mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode='w') as tar:
                for name, path in sorted(sources.items()):
                    if path.is_symlink() or not path.is_file() or not path.stat().st_size:
                        raise ValueError('Missing or unsafe package source: ' + name)
                    data = path.read_bytes()
                    item = tarfile.TarInfo(prefix + '/' + name)
                    item.size = len(data)
                    item.mode = 0o644
                    tar.addfile(item, io.BytesIO(data))


def package(destination):
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    if subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT, text=True).strip():
        raise ValueError('Commit reviewed sources before release packaging')
    destination.mkdir(parents=True, exist_ok=True)
    if list(destination.iterdir()):
        raise ValueError('Use an empty release directory')
    plugin = ROOT / 'tools/openclaw'
    metadata = json.loads((plugin / 'package.json').read_text())
    report = json.loads(subprocess.check_output(
        ['npm', 'pack', '--ignore-scripts', '--json', '--pack-destination', str(destination)], cwd=plugin, text=True))[0]
    allowed = set(metadata['files']) | {'package.json'}
    with tarfile.open(destination / report['filename']) as archive:
        if {item.name.removeprefix('package/') for item in archive} != allowed:
            raise ValueError('OpenClaw package allowlist mismatch')
        if not all(item.isfile() for item in archive):
            raise ValueError('OpenClaw package contains links')
    hermes_version = re.findall(r'^version: (\S+)$', (ROOT / 'tools/hermes/plugin.yaml').read_text(), re.M)[0]
    tarball(destination / f'ai-passport-hermes-{hermes_version}.tar.gz', 'ai-passport-hermes',
            {**{name: ROOT / 'tools/hermes' / name for name in HERMES}, 'LICENSE': ROOT / 'LICENSE'})
    names = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode().split('\0')
    tarball(destination / f'valet-gateway-{VERSION}-source.tar.gz', f'valet-gateway-{VERSION}',
            {name: ROOT / name for name in names if name})
    assets = {p.name: {'size': p.stat().st_size, 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
              for p in destination.iterdir()}
    manifest = {'schema_version': 1, 'version': VERSION, 'tag': f'v{VERSION}', 'commit_sha': commit,
                'adapter_versions': {'openclaw': metadata['version'], 'hermes': hermes_version},
                'protocol': 1, 'assets': assets}
    (destination / 'release-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    sums = ''.join(f'{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n'
                   for p in sorted(destination.iterdir()))
    (destination / 'SHA256SUMS.txt').write_text(sums)
    verify(destination, commit)


def verify(directory, commit):
    receipt = json.loads((directory / 'release-manifest.json').read_text())
    if receipt['tag'] != f'v{VERSION}' or receipt['commit_sha'] != commit or not re.fullmatch('[0-9a-f]{40}', commit):
        raise ValueError('Release identity mismatch')
    expected = set(receipt['assets']) | {'release-manifest.json', 'SHA256SUMS.txt'}
    if {p.name for p in directory.iterdir()} != expected:
        raise ValueError('Release file set mismatch')
    sums = {}
    for line in (directory / 'SHA256SUMS.txt').read_text().splitlines():
        match = re.fullmatch(r'([a-f0-9]{64})  ([A-Za-z0-9_.-]+)', line)
        if not match or match[2] in sums:
            raise ValueError('Invalid checksums')
        sums[match[2]] = match[1]
    if set(sums) != expected - {'SHA256SUMS.txt'}:
        raise ValueError('Incomplete checksums')
    for name, digest in sums.items():
        path = directory / name
        if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError('Checksum mismatch: ' + name)
        if name in receipt['assets'] and receipt['assets'][name] != {'size': path.stat().st_size, 'sha256': digest}:
            raise ValueError('Manifest mismatch: ' + name)
    print('Gateway release verified:', receipt['tag'], commit)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['package', 'verify'])
    parser.add_argument('directory', type=Path)
    parser.add_argument('--commit')
    args = parser.parse_args()
    if args.command == 'package':
        package(args.directory.resolve())
    else:
        verify(args.directory.resolve(), args.commit)
