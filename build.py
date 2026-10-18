# 从唯一包源码递归构建并校验可复现的OKWW插件归档。
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'tools'))

from check_structure import check  # noqa: E402
from resources import validate as validate_resources  # noqa: E402
INSTALL_ID = 'echo-workshop'


def version():
    tree = ast.parse((ROOT / 'src/echo_workshop/__init__.py').read_text(encoding='utf-8'))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == '__version__' for t in node.targets
        ):
            value = ast.literal_eval(node.value)
            if isinstance(value, str):
                return value
    raise ValueError('Missing unique package version')


def native_members():
    source = ROOT / 'src/echo_workshop/native/compute'
    allowed = {
        'Cargo.toml',
        'Cargo.lock',
        'rust-toolchain.toml',
        'rustfmt.toml',
        'clippy.toml',
        'build.rs',
        'data/constants.json',
    }
    members = {}
    for path in sorted(source.rglob('*')):
        if not path.is_file():
            continue
        relative = path.relative_to(source).as_posix()
        if path.is_symlink() or not (
            relative in allowed or relative.startswith('src/') and path.suffix == '.rs'
        ):
            raise ValueError('Unexpected native source member: ' + relative)
        members[relative] = path.read_bytes()
    digest = hashlib.sha256()
    for name, data in sorted(members.items()):
        digest.update(name.encode() + b'\0' + data + b'\0')
    expected_source = digest.hexdigest()
    result = subprocess.run(
        [sys.executable, '-B', str(ROOT / 'tools/compute.py'), 'build'],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding='utf-8',
        errors='replace',
    )
    if result.returncode:
        raise RuntimeError('Rust build failed: ' + result.stdout + result.stderr)
    executable = ROOT / 'build/native/release/echo-workshop-compute.exe'
    description = subprocess.run(
        [str(executable), '--describe'],
        capture_output=True,
        text=True,
        encoding='utf-8',
        timeout=3,
        check=True,
    )
    metadata = json.loads(description.stdout)
    if metadata.get('source_id') != expected_source or metadata.get('product_version') != version():
        raise ValueError('Compiled engine source/product identity mismatch')
    body = executable.read_bytes()
    metadata['sha256'] = hashlib.sha256(body).hexdigest()
    metadata['sources'] = {name: hashlib.sha256(data).hexdigest() for name, data in members.items()}
    return {
        'echo_workshop/native/bin/echo-workshop-compute.exe': body,
        'echo_workshop/native/manifest.json': (
            json.dumps(metadata, sort_keys=True, indent=2) + '\n'
        ).encode(),
    }


def contents(native):
    result = {}
    for folder, prefix in (
        (ROOT / 'src/echo_workshop', 'echo_workshop'),
        (ROOT / 'plugin/okww', ''),
    ):
        for path in sorted(folder.rglob('*')):
            if not path.is_file() or '__pycache__' in path.parts or path.name == 'manifest.json':
                continue
            if folder == ROOT / 'src/echo_workshop' and path.is_relative_to(
                folder / 'native/compute'
            ):
                continue  # Native sources are validated and represented by the compiled-source manifest.
            if path.is_symlink() or path.suffix not in ('.py', '.json', '.png'):
                raise ValueError('Unexpected source member: ' + str(path))
            relative = Path(prefix) / path.relative_to(folder)
            key = relative.as_posix()
            if key in result:
                raise ValueError('Duplicate package path: ' + key)
            result[key] = path.read_bytes()
    result.update(native)
    manifest = json.loads((ROOT / 'plugin/okww/manifest.json').read_text(encoding='utf-8'))
    if manifest['file_name'] != INSTALL_ID:
        raise ValueError('Install identity migration is a separate operation')
    manifest['version'] = version()
    result['manifest.json'] = (json.dumps(manifest, ensure_ascii=False, indent=2) + '\n').encode(
        'utf-8'
    )
    for source, target in (
        ('LICENSE', 'LICENSE.txt'),
        ('THIRD_PARTY_NOTICES.md', 'THIRD_PARTY_NOTICES.md'),
        ('docs/user/guide.md', 'README.md'),
    ):
        result[target] = (ROOT / source).read_bytes()
    expected_tasks = {
        'echo_workshop_settings.py',
        'echo_workshop_overlay.py',
        'echo_workshop_scan.py',
    }
    root_tasks = {key for key in result if '/' not in key and key.endswith('.py')}
    if root_tasks != expected_tasks:
        raise ValueError('The host must discover exactly three thin task entries')
    return result


def verify_archive(archive, expected):
    with zipfile.ZipFile(archive) as package:
        names = package.namelist()
        if len(names) != len(set(names)) or set(names) != {
            INSTALL_ID + '/' + name for name in expected
        }:
            raise ValueError('Archive path inventory differs from source')
        for name, body in expected.items():
            if package.read(INSTALL_ID + '/' + name) != body:
                raise ValueError('Archive bytes differ: ' + name)
        if package.testzip() is not None:
            raise ValueError('Corrupted archive')


def build(output=None):
    structural = check()
    if structural['errors']:
        raise ValueError('Structure gate not passed: ' + '\n'.join(structural['errors']))
    resource_check = validate_resources()
    explicit = output is not None
    output = Path(output).resolve() if explicit else ROOT.resolve()
    source = (ROOT / 'src').resolve()
    if output == source or source in output.parents:
        raise ValueError('Build output must not overwrite source')
    # 解压目录固定在 dist，发行 ZIP 默认落在仓库根目录，便于手动取用。
    folder_root = output if explicit else (ROOT / 'dist')
    folder_root.mkdir(parents=True, exist_ok=True)
    expected = contents(native_members())
    folder = folder_root / INSTALL_ID
    if folder.exists():
        # 删除只针对固定构建目录，并先验证其绝对位置及无外部链接。
        if folder.resolve().parent != folder_root or folder.is_symlink():
            raise ValueError('Unexpected build folder location')
        if any(path.is_symlink() for path in folder.rglob('*')):
            raise ValueError('Build folder contains a symbolic link')
        shutil.rmtree(folder)
    folder.mkdir()
    for name, body in expected.items():
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    target = output / f'echo-workshop-{version()}.zip'
    fd, temporary = tempfile.mkstemp(prefix='.echo_workshop-', suffix='.zip', dir=output)
    os.close(fd)
    try:
        with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as package:
            for name in sorted(expected):
                info = zipfile.ZipInfo(INSTALL_ID + '/' + name, date_time=(2020, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                package.writestr(info, expected[name])
        verify_archive(temporary, expected)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    actual = {
        p.relative_to(folder).as_posix(): p.read_bytes() for p in folder.rglob('*') if p.is_file()
    }
    if actual != expected:
        raise ValueError('Build directory bytes differ from source')
    return {
        'version': version(),
        'folder': str(folder),
        'archive': str(target),
        'files': len(expected),
        'sha256': hashlib.sha256(target.read_bytes()).hexdigest(),
        'resources': resource_check,
    }


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    print(json.dumps(build(args.output), ensure_ascii=False, indent=2))
