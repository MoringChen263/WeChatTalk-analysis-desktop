# -*- coding: utf-8 -*-
"""长期记忆的**首次同意窗**（README.optimized §5.4 / §8）。

设计取舍：
- 说明是一整篇**可滚动、可整段选中复制**的原文，不是几行小字。用户有权看清楚再决定。
- 「同意并启用」按钮**必须先把勾选框打上**才可点。少了这一步，这就是个走过场的形式。
- 默认焦点在「取消」上，回车不会误同意。
- 说明里出现的数字（存哪、多少条、多少字、撤销栈多深）全部取自**官方脚本的常量**，
  不在这里硬编码第二份——否则官方改了上限，这段话就开始撒谎。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QHBoxLayout, QPlainTextEdit, QPushButton, QVBoxLayout,
)

from app.memory import consent as consent_mod
from app.ui._qt import BodyLabel, CaptionLabel, PrimaryPushButton, SubtitleLabel


class ConsentDialog(QDialog):
    """用户点「同意」才返回 Accepted。用于替代裸的 `--confirm`。"""

    def __init__(self, rules=None, parent=None, reason: str = ""):
        super().__init__(parent)
        self.rules = rules
        self.setWindowTitle("启用长期记忆")
        self.setMinimumSize(620, 520)
        self._build(reason)

    def _build(self, reason: str) -> None:
        root = QVBoxLayout(self)
        root.addWidget(SubtitleLabel("启用前，请先看完这一段", self))

        prompt = ("长期记忆能省掉你每次重新交代背景的功夫，但它确实会往本机写东西。"
                  "下面写清了写什么、写在哪、怎么删。")
        if reason:
            prompt = f"{reason}。\n{prompt}"
        lbl = BodyLabel(prompt, self)
        lbl.setWordWrap(True)
        root.addWidget(lbl)

        self.view = QPlainTextEdit(self)
        self.view.setReadOnly(True)
        self.view.setPlainText(consent_mod.policy_text(self.rules))
        self.view.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        root.addWidget(self.view, 1)

        self.chk = QCheckBox("我已了解：记忆只存在本机，我可以随时暂停、撤销或清空", self)
        self.chk.stateChanged.connect(self._sync)
        root.addWidget(self.chk)

        self.hint = CaptionLabel("勾选后才能点「同意并启用」。", self)
        root.addWidget(self.hint)

        row = QHBoxLayout()
        row.addStretch(1)
        self.btn_cancel = QPushButton("取消", self)
        self.btn_cancel.clicked.connect(self.reject)
        self.btn_ok = PrimaryPushButton("同意并启用", self)
        self.btn_ok.setEnabled(False)
        self.btn_ok.clicked.connect(self.accept)
        row.addWidget(self.btn_cancel)
        row.addWidget(self.btn_ok)
        root.addLayout(row)

        # 默认焦点给「取消」，避免回车误同意
        self.btn_cancel.setFocus(Qt.FocusReason.OtherFocusReason)

    def _sync(self) -> None:
        on = self.chk.isChecked()
        self.btn_ok.setEnabled(on)
        self.hint.setText("" if on else "勾选后才能点「同意并启用」。")


__all__ = ["ConsentDialog"]
