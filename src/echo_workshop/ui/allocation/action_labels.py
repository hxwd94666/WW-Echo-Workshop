# 统一配装生效、仅备份和移除按钮的悬浮说明。
APPLY_TIP = (
    '将选定角色的完整五件方案设为当前配装，更新声骸占用及工坊装备者。'
    '替换旧配装时释放旧占用；不操作游戏中的装备。'
)
BACKUP_TIP = (
    '仅新增一份独立备份，冻结当时的要求、声骸和评分；允许未配齐。'
    '不更改当前配装、声骸占用或工坊装备者。'
)
REMOVE_CURRENT_TIP = (
    '移除此角色的当前配装并释放该配装的占用，不删除声骸或备份。独立手动保护保持不变，不操作游戏。'
)
REMOVE_BACKUP_TIP = (
    '仅从备份列表移除此角色的快照，不改变当前配装、声骸占用或其他角色。'
    '底层快照保留，避免破坏当前配装和历史证据引用。'
)
REPLACE_TIP = (
    '仅替换选中的一个槽位，其他四件固定；应用只生成预览。'
    '备份预览可另存备份；明确“设为当前配装”后才改变占用。'
)


def explain(button, text):
    from qfluentwidgets import ToolTipFilter

    button.setToolTip(text)
    button.installEventFilter(ToolTipFilter(button, showDelay=400))
