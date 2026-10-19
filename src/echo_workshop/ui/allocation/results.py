# 展示五件配装结果及保存操作。
"""Three-step allocation UI around the original single-flight solver/jobs."""

import logging
from PySide6.QtCore import QTimer


# Inherit the existing host queue/file sink, without adding any handler.
logger = logging.getLogger('ok.EchoAllocationPrecheck')


class AllocationUX:
    def start_precheck(self):
        if self.incoming.count():
            raise ValueError('待配置角色还未确认；完成第二步或明确取消待配置列表后检查。')
        request = self.request()
        self._pending_precheck = None
        self._start_plan_job(
            'precheck',
            dict(
                profile_id=request.profile_id, request=request, snapshot=self.runtime.weights.active
            ),
        )
        self.precheck_hint.setText('后台检查完整候选池；旧统计仅作历史参考，可取消。')

    def precheck_fresh(self):
        if self._precheck is None or self.runtime._closed:
            return False
        try:
            profile, revision, version, request_version, report = self._precheck
            return (
                profile == self.profile.currentData()
                and revision == self.inventory_revision()
                and version == self.runtime.weights.active.version
                and request_version == self.request_version()
                and report.get('scope_generation', self._scope_generation) == self._scope_generation
            )
        except (ValueError, KeyError):
            return False

    def publish_precheck(self, value):
        profile, revision, version, request_version, report = value
        pending = self._pending_precheck
        if pending is None or pending['value'] is not value:
            pending = dict(
                value=value,
                generation=self._io_generation,
                profile=self.profile.currentData(),
                cancel=self._io_cancel,
                scope=self._scope_generation,
                scheduled=False,
            )
        if not self._precheck_context_valid(pending):
            if self._pending_precheck is pending:
                self._pending_precheck = None
            logger.info(
                'precheck receipt discarded generation=%s reason=inactive-context',
                pending['generation'],
            )
            return False
        if (
            profile != self.profile.currentData()
            or version != self.runtime.weights.active.version
            or request_version != self.request_version()
            or report.get('scope_generation', self._scope_generation) != self._scope_generation
        ):
            self._pending_precheck = None
            self.precheck_hint.setText('库存、权重或要求已变；未采用旧统计，请重新检查。')
            self.status.setText('角色、权重或候选范围已变化；请重新生成，已保存方案保留。')
            logger.info(
                'precheck receipt discarded generation=%s reason=changed-context',
                pending['generation'],
            )
            return False
        current_revision = self.inventory_revision()
        if current_revision is None:
            first_wait = self._pending_precheck is not pending
            self._pending_precheck = pending
            self.precheck_hint.setText('库存正在读取；保留本次检查回执，读取完成后继续，可取消。')
            self.status.setText('库存正在读取，稍后继续检查；可取消，已保存方案保留。')
            if first_wait:
                logger.info(
                    'precheck receipt waiting generation=%s inventory_revision=%s reason=repository-busy',
                    pending['generation'],
                    revision,
                )
            self._schedule_precheck_retry(pending)
            return None
        self._pending_precheck = None
        if revision != current_revision:
            self.precheck_hint.setText('库存、权重或要求已变；未采用旧统计，请重新检查。')
            self.status.setText('库存已变化；请重新生成，已保存方案保留。')
            logger.info(
                'precheck receipt discarded generation=%s expected_revision=%s actual_revision=%s reason=changed-revision',
                pending['generation'],
                revision,
                current_revision,
            )
            return False
        self._precheck = value
        lines = [f'库存修订 {revision}；仅本档案快照，非整个背包。']
        for row in report['roles']:
            lines.append(
                f'{row["label"]}：收录 {row["total"]} → 已确认有效 {row["confirmed"]} → '
                + ('完整调谐' if report['fully_tuned'] else '调谐不限')
                + f' {row["tuned"]} → 元素/套装 {row["matched"]} → 保护/固定 {row["protected"]}'
            )
            lines.append(
                '槽位候选：'
                + '；'.join(
                    f'槽 {index + 1} COST {cost}：{len(pool)} 件'
                    for index, (cost, pool) in enumerate(zip(row['costs'], row['slot_ids']))
                )
            )
            empty = [str(cost) for cost, pool in zip(row['costs'], row['slot_ids']) if not pool]
            if empty:
                lines.append(
                    '空槽 COST ' + '/'.join(empty) + '：修改装备要求，或继续收录这些 COST 的候选。'
                )
            for name, minimum, count in row['sets']:
                if count < minimum:
                    lines.append(
                        f'{name} 候选 {count} / 要求至少 {minimum}：修改要求或继续收录；计数仍未考虑各槽交集。'
                    )
        if report['pending']:
            lines.append(f'快照中有 {report["pending"]} 件记录待核对，可到“我的声骸”处理。')
        if report['missing_pins']:
            lines.append('固定件不在当前快照：到第二步明确选择其他实例或取消固定，原 ID 保留。')
        lines.append('计数不证明名称去重、套装数量、独占或目标可达；生成时重新读取并固定搜索快照。')
        self.precheck_text.setPlainText('\n'.join(lines))
        self.review_button.setText(
            f'去核对 {report["pending"]} 件' if report['pending'] else '查看我的声骸'
        )
        self.precheck_hint.setText(
            '检查完成：查看筛选链后可生成；空池可先去核对、改要求或继续收录。'
        )
        self.status.setText('候选检查完成；尚未搜索或装备，不承诺配齐/达标。')
        logger.info(
            'precheck receipt accepted generation=%s inventory_revision=%s',
            pending['generation'],
            revision,
        )
        return True

    def _precheck_context_valid(self, pending):
        """A delayed GUI receipt belongs to exactly one still-live IO request."""
        return (
            not self._closing
            and not self.runtime._closed
            and not self.task._destroyed
            and self._editor is None
            and self._future is None
            and self._io_future is None
            and pending['generation'] == self._io_generation
            and pending['profile'] == self.profile.currentData()
            and pending['cancel'] is self._io_cancel
            and not pending['cancel'].is_set()
            and pending['scope'] == self._scope_generation
        )

    def _schedule_precheck_retry(self, pending):
        if pending['scheduled']:
            return
        pending['scheduled'] = True
        # Context ownership prevents delivery after QObject deletion. No DB
        # wait or worker submission occurs on the GUI thread during contention.
        QTimer.singleShot(25, self, lambda: self._retry_precheck(pending))

    def _retry_precheck(self, pending):
        pending['scheduled'] = False
        if self._pending_precheck is not pending:
            return
        if not self._precheck_context_valid(pending):
            self._pending_precheck = None
            if hasattr(self, '_generate_after_check'):
                self._generate_after_check = False
            logger.info(
                'precheck delayed receipt discarded generation=%s reason=inactive-context',
                pending['generation'],
            )
            return
        self.action(lambda: self.publish_precheck(pending['value']))
        self.refresh()

    def generate_from_precheck(self):
        if self.incoming.count():
            raise ValueError('还有角色待配置；完成第二步或明确取消待配置列表后生成。')
        if not self.precheck_fresh():
            self.start_precheck()
            return
        if self._precheck[4]['missing_pins']:
            raise ValueError('固定件缺失；先回第二步修改固定选择，再检查。')
        self.start()

        # Navigation never starts capture on the user's behalf.

    def refresh_allocation_ux(self, busy):
        self.check_button.setEnabled(not busy)
        if busy:
            return
        fresh = self.precheck_fresh()
        self.solve_button.setText('生成方案' if fresh else '检查后生成方案')
        if self._precheck is not None and not fresh:
            self.precheck_hint.setText('库存、权重或要求已变；上次统计仅供参考，请重新检查。')

    def show_outcome(self, result):
        lines = []
        limited = not result['exhaustive']
        for row in result['roles']:
            total, target = row['total_cents'] / 100, row['target_cents'] / 100
            count = len(row['item_ids'])
            state = (
                '未配齐'
                if count != 5
                else '未启用目标分'
                if not target
                else '达到目标'
                if total >= target
                else '合法方案，未达目标'
            )
            gap = max(0, target - total)
            score_text = (
                f'{total:.2f} 分' if not target else f'{total:.2f}/{target:.2f} 分 · 差距 {gap:.2f}'
            )
            lines.append(f'{row["label"]}：{state} · {count}/5 件 · {score_text}')
        if limited:
            lines.append('搜索已到上限，当前方案未保证最优。')
        else:
            lines.append(
                '各优先级组在前组结果固定后已完成搜索；不是全角色公平最优。'
                if result.get('request', {}).get('priority_groups')
                else '已完成本次候选范围内的搜索。'
            )
        lines.append('保存不等于已装备，请在游戏中手动装备。')
        self.outcome.setPlainText('\n'.join(lines))
        if hasattr(self, 'outcome_label'):
            self.outcome_label.setText(lines[-2])
            self.outcome_label.setToolTip('\n'.join(lines))
            self.outcome_label.setVisible(True)
        self.precheck_panel.toggle.setChecked(False)
        self.steps.setCurrentIndex(2)
