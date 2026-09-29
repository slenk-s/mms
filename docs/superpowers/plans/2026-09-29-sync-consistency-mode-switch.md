# 同步一致性与运行模式切换实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 SQLite 离线队列升级为可恢复、可诊断、幂等的同步状态机，并让三种运行模式在网络抖动和在途同步期间可安全切换及回滚。

**Architecture:** `LocalDB` 在主线程以事务维护队列状态；MySQL worker 只接收已领取批次并返回每项结果；`SyncEngine` 协调状态转换与退避；`MainWindow` 通过受控模式切换 API 管理 UI、网络监测和自动同步。页面只读加载器不参与同步队列，保持现有线程边界。

**Tech Stack:** Python 3.11、SQLite WAL、PyMySQL、PySide6 QThread/QTimer、pytest。

## Global Constraints

- 不跨线程共享 `LocalDB`，不破坏 `client/page_data_loader.py` 的独立只读连接设计。
- 本地业务写入与同步队列入列必须位于同一 SQLite 事务。
- 每个同步队列项必须有稳定 `operation_id`；重试不得生成新的业务操作。
- 模式切换过程中禁止启动新的同步批次，失败必须恢复上一稳定模式。
- 不修改未跟踪的 `MySQL_/MMS_init.sql`、`MySQL_/sql配置.pptx`。

---

## Task 1: 扩展 SQLite 队列 Schema 与事务 API

**Files:**
- Modify: `client/local_db.py:541-550,951-995`
- Create: `tests/sync_queue_test.py`

**Interfaces:**
- Produces: `enqueue_sync_operation(...)`、`claim_sync_batch(...)`、`mark_sync_succeeded(...)`、`mark_sync_retryable(...)`、`mark_sync_failed(...)`、`recover_in_progress_operations()`。
- Queue row fields: `operation_id`, `status`, `attempt_count`, `last_error`, `next_retry_at`, `batch_id`, `status_updated_at`。

- [ ] **Step 1: 写失败测试**

测试创建临时 `LocalDB`，断言：业务 insert 与队列项同时出现；领取后状态为 `in_progress`；过期重试项可领取；遗留 `in_progress` 可恢复为 `retryable`；成功项不会再次被领取。

- [ ] **Step 2: 运行失败测试**

Run:

```powershell
$env:PYTHONPATH = "$PWD/client"
C:\Users\Administrator\AppData\Local\Programs\Python\Python311\Python311.exe -m pytest tests/sync_queue_test.py -v
```

Expected: 因新增接口和字段不存在而失败。

- [ ] **Step 3: 实现迁移与状态转换**

在 `_init_db()` 对既有 `sync_queue` 使用 `PRAGMA table_info` 检测并 `ALTER TABLE` 添加字段；为 `(status, next_retry_at, created_at)` 建索引。`claim_sync_batch` 在单个事务中筛选 `pending` 或到期 `retryable`，更新为同一 `batch_id` 的 `in_progress` 后返回快照。所有状态更新使用 `WHERE id = ? AND status = ?` 防止重复确认。

- [ ] **Step 4: 运行测试并提交**

```powershell
C:\Users\Administrator\AppData\Local\Programs\Python\Python311\Python311.exe -m pytest tests/sync_queue_test.py -v
git add client/local_db.py tests/sync_queue_test.py
git commit -m "feat: 增加可恢复同步队列状态机"
```

## Task 2: MySQL 幂等写入与结果分类

**Files:**
- Modify: `client/mysql_client.py`
- Modify: `client/sync_engine.py`
- Modify: `MySQL_/Step/07_MMS_增加车间.sql` or create a dedicated additive migration under `MySQL_/Step/`
- Test: `tests/sync_queue_test.py`

**Interfaces:**
- Produces: `MySQLClient.apply_sync_operation(operation_id, table, operation, record_id, payload)` returning `applied`, `already_applied`, `retryable_error`, or `failed_error`.
- Consumes: claimed queue rows; must not create a new operation ID.

- [ ] **Step 1: 写失败测试**

使用 fake MySQL client 模拟：成功、服务端已处理、连接超时、字段校验失败。断言 `SyncEngine` 分别标记 `succeeded`、`succeeded`、`retryable`、`failed`，并保留错误信息。

