# 实时评分 HUD 改造方案

> 状态：P1/P2/P3 已实现，设置区包含角色/模版、评分体系、模式、目标、资源和字号；真实游戏回归待补。
>
> 目标：在现有 PoC 基础上，解决外观、颜色、字体、自动关闭、锁定缩放和设置过少的问题。

## 1. 现状与问题

当前 HUD 已能显示并支持锁定/编辑，但存在五个问题：

1. 外观只是深色圆角框加白色文字，缺少层次；
2. 只有一个 QLabel，无法沿用实时评分的逐行颜色；
3. 离开声骸页面后 HUD 仍然显示；
4. 编辑模式只有实时开关和目标分；
5. 锁定后隐藏了控件，但窗口没有按内容缩小。

## 2. 设计目标

### 2.1 外观

- 深色半透明圆角卡片；
- 顶部细分割线；
- 左侧冰蓝强调条；
- 锁定模式隐藏拖拽条和边框；
- 编辑模式显示边框、拖拽条和设置区；
- 行距、内边距、字体层级统一；
- 窗口不超过屏幕可用区域。

### 2.2 颜色

沿用原实时评分：

- 模版：粉色；
- 当前评分：黄色；
- 理论最高：绿色；
- 策略：冰蓝色。

颜色来自现有 live_summary 返回的 summary_colours，不重新定义一套。

### 2.3 字体

新增配置：

~~~text
策略悬浮窗字号 = 15
~~~

范围：12–32。

编辑模式提供字号调节，实时生效并持久化。

### 2.4 自动关闭

- 浮层离开声骸页面时隐藏 HUD；
- 识别失败时隐藏；
- 关闭实时评分时隐藏；
- 没有数据时不再显示占位窗口；
- 不依赖固定超时，避免静止画面误隐藏。

### 2.5 锁定缩回

锁定模式：

- 隐藏编辑区；
- 重新计算布局；
- 按内容调用 adjustSize；
- 保持左上角位置；
- 夹取到屏幕内。

编辑模式再展开设置区。

### 2.6 设置范围

搬入 HUD 的实时/策略设置：

- 显示实时评分；
- 跟随当前装备角色评分；
- 显示强化/重抽建议；
- 评分体系；
- 目标分；
- 策略模式；
- 资源相对价值；
- 字号。

保留在工作台：

- 测试声骸；
- 候选输入；
- 自定义预设；
- 数据更新；
- 收录/匹配/配装。

## 3. 架构调整

### 3.1 HudState

文件：

F:\\WW\\WW-Echo-Workshop\\src\\ww_echo_workshop\\application\\hud_state.py

从纯文本改为行快照：

~~~text
HudLine:
  text: str
  colour: tuple[int, int, int] | None

HudState:
  rows: tuple[HudLine, ...]
  visible: bool
  updated_at: float
~~~

提供：

- update(rows)
- hide()
- snapshot()

不再把颜色和文本分离成容易错位的两个字段。

### 3.2 HudWindow

文件：

F:\\WW\\WW-Echo-Workshop\\src\\ww_echo_workshop\\ui\\workspace\\hud.py

拆分：

- HudWindow：窗口、锁定/编辑、拖动、缩放；
- HudLineView：逐行彩色显示；
- HudSettingsPanel：编辑模式设置区。

#### 渲染

- 每行一个 QLabel；
- 使用 rows 中颜色；
- 字号来自配置；
- 文本变化时只更新变化行，避免重建全部控件；
- 最多显示策略详情的前 9 行，超出截断。

#### 锁定

- 隐藏设置区和拖拽条；
- 显示细边框或仅保留卡片；
- layout.activate()
- adjustSize()
- 夹取屏幕范围。

#### 编辑

- 显示拖拽条、设置区；
- 关闭鼠标穿透；
- 允许拖动和输入；
- 高度超过屏幕时使用滚动区。

### 3.3 HudController

- 只在 mode == qt 且 live 开启且 state.visible 时显示；
- state.visible == false 时立即隐藏；
- 不再显示占位文字；
- 设置变化后刷新；
- 页面关闭时停止定时器并销毁窗口。

### 3.4 overlay_task

文件：

F:\\WW\\WW-Echo-Workshop\\src\\ww_echo_workshop\\adapters\\okww\\overlay_task.py

调整数据发布顺序：

