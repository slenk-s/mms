# 页面切换性能与质量审查实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 先交付有证据的全工程风险/质量审查报告，再为物料、人员、固定资产各 5,000 条数据的页面切换实现可重复压测和非阻塞加载。

**Architecture:** 保持 `LocalDB` 的主线程专属读写约束。新增页面读取模块在每个工作线程任务内创建并关闭独立 SQLite 只读连接，使用固定白名单查询生成不可变快照；`MainWindow` 负责请求编号、缓存、失效和主线程渲染。视图和同步引擎的既有业务写入接口不改变。

**Tech Stack:** Python 3.11、PySide6、SQLite WAL、pytest、GitHub Actions、flake8。

## Global Constraints

- 不调用、不移动、不共享 `LocalDB` 实例到工作线程。
- 每个后台读取任务使用独立 SQLite 连接，执行 `PRAGMA query_only=ON`，并在任务结束时于该线程关闭。
- 所有 `QWidget`、`QStackedWidget`、`DataTable` 更新只在 Qt 主线程执行。
- `inventory`、`register`、`asset`、`config`、`user_manage` 的既有权限和页面数据语义保持不变。
- 基准数据为物料、人员、资产各 5,000 条；性能报告记录 P50/P95，不将机器相关的绝对耗时阈值写成 CI 断言。
- 不修改或暂存现有未跟踪文件 `MySQL_/MMS_init.sql`、`MySQL_/sql配置.pptx`。

---

## 文件结构

| 文件 | 责任 |
| --- | --- |
| `docs/reviews/2026-09-29-full-project-quality-review.md` | 全工程 P0–P3 风险/质量审查结论与证据。 |
| `client/page_data_loader.py` | 白名单页面查询、只读 SQLite 快照和工作线程读取器。 |
| `client/main.py` | 页面请求编号、缓存、失效、结果应用、关闭清理和重复库存加载修复。 |
| `tests/page_loading_test.py` | 只读连接、快照语义、过期请求和缓存失效的回归测试。 |
| `tests/page_loading_benchmark.py` | 建立 5,000 条临时数据，运行首载/缓存/失效/快速切换基准并输出 JSON。 |
| `docs/reviews/2026-09-29-page-loading-benchmark.md` | 优化前后 P50/P95、测试环境和结论。 |

## Task 1: 交付全工程风险与质量审查报告

**Files:**
- Create: `docs/reviews/2026-09-29-full-project-quality-review.md`
- Read: `client/main.py`, `client/local_db.py`, `client/mysql_client.py`, `client/sync_engine.py`, `client/network_monitor.py`
- Read: `client/views/*.py`, `client/widgets/*.py`, `client/utils/*.py`, `client/web_/*.py`, `.github/workflows/ci.yml`, `MySQL_/Step/*.sql`

**Interfaces:**
- Consumes: 当前 `main` 分支源码和现有 CI 配置。
- Produces: 按优先级排序的审查发现；每项包含精确文件位置、触发条件、影响、建议和验证方式。

- [ ] **Step 1: 运行现有自动化基线并记录输出**

Run:

```powershell
python -m pytest tests/ -v --tb=short --ignore=tests/upload_update.py --ignore=tests/test_update.py
flake8 client/ --count --select=E9,F63,F7,F82 --show-source --statistics --exclude=__pycache__,.git,build,dist,pyd/_build_c
```

Expected: 保存通过、失败或环境缺失的完整摘要；不因现有失败而修改无关代码。

- [ ] **Step 2: 审查主线程、同步和 SQLite 边界**

检查 `client/main.py:1038-1161` 的登录、导航和刷新路径，检查 `client/local_db.py:211-219` 的线程约束，以及 `client/sync_engine.py` 中所有 `local_db` 调用。记录重复库存加载、主线程同步查询、过期信号、连接池和同步后的 UI 刷新风险。

- [ ] **Step 3: 审查页面、网络、安全、Web、构建和 SQL**

检查视图/控件的 Qt 线程归属、定时器和对象生命周期；检查凭据、证书、FTP、API 鉴权、MySQL 超时与异常处理；检查 CI 是否覆盖关键测试；检查 SQL 迁移的顺序和幂等性。

