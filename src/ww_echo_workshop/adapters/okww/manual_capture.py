# 先采样冻结后识别，避免慢OCR完成时的窗口状态作废已取得的手动截图。
import hashlib
import time
from ok.task.exceptions import FinishedException, TaskDisabledException

from ww_echo_workshop.capture.manual_snapshot import ManualFrameBuffer
from ww_echo_workshop.core.cancellation import OperationCancelled
from .capture_device import capture_source_identity, device_manager, read_task_sample
from .panel_ocr import make_panel_reader
from ww_echo_workshop.capture.snapshot_parse import detect_scene, parse_snapshot_record


def _context_current(task, settings, runtime, request, identity):
    capture = runtime.capture
    if capture.single_request is not request or request.cancelled.is_set():
        return False
    reason = ''
    if task._destroyed or settings._destroyed or runtime._closed or runtime.stop_event.is_set():
        reason = '插件已停止，本次收录已取消'
    elif capture.expire_once(request):
        return False
    elif settings.config.get('InventoryProfileID', '') != request.profile_id:
        reason = '档案已切换，本次收录已取消'
    elif runtime.backups.restoring:
        reason = '库存恢复作业已开始，本次收录已取消'
    elif capture_source_identity(device_manager()) != identity:
        reason = '截图来源已变化，本次收录已取消'
    if reason:
        capture.cancel_once(reason, request=request)
        task.log_info('手动收录取消：' + reason)
        return False
    return True


def _guard(task, settings, runtime, request, identity, *, frozen=False):
    if not _context_current(task, settings, runtime, request, identity):
        return False
    if frozen:
        return True
    ready = settings._game_foreground() and settings._capture_ready()
    # Host probes can return after a replacement request or capture selection.
    if not _context_current(task, settings, runtime, request, identity):
        return False
    if ready:
        return True
    reason = '采样尚未完成，请保持游戏前台后重按收录快捷键'
    runtime.capture.cancel_once(reason, request=request)
    task.log_info('手动收录取消：' + reason)
    return False


def _cancel_current(task, capture, request, message, **kwargs):
    if capture.single_request is request:
        capture.cancel_once(message, request=request, **kwargs)
    else:
        task.log_warning(message)


def parse_manual_snapshot(task, settings, runtime, request, snapshot, identity, frozen_settings):
    if not _guard(task, settings, runtime, request, identity, frozen=True):
        return False
    capture = runtime.capture
    frames_required = capture.manual_stable_frames
    seconds_required = capture.manual_stable_seconds
    started = time.monotonic()
    weights = request.weights if request.weights is not None else runtime.weights.active
    sonata = request.sonata if request.sonata is not None else runtime.sonata.active
    parser = make_panel_reader(
        task.executor,
        frozen_settings,
        threshold=task.ocr_default_threshold,
        remembered=getattr(task, 'auto_matched_ids', None),
    )
    # The single manual worker can retain templates across requests, without
    # retaining the previous request's OCR rules or selected role.
    parser.sonata_matcher = getattr(task, '_manual_sonata_matcher', None)
    scene = detect_scene(parser, snapshot.frame, snapshot.viewport)
    metadata, record = parse_snapshot_record(
        parser,
        snapshot.frame,
        snapshot.viewport,
        frozen_settings,
        weights,
        request.profile_id,
        sonata,
        scene=scene,
        observations=runtime.observations,
        cancel_event=request.cancelled,
    )
    task._manual_sonata_matcher = parser.sonata_matcher
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
    from ww_echo_workshop.capture.evidence import prepare_evidence

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
    if capture.is_manual_frozen(request):
        return False

    identity = capture_source_identity(device_manager())
    if request.source_identity is not None and identity != request.source_identity:
        capture.cancel_once('截图来源已变化，请重新按收录快捷键', request=request)
        task.log_info('手动收录取消：按键后截图来源已变化')
        return False
    wait = wait or request.cancelled.wait
    buffer = ManualFrameBuffer(request)
    frames_required = capture.manual_stable_frames
    seconds_required = capture.manual_stable_seconds
    interval = max(0.05, seconds_required / (frames_required - 1))
    frozen_settings = dict(settings.config)
    capture.single_progress(request, 'sampling', f'正在取得收录画面（0/{frames_required}）')
    try:
        while len(buffer.frames) < frames_required:
            if not _guard(task, settings, runtime, request, identity):
                return False
            sample = read_task_sample(task, capture.clock, device_manager)
            if not _guard(task, settings, runtime, request, identity):
                return False
            if sample is None or not buffer.add(sample.pixels, sample.token, sample.acquired_at):
                if wait(0.05):
                    return False
                continue
            count = len(buffer.frames)
            capture.single_progress(
                request, 'sampling', f'正在取得收录画面（{count}/{frames_required}）', frames=count
            )
            if count < frames_required and wait(interval):
                return False
        snapshot = buffer.freeze()
        if not _context_current(
            task, settings, runtime, request, identity
        ) or not capture.mark_manual_frozen(request):
            return False
        capture.single_progress(
            request,
            'recognizing',
            '画面已冻结，正在识别；此时可切出游戏',
            frames=len(snapshot.frames),
        )
        task.log_info(
            f'手动收录画面已冻结：新帧={len(snapshot.frames)}，采样跨度={snapshot.times[-1] - snapshot.times[0]:.2f}s；开始单次识别'
        )
        if not task._enqueue_manual(
            snapshot, request, identity, settings, runtime, frozen_settings
        ):
            capture.cancel_once('解析队列已满，请稍后重试', request=request)
        return False
    except (OperationCancelled, FinishedException, TaskDisabledException):
        capture.cancel_once(
            '本次收录已取消',
            diagnostic_state='cancelled',
            summary_code='capture_cancelled',
            request=request,
        )
        return False
    except ValueError as error:
        capture.cancel_once(
            str(error),
            request=request,
        )
        return False
    except Exception as error:
        capture.fail_manual(
            'sampling',
            error,
            request=request,
            message='本次采样异常，请查看日志后重试',
        )
        return False
