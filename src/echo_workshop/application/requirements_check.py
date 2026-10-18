# 批量校验角色要求布局与套装约束。
"""Batch requirement checks in the shared worker; no Qt or scoring."""

from collections.abc import Mapping
from concurrent.futures import CancelledError
from dataclasses import dataclass
from uuid import uuid4

from echo_workshop.application.compute_bridge import require_background
from echo_workshop.application.compute_protocol import ComputeFailure
from echo_workshop.application.diagnostics import begin_job
from echo_workshop.core.cancellation import OperationCancelled, checkpoint
from echo_workshop.core.requirements import RoleRequirements


@dataclass(frozen=True)
class RequirementCheck:
    future: object
    identity: str
    keys: tuple


class RequirementsCheckService:
    """Freeze explicit requirement models, then let Rust reject invalid layouts."""

    def __init__(self, *, compute):
        self.compute = compute

    def start(self, requirements, *, cancel_event=None):
        if not isinstance(requirements, Mapping) or len(requirements) > 512:
            raise ValueError('要求校验需要明确的角色要求映射，最多 512 项')
        keys, models = [], []
        for key, value in requirements.items():
            if (
                not isinstance(key, tuple)
                or len(key) != 2
                or any(not isinstance(part, str) or not part for part in key)
                or not isinstance(value, RoleRequirements)
            ):
                raise ValueError('要求校验的角色身份或结构无效')
            keys.append(key)
            models.append(
                dict(
                    costs=list(value.costs),
                    main_names=[list(names) for names in value.main_names],
                    allowed_sonatas=list(value.allowed_sonatas),
                    set_requirements=[[name, count] for name, count in value.set_requirements],
                )
            )
        identity = uuid4().hex

        def build():
            checkpoint(cancel_event)
            return dict(identity=identity, models=models)

        future = self.compute.submit(
            'requirements_check', build, timeout=10, cancel_event=cancel_event
        )
        return RequirementCheck(future, identity, tuple(keys))

    @staticmethod
    def response(request):
        if not request.future.done():
            raise ComputeFailure('pending', '要求校验尚未完成')
        value = request.future.result()  # Completed; never wait on the GUI thread.
        if not isinstance(value, dict) or value.get('identity') != request.identity:
            raise ComputeFailure('request_identity', '要求校验返回身份错误')
        errors = value.get('errors')
        if not isinstance(errors, list) or len(errors) != len(request.keys):
            raise ComputeFailure('protocol', '要求校验返回数量错误')
        for error in errors:
            if error is not None and not isinstance(error, str):
                raise ComputeFailure('protocol', '要求校验返回结论错误')
        return tuple(zip(request.keys, errors))

    def check(self, requirements, *, cancel_event=None):
        require_background()
        job = begin_job('requirements')
        job.stage('validating')
        try:
            request = self.start(requirements, cancel_event=cancel_event)
            request.future.result(timeout=12)
            checkpoint(cancel_event)
            errors = self.response(request)
        except (CancelledError, OperationCancelled):
            job.finish('cancelled')
            raise OperationCancelled('要求校验已取消') from None
        except ComputeFailure as error:
            if error.kind in ('cancelled', 'closed'):
                job.finish('cancelled')
                raise OperationCancelled('要求校验已取消') from error
            job.finish('failed', error=error)
            raise
        except Exception as error:
            job.finish('failed', error=error)
            raise
        rejected = [(key, error) for key, error in errors if error]
        if rejected:
            job.finish('failed')
            raise ValueError(rejected[0][1])
        job.finish('completed')
        return errors
