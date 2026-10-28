# 为匹配和配装共用指定角色模版的权重编辑入口。
def open_template_weights(task, key):
    if task is None or getattr(task, '_panel', None) is None:
        raise ValueError('请在声骸工坊中编辑模版权重。')
    if key is None:
        raise ValueError('请先选择角色与模版。')
    snapshot = task.runtime.weights.active
    if key[0] not in snapshot.templates or key[1] not in snapshot.templates[key[0]]:
        raise ValueError('该角色的权重已失效，请更新资料后重试。')
    panel = task._panel
    panel._populate_settings('scoring')
    editor = panel.weights.ensure_custom_editor()
    if editor.pending:
        raise ValueError('权重正在保存，请完成后再管理其他模版。')
    if editor.selection != key:
        editor.refresh()
        character = editor.characters.findData(key[0])
        if character < 0:
            raise ValueError('该角色的权重已失效，请更新资料后重试。')
        editor.characters.setCurrentIndex(character)
        variant = editor.variants.findData(key[1])
        if variant < 0:
            raise ValueError('该模版的权重已失效，请更新资料后重试。')
        editor.variants.setCurrentIndex(variant)
    # 设置页暂时覆盖当前工作区，原角色要求草稿仍留在原编辑器中。
    panel.open_settings_section('weights')
