# 盘点跨层业务计算定义与直接调用，原 Python 性能基线仅保留冻结证据。
import argparse
import ast
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT)]
DOMAINS = (
    'scoring',
    'rating',
    'compatibility',
    'filters',
    'costs',
    'requirements',
    'loadout',
    'allocation',
)


def inventory():
    modules, callers = [], []
    for path in sorted((ROOT / 'src/echo_workshop').rglob('*.py')):
        source = path.read_text(encoding='utf-8')
        tree = ast.parse(source)
        relative = path.relative_to(ROOT).as_posix()
        if '/core/' in relative and any(f'/{name}' in relative for name in DOMAINS):
            functions = [
                dict(name=node.name, line=node.lineno, end_line=node.end_lineno)
                for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]
            modules.append(
                dict(
                    path=relative,
                    sha256=hashlib.sha256(source.encode()).hexdigest(),
                    functions=functions,
                )
            )
        imports = [
            dict(module=node.module, names=[alias.name for alias in node.names], line=node.lineno)
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and node.module
            and node.module.startswith('echo_workshop.core.')
            and any(node.module.split('.')[2] == name for name in DOMAINS)
        ]
        if imports:
            callers.append(dict(path=relative, imports=imports))
    return dict(
        schema_version=1,
        coverage='core definitions and direct cross-layer import edges; indirect calls require review',
        modules=modules,
        callers=callers,
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('inventory',))
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = inventory()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8'
    )
    print(
        json.dumps(
            dict(
                output=str(args.output),
                action=args.action,
                modules=len(result.get('modules', [])),
                callers=len(result.get('callers', [])),
            ),
            ensure_ascii=False,
        )
    )


if __name__ == '__main__':
    main()
