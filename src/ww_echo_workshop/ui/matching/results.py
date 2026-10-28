# 同步匹配角色选择并投影已计算卡片。
class ComparisonUX:
    def sync_participants(self):
        previous = self.participants.currentData()
        selected = self.selections()
        self.participants.blockSignals(True)
        self.participants.clear()
        available = tuple(
            (character, modal)
            for character, modal, _ in self.runtime.weights.active.options.values()
        )
        for key in dict.fromkeys((*selected, *available)):
            try:
                label = self.runtime.weights.active.template(*key)['name']
            except (KeyError, ValueError):
                label = '失效选择：' + '/'.join(key)
            self.participants.addItem(label, key)
        current = previous if previous in selected else (selected[0] if selected else None)
        self.participants.setCurrentIndex(self.participants.findData(current) if current else -1)
        self.participants.blockSignals(False)
        self.state.current_role = current
        self.load_rule()
        self.project_result()

    def participant_changed(self, *_):
        index = self.participants.currentIndex()
        if index < 0 or self.participants.currentText() != self.participants.itemText(index):
            return
        self.state.current_role = self.participants.currentData()
        self.load_rule()
        self.project_result()

    def choose_participant(self, index):
        key = self.participants.itemData(index)
        if key is None:
            return
        if self._rule_dirty:
            raise ValueError('请先修正当前角色要求的错误，再更换参与角色。')
        self.runtime.weights.active.template(*key)
        self.state.roles = tuple(dict.fromkeys((*self.state.roles, tuple(key))))
        self.state.current_role = tuple(key)
        self.changed()
        self.participants.setCurrentIndex(self.participants.findData(key))

    def apply_role_choice(self):
        self.choose_participant(self.participants.currentIndex())

    def remove_role_choice(self):
        self._remove_role(self.participants.currentData())

    def publish_ux_results(self):
        self.project_result()

    def project_result(self, **kwargs):
        return self.project_auto_result()
