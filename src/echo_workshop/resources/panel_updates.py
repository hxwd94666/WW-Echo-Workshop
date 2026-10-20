# 分批获取角色/武器 90 级来源并构建紧凑面板快照。
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import json
import threading
import time

from echo_workshop.core.cancellation import OperationCancelled, checkpoint
from echo_workshop.resources import panel_sources as sources
from echo_workshop.resources.game_sources import MAX_JSON
from echo_workshop.resources.game_transport import get as conditional_get
from echo_workshop.resources.panel_catalog import PanelSnapshot

MAX_DETAILS = 32


class PanelFetchError(ValueError):
    pass


class PanelUpdateClient:
    def __init__(self, *, transport=None, clock=time.monotonic, budget=90):
        self.transport = transport
        self.clock = clock
        self.budget = budget
        self.requests = 0
        self._requests_lock = threading.Lock()

    def _get(self, url, *, end, cancel_event):
        checkpoint(cancel_event)
        remaining = min(20.0, end - self.clock())
        if remaining <= 0:
            raise TimeoutError('角色面板来源请求超时')
        sources.approve_panel_url(url)
        with self._requests_lock:
            self.requests += 1
        if self.transport:
            status, body, _ = self.transport(url, {}, MAX_JSON, remaining)
        else:
            status, body, _ = conditional_get(
                url, {}, MAX_JSON, remaining, check=lambda: checkpoint(cancel_event)
            )
        if status != 200:
            raise PanelFetchError(f'角色面板来源 HTTP {status}')
        try:
            return json.loads(body)
        except (TypeError, ValueError) as error:
            raise PanelFetchError('角色面板来源 JSON 无效') from error

    def release(self, *, cancel_event=None):
        end = self.clock() + self.budget
        manifest = self._get(sources.NANOKA + '/manifest.json', end=end, cancel_event=cancel_event)
        return sources.nanoka_manifest_release(manifest)

    def fetch(self, *, cancel_event=None, max_details=None):
        end = self.clock() + self.budget
        limit = MAX_DETAILS if max_details is None else max_details
        if type(limit) is not int or limit < 0:
            raise ValueError('max_details 无效')
        manifest = self._get(sources.NANOKA + '/manifest.json', end=end, cancel_event=cancel_event)
        release = sources.nanoka_manifest_release(manifest)
        character_list_url, weapon_list_url = sources.nanoka_list_urls(release)
        character_list = self._get(character_list_url, end=end, cancel_event=cancel_event)
        weapon_list = self._get(weapon_list_url, end=end, cancel_event=cancel_event)
        if not isinstance(character_list, dict) or not isinstance(weapon_list, dict):
            raise PanelFetchError('Nanoka 角色/武器列表无效')
        characters, weapons = {}, {}
        used = {'nanoka': release}
        used_lock = threading.Lock()
        weapon_ids = [ident for ident in weapon_list if isinstance(ident, str) and ident.isdigit()]
        weapon_ids = weapon_ids[:limit] if limit else weapon_ids
        character_ids = [
            ident for ident in character_list if isinstance(ident, str) and ident.isdigit()
        ]
        character_ids = character_ids[:limit] if limit else character_ids

        def mark_used(source, payload):
            with used_lock:
                used[source] = str(payload.get('name', '')) or used.get(source, '')

        if weapon_ids:
            with ThreadPoolExecutor(max_workers=min(8, len(weapon_ids))) as pool:
                futures = {
                    pool.submit(
                        self._weapon_detail,
                        ident,
                        release,
                        end,
                        cancel_event,
                        mark_used,
                    ): ident
                    for ident in weapon_ids
                }
                for future in as_completed(futures):
                    ident, row = future.result()
                    if row is not None:
                        weapons[ident] = row
        if character_ids:
            with ThreadPoolExecutor(max_workers=min(8, len(character_ids))) as pool:
                futures = {
                    pool.submit(
                        self._character_detail,
                        ident,
                        release,
                        end,
                        cancel_event,
                        weapons,
                        mark_used,
                    ): ident
                    for ident in character_ids
                }
                for future in as_completed(futures):
                    ident, row = future.result()
                    characters[ident] = row
        return PanelSnapshot.from_data(
            {
                'schema_version': 1,
                'game_version': release,
                'retrieved_at': time.time(),
                'characters': {key: characters[key] for key in sorted(characters)},
                'weapons': {key: weapons[key] for key in sorted(weapons)},
                'provenance': used,
            }
        )

    def _weapon_detail(self, weapon_id, release, end, cancel_event, mark_used):
        row = None
        errors = []
        for source in ('nanoka', 'xwuid'):
            hosts = (None,) if source == 'nanoka' else sources.XWUID_HOSTS
            for host in hosts:
                try:
                    if host is None:
                        payload = self._get(
                            sources.nanoka_weapon_url(release, weapon_id),
                            end=end,
                            cancel_event=cancel_event,
                        )
                        row = sources.parse_nanoka_weapon(payload)
                    else:
                        payload = self._get(
                            sources.xwuid_weapon_url(host, weapon_id),
                            end=end,
                            cancel_event=cancel_event,
                        )
                        row = sources.parse_xwuid_weapon(payload)
                    mark_used(source, payload)
                    break
                except Exception as error:
                    errors.append(f'{source}: {error}')
            if row is not None:
                break
        return weapon_id, row

    def _character_detail(self, character_id, release, end, cancel_event, weapons, mark_used):
        row = None
        errors = []
        for source in ('nanoka', 'xwuid', 'encore'):
            hosts = (None,) if source != 'xwuid' else sources.XWUID_HOSTS
            for host in hosts:
                try:
                    if source == 'nanoka':
                        payload = self._get(
                            sources.nanoka_character_url(release, character_id),
                            end=end,
                            cancel_event=cancel_event,
                        )
                        row = sources.parse_nanoka_character(payload)
                    elif host is not None:
                        payload = self._get(
                            sources.xwuid_character_url(host, character_id),
                            end=end,
                            cancel_event=cancel_event,
                        )
                        row = sources.parse_xwuid_character(payload)
                    else:
                        payload = self._get(
                            sources.encore_character_url(character_id),
                            end=end,
                            cancel_event=cancel_event,
                        )
                        row = sources.parse_encore_character(payload)
                    mark_used(source, payload)
                    break
                except Exception as error:
                    errors.append(f'{source}: {error}')
            if row is not None:
                break
        if row is None:
            raise PanelFetchError(f'角色 {character_id} 无可用来源：' + '; '.join(errors))
        if not row.get('default_weapon_id'):
            try:
                defaults = self._xwuid_defaults(end, cancel_event, {})
                row['default_weapon_id'] = defaults.get(character_id, '')
            except Exception:
                pass
        candidates = row.get('recommended_weapon_ids', [])
        if row.get('default_weapon_id') not in weapons:
            row['default_weapon_id'] = next((ident for ident in candidates if ident in weapons), '')
        return character_id, row

    def _xwuid_defaults(self, end, cancel_event, cached):
        if cached:
            return cached
        error = None
        for host in sources.XWUID_HOSTS:
            try:
                payload = self._get(
                    sources.xwuid_default_url(host), end=end, cancel_event=cancel_event
                )
                return sources.default_weapon_from_xwuid(payload)
            except Exception as exc:
                error = exc
        raise error or PanelFetchError('XWUID 默认武器来源不可用')


