# 实时评分透明悬浮窗 PoC 方案

> 状态：PoC 已实现；透明/置顶/穿透/编辑/拖拽/目标分输入/位置保存已接入，真实游戏显示待验收。
>
> 目标：用最小改动验证透明、置顶、鼠标穿透、编辑模式和窗口内输入在 OKWW 里是否可行；确认效果后再做正式架构。

## 1. 先验证什么

只验证四件事：

1. 无边框透明窗口能否覆盖在游戏上；
2. 锁定模式下鼠标能否穿透到游戏；
3. 快捷键能否切到编辑模式并拖动窗口；
4. 窗口内控件能否点击、输入并写回配置。

暂不处理：

- 完整工作台迁移；
- 复杂布局；
- 多语言；
- 多显示器高级适配；
- 与旧 GDI 浮层的长期共存。

## 2. PoC 形态

~~~text
锁定模式
  - 透明无边框
  - 始终置顶
  - 鼠标穿透
  - 不抢焦点
  - 显示实时策略文字

编辑模式
  - 显示细边框/拖拽条
  - 鼠标可交互
  - 可拖动
  - 可点击按钮
  - 可输入目标分
~~~

默认启动为锁定模式。

## 3. 快捷键

新增动作：

~~~text
policy_hud_edit
~~~

默认：

~~~text
Ctrl+Alt+L
~~~

行为：

- 锁定 → 编辑；
- 编辑 → 锁定；
- 只在游戏前台响应；
- 不依赖 OCR。

如果快捷键不可用，PoC 先提供一个“编辑”按钮作为兜底。

## 4. 数据来源

PoC 不重新计算策略，只复用现有实时摘要。

在运行时增加一个极轻的 HUD 状态：

~~~python
class HudState:
    text: str
    colours: tuple
    updated_at: float
~~~

浮层任务在现有 live_summary() 之后写入：

~~~python
runtime.hud_state.update(summary, summary_colours)
~~~

HUD 窗口每 100 ms 读取一次，不直接参与 OCR 或策略计算。

## 5. PoC 窗口

新增：

F:\\WW\\WW-Echo-Workshop\\src\\ww_echo_workshop\\ui\\workspace\\hud.py

最小实现：

- QWidget
- Qt.FramelessWindowHint
- Qt.WindowStaysOnTopHint
- Qt.Tool
- WA_TranslucentBackground
- WA_NoSystemBackground

### 锁定模式

- Qt.WindowTransparentForInput
- Qt.WindowDoesNotAcceptFocus
- 不显示边框
- 不接收鼠标和键盘

### 编辑模式

- 恢复鼠标输入；
- 显示顶部拖拽条；
- 允许点击控件；
- 允许输入目标分；
- 位置变化写入配置。

## 6. 最小控件

PoC 只放三项：

1. 复选框：显示实时评分；
2. 目标分输入框；
3. 按钮：打开工作台。

再加一个状态文字：

~~~text
实时评分：开 / 关
模式：锁定 / 编辑
~~~

不放入 13 项权重、角色预设、导入导出。

## 7. 显示开关

新增配置：

~~~text
策略浮层模式 = gdi | qt
~~~

PoC 默认仍为 gdi，只有手动切到 qt 才启用悬浮窗。

这样验证失败时不会污染现有浮层。

## 8. 与现有 GDI 浮层的关系

PoC 阶段：

- GDI 浮层保持原样；
- Qt 悬浮窗作为可选实验；
- 两者不同时显示；
- 选择 qt 时隐藏 GDI 策略摘要；
- 选择 gdi 时关闭 Qt 窗口。

正式方案再决定是否完全替换 GDI。

## 9. 位置保存

配置：

~~~text
策略悬浮窗位置 = {"x": 1200, "y": 300, "screen": "..."}
~~~

规则：

- 编辑模式拖动后保存；
- 恢复时检查屏幕范围；
- 越界回退到默认位置；
- 缩放/分辨率变化时先按屏幕比例夹取。

## 10. 风险点

### 鼠标穿透

需要验证：

- WindowTransparentForInput 在 Windows 上是否完全穿透；
- 是否仍会抢焦点；
- 游戏窗口是否还能收到点击。

### 置顶层级

需要验证：

- Qt HUD 与宿主 overlay 谁在上；
- 是否需要周期 raise_()；
- 全屏/无边框模式是否显示。

### 输入焦点

编辑模式会抢游戏焦点，必须在非战斗或暂停时使用。

### 性能

PoC 使用 100 ms 轮询；文字不变不重绘。

## 11. 文件清单

### 新增

- F:\\WW\\WW-Echo-Workshop\\src\\ww_echo_workshop\\ui\\workspace\\hud.py
- F:\\WW\\WW-Echo-Workshop\\src\\ww_echo_workshop\\application\\hud_state.py

### 修改

- F:\\WW\\WW-Echo-Workshop\\src\\ww_echo_workshop\\adapters\\okww\\overlay_task.py
  - 发布 HUD 状态；
  - 根据策略浮层模式隐藏 GDI 摘要。
- F:\\WW\\WW-Echo-Workshop\\src\\ww_echo_workshop\\adapters\\okww\\settings_task.py
  - 新增策略浮层模式、策略悬浮窗位置。
- F:\\WW\\WW-Echo-Workshop\\src\\ww_echo_workshop\\adapters\\okww\\capture_commands.py
  - 新增 policy_hud_edit 动作。
- F:\\WW\\WW-Echo-Workshop\\src\\ww_echo_workshop\\core\\hotkeys.py
  - 注册 policy_hud_edit。
- F:\\WW\\WW-Echo-Workshop\\src\\ww_echo_workshop\\ui\\workspace\\live_card.py
  - 增加“使用透明悬浮窗（实验）”开关。

## 12. PoC 验收

- [ ] 游戏窗口上能看到透明 HUD；
- [ ] 锁定模式下点击穿透，不抢焦点；
- [ ] 按 Ctrl+Alt+L 进入编辑模式；
- [ ] 编辑模式可拖动；
- [ ] 目标分输入可写回配置；
- [ ] 再按快捷键恢复锁定；
- [ ] 重启后位置恢复；
- [ ] 关闭实时评分后 HUD 隐藏；
- [ ] 不影响宿主 OCR。

## 13. 回滚

- 策略浮层模式保持默认 gdi；
- 删除 hud.py 和 hud_state.py；
- 移除快捷键动作；
- 不改玩家库存、配置和宿主文件。

## 14. 实施顺序

1. 建 HUD 窗口，写死显示一段测试文字；
2. 验证透明、置顶、点击穿透；
3. 加编辑模式开关和拖拽；
4. 接 hud_state 实时文字；
5. 加目标分输入；
6. 位置持久化；
7. 用开关切 gdi/qt，验收后再做正式方案。
