# -*- coding: utf-8 -*-
"""主窗口：左栏会话监控 / 右栏分析 + 话术卡，顶栏设置与档案。"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QSplitter, QVBoxLayout, QWidget

from app import logging_setup as logs
from app.ui import _qt
from app.ui._qt import BodyLabel, FluentWindow, PushButton, SubtitleLabel
from app.ui.analysis_panel import AnalysisPanel
from app.ui.capture_panel import CapturePanel
from app.ui.profile_panel import ProfilePanel
from app.version import APP_DISPLAY_NAME, __version__


class AnalyzerPage(QWidget):
    """左右双栏主页面。"""

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.capture_panel = CapturePanel(config, self)
        self.analysis_panel = AnalysisPanel(config, self)
        # 左栏每拿到一份新 transcript（采集或导入）就推给右栏，右栏据此刷新预检
        self.capture_panel.transcriptReady.connect(self.analysis_panel.set_transcript)

        split = QSplitter(Qt.Orientation.Horizontal, self)
        split.addWidget(self.capture_panel)
        split.addWidget(self.analysis_panel)
        split.setStretchFactor(0, 4)
        split.setStretchFactor(1, 6)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(split)


class MainWindow(FluentWindow if _qt.HAS_FLUENT else QWidget):  # type: ignore[misc]
    def __init__(self, config):
        super().__init__()
        self.config = config
        self.setWindowTitle(f"{APP_DISPLAY_NAME} v{__version__}")
        self.resize(1180, 760)

        self.page = AnalyzerPage(config, self)
        self.page.setObjectName("analyzerPage")  # FluentWindow 要求子界面必须有 objectName
        self.profile = ProfilePanel(config, self)
        self.profile.setObjectName("profilePage")

        if _qt.HAS_FLUENT:
            from app.ui._qt import FluentIcon

            self.addSubInterface(self.page, FluentIcon.HOME, "分析台")
            self.addSubInterface(self.profile, FluentIcon.PEOPLE, "档案")
            self._add_settings_entry()
        else:
            # 原生回落：一个简单的上下/左右布局，功能等价
            lay = QVBoxLayout(self)
            split = QSplitter(Qt.Orientation.Vertical, self)
            split.addWidget(self.page)
            split.addWidget(self.profile)
            lay.addWidget(split)

    def _add_settings_entry(self) -> None:
        """设置入口常驻（密钥、base_url、模型、本地模式）。M3/M4 接入真实表单。"""
        from app.ui._qt import FluentIcon, NavigationItemPosition

        try:
            self.navigationInterface.addItem(
                routeKey="settings",
                icon=FluentIcon.SETTING,
                text="设置",
                onClick=self.show_settings,
                selectable=False,
                position=NavigationItemPosition.BOTTOM,
            )
        except Exception as e:  # 导航项挂不上不该拦住程序启动
            logs.warn(f"设置入口挂载失败：{e}", exc_info=True)

    def show_settings(self) -> None:
        """模型后端、密钥、上下文预算。保存后右栏立刻按新配置重新预检。"""
        from app.ui.settings_dialog import SettingsDialog

        dlg = SettingsDialog(self.config, self)
        if dlg.exec():
            _qt.notify(self, "已保存", "设置已写入 config.json，右栏预检已刷新。")
        try:
            self.page.analysis_panel._refresh_plan()
        except Exception as e:
            logs.warn(f"刷新分析预检失败：{e}", exc_info=True)
