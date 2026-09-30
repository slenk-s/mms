"""同步状态栏手动同步 UX 的真实 PySide6 离屏回归测试。"""
import os
import subprocess
import sys
import textwrap
from pathlib import Path


CLIENT_DIR = Path(__file__).resolve().parents[1] / "client"


def test_sync_status_bar_manual_sync_ux_offscreen():
    """验证初始数量、模式保护、防重入及同步结束后的 UI 恢复。"""
    script = textwrap.dedent(
        """
        from PySide6.QtCore import QObject, Signal
        from PySide6.QtWidgets import QApplication, QMainWindow, QStatusBar
        from main import MainWindow
        from widgets.sync_status_bar import SyncStatusBar
        import widgets.toast as toast

        class FakeLocalDB:
            def __init__(self, count):
                self.count = count
                self.reads = 0

            def get_sync_queue_count(self):
                self.reads += 1
                return self.count

        class FakeEngine(QObject):
            sync_started = Signal(str)
            sync_completed = Signal(str, int)
            sync_failed = Signal(str, str)
            sync_status_changed = Signal(str)
            queue_updated = Signal(int)
            batch_conflict_detected = Signal(object)
            STOPPED = "STOPPED"
            PAUSED = "PAUSED"
            RUNNING = "RUNNING"

            def __init__(self, state="RUNNING"):
                super().__init__()
                self.state = state
                self.calls = 0

            def force_sync(self):
                self.calls += 1

        app = QApplication([])
        window = MainWindow.__new__(MainWindow)
        QMainWindow.__init__(window)
        window.local_db = FakeLocalDB(7)
        window.sync_engine = FakeEngine()
        window.sync_status_bar = SyncStatusBar()
        window.status_bar = QStatusBar()
        window.setStatusBar(window.status_bar)
        window._app_mode = "online"
        window._force_sync_in_progress = False
        feedback = []
        toast.toast_info = lambda _parent, message: feedback.append(message)

        window._connect_sync_signals()
        window._refresh_sync_queue_count()
        assert window.sync_status_bar.queue_label.text() == "待同步: 7"

        window._on_force_sync_clicked()
        assert window.sync_engine.calls == 1
        assert window.sync_status_bar.sync_btn.text() == "同步中..."
        assert not window.sync_status_bar.sync_btn.isEnabled()

        window._on_force_sync_clicked()
        assert window.sync_engine.calls == 1

        window.local_db.count = 2
        window.sync_engine.sync_completed.emit("cycle", 0)
        app.processEvents()
        assert window.sync_status_bar.sync_btn.isEnabled()
        assert window.sync_status_bar.sync_btn.text() == "立即同步"
        assert window.sync_status_bar.queue_label.text() == "待同步: 2"

        window._app_mode = "offline"
        window._on_force_sync_clicked()
        assert window.sync_engine.calls == 1
        assert feedback
        assert window.sync_status_bar.sync_label.text() == "同步: 离线模式"

        window._app_mode = "online"
        window.sync_engine.state = window.sync_engine.STOPPED
        window._on_force_sync_clicked()
        assert window.sync_engine.calls == 1
        assert len(feedback) == 2

        window.sync_engine.state = window.sync_engine.PAUSED
        window._on_force_sync_clicked()
        assert window.sync_engine.calls == 2
        window.local_db.count = 0
        window.sync_engine.sync_failed.emit("push", "network")
        app.processEvents()
        assert window.sync_status_bar.sync_btn.isEnabled()
        assert window.sync_status_bar.queue_label.text() == "待同步: 0"
        """
    )
    env = os.environ.copy()
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["PYTHONPATH"] = str(CLIENT_DIR)
    result = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True,
        text=True, timeout=20, check=False,
    )
    assert result.returncode == 0, result.stderr
