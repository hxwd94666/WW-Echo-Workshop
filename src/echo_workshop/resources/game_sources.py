# 适配 Nanoka 与 Encore 的正式服目录、套装和受限图片地址。
import json
import re
from urllib.parse import urlsplit

NANOKA = 'https://static.nanoka.cc'
ENCORE = 'https://api-v2.encore.moe'
SOURCES = (NANOKA, ENCORE)
MANIFEST = NANOKA + '/manifest.json'
MAX_JSON = 4 * 1024 * 1024
ELEMENTS = {1: '冷凝', 2: '热熔', 3: '导电', 4: '气动', 5: '衍射', 6: '湮灭'}
_ID = re.compile(r'[1-9][0-9]{0,9}\Z')
_VERSION = re.compile(r'[0-9]{1,3}(?:\.[0-9]{1,3}){1,3}\Z')
_ASSET = re.compile(r'UIResources/Common/(?:Image|Atlas)/[A-Za-z0-9_/-]+\.webp\Z')


def identity(value):
    if type(value) not in (int, str) or not _ID.fullmatch(str(value)):
        raise ValueError('游戏资源 ID 无效')
    return str(value)


def label(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise ValueError('游戏资源名称无效')
    if any(ord(c) < 32 for c in value) or '<' in value or '>' in value:
        raise ValueError('游戏资源名称包含控制字符')
    return value.strip()


def approve(url):
    if not isinstance(url, str):
        raise ValueError('资源 URL 无效')
    parts = urlsplit(url)
    if parts.scheme != 'https' or parts.netloc not in ('static.nanoka.cc', 'api-v2.encore.moe'):
        raise ValueError('资源来源不在白名单')
    if parts.query or parts.fragment or '%' in parts.path or '..' in parts.path:
        raise ValueError('资源路径无效')
    path = parts.path
    if parts.netloc == 'static.nanoka.cc':
        valid = path == '/manifest.json' or re.fullmatch(
            r'/ww/[0-9]{1,3}(?:\.[0-9]{1,3}){1,3}/(?:character|echo|zh/sonata)\.json', path
        )
        asset = path.removeprefix('/assets/ww/') if path.startswith('/assets/ww/') else ''
    else:
        valid = path in ('/api/zh-Hans/character', '/api/zh-Hans/echo')
        prefix = '/resource/Data/Game/Aki/UI/'
        asset = path.removeprefix(prefix) if path.startswith(prefix) else ''
    if not valid and not _ASSET.fullmatch(asset):
        raise ValueError('资源路径不在白名单')
    return url


def asset_urls(value):
    if not isinstance(value, str):
        raise ValueError('图片路径无效')
    if value.startswith('/Game/Aki/UI/'):
        path = value[len('/Game/Aki/UI/') :]
        path = path.rsplit('/', 1)[0] + '/' + path.rsplit('/', 1)[1].split('.')[0] + '.webp'
    else:
        value = value.replace('https://api.encore.moe/', ENCORE + '/')
        approve(value)
        prefix = (
            NANOKA + '/assets/ww/'
            if value.startswith(NANOKA)
            else ENCORE + '/resource/Data/Game/Aki/UI/'
        )
        if not value.startswith(prefix):
            raise ValueError('图片 URL 不是图片资源')
        path = value[len(prefix) :]
    urls = (NANOKA + '/assets/ww/' + path, ENCORE + '/resource/Data/Game/Aki/UI/' + path)
    return tuple(approve(url) for url in urls)


def document(body):
    if not isinstance(body, bytes) or len(body) > MAX_JSON:
        raise ValueError('游戏资料大小无效')

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('游戏资料字段重复')
            result[key] = value
        return result

    value = json.loads(
        body,
        object_pairs_hook=unique,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError('非有限数值')),
    )
    if not isinstance(value, dict) or not value:
        raise ValueError('游戏目录为空')
    return value


def live_version(manifest):
    value = manifest.get('ww', {}).get('live')
    if not isinstance(value, str) or not _VERSION.fullmatch(value):
        raise ValueError('Nanoka 缺少正式服版本，不采用测试服 latest')
    return value


def _rows(value, maximum):
    if not isinstance(value, dict) or not 1 <= len(value) <= maximum:
        raise ValueError('游戏目录条目数量无效')
    return value.items()