- [ ] **Step 4: 写入审查报告并验证证据完整性**

每项使用以下模板：

```markdown
### [P2] 简短风险标题

- **位置：** `client/path.py:123-130`
- **触发条件：** 明确的用户操作、数据规模或异常条件。
- **影响：** 对正确性、性能、安全性或可维护性的可观察后果。
- **证据：** 代码调用链、测试输出或可重复命令。
- **建议：** 最小修复方向和验证命令。
- **本轮归属：** 页面加载优化 / 后续专项。
```

Run:

```powershell
git diff --check -- docs/reviews/2026-09-29-full-project-quality-review.md
```

Expected: 无空白错误，所有发现均包含位置和证据。

- [ ] **Step 5: 提交审查报告**

```powershell
git add docs/reviews/2026-09-29-full-project-quality-review.md
git commit -m "docs: 增加全工程质量审查报告"
```

## Task 2: 为独立只读页面快照建立测试和基准数据

**Files:**
- Create: `tests/page_loading_test.py`
- Create: `tests/page_loading_benchmark.py`
- Test: `tests/page_loading_test.py`

**Interfaces:**
- Consumes: `LocalDB(db_path, workshop)` 用于生成临时业务数据；生产读取器不得跨线程使用该对象。
- Produces: `build_benchmark_database(root: Path) -> tuple[str, str, str]` 和针对读取器的失败测试。

- [ ] **Step 1: 写入确定性数据工厂**

在 `tests/page_loading_benchmark.py` 定义：

```python
def build_benchmark_database(root: Path) -> tuple[str, str, str]:
    """返回 (workshop_db_path, shared_db_path, workshop_name)。"""
    workshop = "压测车间"
    db = LocalDB(db_path=str(root / "local_cache_压测车间.db"), workshop=workshop)
    # 使用固定主键，写入 5,000 条物料、5,000 条资产、5,000 条用户/员工记录。
    # 返回 db.db_path、db._shared_db_path 和 workshop。
```

数据字段必须覆盖页面渲染所需字段：物料的 `id/material_code/material_name/stock_qty/last_update`，资产的 `id/asset_no/asset_name/updated_at`，用户/员工的 `id/username/employee_no/name`。

- [ ] **Step 2: 写入读取器失败测试**

在 `tests/page_loading_test.py` 先导入尚不存在的接口：

```python
from page_data_loader import load_page_snapshot


def test_inventory_snapshot_returns_only_current_workshop(tmp_path):
    db_path, shared_path, workshop = build_benchmark_database(tmp_path)
    snapshot = load_page_snapshot("inventory", db_path, shared_path, workshop)
    assert len(snapshot["materials"]) == 5000
    assert {row["workshop"] for row in snapshot["materials"]} == {workshop}
```

再为 `user_manage` 编写断言，确认用户与员工快照分别来自公共库；为未知页面写 `pytest.raises(ValueError)` 断言。

- [ ] **Step 3: 验证测试在实现前失败**

Run:

```powershell
python -m pytest tests/page_loading_test.py -v
```

Expected: 因 `page_data_loader` 不存在而失败。

- [ ] **Step 4: 增加只读与数据一致性测试**

新增测试使用读取器返回的连接路径执行写入尝试，断言抛出 `sqlite3.OperationalError`；新增测试验证物料、资产、用户/员工快照的数量和固定主键集合与构造数据一致。

## Task 3: 实现独立 SQLite 页面读取器

**Files:**
- Create: `client/page_data_loader.py`
- Test: `tests/page_loading_test.py`

**Interfaces:**
- Consumes: `page_id: str`、`workshop_db_path: str`、`shared_db_path: str`、`workshop: str`、`request_id: int`。
- Produces: `PageLoadResult(page_id, request_id, payload, error)`；`payload` 的键固定为 `materials`、`borrow_records`、`assets`、`config_items`、`users`、`employees` 中与页面相关的子集。

- [ ] **Step 1: 实现固定白名单快照函数**

