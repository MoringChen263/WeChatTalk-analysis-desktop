# -*- coding: utf-8 -*-
"""快照当前微信聊天区：把每一行的「判定方 / x 相对位置 / 置信度 / 文本」原样打出来。

用途：核对「某条消息到底是我发的还是对方发的」。判据是气泡底色（绿=我），
x 位置只用来交叉验证自洽性——两者不一致就说明判定可疑。

不落盘任何截图（红线）。
    python probe/snapshot_chat.py
"""
from __future__ import annotations

import ctypes
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.capture import ocr, window_capture, window_finder  # noqa: E402
from app.config import load_config  # noqa: E402

ctypes.windll.user32.SetProcessDPIAware()


def main() -> int:
    cfg = load_config()
    procs = cfg.get("capture.target_process", ["Weixin.exe", "WeChat.exe"])
    wins = window_finder.enum_windows(procs)
    win = window_finder.pick_main_window(
        wins, cfg.get("capture.title_hint", "微信"), cfg.get("capture.window_class_fallback", ""))
    if win is None:
        print("没找到微信主窗口")
        return 2
    print(f"窗口 hwnd={win.hwnd} {win.cls} {win.size} "
          f"visible={win.visible} iconic={win.iconic}")

    cap = window_capture.Capture(win.hwnd)
    frame = None
    deadline = time.perf_counter() + 15.0
    while time.perf_counter() < deadline:
        frame = cap.settled()
        if frame is not None:
            break
        time.sleep(0.1)
    if frame is None:
        print(f"等不到稳定帧（status={cap.status}）")
        cap.stop()
        return 3
    frame = np.ascontiguousarray(frame)
    area = window_capture.chat_area(frame)
    if area is None:
        print("认不出聊天区布局")
        cap.stop()
        return 4
    x0, y_top, x1, y_in, pane_bg, y_pane = area
    chat = frame[y_top:y_in, x0:x1]
    print(f"聊天区 x=[{x0},{x1}) y=[{y_top},{y_in}) 宽 {chat.shape[1]} 高 {chat.shape[0]} "
          f"底色={[int(v) for v in pane_bg]}")

    reader = ocr.Reader()
    lines = reader.read(chat, pane_bg, ocr_min_score=cfg.get("capture.ocr_min_score", 0.9))
    title = ocr.read_title(window_capture.header_area(frame, area)) if hasattr(ocr, "read_title") else None
    print(f"会话名={title!r}　本帧气泡行 {len(lines)} 条（同气泡多行已合并）\n")
    print(f"{'判定':<5} {'x_rel':>6} {'位置':<5} {'置信':>8}  {'y':>5}  文本")
    print("-" * 92)
    for l in lines:
        pos = "右" if l.x_rel > 0.5 else "左"
        print(f"{l.who:<5} {l.x_rel:>6.2f} {pos:<5} {l.score:>8.4f}  "
              f"{l.y_top:>5.0f}  {l.text[:56]}")

    print(f"\n灰度行（疑似时间戳）：{[(t, round(y)) for t, y in reader.grays]}")
    print(f"正常气泡字高 lh = {reader.lh}")

    me = [l for l in lines if l.who == "me"]
    her = [l for l in lines if l.who == "her"]
    if me:
        print(f"\n我（绿底）：{len(me)} 条，x_rel 均值 {np.mean([l.x_rel for l in me]):.2f}（期望 > 0.5）")
        print("  " + " | ".join(f"{l.text[:16]}({l.x_rel:.2f})" for l in me[-5:]))
    if her:
        print(f"对方（灰底）：{len(her)} 条，x_rel 均值 {np.mean([l.x_rel for l in her]):.2f}（期望 < 0.5）")
        print("  " + " | ".join(f"{l.text[:16]}({l.x_rel:.2f})" for l in her[-5:]))
    cap.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
