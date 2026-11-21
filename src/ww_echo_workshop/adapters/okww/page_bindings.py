# 将 OKWW 配置与生命周期绑定到工坊外壳和各模式的显式服务。
from functools import partial


def _workshop_host_theme():
    try:
        from ok.ui.qt.kit import theme
    except ModuleNotFoundError as exc:
        if exc.name != 'ok.ui.qt.kit':
            raise
        return None
    return theme


def assistant_services(task):
    from ww_echo_workshop.ui.shell import AssistantServices

    runtime = task.runtime

    def set_profile(profile_id):
        task.config['InventoryProfileID'] = profile_id

    return AssistantServices(
        runtime=runtime,
        closed=lambda: task._destroyed or runtime._closed or task.runtime is not runtime,
        set_profile=set_profile,
        build_settings=partial(build_settings_services, task),
        host_theme=_workshop_host_theme(),
        pages={
            'inventory': partial(build_inventory_page, task),
            'comparison': partial(build_matching_page, task),
            'allocation': partial(build_allocation_page, task),
            'workspace': partial(build_workspace_page, task),
        },
    )


def build_inventory_page(task, panel):
    from ww_echo_workshop.ui.inventory.page import (
        InventoryNavigation,
        InventoryPage,
        InventoryPageServices,
    )

    navigation = InventoryNavigation(
        profile_switch_error=panel.profile_switch_error,
        profile_changed=panel._inventory_profile_changed,
        has_edit_draft=panel.has_edit_draft,
        refresh_profile_guard=panel.refresh_profile_guard,
        refresh_status=panel.refresh_status,
        show_feedback=panel._show_inventory_feedback,
        candidate_session=lambda: panel._match_inventory_session,
        apply_candidates=lambda filtered: panel.finish_inventory_selection(filtered=filtered),
        cancel_candidates=panel.cancel_inventory_selection,
        open_inventory=panel._open_inventory_route,
        open_shortcuts=lambda: panel.open_settings_section('shortcuts'),
    )
    runtime = panel.runtime
    services = InventoryPageServices(
        inventory=runtime.inventory,
        queries=runtime.inventory_queries,
        new_reader=runtime.new_inventory_reader,
        intake=task.inventory_intake(),
        backups=runtime.backups,
        evidence=runtime.evidence.views,
        review_choices=runtime.inventory_review_choices,
        quality_checks=runtime.quality_checks,
        quick_score=runtime.quick_score,
        sonata_snapshot=lambda: runtime.sonata.active,
        avatars=runtime.avatars,
        closed=panel.closed,
        profile_id=lambda: task.config.get('InventoryProfileID', ''),
        set_profile=panel._set_inventory_profile,
        comparison_preferences=lambda: task.config.get('ComparisonSettingsJSON', ''),
    )
    return InventoryPage(services, panel.stack, navigation=navigation)


def build_matching_page(task, panel):
    from ww_echo_workshop.application.matching.preferences import MatchingPreferences
    from ww_echo_workshop.application.search_budget import allocation_budget, set_allocation_budget
    from ww_echo_workshop.ui.matching.page import (
        ComparisonPage,
        MatchingNavigation,
        MatchingPageServices,
    )
    from ww_echo_workshop.ui.widgets.template_weights import open_template_weights

    runtime = panel.runtime

    def save_preference(raw):
        task.config['ComparisonSettingsJSON'] = raw

    def profile_for_budget(profile_id):
        selector = getattr(panel._owned_pages.get('allocation'), 'profile', None)
        return (selector.currentData() if selector is not None else None) or profile_id

    def pending_count(profile_id):
        inventory = panel.inventory_page
        status = inventory._reader.status if inventory is not None else None
        if status is None or status.state != 'ready' or status.result is None:
            return None
        if status.result.profile_id != profile_id:
            return None
        return status.result.pending_count

    services = MatchingPageServices(
        matching=runtime.matching,
        preferences=MatchingPreferences(
            lambda: task.config.get('ComparisonSettingsJSON', ''), save_preference
        ),
        pin_checks=runtime.pin_checks,
        avatars=runtime.avatars,
        inventory_profiles=runtime.inventory.profiles,
        try_revision=runtime.inventory.try_revision,
        weight_snapshot=lambda: runtime.weights.active,
        defaults_snapshot=lambda: runtime.role_defaults.active,
        sonata_snapshot=lambda: runtime.sonata.active,
        closed=panel.closed,
        profile_id=lambda: task.config.get('InventoryProfileID', ''),
        fallback_role=lambda: (
            task.config.get('TemplateCharacterID'),
            task.config.get('TemplateModalID'),
        ),
        read_budget=lambda profile: allocation_budget(task.config, profile_for_budget(profile)),
        write_budget=lambda profile, budget: set_allocation_budget(
            task.config, profile_for_budget(profile), budget
        ),
    )
    navigation = MatchingNavigation(
        choose_inventory=panel.begin_inventory_selection,
        open_pending_review=panel.show_intake_review,
        pending_count=pending_count,
        edit_weights=lambda key: open_template_weights(panel, key),
        refresh_profile_guard=panel.refresh_profile_guard,
        budget_saved=lambda: panel.feedback.setText('配装搜索预算已保存，下一次生成生效。'),
    )
    return ComparisonPage(services, panel.stack, navigation=navigation)