```python
@dataclass(frozen=True)
class PageLoadResult:
    page_id: str
    request_id: int
    payload: dict[str, list[dict]] | None
    error: str | None


def load_page_snapshot(
    page_id: str,
    workshop_db_path: str,
    shared_db_path: str,
    workshop: str,
) -> dict[str, list[dict]]:
    """用独立只读连接读取单个页面的本地数据快照。"""
```

用 `sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True, timeout=5.0)` 打开连接，设置 `row_factory = sqlite3.Row` 和 `PRAGMA query_only=ON`，在 `finally` 中关闭连接。只允许以下 `page_id`：

```python
PAGE_IDS = {"inventory", "register", "asset", "config", "user_manage"}
```

查询不得拼接用户输入；表名和排序字段只来自模块内常量。库存和资产按车间筛选；登记页读取物料和未归档领用记录；用户管理从 `shared_db_path` 读取用户与员工数据。

- [ ] **Step 2: 实现 QThread 结果封装**

```python
class PageDataLoadWorker(QThread):
    loaded = Signal(object)

    def __init__(self, page_id, request_id, workshop_db_path, shared_db_path, workshop, parent=None):
        super().__init__(parent)
        # 仅保存不可变参数；不接收 LocalDB 对象。

    def run(self) -> None:
        # 调用 load_page_snapshot；始终发射一个 PageLoadResult。
```

`run()` 不引用任何 Widget，不修改缓存，不调用 `LocalDB`。

- [ ] **Step 3: 运行读取器测试并验证通过**

Run:

```powershell
python -m pytest tests/page_loading_test.py -v
```

Expected: 快照、白名单、车间过滤、只读写入失败测试全部通过。

- [ ] **Step 4: 提交读取器与测试**

```powershell
git add client/page_data_loader.py tests/page_loading_test.py tests/page_loading_benchmark.py
git commit -m "feat: 添加异步页面数据读取器"
```

## Task 4: 将请求编号、缓存和失效接入 MainWindow

**Files:**
- Modify: `client/main.py:397-570`
- Modify: `client/main.py:1038-1161`
- Test: `tests/page_loading_test.py`

**Interfaces:**
- Consumes: `PageDataLoadWorker.loaded(PageLoadResult)`。
- Produces: `_request_page_load(page_id, force=False)`、`_invalidate_page_cache(*page_ids)`、`_apply_page_result(result)`。

- [ ] **Step 1: 写入 MainWindow 行为测试**

使用 `QApplication` 离屏夹具和可替换的测试 worker，断言：

```python
def test_newer_request_drops_older_result(window, result_factory):
    window._request_page_load("inventory")
    first_request = window._page_request_ids["inventory"]
    window._request_page_load("inventory", force=True)
    second_request = window._page_request_ids["inventory"]
    window._apply_page_result(result_factory("inventory", first_request, materials=[]))
    assert window._page_cache.get("inventory") is None
    window._apply_page_result(result_factory("inventory", second_request, materials=[]))
    assert "inventory" in window._page_cache
```

增加测试：缓存命中不创建第二个 worker；缓存失效后创建新 worker；非管理员不能触发管理页面加载。

- [ ] **Step 2: 初始化页面加载状态并修复重复库存加载**

在 `MainWindow.__init__` / `_init_ui` 后初始化：

```python
self._page_cache: dict[str, dict[str, list[dict]]] = {}
self._page_request_ids: dict[str, int] = {}
self._page_workers: dict[str, PageDataLoadWorker] = {}
```

删除 `client/main.py:1066` 对 `_load_data_for_page("inventory")` 的直接调用，仅保留 `_on_page_changed("inventory")` 作为首次加载入口。

- [ ] **Step 3: 用异步请求替换同步页面加载**

保留 `_load_data_for_page()` 作为仅用于诊断回退的同步函数；新增 `_request_page_load()` 默认用于导航、手动刷新和同步后刷新。缓存命中时直接调用 `_apply_page_payload()`；缓存未命中时创建 `PageDataLoadWorker`，连接 `loaded`、`finished` 和 `deleteLater`，并将 worker 保存到 `_page_workers` 防止提前释放。

在 `_apply_page_result()` 中严格检查：结果页面仍等于当前页面、请求号等于 `_page_request_ids[page_id]`、页面权限仍然满足。检查失败时不更新任何视图。

