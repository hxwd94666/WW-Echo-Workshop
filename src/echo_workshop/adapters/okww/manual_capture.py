# 先采样冻结后识别，避免慢OCR完成时的窗口状态作废已取得的手动截图。
import hashlib
import time

from ok import og
from echo_workshop.capture.manual_snapshot import FrozenManualSnapshot
from echo_workshop.capture.viewport import detect_echo_viewport
from echo_workshop.application.compute_protocol import ComputeFailure
from echo_workshop.core.cancellation import OperationCancelled
from .capture_device import capture_source_identity
from .snapshot_parse import detect_scene, parse_snapshot_record


def _guard(task, settings, runtime, request, identity, *, frozen=False):
    capture = runtime.capture
    if capture.single_request is not request and not any(
        pending is request for pending in getattr(task, '_manual_pending', ())
    ):
        return False
    reason = ''
    if task._destroyed or settings._destroyed or runtime._closed or runtime.stop_event.is_set():
        reason = '插件已停止，本次收录已取消'
    elif capture.expire_once():
        return False
    elif settings.config.get('InventoryProfileID', '') != request.profile_id:
        reason = '档案已切换，本次收录已取消'
    elif runtime.backups.restoring:
        reason = '库存恢复作业已开始，本次收录已取消'
    elif capture_source_identity(getattr(og, 'device_manager', None)) != identity:
        reason = '截图来源已变化，本次收录已取消'
    elif not frozen and (not settings._game_foreground() or not settings._capture_ready()):
        reason = '采样尚未完成，请保持游戏前台后重按收录快捷键'
    if reason:
        capture.cancel_once(reason)
        task.log_info('手动收录取消：' + reason)
        return False
    return True


def _cancel_current(task, capture, request, message, **kwargs):
    if capture.single_request is request:
        capture.cancel_once(message, **kwargs)
    else:
        task.log_warning(message)


def parse_manual_snapshot(task, settings, runtime, request, snapshot, identity):
    capture = runtime.capture
    frames_required = capture.manual_stable_frames
    seconds_required = capture.manual_stable_seconds
    started = time.monotonic()
    weights = request.weights if request.weights is not None else runtime.weights.active
    sonata = request.sonata if request.sonata is not None else runtime.sonata.active
    scene = detect_scene(task, snapshot.frame, snapshot.viewport)
    metadata, record = parse_snapshot_record(
        task,
        snapshot.frame,
        snapshot.viewport,
        settings.config,
        weights,
        request.profile_id,
        sonata,
        scene=scene,
        observations=runtime.observations,
        cancel_event=runtime.stop_event,
        cost_lookup=getattr(task, '_cached_cost', None),
    )
    if not _guard(task, settings, runtime, request, identity, frozen=True):
        return False
    analysis = metadata.analysis
    if record is None or analysis.layout not in ('bag', 'display'):
        _cancel_current(
            task,
            capture,
            request,
            '请在背包或声骸展示页收录',
            diagnostic_state='failed',
            summary_code='capture_wrong_page',
        )
        return False
    proof = snapshot.prove(metadata, record)
    if proof is None or not proof.valid(
        request,
        stable_frames=frames_required,
        stable_seconds=seconds_required,
        max_frame_gap=capture.max_frame_gap,
    ):
        _cancel_current(
            task,
            capture,
            request,
            '采样期间声骸信息发生变化，请保持选中项后重试',
            diagnostic_state='failed',
            summary_code='capture_unstable',
        )
        task.log_info('手动收录未提交：采样词条或名称发生变化')
        return False
    capture.single_progress(request, 'capturing_evidence', '正在保存收录截图', records=1)
    from echo_workshop.capture.evidence import prepare_evidence

    record, image = prepare_evidence(record, snapshot.frame, analysis)
    # Encoding and parsing must refer to the same immutable final sample.
    if hashlib.sha256(snapshot.frame.tobytes()).hexdigest() != proof.signature:
        raise ValueError('冻结截图内容发生变化')
    saved = capture.commit_single_snapshot(
        record,
        image,
        proof,
        request=request,
        commit_guard=lambda: _guard(task, settings, runtime, request, identity, frozen=True),
    )
    if saved:
        task._manual_total_added += 1
    if not saved and capture.single_request is request:
        _cancel_current(task, capture, request, '采样证明已失效，本次未保存，请重新按快捷键')
    task.log_info(
        f'手动收录冻结帧识别完成：页面={analysis.layout}，主词条={len(analysis.main_rows)}，副词条={len(analysis.sub_rows)}，解析耗时={time.monotonic() - started:.2f}s，已提交={saved}'
    )
    return False


