# 串行执行前台扫描点击、排序与滚轮。
from ok import og
from ww_echo_workshop.capture.viewport import EchoViewport
from ww_echo_workshop.capture.scan.grid import bag_sort_direction
from ww_echo_workshop.capture.scan.scroll_receipt import (
    FixedScrollBatch,
    ROWS_PER_BATCH,
    WHEEL_INTERVAL,
    WHEEL_SETTLE,
)


class ScanInput:
    def _fresh(self, *, require_grid=True, predicate=None):
        # A bounded fresh-frame wait, including after menu/scroll animation.
        for _ in range(5):
            observed = self._observe()
            if (
                observed is not None
                and (observed[-1] is not None or not require_grid)
                and (predicate is None or predicate(observed))
            ):
                return observed
            if self._scan.status.state not in ('preparing', 'running', 'paused'):
                break
            self.sleep(0.15)
        return None

    def _click_checked(self, point, *, settle=0.25):
        with self._measure('click_and_settle'):
            if not self._send_scan_click(*point):
                return False
            self.sleep(settle)
        return True

    @staticmethod
    def _sort_box(observed, menu=False):
        frame, _, _, viewport, metadata, _ = observed
        area = EchoViewport.checked(viewport, frame.shape[1], frame.shape[0])
        lower, upper = (0.52, 0.88) if menu else (0.87, 0.96)
        candidates = [
            b
            for b in metadata.original_boxes
            if area.x + area.reference_width * 0.13 <= b.x < area.x + area.reference_width * 0.33
            and area.y + area.height * lower <= b.y < area.y + area.height * upper
            and (
                str(b.name).strip()
                in (
                    '等级顺序',
                    'Level Order',
                    '品质顺序',
                    'Quality Order',
                    '获得时间顺序',
                    'Time Obtained',
                    '调谐状态顺序',
                    'Tuning Status',
                    '已弃置优先',
                    'Discarded First',
                )
                if not menu
                else str(b.name).strip() in ('等级顺序', 'Level Order')
            )
        ]
        return candidates[0] if len(candidates) == 1 else None

    def _prepare_sort(self):
        self._prepare_error = ''

        def fail(message):
            self._prepare_error = message
            return None

        def is_level(view):
            label = self._sort_box(view)
            return label is not None and str(label.name).strip() in ('等级顺序', 'Level Order')

        self.log_info('扫描准备：确认背包列表和排序')
        self.log_info('扫描输入：前台原生；每批三行，连续5批24格、第6批25格；固定滚轮及点击点位')
        observed = self._fresh()
        if observed is None:
            return fail('背包列表未识别；请打开背包并回到顶部')
        if not observed[-1].at_top:
            return fail('请将声骸背包拉至顶部，再开始扫描')
        # Warm the shared selected-panel/icon path before the first input. This
        # never commits the observation and never fetches remote resources.
        prepared = self._scan_parser.read_record(
            observed[4],
            observed[0],
            observed[3],
            self._scan.profile_id,
            self._scan.weights,
            self._scan.sonata,
        )
        if prepared is None:
            return fail('右侧声骸词条未识别；请选中一件声骸后重试')
        box = self._sort_box(observed) if observed else None
        if box is None:
            return fail('排序按钮未识别；请关闭遮挡背包的窗口后重试')
        if not is_level(observed) or self._sort_box(observed, menu=True) is not None:
            if self._sort_box(observed, menu=True) is None:
                if not self._click_checked(
                    (round(box.x + box.width / 2), round(box.y + box.height / 2))
                ):
                    return None
            self.log_info('扫描准备：等待等级排序选项')
            observed = self._fresh(
                require_grid=False,
                predicate=lambda view: self._sort_box(view, menu=True) is not None,
            )
            box = self._sort_box(observed, menu=True) if observed else None
            if box is None:
                return fail('等级排序选项未识别；请关闭菜单后重试')
            if not self._click_checked(
                (round(box.x + box.width / 2), round(box.y + box.height / 2))
            ):
                return None
            observed = self._fresh(
                predicate=lambda view: is_level(view) or self._sort_box(view, menu=True) is not None
            )
            if observed is None or not observed[-1].at_top:
                return fail('排序后未确认列表顶部；请切换等级排序并回到顶部重试')
            if self._sort_box(observed, menu=True) is not None:
                # Some host/game versions keep the menu open. Only use a
                # fresh, measured top slot above the menu to dismiss it.
                first = min(observed[-1].tiles, key=lambda tile: (tile.row, tile.column))
                _, top, _, height = observed[3]
                if first.row != 0 or first.center[1] >= top + height * 0.52:
                    return fail('排序菜单遮挡列表；请手动关闭菜单后重试')
                self.log_info('扫描准备：使用排序后实测首格关闭菜单')
                if not self._click_checked(first.center):
                    return None
                observed = self._fresh(
                    predicate=lambda view: (
                        is_level(view)
                        and view[-1].at_top
                        and self._sort_box(view, menu=True) is None
                        and view[-1].selected == (first.row, first.column)
                    )
                )
                if observed is None:
                    return fail('等级排序或第一件声骸未确认；请回到背包顶部重试')
        else:
            self.log_info('扫描准备：已是等级排序，直接检查降序')
        direction = bag_sort_direction(observed[0], observed[3])
        if direction == 'ascending':
            area = EchoViewport.checked(observed[3], observed[0].shape[1], observed[0].shape[0])
            if not self._click_checked(
                (area.x + round(area.reference_width * 0.350), area.y + round(area.height * 0.917))
            ):
                return None
            observed = self._fresh(
                predicate=lambda view: (
                    is_level(view)
                    and view[-1].at_top
                    and self._sort_box(view, menu=True) is None
                    and bag_sort_direction(view[0], view[3]) == 'descending'
                )
            )
        if observed is None or bag_sort_direction(observed[0], observed[3]) != 'descending':
            return fail('等级降序未确认；请切换为等级从高到低后重试')
        self.log_info('扫描准备完成：等级降序已确认')
        return observed

    def _move_scan_pointer(self, x, y):
        """Reuse the host's real-pointer move, never switch its global backend."""
        if not self._guard('input'):
            return False
        from types import SimpleNamespace
        from ok.device.interaction_methods.pydirect import PyDirectInteraction

        # The host move checks clickable and converts captured pixels through
        # capture.get_abs_cords. No interaction construction/on_run, activation,
        # input blocking or global FAILSAFE/config mutation is performed here.
        adapter = SimpleNamespace(
            capture=og.device_manager.capture_method, clickable=lambda: self._guard('input')
        )
        PyDirectInteraction.move(adapter, x, y)
        return self._guard('input')

    def _send_scan_wheel(self, x, y, count):
        """One native wheel step, guarded again after establishing real hover."""
        if not self._guard('input'):
            return False
        import mouse

        if self._mouse_button_busy(mouse):
            return False
        absolute = og.device_manager.capture_method.get_abs_cords(x, y)
        if not self._pointer_matches(mouse, absolute) and not self._move_scan_pointer(x, y):
            return False
        if not self._ready_for_mouse_input(mouse, absolute):
            return False
        mouse.wheel(count)
        self.executor.reset_scene()
        return True

    def _send_scan_click(self, x, y):
        """Foreground click using the same captured-to-desktop map as wheel.

        PostMessage may miss repeated selections even while the native wheel
        works. Do not change the configured host interaction for other tasks.
        Always release our button, including stop/focus-loss during the hold.
        """
        if not self._guard('input'):
            return False
        import mouse

        if self._mouse_button_busy(mouse):
            return False
        if not self._move_scan_pointer(x, y):
            return False
        self.sleep(0.02)
        if not self._guard('input'):
            return False
        absolute = og.device_manager.capture_method.get_abs_cords(x, y)
        if not self._pointer_matches(mouse, absolute) and not self._move_scan_pointer(x, y):
            return False
        if not self._ready_for_mouse_input(mouse, absolute):
            return False
        try:
            mouse.press('left')
            self.sleep(0.03)
        finally:
            mouse.release('left')
            self.executor.reset_scene()
        return self._guard('input')

    @staticmethod
    def _pointer_matches(mouse, absolute):
        return max(abs(a - b) for a, b in zip(absolute, mouse.get_position())) <= 5

    def _ready_for_mouse_input(self, mouse, absolute):
        # The foreground probe and pointer move can take long enough for
        # the user to have moved or pressed a button since the first check.
        if not self._guard('input') or self._mouse_button_busy(mouse):
            return False
        if self._pointer_matches(mouse, absolute):
            return True
        self._pause_for_mouse()
        return False

    def _pause_for_mouse(self):
        self._scan.pause('鼠标正在操作，扫描已暂停；松开后继续', generation=self._generation)
        self._active_runtime.capture.pause(generation=self._generation)
        self._mouse_paused = True

    def _mouse_button_busy(self, mouse):
        busy = mouse.is_pressed('left') or mouse.is_pressed('right')
        if busy:
            self._pause_for_mouse()
        return busy

    @staticmethod
    def _fixed_wheel_point(grid, area):
        rows = grid.rows()
        if len(rows) >= 2 and len(rows[0]) >= 2:
            left, right = rows[0][:2]
            below = rows[1][0]
            right_edge, bottom_edge = left.bounds[0] + left.bounds[2], left.bounds[1] + left.bounds[3]
            if right.bounds[0] > right_edge and below.bounds[1] > bottom_edge:
                return round((right_edge + right.bounds[0]) / 2), round(
                    (bottom_edge + below.bounds[1]) / 2
                )
        return area.x + round(area.reference_width * 0.40), area.y + round(area.height * 0.50)

    @staticmethod
    def _fixed_slot_points(grid, area):
        rows = grid.rows()
        first = rows[0][0]
        x, y = first.center
        dx = rows[0][1].bounds[0] - first.bounds[0] if len(rows[0]) > 1 else round(
            area.height * 0.1633
        )
        dy = rows[1][0].bounds[1] - first.bounds[1] if len(rows) > 1 else round(
            area.height * 0.196
        )
        return {
            (row, col): (x + col * dx, y + row * dy)
            for row in range(ROWS_PER_BATCH)
            for col in range(6)
        }

    def _click_scan_slot(self, tile):
        point = self._slot_points.get((tile.row, tile.column))
        x, y, width, height = tile.bounds
        if point is None or not (x < point[0] < x + width and y < point[1] < y + height):
            self._scan.stop('背包卡片偏离固定点击点位；请重新扫描', generation=self._generation)
            return False
        return self._click_checked(point, settle=0.02)

    def _scroll_batch(self, observed=None):
        from ww_echo_workshop.capture.scan.levels import page_content

        if self._pending_scroll is None:
            self._scroll_batches += 1
            self._pending_scroll = FixedScrollBatch(self._fixed_grid, self._scroll_batches)
        batch = self._pending_scroll
        self._scan_job.stage('scrolling')

        def pause(message):
            if self._scan.status.state == 'running':
                self._scan.pause(message, generation=self._generation)
                self._active_runtime.capture.pause(generation=self._generation)

        token = observed[1] if observed is not None else self._last_token
        if batch.remaining:
            while batch.remaining:
                if not self._guard() or not self._send_scan_wheel(*self._wheel_position, -1):
                    return
                batch.input_sent(token)
                self._wheel_steps += 1
                self.sleep(WHEEL_INTERVAL)
            self.sleep(WHEEL_SETTLE)
        observed = self._fresh(predicate=lambda view: batch.settled(view[-1]))
        if observed is None:
            batch.require_observation()
            pause('固定滚动后的列表未确认；请勿移动列表，已收录记录保留')
            return
        destination = observed[-1]
        if batch.incomplete_turn(destination):
            self._scan.stop('固定滚动未翻满三行；请从列表顶部重新扫描', generation=self._generation)
            return
        if not self._guard() or not self._scan.accept_fixed_page(
            batch.origin, destination, generation=self._generation
        ):
            return
        self._fixed_grid = destination
        self._page_signature = page_content(observed[0], destination.tiles)
        self._page += 1
        self._submit_page_levels(observed)
        self._pending_scroll = None
        self._scan_job.stage(
            'scanning', {'scroll_batches': self._scroll_batches, 'wheel_steps': self._wheel_steps}
        )
        self.log_info(f'固定滚动完成：第{batch.number}批；滚轮={batch.steps}；行偏移={ROWS_PER_BATCH}')

    def _resume_page_if_requested(self):
        if getattr(self, '_mouse_paused', False) and self._settings._game_foreground():
            import mouse

            if not mouse.is_pressed('left') and not mouse.is_pressed('right'):
                self._scan.request_resume(generation=self._generation)
                self._mouse_paused = False
        if not self._scan.resume_requested or self._observe(operation='resume') is None:
            return False
        if not self._guard('resume') or not self._scan.resume(generation=self._generation):
            return False
        if self._pending_scroll is not None:
            self._pending_scroll.require_observation()
        self._active_runtime.capture.resume(generation=self._generation)
        return True
