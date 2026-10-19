# 将统一游戏目录投影为既有套装与效果快照，不修改评分和玩家配置。
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from echo_workshop.core.cancellation import checkpoint
from echo_workshop.resources.game_assets import placeholder_png


class CatalogSonataClient:
    def __init__(self, catalog, assets, *, clock=time.monotonic, budget=60):
        self.catalog, self.assets = catalog, assets
        self.clock, self.budget = clock, budget
        self.requests = self.cache_hits = self.deferred = 0
        self.progress = None

    def fetch(self, provider, *, cancel_event=None):
        from echo_workshop.resources.avatar_download import AvatarDeferred
        from echo_workshop.resources.sonata import checked_png, _digest

        active = self.catalog.active
        names = {ident: row['name'] for ident, row in active.entries.items()}
        before, hits = self.assets.requests, self.assets.cache_hits
        deadline, icons = self.clock() + self.budget, {}
        self.deferred = 0

        def load(ident, row):
            checkpoint(cancel_event)
            try:
                return (
                    ident,
                    self.assets.load(row['icons'], cancel_event=cancel_event, deadline=deadline),
                    False,
                )
            except AvatarDeferred:
                old = provider.active.icons.get(ident)
                body = placeholder_png()
                if old is not None and old.name == row['name']:
                    candidate = checked_png(old.path.read_bytes())
                    if _digest(candidate) == old.sha256:
                        body = candidate
                return ident, body, True

        # Four bounded image requests overlap DNS/TLS waits; full validation and
        # atomic publication remain unchanged. Failed images retain their old bytes.
        if self.progress:
            self.progress(f'正在检查套装图标：0 / {len(names)}')
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix='echo-set-icons') as pool:
            futures = [pool.submit(load, ident, row) for ident, row in active.entries.items()]
            try:
                for future in as_completed(futures):
                    ident, body, deferred = future.result()
                    icons[ident] = body
                    self.deferred += deferred
                    if self.progress:
                        self.progress(f'正在检查套装图标：{len(icons)} / {len(names)}')
                    checkpoint(cancel_event)
            finally:
                for future in futures:
                    future.cancel()
        self.requests, self.cache_hits = (
            self.assets.requests - before,
            self.assets.cache_hits - hits,
        )
        return names, icons, active.source, {}


class CatalogEffectsClient:
    def __init__(self, catalog):
        self.catalog = catalog
        self.requests = self.cache_hits = 0

    def fetch(self, *args, cancel_event=None, **kwargs):
        checkpoint(cancel_event)
        active = self.catalog.active
        items = {
            ident: dict(name=row['name'], tiers=dict(row['tiers']), data_version=active.release)
            for ident, row in active.entries.items()
        }
        return items, active.source, {}