def run_manual_capture(task, settings, runtime, request, *, wait=None):
    """Acquire fresh pixels under foreground guards; parse immutable pixels once."""
    capture = runtime.capture
    if any(pending is request for pending in getattr(task, '_manual_pending', ())):
        return False
    identity = capture_source_identity(getattr(og, 'device_manager', None))
    if request.source_identity is not None and identity != request.source_identity:
        capture.cancel_once('截图来源已变化，请重新按收录快捷键')
        task.log_info('手动收录取消：按键后截图来源已变化')
        return False
    wait = wait or runtime.stop_event.wait
    tokens, times, frames = [], [], []
    viewport = None
    frames_required = capture.manual_stable_frames
    seconds_required = capture.manual_stable_seconds
    interval = max(0.05, seconds_required / (frames_required - 1))
    capture.single_progress(request, 'sampling', '正在取得收录画面（0/3）')
    try:
        while len(frames) < frames_required:
            if not _guard(task, settings, runtime, request, identity):
                return False
            acquired_at = capture.clock()
            # Request the host's real acquisition, never its cached frame property.
            frame = task.next_frame()
            token = getattr(task.executor, '_last_frame_time', None)
            if not _guard(task, settings, runtime, request, identity):
                return False
            if frame is None or token is None or token == request.after_token or token in tokens:
                if wait(0.05):
                    return False
                continue
            observed = detect_echo_viewport(frame)
            if observed is None or (viewport is not None and observed != viewport):
                capture.cancel_once('采样画面无效或尺寸变化，请保持声骸页面后重试')
                return False
            viewport = observed
            frozen = frame.copy()
            frozen.flags.writeable = False
            frames.append(frozen)
            tokens.append(token)
            times.append(acquired_at)
            capture.single_progress(
                request,
                'sampling',
                f'正在取得收录画面（{len(frames)}/{frames_required}）',
                frames=len(frames),
            )
            if len(frames) < frames_required and wait(interval):
                return False
        snapshot = FrozenManualSnapshot(
            request, tuple(frames), tuple(tokens), tuple(times), viewport
        )
        capture.single_progress(
            request, 'recognizing', '画面已冻结，正在识别；此时可切出游戏', frames=len(frames)
        )
        task.log_info(
            f'手动收录画面已冻结：新帧={len(frames)}，采样跨度={times[-1] - times[0]:.2f}s；开始单次识别'
        )
        if not task._enqueue_manual(snapshot, request, identity, settings, runtime):
            capture.cancel_once('解析队列已满，请稍后重试')
        return False
    except OperationCancelled:
        capture.cancel_once(
            '本次收录已取消',
            diagnostic_state='cancelled',
            summary_code='capture_cancelled',
        )
        return False
    except (ValueError, OSError, ComputeFailure) as error:
        capture.cancel_once(
            '本次收录未完成：' + str(error),
            diagnostic_state='failed',
            summary_code='capture_failed',
        )
        task.log_warning('手动收录失败：' + str(error))
        return False
    except Exception as error:
        capture.cancel_once(
            '本次识别异常，请查看日志后重试',
            diagnostic_state='failed',
            summary_code='capture_failed',
        )
        task.log_warning(f'手动收录异常：{error}')
        return False
