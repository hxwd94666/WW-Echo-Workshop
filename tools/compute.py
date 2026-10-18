# 构建和检查独立 Rust 计算程序并记录可复查的命令输出。
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import tomllib

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / 'src/echo_workshop/native/compute/Cargo.toml'
TARGET = ROOT / 'build/native'


def _host_triple(toolchain, env):
    """Resolve the locked toolchain's host triple for reproducibility flags."""
    try:
        located = subprocess.run(
            ['rustup', 'which', '--toolchain', toolchain, 'rustc'],
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            env=env,
        )
        if located.returncode:
            return ''
        version = subprocess.run(
            [located.stdout.strip(), '-vV'],
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            env=env,
        )
    except OSError:
        return ''
    for line in version.stdout.splitlines():
        if line.startswith('host:'):
            return line.split(':', 1)[1].strip()
    return ''


def execute(command, *, record=None):
    env = dict(os.environ)
    temporary = ROOT / '.tmp'
    temporary.mkdir(exist_ok=True)
    env['TEMP'] = env['TMP'] = str(temporary)
    toolchain = tomllib.loads(
        (MANIFEST.parent / 'rust-toolchain.toml').read_text(encoding='utf-8')
    )['toolchain']['channel']
    env['RUSTUP_TOOLCHAIN'] = toolchain
    # MSVC 默认把链接时间写进 PE 头；/Brepro 让同源重构得到相同字节。
    if 'msvc' in _host_triple(toolchain, env):
        flags = env.get('RUSTFLAGS', '').strip()
        if '/Brepro' not in flags:
            env['RUSTFLAGS'] = (flags + ' -C link-arg=/Brepro').strip()
    located = subprocess.run(
        ['rustup', 'which', '--toolchain', toolchain, command[0]],
        capture_output=True,
        text=True,
        encoding='utf-8',
        errors='replace',
        env=env,
    )
    if located.returncode:
        raise RuntimeError('缺少锁定的本地 Rust 工具链：' + located.stderr)
    tool = Path(located.stdout.strip())
    command = [str(tool), *command[1:]]
    env['PATH'] = str(tool.parent) + os.pathsep + env.get('PATH', '')
    env['CARGO_TARGET_DIR'] = str(TARGET)
    env['CLIPPY_CONF_DIR'] = str(MANIFEST.parent)
    started = time.monotonic()
    result = subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding='utf-8',
        errors='replace',
    )
    report = dict(
        command=command,
        cwd=str(ROOT),
        stdout=result.stdout,
        stderr=result.stderr,
        exit_code=result.returncode,
        seconds=time.monotonic() - started,
    )
    if record is not None:
        record = Path(record)
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return result.returncode


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('build', 'test', 'fmt', 'clippy'))
    parser.add_argument('--record', type=Path)
    args = parser.parse_args()
    if args.action == 'fmt':
        command = ['rustfmt', '--config-path', str(MANIFEST.parent / 'rustfmt.toml'), '--check']
        command.append(str(MANIFEST.parent / 'build.rs'))
        command += [str(p) for p in sorted((MANIFEST.parent / 'src').glob('*.rs'))]
    else:
        command = ['cargo', args.action, '--manifest-path', str(MANIFEST)]
        command += ['--offline', '--locked', '--target-dir', str(TARGET)]
        if args.action == 'build':
            command.append('--release')
        elif args.action == 'clippy':
            command += ['--all-targets', '--', '-D', 'warnings']
    return execute(command, record=args.record)


if __name__ == '__main__':
    raise SystemExit(main())
