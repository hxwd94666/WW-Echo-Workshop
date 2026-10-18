# 为自定义权重提供异步编辑投影与后台 Rust 构造、验证入口。
from concurrent.futures import CancelledError
from collections.abc import Mapping
import math

from echo_workshop.application.compute_bridge import plain, require_background
from echo_workshop.application.compute_protocol import ComputeFailure
from echo_workshop.application.diagnostics import begin_job
from echo_workshop.core.cancellation import OperationCancelled, checkpoint


class CustomWeightService:
    def __init__(self, *, compute):
        self.compute = compute

    def project_async(self, original, *, identity, weight_version, cancel_event=None):
        return self._submit(
            'project',
            original,
            identity=identity,
            weight_version=weight_version,
            cancel_event=cancel_event,
        )

    def _submit(
        self,
        mode,
        original,
        *,
        identity,
        weight_version,
        main=None,
        sub=None,
        edited=None,
        cancel_event=None,
    ):
        checkpoint(cancel_event)

        def build():
            checkpoint(cancel_event)
            # Non-finite numbers have no JSON representation; this is transport
            # schema checking, not coefficient normalization or a Python backend.
            payload = dict(
                mode=mode,
                identity=list(identity),
                weight_version=weight_version,
                original=plain(original),
                main=plain(main),
                sub=plain(sub),
                edited=plain(edited),
            )
            pending = [payload]
            while pending:
                checkpoint(cancel_event)
                value = pending.pop()
                if isinstance(value, Mapping):
                    pending.extend(value.values())
                elif isinstance(value, (list, tuple)):
                    pending.extend(value)
                elif type(value) is float and not math.isfinite(value):
                    raise ValueError('权重 JSON 数值须为有限值')
            return payload

        return self.compute.submit('custom_weights', build, timeout=10, cancel_event=cancel_event)

    @staticmethod
    def response(value, *, identity, weight_version):
        if not isinstance(value, dict):
            raise ComputeFailure('protocol', '自定义权重返回数据结构错误')
        if value.get('identity') != list(identity) or value.get('weight_version') != weight_version:
            raise ComputeFailure('request_identity', '自定义权重返回模板或资料身份错误')
        return value

    @classmethod
    def projected_values(cls, value, *, identity, weight_version):
        try:
            value = cls.response(value, identity=identity, weight_version=weight_version)
            main, sub = value['main'], value['sub']
            if not isinstance(main, dict) or not isinstance(sub, dict):
                raise ComputeFailure('protocol', '权重编辑投影结构错误')
            from echo_workshop.resources.custom_weights import MAIN_NAMES, SUB_NAMES

            if (
                set(main) != {'1', '3', '4'}
                or set(sub) != set(SUB_NAMES)
                or any(
                    not isinstance(row, dict) or not row or set(row) - set(MAIN_NAMES)
                    for row in main.values()
                )
            ):
                raise ComputeFailure('protocol', '权重编辑投影字段错误')
            values = [v for row in main.values() for v in row.values()] + list(sub.values())
            if any(type(v) not in (int, float) or not math.isfinite(v) for v in values):
                raise ComputeFailure('protocol', '权重编辑投影数值结构错误')
            return main, sub
        except (KeyError, TypeError, AttributeError) as error:
            raise ComputeFailure('protocol', '权重编辑投影结构错误') from error

    def _wait(self, mode, original, *, identity, weight_version, cancel_event=None, **kwargs):
        require_background()
        checkpoint(cancel_event)
        job = begin_job('custom-weights', metadata={'weight_version': weight_version})
        job.stage(mode)
        try:
            value = self._submit(
                mode,
                original,
                identity=identity,
                weight_version=weight_version,
                cancel_event=cancel_event,
                **kwargs,
            ).result(timeout=12)
            checkpoint(cancel_event)
            value = self.response(value, identity=identity, weight_version=weight_version)
            if mode != 'project' and not isinstance(value.get('template'), dict):
                raise ComputeFailure('protocol', '自定义权重返回模板结构错误')
        except (CancelledError, OperationCancelled) as error:
            job.finish('cancelled')
            raise OperationCancelled('自定义权重计算已取消') from error
        except ComputeFailure as error:
            if error.kind in ('cancelled', 'closed'):
                job.finish('cancelled')
                raise OperationCancelled('自定义权重计算已取消') from error
            job.finish('failed', error=error)
            if error.kind in (
                'invalid_custom_weights',
                'invalid_request',
                'invalid_numeric',
                'numeric_limit',
                'input_limit',
            ):
                raise ValueError(str(error)) from error
            raise
        except Exception as error:
            job.finish('failed', error=error)
            raise
        job.finish('completed')
        return value

    def editable_values(self, original, **kwargs):
        value = self._wait('project', original, **kwargs)
        return self.projected_values(
            value, identity=kwargs['identity'], weight_version=kwargs['weight_version']
        )

    def build(self, original, main, sub, **kwargs):
        return self._wait('build', original, main=main, sub=sub, **kwargs)['template']

    def validate(self, original, edited, **kwargs):
        return self._wait('validate', original, edited=edited, **kwargs)['template']
