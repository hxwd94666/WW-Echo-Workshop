# 在独立 Rust 作业队列执行配装、预检及证据重验并记录作业状态。
from concurrent.futures import CancelledError
import math

from echo_workshop.application.allocation_transport import (
    allocation_payload,
    allocation_result,
    precheck_result,
)
from echo_workshop.application.compute_bridge import plain, require_background
from echo_workshop.application.compute_protocol import ComputeFailure
from echo_workshop.application.diagnostics import begin_job
from echo_workshop.core.allocation.models import AllocationCancelled
from echo_workshop.core.cancellation import OperationCancelled
from echo_workshop.core.scoring import ALGORITHM_VERSION


class AllocationService:
    def __init__(self, scores, *, compute=None):
        self.scores = scores
        self.compute = compute if compute is not None else scores.compute

    def _execute(
        self,
        operation,
        records,
        request,
        *,
        inventory_revision,
        snapshot,
        cancel_event=None,
        seconds=10,
        max_nodes=200000,
        reservations=None,
        edit_plans=None,
        replace_plans=None,
        allocations=None,
        require_complete=False,
        completed=(),
    ):
        require_background()
        if type(require_complete) is not bool:
            raise ValueError('完整分配校验标记须为布尔值')
        if (
            type(seconds) not in (int, float)
            or not math.isfinite(seconds)
            or not 0 < seconds <= 120
            or type(max_nodes) is not int
            or not 1 <= max_nodes <= 5000000
        ):
            raise ValueError('搜索预算无效')
        if cancel_event is not None and cancel_event.is_set():
            raise AllocationCancelled('配装已取消')
        job = begin_job(
            operation,
            {
                'inventory_revision': inventory_revision,
                'weight_version': getattr(snapshot, 'version', ''),
                'algorithm_version': ALGORITHM_VERSION,
            },
        )

        def build():
            payload = allocation_payload(
                records,
                request,
                inventory_revision=inventory_revision,
                snapshot=snapshot,
                seconds=seconds,
                max_nodes=max_nodes,
                reservations=reservations,
                edit_plans=edit_plans,
                replace_plans=replace_plans,
                cancel_event=cancel_event,
            )
            if allocations is not None:
                payload['allocations'] = plain(allocations)
                payload['require_complete'] = require_complete
                payload['completed'] = plain(completed)
            return payload

        try:
            job.stage('preparing')
            value = self.compute.submit(
                operation,
                build,
                lane='interactive' if operation == 'validate_allocation' else 'search',
                timeout=seconds + 3,
                cancel_event=cancel_event,
            ).result(timeout=seconds + 5)
            if cancel_event is not None and cancel_event.is_set():
                raise AllocationCancelled('配装已取消')
            if operation == 'allocate':
                result = allocation_result(value, request, inventory_revision, snapshot)
                job.finish(
                    counters={
                        'nodes': result.nodes,
                        'roles': len(result.roles),
                        'search_ms': round(result.elapsed * 1000, 2),
                    }
                )
            elif operation == 'allocation_precheck':
                result = precheck_result(value, request)
                job.finish()
            else:
                result = value
                job.finish()
            return result
        except (CancelledError, AllocationCancelled) as error:
            job.finish('cancelled')
            raise AllocationCancelled('配装已取消') from error
        except ComputeFailure as error:
            if error.kind in ('cancelled', 'closed'):
                job.finish('cancelled')
                raise AllocationCancelled('配装已取消') from error
            job.finish('failed', error=error)
            if error.kind in (
                'invalid_request',
                'invalid_record',
                'retired_policy',
                'invalid_allocation',
                'invalid_evidence',
                'invalid_rule',
                'invalid_numeric',
                'numeric_limit',
                'input_limit',
            ):
                raise ValueError(str(error)) from error
            raise
        except Exception as error:
            job.finish('failed', error=error)
            raise

    def solve(self, records, request, **kwargs):
        return self._execute('allocate', records, request, **kwargs)

    def precheck(self, records, request, **kwargs):
        try:
            return self._execute('allocation_precheck', records, request, **kwargs)
        except AllocationCancelled as error:
            raise OperationCancelled('配装预检已取消') from error

    def validate(
        self, records, request, *, allocations, require_complete=False, completed=(), **kwargs
    ):
        try:
            return self._execute(
                'validate_allocation',
                records,
                request,
                allocations=allocations,
                require_complete=require_complete,
                completed=completed,
                **kwargs,
            )
        except AllocationCancelled as error:
            raise OperationCancelled('方案重验已取消') from error
