# 验证角色搜索选中后模版立即生成，切换模版后 key 正确。
import unittest
from types import SimpleNamespace

try:
    from PySide6.QtWidgets import QApplication

    from ww_echo_workshop.ui.widgets.roles import RolePicker
except ImportError:
    QApplication = None
    RolePicker = None


@unittest.skipIf(RolePicker is None, '当前解释器没有 PySide6')
class RolePickerContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def snapshot(self):
        return SimpleNamespace(
            options={
                '秧秧-通用': ('yangyang', 'default', 1),
                '秧秧-辅助': ('yangyang', 'support', 1),
                '安可-通用': ('anke', 'default', 1),
            },
            templates={
                'yangyang': {'default': {}, 'support': {}},
                'anke': {'default': {}},
            },
        )

    def test_first_selection_loads_templates(self):
        picker = RolePicker(self.snapshot())
        picker.characters.setEditText('秧')
        picker.characters.setCurrentIndex(picker.characters.findData('yangyang'))
        self.app.processEvents()
        self.assertEqual(picker.modals.count(), 2)
        self.assertIsNotNone(picker.key())

    def test_manual_template_switch_updates_key(self):
        picker = RolePicker(self.snapshot())
        picker.characters.setCurrentIndex(picker.characters.findData('yangyang'))
        picker.modals.setCurrentIndex(picker.modals.findData(('yangyang', 'support')))
        self.assertEqual(picker.key(), ('yangyang', 'support'))

    def test_unmatched_text_clears_templates(self):
        picker = RolePicker(self.snapshot())
        picker.characters.setCurrentIndex(picker.characters.findData('yangyang'))
        picker.characters.setEditText('不存在的角色')
        self.assertEqual(picker.modals.count(), 0)
        self.assertIsNone(picker.key())
