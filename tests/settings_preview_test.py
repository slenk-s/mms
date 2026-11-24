"""Approved settings preview: accordion, toolbar and scrollable status sidebar."""
import sys
from pathlib import Path
import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'client'))
from views.config_view import ConfigView, StatusCard

@pytest.fixture
def view(qtbot, monkeypatch):
    monkeypatch.setattr(ConfigView, '_sync_web_service_status', lambda self: None)
    monkeypatch.setattr(ConfigView, '_update_status_panel', lambda self: None)
    widget = ConfigView()
    qtbot.addWidget(widget)
    yield widget
    assert widget.shutdown_for_application_exit()
    widget.close()


def test_preview_structure_and_independent_status_scroll(view):
    assert not hasattr(view, 'card_overview')
    assert not hasattr(view, 'card_actions')
    assert not hasattr(view, '_bottom_stack')
    assert [view._header_actions.itemAt(i).widget() for i in range(4)] == [
        view.btn_export, view.btn_import, view.btn_save, view.btn_reset]
    assert [view._right_layout.itemAt(i).widget() for i in range(4)] == [
        view.card_stats, view.card_mysql, view.card_sync, view.card_ftp]
    assert view.card_stats.isAncestorOf(view.cbx_workshop)
    assert view.card_stats.isAncestorOf(view.btn_workshop_manage)
    assert view.card_sync.isAncestorOf(view.btn_clear_queue)
    assert view._status_scroll.widget() is view._status_content


def test_config_single_selection_keeps_inputs(view):
    assert [key for key, group in view._config_groups.items() if group._expanded] == ['database']
    view.db_host.setText('example.invalid')
    view._config_groups['sync']._toggle_btn.click()
    assert [key for key, group in view._config_groups.items() if group._expanded] == ['sync']
    view._config_groups['sync']._toggle_btn.click()
    assert view._config_groups['sync']._expanded
    view._config_groups['database']._toggle_btn.click()
    assert view.db_host.text() == 'example.invalid'


@pytest.mark.parametrize('width', [500, 800, 1000, 1280])
def test_status_controls_scroll_into_view(view, width):
    view.resize(width, 700)
    view.show()
    QApplication.processEvents()
    for control in [view.cbx_workshop, view.btn_workshop_manage, view.btn_clear_queue]:
        view._status_scroll.ensureWidgetVisible(control, 0, 0)
        QApplication.processEvents()
        assert control.visibleRegion().boundingRect().height() >= control.height()
    assert len(view.findChildren(StatusCard)) == 4

@pytest.mark.parametrize('font_pixels', [12, 16, 20])
def test_group_headers_compact_under_application_button_style(qtbot, font_pixels):
    from views.config_view import ConfigGroup
    from PySide6.QtWidgets import QWidget, QVBoxLayout
    host = QWidget()
    qtbot.addWidget(host)
    host.setStyleSheet(
        f'QPushButton {{ padding: 8px 20px; min-height: 32px; font-size: {font_pixels}px; }}'
    )
    layout = QVBoxLayout(host)
    group = ConfigGroup('数据库配置', 'database')
    layout.addWidget(group)
    layout.addStretch()
    host.resize(800, 300)
    host.show()
    QApplication.processEvents()
    header = group._header
    button = group._toggle_btn
    assert header.height() <= max(40, button.fontMetrics().height() + 16)
    margins = header.layout().contentsMargins()
    assert margins.left() <= 12
    assert margins.top() + margins.bottom() <= 8
    assert button.height() >= button.fontMetrics().height()
    assert button.width() >= button.fontMetrics().horizontalAdvance(button.text())
