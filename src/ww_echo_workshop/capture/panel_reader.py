# 共享同帧详情裁剪、词条解析、元数据和记录构建。
from contextlib import nullcontext
from dataclasses import dataclass

from ww_echo_workshop.application.live_selection import resolve_live_selection
from ww_echo_workshop.application.templates import DEFAULT_TEMPLATE
from ww_echo_workshop.capture.metadata import (
    MetadataPass,
    record_with_metadata,
    refine_echo_metadata,
    scan_ocr_regions,
)
from ww_echo_workshop.capture.stat_observation import analyze_echo_stats
from ww_echo_workshop.capture.viewport import COMPARISON_SPLIT, EchoViewport, detect_echo_viewport
from ww_echo_workshop.core.cancellation import checkpoint


@dataclass
class OcrTextBox:
    x: int
    y: int
    width: int
    height: int
    name: str
    confidence: float


class PanelReader:
    def __init__(
        self, read_region, *, remembered=None, timings=None, scan_icons=False, rating_rgb=None
    ):
        self.read_region = read_region
        self.remembered = remembered
        self.timings = timings
        self.scan_icons = scan_icons
        self.rating_rgb = rating_rgb
        self.sonata_matcher = None

    def read_metadata(
        self,
        frame,
        settings,
        snapshot,
        *,
        observation_service,
        cancel_event=None,
        viewport=None,
        boxes=None,
        scan=False,
        comparison_side='right',
        fast=False,
        layout_hint='',
        comparison_hint=False,
        cost_lookup=None,
        retry_live_cost=False,
    ):
        """Shared same-frame selected-panel parser for manual and automatic intake."""
        height, width = frame.shape[:2]
        viewport = viewport or detect_echo_viewport(frame)
        if viewport is None:
            raise ValueError('未获取到游戏画面')

        def read(region, name):
            checkpoint(cancel_event)
            return self.read_region(frame, region, name)

        if boxes is None:
            regions = (
                scan_ocr_regions(viewport, width, height)
                if scan
                else EchoViewport.checked(viewport, width, height).ocr_regions()
            )
            boxes = [b for region in regions for b in read(region, 'echo-content')]
        resolver = None
        if snapshot is not None:

            def resolver(normalized, kind):
                return resolve_live_selection(
                    snapshot,
                    normalized,
                    fixed=(
                        settings.get('TemplateCharacterID', ''),
                        settings.get('TemplateModalID', ''),
                    ),
                    auto=bool(settings.get('自动匹配评分模板', False)),
                    remembered=self.remembered,
                    page_kind=kind,
                )

        def analyze(observed):
            checkpoint(cancel_event)
            return analyze_echo_stats(
                observed,
                width,
                height,
                settings.get('角色评分模板', DEFAULT_TEMPLATE),
                auto_match=bool(settings.get('自动匹配评分模板', False)),
                template_resolver=resolver,
                viewport=viewport,
                comparison_side=comparison_side,
                observation_service=observation_service,
                weight_version=snapshot.version,
                cancel_event=cancel_event,
                layout_hint=layout_hint,
                comparison_hint=comparison_hint,
                cost_lookup=cost_lookup,
                retry_cost=(lambda region: read(region, 'echo-live-cost'))
                if retry_live_cost
                else None,
                rating_rgb=self.rating_rgb,
            )

        analysis = analyze(boxes)
        if fast or comparison_side == 'left':
            return MetadataPass(analysis, tuple(boxes), tuple(boxes))
        if analysis.comparison:
            # Detect with both columns, then retain only the selected side.
            # Scoped metadata rereads no longer include both section headings.
            comparison_hint = True
            area = EchoViewport.checked(viewport, width, height)
            split = area.offset('right')[0] + area.reference_width * COMPARISON_SPLIT
            boxes = [box for box in boxes if box.x >= split]
        return refine_echo_metadata(
            analysis,
            boxes,
            width,
            height,
            lambda region: read(region, 'echo-metadata'),
            analyze,
            complete_only=scan,
            characters=getattr(snapshot, 'characters', None),
        )

    def read_record(self, metadata, frame, viewport, profile, snapshot, sonata):
        timings = self.timings
        icon = None
        layout = getattr(metadata.analysis, 'layout', '')
        if layout in ('bag', 'display') and sonata is not None:
            from ww_echo_workshop.capture.sonata_icons import SonataMatcher

            matcher = self.sonata_matcher
            if matcher is None or matcher.snapshot.version != sonata.version:
                matcher = self.sonata_matcher = SonataMatcher(sonata)
            with timings.stage('sonata_match') if timings is not None else nullcontext():
                icon = (
                    matcher.match(frame, layout, viewport, scan=True)
                    if self.scan_icons and layout == 'bag'
                    else matcher.match(frame, layout, viewport)
                )
            if timings is not None:
                timings.increment('sonata_' + getattr(matcher, 'last_search', 'unknown'))
        height, width = frame.shape[:2]
        with timings.stage('record_and_game_status') if timings is not None else nullcontext():
            return record_with_metadata(
                metadata,
                profile,
                width,
                height,
                icon=icon,
                characters=getattr(snapshot, 'characters', None),
                frame=frame,
            )
