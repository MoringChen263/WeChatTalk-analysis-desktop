# -*- coding: utf-8 -*-
"""诊断 WGC 帧送达率：窗口被遮住/在后台时，微信到底还在不在画？

背景：M1 增量监听里，420 秒只拿到 7 个「内容变化帧」，新消息的发现时间比微信
自带时间戳晚了十几秒到几十秒。可能是：
  (a) 画面确实静止（设计上不出帧）——无害；
  (b) 微信被遮挡后 Chromium 触发 occlusion 节流、停止重绘——**产品级风险**，
      意味着用户一边干活一边监控时，新消息会延迟到微信重新可见才被采到。

区分办法：看整帧哈希（含闪烁的输入光标）随时间变不变。
光标在闪 = 微信还在画 = 帧是新鲜的；整帧冻结 = 微信停止呈现，帧是旧内容。

    python probe/diag_framerate.py --seconds 20
"""
from __future__ import annotations

import argparse
import ctypes
import sys
import time
import zlib
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.capture import window_capture, window_finder  # noqa: E402
from app.config import load_config  # noqa: E402

ctypes.windll.user32.SetProcessDPIAware()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=20.0)
    args = ap.parse_args()

    cfg = load_config()
    procs = cfg.get("capture.target_process", ["Weixin.exe", "WeChat.exe"])
    win = window_finder.pick_main_window(
        window_finder.enum_windows(procs),
        cfg.get("capture.title_hint", "微信"),
        cfg.get("capture.window_class_fallback", ""))
    if win is None:
        print("没找到微信主窗口")
        return 2

    fg = ctypes.windll.user32.GetForegroundWindow()
    print(f"窗口 hwnd={win.hwnd} {win.size} visible={win.visible} iconic={win.iconic}")
    print(f"前台窗口 hwnd={fg}　微信是前台？{'是' if fg == win.hwnd else '否'}")
    print(f"微信矩形={win.rect}　（可与前台窗口位置对比判断是否被遮挡）\n")

    cap = window_capture.Capture(win.hwnd)
    # 我们自己记 walk：cap.frames 只统计 WGC 回调；再看整帧哈希是否变化
    hashes: list[tuple[float, int, int]] = []
    t0 = time.perf_counter()
    last_frames = 0
    rows = []
    while time.perf_counter() - t0 < args.seconds:
        time.sleep(1.0)
        el = time.perf_counter() - t0
        got = cap.settled()
        fps = cap.frames - last_frames
        last_frames = cap.frames
        if got is not None:
            h_full = zlib.crc32(np.ascontiguousarray(got).tobytes())
            area = window_capture.chat_area(got)
            if area is not None:
                x0, y0, x1, y1 = area[:4]
                h_chat = zlib.crc32(np.ascontiguousarray(got[y0:y1, x0:x1]).tobytes())
            else:
                h_chat = -1
            hashes.append((el, h_full, h_chat))
            rows.append((el, fps, f"{h_full:08x}", f"{h_chat:08x}"))
        else:
            rows.append((el, fps, "-", "-"))

    print(f"{'t(s)':>5} {'近1s回调帧':>10}  {'整帧哈希':>9}  {'聊天区哈希':>10}")
    for el, fps, hf, hc in rows:
        print(f"{el:>5.0f} {fps:>10}  {hf:>9}  {hc:>10}")

    tot = cap.frames
    print(f"\n{args.seconds:.0f}s 内 WGC 回调总帧数={tot}（{(tot / args.seconds):.1f} 帧/秒）")
    print(f"内容变化（settled 出帧）次数={len(hashes)}")
    if hashes:
        uniq_full = len({h[1] for h in hashes})
        uniq_chat = len({h[2] for h in hashes})
        print(f"整帧哈希去重后={uniq_full}（含输入框光标闪烁；>1 说明微信仍在重绘）")
        print(f"聊天区哈希去重后={uniq_chat}（=1 说明消息区确实静止）")
        if uniq_full <= 1:
            print("\n⚠️ 整帧完全冻结 → 微信在被遮挡时可能已停止呈现（产品级风险，需提示用户保持窗口可见）")
        else:
            print("\n✅ 整帧有变化 → 微信仍在重绘，帧是新鲜的；延迟另有原因")
    cap.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
