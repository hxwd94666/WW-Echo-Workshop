# 从固定提交的 XWUID 声骸登记源码静态提取 COST，不导入或执行上游代码。
import argparse
import ast
import hashlib
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


def extract(body, commit):
    if len(body) > 2 * 1024 * 1024 or not re.fullmatch(r'[a-f0-9]{40}', commit):
        raise ValueError('需要有界源码和完整提交 SHA')
    tree = ast.parse(body.decode('utf-8'))
    classes = {}
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or not node.name.startswith('Echo_'):
            continue
        values = {}
        for field in node.body:
            if (
                isinstance(field, ast.Assign)
                and len(field.targets) == 1
                and isinstance(field.targets[0], ast.Name)
            ):
                name = field.targets[0].id
                if name in ('id', 'name', 'cost'):
                    values[name] = ast.literal_eval(field.value)
        classes[node.name] = (
            values,
            [base.id for base in node.bases if isinstance(base, ast.Name)],
        )

    def resolve(name, trail=()):
        if name not in classes:
            return {}
        if name in trail or len(trail) > 32:
            raise ValueError('声骸继承循环或超限')
        own, bases = classes[name]
        value = {}
        for base in bases:
            value.update(resolve(base, (*trail, name)))
        value.update(own)
        return value

    items = {}
    for name in classes:
        row = resolve(name)
        if (
            type(row.get('id')) is not int
            or type(row.get('cost')) is not int
            or row['cost'] not in (1, 3, 4)
        ):
            raise ValueError('登记声骸缺少明确 ID 或 COST：' + name)
        if not isinstance(row.get('name'), str) or not row['name'] or len(row['name']) > 128:
            raise ValueError('声骸名称无效')
        ident = str(row['id'])
        if ident in items:
            raise ValueError('声骸登记 ID 重复')
        items[ident] = {'name': row['name'], 'cost': row['cost']}
    if not 1 <= len(items) <= 2048:
        raise ValueError('声骸目录条目超限')
    return dict(
        schema_version=1,
        commit=commit,
        source=f'https://github.com/Loping151/XutheringWavesUID/blob/{commit}/XutheringWavesUID/utils/damage/register_echo.py',
        sha256=hashlib.sha256(body).hexdigest(),
        items=items,
    )


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('source', type=Path)
    parser.add_argument('--commit', required=True)
    args = parser.parse_args()
    data = extract(args.source.read_bytes(), args.commit)
    target = ROOT / 'src/echo_workshop/resources/builtin/echo_costs.json'
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(
        json.dumps(
            {'items': len(data['items']), 'source_sha256': data['sha256']}, ensure_ascii=False
        )
    )
