# -*- coding: utf-8 -*-
"""M1 GUI 端到端：真主窗口 + 真 worker 线程 + 真微信窗口，验证「点开始监控 → 出 transcript」。

比 m0_gui_smoke 强的地方：它真的把采集线程跑起来、走 Qt 信号回 UI 线程，
并把左栏显示的文本抓回来核对（信号连错、线程里崩了，这里都会暴露）。

    python probe/m1_gui_live.py --seconds 14
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.config import load_config  # noqa: E402
from app.ui import _qt  # noqa: E402

EVENTS: list[str] = []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=14.0)
    args = ap.parse_args()

    cfg = load_config()
    app = QApplication(sys.argv)
    _qt.apply_theme()

    from app.ui.main_window import MainWindow

    win = MainWindow(cfg)
    win.show()
    panel = win.page.capture_panel

    # 把 worker 的关键信号记下来，便于断言「信号通路真的通」
    panel.set_status = _wrap(panel.set_status, "status")

    print(f"HAS_FLUENT={_qt.HAS_FLUENT}  窗口={win.width()}x{win.height()}")
    print(f"左栏下拉项数={panel.window_box.count()} 首项={panel.window_box.itemText(0)[:70]}")

    QTimer.singleShot(300, panel.toggle_monitor)

    def finish() -> None:
        panel.stop_monitor()
        QTimer.singleShot(1200, check)

    def check() -> None:
        t = panel.transcript
        print(f"\n--- 事件（前 8 条）---")
        for e in EVENTS[:8]:
            print("  ·", e[:110])
        if t is None:
            print("❌ 左栏没有拿到 transcript")
            app.exit(1)
            return
        print(f"\n--- transcript ---")
        print(t.to_text())
        errs = t.validate()
        print(f"\n条数={len(t.messages)}　schema={'通过 ✅' if not errs else '失败 ❌ ' + str(errs[:3])}")
        print(f"左栏文本框字符数={len(panel.ocr_text.toPlainText())}（应与 transcript 一致）")
        print(f"预览已贴图={panel.preview.pixmap() is not None and not panel.preview.pixmap().isNull()}")
        ok = bool(t.messages) and not errs and len(panel.ocr_text.toPlainText()) > 0
        print(f"\nGUI 端到端：{'通过 ✅' if ok else '失败 ❌'}")
        app.exit(0 if ok else 1)

    QTimer.singleShot(int(args.seconds * 1000), finish)
    rc = app.exec()
    print(f"GUI 端到端结束 rc={rc}")
    return rc


def _wrap(fn, tag):
    def inner(text):
        EVENTS.append(f"[{tag}] {text}")
        return fn(text)
    return inner


if __name__ == "__main__":
    raise SystemExit(main())
