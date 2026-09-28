# -*- coding: utf-8 -*-
"""诊断：微信进程到底有没有可枚举的顶层窗口（含隐藏/最小化）。只读。"""
import ctypes
import ctypes.wintypes as wt
import os

u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
u32.SetProcessDPIAware()


def exe_of(pid):
    h = k32.OpenProcess(0x1000, False, pid)
    if not h:
        return ""
    buf, size = ctypes.create_unicode_buffer(1024), ctypes.c_uint(1024)
    ok = k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
    k32.CloseHandle(h)
    return os.path.basename(buf.value).lower() if ok else ""


rows = []


@ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
def cb(hwnd, _):
    pid = ctypes.c_ulong()
    u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    exe = exe_of(pid.value)
    if exe not in ("weixin.exe", "wechat.exe"):
        return True
    t = ctypes.create_unicode_buffer(512)
    u32.GetWindowTextW(hwnd, t, 512)
    c = ctypes.create_unicode_buffer(256)
    u32.GetClassNameW(hwnd, c, 256)
    r = wt.RECT()
    u32.GetWindowRect(hwnd, ctypes.byref(r))
    rows.append({
        "hwnd": int(hwnd), "pid": pid.value, "title": t.value, "class": c.value,
        "visible": bool(u32.IsWindowVisible(hwnd)),
        "iconic": bool(u32.IsIconic(hwnd)),
        "rect": [r.left, r.top, r.right, r.bottom],
    })
    return True


print("session/desktop check")
h_desk = u32.GetThreadDesktop(k32.GetCurrentThreadId())
buf = ctypes.create_unicode_buffer(256)
n = ctypes.c_uint(0)
ctypes.windll.user32.GetUserObjectInformationW(h_desk, 2, buf, 512, ctypes.byref(n))
print("  current desktop:", buf.value)
print("  process session:", k32.WTSGetActiveConsoleSessionId() if hasattr(k32, "WTSGetActiveConsoleSessionId") else "?")

print("\nEnumWindows (this desktop):")
u32.EnumWindows(cb, 0)
if not rows:
    print("  (no Weixin.exe windows at all)")
for r in rows:
    print("  ", r)

print("\nEnumWindows merged for current desktop only? retry with GetWindow fallback:")
found = []
hwnd = 0
while True:
    hwnd = u32.FindWindowExW(0, hwnd, None, None)
    if not hwnd:
        break
    pid = ctypes.c_ulong()
    u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if exe_of(pid.value) in ("weixin.exe", "wechat.exe"):
        found.append(int(hwnd))
print("  top-level hwnds by FindWindowEx:", found)
