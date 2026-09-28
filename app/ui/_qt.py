# -*- coding: utf-8 -*-
"""控件别名层：优先用 qfluentwidgets（视觉与 jev-chat-windows 对齐），
不可用时回落到原生 PySide6。其余 UI 代码只 import 本模块，不直接 import 两个库。
"""
from __future__ import annotations

HAS_FLUENT = False

try:  # pragma: no cover - 取决于运行环境
    from qfluentwidgets import (  # type: ignore
        BodyLabel, CardWidget, CaptionLabel, ComboBox, FluentIcon, FluentWindow,
        InfoBar, InfoBarPosition, NavigationItemPosition, PlainTextEdit,
        PrimaryPushButton, PushButton, StrongBodyLabel, SubtitleLabel, TextEdit,
        Theme, setTheme,
    )

    HAS_FLUENT = True
except Exception:  # pragma: no cover
    from PySide6.QtWidgets import (  # type: ignore
        QComboBox as ComboBox, QFrame as CardWidget, QLabel as BodyLabel,
        QLabel as CaptionLabel, QLabel as StrongBodyLabel, QLabel as SubtitleLabel,
        QPlainTextEdit as PlainTextEdit, QPlainTextEdit as TextEdit,
        QPushButton as PrimaryPushButton, QPushButton as PushButton,
    )

    class _Pos:  # type: ignore
        TOP = "top"
        BOTTOM = "bottom"
        SCROLL = "scroll"

    NavigationItemPosition = _Pos  # type: ignore
    FluentIcon = None  # type: ignore
    FluentWindow = None  # type: ignore
    InfoBar = None  # type: ignore
    InfoBarPosition = None  # type: ignore
    Theme = None  # type: ignore

    def setTheme(*_a, **_k) -> None:  # type: ignore
        return None


def apply_theme() -> None:
    if HAS_FLUENT:
        try:
            setTheme(Theme.AUTO)  # type: ignore[attr-defined]
        except Exception:
            pass


def notify(parent, title: str, content: str, ok: bool = True, ms: int = 4000) -> None:
    """统一的浮层提示；无 qfluentwidgets 时降级为写日志（不再用 print —— 打包后没有 stdout）。"""
    if HAS_FLUENT and InfoBar is not None:
        try:
            factory = InfoBar.success if ok else InfoBar.warning
            factory(title=title, content=content, orient=1, isClosable=True,
                    position=InfoBarPosition.TOP, duration=ms, parent=parent)
            return
        except Exception:
            pass
    from app import logging_setup as logs

    if ok:
        logs.log(f"[OK] {title}: {content}")
    else:
        logs.warn(f"[WARN] {title}: {content}")
