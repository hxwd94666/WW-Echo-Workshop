# 验证响应截断等协议故障也走主备回退，并在全部失败时保留快照。
from dataclasses import replace
import http.client
import tempfile
import unittest
from unittest.mock import Mock

from echo_workshop.resources.game_catalog import GameCatalog, GameCatalogClient
from echo_workshop.resources.game_assets import GameAssets, placeholder_png
from echo_workshop.resources.game_sources import NANOKA, ENCORE
from echo_workshop.resources.avatar_download import AvatarDeferred
from echo_workshop.resources.role_defaults import RoleDefaultsProvider, SOURCES
from echo_workshop.resources.role_default_updates import RoleDefaultsHttpClient
from echo_workshop.resources.http import MirrorWeightHttpClient, FetchResult, MirrorFetchError
from echo_workshop.resources.weights import WeightSnapshot
from tests.contracts.test_game_resources import fixtures
import json


def upstream(entries):
    return json.dumps(
        [
            dict(
                role={'roleId': int(ident)},
                phantomData={
                    'equipPhantomList': [
                        {'fetterDetail': {'name': name}}
                        for name, count in sets
                        for _ in range(count)
                    ]
                },
            )
            for ident, sets in entries.items()
        ],
        ensure_ascii=False,
    ).encode('utf-8')


class ProtocolFallbackContract(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def test_catalog_truncated_primary_uses_encore(self):
        docs = fixtures()

        def fetch(url, headers, *args):
            if url.startswith(NANOKA):
                raise http.client.IncompleteRead(b'partial')
            self.assertEqual({}, headers)
            return 200, json.dumps(docs[url]).encode(), {}

        data, _ = GameCatalogClient(transport=fetch).fetch(GameCatalog(self.temp.name))
        self.assertEqual(ENCORE, data['source'])

    def test_images_bad_status_primary_uses_backup(self):
        catalog = GameCatalog(self.temp.name)
        urls = next(iter(catalog.active.entries.values()))['icons']

        def fetch(url, *args):
            if url.startswith(NANOKA):
                raise http.client.BadStatusLine('broken')
            return 200, placeholder_png(), {}

        assets = GameAssets(self.temp.name, transport=fetch)
        self.assertEqual(placeholder_png(), assets.load(urls))
        assets.transport = Mock(side_effect=http.client.IncompleteRead(b'partial'))
        with self.assertRaises(AvatarDeferred):
            assets.load(urls)
        self.assertEqual(placeholder_png(), assets._cache(urls[1])[1])

    def test_default_sets_truncated_primary_uses_next_mirror(self):
        provider = RoleDefaultsProvider(self.temp.name)
        body = upstream(provider.active.entries)

        def fetch(url, headers, *args):
            if url == SOURCES[0]:
                raise http.client.IncompleteRead(b'partial')
            self.assertEqual({}, headers)
            return 200, body, {}

        value, _ = RoleDefaultsHttpClient(transport=fetch).fetch(provider)
        self.assertEqual(SOURCES[1], value.source)
        self.assertEqual(provider.active.entries, value.entries)

    def test_weight_protocol_failure_uses_next_complete_source(self):
        client = MirrorWeightHttpClient(self.temp.name)
        active = WeightSnapshot.builtin()
        result = FetchResult(replace(active, source=client.clients[1].base), {}, (), 1, 1)
        client.clients[0].fetch = Mock(side_effect=http.client.BadStatusLine('broken'))
        client.clients[1].fetch = Mock(return_value=result)
        client.clients[2].fetch = Mock(side_effect=AssertionError('extra fallback'))
        fetched = client.fetch(active)
        self.assertEqual(client.clients[1].base, fetched.snapshot.source)
        self.assertEqual(['failed', 'validated'], [row.state for row in fetched.attempts])
        for source in client.clients:
            source.fetch = Mock(side_effect=http.client.IncompleteRead(b'partial'))
        with self.assertRaises(MirrorFetchError):
            client.fetch(active)
        self.assertEqual(3, len(client.attempts))
        self.assertEqual(active, WeightSnapshot.builtin())
