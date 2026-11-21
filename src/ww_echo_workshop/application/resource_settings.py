# 装配资料检查与自定义权重用例，统一使用运行时已有的资料作业。
from dataclasses import dataclass

from ww_echo_workshop.resources.effects import SonataEffectsSnapshot, SonataEffectsStatus
from ww_echo_workshop.resources.sonata import SonataSnapshot, SonataUpdateStatus
from ww_echo_workshop.resources.updates import ResourceUpdateStatus
from ww_echo_workshop.resources.weight_updates import UpdateStatus
from ww_echo_workshop.resources.weights import WeightSnapshot


@dataclass(frozen=True)
class ResourceFacts:
    weights: WeightSnapshot
    weight_status: UpdateStatus
    sonata: SonataSnapshot
    sonata_status: SonataUpdateStatus
    effects: SonataEffectsSnapshot
    effects_status: SonataEffectsStatus
    resources: ResourceUpdateStatus
    custom_count: int
    network_budget: int


class ResourceSettings:
    def __init__(
        self,
        coordinator,
        updates,
        custom,
        sonata_updates,
        effects_updates,
        *,
        read_auto,
        write_auto,
        selected_character,
        closed,
    ):
        self._coordinator = coordinator
        self._updates, self.custom = updates, custom
        self._sonata_updates, self._effects_updates = sonata_updates, effects_updates
        self.auto = read_auto
        self._write_auto = write_auto
        self.selected_character, self.closed = selected_character, closed

    @property
    def weight_snapshot(self):
        return self._updates.provider.active

    @property
    def weight_status(self):
        return self._updates.status

    @property
    def customized_characters(self):
        return self._updates.provider.customized_characters

    def facts(self):
        return ResourceFacts(
            self.weight_snapshot,
            self.weight_status,
            self._sonata_updates.provider.active,
            self._sonata_updates.status,
            self._effects_updates.provider.active,
            self._effects_updates.status,
            self._coordinator.status,
            len(self.customized_characters),
            int(getattr(self._updates.client, 'deadline', 300)),
        )

    def set_auto(self, enabled):
        if not self.closed():
            self._write_auto(bool(enabled))
            self._coordinator.set_auto(enabled)

    def check(self, *, allow_removals):
        return not self.closed() and self._coordinator.request(allow_removals=allow_removals)

    def cancel(self):
        if not self.closed():
            self._coordinator.cancel()

    def reload_local(self):
        return not self.closed() and self._updates.request_reload()

    def request_custom(self, char_id, variant=None, **options):
        if self.closed():
            return False
        if variant is None:  # Restore the role's default snapshots.
            return self._updates.request_custom(char_id, **options)
        return self._updates.request_custom(char_id, variant, **options)