@dataclass(frozen=True)
class PanelUpdateStatus:
    state: str = 'idle'
    message: str = '角色与武器资料离线可用'
    next_check: float = 0
    requests: int = 0
    cache_hits: int = 0


class PanelCatalogUpdates:
    def __init__(self, provider, *, stop_event=None, client=None, clock=time.time):
        self.provider = provider
        self.client = client or PanelUpdateClient()
        self.stop_event = stop_event
        self.clock = clock
        self.coordinator = None
        self.status = PanelUpdateStatus(next_check=clock() + 30)

    def check_once(self, *, cancel_event=None):
        token = cancel_event or self.stop_event
        try:
            release = self.client.release(cancel_event=token)
            if release == self.provider.active.game_version:
                self.status = PanelUpdateStatus(
                    'current',
                    '角色与武器资料已是最新',
                    self.clock() + 86400,
                    self.client.requests,
                    0,
                )
                return False
            snapshot = self.client.fetch(max_details=0, cancel_event=token)
            changed = self.provider.activate(
                snapshot.to_data(), retrieved_at=snapshot.retrieved_at, cancel_event=token
            )
            self.status = PanelUpdateStatus(
                'updated' if changed else 'current',
                '角色与武器资料已检查',
                self.clock() + 86400,
                self.client.requests,
                0,
            )
            return changed
        except OperationCancelled:
            raise
        except Exception:
            self.status = PanelUpdateStatus(
                'failed',
                '角色与武器资料检查未完成，已有资料保留',
                self.clock() + 1800,
                self.client.requests,
                0,
            )
            return False
