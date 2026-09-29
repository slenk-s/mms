# MMS 全工程风险与质量审查报告

**审查日期：** 2026-09-29
**审查范围：** 桌面端、页面与控件、SQLite/MySQL 同步、Web 查询服务、更新/构建、CI、SQL 和测试资产。
**代码基线：** `main`，审查开始时 HEAD 为 `b5da778`；本报告不包含未跟踪的 `MySQL_/MMS_init.sql` 与 `MySQL_/sql配置.pptx`。

## 执行摘要

本次审查确认了一个需要优先修复的 Web 查询服务认证缺陷：配置的 API Key 会被服务端主动下发给任意访问首页的客户端，并同时嵌入页面 JavaScript，因此不能构成访问控制。页面切换性能问题也已定位为主线程同步 SQLite 查询与数据设置链路；在 5,000 条数据下，它是本轮性能优化的首要目标。

自动化保障目前不足：CI 排除了两个 Python 脚本，剩余 JavaScript 文件未被 CI 调用，且未发现可被 pytest 收集的 Python 测试；Cython 编译失败也被 CI 忽略。线程停止路径仍使用强制终止，存在中断执行中工作任务的风险。

## 验证基线

| 检查 | 结果 | 证据 |
| --- | --- | --- |
| Python 源码语法编译 | 通过 | 使用 Codex 随附 Python 3.12 执行 `python -m compileall -q client`，退出码 0。 |
| pytest | 未执行 | 系统 Python 3.11 的可执行文件缺失；随附 Python 无 `pytest` 与 `PySide6`。 |
| flake8 | 未执行 | 当前环境未安装 `flake8` 命令。 |
| CI 测试发现 | 无有效 Python 用例 | `tests/` 仅包含 `test_update_chain.js`、`test_update.py`、`upload_update.py`；CI 排除了两个 Python 文件，且未执行 Node 测试。 |

本地环境缺少可用的 Python 3.11 与开发依赖，不代表 CI 必然失败；但仓库没有锁定依赖或一键初始化脚本，使本地复现 CI 的成本和不确定性较高。

## 发现

### [P1] Web 查询 API Key 被下发给未认证访问者，认证实际失效

- **位置：** `client/web_/app.py:60-70`、`client/web_/routes/query.py:44-49`、`client/web_/templates/index.html:110-111,170`。
- **触发条件：** Web 查询服务启用了 `web_query_api_key`，任意客户端访问 `GET /`。
- **影响：** 中间件将真实 API Key 写入 HttpOnly Cookie；路由又把同一 Key 注入页面；模板将其写进 `var API_KEY` 并通过 `X-API-Key` 发送。任何能访问首页的用户都可获得已认证 Cookie 和 JavaScript 中的明文 Key，从而调用所有 `/api/*` 查询接口。该 Key 仅阻止未先访问首页的直接请求，不提供身份认证或授权边界。
- **证据：** 首页不要求认证，但 `app.py` 在 `path == "/" and api_key` 时执行 `response.set_cookie(key="api_key", value=api_key, ...)`；`query.py` 向模板传入 `get_web_query_api_key()`；`index.html` 读取 `{{ api_key }}`。
- **建议：** 本轮之外立即修复。移除模板和 Cookie 中的 API Key；为首页和 API 使用真正的访问控制（例如服务端会话、反向代理认证或设备/用户认证）。若业务只需内网匿名查询，应移除“API Key 已认证”的误导性设计，并通过网络隔离控制暴露范围。
- **验证方式：** 启用 Key 后，以无凭据浏览器访问首页；修复后页面源代码、Cookie 和浏览器网络请求中均不应包含 Key，且未认证客户端应不能访问 `/api/records`。
- **本轮归属：** 后续安全专项，不纳入页面加载性能改动。

### [P2] 页面导航在 GUI 线程同步查询 SQLite 并重建页面数据

