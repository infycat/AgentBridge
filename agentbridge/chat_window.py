"""The local chat page as a widget in the desktop window."""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QColor, QKeySequence, QShortcut
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QLabel, QPushButton, QStackedWidget, QVBoxLayout, QWidget

from .config import data_dir

logger = logging.getLogger(__name__)


class LocalChatPage(QWebEnginePage):
    def __init__(self, profile: QWebEngineProfile, url: QUrl, parent: QWidget) -> None:
        super().__init__(profile, parent)
        self.origin = (url.scheme(), url.host(), url.port())

    def acceptNavigationRequest(self, address, _kind, _main_frame):
        return (address.scheme(), address.host(), address.port()) == self.origin


class ChatPane(QStackedWidget):
    def __init__(self, url: QUrl, parent: QWidget) -> None:
        super().__init__(parent)
        self.url = url
        self.service_available = False

        # Keep the existing browser profile so the chat remembers its theme.
        profile_path = data_dir() / "webview"
        self.view = QWebEngineView(self)
        self.profile = QWebEngineProfile("AgentBridgeChat", self)
        self.profile.setPersistentStoragePath(str(profile_path / "storage"))
        self.profile.setCachePath(str(profile_path / "cache"))
        self.view.setObjectName("groupChat")
        page = LocalChatPage(self.profile, self.url, self.view)
        page.setBackgroundColor(QColor("#0f171c"))
        self.view.setPage(page)

        self.status = QLabel()
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status.setWordWrap(True)
        self.retry = QPushButton("重新載入")
        self.placeholder = QWidget()
        self.placeholder.setObjectName("chatPlaceholder")
        layout = QVBoxLayout(self.placeholder)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(14)
        layout.addStretch()
        layout.addWidget(self.status)
        layout.addWidget(self.retry, alignment=Qt.AlignmentFlag.AlignHCenter)
        layout.addStretch()

        self.addWidget(self.placeholder)
        self.addWidget(self.view)
        self.show_unavailable("正在啟動 MCP 服務…")

        self.view.loadFinished.connect(self._loaded)
        page.renderProcessTerminated.connect(self._renderer_stopped)
        self.retry.clicked.connect(self.load_chat)
        self.refresh_shortcut = QShortcut(QKeySequence.StandardKey.Refresh, self)
        self.refresh_shortcut.activated.connect(self.load_chat)

    def show_unavailable(self, message: str, *, retry: bool = False) -> None:
        self.status.setText(message)
        self.retry.setVisible(retry)
        self.setCurrentWidget(self.placeholder)

    def set_service_available(self, available: bool, message: str = "") -> None:
        self.service_available = available
        if available:
            self.load_chat()
        else:
            self.view.stop()
            self.show_unavailable(message)

    def load_chat(self) -> None:
        if self.service_available:
            self.show_unavailable("正在載入公司聊天室…")
            self.view.load(self.url)

    def _loaded(self, ok: bool) -> None:
        if not self.service_available:
            return
        if ok:
            self.setCurrentWidget(self.view)
            logger.info("Embedded chat loaded: %s", self.view.title())
        else:
            logger.warning("Embedded chat could not load %s", self.url.toString())
            self.show_unavailable("無法載入公司聊天室。", retry=True)

    def _renderer_stopped(self, status, code: int) -> None:
        logger.warning("Chat renderer stopped (%s, %s)", status, code)
        if self.service_available:
            self.show_unavailable("內建瀏覽器已停止。請重新載入聊天室。", retry=True)
