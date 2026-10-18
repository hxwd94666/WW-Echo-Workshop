# 验证游戏资料主备、正式服选择、原子缓存、图片解耦与取消合同。
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from echo_workshop.application.operations import OperationCancelled
from echo_workshop.resources import game_sources as s
from echo_workshop.resources.game_catalog import GameCatalog, GameCatalogClient, GameCatalogUpdates
from echo_workshop.resources.game_assets import GameAssets, placeholder_png, CatalogAvatarFetcher
from echo_workshop.resources.game_projection import CatalogSonataClient, CatalogEffectsClient
from echo_workshop.resources.sonata import SonataProvider, SonataUpdateService
from echo_workshop.resources.effects import SonataEffectsProvider, SonataEffectsUpdateService
from echo_workshop.resources.paths import builtin_path


def fixtures():
    data = json.loads(builtin_path('game_catalog.json').read_bytes())
    sets, roles, echoes = data['sonatas'], data['characters'], data['echoes']
    manifest = {'ww': {'live': '3.7', 'latest': '99.1'}}
    chars = {
        k: dict(
            zh=v['name'],
            element=next(i for i, n in s.ELEMENTS.items() if n == v['element']),
            icon=v['icons'][0],
        )
        for k, v in roles.items()
    }
    sonata = {
        k: dict(
            id=int(k),
            name=v['name'],
            icon=v['icons'][0],
            set={n: {'desc': d} for n, d in v['tiers'].items()},
        )
        for k, v in sets.items()
    }
    echo = {
        k.split(':')[1]: dict(zh=v['name'], group=v['sets'], icon=v['icons'][0])
        for k, v in echoes.items()
    }
    encore_chars = {
        'roleList': [
            dict(
                Id=int(k),
                Name=v['name'],
                Element={'Id': chars[k]['element']},
                RoleHeadIcon=v['icons'][1],
            )
            for k, v in roles.items()
        ]
    }
    encore_echo = {
        'Echo': [
            dict(
                Id=int(k),
                Name=v['zh'],
                Icon=v['icon'],
                FetterGroups=[
                    dict(
                        Id=int(g),
                        Name=sets[g]['name'],
                        Icon=sets[g]['icons'][1],
                        Fetters=[
                            dict(Key=int(n), Name=sets[g]['name'], EffectDescription=d)
                            for n, d in sets[g]['tiers'].items()
                        ],
                    )
                    for g in v['group']
                ],
            )
            for k, v in echo.items()
        ]
    }
    return {
        s.MANIFEST: manifest,
        s.NANOKA + '/ww/3.7/character.json': chars,
        s.NANOKA + '/ww/3.7/echo.json': echo,
        s.NANOKA + '/ww/3.7/zh/sonata.json': sonata,
        s.ENCORE + '/api/zh-Hans/character': encore_chars,
        s.ENCORE + '/api/zh-Hans/echo': encore_echo,
    }


