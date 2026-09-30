# 响应式主窗口布局验证报告

- 验证日期：2026-09-30
- 范围：Task3——主窗口断点分发与导航自动折叠
- 验证解释器：`C:\Users\Administrator\AppData\Local\Programs\Python\Python311\Python311.exe`（Python 3.11.3）
- Qt 运行时：PySide6 / Qt 6.10.0

## 实现验证

`MainWindow._responsive_mode_for_width(width)` 的断点如下：

| 主窗口 central widget 内容宽度 | 期望模式 | 实测结果 |
| --- | --- | --- |
| 1400 | `wide` | `wide` |
| 1399 | `medium` | `medium` |
| 1099 | `narrow` | `narrow` |

真实 `resizeEvent` 以 `central_widget.contentsRect().width()` 作为唯一断点来源：同步调用 `ConfigView.set_responsive_mode()`；在 Qt 布局完成后，使用 `QTimer.singleShot(0, guarded callback)` 仅当主窗口与配置视图仍有效、宽度未过期且模式确有差异时，再调用该公开接口确认最终模式。该保护避免 `ConfigView` 的既有自身 `resizeEvent` 以局部宽度覆盖主窗口断点。

窄屏进入时，导航栏通过 `set_collapsed(True, persist=False)` 自动折叠，并由 `_navigation_auto_collapsed` 记录为临时状态。回到宽/中屏时，从持久化的 `navigation_collapsed` 读取用户偏好并以 `persist=False` 恢复；自动状态不会写入或覆盖用户偏好。

## 真实 Qt offscreen 回归

`tests/responsive_layout_test.py::test_main_window_responsive_layout_uses_real_offscreen_qt` 在独立子进程创建真实 Qt 主窗口骨架、真实 `NavigationBar` 与真实 `ConfigView`，覆盖：

1. 1400 / 1399 / 1099 三个断点与 resize 后的 `ConfigView` 模式；
2. 窄屏导航自动折叠；
3. 宽屏恢复已持久化的“导航展开”用户偏好，且偏好值不被自动折叠改写；
4. 登录覆盖层可见时，resize 后仍为 `(0, 0, window.width(), window.height())`。

结果：`1 passed`。

同一模块完整回归结果：`21 passed in 8.45s`。

## Offscreen / DPI 限制

本次使用 `QT_QPA_PLATFORM=offscreen`。实测设备像素比为 `1.0`，逻辑 DPI 为 `96 x 96`。该后端验证 Qt 事件循环、几何尺寸、布局与响应式状态分发，但不代表真实显示器的高 DPI 缩放、窗口管理器装饰、字体渲染、触控或多显示器行为；这些需在目标桌面环境补充人工验证。

## 最终验证结果

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 全量测试 | `QT_QPA_PLATFORM=offscreen Python311.exe -m pytest -v` | `50 passed in 12.28s` |
| 静态检查 | `Python311.exe -m flake8 client tests --select=E9,F63,F7,F82` | 退出码 `0`，无输出 |
| 差异检查 | `git diff --check` | 退出码 `0`，无输出 |
| 暂存范围 | `git diff --name-only` 与未跟踪文件检查 | 仅暂存本 Task 允许修改的三个文件；既有 `MySQL_/sql配置.pptx` 修改不纳入提交 |
