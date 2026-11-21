# 工作台数据卡：显示权重版本、策略数据来源与更新入口，不执行远程代码。
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLineEdit,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtWidgets import QPushButton
from ww_echo_workshop.ui.widgets.controls import CaptionLabel, CardWidget, SubtitleLabel

from ww_echo_workshop.resources.wuwa_updates import WUWA_BASE_URL
from ww_echo_workshop.core.policy import POLICY_SOURCE
from ww_echo_workshop.ui.widgets.collapsible import CollapsibleSection


class _WrappingCaption(CaptionLabel):
    def __init__(self, text, parent=None):
        super().__init__(text, parent)
        policy = QSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)
        self.setWordWrap(True)

    def minimumSizeHint(self):
        size = super().minimumSizeHint()
        size.setWidth(0)
        return size


class PolicyDataCard(CardWidget):
    def __init__(
        self, parent=None, *, jobs, weight_snapshot, weight_settings, open_weights, policy_changed
    ):
        super().__init__(parent)
        self.jobs = jobs
        self._weight_snapshot = weight_snapshot
        self._weight_settings = weight_settings
        self._policy_changed = policy_changed
        self.weights = None
        self._controls_installed = False
        self._policy_future = None
        self._wuwa_future = None
        self._policy_timer = QTimer(self)
        self._policy_timer.setInterval(100)
        self._policy_timer.timeout.connect(self._poll_policy_update)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(8)
        heading = SubtitleLabel('数据更新', self)
        layout.addWidget(heading)
        self.advanced_section = CollapsibleSection('高级：数据来源与代理更新', self)
        self.advanced_help = _WrappingCaption(
            '日常使用只需要上面的“检查更新”或“导入策略数据”。'
            '这里用于查看数据来源，以及配置国内网络下的代理地址。',
            self,
        )
        self.advanced_section.content_layout.addWidget(self.advanced_help)
        self.weight_version = _WrappingCaption('角色权重：读取中…', self)
        layout.addWidget(self.weight_version)
        self.weight_source = _WrappingCaption('', self)
        self.policy_source = _WrappingCaption('策略数据：' + POLICY_SOURCE, self)
        self.update_scope = _WrappingCaption(
            '更新方式：\n'
            '· XWUID 权重：直接检查更新。\n'
            '· Wuwa 权重：填代理地址后，从 base.js 静态提取，不执行远程 JS。\n'
            '· 策略预设：只有已有 echo-data.json 镜像地址时才用代理更新；'
            '否则用“导入策略数据”。\n'
            '· 漂泊者强化助手 / linear / fixed：没有在线数据源，使用内置或导入。',
            self,
        )
        self.policy_version = _WrappingCaption('策略预设：读取中…', self)
        layout.addWidget(self.policy_version)
        self.limits_note = _WrappingCaption(
            '概率表与资源成本表随插件版本分发，不提供在线更新。',
            self,
        )
        self.source_heading = CaptionLabel('XWUID 权重（直连）', self)
        for widget in (
            self.weight_source,
            self.policy_source,
            self.update_scope,
            self.limits_note,
            self.source_heading,
        ):
            self.advanced_section.content_layout.addWidget(widget)
        self.controls_area = QWidget(self)
        self.controls_layout = QVBoxLayout(self.controls_area)
        self.controls_layout.setContentsMargins(0, 0, 0, 0)
        self.controls_area.hide()
        layout.addWidget(self.controls_area)
        self.install_note = _WrappingCaption('', self)
        layout.addWidget(self.install_note)
        row = QHBoxLayout()
        self.update_button = QPushButton('打开自定义权重', self)
        self.update_button.setToolTip('在设置中编辑角色自定义权重；资料检查仍可在上方直接使用。')
        self.update_button.clicked.connect(open_weights)
        row.addWidget(self.update_button)
        self.policy_button = QPushButton('导入策略数据', self)
        self.policy_button.setToolTip('导入 echo-data.json；校验后原子替换，不执行远程代码。')
        self.policy_button.clicked.connect(self._import_policy_data)
        row.addWidget(self.policy_button)
        row.addStretch()
        layout.addLayout(row)
        self.advanced_section.content_layout.addWidget(
            _WrappingCaption(
                '策略数据代理：只有已有 echo-data.json 镜像地址时才需要填写。',
                self,
            )
        )
        url_row = QHBoxLayout()
        self.policy_url = QLineEdit(self)
        self.policy_url.setPlaceholderText('填入可信的 echo-data.json 代理地址（HTTPS）')
        self.policy_url.setAccessibleName('策略数据地址')
        self.policy_url.setToolTip(
            '只接受 HTTPS；没有上游五体系数据仓库，请填入已生成的数据包地址。'
        )
        url_row.addWidget(self.policy_url, 1)
        self.proxy_button = QPushButton('代理更新', self)
        self.proxy_button.setEnabled(False)
        self.policy_url.textChanged.connect(
            lambda text: self.proxy_button.setEnabled(bool(text.strip()))
        )
        self.proxy_button.clicked.connect(lambda: self._start_policy_update(self.policy_url.text()))
        url_row.addWidget(self.proxy_button)
        self.advanced_section.content_layout.addLayout(url_row)
        self.advanced_section.content_layout.addWidget(
            _WrappingCaption(
                'Wuwa 权重：需要代理下载 base.js 后再静态提取。',
                self,
            )
        )
        self.wuwa_status = _WrappingCaption('Wuwa：未更新', self)
        self.advanced_section.content_layout.addWidget(self.wuwa_status)
        wuwa_row = QHBoxLayout()
        self.wuwa_url = QLineEdit(WUWA_BASE_URL, self)
        self.wuwa_url.setAccessibleName('Wuwa base.js 地址')
        self.wuwa_url.setToolTip('代理地址，只接受 HTTPS；下载后静态解析，不执行远程 JS。')
        wuwa_row.addWidget(self.wuwa_url, 1)
        self.wuwa_button = QPushButton('更新 Wuwa 权重', self)
        self.wuwa_button.clicked.connect(lambda: self._start_wuwa_update(self.wuwa_url.text()))
        wuwa_row.addWidget(self.wuwa_button)
        self.advanced_section.content_layout.addLayout(wuwa_row)
        layout.addWidget(self.advanced_section)
        self.refresh()

    def showEvent(self, event):
        super().showEvent(event)
        if not self._controls_installed:
            QTimer.singleShot(0, self._install_controls)

    def _install_controls(self):
        if self._controls_installed:
            return
        self._controls_installed = True
        try:
            self.weights = self._weight_settings()
        except Exception as error:
            self.install_note.setText('资料更新控件未加载：' + str(error))
            return
        controls = getattr(self.weights, 'resource_controls', None)
        if controls is None:
            return
        parent_layout = getattr(self.weights, 'layout', lambda: None)()
        if parent_layout is not None:
            parent_layout.removeWidget(controls)
        self.controls_layout.addWidget(controls)
        controls.show()
        self.controls_area.show()
        self.refresh()

    def _import_policy_data(self):
        path, _ = QFileDialog.getOpenFileName(self, '导入策略数据包', '', 'JSON 数据包 (*.json)')
        if not path:
            return
        try:
            version = self.jobs.import_file(path)
        except Exception as error:
            self.policy_version.setText('策略预设导入失败：' + str(error))
            return
        self.policy_version.setText(f'策略预设：{version}（已导入）')
        self._policy_changed()

    def _start_policy_update(self, url):
        if self._policy_future is not None or self._wuwa_future is not None:
            return
        self.policy_version.setText('策略预设：正在更新…')
        self.proxy_button.setEnabled(False)
        self.wuwa_button.setEnabled(False)
        self._policy_future = self.jobs.submit_policy(url)
        self._policy_timer.start()

    def _start_wuwa_update(self, url):
        if self._policy_future is not None or self._wuwa_future is not None:
            return
        self.wuwa_status.setText('Wuwa：正在下载并静态提取…')
        self.proxy_button.setEnabled(False)
        self.wuwa_button.setEnabled(False)
        self._wuwa_future = self.jobs.submit_wuwa(url)
        self._policy_timer.start()

    def _poll_policy_update(self):
        busy = False
        if self._policy_future is not None:
            if not self._policy_future.done():
                busy = True
            else:
                future, self._policy_future = self._policy_future, None
                try:
                    final, version = future.result()
                except Exception as error:
                    self.policy_version.setText('策略预设：更新失败：' + str(error))
                else:
                    self.policy_version.setText(f'策略预设：{version}（{final}）')
                    self._policy_changed()
        if self._wuwa_future is not None:
            if not self._wuwa_future.done():
                busy = True
            else:
                future, self._wuwa_future = self._wuwa_future, None
                try:
                    final, version, roles = future.result()
                except Exception as error:
                    self.wuwa_status.setText('Wuwa：更新失败：' + str(error))
                else:
                    self.wuwa_status.setText(f'Wuwa：{version} · {roles} 个角色（{final}）')
                    self._policy_changed()
        if busy:
            return
        self._policy_timer.stop()
        self.proxy_button.setEnabled(bool(self.policy_url.text().strip()))
        self.wuwa_button.setEnabled(bool(self.wuwa_url.text().strip()))

    def closeEvent(self, event):
        self._policy_timer.stop()
        self.stop_worker()
        super().closeEvent(event)

    def stop_worker(self):
        self.jobs.close()

    def refresh(self):
        active = self._weight_snapshot()
        version = str(getattr(active, 'version', '') or '未知')
        source = str(getattr(active, 'source', '') or '未记录来源')
        self.weight_version.setText('角色权重：已就绪')
        self.weight_version.setToolTip(f'版本：{version}\n来源：{source}')
        self.weight_source.setText(
            '来源：XWUID 权重仓库'
            if 'loping151' in source or 'XutheringWavesUID' in source
            else '来源：本机记录'
        )
        self.weight_source.setToolTip(source)
        try:
            pack, source = self.jobs.load_with_source()
        except Exception as error:
            self.policy_version.setText('策略预设：读取失败：' + str(error))
        else:
            if pack is None:
                self.policy_version.setText('策略预设：无可用数据')
                self.policy_source.setText('策略数据：无')
            else:
                label = '内置快照' if source == 'builtin' else '已导入'
                self.policy_version.setText(f'策略预设：已就绪（{label}）')
                self.policy_source.setText(f'策略数据：{label}')
        if self.weights is not None:
            self.weights.refresh()
