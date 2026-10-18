# 检查中文首注、文件行数、源码语法和纯计算依赖边界。
from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = (
    'echo_workshop.application',
    'echo_workshop.storage',
    'echo_workshop.resources',
    'echo_workshop.capture',
    'echo_workshop.ui',
    'echo_workshop.adapters',
    'PySide6',
    'qfluentwidgets',
    'ok',
    'sqlite3',
    'urllib',
    'requests',
    'http',
)
IO_METHODS = {'read_bytes', 'read_text', 'write_bytes', 'write_text', 'unlink', 'mkdir', 'connect'}


def imported_modules(node, path, root):
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    name = node.module or ''
    if not node.level or not path.is_relative_to(root / 'src'):
        return [name]
    parts = list(path.relative_to(root / 'src').with_suffix('').parts)
    package = parts[:-1]
    prefix = package[: len(package) - node.level + 1]
    return ['.'.join(prefix + name.split('.')) if name else '.'.join(prefix)]


def check(root=ROOT, *, core_only=False):
    errors, files = [], []
    folders = ('src/echo_workshop/core',) if core_only else ('src', 'plugin', 'tests', 'tools')
    for folder in folders:
        for path in sorted((root / folder).rglob('*.py')):
            relative = path.relative_to(root).as_posix()
            text = path.read_text(encoding='utf-8-sig')
            lines = text.splitlines()
            files.append(relative)
            if not lines or not re.match(r'^# .*?[\u3400-\u9fff]', lines[0]):
                errors.append(relative + ': first line needs a Chinese responsibility comment')
            if len(lines) > 800:
                errors.append(f'{relative}: {len(lines)} lines exceeds 800')
            try:
                tree = ast.parse(text, filename=relative, feature_version=(3, 11))
            except SyntaxError as error:
                errors.append(f'{relative}:{error.lineno}: {error.msg}')
                continue
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    imports = imported_modules(node, path, root)
                    for name in imports:
                        if name == 'echo_workshop' or name.startswith('echo_workshop.'):
                            target = root / 'src' / Path(*name.split('.'))
                            if (
                                not target.with_suffix('.py').is_file()
                                and not (target / '__init__.py').is_file()
                            ):
                                errors.append(
                                    f'{relative}:{node.lineno}: missing internal module {name}'
                                )
                        if (
                            name.startswith('echo_')
                            and not (name == 'echo_workshop' or name.startswith('echo_workshop.'))
                        ) or name in (
                            'overlay_status',
                            'xwuid_echo_data',
                        ):
                            errors.append(f'{relative}:{node.lineno}: old flat import {name}')
                        if '/core/' in relative and any(
                            name == item or name.startswith(item + '.') for item in FORBIDDEN
                        ):
                            errors.append(f'{relative}:{node.lineno}: core dependency {name}')
                        if '/ui/' in relative and (
                            name == 'echo_workshop.storage'
                            or name.startswith('echo_workshop.storage.')
                        ):
                            errors.append(
                                f'{relative}:{node.lineno}: UI must call an application service, not {name}'
                            )
                if '/core/' in relative and isinstance(node, ast.Call):
                    fun = node.func
                    if isinstance(fun, ast.Name) and fun.id == 'open':
                        errors.append(f'{relative}:{node.lineno}: core file I/O')
                    elif isinstance(fun, ast.Attribute) and fun.attr in IO_METHODS:
                        errors.append(f'{relative}:{node.lineno}: core I/O method {fun.attr}')
    return {
        'files': len(files),
        'errors': errors,
        'maximum_lines': 800,
        'core_only': core_only,
        'status': 'passed' if not errors else 'incomplete',
    }


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--core', action='store_true')
    parser.add_argument('--record', type=Path)
    args = parser.parse_args()
    result = check(core_only=args.core)
    if args.record:
        args.record.parent.mkdir(parents=True, exist_ok=True)
        args.record.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8'
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(bool(result['errors']))
