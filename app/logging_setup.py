# -*- coding: utf-8 -*-
"""日志与崩溃兜底（E36）。

**为什么必须单独有这一层**：打成 `--windowed` 之后进程没有控制台，

- `sys.stdout is None`，CPython 的 `print()` 会**静默返回**——不是报错，是丢掉。
  界面上「设置入口挂载失败」这种 warning 就此人间蒸发；
- 启动期任何未捕获异常会让进程**无声退出**，用户看到的现象是「双击没反应」，
  连一句错误都没有，根本无从排查。

所以：GUI 路径一律不用 `print`，走 `log()`；未捕获异常（主线程 + worker 线程）
统一记进 `%APPDATA%/jev-chat-analyzer/logs/app.log`，并在 Qt 存活时弹窗告诉用户日志在哪。
"""
from __future__ import annotations

import logging
import sys
import threading
import traceback
from logging.handlers import RotatingFileHandler

_LOGGER_NAME = "jev"
_ready = False
_in_dialog = False  # 弹窗防重入：崩溃处理里再崩一次不能无限弹


def log_path():
    from app import paths

    return paths.log_path()


def setup(level: int = logging.INFO) -> logging.Logger:
    """装配日志（幂等）。拿不到日志文件时**降级为只往 stderr 打**，绝不因此让程序起不来。"""
    global _ready
    logger = logging.getLogger(_LOGGER_NAME)
    if _ready:
        return logger

    logger.setLevel(level)
    logger.propagate = False
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s [%(threadName)s] %(message)s")

    try:
        handler = RotatingFileHandler(log_path(), maxBytes=1_000_000, backupCount=3,
                                      encoding="utf-8")
        handler.setFormatter(fmt)
        logger.addHandler(handler)
    except OSError:
        pass  # 数据目录不可写也不该拖垮启动

    if sys.stderr is not None:  # 源码运行时也想在终端直接看到
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(fmt)
        logger.addHandler(stream)

    _ready = True
    logger.info("日志就绪：%s", log_path())
    return logger


def log(msg: str, *, level: int = logging.INFO, exc_info: bool = False) -> None:
    setup().log(level, msg, exc_info=exc_info)


def warn(msg: str, exc_info: bool = False) -> None:
    log(msg, level=logging.WARNING, exc_info=exc_info)


def error(msg: str, exc_info: bool = False) -> None:
    log(msg, level=logging.ERROR, exc_info=exc_info)


def _show_crash_dialog(exc_type, exc, tb) -> None:
    """有 Qt 就弹个能看的窗；没有就什么都不做（日志已经写了）。"""
    global _in_dialog
    if _in_dialog:
        return
    _in_dialog = True
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox

        if QApplication.instance() is None:
            return
        box = QMessageBox()
        box.setIcon(QMessageBox.Critical)
        box.setWindowTitle("出现未预期的错误")
        box.setText(f"{exc_type.__name__}: {exc}")
        box.setInformativeText(f"程序可能无法继续正常工作。\n详细信息已写入日志：\n{log_path()}")
        box.setDetailedText("".join(traceback.format_exception(exc_type, exc, tb)))
        box.exec()
    except Exception:  # noqa: BLE001 - 崩溃处理自身绝不能再抛
        pass
    finally:
        _in_dialog = False


def install_excepthook() -> None:
    """接管主线程与 worker 线程的未捕获异常。

    装 `threading.excepthook` 是必需的，不是锦上添花：采集、分析、记忆召回
    全跑在 worker 线程里，而线程里的异常**不会**经过 `sys.excepthook`。
    """
    logger = setup()

    def _main_hook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        logger.critical("未捕获异常", exc_info=(exc_type, exc, tb))
        _show_crash_dialog(exc_type, exc, tb)

    sys.excepthook = _main_hook

    def _thread_hook(args) -> None:
        if issubclass(args.exc_type, SystemExit):
            return
        name = args.thread.name if args.thread is not None else "?"
        logger.critical("worker 线程 %s 未捕获异常", name,
                        exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    threading.excepthook = _thread_hook  # type: ignore[assignment]
