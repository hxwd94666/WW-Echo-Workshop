# 串行执行前台扫描点击、排序与滚轮。
from ok import og
from echo_workshop.capture.viewport import EchoViewport
from echo_workshop.capture.scan.grid import bag_sort_direction


class ScanInput:
    def _fresh(
        self, *, allow_paused=False, require_grid=True, observation_only=False, predicate=None
    ):
        # A bounded fresh-frame wait, including after menu/scroll animation.
        for _ in range(5):
            observed = self._observe(allow_paused=allow_paused, observation_only=observation_only)
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
        self.log_info('扫描输入：前台原生；连续5次24下、第6次25下；固定点击位置')
        observed = self._fresh()
        if observed is None:
            return fail('背包列表未识别；请打开背包并回到顶部')
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
            # Re-selecting the active sort can leave the game menu open. Its
            # dismissal is not proof of a successful sort. Verify the label,
            # explicitly select the first previously detected tile, then wait
            # for the closed menu and selected grid on new captured frames.
            first = min(observed[-1].tiles, key=lambda tile: (tile.row, tile.column))
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
            self.log_info('扫描准备：选中第一件声骸并关闭排序菜单')
            if not self._click_checked(first.center):
                return None
            observed = self._fresh(
                predicate=lambda view: (
                    is_level(view)
                    and self._sort_box(view, menu=True) is None
                    and view[-1].selected == (0, 0)
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
                    is_level(view) and bag_sort_direction(view[0], view[3]) == 'descending'
                )
            )
        if observed is None or bag_sort_direction(observed[0], observed[3]) != 'descending':
            return fail('等级降序未确认；请切换为等级从高到低后重试')
        self.log_info('扫描准备完成：等级降序已确认')
        return observed

    def _move_scan_pointer(self, x, y):
        """Reuse the host's real-pointer move, never switch its global backend."""
        if not self._guard():
            return False
        from types import SimpleNamespace
        from ok.device.interaction_methods.pydirect import PyDirectInteraction

        # The host move checks clickable and converts captured pixels through
        # capture.get_abs_cords. No interaction construction/on_run, activation,
        # input blocking or global FAILSAFE/config mutation is performed here.
        adapter = SimpleNamespace(capture=og.device_manager.capture_method, clickable=self._guard)
        PyDirectInteraction.move(adapter, x, y)
        return self._guard()

    def _send_scan_wheel(self, x, y, count):
        """One native wheel step, guarded again after establishing real hover."""
        if not self._guard():
            return False
        import mouse

        if self._mouse_button_busy(mouse):
            return False
        absolute = og.device_manager.capture_method.get_abs_cords(x, y)
        position = mouse.get_position()
        if max(abs(a - b) for a, b in zip(absolute, position)) > 5:
            if not self._move_scan_pointer(x, y):
                return False
            position = mouse.get_position()
            if max(abs(a - b) for a, b in zip(absolute, position)) > 5:
                self._scan.pause('鼠标正在操作，扫描已暂停；松开后继续')
                self._active_runtime.capture.pause()
                self._mouse_paused = True
                return False
        if not self._guard():
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
        if not self._guard():
            return False
        import mouse

        if self._mouse_button_busy(mouse):
            return False
        if not self._move_scan_pointer(x, y):
            return False
        self.sleep(0.02)
        if not self._guard():
            return False
        absolute = og.device_manager.capture_method.get_abs_cords(x, y)
        position = mouse.get_position()
        if max(abs(a - b) for a, b in zip(absolute, position)) > 5:
            if not self._move_scan_pointer(x, y):
                return False
            position = mouse.get_position()
            if max(abs(a - b) for a, b in zip(absolute, position)) > 5:
                self._scan.pause('鼠标正在操作，扫描已暂停；松开后继续')
                self._active_runtime.capture.pause()
                self._mouse_paused = True
                return False
        if not self._guard() or self._mouse_button_busy(mouse):
            return False
        try:
            mouse.press('left')
            self.sleep(0.03)
        finally:
            mouse.release('left')
            self.executor.reset_scene()
        return self._guard()

    def _mouse_button_busy(self, mouse):
        if mouse.is_pressed('left') or mouse.is_pressed('right'):
            self._scan.pause('鼠标正在操作，扫描已暂停；松开后继续')
            self._active_runtime.capture.pause()
            self._mouse_paused = True
            return True
        return False

    @staticmethod
    def _wheel_point(grid, area):
        # Stay inside the scrollable list, but between cards: hovering a card
        # changes its highlight and can spoil the overlap signature afterward.
        rows = grid.rows()
        if len(rows) >= 2 and len(rows[0]) >= 2:
            left, right = rows[0][:2]
            below = rows[1][0]
            x = round((left.bounds[0] + left.bounds[2] + right.bounds[0]) / 2)
            y = round((left.bounds[1] + left.bounds[3] + below.bounds[1]) / 2)
            if (
                right.bounds[0] > left.bounds[0] + left.bounds[2]
                and below.bounds[1] > left.bounds[1] + left.bounds[3]
            ):
                return x, y
        return (area.x + round(area.reference_width * 0.40), area.y + round(area.height * 0.50))

    def _scroll_batch(self, observed):
        from echo_workshop.capture.scan.levels import wheel_count

        if self._pending_scroll is None:
            self._scroll_batches += 1
            self._pending_scroll = [self._scroll_batches, wheel_count(self._scroll_batches), 0]
        batch, steps, sent = self._pending_scroll
        self._scan_job.stage('scrolling')
        point = self._wheel_point(self._fixed_grid, self._fixed_area)
        for _ in range(steps - sent):
            while True:
                observed = self._scroll_ready(observed)
                if observed is None:
                    return
                if self._move_scan_pointer(*point) and self._send_scan_wheel(*point, -1):
                    break
                if self._scan.status.state != 'paused':
                    return
            # Pace individual notches so the game can consume each wheel event.
            self._wheel_steps += 1
            self._pending_scroll[2] += 1
            self.sleep(0.08)
        self.sleep(0.30)
        observed = self._fresh()
        if observed is None:
            return
        self._scan.fixed_next_page(observed[-1])
        self._submit_page_levels(observed)
        self._pending_scroll = None
        self._scan_job.stage(
            'scanning', {'scroll_batches': batch, 'wheel_steps': self._wheel_steps}
        )
        self.log_info(
            '固定滚动完成：第'
            + str(batch)
            + '次；滚轮='
            + str(steps)
            + '；不进行金边、重叠或坐标校验'
        )

    def _scroll_ready(self, observed):
        # Keep already-sent wheel count in this loop, including pause/resume.
        while not self._guard():
            if (
                self._destroyed
                or self._active_runtime._closed
                or self._active_runtime.stop_event.is_set()
                or self.executor.exit_event.is_set()
            ):
                self._scan.stop('扫描已取消')
                return None
            if self._scan.status.state != 'paused':
                return None
            self._resume_fixed_if_requested()
            self.sleep(0.05)
        return observed

    def _resume_fixed_if_requested(self):
        if getattr(self, '_mouse_paused', False) and self._settings._game_foreground():
            import mouse

            if not mouse.is_pressed('left') and not mouse.is_pressed('right'):
                self._scan.resume_requested = True
                self._mouse_paused = False
        if self._scan.resume_requested and self._guard(allow_paused=True):
            if self._scan.resume(self._fixed_grid):
                self._active_runtime.capture.resume()
