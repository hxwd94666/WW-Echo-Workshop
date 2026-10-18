# 合并已保存配装的库存核验，并在失败后提供就地重试而不反复自动提交。
from PySide6.QtCore import QTimer
from qfluentwidgets import PushButton


class SavedRefresh:
    def build_verification(self, header):
        self._verify_failed = None
        self._verify_requested = None
        self._verify_error = ''
        self._verify_waiting = False
        self._verify_profile = None
        self._verify_timer = QTimer(self)
        self._verify_timer.setSingleShot(True)
        self._verify_timer.timeout.connect(self._start_verification)
        self.recheck_button = PushButton('重新核验', self)
        self.recheck_button.clicked.connect(self.retry_verification)
        self.recheck_button.hide()
        header.addWidget(self.recheck_button)

    def _verification_key(self):
        return self.page.profile.currentData(), self.page.inventory_revision()

    def _poll_verification(self):
        profile, revision = self._verification_key()
        if profile != self._verify_profile:
            self._verify_profile = profile
            self._verify_failed = self._verify_requested = None
            self._verify_error = ''
        self._verify_waiting = revision is None
        if revision is None:
            return
        snapshot = self.page._current_loadouts
        stale = (
            snapshot is None
            or snapshot.profile_id != profile
            or (revision is not None and snapshot.inventory_revision != revision)
        )
        if not stale:
            self._verify_failed = None
            self._verify_error = ''
            return
        key = profile, revision
        if (
            revision is not None
            and key != self._verify_failed
            and not self._busy()
            and self._dialog is None
            and not self._verify_timer.isActive()
        ):
            self._verify_timer.start(0)

    def _start_verification(self):
        if not self._lifecycle_active() or not self.isVisible() or self._busy() or self._dialog:
            return
        with self.page.refresh_projection():
            key = self._verification_key()
            snapshot = self.page._current_loadouts
            if (
                key[1] is None
                or key == self._verify_failed
                or (
                    snapshot is not None
                    and snapshot.profile_id == key[0]
                    and snapshot.inventory_revision == key[1]
                )
            ):
                return
            self._verify_requested = key
            self.page.action(
                lambda: self.page._start_plan_job(
                    'verify-loadouts',
                    dict(profile_id=key[0], saved_recheck=True, recheck_revision=key[1]),
                )
            )

    def retry_verification(self):
        if self._lifecycle_active() and not self._busy() and not self._dialog:
            self._verify_failed = None
            self._verify_error = ''
            self._verify_timer.start(0)
            self.project()

    def publish_verification(self, snapshot, arguments):
        if arguments['profile_id'] != self.page.profile.currentData():
            return
        self._verify_requested = self._verify_failed = None
        self._verify_error = ''
        self.page.publish_current_loadouts(snapshot)
        self.project()

    def verification_failed(self, message, arguments):
        if arguments['profile_id'] != self.page.profile.currentData():
            return
        self._verify_requested = None
        self._verify_failed = (arguments['profile_id'], arguments['recheck_revision'])
        self._verify_error = '核验未完成，原配装保留。' + str(message)[:180]
        self.project()

    def _verification_notice(self):
        failed = self._verify_failed is not None
        self.recheck_button.setVisible(failed)
        self.recheck_button.setEnabled(not self._busy() and self._dialog is None)
        if self._verify_error:
            self.status.setText(self._verify_error)
            self.status.show()
        elif self._verify_requested is not None or self._verify_timer.isActive():
            self.status.setText('库存已更新，正在核验配装…')
            self.status.show()
        elif self._verify_waiting:
            self.status.setText('库存正在处理，请稍后。')
            self.status.show()
        elif self.status.text() == '库存正在处理，请稍后。':
            self.status.clear()
            self.status.hide()
