# 校验内置资料及来源哈希，并从声明式JSON可复现生成权重快照。
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
BUILTIN = ROOT / 'src/echo_workshop/resources/builtin'


def source_inventory(folder=BUILTIN):
    items = {}
    for path in sorted(folder.iterdir()):
        if not path.is_file() or path.name == 'sources.json':
            continue
        if path.is_symlink() or path.suffix not in ('.json', '.png'):
            raise ValueError('Unexpected resource file')
        items[path.name] = {
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'bytes': path.stat().st_size,
        }
    return items


def metadata(folder=BUILTIN):
    weights = json.loads((folder / 'weights.json').read_text(encoding='utf-8'))
    catalog = json.loads((folder / 'sonata_catalog.json').read_text(encoding='utf-8'))
    effects = json.loads((folder / 'sonata_effects_catalog.json').read_text(encoding='utf-8'))
    names = json.loads((folder / 'echo_names.json').read_text(encoding='utf-8'))
    defaults = json.loads((folder / 'role_sonatas.json').read_text(encoding='utf-8'))
    panel = json.loads((folder / 'panel_catalog.json').read_text(encoding='utf-8'))
    return {
        'schema': 1,
        'generator': 'tools/resources.py',
        'baseline': '0.4.0.dev115',
        'weights': {
            'repository': weights['source_repository'],
            'commit': weights['source_commit'],
            'provenance_scope': 'Preserved builtin snapshot lineage; HTTP update bytes carry separate content hashes',
        },
        'sonata': {
            'source': catalog.get('source'),
            'source_commit': catalog.get('source_commit', ''),
            'icon_source': catalog.get('icon_source'),
        },
        'effects': {'source': effects.get('source')},
        'echo_names': names['source'],
        'role_sonatas': {key: defaults[key] for key in ('source', 'sha256', 'retrieved')},
        'game_catalog': json.loads((folder / 'game_sources.json').read_text(encoding='utf-8')),
        'panel_catalog': {
            'schema_version': panel['schema_version'],
            'game_version': panel['game_version'],
            'retrieved_at': panel['retrieved_at'],
            'characters': len(panel['characters']),
            'weapons': len(panel['weapons']),
        },
        'pinyin': {
            'project': 'pypinyin',
            'version': '0.55.0',
            'license': 'MIT',
            'notice': 'THIRD_PARTY_NOTICES.md',
        },
        'items': source_inventory(folder),
    }


def validate():
    from echo_workshop.resources.weights import WeightSnapshot
    from echo_workshop.resources.sonata import SonataSnapshot
    from echo_workshop.resources.effects import _snapshot
    from echo_workshop.resources.echo_names import builtin_echo_names
    from echo_workshop.resources.game_catalog import GameSnapshot
    from echo_workshop.resources.role_defaults import builtin_snapshot

    document = json.loads((BUILTIN / 'sources.json').read_text(encoding='utf-8'))
    if document != metadata():
        raise ValueError('Resource source inventory differs from actual files')
    data = json.loads((BUILTIN / 'weights.json').read_text(encoding='utf-8'))
    snapshot = WeightSnapshot(
        data['templates'], data['conditions'], data['source_repository'], data['source_commit']
    )
    sonata = SonataSnapshot.load(BUILTIN)
    effects = json.loads((BUILTIN / 'sonata_effects_catalog.json').read_text(encoding='utf-8'))
    _snapshot(effects['items'], effects['source'])
    readings = json.loads((BUILTIN / 'pinyin_table.json').read_text(encoding='utf-8'))
    if not isinstance(readings, dict) or not readings:
        raise ValueError('Missing offline pinyin readings')
    GameSnapshot.from_data(json.loads((BUILTIN / 'game_catalog.json').read_bytes()))
    panel = json.loads((BUILTIN / 'panel_catalog.json').read_text(encoding='utf-8'))
    from echo_workshop.resources.panel_catalog import PanelSnapshot

    PanelSnapshot.from_data(panel)
    return {
        'status': 'passed',
        'files': len(document['items']),
        'templates': len(snapshot.options),
        'sonata_icons': len(sonata.icons),
        'echo_names': len(builtin_echo_names()),
        'role_sonatas': len(builtin_snapshot().entries),
        'panel_characters': len(panel['characters']),
        'panel_weapons': len(panel['weapons']),
        'weight_version': snapshot.version,
    }


