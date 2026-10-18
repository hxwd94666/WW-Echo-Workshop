# 组合插件运行时并管理服务生命周期。
"""Explicit host-config-relative runtime ownership, without module import I/O."""

from pathlib import Path
import threading

from echo_workshop.storage.evidence import EvidenceStore
from echo_workshop.storage.inventory import EchoRepository
from echo_workshop.application.scoring import ScoreService
from echo_workshop.application.compute_jobs import ComputeJobs
from echo_workshop.application.observations import ObservationService
from echo_workshop.application.custom_weights import CustomWeightService
from echo_workshop.application.inventory_query import InventoryQueryService
from echo_workshop.application.match_projection import MatchProjectionService
from echo_workshop.application.pins import PinCheckService
from echo_workshop.application.requirements_check import RequirementsCheckService
from echo_workshop.resources.weights import WeightProvider
from echo_workshop.resources.game_catalog import GameCatalog, GameCatalogUpdates
from echo_workshop.resources.game_assets import GameAssets, CatalogAvatarFetcher
from echo_workshop.resources.game_projection import CatalogSonataClient, CatalogEffectsClient
from echo_workshop.resources.http import MirrorWeightHttpClient
from echo_workshop.capture.intake import CaptureSession
from echo_workshop.resources.weight_updates import WeightUpdateService
from echo_workshop.application.allocation import AllocationService
from echo_workshop.storage.plans import AllocationStore
from echo_workshop.application.backup_jobs import BackupService
from echo_workshop.storage.workspaces import WorkspaceManager
from echo_workshop.resources.sonata import SonataProvider, SonataUpdateService
from echo_workshop.resources.effects import SonataEffectsProvider, SonataEffectsUpdateService
from echo_workshop.capture.scan import ScanTraversal
from echo_workshop.resources.avatars import AvatarCache
from echo_workshop.resources.updates import ResourceUpdateService
from echo_workshop.resources.role_defaults import RoleDefaultsProvider
from echo_workshop.resources.role_default_updates import RoleDefaultsUpdateService
from echo_workshop.resources.paths import package_manifest, plugin_root
from echo_workshop.application.inventory import InventoryService
from echo_workshop.application.plan_jobs import PlanCommands


def data_dir_for_config(config_file):
    if not isinstance(config_file, (str, Path)) or not str(config_file):
        raise ValueError('Host config_file is required')
    root = Path(config_file).resolve().parent / 'plugin_data' / 'echo-workshop'
    installation = plugin_root()
    if root == installation or installation in root.parents:
        raise ValueError('User data must not be inside the plugin installation')
    return root


