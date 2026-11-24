"""The AgentBridge desktop window and system tray."""

from __future__ import annotations

import logging
import os
from concurrent.futures import Future, ThreadPoolExecutor
from hashlib import sha256
from time import monotonic, sleep

from PySide6.QtCore import QLockFile, QTimer, QUrl, Qt
from PySide6.QtGui import QIcon, QKeySequence
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QMainWindow, QMenu, QMessageBox, QProgressBar, QPushButton,
    QSystemTrayIcon, QVBoxLayout, QWidget,
)

from .chat_window import ChatPane
from .config import app_icon_path, data_dir
from .server import ServerController
from .store import BridgeStore

logger = logging.getLogger(__name__)
WINDOW_TITLE = "AgentBridge AI 軟體公司"


def run_viewer(port: int) -> int:
    """Open the embedded chat without starting a server or opening its database."""
    app = QApplication(["AgentBridge"])
    icon = QIcon(str(app_icon_path()))
    app.setWindowIcon(icon)
    window = QMainWindow()
    window.setWindowTitle(WINDOW_TITLE)
    window.setWindowIcon(icon)
    window.resize(1130, 760)
    chat = ChatPane(QUrl(f"http://127.0.0.1:{port}/"), window)
    window.setCentralWidget(chat)
    window.setStyleSheet(STYLES)
    window.show()
    QTimer.singleShot(0, lambda: chat.set_service_available(True))
    return app.exec()

STYLES = """
#projectHeader { background: #142322; color: #edf5f3; border-bottom: 1px solid #34504b; }
#projectHeader QLabel { color: #edf5f3; }
#projectTitle { font-size: 18px; font-weight: 700; }
#mcpStatus { font-size: 12px; }
#workstationBadge { font-size: 12px; background: #244b46; border-radius: 6px; padding: 4px 8px; }
#stageProgress { color: #edf5f3; text-align: center; background: #243432; border: 1px solid #52847a; border-radius: 5px; }
#stageProgress::chunk { background: #518d7e; border-radius: 4px; }
#chatPlaceholder { background: #0f171c; color: #e9edef; }
#chatPlaceholder QLabel { color: #e9edef; font-size: 14px; }
#projectHeader QPushButton, #chatPlaceholder QPushButton {
  background: #244b46; color: #f1f9f7; border: 1px solid #52847a;
  border-radius: 8px; padding: 8px 14px; min-height: 26px;
}
#projectHeader QPushButton:hover, #chatPlaceholder QPushButton:hover { background: #305f56; }
#projectHeader QPushButton:focus, #chatPlaceholder QPushButton:focus { border: 2px solid #9be3ce; }
#projectHeader QPushButton:disabled { background: #243432; color: #b7c8c5; border-color: #405653; }
"""