def build_allocation_page(task, panel):
    from ww_echo_workshop.application.allocation_preferences import AllocationPreferences
    from ww_echo_workshop.application.search_budget import allocation_budget
    from ww_echo_workshop.ui.allocation.page import (
        AllocationNavigation,
        AllocationPage,
        AllocationPageServices,
    )
    from ww_echo_workshop.ui.widgets.template_weights import open_template_weights

    runtime = panel.runtime

    def save_preference(raw):
        task.config['AllocationSettingsJSON'] = raw

    def inventory_remedy(kind, profile_id):
        inventory = panel._open_profile_route('inventory', profile_id)
        if kind == 'review':
            inventory.show_pending_review()
        else:
            inventory.compact_status.setText('打开游戏中的声骸详情，再开始收录。')

    services = AllocationPageServices(
        jobs=runtime.allocation_jobs,
        preferences=AllocationPreferences(
            lambda: task.config.get('AllocationSettingsJSON', ''), save_preference
        ),
        queries=runtime.inventory_queries,
        avatars=runtime.avatars,
        panel=runtime.panel,
        evidence=runtime.evidence.views,
        closed=panel.closed,
        backup_busy=lambda: runtime.backups.status.busy,
        profile_id=lambda: task.config.get('InventoryProfileID'),
        profiles=runtime.inventory.profiles,
        try_revision=runtime.inventory.try_revision,
        revision=lambda: runtime.inventory.revision,
        weight_snapshot=lambda: runtime.weights.active,
        sonata_snapshot=lambda: runtime.sonata.active,
        effects_snapshot=lambda: runtime.sonata_effects.active,
        budget=lambda profile_id: allocation_budget(task.config, profile_id),
    )
    navigation = AllocationNavigation(
        choose_inventory=panel.begin_inventory_selection,
        inventory_remedy=inventory_remedy,
        edit_weights=lambda key: open_template_weights(panel, key),
    )
    page = AllocationPage(services, panel.stack, navigation=navigation)
    page.apply_surface_palette(panel.palette())
    return page


def build_workspace_page(task, panel):
    from ww_echo_workshop.application.live_preferences import LivePreferences
    from ww_echo_workshop.application.policy_resource_jobs import PolicyResourceJobs
    from ww_echo_workshop.application.policy_ui_state import PolicyUiStateStore
    from ww_echo_workshop.application.preset_catalog import PresetCatalog
    from ww_echo_workshop.ui.workspace.page import WorkspacePage, WorkspacePageServices

    runtime = panel.runtime
    jobs = PolicyResourceJobs(runtime.data_dir)
    try:
        services = WorkspacePageServices(
            preferences=LivePreferences(task.config),
            policy_resources=jobs,
            live_presets=PresetCatalog(runtime.data_dir),
            test_presets=PresetCatalog(runtime.data_dir),
            test_state=PolicyUiStateStore(runtime.data_dir),
            hud_state=runtime.hud_state,
            weight_snapshot=lambda: runtime.weights.active,
            live_snapshot=runtime.live_snapshots.latest,
            solve_policy=runtime.policy.solve,
            closed=panel.closed,
        )
        return WorkspacePage(
            services,
            panel.stack,
            profiles=runtime.inventory.profiles(),
            profile_id=task.config.get('InventoryProfileID'),
            weight_settings=panel.ensure_weight_settings,
            open_weights=lambda: panel.open_settings_section('weights'),
        )
    except BaseException:
        jobs.close()
        raise


def build_settings_services(task, panel):
    from ww_echo_workshop.application.profiles import ProfileSettingsService
    from ww_echo_workshop.ui.settings.controller import SettingsServices

    runtime = panel.runtime

    def request_clear_screenshots():
        if panel.closed():
            return False
        runtime.stop_intake()
        return runtime.screenshot_cleanup.request_all()

    return SettingsServices(
        profiles=ProfileSettingsService(task.config, runtime.inventory, runtime.data_dir),
        backups=runtime.backups,
        resources=task.resource_settings(),
        shortcuts=task.shortcut_controls(),
        new_repair=task.new_repair_controls,
        request_clear_screenshots=request_clear_screenshots,
        screenshot_cleanup_status=lambda: runtime.screenshot_cleanup.status,
        deleted_profile_archives=runtime.base_data_dir / 'deleted-profiles',
        stop_event=runtime.stop_event,
    )