- **位置：** `client/main.py:1108-1140`、`client/main.py:1156-1161`、`client/widgets/data_table.py:125-142`。
- **触发条件：** 用户切换库存、登记、资产、配置或用户管理页面；手动刷新、同步完成和语言切换也会调用相同加载函数。
- **影响：** `QStackedWidget.setCurrentIndex()` 后立即调用 `_load_data_for_page()`；该函数在 GUI 线程执行 `LocalDB.query()` / `get_all_users()`，再同步调用各页面 `set_data()`。5,000 条数据下，SQLite 读取、Python 字典创建、全量数据引用替换和过滤准备会占用事件循环，使窗口重绘和输入延后。
- **证据：** `_on_page_changed()` 在 `setCurrentIndex()` 的下一行调用 `_load_data_for_page()`；`_do_refresh()`、`_on_sync_completed()`、`_refresh_current_view()` 和语言切换同样直接调用它。`DataTable.set_data()` 保存完整列表并立即进入刷新路径。
- **建议：** 按已确认设计，引入独立只读 SQLite 工作线程、请求序号、页面快照缓存和精确失效；所有 Widget 更新仍保留在主线程。
- **验证方式：** 使用物料、人员、资产各 5,000 条临时数据，记录首载、缓存命中、失效重载和快速连续切换的 P50/P95；验证旧请求结果不覆盖新页面。
- **本轮归属：** 本轮页面加载优化。

### [P2] 首次进入主界面重复加载库存数据

- **位置：** `client/main.py:1065-1068`。
- **触发条件：** 用户登录成功或自动登录成功，执行 `_enter_main_ui()`。
- **影响：** 代码先直接调用 `_load_data_for_page("inventory")`，随后调用 `_on_page_changed("inventory")`；后者又调用 `_load_data_for_page("inventory")`。首次登录会执行两次库存 SQLite 查询和两次页面数据设置，数据量越大，首屏等待越明显。
- **证据：** 两个连续调用之间没有缓存、条件判断或数据版本变化。
- **建议：** 仅保留统一的页面导航/加载入口；在异步方案中由 `_on_page_changed("inventory")` 发起一次请求。
- **验证方式：** 使用注入的读取器或计数器断言首次登录仅发起一次库存页面加载。
- **本轮归属：** 本轮页面加载优化。

### [P2] CI 对核心测试与 Cython 构建缺少失败保护

- **位置：** `.github/workflows/ci.yml:67-69,76-78`、`tests/`。
- **触发条件：** 推送或创建面向 `main` 的 Pull Request。
- **影响：** Cython 构建命令使用 `|| true`，编译失败不会使 CI 失败；pytest 明确排除 `tests/test_update.py` 和 `tests/upload_update.py`，余下 `test_update_chain.js` 未被 Node/npm 命令执行。本次审查未在 `tests/` 中发现 `def test_`、`class Test` 或被 CI 调用的 JavaScript 测试，因此工作流可能显示通过但没有验证关键业务逻辑。
- **证据：** CI 安装 pytest 后只运行带两个 `--ignore` 的 pytest；没有 Node 设置或 JS 测试步骤；`tests/` 文件清单仅有上述三个脚本。
- **建议：** 新增可收集的 pytest 单元/集成测试；把 FTP/更新流程改造成可注入 fake server 的测试；将 Cython 编译作为可配置但可见的构建状态，发布分支不得静默忽略失败。
- **验证方式：** CI 日志必须显示非零的收集测试数；故意破坏一个被测函数或 Cython 源时，对应受保护工作流应失败。
- **本轮归属：** 本轮新增页面加载回归测试；完整 CI 重构作为后续专项。

### [P2] 后台线程超时后使用 `terminate()`，可能在任务中间强制停止

