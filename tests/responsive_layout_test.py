"""导航栏折叠状态与 UI 偏好持久化测试。"""
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication


CLIENT_DIR = Path(__file__).resolve().parents[1] / "client"
if str(CLIENT_DIR) not in sys.path:
    sys.path.insert(0, str(CLIENT_DIR))

from i18n import tr, translations
from utils import ui_settings
from widgets.navigation import NavigationBar
from views.config_view import ConfigView, StatusCard


@pytest.fixture(autouse=True)
def temporary_language():
    """在内存中固定测试语言，且不写入语言偏好文件。"""
    previous_language = translations.get_current()
    translations.set_current("zh_CN")
    yield
    translations.set_current(previous_language)


@pytest.fixture
def isolated_ui_settings(monkeypatch, tmp_path):
    """将偏好写入临时目录，避免污染真实配置备份。"""
    monkeypatch.setattr(ui_settings, "_BACKUP_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def visible_config_view(qtbot):
    """以确定性清理配置页后台资源，不依赖固定延迟。"""
    view = ConfigView()
    qtbot.addWidget(view)
    view.resize(1280, 900)
    view.show()
    QApplication.processEvents()
    yield view
    view.dispose()
    assert not view._status_timer.isActive()
    view.close()


def test_navigation_collapses_and_expands_with_translated_tooltips(
    qtbot, isolated_ui_settings
):
    """折叠保留可辨识图标与翻译辅助文本，展开后恢复信息。"""
    navigation = NavigationBar()
    qtbot.addWidget(navigation)
    navigation.set_user_info("Alice", "admin")
    navigation.set_library_name("测试库")

    navigation.set_collapsed(True)

    assert navigation.is_collapsed() is True
    assert 64 <= navigation.minimumWidth() <= 72
    assert 64 <= navigation.maximumWidth() <= 72
    for page_id, button in navigation._buttons.items():
        assert button.text() == NavigationBar.NAV_ITEM_ICONS[page_id]
        assert button.toolTip() == tr(f"nav.{page_id}")
        assert button.accessibleName() == tr(f"nav.{page_id}")
    assert navigation.logout_btn.text() == ""
    assert navigation.logout_btn.toolTip() == tr("nav.logout")
    assert navigation.logout_btn.accessibleName() == tr("nav.logout")
    assert navigation.user_label.isHidden()
    assert navigation.lib_label.isHidden()

    navigation.set_collapsed(False)

    assert navigation.is_collapsed() is False
    assert 200 <= navigation.minimumWidth() <= 240
    assert 200 <= navigation.maximumWidth() <= 240
    for page_id, button in navigation._buttons.items():
        assert button.text() == tr(f"nav.{page_id}")
        assert button.accessibleName() == tr(f"nav.{page_id}")
    assert navigation.logout_btn.text() == tr("nav.logout")
    assert navigation.logout_btn.accessibleName() == tr("nav.logout")
    assert navigation.user_label.text() == "👤 Alice\n[管理员]"
    assert navigation.lib_label.text() == tr("nav.local_library", name="测试库")


def test_collapse_button_uses_translated_tooltip_and_accessible_name(
    qtbot, isolated_ui_settings
):
    """折叠开关应随语言和状态更新 Tooltip 与可访问名称。"""
    navigation = NavigationBar()
    qtbot.addWidget(navigation)

    assert navigation.collapse_button.toolTip() == tr("nav.collapse")
    assert navigation.collapse_button.accessibleName() == tr("nav.collapse")

    navigation.set_collapsed(True)
    assert navigation.collapse_button.toolTip() == tr("nav.expand")
    assert navigation.collapse_button.accessibleName() == tr("nav.expand")

    translations.set_current("es_ES")
    navigation.retranslate_ui()
    assert navigation.collapse_button.toolTip() == tr("nav.expand")
    assert navigation.collapse_button.accessibleName() == tr("nav.expand")
    for page_id, button in navigation._buttons.items():
        assert button.text() == NavigationBar.NAV_ITEM_ICONS[page_id]
        assert button.toolTip() == tr(f"nav.{page_id}")
        assert button.accessibleName() == tr(f"nav.{page_id}")


def test_navigation_collapsed_preference_is_saved_and_restored(
    qtbot, isolated_ui_settings
):
    """NavigationBar 保存折叠偏好，并由新实例恢复。"""
    navigation = NavigationBar()
    qtbot.addWidget(navigation)

    navigation.set_collapsed(True)

    assert ui_settings.restore_ui_preference("navigation_collapsed", False) is True

    restored_navigation = NavigationBar()
    qtbot.addWidget(restored_navigation)
    assert restored_navigation.is_collapsed() is True


def test_ui_preference_returns_default_without_saved_value(isolated_ui_settings):
    """没有保存的通用偏好应返回调用方指定的默认值。"""
    assert ui_settings.restore_ui_preference("unknown_preference", "default") == "default"



def test_config_view_reflows_existing_containers_without_recreating_controls(visible_config_view):
    """三档模式只重排容器，保留已经创建的表单和状态控件。"""
    view = visible_config_view

    db_host = view.db_host
    status_panel = view._status_panel
    stats_card = view.card_stats
    actions_card = view.card_actions

    view.set_responsive_mode("wide")
    assert view._horizontal_area.isVisible()
    assert status_panel.parentWidget() is view._horizontal_area
    assert view._horizontal_area.orientation() == Qt.Horizontal
    assert view._bottom_stack.orientation() == Qt.Horizontal

    view.set_responsive_mode("medium")
    assert status_panel.parentWidget() is view._horizontal_area
    assert view._horizontal_area.orientation() == Qt.Horizontal
    assert view._bottom_stack.orientation() == Qt.Horizontal
    assert view._horizontal_area.sizes()[0] > view._horizontal_area.sizes()[1]

    view.set_responsive_mode("narrow")
    assert status_panel.parentWidget() is view._horizontal_area
    assert view._horizontal_area.orientation() == Qt.Vertical
    assert view._horizontal_area.indexOf(view._config_panel) == 0
    assert view._horizontal_area.indexOf(status_panel) == 1
    assert view._bottom_stack.orientation() == Qt.Vertical
    assert view.db_host is db_host
    assert view.card_stats is stats_card
    assert view.card_actions is actions_card

    view.set_responsive_mode("wide")
    assert status_panel.parentWidget() is view._horizontal_area
    assert view.db_host is db_host


def test_config_view_resize_selects_breakpoint_mode_only_when_it_changes(
    visible_config_view, monkeypatch
):
    """生产 resizeEvent 根据宽度切换模式，并避免同模式重复重排。"""
    view = visible_config_view
    view.resize(1400, 700)
    QApplication.processEvents()
    assert view._responsive_mode == "wide"

    calls = []
    original_set_mode = view.set_responsive_mode

    def record_mode(mode):
        calls.append(mode)
        original_set_mode(mode)

    monkeypatch.setattr(view, "set_responsive_mode", record_mode)

    view.resize(1399, 700)
    QApplication.processEvents()
    assert view._responsive_mode == "medium"

    view.resize(1200, 700)
    QApplication.processEvents()
    assert view._responsive_mode == "medium"

    view.resize(1099, 700)
    QApplication.processEvents()
    assert view._responsive_mode == "narrow"
    assert calls == ["medium", "narrow"]


def test_config_view_narrow_geometry_keeps_configuration_scrollable_at_500_by_700(
    visible_config_view,
):
    """500×700 窄屏不应被配置面板的固定最小尺寸撑大。"""
    view = visible_config_view

    view.resize(500, 700)
    QApplication.processEvents()

    assert view._responsive_mode == "narrow"
    assert view.width() == 500
    assert view.height() == 700
    assert view._config_panel.minimumWidth() == 0
    assert view.minimumHeight() <= 700
    assert view._config_scroll.height() > 0
    assert view._config_scroll.verticalScrollBar().maximum() > 0


class _UnstoppableThread:
    """模拟已请求退出但尚未发出 finished 的后台线程。"""

    def __init__(self):
        self.quit_calls = 0
        self.wait_calls = 0
        self.terminate_calls = 0
        self.interruption_requests = 0

    def requestInterruption(self):
        self.interruption_requests += 1

    def isRunning(self):
        return True

    def quit(self):
        self.quit_calls += 1

    def wait(self, *_args):
        self.wait_calls += 1
        return False

    def terminate(self):
        self.terminate_calls += 1


def test_clear_memory_never_starts_port_check_thread(qtbot, monkeypatch):
    """释放内存不能经由 Web 状态同步启动新的端口检测线程。"""
    view = ConfigView()
    qtbot.addWidget(view)
    sync_calls = []
    monkeypatch.setattr(
        view, "_sync_web_service_status", lambda: sync_calls.append("sync")
    )

    view.clear_memory()

    assert sync_calls == []
    assert view._port_check_thread is None


def test_cleanup_running_thread_requests_quit_without_wait_or_terminate(qtbot):
    """仍在运行的线程保留引用，等待 finished 槽统一清理。"""
    view = ConfigView()
    qtbot.addWidget(view)
    thread = _UnstoppableThread()
    view._mysql_test_thread = thread

    view._cleanup_mysql_test_thread()

    assert thread.interruption_requests == 1
    assert thread.quit_calls == 1
    assert thread.wait_calls == 0
    assert thread.terminate_calls == 0
    assert view._mysql_test_thread is thread


def test_mysql_finished_cleanup_keeps_replacement_thread(qtbot):
    """旧 MySQL 线程的 finished 回调不能清空已替换的新线程。"""
    view = ConfigView()
    qtbot.addWidget(view)
    thread_a = object()
    thread_b = object()
    view._mysql_test_thread = thread_b

    view._cleanup_mysql_thread(thread_a)

    assert view._mysql_test_thread is thread_b


def test_ftp_finished_cleanup_keeps_replacement_thread(qtbot):
    """旧 FTP 线程的 finished 回调不能清空已替换的新线程。"""
    view = ConfigView()
    qtbot.addWidget(view)
    thread_a = object()
    thread_b = object()
    view._ftp_test_thread = thread_b

    view._cleanup_ftp_thread(thread_a)

    assert view._ftp_test_thread is thread_b


def test_old_thread_results_do_not_update_replacement_thread_ui(qtbot, monkeypatch):
    """A 的迟到结果不能覆盖已替换为 B 的 MySQL、FTP 或端口状态。"""
    view = ConfigView()
    qtbot.addWidget(view)
    source_thread = object()

    mysql_updates = []
    ftp_updates = []
    port_updates = []
    monkeypatch.setattr(
        view.card_mysql, "set_status_row", lambda *args: mysql_updates.append(args)
    )
    monkeypatch.setattr(
        view.card_ftp, "set_status_row", lambda *args: ftp_updates.append(args)
    )
    monkeypatch.setattr(
        view, "_update_web_service_ui", lambda value: port_updates.append(value)
    )

    view._mysql_test_thread = object()
    view._ftp_test_thread = object()
    view._port_check_thread = object()
    view._on_mysql_test_result(True, "old", source_thread)
    view._on_ftp_test_result(True, "old", source_thread)
    view._on_port_check_finished(view.web_port.value(), True, source_thread)

    assert mysql_updates == []
    assert ftp_updates == []
    assert port_updates == []


def test_visible_config_view_dispose_and_exit_without_native_thread_crash():
    """真实离屏子进程显示页面后立即清理并退出，不得遗留运行线程。"""
    script = textwrap.dedent(
        """
        from PySide6.QtWidgets import QApplication
        from views.config_view import ConfigView

        app = QApplication([])
        view = ConfigView()
        view.resize(1280, 900)
        view.show()
        app.processEvents()
        view.clear_memory()
        assert view.shutdown_for_application_exit(5000)
        view.close()
        app.processEvents()
        """
    )
    env = os.environ.copy()
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["PYTHONPATH"] = str(CLIENT_DIR)
    result = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, text=True,
        timeout=15, check=False,
    )

    assert result.returncode == 0, result.stderr


