# -*- coding: utf-8 -*-
"""诊断：微信主窗口的子窗口结构（4.x 是否把内容画在子窗口里）。不落盘。"""
import ctypes
import ctypes.wintypes as wt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from m1_feasibility import find_wechat_main_hwnd  # noqa: E402

u32 = ctypes.windll.user32
u32.SetProcessDPIAware()

hwnd, _ = find_wechat_main_hwnd()

rows = []


@ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
def cb(child, _):
    t = ctypes.create_unicode_buffer(512)
    u32.GetWindowTextW(child, t, 512)
    c = ctypes.create_unicode_buffer(256)
    u32.GetClassNameW(child, c, 256)
    r = wt.RECT()
    u32.GetWindowRect(child, ctypes.byref(r))
    rows.append({
        "hwnd": int(child), "class": c.value, "title": t.value,
        "visible": bool(u32.IsWindowVisible(child)),
        "rect": [r.left, r.top, r.right, r.bottom],
    })
    return True


u32.EnumChildWindows(hwnd, cb, 0)
print(f"主窗口 hwnd={hwnd}，子窗口 {len(rows)} 个：")
for x in rows:
    print(f"  {x['hwnd']:>10} vis={int(x['visible'])} {x['class']:<34} {x['title'][:24]:<24} {x['rect']}")

names = [x["class"] for x in rows]
print("\n是否含 MMUIRenderSubWindowHW:", any("MMUIRender" in n for n in names))
print("是否含 Qt 渲染子窗:", [n for n in names if "Qt" in n or "Render" in n])