1. 计算 summary 和 summary_colours；
2. 发布到 hud_state；
3. 如果模式为 qt，清空 GDI 摘要；
4. 如果离开声骸页面/识别失败，调用 hud_state.hide()；
5. 关闭实时评分时也调用 hide()。

注意：

- qt 模式清空 GDI 本身不能触发 hud_state.hide；
- 只有真正的页面退出、识别失败、实时评分关闭才隐藏 HUD。

## 4. 设置项

新增：

| 键 | 默认 | 说明 |
|---|---|---|
| 策略悬浮窗字号 | 15 | 12–32 |
| 策略悬浮窗宽高 | 空 | 可选，保存用户调整尺寸 |
| 策略悬浮窗位置 | 已有 | 保持 |

编辑区布局：

~~~text
显示
  字号
  显示实时评分
  跟随当前装备角色评分
  显示强化/重抽建议

评分
  评分体系
  角色/模版
  使用此角色评分

策略
  目标分
  策略模式
  资源相对价值

[锁定并穿透]
~~~

角色选择使用现有 RolePicker；不复制评分逻辑。

## 5. 文件改动

### 修改

- F:\\WW\\WW-Echo-Workshop\\src\\ww_echo_workshop\\application\\hud_state.py
- F:\\WW\\WW-Echo-Workshop\\src\\ww_echo_workshop\\ui\\workspace\\hud.py
- F:\\WW\\WW-Echo-Workshop\\src\\ww_echo_workshop\\adapters\\okww\\overlay_task.py
- F:\\WW\\WW-Echo-Workshop\\src\\ww_echo_workshop\\adapters\\okww\\settings_task.py
- F:\\WW\\WW-Echo-Workshop\\src\\ww_echo_workshop\\ui\\workspace\\live_card.py

### 测试

- F:\\WW\\WW-Echo-Workshop\\tests\\contracts\\test_hud_state.py
- F:\\WW\\WW-Echo-Workshop\\tests\\contracts\\test_hud_window.py

## 6. 实施阶段

### P1：数据与可见性

- HudLine/HudState 改造；
- overlay 发布和隐藏；
- HUD 不再显示占位。

### P2：视觉、颜色、字号、缩放

- 逐行彩色标签；
- 圆角卡片样式；
- 字号配置；
- 锁定 adjustSize 缩回。

### P3：设置区

- 搬入实时/策略设置；
- 滚动区；
- 角色选择和目标分；
- 设置与工作台双向一致。

### P4：真实游戏回归

- 进入/离开声骸页面；
- 锁定/编辑；
- 字号和颜色；
- 设置修改；
- 重启后位置/字号恢复。

## 7. 测试计划

### 单元/离屏

- HudState 行颜色和 visible；
- HUD 行数与颜色一致；
- 字号改变后标签字号改变；
- lock 后高度小于 edit 后高度；
- state 隐藏后窗口隐藏；
- 位置越界夹取。

### 宿主

- 宿主 Python 下 HUD 测试；
- 工作台离屏构建；
- 原生包校验；
- 真实 OKWW 一轮。

## 8. 风险

| 风险 | 说明 | 缓解 |
|---|---|---|
| 逐行控件开销 | 最多约 9 行 | 只更新变化行 |
| 窗口缩回跳动 | 布局延迟 | activate 后 adjustSize |
| 自动隐藏误判 | 静止画面无更新 | 只在页面退出/失败时 hide |
| 编辑模式抢焦点 | 会打断战斗 | 仅手动触发 |
| DPI 缩放 | 字号和位置偏移 | 保存相对位置，屏幕夹取 |
| 独占全屏 | 窗口不可见 | 文档提示窗口化验证 |

## 9. 验收

- [ ] HUD 外观为统一半透明卡片；
- [ ] 四类文字颜色与旧浮层一致；
- [ ] 字号可调并持久化；
- [ ] 离开声骸界面自动隐藏；
- [ ] 锁定后窗口缩回内容大小；
- [ ] 编辑模式可调实时/策略配置；
- [ ] 重启后位置和字号恢复；
- [ ] 宿主 OCR 不受影响。

## 10. 回滚

- 透明悬浮窗开关关闭，回到 GDI；
- 删除 HUD 状态和窗口新增字段；
- 不影响工作台、OCR、库存和宿主。
