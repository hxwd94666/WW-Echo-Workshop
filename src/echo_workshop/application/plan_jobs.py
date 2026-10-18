# 调度历史方案读取与提交作业。
"""Plain-data plan operations for a caller-owned single-flight background executor."""

from dataclasses import dataclass

from echo_workshop.core.allocation.models import AllocationRequest
from echo_workshop.core.models import EchoRecord, canonical_json
from echo_workshop.application.operations import OperationCancelled, checkpoint, report_progress


@dataclass(frozen=True)
class PlanJobResult:
    kind: str
    payload: object = None
    records: tuple = ()
    request: object = None
    rows: tuple = ()
    errors: tuple = ()
    committed: bool = False
    loadouts: object = None
    summaries: tuple = ()
    source_payload: object = None
    current_committed: bool | None = None
    notice: str = ''


class PlanCommands:
    """持有存储边界的配装准备与提交用例，页面只提交明确请求。"""

    def __init__(self, store, repository, *, allocation=None, queries=None):
        self._store, self._repository = store, repository
        self._allocation = allocation
        self._queries = queries

    def execute(
        self,
        kind,
        arguments,
        *,
        cancel_event=None,
        progress=None,
        before_commit=None,
        candidate_ids=None,
        candidate_filters=None,
        scope_generation=0,
        edit_plans=None,
        replace_plans=None,
    ):
        if kind not in ('precheck', 'edit-role'):
            return execute_plan_job(
                self._store,
                kind,
                arguments,
                cancel_event=cancel_event,
                progress=progress,
                before_commit=before_commit,
                repository=self._repository,
            )
        from echo_workshop.application.diagnostics import begin_job
        from echo_workshop.application.matching.scope import select_match_records
        from echo_workshop.application.pins import prepare_pin_catalog

        job = begin_job('plan-preparation')
        job.stage(kind)
        try:
            if kind == 'precheck':
                request, snapshot = arguments['request'], arguments['snapshot']
                reservations = self._repository.loadout_snapshot(
                    request.profile_id, cancel_event=cancel_event
                )
                records = reservations.records
            else:
                revision, records = self._repository.inventory_snapshot(
                    arguments['profile_id'], cancel_event=cancel_event
                )
            if candidate_filters is not None:
                if self._queries is None:
                    raise ValueError('候选筛选需要显式的 Rust 库存查询服务')
                records = self._queries.filter_records(
                    records, cancel_event=cancel_event, **candidate_filters
                )
            records = select_match_records(records, candidate_ids, cancel_event=cancel_event)
            if kind == 'precheck':
                if self._allocation is None:
                    raise ValueError('配装预检需要已装配的 Rust 计算服务')
                report = self._allocation.precheck(
                    records,
                    request,
                    snapshot=snapshot,
                    inventory_revision=reservations.inventory_revision,
                    cancel_event=cancel_event,
                    reservations=reservations,
                    edit_plans=edit_plans or {},
                    replace_plans=replace_plans or {},
                )
                report['scope_generation'] = scope_generation
                result = PlanJobResult(
                    kind,
                    payload=(
                        request.profile_id,
                        reservations.inventory_revision,
                        snapshot.version,
                        request.version,
                        report,
                    ),
                )
            else:
                catalog = prepare_pin_catalog(
                    records, queries=self._queries, cancel_event=cancel_event, progress=progress
                )
                checkpoint(cancel_event)
                result = PlanJobResult(
                    kind,
                    payload=dict(
                        arguments,
                        revision=revision,
                        catalog=catalog,
                        scope_generation=scope_generation,
                    ),
                )
        except OperationCancelled:
            job.finish('cancelled')
            raise
        except Exception as error:
            job.finish('failed', error=error)
            raise
        job.finish('completed', counters={'records': len(records)})
        return result


def execute_plan_job(
    store, kind, arguments, *, cancel_event=None, progress=None, before_commit=None, repository=None
):
    from echo_workshop.application.diagnostics import begin_job

    job = begin_job('plan-storage')
    job.stage(kind)
    try:
        result = _execute_plan_job(
            store,
            kind,
            arguments,
            cancel_event=cancel_event,
            progress=progress,
            before_commit=before_commit,
            repository=repository,
        )
    except OperationCancelled:
        job.finish('cancelled')
        raise
    except Exception as error:
        job.finish('failed', error=error)
        raise
    # No cancellation checkpoint after publication: keep committed semantics.
    job.finish(
        counters={
            'committed': int(result.committed),
            'rows': len(result.rows),
            'records': len(result.records),
            'errors': len(result.errors),
            'warnings': int(bool(result.notice)),
            'current_committed': int(result.current_committed is True),
        }
    )
    return result


