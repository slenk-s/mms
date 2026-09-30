# 响应式系统配置布局实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在系统配置页实现宽屏三栏、窄屏自动重排、可持久化导航折叠，以及长文本防截断。

**Architecture:** `MainWindow.resizeEvent()` 根据内容宽度选择 `wide` / `medium` / `narrow` 模式，并同步导航和配置页。`NavigationBar` 管理可折叠显示与持久化偏好；`ConfigView` 保持现有配置控件，只移动既有容器而不重建控件。长状态值由统一的省略/Tooltip 辅助函数处理。

**Tech Stack:** Python 3.11、PySide6、Qt Layout、QSplitter、QScrollArea、pytest-qt。

## Global Constraints

- 只改变布局与可读性，不改变同步、数据库、配置保存和权限业务语义。
- 不按窗口宽度缩小正文文字；正文最小 12 px，关键按钮/状态最小 13 px。
- 不修改未跟踪的 `MySQL_/MMS_init.sql`、`MySQL_/sql配置.pptx`。
- 不重建已存在的配置表单控件；布局切换只调整容器、伸缩和可见性。
- 窄屏下重要内容必须可滚动、换行或通过 Tooltip 访问完整文本。

---

## Task 1: 导航折叠状态与持久化

**Files:**
- Modify: `client/widgets/navigation.py`
- Modify: `client/utils/ui_settings.py`
- Create: `tests/responsive_layout_test.py`

**Interfaces:**
- Produces: `NavigationBar.set_collapsed(collapsed: bool)`、`NavigationBar.is_collapsed()`、`save_ui_preference(key, value)`、`restore_ui_preference(key, default)`。

- [ ] **Step 1: 写失败测试**

创建 `QApplication` 夹具，断言折叠后导航宽度降低、每个导航按钮文字隐藏但 Tooltip 包含完整页面名称；恢复后文字再次显示。断言偏好保存后可被读取。

- [ ] **Step 2: 运行失败测试**

```powershell
$env:PYTHONPATH = "$PWD/client"
$env:QT_QPA_PLATFORM = "offscreen"
C:\Users\Administrator\AppData\Local\Programs\Python\Python311\Python311.exe -m pytest tests/responsive_layout_test.py -v
```

Expected: 因折叠/偏好 API 尚不存在而失败。

- [ ] **Step 3: 实现导航折叠**

为导航按钮保存原始翻译文本与图标；新增顶部折叠按钮。折叠时宽度设为 64–72 px、隐藏 logo/user/lib/logout 的文字内容、保留导航图标、设置 Tooltip；展开时恢复 200–240 px 和原内容。使用 `ui_settings` 保存 `navigation_collapsed`。

- [ ] **Step 4: 运行测试并提交**

```powershell
C:\Users\Administrator\AppData\Local\Programs\Python\Python311\Python311.exe -m pytest tests/responsive_layout_test.py -v
git add client/widgets/navigation.py client/utils/ui_settings.py tests/responsive_layout_test.py
git commit -m "feat: 支持导航栏折叠与布局偏好"
```

## Task 2: 配置页响应式容器重排与状态文本防截断

**Files:**
- Modify: `client/views/config_view.py`
- Modify: `tests/responsive_layout_test.py`

**Interfaces:**
- Produces: `ConfigView.set_responsive_mode(mode: str)`；模式仅允许 `wide`、`medium`、`narrow`。

- [ ] **Step 1: 写失败测试**

创建 `ConfigView`，依次调用三种模式并断言：

```python
view.set_responsive_mode("wide")
assert view._horizontal_area.isVisible()
assert view._status_panel.parentWidget() is view._horizontal_area

view.set_responsive_mode("narrow")
assert view._status_panel.parentWidget() is view
assert view._bottom_stack.orientation() == Qt.Vertical
```

为长状态标签设置超长 IP/时间字符串，断言 Tooltip 保存完整文本，显示文本不超过可用宽度。

- [ ] **Step 2: 运行失败测试**

```powershell
C:\Users\Administrator\AppData\Local\Programs\Python\Python311\Python311.exe -m pytest tests/responsive_layout_test.py -v
```

Expected: 因 `set_responsive_mode` 和长文本辅助 API 不存在而失败。

- [ ] **Step 3: 实现可移动容器**

将现有左配置区、右状态区、数据统计、快捷操作包装为可重用容器。宽屏/中等模式使用水平 `QSplitter`；窄屏把状态区加入主垂直布局，数据统计和快捷操作加入垂直 `QSplitter` 或 `QVBoxLayout`。切换前从旧布局 `takeAt()`，切换后插入同一 QWidget，禁止重新构建配置控件。

- [ ] **Step 4: 实现状态值省略与 Tooltip**

为 `StatusCard` 增加 `set_value(row_id, text, wrap=False)`：默认用 `QFontMetrics.elidedText(..., Qt.ElideRight)` 省略并设 Tooltip；错误和诊断文本允许 `wordWrap`。在 `resizeEvent` 或布局变化后重算省略显示。

- [ ] **Step 5: 运行测试并提交**

```powershell
C:\Users\Administrator\AppData\Local\Programs\Python\Python311\Python311.exe -m pytest tests/responsive_layout_test.py -v
git add client/views/config_view.py tests/responsive_layout_test.py
git commit -m "feat: 响应式重排系统配置页面"
```

## Task 3: 主窗口断点分发与 DPI/窗口回归

**Files:**
- Modify: `client/main.py`
- Modify: `tests/responsive_layout_test.py`
- Create: `docs/reviews/2026-09-30-responsive-config-layout-verification.md`

**Interfaces:**
- Produces: `MainWindow._responsive_mode_for_width(width)`；`MainWindow._apply_responsive_layout(width)`。

- [ ] **Step 1: 写失败测试**

断言断点：

```python
assert window._responsive_mode_for_width(1400) == "wide"
assert window._responsive_mode_for_width(1399) == "medium"
assert window._responsive_mode_for_width(1099) == "narrow"
```

窄屏调用后断言导航折叠、配置页为 narrow；宽屏恢复时按用户偏好恢复导航状态。

- [ ] **Step 2: 实现主窗口布局分发**

在 `resizeEvent()` 调用 `_apply_responsive_layout(self.central_widget.width())`。宽度低于 1100 时自动折叠导航并调用 `config_view.set_responsive_mode("narrow")`；1100–1399 使用 medium；1400 以上使用 wide。用户显式导航偏好通过 `ui_settings` 恢复，自动窄屏折叠不覆盖持久化偏好。

- [ ] **Step 3: 执行回归和人工截图检查**

运行 Qt 离屏测试；使用 1920、1366、1280、1024 宽度调用 `resize()`，保存布局截图或读取可见区域，确认没有控件几何宽度为负、重叠或关键文本无 Tooltip。

- [ ] **Step 4: 写验证报告并执行全量检查**

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
C:\Users\Administrator\AppData\Local\Programs\Python\Python311\Python311.exe -m pytest tests/ -v --tb=short --ignore=tests/upload_update.py --ignore=tests/test_update.py
C:\Users\Administrator\AppData\Local\Programs\Python\Python311\Python311.exe -m flake8 client/ --count --select=E9,F63,F7,F82 --show-source --statistics --exclude=__pycache__,.git,build,dist,pyd/_build_c
git diff --check
```

报告必须记录实际测试分辨率、DPI 环境、通过项和无法自动验证的视觉限制。

- [ ] **Step 5: 提交**

```powershell
git add client/main.py tests/responsive_layout_test.py docs/reviews/2026-09-30-responsive-config-layout-verification.md
git commit -m "feat: 适配系统配置页多分辨率布局"
```