- [ ] **Step 4: 实现精确缓存失效**

在已有业务成功回调完成本地写入后调用：

```python
self._invalidate_page_cache("inventory", "register")  # 物料或领用变更
self._invalidate_page_cache("asset")                    # 资产变更
self._invalidate_page_cache("user_manage")              # 用户或员工变更
self._invalidate_page_cache("config")                   # 配置变更
```

在 `_on_sync_completed()` 中根据同步类型和数据表失效相关页面；无法区分表时失效当前页面。登出、切换车间或切换数据库时调用 `_invalidate_page_cache()` 清空全部缓存并使所有未完成请求过期。

- [ ] **Step 5: 将刷新路径改为强制异步重载**

将 `_do_refresh()` 和 `_on_sync_completed()` 中的 `_load_data_for_page()` 调用改为：

```python
self._invalidate_page_cache(self._current_page)
self._request_page_load(self._current_page, force=True)
```

保留现有同步拉取与状态栏消息顺序；不得在 GUI 线程等待 worker 完成。

- [ ] **Step 6: 运行行为测试**

Run:

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
python -m pytest tests/page_loading_test.py -v
```

Expected: 过期结果、缓存命中、缓存失效、权限拦截和主线程渲染测试通过。

- [ ] **Step 7: 提交 MainWindow 集成**

```powershell
git add client/main.py tests/page_loading_test.py
git commit -m "perf: 异步加载并缓存页面数据"
```

## Task 5: 运行 5,000 条基准、完成报告与全量验证

**Files:**
- Modify: `tests/page_loading_benchmark.py`
- Create: `docs/reviews/2026-09-29-page-loading-benchmark.md`
- Test: `tests/page_loading_test.py`

**Interfaces:**
- Consumes: 临时 SQLite 数据库、`MainWindow` 页面加载接口、页面读取器。
- Produces: 机器可读 JSON 输出和包含优化前后 P50/P95 的 Markdown 报告。

- [ ] **Step 1: 实现四类基准场景**

`tests/page_loading_benchmark.py` 使用 `time.perf_counter_ns()` 记录至少 10 次样本，并输出如下结构：

```python
{
  "environment": {"python": sys.version, "qt_platform": os.environ.get("QT_QPA_PLATFORM")},
  "records_per_type": 5000,
  "scenarios": {
    "first_load": {"inventory": [], "user_manage": [], "asset": []},
    "cache_hit": {"inventory": [], "user_manage": [], "asset": []},
    "invalidated_reload": {"inventory": [], "user_manage": [], "asset": []},
    "rapid_switch": {"final_page": [], "stale_result_overwrites": 0}
  }
}
```

- [ ] **Step 2: 记录基线与优化后数据**

在异步集成前运行一次基线；集成后使用同一脚本、同一数据构造器再运行一次。每个场景计算：

```python
def percentile(samples_ns: list[int], percentile: float) -> float:
    ordered = sorted(samples_ns)
    index = round((len(ordered) - 1) * percentile / 100)
    return ordered[index] / 1_000_000
```

- [ ] **Step 3: 写入基准报告**

报告列出：运行日期、Python/Qt 平台、数据规模、各场景每页 P50/P95、失败次数、快速切换的过期覆盖次数、优化前后差异和尚存瓶颈。若优化前基线无法在当前环境重建，报告必须明确标记原因，不得伪造比较数字。

- [ ] **Step 4: 执行完整验证**

Run:

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
python -m pytest tests/ -v --tb=short --ignore=tests/upload_update.py --ignore=tests/test_update.py
flake8 client/ --count --select=E9,F63,F7,F82 --show-source --statistics --exclude=__pycache__,.git,build,dist,pyd/_build_c
git diff --check
```

Expected: 全部受控测试通过、阻塞级 flake8 规则无错误、无 Git 空白错误。

- [ ] **Step 5: 提交基准与报告**

```powershell
git add tests/page_loading_benchmark.py docs/reviews/2026-09-29-page-loading-benchmark.md
git commit -m "test: 增加页面加载性能基准"
```
