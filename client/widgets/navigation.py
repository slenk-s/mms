"""导航组件：左侧垂直导航栏，包含页面切换功能和用户信息。"""
import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QLabel,
    QPushButton,
    QSizePolicy,
    QSpacerItem,
    QVBoxLayout,
    QWidget,
)

from config import IMAGES_DIR
from i18n import tr


class NavigationBar(QWidget):
    """左侧导航栏组件。"""

    EXPANDED_MIN_WIDTH = 200
    EXPANDED_MAX_WIDTH = 240
    page_changed = Signal(str)
    logout_clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("navBar")
        self._buttons: dict = {}
        self._cached_username = ""
        self._cached_role = ""
        self._cached_lib_name = ""
        self._init_ui()
        self._apply_expanded_presentation()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 16, 12, 16)
        layout.setSpacing(4)

        logo_label = QLabel()
        logo_label.setAlignment(Qt.AlignCenter)
        logo_label.setStyleSheet("padding: 8px;")
        logo_path = os.path.join(IMAGES_DIR, "tcl_logo.png")
        if os.path.exists(logo_path):
            pixmap = QPixmap(logo_path)
            if not pixmap.isNull():
                logo_label.setPixmap(
                    pixmap.scaled(
                        120, 50, Qt.KeepAspectRatio, Qt.SmoothTransformation
                    )
                )
        layout.addWidget(logo_label)

        line = QLabel()
        line.setStyleSheet(
            "background-color: #e0e4e8; min-height: 1px; max-height: 1px;"
        )
        line.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        layout.addWidget(line)

        self.nav_items = [
            ("inventory", "inventory"),
            ("register", "register"),
            ("asset", "asset"),
            ("config", "config"),
            ("user_manage", "user_manage"),
        ]
        for label_key, page_id in self.nav_items:
            btn = QPushButton(tr(f"nav.{label_key}"))
            btn.setObjectName("navButton")
            btn.setCheckable(True)
            btn.clicked.connect(lambda checked, pid=page_id: self._on_nav_clicked(pid))
            self._buttons[page_id] = btn
            layout.addWidget(btn)

        layout.addSpacerItem(
            QSpacerItem(20, 40, QSizePolicy.Minimum, QSizePolicy.Expanding)
        )

        line2 = QLabel()
        line2.setStyleSheet(
            "background-color: #e0e4e8; min-height: 1px; max-height: 1px;"
        )
        line2.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        layout.addWidget(line2)

        self.user_label = QLabel(tr("nav.not_logged_in"))
        self.user_label.setStyleSheet("color: #5a6573; font-size: 12px; padding: 4px;")
        self.user_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.user_label)

        self.logout_btn = QPushButton(tr("nav.logout"))
        self.logout_btn.setStyleSheet("""
            QPushButton {
                color: #dc2626;
                font-size: 12px;
                border: 1px solid #fecaca;
                border-radius: 4px;
                padding: 4px 8px;
                background: transparent;
            }
            QPushButton:hover {
                background-color: #fef2f2;
                border-color: #f87171;
            }
        """)
        self.logout_btn.clicked.connect(self.logout_clicked.emit)
        layout.addWidget(self.logout_btn)

        self.lib_label = QLabel(tr("nav.local_library", name="模组机架一组"))
        self.lib_label.setStyleSheet("color: #9ca3af; font-size: 11px; padding: 4px;")
        self.lib_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.lib_label)

    def set_collapsed(self, collapsed: bool, persist: bool = True):
        """兼容旧调用；导航栏始终以完整文字展开显示。"""
        self._apply_expanded_presentation()

    def is_collapsed(self) -> bool:
        """导航栏不再支持折叠。"""
        return False

    def _apply_expanded_presentation(self):
        self.setMinimumWidth(self.EXPANDED_MIN_WIDTH)
        self.setMaximumWidth(self.EXPANDED_MAX_WIDTH)
        for page_id, button in self._buttons.items():
            label = tr(f"nav.{page_id}")
            button.setText(label)
            button.setToolTip(label)
            button.setAccessibleName(label)

        logout_label = tr("nav.logout")
        self.logout_btn.setText(logout_label)
        self.logout_btn.setToolTip(logout_label)
        self.logout_btn.setAccessibleName(logout_label)
        self.user_label.setVisible(True)
        self.lib_label.setVisible(True)

    def _on_nav_clicked(self, page_id: str):
        """导航按钮点击事件。"""
        for pid, btn in self._buttons.items():
            if pid != page_id:
                btn.setChecked(False)
        self._buttons[page_id].setChecked(True)
        self.page_changed.emit(page_id)

    def set_active_page(self, page_id: str):
        """设置当前激活页面。"""
        for pid, btn in self._buttons.items():
            btn.setChecked(pid == page_id)

    def set_library_name(self, name: str):
        """设置本地库名显示。"""
        self._cached_lib_name = name
        self.lib_label.setText(tr("nav.local_library", name=name))

    def set_user_info(self, username: str, role: str):
        """设置用户信息显示。"""
        self._cached_username = username
        self._cached_role = role
        role_text = tr("nav.role_admin") if role == "admin" else tr("nav.role_user")
        self.user_label.setText(f"👤 {username}\n[{role_text}]")
        self.user_label.setStyleSheet("""
            color: #1f2937;
            font-size: 12px;
            padding: 4px;
            font-weight: bold;
        """)

    def clear_user_info(self):
        """清空用户信息。"""
        self._cached_username = ""
        self._cached_role = ""
        self.user_label.setText(tr("nav.not_logged_in"))
        self.user_label.setStyleSheet("color: #5a6573; font-size: 12px; padding: 4px;")

    def set_admin_visible(self, visible: bool):
        """设置管理员专属导航项的可见性。"""
        for page_id in ("user_manage", "config"):
            if page_id in self._buttons:
                self._buttons[page_id].setVisible(visible)

    def retranslate_ui(self):
        """重新应用当前语言的文本。"""
        if self._cached_username:
            self.set_user_info(self._cached_username, self._cached_role)
        else:
            self.user_label.setText(tr("nav.not_logged_in"))
        if self._cached_lib_name:
            self.set_library_name(self._cached_lib_name)
        else:
            self.lib_label.setText(tr("nav.local_library", name="模组机架一组"))
        self._apply_expanded_presentation()
