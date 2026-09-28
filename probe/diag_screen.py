# -*- coding: utf-8 -*-
"""诊断：直接 BitBlt 屏幕对应区域，判断窗口「在屏幕上」是否真有内容。不落盘。"""
import ctypes
import ctypes.wintypes as wt
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from m1_feasibility import find_wechat_main_hwnd  # noqa: E402

u32, g32 = ctypes.windll.user32, ctypes.windll.gdi32
u32.SetProcessDPIAware()
HWND_TOP = 0
SWP = 0x0001 | 0x0002 | 0x0010


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
                ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
                ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]


def grab_screen(x, y, w, h):
    hdc = u32.GetDC(0)
    mem = g32.CreateCompatibleDC(hdc)
    bmp = g32.CreateCompatibleBitmap(hdc, w, h)
    g32.SelectObject(mem, bmp)
    g32.BitBlt(mem, 0, 0, w, h, hdc, x, y, 0x00CC0020)  # SRCCOPY
    bi = BITMAPINFOHEADER()
    bi.biSize, bi.biWidth, bi.biHeight, bi.biPlanes, bi.biBitCount = 40, w, -h, 1, 32
    buf = ctypes.create_string_buffer(w * h * 4)
    g32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bi), 0)
    arr = np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 4)[:, :, :3][:, :, ::-1].copy()
    g32.DeleteObject(bmp)
    g32.DeleteDC(mem)
    u32.ReleaseDC(0, hdc)
    return arr


def describe(full, tag):
    H, W = full.shape[:2]
    flat = full.reshape(-1, 3)
    uniq = len(np.unique(flat[::23], axis=0))
    lum = flat.mean(1)
    print(f"[{tag}] {full.shape} 唯一色={uniq} 均值={flat.mean(0).round(1)} "
          f"min={flat.min(0)} max={flat.max(0)} 暗像素={float((lum < 200).mean()):.3f}")
    small = full @ np.array([0.299, 0.587, 0.114])
    small = small[:: max(1, H // 20), :: max(1, W // 50)][:20, :50]
    ramp = " .:-=+*#%@"
    for row in small:
        print("   " + "".join(ramp[min(9, int(v / 25.6))] for v in row))


hwnd, _ = find_wechat_main_hwnd()
u32.SetWindowPos(hwnd, HWND_TOP, 0, 0, 0, 0, SWP)
time.sleep(1.5)
r = wt.RECT()
u32.GetWindowRect(hwnd, ctypes.byref(r))
print("window rect:", r.left, r.top, r.right, r.bottom,
      "visible", bool(u32.IsWindowVisible(hwnd)))

W, H = r.right - r.left, r.bottom - r.top
sub = grab_screen(r.left, r.top, W, H)
describe(sub, "屏幕对应区域")

print("\n窗口内部子区域（右半：聊天面板）:")
half = sub[:, W // 2:]
describe(half, "屏幕·右半")

print("\n中间一条（y 100~300）:")
print(np.unique(sub[100:300, :, :].reshape(-1, 3)[::11], axis=0)[:10])