- **位置：** `client/network_monitor.py:124-130`、`client/sync_engine.py:384-390`。
- **触发条件：** 网络监测线程在 3 秒内未退出，或同步工作线程在 5 秒内未退出。
- **影响：** `QThread.terminate()` 可能在任意指令边界终止线程。对于网络 I/O、信号投递或未来增加的资源管理代码，可能留下未完成操作、丢失清理逻辑或造成状态与界面不同步。
- **证据：** 两处都先调用 `quit()` / `wait()`，超时后无任务取消确认或资源收尾，直接调用 `terminate()`。
- **建议：** 为 worker 实现协作式取消标志、短超时 I/O 和已停止信号；关闭流程等待 worker 确认取消。仅在进程退出的最后手段记录明确告警，不把强制终止作为常规路径。
- **验证方式：** 用可控的阻塞网络任务模拟关闭；断言 worker 收到取消、连接关闭、线程正常结束且不调用 `terminate()`。
- **本轮归属：** 后续线程生命周期专项。

### [P2] Web 服务把运行时异常原文返回给客户端

- **位置：** `client/web_/app.py:98-104`。
- **触发条件：** 查询服务处理请求时抛出 `RuntimeError`，例如数据库连接或配置错误。
- **影响：** `str(exc)` 可能向浏览器暴露内部主机名、端口、数据库状态、路径或实现细节；同时不利于保持稳定的 API 错误契约。
- **证据：** 全局异常处理器直接返回 `{"detail": str(exc)}`，没有日志关联 ID 或面向客户端的通用错误消息。
- **建议：** 在服务端记录完整异常与关联 ID；客户端返回稳定的通用消息和该 ID。仅在受限开发模式返回调试细节。
- **验证方式：** 模拟数据库连接异常；响应不得包含连接字符串、路径或底层异常文本，服务端日志必须可定位完整堆栈。
- **本轮归属：** 后续安全与可靠性专项。

### [P3] 核心职责集中在超大模块，增加回归与性能改动风险

- **位置：** `client/views/config_view.py`（2,964 行）、`client/main.py`（1,865 行）、`client/views/register_view.py`（1,494 行）、`client/views/user_manage_view.py`（1,167 行）、`client/local_db.py`（1,138 行）。
- **触发条件：** 修改导航、同步、配置、用户/员工或数据库逻辑。
- **影响：** 页面构建、业务编排、线程管理、配置写入和渲染逻辑交织，使行为定位和独立测试困难；一次性能改动更容易影响无关功能。
- **证据：** `main.py` 同时负责认证、工作区切换、同步、导航、刷新、语言和关闭；`config_view.py` 同时负责 UI、网络检测、Web 服务进程控制、导入导出和车间管理。
- **建议：** 不进行一次性大重构。性能优化先以独立 `page_data_loader.py` 隔离读取职责；后续按领域提取 controller/service，配套测试后逐步收缩大文件。
- **验证方式：** 新增页面加载模块不增加 `main.py` 的数据库查询分支数量；相关单元测试可直接测试读取器而无需构造完整主窗口。
- **本轮归属：** 本轮通过新增独立加载器局部缓解；全面拆分为后续可维护性专项。

## 优先级与处理顺序

1. **立即安排：** P1 Web API Key 认证失效；在服务对非受信网络开放前必须修复或限制网络暴露。
2. **本轮执行：** P2 页面同步加载、首次库存重复加载、页面加载测试与 5,000 条压测。
3. **下一轮：** P2 CI 测试保护、Cython 构建可见性、线程协作式关闭、Web 异常脱敏。
4. **持续治理：** P3 按职责拆分超大模块并降低广泛 `except Exception` 的可观测性缺口。

## 审查限制

- 当前机器的 Python 3.11 注册路径指向不存在的解释器，且没有可用的 `flake8`、`pytest`、`PySide6` 开发环境，因此未在本机完成运行时单测、GUI 压测或 flake8 结果验证。
- 使用随附 Python 3.12 只完成了源码语法编译；它不能替代 Python 3.11 + PySide6 的行为验证。
- 本报告仅列出已由源码和命令输出支撑的发现；未将未验证的风格偏好写成缺陷。
