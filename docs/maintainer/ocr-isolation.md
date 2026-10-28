# 插件独立 OCR 引擎方案

> 状态：P1/P2/P3 已实现；私有 CPU OpenVINO 引擎、路由、可见开关、空闲卸载和实时评分关闭停用已落地，真实战斗验收待补。
>
> 目标：让插件 OCR 与 OKWW 宿主 OCR 完全解耦，不再共享 ONNX/OpenVINO/NPU 引擎；关闭实时评分后插件不启动后台 OCR，但保留玩家手动收录/扫描。

## 1. 问题背景

宿主当前使用：

- `ok.task.Task.ocr()`
- `TaskExecutor.ocr_lib()`
- `onnxocr`，并启用 OpenVINO/NPU

插件当前通过 `overlay_task.ocr_fun()` 调用宿主同一个 `executor.ocr_lib('default')`，插件只加了自身 `OCR_LOCK`，宿主 OCR 没有同一把锁。两边并发推理同一个引擎时，宿主战斗 OCR 可能返回空结果或超时，表现为只普攻、不识别技能。

该问题不能靠“插件检测到宿主 OCR 后再暂停”彻底解决：

- 检测与调用之间存在竞态；
- 插件推理已开始时，宿主可能已经开始；
- 宿主 OCR 入口没有共享锁或 busy 信号。

## 2. 设计原则

1. **不修改宿主源码。**
2. **不包装、不替换宿主 `executor.ocr_lib`。**
3. **插件不调用宿主 OCR 引擎。**
4. **插件使用独立模型实例，强制 CPU，不使用 OpenVINO/NPU。**
5. **宿主 OCR 永远优先；插件 OCR 失败只影响插件。**
6. **关闭实时评分后，插件不启动后台 OCR。**
7. **手动收录/扫描仍由玩家显式触发，使用插件私有 OCR。**

## 3. 总体架构

```text
宿主战斗 OCR
  -> 宿主自己的 onnxocr / OpenVINO / NPU
  -> 不受插件影响

插件 OCR
  -> PluginOcrService
  -> 独立 ONNXPaddleOcr 实例
  -> 强制 CPU
  -> 插件内部串行队列
```

插件仍复用宿主 `Task.ocr()` 的外层处理：

- 按 `Box` 裁剪；
- 图像缩放；
- `Box` 坐标构造；
- 文本匹配；
- 调试框绘制；
- `fix_texts`。

但实际推理改为插件私有函数：

```text
Task.ocr()
  -> 插件 override 的 ocr_fun()
  -> PluginOcrService.infer()
  -> 私有 CPU OCR 引擎
```

宿主自己的 `task.ocr()` 和 `executor.ocr_lib()` 完全不动。

## 4. 新增私有 OCR 服务

### 文件

`F:\WW\WW-Echo-Workshop\src\ww_echo_workshop\adapters\okww\private_ocr.py`

### 职责

- 惰性加载 `onnxocr.onnx_paddleocr.ONNXPaddleOcr`；
- 强制参数：
  - `use_openvino=True`，只选 CPU 设备
  - `use_npu=False`
- 单实例；
- 单线程推理；
- 提供与宿主 `onnx_ocr` 相同的返回结构：
  - `detected_boxes`
  - `ocr_boxes`
- 加载失败、推理失败时返回空结果并记录一次日志，不抛出到宿主任务循环；
- 记录：
  - 引擎加载耗时；
  - 调用次数；
  - 最近失败原因；
  - 是否启用。

### 接口

```python
class PluginOcrService:
    def available(self) -> bool: ...
    def infer(self, box, image, match, scale_factor, threshold, lib) -> tuple: ...
    def close(self) -> None: ...
```

`infer()` 内部执行：

1. 私有引擎推理；
2. 按宿主 `onnx_ocr` 的格式解析 `result`；
3. 用宿主 `self.get_box(...)` 构造 `Box`；
4. 应用 `fix_texts` 和 `find_boxes_by_name`；
5. 返回 `(detected_boxes, ocr_boxes)`。

## 5. 插件 OCR 路由改造

### 文件

`F:\WW\WW-Echo-Workshop\src\ww_echo_workshop\adapters\okww\overlay_task.py`

修改 `ocr_fun()`：

- 不再调用 `super().ocr_fun(lib)`；
- 返回插件私有推理函数；
- 不再使用只覆盖插件的 `OCR_LOCK`；
- 私有 OCR 不可用时返回空结果；
- 不把私有 OCR 请求转交宿主引擎。

### 文件

`F:\WW\WW-Echo-Workshop\src\ww_echo_workshop\adapters\okww\scan_observation.py`

`F:\WW\WW-Echo-Workshop\src\ww_echo_workshop\capture\scan\pipeline.py`

- 扫描器复制 parser 时使用私有 OCR；
- 移除对插件 `OCR_LOCK` 的依赖；
- 扫描 OCR 不再穿透到宿主 `executor.ocr_lib`。

### 文件

`F:\WW\WW-Echo-Workshop\src\ww_echo_workshop\adapters\okww\capture_commands.py`

