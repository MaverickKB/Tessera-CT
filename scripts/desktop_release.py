"""Assemble and verify the exact native assets consumed by desktop_install.

The same target table drives CI builds and publication verification. Nothing in
this script publishes a release or runs an installer.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import tarfile
import tomllib

TARGETS = [
    {'platform': 'linux', 'arch': 'x86_64', 'runner': 'ubuntu-24.04', 'target': 'x86_64-unknown-linux-gnu', 'bundles': 'appimage'},
    {'platform': 'linux', 'arch': 'aarch64', 'runner': 'ubuntu-24.04-arm', 'target': 'aarch64-unknown-linux-gnu', 'bundles': 'appimage'},
    {'platform': 'macos', 'arch': 'x86_64', 'runner': 'macos-15-intel', 'target': 'x86_64-apple-darwin', 'bundles': 'app'},
    {'platform': 'macos', 'arch': 'aarch64', 'runner': 'macos-15', 'target': 'aarch64-apple-darwin', 'bundles': 'app'},
    {'platform': 'windows', 'arch': 'x86_64', 'runner': 'windows-latest', 'target': 'x86_64-pc-windows-msvc', 'bundles': 'nsis,msi'},
    {'platform': 'windows', 'arch': 'aarch64', 'runner': 'windows-11-arm', 'target': 'aarch64-pc-windows-msvc', 'bundles': 'nsis,msi'},
]


def artifact_names(platform, arch):
    suffixes = {'linux': ['.AppImage'], 'macos': ['.app.tar.gz'], 'windows': ['.exe', '.msi']}[platform]
    return ['tessera-' + platform + '-' + arch + suffix for suffix in suffixes]


def only(directory, pattern):
    candidates = list(directory.glob(pattern))
    if len(candidates) != 1:
        raise ValueError(f'Expected exactly one {pattern} in {directory}, found {len(candidates)}')
    return candidates[0]


def assemble(root, output, target):
    row = next(row for row in TARGETS if row['target'] == target)
    bundle = root / 'desktop' / 'target' / target / 'release' / 'bundle'
    output.mkdir(parents=True, exist_ok=True)
    names = artifact_names(row['platform'], row['arch'])
    if row['platform'] == 'macos':
        app = only(bundle / 'macos', '*.app')
        if not app.is_dir() or not (app / 'Contents' / 'MacOS' / 'tessera-desktop').is_file():
            raise ValueError(f'{app} is not a Tessera application bundle')
        with tarfile.open(output / names[0], 'w:gz') as archive:
            archive.add(app, arcname=app.name)
    else:
        formats = [('appimage', '*.AppImage')] if row['platform'] == 'linux' else [('nsis', '*.exe'), ('msi', '*.msi')]
        for name, (directory, pattern) in zip(names, formats):
            source = only(bundle / directory, pattern)
            if not source.is_file():
                raise ValueError(f'{source} is not a file')
            shutil.copyfile(source, output / name)
    for name in names:
        digest = hashlib.sha256((output / name).read_bytes()).hexdigest()
        (output / (name + '.sha256')).write_text(digest + '  ' + name + '\n', encoding='utf-8')
    return names


def verify(directory):
    expected = {name for row in TARGETS for name in artifact_names(row['platform'], row['arch'])}
    expected |= {name + '.sha256' for name in list(expected)}
    actual = {item.name for item in directory.iterdir() if item.is_file()}
    if actual != expected:
        raise ValueError(f'Release assets differ: missing={sorted(expected - actual)}, unexpected={sorted(actual - expected)}')
    for name in sorted(expected):
        if name.endswith('.sha256'):
            continue
        digest = hashlib.sha256((directory / name).read_bytes()).hexdigest()
        if (directory / (name + '.sha256')).read_text(encoding='utf-8') != digest + '  ' + name + '\n':
            raise ValueError(f'Checksum mismatch for {name}')


def check_version(root, tag):
    package = tomllib.loads((root / 'pyproject.toml').read_text(encoding='utf-8'))['project']['version']
    native = json.loads((root / 'desktop' / 'tauri.conf.json').read_text(encoding='utf-8'))['version']
    crate = tomllib.loads((root / 'desktop' / 'Cargo.toml').read_text(encoding='utf-8'))['package']['version']
    if tag != 'v' + package or len({package, native, crate}) != 1:
        raise ValueError(f'Release tag {tag} must match package/native/crate versions: {package}/{native}/{crate}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--matrix', action='store_true')
    parser.add_argument('--target', choices=[row['target'] for row in TARGETS])
    parser.add_argument('--root', type=Path, default=Path('.'))
    parser.add_argument('--output', type=Path, default=Path('dist'))
    parser.add_argument('--verify', action='store_true')
    parser.add_argument('--check-version')
    args = parser.parse_args()
    if args.check_version:
        check_version(args.root, args.check_version)
    elif args.matrix:
        print(json.dumps({'include': TARGETS}, separators=(',', ':')))
    elif args.verify:
        verify(args.output)
    elif args.target:
        for name in assemble(args.root, args.output, args.target):
            print(name)
    else:
        parser.error('Choose --matrix, --target or --verify')


if __name__ == '__main__':
    main()