def nanoka(characters, echoes, sonatas, version):
    from echo_workshop.resources.effects import _plain

    sets, roles, items = {}, {}, {}
    for key, row in _rows(sonatas, 256):
        ident = identity(key)
        if identity(row['id']) != ident:
            raise ValueError('套装 ID 不匹配')
        sets[ident] = dict(
            name=label(row['name']),
            icons=list(asset_urls(row['icon'])),
            tiers={str(k): _plain(v['desc'], v.get('param', ())) for k, v in row['set'].items()},
        )
    for key, row in _rows(characters, 512):
        roles[identity(key)] = dict(
            name=label(row['zh']),
            element=ELEMENTS[row['element']],
            icons=list(asset_urls(row['icon'])),
        )
    for key, row in _rows(echoes, 2048):
        groups = [identity(g) for g in row['group']]
        if any(g not in sets for g in groups):
            raise ValueError('声骸引用未知套装')
        items['nanoka:' + identity(key)] = dict(
            name=label(row['zh']), sets=groups, icons=list(asset_urls(row['icon']))
        )
    return checked(
        dict(source=NANOKA, release=version, characters=roles, echoes=items, sonatas=sets)
    )


def encore(characters, echoes):
    from echo_workshop.resources.effects import _plain

    sets, roles, items = {}, {}, {}
    for row in characters['roleList']:
        ident = identity(row['Id'])
        if ident in roles:
            raise ValueError('角色 ID 重复')
        roles[ident] = dict(
            name=label(row['Name']),
            element=ELEMENTS[row['Element']['Id']],
            icons=list(asset_urls(row['RoleHeadIcon'])),
        )
    for row in echoes['Echo']:
        groups = []
        for group in row['FetterGroups']:
            ident = identity(group['Id'])
            entry = dict(
                name=label(group['Name']),
                icons=list(asset_urls(group['Icon'])),
                tiers={str(f['Key']): _plain(f['EffectDescription']) for f in group['Fetters']},
            )
            if ident in sets and sets[ident] != entry:
                raise ValueError('Encore 同一套装数据冲突')
            if any(f['Name'] != group['Name'] for f in group['Fetters']):
                raise ValueError('Encore 套装效果关联错位')
            sets[ident] = entry
            groups.append(ident)
        ident = 'encore:' + identity(row['Id'])
        if ident in items:
            raise ValueError('声骸 ID 重复')
        items[ident] = dict(
            name=label(row['Name']), sets=groups, icons=list(asset_urls(row['Icon']))
        )
    return checked(
        dict(source=ENCORE, release='live', characters=roles, echoes=items, sonatas=sets)
    )


def checked(value):
    if value.get('source') not in SOURCES or not isinstance(value.get('release'), str):
        raise ValueError('资料来源或版本无效')
    if value['release'] != 'live' and not _VERSION.fullmatch(value['release']):
        raise ValueError('资料版本无效')
    seen = set()
    for key, row in _rows(value['sonatas'], 256):
        identity(key)
        name = label(row['name'])
        if name in seen or not isinstance(row['tiers'], dict) or not row['tiers']:
            raise ValueError('套装名称重复或效果为空')
        seen.add(name)
        for count, desc in row['tiers'].items():
            if (
                count not in ('1', '2', '3', '4', '5')
                or not isinstance(desc, str)
                or not 1 <= len(desc) <= 8192
            ):
                raise ValueError('套装效果门槛无效')
    for key, row in _rows(value['characters'], 512):
        identity(key)
        label(row['name'])
        if row['element'] not in ELEMENTS.values():
            raise ValueError('角色属性无效')
    for key, row in _rows(value['echoes'], 2048):
        namespace, ident = key.split(':')
        if namespace not in ('nanoka', 'encore'):
            raise ValueError('声骸 ID 缺少来源命名空间')
        identity(ident)
        label(row['name'])
        if (
            not isinstance(row['sets'], list)
            or not row['sets']
            or any(g not in value['sonatas'] for g in row['sets'])
        ):
            raise ValueError('声骸套装关联无效')
    for section in ('characters', 'echoes', 'sonatas'):
        for row in value[section].values():
            if (
                not isinstance(row['icons'], list)
                or len(row['icons']) != 2
                or tuple(row['icons']) != asset_urls(row['icons'][0])
            ):
                raise ValueError('图片主备路径不一致')
    return value