class ProjectWindow(QMainWindow):
    def __init__(self, controller: ServerController, url: QUrl, icon: QIcon) -> None:
        super().__init__()
        self.controller = controller
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="AgentBridge-Service")
        self.operation: Future[None] | None = None
        self.quitting = False
        self.setWindowTitle(WINDOW_TITLE)
        self.setWindowIcon(icon)
        self.resize(1130, 760)
        self.setMinimumSize(540, 450)

        group = QWidget()
        layout = QVBoxLayout(group)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.setCentralWidget(group)

        header = QFrame()
        header.setObjectName("projectHeader")
        bar = QHBoxLayout(header)
        bar.setContentsMargins(16, 9, 16, 9)
        bar.setSpacing(12)
        logo = QLabel()
        logo.setPixmap(icon.pixmap(42, 42))
        bar.addWidget(logo)
        labels = QVBoxLayout()
        labels.setSpacing(2)
        title = QLabel(WINDOW_TITLE)
        title.setObjectName("projectTitle")
        labels.addWidget(title)
        self.status = QLabel()
        self.status.setObjectName("mcpStatus")
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        labels.addWidget(self.status)
        bar.addLayout(labels, 1)
        self.workstation_badge = QLabel()
        self.workstation_badge.setObjectName("workstationBadge")
        self.workstation_badge.hide()
        bar.addWidget(self.workstation_badge)
        self.stage_progress = QProgressBar()
        self.stage_progress.setObjectName("stageProgress")
        self.stage_progress.setAccessibleName("目前階段進度")
        self.stage_progress.setMinimumWidth(100)
        self.stage_progress.setMaximumWidth(190)
        self.stage_progress.hide()
        bar.addWidget(self.stage_progress)
        self.service_button = QPushButton()
        self.service_button.setObjectName("mcpServiceButton")
        self.service_button.clicked.connect(self.toggle_service)
        bar.addWidget(self.service_button)
        layout.addWidget(header)

        self.chat = ChatPane(url, group)
        layout.addWidget(self.chat, 1)
        self.setStyleSheet(STYLES)

        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self.finish_service_change)
        self.workstations_timer = QTimer(self)
        self.workstations_timer.timeout.connect(self.update_company_status)
        self.workstations_timer.start(5000)

        quit_action = self.addAction("結束 AgentBridge")
        quit_action.setShortcut(QKeySequence("Ctrl+Q"))
        quit_action.triggered.connect(self.quit)
        self.tray = None
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = QSystemTrayIcon(icon, self)
            self.tray.setToolTip(WINDOW_TITLE)
            self.tray_menu = QMenu(self)
            self.tray_menu.addAction("顯示AI 軟體公司", self.show_project)
            self.tray_service = self.tray_menu.addAction("", self.toggle_service)
            self.tray_channel = self.tray_menu.addAction("", self.controller.store.toggle_channel)
            self.tray_menu.addAction("重置會話…", self.reset_session)
            self.tray_menu.aboutToShow.connect(self.update_tray_channel)
            self.tray_menu.addSeparator()
            self.tray_menu.addAction(quit_action)
            self.tray.setContextMenu(self.tray_menu)
            self.tray.activated.connect(self.tray_activated)
            self.tray.show()
        self.update_service_controls("MCP 已停止")

    def update_tray_channel(self) -> None:
        enabled = self.controller.store.channel_enabled
        self.tray_channel.setText("關閉同步通道" if enabled else "開啟同步通道")

    def update_company_status(self) -> None:
        if not self.controller.running or not self.isVisible():
            self.workstation_badge.hide()
            self.stage_progress.hide()
            return
        seats = self.controller.store.company.workstations()
        occupied = [seat["title"] for seat in seats if seat["assigned_agent_id"]]
        self.workstation_badge.setText(f"{len(occupied)} / {len(seats)} 工位入座")
        self.workstation_badge.setToolTip("、".join(occupied) or "目前沒有入座員工")
        self.workstation_badge.show()
        roadmap = self.controller.store.company.roadmap.state(grouped=False)
        current = next((stage for stage in roadmap["stages"] if stage["status"] == "in_progress"), None)
        goal = roadmap["active_goal"] or roadmap["completed_goal"]
        self.stage_progress.setVisible(bool(goal))
        if current:
            self.stage_progress.setValue(current["progress_percent"])
            self.stage_progress.setFormat(f"階段 {current['stage_order']} · %p%")
            self.stage_progress.setToolTip(current["title"])
        elif goal:
            completed = bool(roadmap["completed_goal"])
            self.stage_progress.setValue(100 if completed else 0)
            self.stage_progress.setFormat("全部完成 · 100%" if completed else "等待規劃")
            self.stage_progress.setToolTip(goal["title"])

    def reset_session(self) -> None:
        self.show_project()
        if QMessageBox.question(self, "重置會話", "清除本次會話的訊息與檔案鎖？") == QMessageBox.StandardButton.Yes:
            self.controller.store.reset_session()

    def update_service_controls(self, status: str) -> None:
        busy = self.operation is not None
        toggle = "停止 MCP" if self.controller.running else "啟動 MCP"
        self.status.setText(status)
        self.service_button.setText(toggle)
        self.service_button.setDisabled(busy)
        if self.tray is not None:
            self.tray_service.setText(toggle)
            self.tray_service.setDisabled(busy)

    def toggle_service(self) -> None:
        if self.operation is not None:
            return
        starting = not self.controller.running
        if starting:
            self.chat.show_unavailable("正在啟動 MCP 服務…")
        self.operation = self.worker.submit(self.controller.start if starting else self.controller.stop)
        self.update_service_controls("MCP 啟動中…" if starting else "MCP 停止中…")
        self.timer.start()

    def finish_service_change(self) -> None:
        if self.operation is None or not self.operation.done():
            return
        self.timer.stop()
        operation, self.operation = self.operation, None
        try:
            operation.result()
        except Exception as exc:
            logger.exception("Could not change the MCP service state")
            self.update_service_controls("MCP 無法啟動")
            self.chat.set_service_available(False, f"無法啟動 MCP 服務。\n{exc}")
            return
        if self.controller.running:
            self.update_service_controls(f"MCP 運行中 · {self.chat.url.toString()}mcp")
            self.chat.set_service_available(True)
        else:
            self.update_service_controls("MCP 已停止")
            self.chat.set_service_available(False, "MCP 已停止。點擊「啟動 MCP」即可恢復聊天室。")
        self.update_company_status()

    def show_project(self) -> None:
        if self.isMinimized():
            self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()

    def tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self.show_project()

    def quit(self) -> None:
        self.quitting = True
        QApplication.quit()

    def closeEvent(self, event) -> None:
        if self.tray is not None and not self.quitting:
            event.ignore()
            self.hide()
        else:
            super().closeEvent(event)