def snapshot(source, output):
    from echo_workshop.resources.http import strict_json
    from echo_workshop.resources.weights import WeightSnapshot

    if source.stat().st_size > 16 * 1024 * 1024:
        raise ValueError('Snapshot input exceeds limit')
    data = strict_json(source.read_bytes())
    required = {'templates', 'conditions', 'source_repository', 'source_commit'}
    if not isinstance(data, dict) or set(data) != required:
        raise ValueError('Snapshot requires templates, conditions and explicit provenance')
    validated = WeightSnapshot(
        data['templates'], data['conditions'], data['source_repository'], data['source_commit']
    )
    output = output.resolve()
    if output.parent != BUILTIN.resolve() or output.name != 'weights.json':
        raise ValueError('Snapshot output must be the fixed builtin weights file')
    body = (json.dumps(data, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    # 调用者提供完整且经验证的本地数据，不运行下载脚本或修改运行时权重缓存。
    output.write_bytes(body)
    (BUILTIN / 'sources.json').write_text(
        json.dumps(metadata(), ensure_ascii=False, indent=2) + '\n', encoding='utf-8'
    )
    return {
        'status': 'generated',
        'weight_version': validated.version,
        'sha256': hashlib.sha256(body).hexdigest(),
    }


def role_defaults(source, retrieved):
    from collections import Counter
    from datetime import date
    from echo_workshop.resources.http import strict_json

    date.fromisoformat(retrieved)
    if source.stat().st_size > 4 * 1024 * 1024:
        raise ValueError('Default build input exceeds limit')
    body = source.read_bytes()
    rows = strict_json(body)
    if not isinstance(rows, list) or not 1 <= len(rows) <= 300:
        raise ValueError('Invalid default build list')
    characters = {}
    for row in rows:
        role = row['role']
        ident = str(role['roleId'])
        equipment = row['phantomData']['equipPhantomList']
        if not ident.isdigit() or ident in characters or len(equipment) != 5:
            raise ValueError('Invalid default build identity or slots')
        names = [echo['fetterDetail']['name'] for echo in equipment]
        if any(not isinstance(name, str) or not name.strip() for name in names):
            raise ValueError('Invalid default sonata name')
        characters[ident] = dict(name=role['roleName'], sets=list(Counter(names).items()))
    data = dict(
        schema_version=1,
        source='https://ww3.loping151.cn/XutheringWavesUID/resource/map/1.json',
        sha256=hashlib.sha256(body).hexdigest(),
        retrieved=retrieved,
        characters=characters,
    )
    (BUILTIN / 'role_sonatas.json').write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8'
    )
    (BUILTIN / 'sources.json').write_text(
        json.dumps(metadata(), ensure_ascii=False, indent=2) + '\n', encoding='utf-8'
    )
    return {'status': 'generated', 'characters': len(characters), 'sha256': data['sha256']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('validate')
    commands.add_parser('inventory')
    generate = commands.add_parser('snapshot')
    generate.add_argument('--from-json', required=True, type=Path)
    defaults = commands.add_parser('role-defaults')
    defaults.add_argument('--from-json', required=True, type=Path)
    defaults.add_argument('--retrieved', required=True)
    args = parser.parse_args()
    if args.command == 'inventory':
        (BUILTIN / 'sources.json').write_text(
            json.dumps(metadata(), ensure_ascii=False, indent=2) + '\n', encoding='utf-8'
        )
        result = {'status': 'source_inventory_generated', 'files': len(source_inventory())}
    elif args.command == 'snapshot':
        result = snapshot(args.from_json, BUILTIN / 'weights.json')
    elif args.command == 'role-defaults':
        result = role_defaults(args.from_json, args.retrieved)
    else:
        result = validate()
    print(json.dumps(result, ensure_ascii=False, indent=2))
