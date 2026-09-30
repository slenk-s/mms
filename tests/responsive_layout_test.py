"""导航栏折叠状态与 UI 偏好持久化测试。"""
import sys
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
    assert view._mysql_test_thread is None
    assert view._port_check_thread is None
    assert view._ftp_test_thread is None
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
    qtbot.wait(20)

    value_label = card._rows["endpoint"][1]
    assert value_label.toolTip() == full_value
    assert value_label.text() != full_value

    card.resize(9000, 120)
    qtbot.wait(20)
    assert value_label.text() == full_value
    assert value_label.toolTip() == ""

    card.set_value("endpoint", "诊断内容：" + full_value, wrap=True)
    qtbot.wait(20)
    assert value_label.wordWrap() is True
    assert value_label.text() == "诊断内容：" + full_value
