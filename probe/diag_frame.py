# -*- coding: utf-8 -*-
"""诊断：微信窗口在不同「可见性/层叠状态」下，WGC 能不能抓到真实内容。不落盘。"""
import ctypes
import ctypes.wintypes as wt
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from m1_feasibility import find_wechat_main_hwnd  # noqa: E402

u32 = ctypes.windll.user32
u32.SetProcessDPIAware()
from windows_capture import WindowsCapture  # noqa: E402

HWND_TOP, HWND_BOTTOM = 0, 1
SWP = 0x0001 | 0x0002 | 0x0010  # NOSIZE | NOMOVE | NOACTIVATE


def grab(hwnd, seconds=3.0):
    frames = []

    def on_frame_arrived(frame, control):
        frames.append(np.ascontiguousarray(frame.frame_buffer[:, :, :3][:, :, ::-1]))

    def on_closed():
        pass

    cap = WindowsCapture(window_hwnd=hwnd)
    cap.event(on_frame_arrived)
    cap.event(on_closed)
    ctl = cap.start_free_threaded()
    t0 = time.time()
    while time.time() - t0 < seconds and not frames:
        time.sleep(0.1)
    try:
        ctl.stop()
    except Exception:
        pass
    if not frames:
        return None
    return frames[-1]


def describe(full, tag):
    if full is None:
        print(f"  [{tag}] 没收到帧")
        return
    H, W = full.shape[:2]
    flat = full.reshape(-1, 3)
    uniq = len(np.unique(flat[::17], axis=0))
    ink = int((flat.mean(1) < 200).sum())
    print(f"  [{tag}] {full.shape} 唯一色={uniq} 均值={flat.mean(0).round(1)} "
          f"min={flat.min(0)} 暗像素占比={ink / len(flat):.3f}")


hwnd, _ = find_wechat_main_hwnd()
r = wt.RECT()

states = [
    ("A 当前状态（之前被压到最底层）", lambda: None),
    ("B SetWindowPos(HWND_TOP, NOACTIVATE) 置顶但不激活",
     lambda: u32.SetWindowPos(hwnd, HWND_TOP, 0, 0, 0, 0, SWP)),
    ("C ShowWindow(SW_SHOW) + SetForegroundWindow（会抢焦点）",
     lambda: (u32.ShowWindow(hwnd, 5), u32.SetForegroundWindow(hwnd))),
]

for name, act in states:
    act()
    time.sleep(1.2)
    u32.GetWindowRect(hwnd, ctypes.byref(r))
    print(f"\n{name}\n  rect={[r.left, r.top, r.right, r.bottom]} "
          f"visible={bool(u32.IsWindowVisible(hwnd))} iconic={bool(u32.IsIconic(hwnd))}")
    describe(grab(hwnd), name.split()[0])

print("\n恢复：压回最底层、不激活")
u32.SetWindowPos(hwnd, HWND_BOTTOM, 0, 0, 0, 0, SWP)
