# 三断点一致性配置页实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在宽屏、中等宽度和窄屏保持完全一致的系统配置功能与卡片内容，仅通过排列方向、网格列数、尺寸策略和滚动方式适配屏幕。

**Architecture:** 所有六个 `ConfigGroup` 固定展开并放入配置滚动区；四张既有 `StatusCard` 固定放入状态滚动区；数据统计与快捷操作固定存在于辅助区。`ConfigView.set_responsive_mode()` 只调整 splitter 方向、统计网格列数和车间操作布局，不切换可见卡片、重建控件或干扰状态线程。

**Tech Stack:** Python 3.11、PySide6、QSplitter、QScrollArea、QGridLayout、pytest-qt。

## Global Constraints

- 三种宽度下配置/状态/统计/快捷操作功能入口完全一致；不折叠、不隐藏。
- 不重建配置表单、状态卡、统计卡、快捷操作控件。
- 不修改同步、数据库、MySQL、FTP 或后台线程业务逻辑。
- 左侧导航保持完整文字。
- 不修改或暂存 `MySQL_/sql配置.pptx`。

---

## Task 1: 固定展开的配置区和状态区滚动容器

**Files:**
- Modify: `client/views/config_view.py`
- Modify: `tests/responsive_layout_test.py`

**Interfaces:**
- Produces: `_config_scroll` 始终承载六个展开的 `ConfigGroup`；`_status_scroll` 始终承载四张完整 `StatusCard`。

- [ ] **Step 1: 写失败测试**

创建 ConfigView，断言六个配置组均为展开状态、内容可见；四张状态卡都是状态滚动容器的后代且可见。断言在 `wide → medium → narrow` 切换后，这些对象身份、可见性与可访问性不变。

- [ ] **Step 2: 运行失败测试**

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
C:\Users\Administrator\AppData\Local\Programs\Python\Python311\Python311.exe -m pytest tests/responsive_layout_test.py -k "consistent_content" -v
```

Expected: 当前手风琴/状态标签实现会使部分内容不可见，测试失败。

- [ ] **Step 3: 固定展开配置组**

移除 `ConfigGroup.expanded_changed` 单选逻辑的连接；构建后对六个组调用 `set_expanded(True)`。配置区继续由 `QScrollArea` 垂直滚动，组标题和表单内容全部保留。

- [ ] **Step 4: 还原完整状态卡容器**

移除/停用状态标签栏和 `QStackedWidget` 单卡切换；创建 `_status_scroll` + 内容容器，在其中依次加入概览、MySQL、同步、FTP 四张既有卡。保留 StatusCard 对象和状态更新逻辑。

- [ ] **Step 5: 验证并提交**

```powershell
C:\Users\Administrator\AppData\Local\Programs\Python\Python311\Python311.exe -m pytest tests/responsive_layout_test.py -v
git add client/views/config_view.py tests/responsive_layout_test.py
git commit -m "feat: 保持三断点配置内容一致"
```

## Task 2: 三断点排列、统计网格和车间操作

**Files:**
- Modify: `client/views/config_view.py`
- Modify: `tests/responsive_layout_test.py`

**Interfaces:**
- Consumes: `set_responsive_mode("wide" | "medium" | "narrow")`。
- Produces: 三种模式仅布局不同、内容不变。

- [ ] **Step 1: 写失败测试**

断言：

```python
view.set_responsive_mode("wide")
assert view._horizontal_area.orientation() == Qt.Horizontal
assert view._bottom_stack.orientation() == Qt.Horizontal

view.set_responsive_mode("medium")
assert view._horizontal_area.orientation() == Qt.Vertical
assert view._bottom_stack.orientation() == Qt.Vertical
assert view._stat_grid.columnCount() == 2

view.set_responsive_mode("narrow")
assert view._horizontal_area.orientation() == Qt.Vertical
assert view._bottom_stack.orientation() == Qt.Vertical
assert view._stat_grid.columnCount() == 1
```

同时断言导出、导入、清空队列按钮高度大于 0；窄屏车间控件纵向、不相交。

- [ ] **Step 2: 实现辅助区与车间布局切换**

- wide：主区横向、辅助区横向、统计 2×2、车间横向；
- medium：主区纵向、辅助区纵向、统计 2×2、车间保持横向优先；
- narrow：主区纵向、辅助区纵向、统计单列、车间纵向。

移动既有 mini stat card 到同一个新/重用网格布局时保留对象身份；不改变快捷操作按钮回调。

- [ ] **Step 3: 执行全量验证并提交**

```powershell
$env:PYTHONPATH = "$PWD/client"
$env:QT_QPA_PLATFORM = "offscreen"
C:\Users\Administrator\AppData\Local\Programs\Python\Python311\Python311.exe -m pytest -q
C:\Users\Administrator\AppData\Local\Programs\Python\Python311\Python311.exe -m flake8 client tests --count --select=E9,F63,F7,F82 --show-source --statistics --exclude=__pycache__,.git,build,dist,pyd/_build_c
git diff --check
git add client/views/config_view.py tests/responsive_layout_test.py
git commit -m "feat: 适配三断点一致配置布局"
```

## Task 3: 验证报告

**Files:**
- Create: `docs/reviews/2026-09-30-three-breakpoint-consistent-config-layout-verification.md`

- [ ] **Step 1: 记录实际几何和内容检查**

报告 1280、1000、800 内容宽度下的排列方向、滚动区、状态卡/配置组数量、统计列数、车间布局和按钮可见性。

- [ ] **Step 2: 提交报告**

```powershell
git add docs/reviews/2026-09-30-three-breakpoint-consistent-config-layout-verification.md
git commit -m "docs: 验证三断点一致配置布局"
```