def test_config_view_dispose_stops_status_timer_and_threads(qtbot):
    """确定性资源清理会停止状态计时器并释放线程引用。"""
    view = ConfigView()
    qtbot.addWidget(view)
    view._start_status_timer()

    assert view._status_timer.isActive()
    view.dispose()

    assert not view._status_timer.isActive()
    assert view._mysql_test_thread is None
    assert view._port_check_thread is None
    assert view._ftp_test_thread is None


def test_config_view_rejects_unknown_responsive_mode(visible_config_view):
    view = visible_config_view

    with pytest.raises(ValueError, match="wide.*medium.*narrow"):
        view.set_responsive_mode("compact")


def test_status_card_elides_long_value_and_keeps_full_tooltip(qtbot):
    card = StatusCard("状态", "i", "#2563eb")
    qtbot.addWidget(card)
    full_value = "https://example.invalid/diagnostics/" + "very-long-status-value-" * 12
    card.set_row("endpoint", "服务地址", full_value)
    card.resize(180, 120)
    card.show()
    QApplication.processEvents()

    value_label = card._rows["endpoint"][1]
    assert value_label.toolTip() == full_value
    assert value_label.text() != full_value

    card.resize(9000, 120)
    QApplication.processEvents()
    assert value_label.text() == full_value
    assert value_label.toolTip() == ""

    card.set_value("endpoint", "诊断内容：" + full_value, wrap=True)
    QApplication.processEvents()
    assert value_label.wordWrap() is True
    assert value_label.text() == "诊断内容：" + full_value
