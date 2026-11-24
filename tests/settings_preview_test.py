"""Regression tests for compact configuration group headers."""
import sys
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication, QSizePolicy, QVBoxLayout, QWidget

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "client"))

from views.config_view import ConfigGroup


@pytest.mark.parametrize("font_pixels", [12, 16, 20])
def test_group_headers_stay_compact_across_font_sizes(qtbot, font_pixels):
    host = QWidget()
    qtbot.addWidget(host)
    layout = QVBoxLayout(host)
    group = ConfigGroup("数据库配置", "database")
    layout.addWidget(group)
    layout.addStretch()

    title = group._title_lbl
    font = title.font()
    font.setPixelSize(font_pixels)
    title.setFont(font)
    host.resize(800, 300)
    host.show()
    QApplication.processEvents()

    header = group._header
    margins = header.layout().contentsMargins()
    assert header.sizePolicy().verticalPolicy() == QSizePolicy.Maximum
    assert header.height() <= max(40, title.fontMetrics().height() + 16)
    assert margins.left() <= 12
    assert margins.top() + margins.bottom() <= 8
    assert title.height() >= title.fontMetrics().height()
    assert title.width() >= title.fontMetrics().horizontalAdvance(title.text())