- 移除宿主 `OCR_LOCK` 直接引用；
- 手动收录快捷键使用私有 OCR；
- 宿主 OCR 无感知。

## 6. 生命周期与实时评分开关

### 文件

`F:\WW\WW-Echo-Workshop\src\ww_echo_workshop\adapters\okww\settings_task.py`

`F:\WW\WW-Echo-Workshop\src\ww_echo_workshop\adapters\okww\assistant_ui.py`

新增：

- `使用独立OCR引擎`，默认开启；
- 私有 OCR 惰性加载；
- 关闭实时评分后：
  - 停止浮层后台 OCR；
  - 停止自动扫描 OCR；
  - 不主动加载私有引擎；
- 手动收录/扫描：
  - 仍允许；
  - 仅在玩家显式操作时加载和调用；
- 空闲 TTL 后释放私有引擎，降低内存占用；
- 插件关闭时释放。

### 判定规则

```text
实时评分开启 + 声骸页面
  -> 允许后台私有 OCR

实时评分关闭
  -> 禁止后台 OCR
  -> 自动扫描停止
  -> 手动收录/扫描仍可显式 OCR
```

手动 OCR 也使用私有引擎，不调用宿主 OCR。

## 7. 失败保护

- `onnxocr` 不存在、模型加载失败、CPU 推理异常：
  - 只停用插件 OCR；
  - 记录一次明确错误；
  - 宿主 OCR 不受影响；
  - 不回退到宿主引擎。
- 私有 OCR 单次失败：
  - 返回空结果；
  - 不破坏宿主任务；
  - 下一帧可重试。
- 不自动下载模型，不修改宿主配置。

## 8. 测试计划

### 新增

`F:\WW\WW-Echo-Workshop\tests\contracts\test_private_ocr.py`

覆盖：

1. 私有 OCR 不调用宿主 `executor.ocr_lib`；
2. 宿主 OCR 入口被替换为“调用即报错”时，插件私有 OCR 仍工作；
3. 插件 OCR 并发调用串行；
4. 私有引擎加载失败时返回空结果，宿主不受影响；
5. 关闭实时评分后，插件后台 OCR 调用次数为 0；
6. 手动收录/扫描仍可显式调用私有 OCR；
7. `Box`、匹配、阈值结果与改造前样例一致。

### 现有回归

- `tests.contracts.test_live_policy`
- `tests.contracts.test_live_snapshot`
- `tests.contracts.test_policy_ui_state`
- 工作台离屏构建；
- 原生包校验；
- 真实 OKWW 战斗冒烟：
  - 实时评分开启；
  - 实时评分关闭；
  - 两者分别运行自动战斗。

## 9. 风险

| 风险 | 说明 | 缓解 |
|---|---|---|
| CPU 占用 | 私有引擎使用 CPU | 单线程、低优先级、空闲卸载 |
| 内存 | 多一份模型 | 惰性加载 + TTL |
| 精度差异 | CPU 与宿主 NPU 模型可能不同 | 样例逐字对比后再启用 |
| 依赖缺失 | 宿主环境无 `onnxocr` | 停用插件 OCR，不影响宿主 |
| 启动变慢 | 首次加载模型 | 后台加载，不阻塞宿主 |
| 宿主升级 | `onnxocr` API 变化 | 失败关闭插件 OCR |

### 不再存在的风险

- 共享宿主 OCR 引擎；
- 包装宿主 `ocr_lib`；
- 插件 OCR 破坏宿主战斗识别；
- 宿主新增 OCR 功能导致插件冲突。

## 10. 实施阶段

### P1：私有引擎

- 新增 `private_ocr.py`；
- 独立 CPU 引擎加载与推理；
- 不改 `ocr_fun` 行为，先做契约测试。

### P2：路由切换

- `ocr_fun` 改为私有推理；
- 扫描、手动收录切换到私有 OCR；
- 移除插件对宿主 OCR 入口的调用。

### P3：生命周期与开关

- 实时评分关闭后停止后台 OCR；
- 手动 OCR 保留；
- 自动扫描停止；
- TTL 释放。

### P4：真实游戏回归

- 宿主战斗 OCR；
- 插件实时评分；
- 手动收录/扫描；
- 高精度自动战斗。

## 11. 验收标准

- [ ] 插件 OCR 不调用宿主 `ocr_lib`；
- [ ] 宿主 OCR 在插件开启/关闭实时评分时均正常；
- [ ] 关闭实时评分后插件后台 OCR 为 0；
- [ ] 手动收录/扫描仍可用；
- [ ] 私有 OCR 失败时宿主无感；
- [ ] 样例 OCR 结果与改造前一致；
- [ ] 真实战斗技能识别恢复；
- [ ] 宿主源码无修改、无 monkeypatch。

## 12. 回滚

- 关闭 `使用独立OCR引擎`；
- 恢复原 `ocr_fun`；
- 删除私有 OCR 实例；
- 不修改玩家库存、角色配置、宿主文件。
