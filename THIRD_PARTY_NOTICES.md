# 许可与第三方来源

本项目是独立维护、已修改的声骸工具；重构于2026-10-15开始。许可、版权与资料来源集中保留在本文件，日常界面不展示历史开发流水账。

## 项目代码与最初实现

项目代码使用 GNU GPL v3，完整文本见 `LICENSE`（插件归档内为 `LICENSE.txt`）。最初插件评分/OCR实现来源于 https://github.com/IceHe/okww-xwuid-echo-score 。本项目已修改其结构与维护方式，不表示原维护者仍负责当前版本。

宿主是 https://github.com/ok-oldking/ok-wuthering-waves 和 OK Script；宿主代码未包含在本插件归档中，使用者另行取得并遵守其许可。

## Echo Studio 策略内核

强化/重抽策略求解使用 vendored 的 `echo_policy` crate：

- 项目：Echo Studio / echo-policy，https://github.com/2-07665/echo-policy
- 组件：`echo_policy` crate，固定副本位于 `src/ww_echo_workshop/native/compute/vendor/echo_policy/`，来源提交 `72ec29f7346640ba3d660eb9fb48dc82bfa0d9b9`。
- 许可：MIT，Copyright (c) 2025 2-07665；完整文本见该目录 `LICENSE`。
- 用途：强化/重抽策略的状态、概率档位与资源成本模型；本项目仅做 XWUID 评分适配和界面，不复制其 Web/Tauri/服务端/OCR 代码。

## 五套策略评分起始数据

随包 `src/ww_echo_workshop/resources/policy/echo-data.json` 为起始快照，来源为 Echo Studio 计算器：

- 数据版本：`2026.10.19.1`
- 来源：`https://github.com/2-07665/ww-echo-policy-calculator/blob/main/data/echo-data.json`
- 文件 SHA-256：`8c809bdab2077d394724df44f508eccfccf4c44ef7738c50945ab7af88130bcd`
- 更新策略：上游已停止维护；本插件不自动更新该快照，玩家可通过代理地址或手动导入替换。

五套评分的来源与许可状态：

- XWUID / qq_bot：GPL-3.0，来源 `IceHe/okww-xwuid-echo-score`，保留上游 `LICENSE-XWUID`。
- Wuwa Echo Tool：当前源码参考 `GQin404/WuwaEchoTool`（历史快照来自 `2-07665/WuwaEchoTool`）；权重内嵌 `js/base.js`，没有独立数据仓库/release。上游快照未发现 license 文件，本包仅作起始数据并保留署名，不主张额外再分发授权。
- 漂泊者强化助手（mc_boost_assistant）：来源为微信小程序，无公开仓库/数据 API；本包使用上游转存快照。
- linear / fixed：通用手动评分默认值，无独立第三方数据源。

如相关权利人提出异议，应移除或替换对应预设；玩家导入/代理数据由玩家自行确认来源。

## 评分与游戏资料

声明式权重、评级表、套装资料与部分展示资源来自 XutheringWavesUID 及其资源目录：

- https://github.com/Loping151/XutheringWavesUID
- https://cnb.cool/loping151/XutheringWavesUID-Resources

内置资源及派生版本记录见 `src/ww_echo_workshop/resources/builtin/sources.json`。在线资料另存来源URL、时间、内容hash与schema；HTTP快照不冒称固定Git提交。

套装图标随包；角色头像按需获取并缓存。游戏名称、图标和头像的权利属于相应权利人。资料中的描述用于展示，不作为可执行公式或官方推荐。

## 游戏基础资料主备来源

- Nanoka： https://static.nanoka.cc ，以 manifest 的 ww.live 正式服目录为准。
- Encore： https://api-v2.encore.moe ，使用 zh-Hans 角色与声骸列表，不采用已发现关联错位的详情 FetterGroupDetails。
- 来源发现参考： https://gitee.com/Xinglingsuiyue/waves-plugin 。未复制其插件或渲染代码。

内置游戏目录、套装图标与效果通过独立适配器生成；请求 URL、正式服版本、原始 JSON SHA-256、规范化图片 SHA-256 记录于 builtin/game_sources.json。声骸 COST 来自 XWUID 固定提交 register_echo.py 的字面量静态提取，来源和哈希在 builtin/echo_costs.json。运行时不下载或执行上游 Python/二进制。

这些游戏素材与事实资料不因本项目 GPL 许可而变为本项目自有资产；游戏素材权利仍归原权利人。公开下载地址不等于独立素材授权，发行方应继续核实再分发条款并响应权利方要求。

## 离线拼音资料

`pinyin_table.json` 派生自 pypinyin 0.55.0，去音调并将 ü 表示为 v；无需 pypinyin 运行依赖。来源：https://github.com/mozillazg/python-pinyin 。

The MIT License (MIT)

Copyright (c) 2016 mozillazg, 闲耘 <hotoo.cn@gmail.com>

Permission is hereby granted, free of charge, to any person obtaining a copy of
this software and associated documentation files (the "Software"), to deal in
the Software without restriction, including without limitation the rights to
use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of
the Software, and to permit persons to whom the Software is furnished to do so,
subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS
FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR
COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER
IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