def _execute_plan_job(
    store, kind, arguments, *, cancel_event=None, progress=None, before_commit=None, repository=None
):
    checkpoint(cancel_event)
    arguments = dict(arguments)
    if kind == 'verify-loadouts':
        if repository is None:
            raise ValueError('配装核验需要当前声骸库')
        snapshot = repository.loadout_snapshot(arguments['profile_id'], cancel_event=cancel_event)
        checkpoint(cancel_event)
        return PlanJobResult(kind, loadouts=snapshot)
    if kind == 'remove-backup':
        payload = store.remove_backup_role(
            arguments['profile_id'],
            arguments['plan_id'],
            arguments['character_id'],
            arguments['modal_id'],
            expected_hash=arguments['expected_hash'],
            cancel_event=cancel_event,
            before_commit=before_commit,
        )
        return PlanJobResult(kind, payload=payload, committed=True)
    if kind == 'release-items':
        if repository is None:
            raise ValueError('配装操作需要当前声骸库')
        common = dict(
            expected_revision=arguments['expected_revision'],
            expected_loadout_revision=arguments['expected_loadout_revision'],
            cancel_event=cancel_event,
            before_commit=before_commit,
        )
        repository.release_loadout_items(
            arguments['profile_id'],
            arguments['character_id'],
            arguments['plan_id'],
            arguments['item_ids'],
            **common,
        )
        loadouts = None
        try:
            loadouts = repository.loadout_snapshot(arguments['profile_id'])
        except Exception:
            import logging

            logging.getLogger(__name__).exception('Committed role operation readback failed')
        return PlanJobResult(kind, committed=True, loadouts=loadouts)
    if kind in ('backup-loadout', 'save-loadout'):
        from echo_workshop.application.loadout_actions import (
            CurrentLoadoutRejected,
            persist_loadout,
        )

        try:
            payload, source = persist_loadout(
                store,
                repository,
                kind,
                arguments,
                cancel_event=cancel_event,
                before_commit=before_commit,
            )
        except CurrentLoadoutRejected as rejected:
            import logging

            log = logging.getLogger(__name__)
            emit = log.info if isinstance(rejected.cause, OperationCancelled) else log.warning
            emit(
                'loadout current rejected history_published=true error_type=%s',
                type(rejected.cause).__name__,
            )
            return PlanJobResult(
                kind,
                payload=rejected.payload,
                source_payload=rejected.source,
                committed=True,
                current_committed=False,
                notice='历史备选已保留，当前配装未更新。' + str(rejected.cause)[:240],
            )
        loadouts = None
        if kind == 'save-loadout':
            try:
                # Publication has completed: a late cancel/readback failure
                # must not turn a committed save into a retryable failure.
                loadouts = repository.loadout_snapshot(arguments['profile_id'])
            except Exception:
                import logging

                logging.getLogger(__name__).exception('Committed loadout readback failed')
        return PlanJobResult(
            kind,
            payload=payload,
            source_payload=source,
            committed=True,
            loadouts=loadouts,
            current_committed=True if kind == 'save-loadout' else None,
        )
    if kind in ('replacement-candidates', 'preview-replacement'):
        from echo_workshop.application.replacement import (
            replacement_candidates,
            replacement_preview,
        )

        if repository is None:
            raise ValueError('替换预览需要当前库存')
        profile = (
            arguments['payload']['request']['profile_id']
            if kind == 'replacement-candidates'
            else arguments['options'].profile_id
        )
        reservations = repository.loadout_snapshot(profile, cancel_event=cancel_event)
        if kind == 'replacement-candidates':
            value = replacement_candidates(
                arguments['payload'],
                reservations,
                arguments['character_id'],
                arguments['slot_index'],
                cancel_event=cancel_event,
                plan_evaluator=store.plan_evaluator,
            )
        else:
            value = replacement_preview(
                arguments['options'],
                arguments['item_id'],
                reservations,
                cancel_event=cancel_event,
                plan_evaluator=store.plan_evaluator,
            )
        return PlanJobResult(kind, payload=value)
    if kind == 'list':
        summaries = {}
        rows, errors = store.list(
            arguments['profile_id'],
            cancel_event=cancel_event,
            progress=progress,
            summary_sink=summaries,
        )
        loadouts = (
            repository.loadout_snapshot(arguments['profile_id'], cancel_event=cancel_event)
            if repository is not None
            else None
        )
        return PlanJobResult(
            kind,
            rows=rows,
            errors=errors,
            loadouts=loadouts,
            summaries=tuple(summaries[plan_id] for plan_id, _ in rows),
        )
    if kind in ('save-protect', 'protect', 'release-current', 'remove-current'):
        if repository is None:
            raise ValueError('配装保护需要当前库存')
        if kind in ('release-current', 'remove-current'):
            report_progress(progress, '更新配装保护', 0, 1, cancel_event)
            repository.change_current_loadout(
                arguments['profile_id'],
                arguments['character_id'],
                'release' if kind == 'release-current' else 'remove',
                expected_revision=arguments['expected_revision'],
                expected_loadout_revision=arguments['expected_loadout_revision'],
                cancel_event=cancel_event,
                before_commit=before_commit,
            )
            return PlanJobResult(kind, committed=True)
        report_progress(progress, '校验并保护配装', 0, 1, cancel_event)
        if kind == 'save-protect':
            result = arguments['result']
            if any(len(row.item_ids) != 5 for row in result.roles):
                raise ValueError('请先生成完整五件套；缺件结果可单独保存历史')
            try:
                payload = store.load(result.plan_id, cancel_event=cancel_event)
            except FileNotFoundError:
                payload = store.save(
                    result,
                    arguments['records'],
                    arguments['snapshot'],
                    cancel_event=cancel_event,
                    before_commit=before_commit,
                )
            else:
                # Retry after a failed activation may reuse this exact saved preview.
                if canonical_json(payload['result']) != canonical_json(result.to_dict()):
                    raise ValueError('已保存方案与本次预览不一致，请重新生成')
        else:
            payload = store.load(arguments['plan_id'], cancel_event=cancel_event)
        if payload['request']['profile_id'] != arguments['profile_id']:
            raise ValueError('此方案属于其他档案')
        repository.commit_current_loadouts(
            payload,
            expected_revision=arguments['expected_revision'],
            expected_loadout_revision=arguments['expected_loadout_revision'],
            replace_plans=arguments.get('replace_plans'),
            characters=arguments.get('characters'),
            cancel_event=cancel_event,
            before_commit=before_commit,
        )
        return PlanJobResult(kind, payload=payload, committed=True)
    if kind not in ('load', 'save', 'mark'):
        raise ValueError('未知方案作业')
    report_progress(
        progress,
        {'load': '读取与校验方案', 'save': '校验并保存方案', 'mark': '校验并保存手动进度'}[kind],
        0,
        1,
        cancel_event,
    )
    if kind == 'save':
        payload = store.save(
            arguments['result'],
            arguments['records'],
            arguments['snapshot'],
            cancel_event=cancel_event,
            before_commit=before_commit,
        )
    elif kind == 'mark':
        payload = store.mark(**arguments, cancel_event=cancel_event, before_commit=before_commit)
    else:
        payload = store.load(arguments['plan_id'], cancel_event=cancel_event)
        checkpoint(cancel_event)
        request = AllocationRequest.from_dict(payload['request'])
        if request.profile_id != arguments['profile_id']:
            raise ValueError('此方案属于其他档案')
        records = tuple(EchoRecord.from_dict(row) for row in payload['records'])
        checkpoint(cancel_event)
        loadouts = (
            repository.loadout_snapshot(arguments['profile_id'], cancel_event=cancel_event)
            if repository is not None
            else None
        )
        return PlanJobResult(
            kind, payload=payload, records=records, request=request, loadouts=loadouts
        )
    # Publication already happened. A late cancellation must not misreport persisted data.
    return PlanJobResult(kind, payload=payload, committed=True)