class GameResourceContract(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.catalog = GameCatalog(self.root)
        self.documents = fixtures()
        self.calls = []

    def transport(self, url, headers, limit, timeout):
        self.calls.append((url, headers))
        return (
            200,
            json.dumps(self.documents[url], ensure_ascii=False).encode(),
            {'If-None-Match': '"fixture"'},
        )

    def test_primary_live_not_latest(self):
        data, raw = GameCatalogClient(transport=self.transport).fetch(self.catalog)
        self.assertEqual(s.NANOKA, data['source'])
        self.assertEqual(4, len(raw))
        self.assertFalse(any('99.1' in url for url, _ in self.calls))

    def test_primary_timeout_falls_back_without_cross_source_headers(self):
        def fetch(url, headers, *args):
            if url.startswith(s.NANOKA):
                raise TimeoutError('offline')
            self.assertEqual({}, headers)
            return self.transport(url, headers, *args)

        data, raw = GameCatalogClient(transport=fetch).fetch(self.catalog)
        self.assertEqual(s.ENCORE, data['source'])
        self.assertTrue(all(k.startswith(s.ENCORE) for k in raw))
        self.assertTrue(all(k.startswith('encore:') for k in data['echoes']))

    def test_304_and_offline_restore(self):
        data, raw = GameCatalogClient(transport=self.transport).fetch(self.catalog)
        self.catalog.activate(data, raw, checked_at=100)
        restored = GameCatalog(self.root)

        def cached(url, headers, *args):
            self.assertEqual({'If-None-Match': '"fixture"'}, headers)
            return 304, b'', {}

        client = GameCatalogClient(transport=cached)
        again, _ = client.fetch(restored)
        self.assertEqual(4, client.cache_hits)
        self.assertEqual(data, again)
        self.assertEqual(self.catalog.active.version, restored.active.version)

    def test_failure_does_not_replace_cache(self):
        data, raw = GameCatalogClient(transport=self.transport).fetch(self.catalog)
        self.catalog.activate(data, raw)
        before = self.catalog.path.read_bytes()

        def fail(*args):
            raise TimeoutError('offline')

        service = GameCatalogUpdates(
            self.catalog, client=GameCatalogClient(transport=fail), clock=lambda: 100
        )
        self.assertFalse(service.check_once())
        self.assertEqual('failed', service.status.state)
        self.assertEqual(1900, service.status.next_check)
        self.assertEqual(before, self.catalog.path.read_bytes())

    def test_invalid_primary_falls_back(self):
        self.documents[s.NANOKA + '/ww/3.7/zh/sonata.json']['1']['id'] = 999
        data, _ = GameCatalogClient(transport=self.transport).fetch(self.catalog)
        self.assertEqual(s.ENCORE, data['source'])

    def test_wrong_nested_type_uses_fallback(self):
        self.documents[s.MANIFEST] = {'ww': []}
        data, _ = GameCatalogClient(transport=self.transport).fetch(self.catalog)
        self.assertEqual(s.ENCORE, data['source'])

    def test_partial_catalog_is_rejected(self):
        del self.documents[s.NANOKA + '/ww/3.7/character.json']['1102']

        def fetch(url, *args):
            if url.startswith(s.ENCORE):
                raise TimeoutError('offline')
            return self.transport(url, *args)

        with self.assertRaises(ValueError):
            GameCatalogClient(transport=fetch).fetch(self.catalog)

    def test_cancellation_does_not_fallback_or_commit(self):
        token = threading.Event()

        def fetch(*args):
            token.set()
            return self.transport(*args)

        with self.assertRaises(OperationCancelled):
            GameCatalogClient(transport=fetch).fetch(self.catalog, cancel_event=token)
        self.assertEqual(1, len(self.calls))
        self.assertFalse(self.catalog.path.exists())

    def test_atomic_replace_failure_retains_active(self):
        data, raw = GameCatalogClient(transport=self.transport).fetch(self.catalog)
        before = self.catalog.active
        with patch('echo_workshop.resources.game_catalog.os.replace', side_effect=OSError('disk')):
            with self.assertRaises(OSError):
                self.catalog.activate(data, raw)
        self.assertIs(before, self.catalog.active)
        self.assertFalse(self.catalog.path.exists())

    def test_corrupt_cache_restores_builtin(self):
        self.catalog.path.parent.mkdir(parents=True)
        self.catalog.path.write_text('{broken', encoding='utf-8')
        restored = GameCatalog(self.root)
        self.assertTrue(restored.warning)
        self.assertEqual(self.catalog.active.version, restored.active.version)

    def test_reject_untrusted_paths_and_duplicate_keys(self):
        for url in (
            'http://static.nanoka.cc/manifest.json',
            s.NANOKA + '/assets/ww/../secret',
            s.NANOKA + '/manifest.json?x=1',
            'https://evil.test/image.webp',
        ):
            with self.assertRaises(ValueError):
                s.approve(url)
        with self.assertRaises(ValueError):
            s.document(b'{"a": 1, "a": 2}')

    def test_asset_primary_failure_and_conditional_backup(self):
        urls = next(iter(self.catalog.active.entries.values()))['icons']

        def fetch(url, headers, *args):
            if url.startswith(s.NANOKA):
                raise TimeoutError('offline')
            return (
                (304, b'', {})
                if headers
                else (200, placeholder_png(), {'If-None-Match': '"asset"'})
            )

        assets = GameAssets(self.root, transport=fetch)
        self.assertEqual(placeholder_png(), assets.load(urls))
        self.assertEqual(placeholder_png(), assets.load(urls))
        self.assertEqual(1, assets.cache_hits)

    def test_failed_icons_do_not_block_names_or_effects(self):
        def fail(*args):
            raise TimeoutError('offline')

        assets = GameAssets(self.root, transport=fail)
        sonata = SonataProvider(self.root)
        client = CatalogSonataClient(self.catalog, assets)
        update = SonataUpdateService(sonata, client=client)
        update.check_once()
        self.assertNotEqual('failed', update.status.state)
        self.assertEqual(len(self.catalog.active.entries), client.deferred)
        effects = SonataEffectsProvider(self.root)
        effect_update = SonataEffectsUpdateService(
            effects, sonata, client=CatalogEffectsClient(self.catalog)
        )
        effect_update.check_once()
        self.assertNotEqual('failed', effect_update.status.state)
        self.assertEqual((2, 5), effects.active.tiers('凝夜白霜'))

    def test_avatar_uses_catalog_identity(self):
        assets = GameAssets(self.root, transport=lambda *args: (200, placeholder_png(), {}))
        self.assertEqual(
            placeholder_png(), CatalogAvatarFetcher(self.catalog, assets)('1102', None)
        )

    def test_success_schedule_is_daily(self):
        service = GameCatalogUpdates(
            self.catalog, client=GameCatalogClient(transport=self.transport), clock=lambda: 100
        )
        self.assertEqual(130, service.status.next_check)
        service.check_once()
        self.assertEqual(86500, service.status.next_check)

    def test_metadata_has_no_weights_or_player_data(self):
        data, _ = GameCatalogClient(transport=self.transport).fetch(self.catalog)
        self.assertEqual({'source', 'release', 'characters', 'echoes', 'sonatas'}, set(data))

    def test_icons_use_bounded_parallelism_and_report_progress(self):
        active = peak = 0
        lock = threading.Lock()

        def fetch(*args):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(active, peak)
            try:
                time.sleep(0.02)
                return 200, placeholder_png(), {}
            finally:
                with lock:
                    active -= 1

        assets = GameAssets(self.root, transport=fetch)
        client = CatalogSonataClient(self.catalog, assets)
        messages = []
        client.progress = messages.append
        names, icons, _, _ = client.fetch(SonataProvider(self.root))
        self.assertGreater(peak, 1)
        self.assertLessEqual(peak, 4)
        self.assertEqual(set(names), set(icons))
        self.assertEqual(len(names), assets.requests)
        self.assertEqual(len(names) + 1, len(messages))
        self.assertTrue(messages[-1].endswith(f'{len(names)} / {len(names)}'))
