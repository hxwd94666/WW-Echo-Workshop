# 许可与第三方来源

本项目是独立维护、已修改的声骸工具；重构于2026-10-15开始。许可、版权与资料来源集中保留在本文件，日常界面不展示历史开发流水账。

## 项目代码与最初实现

项目代码使用 GNU GPL v3，完整文本见 `LICENSE`（插件归档内为 `LICENSE.txt`）。最初插件评分/OCR实现来源于 https://github.com/IceHe/okww-xwuid-echo-score 。本项目已修改其结构与维护方式，不表示原维护者仍负责当前版本。

宿主是 https://github.com/ok-oldking/ok-wuthering-waves 和 OK Script；宿主代码未包含在本插件归档中，使用者另行取得并遵守其许可。

## 评分与游戏资料

声明式权重、评级表、套装资料与部分展示资源来自 XutheringWavesUID 及其资源目录：

- https://github.com/Loping151/XutheringWavesUID
- https://cnb.cool/loping151/XutheringWavesUID-Resources

内置资源及派生版本记录见 `src/echo_workshop/resources/builtin/sources.json`。在线资料另存来源URL、时间、内容hash与schema；HTTP快照不冒称固定Git提交。

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
