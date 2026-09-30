# 中小宽度系统配置页纵向布局实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在中小宽度下让系统配置页变为清晰的纵向布局：配置区单选展开、状态区标签式切换、统计和快捷操作常驻可用。

**Architecture:** 保留既有配置表单、状态卡、统计和快捷操作 QWidget 实例。配置表单继续使用 `ConfigGroup` 手风琴；状态区新增轻量标签栏和单内容承载容器；统计和快捷操作保留常驻区域。响应式模式只调整 `QSplitter` 方向、容器可见性和网格列数，不重建业务控件或影响后台检测线程。

**Tech Stack:** Python 3.11、PySide6、QSplitter、QScrollArea、QStackedWidget、pytest-qt。

## Global Constraints

- 不修改同步、数据库、MySQL、FTP、权限或配置保存业务逻辑。
- 不重建既有表单、`StatusCard`、统计或快捷操作控件；仅调整容器和可见性。
- 配置区、状态区、辅助区的展开状态分开管理；默认数据库配置、概览、数据统计。
- 中小宽度下状态区、统计和快捷操作必须完全可见且可操作，不得重叠或裁切。
- 不修改或暂存 `MySQL_/sql配置.pptx`。

---

## Task 1: 配置区单选手风琴和状态标签切换

**Files:**
- Modify: `client/views/config_view.py`
- Modify: `tests/responsive_layout_test.py`

**Interfaces:**
- Produces: `_select_config_group(key)`、`_select_status_card(key)`；配置区键：`database/sync/alert/web_query/hardware/update`；状态区键：`overview/mysql/sync/ftp`。

- [ ] **Step 1: 写失败测试**

测试默认状态：数据库配置展开，其他配置组收起；概览状态卡可见，MySQL/同步/FTP 卡片内容不可见。测试点击同步配置后数据库收起且同步展开。测试点击 MySQL 标签后只有 MySQL 内容可见。

- [ ] **Step 2: 运行失败测试**

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
C:\Users\Administrator\AppData\Local\Programs\Python\Python311\Python311.exe -m pytest tests/responsive_layout_test.py -k "accordion or status_tabs" -v
```

Expected: 因状态标签容器/切换接口尚不存在而失败。

- [ ] **Step 3: 实现配置手风琴默认与持久化**

在 ConfigGroup 创建后连接 `expanded_changed`。首次按 `restore_ui_preference("config_group", "database")` 选择并调用 `_select_config_group()`；用户展开新组时收起其他组并保存该键。收起当前唯一展开组时立即重新展开，保证配置区至少有一项可见。

- [ ] **Step 4: 实现状态标签栏**

创建标签/按钮栏：概览、MySQL、同步、FTP。将既有四个 `StatusCard` 放入一个 `QStackedWidget`；标签点击只切换当前内容，不销毁状态卡。默认选择 `overview`，用 `restore_ui_preference("config_status_card", "overview")` 恢复用户选择。

- [ ] **Step 5: 运行测试并提交**

```powershell
C:\Users\Administrator\AppData\Local\Programs\Python\Python311\Python311.exe -m pytest tests/responsive_layout_test.py -v
git add client/views/config_view.py tests/responsive_layout_test.py
git commit -m "feat: 增加配置与状态卡片切换"
```

## Task 2: 统计/快捷操作常驻与中小宽度重排

**Files:**
- Modify: `client/views/config_view.py`
- Modify: `tests/responsive_layout_test.py`

**Interfaces:**
- Consumes: `set_responsive_mode("wide" | "medium" | "narrow")`。
- Produces: 中等宽度纵向配置/状态布局；窄屏单列统计和纵向车间操作。

- [ ] **Step 1: 写失败测试**

测试 `medium` 下：配置面板和状态面板为纵向、状态标签栏可见、数据统计和快捷操作均可见。测试 `narrow` 下：统计网格单列、车间选择布局纵向、导入/导出/清空队列按钮均可见且有正高度。

- [ ] **Step 2: 实现常驻辅助区**

保留数据统计和快捷操作卡，不加入单选折叠容器。中等宽度时将辅助区纵向排列；窄屏时设置统计网格为单列，将车间选择行调整为纵向布局并让组合框与管理按钮均填满可用宽度。

- [ ] **Step 3: 实现中小宽度状态区下移**

`medium`、`narrow` 都使 `_horizontal_area` 纵向排列；状态面板中只放状态标签栏和当前 `QStackedWidget` 内容，避免四张状态卡叠加导致视口外裁切。`wide` 恢复横向分割器及合理状态区最小宽度。

- [ ] **Step 4: 运行测试并提交**

```powershell
C:\Users\Administrator\AppData\Local\Programs\Python\Python311\Python311.exe -m pytest tests/responsive_layout_test.py -v
git add client/views/config_view.py tests/responsive_layout_test.py
git commit -m "feat: 重排中小宽度系统配置页面"
```

## Task 3: 验证、视觉检查与报告

**Files:**
- Modify: `docs/reviews/2026-09-30-medium-width-config-layout-verification.md`
- Test: `tests/responsive_layout_test.py`

- [ ] **Step 1: 增加离屏几何检查**

使用 1000px、800px 内容宽度创建 ConfigView，断言滚动区可滚动、当前状态卡与快捷操作控件在可见布局中、没有负几何或零高度按钮。

- [ ] **Step 2: 更新验证报告**

记录：默认展开项、状态标签行为、1000px 中等布局、800px 窄屏布局、长文本 Tooltip、后台状态线程未被状态卡切换中断的证据。

- [ ] **Step 3: 执行全量验证**

```powershell
$env:PYTHONPATH = "$PWD/client"
$env:QT_QPA_PLATFORM = "offscreen"
C:\Users\Administrator\AppData\Local\Programs\Python\Python311\Python311.exe -m pytest -q
C:\Users\Administrator\AppData\Local\Programs\Python\Python311\Python311.exe -m flake8 client tests --count --select=E9,F63,F7,F82 --show-source --statistics --exclude=__pycache__,.git,build,dist,pyd/_build_c
git diff --check
```

Expected: 测试通过、阻塞级 flake8 规则无错误、无 Git 空白错误。

- [ ] **Step 4: 提交验证报告**

```powershell
git add docs/reviews/2026-09-30-medium-width-config-layout-verification.md
git commit -m "docs: 验证中小宽度配置布局"
```
