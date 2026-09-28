# -*- coding: utf-8 -*-
"""首次启动引导：3 步把「选窗口 → 开始监控 → 确认说话人」讲清楚。

为什么是它（P0-1）：目标用户是非技术的暧昧期用户，打开双栏容易懵。
引导只讲「怎么采到一段可分析的聊天」，不自动开始监控（隐私门是 P0-3 的事），
也不帮忙填 API Key（那是 P0-2）。点「开始使用」后写 onboarding.done，之后不再自动弹；
「跳过」则不打标记，下次启动还会再出现一次。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QApplication, QDialog, QHBoxLayout, QLabel,
                               QStackedWidget, QVBoxLayout, QWidget)

from app.ui import _qt
from app.ui._qt import (BodyLabel, CaptionLabel, PrimaryPushButton,
                        PushButton, SubtitleLabel)
from app.version import APP_DISPLAY_NAME

_STEPS = [
    ("1", "选窗口", "在左侧选好你的微信主窗口"),
    ("2", "开始监控", "点开始监控，实时抓取这段对话"),
    ("3", "确认说话人", "采到消息后确认「我」在哪一侧"),
]

_PAGES = [
    {
        "title": "选好你的微信窗口",
        "lines": [
            "在左侧「会话监控」里，先点「重新扫描」，在下拉框选你的微信主窗口（带 ★ 的一般就是它）。",
            "列表是空的？先把微信打开，再扫一次就好。",
        ],
        "tip": "选错窗口不会出事：监控只在你点「开始监控」之后才真正读取画面。",
    },
    {
        "title": "开始监控，实时抓取对话",
        "lines": [
            "选好窗口后点「开始监控」。工具会持续读取微信消息区的画面并做文字识别，"
            "只取你正在看的这段对话。",
            "右下角「清空 / 新段落」可以随时开始一段新的对话。",
        ],
        "tip": "隐私说明：消息区像素不落盘、不进内存，关掉程序就清空；我们不会上传你的聊天内容。",
    },
    {
        "title": "确认「我」在哪一边",
        "lines": [
            "采到第一条消息后，左侧会给出「我 = 左侧 / 右侧」的建议。点一下确认，"
            "分析才知道哪边是你。",
            "之后去「设置」里填好 AI 后端的 API Key（或试用档），就能出五步分析 + 话术卡了。",
        ],
        "tip": "引导只会自动出现这一次；看完点「开始使用」就能直接用了。",
    },
]


class _StepPill(QWidget):
    """步骤指示小药丸：当前步高亮，其余置灰。"""

    def __init__(self, num: str, text: str, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 6, 10, 6)
        lay.setSpacing(6)
        self.dot = QLabel(num, self)
        self.dot.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.dot.setFixedSize(22, 22)
        self.lbl = BodyLabel(text, self)
        self.lbl.setStyleSheet("background:transparent;")
        lay.addWidget(self.dot)
        lay.addWidget(self.lbl)
        self._active = False
        self._apply()

    def set_active(self, active: bool) -> None:
        if active != self._active:
            self._active = active
            self._apply()

    def _apply(self) -> None:
        if self._active:
            self.setStyleSheet("background:#4f46e5;border-radius:8px;")
            self.dot.setStyleSheet(
                "color:white;background:#4f46e5;border-radius:11px;font-weight:bold;")
            self.lbl.setStyleSheet("color:white;background:transparent;")
        else:
            self.setStyleSheet("background:rgba(0,0,0,.05);border-radius:8px;")
            self.dot.setStyleSheet(
                "color:#666;background:rgba(0,0,0,.10);border-radius:11px;font-weight:bold;")
            self.lbl.setStyleSheet("color:#666;background:transparent;")


class _Page(QWidget):
    """单步内容：标题 + 若干行说明 + 一句提示。"""

    def __init__(self, data: dict, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 10, 4, 4)
        lay.setSpacing(10)

        lay.addWidget(SubtitleLabel(data["title"], self))
        for line in data["lines"]:
            lb = BodyLabel(line, self)
            lb.setWordWrap(True)
            lay.addWidget(lb)

        tip = CaptionLabel("提示：" + data["tip"], self)
        tip.setWordWrap(True)
        tip.setStyleSheet(
            "color:#9a6a00;background:rgba(185,119,14,.10);border-radius:8px;padding:10px;")
        lay.addWidget(tip)
        lay.addStretch(1)


class OnboardingWizard(QDialog):
    """首次启动的 3 步引导对话框（模态）。完成则写 onboarding.done。"""

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config
        self.setWindowTitle(f"欢迎使用 {APP_DISPLAY_NAME}")
        self.setMinimumSize(580, 440)
        self.setModal(True)
        self._build()
        self._go(0)

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 16)

        root.addWidget(SubtitleLabel(f"欢迎使用 {APP_DISPLAY_NAME}", self))
        root.addWidget(BodyLabel("三步走完，你就能拿到第一段可分析的聊天。", self))

        # 步骤指示
        self.pills: list[_StepPill] = []
        pill_row = QHBoxLayout()
        pill_row.setSpacing(8)
        for num, short, _ in _STEPS:
            pill = _StepPill(num, short, self)
            self.pills.append(pill)
            pill_row.addWidget(pill)
        pill_row.addStretch(1)
        root.addLayout(pill_row)

        # 步骤内容
        self.stack = QStackedWidget(self)
        for data in _PAGES:
            self.stack.addWidget(_Page(data, self))
        root.addWidget(self.stack, 1)

        # 底部按钮
        foot = QHBoxLayout()
        self.btn_skip = PushButton("跳过", self)
        self.btn_skip.clicked.connect(self.reject)
        foot.addWidget(self.btn_skip)
        foot.addStretch(1)
        self.btn_back = PushButton("上一步", self)
        self.btn_back.clicked.connect(lambda: self._go(self.stack.currentIndex() - 1))
        self.btn_next = PrimaryPushButton("下一步", self)
        self.btn_next.clicked.connect(lambda: self._go(self.stack.currentIndex() + 1))
        self.btn_finish = PrimaryPushButton("开始使用", self)
        self.btn_finish.clicked.connect(self._finish)
        foot.addWidget(self.btn_back)
        foot.addWidget(self.btn_next)
        foot.addWidget(self.btn_finish)
        root.addLayout(foot)

    def _go(self, idx: int) -> None:
        idx = max(0, min(len(_PAGES) - 1, idx))
        self.stack.setCurrentIndex(idx)
        for i, pill in enumerate(self.pills):
            pill.set_active(i == idx)
        last = idx == len(_PAGES) - 1
        self.btn_back.setVisible(idx > 0)
        self.btn_next.setVisible(not last)
        self.btn_finish.setVisible(last)

    def _finish(self) -> None:
        """完成引导：标记 onboarding.done，下次不再自动弹。"""
        self.config.set("onboarding.done", True)
        try:
            self.config.save()
        except Exception:
            # 写盘失败不应卡住用户进入主界面（标记只是「少弹一次窗」）
            pass
        self.accept()


def maybe_show_onboarding(config, parent=None) -> None:
    """主窗口启动后调用：没引导过就弹一次。任何异常都静默跳过，绝不挡住主界面。"""
    try:
        if config.get("onboarding.done", False):
            return
        OnboardingWizard(config, parent).exec()
    except Exception:  # noqa: BLE001 - 引导挂了也不能影响进主界面
        from app import logging_setup as logs
        logs.warn("首启引导启动失败（已跳过）", exc_info=True)
