# 只运行显式指定的计算或关键契约检查，避免自动遍历旧套件。
import argparse
import os
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT)]

# 强路径契约：任一失败即意味着整条功能线不可用；各自独立进程运行，避免 Qt/计算串扰。
SMOKE_MODULES = (
    'tests.contracts.test_host_loading',
    'tests.contracts.test_compute_startup',
    'tests.contracts.test_compute_process',
    'tests.contracts.test_compute_chunks',
    'tests.contracts.test_native_package',
    'tests.contracts.test_data_compatibility',
    'tests.contracts.test_loadout_actions',
    'tests.contracts.test_game_resources',
    'tests.contracts.test_update_fallback',
)


def run_smoke():
    environment = dict(os.environ)
    environment.setdefault('QT_QPA_PLATFORM', 'offscreen')
    failed = []
    for name in SMOKE_MODULES:
        print(f'--- {name} ---', flush=True)
        completed = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), name],
            cwd=ROOT,
            env=environment,
        )
        if completed.returncode:
            failed.append(name)
    if failed:
        print('SMOKE FAILED: ' + ', '.join(failed))
        return False
    print(f'SMOKE OK: {len(SMOKE_MODULES)} contract modules')
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('names', nargs='*', help='unittest dotted method or module names')
    parser.add_argument(
        '--calculation', action='store_true', help='Explicit calculation migration convergence'
    )
    parser.add_argument(
        '--smoke', action='store_true', help='Run the strong-path contract modules in isolation'
    )
    args = parser.parse_args()
    if args.smoke and (args.calculation or args.names):
        parser.error('Choose smoke, named checks, or calculation convergence')
    if args.smoke:
        return 0 if run_smoke() else 1
    if args.calculation:
        suite = unittest.defaultTestLoader.discover(
            str(ROOT / 'tests/calculation'), pattern='test_*.py'
        )
    elif args.names:
        suite = unittest.defaultTestLoader.loadTestsFromNames(args.names)
    else:
        parser.error('Specify related checks; there is no implicit full-suite mode')
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return not result.wasSuccessful()


if __name__ == '__main__':
    raise SystemExit(main())
