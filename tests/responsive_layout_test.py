"""导航栏折叠状态与 UI 偏好持久化测试。"""
import sys
from pathlib import Path

import pytest


CLIENT_DIR = Path(__file__).resolve().parents[1] / "client"
if str(CLIENT_DIR) not in sys.path:
    sys.path.insert(0, str(CLIENT_DIR))

from i18n import tr, translations
from utils import ui_settings
from widgets.navigation import NavigationBar


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