def run_desktop(store: BridgeStore, host: str, port: int) -> int:
    app = QApplication(["AgentBridge"])
    icon = QIcon(str(app_icon_path()))
    app.setWindowIcon(icon)
    lock_path = data_dir() / f"desktop-{port}.lock"
    endpoint = "AgentBridge-" + sha256(os.path.normcase(str(lock_path.resolve())).encode()).hexdigest()[:24]
    lock = QLockFile(str(lock_path))
    lock.setStaleLockTime(0)
    if not lock.tryLock(0):
        if lock.error() == QLockFile.LockError.LockFailedError:
            deadline = monotonic() + 3
            client = QLocalSocket()
            while monotonic() < deadline:
                client.connectToServer(endpoint)
                if client.waitForConnected(100) and (client.bytesAvailable() or client.waitForReadyRead(400)):
                    if client.read(1) == b"\x01":
                        return 0
                client.abort()
                sleep(0.05)
            QMessageBox.information(None, WINDOW_TITLE, "AgentBridge 已在執行。可從系統匣開啟原本的視窗。")
        else:
            logger.error("Could not open desktop lock at %s: %s", lock_path, lock.error())
            QMessageBox.warning(None, WINDOW_TITLE, "無法建立 AgentBridge 啟動鎖。請查看錯誤記錄。")
        return 1

    activation = QLocalServer()
    try:
        QLocalServer.removeServer(endpoint)
        activation.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        if not activation.listen(endpoint):
            logger.warning("Could not register desktop activation: %s", activation.errorString())
        controller = ServerController(store, host, port, enable_replies=True)
        loopback = "[::1]" if host in {"::", "::1"} else "127.0.0.1"
        window = ProjectWindow(controller, QUrl(f"http://{loopback}:{port}/"), icon)

        def show_existing() -> None:
            window.show_project()
            while activation.hasPendingConnections():
                connection = activation.nextPendingConnection()
                connection.disconnected.connect(connection.deleteLater)
                connection.write(b"\x01")
                connection.disconnectFromServer()

        activation.newConnection.connect(show_existing)
        window.show()
        QTimer.singleShot(0, window.toggle_service)
        try:
            return app.exec()
        finally:
            window.worker.shutdown(wait=True)
            controller.stop()
    finally:
        activation.close()
        lock.unlock()