- [ ] **Step 2: 设计服务端操作日志迁移**

新增仅追加的 `MMS_同步操作日志` 表，主键为 `operation_id`，保存表名、动作、记录 ID、处理时间和结果。写入业务表与插入操作日志置于同一 MySQL 事务；重复主键返回 `already_applied`。

- [ ] **Step 3: 实现错误分类和指数退避**

`OperationalError`、连接超时和网络不可达进入 `retryable`，其 `next_retry_at` 根据 `attempt_count` 计算有限指数退避；校验、权限和不可自动合并冲突进入 `failed`。达到 `SYNC_RETRY_MAX` 后转 `failed`。

- [ ] **Step 4: 运行测试并提交**

```powershell
C:\Users\Administrator\AppData\Local\Programs\Python\Python311\Python311.exe -m pytest tests/sync_queue_test.py -v
git add client/mysql_client.py client/sync_engine.py MySQL_/Step tests/sync_queue_test.py
git commit -m "feat: 增加幂等同步与断网重试"
```

## Task 3: 受控运行模式切换与回滚

**Files:**
- Modify: `client/main.py:1163-1195,1648-1688`
- Modify: `client/sync_engine.py`
- Modify: `client/network_monitor.py`
- Create: `tests/mode_switch_test.py`

**Interfaces:**
- Produces: `SyncEngine.begin_mode_transition()`、`finish_mode_transition(target_mode)`、`rollback_mode_transition(previous_mode)`。
- `MainWindow._apply_mode_change(old_mode, new_mode)` 返回成功/失败，并只在稳定后更新 `_app_mode` 与状态栏。

- [ ] **Step 1: 写失败测试**

使用 fake `SyncEngine` 和 fake `NetworkMonitor`，覆盖 online→offline、offline→online、semi_offline→online、在途批次超时、健康检查失败回滚。断言切换期间不允许新批次领取，失败后恢复原模式的定时器和监测状态。

- [ ] **Step 2: 实现切换屏障**

`switching` 状态下暂停自动同步和队列领取；等待当前批次完成。超过限定时间时将该批次的 `in_progress` 项释放为 `retryable`，而不是 `terminate()` 线程。目标模式组件全部启动且健康检查成功后才提交模式配置和 UI 状态。

- [ ] **Step 3: 实现失败回滚**

任一步骤失败时停止半启动组件，恢复上一个稳定模式的同步定时器、网络监测和状态栏文本；记录切换错误并给出非阻塞提示。

- [ ] **Step 4: 运行测试并提交**

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
C:\Users\Administrator\AppData\Local\Programs\Python\Python311\Python311.exe -m pytest tests/mode_switch_test.py -v
git add client/main.py client/sync_engine.py client/network_monitor.py tests/mode_switch_test.py
git commit -m "feat: 稳定运行模式切换"
```

## Task 4: 集成验证、迁移说明与全量检查

**Files:**
- Modify: `README.md`
- Create: `docs/reviews/2026-09-29-sync-consistency-verification.md`
- Test: `tests/sync_queue_test.py`, `tests/mode_switch_test.py`, `tests/page_loading_test.py`

- [ ] **Step 1: 编写验证报告**

记录队列恢复、重复响应、超时重试、模式切换回滚的实际测试结果；明确 MySQL 迁移执行顺序、回滚边界和人工处理 `failed` 队列项的方法。

- [ ] **Step 2: 运行全量验证**

```powershell
$env:PYTHONPATH = "$PWD/client"
$env:QT_QPA_PLATFORM = "offscreen"
C:\Users\Administrator\AppData\Local\Programs\Python\Python311\Python311.exe -m pytest tests/ -v --tb=short --ignore=tests/upload_update.py --ignore=tests/test_update.py
C:\Users\Administrator\AppData\Local\Programs\Python\Python311\Python311.exe -m flake8 client/ --count --select=E9,F63,F7,F82 --show-source --statistics --exclude=__pycache__,.git,build,dist,pyd/_build_c
git diff --check
```

Expected: 测试通过、阻塞级 flake8 错误为 0、无 Git 空白错误。

- [ ] **Step 3: 提交验证文档**

```powershell
git add README.md docs/reviews/2026-09-29-sync-consistency-verification.md
git commit -m "docs: 记录同步一致性验证"
```