class PluginRuntime:
    def __init__(self, config_file, *, compute=None):
        self.compute = compute if compute is not None else ComputeJobs()
        self.observations = ObservationService(compute=self.compute)
        self.custom_weights = CustomWeightService(compute=self.compute)
        self.inventory_queries = InventoryQueryService(compute=self.compute)
        self.match_projections = MatchProjectionService(compute=self.compute)
        self.pin_checks = PinCheckService(compute=self.compute)
        self.requirements_checks = RequirementsCheckService(compute=self.compute)
        self.allocation = AllocationService(None, compute=self.compute)
        try:
            self._initialize(config_file)
        except BaseException:
            self.compute.close()
            repository = getattr(self, 'repository', None)
            if repository is not None:
                repository.close()
            raise

    def _initialize(self, config_file):
        self.base_data_dir = data_dir_for_config(config_file)
        self.workspaces = WorkspaceManager(self.base_data_dir, plan_evaluator=self.allocation)
        self.workspace_id, self.data_dir, self.workspace_profile = self.workspaces.startup()
        self.stop_event = threading.Event()
        self.shortcuts = None
        self.scan_pipeline = None
        self.repository = EchoRepository(self.data_dir, plan_evaluator=self.allocation)
        self.inventory = InventoryService(self.repository, self.stop_event)
        from echo_workshop.application.diagnostics import begin_job

        version = ''
        try:
            version = package_manifest()['version']
        except (OSError, ValueError, TypeError):
            pass
        init_job = begin_job('plugin-runtime', metadata={'plugin_version': version})
        init_job.stage('offline-providers')
        try:
            self.evidence = EvidenceStore(self.data_dir)
            self.game_catalog = GameCatalog(self.data_dir)
            self.game_updates = GameCatalogUpdates(self.game_catalog, stop_event=self.stop_event)
            self.game_assets = GameAssets(self.data_dir)
            self.weights = WeightProvider(self.data_dir, custom_evaluator=self.custom_weights)
            self.avatars = AvatarCache(
                self.data_dir,
                lambda: {k: v['name'] for k, v in self.game_catalog.active.characters.items()},
                fetch=CatalogAvatarFetcher(self.game_catalog, self.game_assets),
            )
            self.sonata = SonataProvider(self.data_dir)
            self.sonata_updates = SonataUpdateService(
                self.sonata,
                stop_event=self.stop_event,
                client=CatalogSonataClient(self.game_catalog, self.game_assets),
            )
            self.sonata_effects = SonataEffectsProvider(self.data_dir)
            self.sonata_effect_updates = SonataEffectsUpdateService(
                self.sonata_effects,
                self.sonata,
                stop_event=self.stop_event,
                client=CatalogEffectsClient(self.game_catalog),
            )
            self.role_defaults = RoleDefaultsProvider(self.data_dir)
            self.role_default_updates = RoleDefaultsUpdateService(
                self.role_defaults, stop_event=self.stop_event
            )
            self.scores = ScoreService(self.weights, compute=self.compute)
            self.allocation.scores = self.scores
            self.plans = AllocationStore(self.data_dir, plan_evaluator=self.allocation)
            self.plan_commands = PlanCommands(
                self.plans,
                self.repository,
                allocation=self.allocation,
                queries=self.inventory_queries,
            )
            self.capture = CaptureSession(self.repository, self.evidence)
            self.scan = ScanTraversal()
            self.updates = WeightUpdateService(
                self.weights,
                self.data_dir,
                stop_event=self.stop_event,
                client=MirrorWeightHttpClient(self.data_dir, catalog=self.game_catalog),
            )
            self.resources = ResourceUpdateService(
                self.updates,
                self.sonata_updates,
                self.sonata_effect_updates,
                self.avatars,
                self.data_dir,
                stop_event=self.stop_event,
                defaults=self.role_default_updates,
                catalog=self.game_updates,
            )
            self.backups = BackupService(
                self.repository,
                self.stop_event,
                before_restore=self.stop_intake,
                plans=self.plans,
                workspaces=self.workspaces,
                requirements_checks=self.requirements_checks,
            )
            for channel, provider in (
                ('catalog', self.game_catalog),
                ('weights', self.weights),
                ('sonata', self.sonata),
                ('effects', self.sonata_effects),
                ('role-defaults', self.role_defaults),
            ):
                init_job.part(channel, 'cached', metadata={'version': provider.active.version[:12]})
            init_job.finish(
                'completed',
                counters={
                    'offline_providers': 5,
                    'warnings': int(bool(self.weights.last_error))
                    + int(bool(self.sonata.warning))
                    + int(bool(self.sonata_effects.warning))
                    + int(bool(self.role_defaults.warning)),
                },
            )
        except Exception as error:
            init_job.finish('failed', error=error)
            raise
        self._closed = False
        from echo_workshop.application.screenshot_cleanup import ScreenshotCleanupService

        self.screenshot_cleanup = ScreenshotCleanupService(
            self.repository, self.evidence, self.stop_event
        )
        self.inventory.after_mutation = self.screenshot_cleanup.notify
        self.screenshot_cleanup.start()

    def close(self):
        if not self._closed:
            self._closed = True
            self.stop_event.set()
            self.compute.close()
            self.stop_intake()
            self.backups.close()
            if self.shortcuts is not None:
                self.shortcuts.stop()
            self.avatars.close()
            self.resources.stop()
            self.updates.stop()
            self.sonata_updates.stop()
            self.sonata_effect_updates.stop()
            self.evidence.views.close()
            self.screenshot_cleanup.close()
            self.repository.close()

    def stop_intake(self):
        self.scan.stop()
        if self.scan_pipeline is not None:
            self.scan_pipeline.cancel()
        self.capture.stop()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
