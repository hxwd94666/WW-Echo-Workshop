# 精简配装常规状态展示并保留需要处理的提示。
import re

from echo_workshop.ui.widgets.workspace import BriefStatus


_ROUTINE_PREFIXES = (
    '添加/编辑角色约束后生成方案',
    '后台搜索中；',
    '后台检查完整候选池；',
    '后台准备角色固定实例；',
    '后台方案作业：',
    '候选检查完成；尚未搜索',
    '搜索完成：',
    '历史列表已校验：',
    '约束已保存；',
    '装备要求已保存',
    '配装已保存，声骸装备者已更新。',
)
_PROGRESS = re.compile(
    r'^(?:准备完整固定实例候选|逐项校验历史方案|更新配装保护|校验并保护配装|'
    r'读取与校验方案|校验并保存方案|校验并保存手动进度)：\d+/\d+；'
    r'可取消，未完成时保留原清单$'
)
_SUCCESS = re.compile(r'^已(?:保存|备份) \d+ 个角色配装(?:并更新声骸装备者。|，未更改装备者。)$')


class AllocationNotice(BriefStatus):
    def clear(self):
        self.setText('')

    def setText(self, text):
        super().setText(text)
        page = self.parentWidget()
        self.setVisible(
            self.has_notice()
            and not getattr(page, '_saved_view', False)
            and getattr(page, '_editor', None) is None
        )

    def has_notice(self):
        text = getattr(self, 'full_text', self.text()).strip()
        return bool(text) and not (
            text.startswith(_ROUTINE_PREFIXES)
            or _PROGRESS.fullmatch(text)
            or _SUCCESS.fullmatch(text)
        )
