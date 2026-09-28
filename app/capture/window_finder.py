# -*- coding: utf-8 -*-
"""微信窗口发现与「可截取性」保障。

本模块是对 jev-chat-src/app/capture.py 的**修正版**，修正点全部来自 M-1 真机验证
（见 README.optimized §0.1 E1/E5 与 probe/m1_report.md）：

1. 上游 `find_wechat_hwnd()` 硬筛 `IsWindowVisible`，微信收进托盘时直接抛
   RuntimeError。微信 4.x「关闭到托盘」是常态，长驻分析台不能因此崩。
   → 本实现**不筛可见性**，按进程名 + 类名/标题锁定主窗口。
2. 上游 `unminimize()` 只救 `IsIconic`（最小化），不救 `visible=False`（隐藏）。
   → 本实现两者都处理，且全程 SW_SHOWNOACTIVATE / SWP_NOACTIVATE，不抢焦点。
3. 【重要】微信 4.x 的真实界面画在内嵌 Chromium 子窗口（`Chrome_WidgetWin_0`）里。
   如果用 ShowWindow 强行把托盘隐藏的主窗口唤起，Chromium 视图**不会跟着恢复**，
   结果是帧全白、OCR 一个字都认不出（实测：唯一色 9、暗像素 0.8%）。
   → 因此提供 `frame_is_blank()` 探测，遇到空白帧要**明确提示用户手动打开微信**，
   而不是静默失败。该判据只用「唯一色数 + 相对底色的 ink 占比」，**与深浅主题无关**。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import time
from dataclasses import dataclass, field

import numpy as np

u32 = ctypes.windll.user32
k32 = ctypes.windll.kernel32

SW_SHOWNOACTIVATE = 4
HWND_BOTTOM = 1
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x0001, 0x0002, 0x0010
SWP_FLAGS = SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE

# 4.x / 3.x 主窗口类名；子窗口类名（Chrome_WidgetWin_0）不算主窗口
MAIN_WINDOW_CLASSES = ("Qt51514QWindowIcon", "WeChatMainWndForPC")
# 同进程的非主窗口，标题/类名长得像微信但截了没用
NOT_MAIN_TITLES = ("Weixin", "图片和视频", "WxTrayIconMessageWindow", "微信通知")
NOT_MAIN_CLASSES = ("Qt51514WxTrayIconMessageWindowClass", "Base_PowerMessageWindow")

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


@dataclass
class WindowInfo:
    hwnd: int
    pid: int
    exe: str
    title: str
    cls: str
    visible: bool
    iconic: bool
    rect: tuple[int, int, int, int]

    @property
    def size(self) -> tuple[int, int]:
        return (max(0, self.rect[2] - self.rect[0]), max(0, self.rect[3] - self.rect[1]))

    @property
    def area(self) -> int:
        w, h = self.size
        return w * h


def _exe_of(pid: int) -> str:
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf, size = ctypes.create_unicode_buffer(1024), ctypes.c_uint(1024)
        ok = k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
        return os.path.basename(buf.value).lower() if ok else ""
    finally:
        k32.CloseHandle(h)


def enum_windows(processes: list[str]) -> list[WindowInfo]:
    """枚举属于目标进程的**所有**顶层窗口（含隐藏/最小化）。"""
    want = {p.lower() for p in processes}
    out: list[WindowInfo] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def cb(hwnd, _):
        pid = ctypes.c_ulong()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        exe = _exe_of(pid.value)
        if exe not in want:
            return True
        t = ctypes.create_unicode_buffer(512)
        u32.GetWindowTextW(hwnd, t, 512)
        c = ctypes.create_unicode_buffer(256)
        u32.GetClassNameW(hwnd, c, 256)
        r = wt.RECT()
        u32.GetWindowRect(hwnd, ctypes.byref(r))
        out.append(WindowInfo(
            hwnd=int(hwnd), pid=pid.value, exe=exe, title=t.value, cls=c.value,
            visible=bool(u32.IsWindowVisible(hwnd)), iconic=bool(u32.IsIconic(hwnd)),
            rect=(r.left, r.top, r.right, r.bottom)))
        return True

    u32.EnumWindows(cb, 0)
    return out


def pick_main_window(wins: list[WindowInfo], title_hint: str = "微信",
                     class_fallback: str = "") -> WindowInfo | None:
    """挑主窗口：类名命中 > 标题命中；明确排除托盘/工具/看图窗。"""
    if not wins:
        return None
    good = [w for w in wins
            if w.title not in NOT_MAIN_TITLES and w.cls not in NOT_MAIN_CLASSES and w.area > 0]
    if not good:
        return None

    def rank(w: WindowInfo) -> tuple:
        cls_hit = 1 if (class_fallback and w.cls == class_fallback) else 0
        main_cls = 1 if w.cls in MAIN_WINDOW_CLASSES else 0
        title_hit = 1 if (title_hint and w.title == title_hint) else 0
        return (-(cls_hit * 4 + main_cls * 2 + title_hit), -w.area)

    return sorted(good, key=rank)[0]


def find_wechat_main_hwnd(processes: list[str] | None = None, title_hint: str = "微信",
                          class_fallback: str = "", hwnd_hint: int = 0):
    """→ (窗口信息, 候选列表)。找不到返回 (None, 候选列表)。"""
    processes = processes or ["Weixin.exe", "WeChat.exe"]
    wins = enum_windows(processes)
    if hwnd_hint:
        for w in wins:
            if w.hwnd == hwnd_hint and w.area > 0:
                return w, wins
    return pick_main_window(wins, title_hint, class_fallback), wins


def ensure_capturable(hwnd: int, settle_s: float = 0.6) -> str:
    """把窗口弄到「DWM 会渲染」的状态。返回做了什么，供日志/UI 展示。

    注意：对**托盘隐藏**的微信，本函数能让窗口出现在屏幕上，但实测 Chromium 子视图
    不会恢复 → 帧仍可能是空白。所以调用方必须配合 `frame_is_blank()` 判空。
    """
    if u32.IsIconic(hwnd):
        u32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
        u32.SetWindowPos(hwnd, HWND_BOTTOM, 0, 0, 0, 0, SWP_FLAGS)
        time.sleep(settle_s)
        return "minimized->restored"
    if not u32.IsWindowVisible(hwnd):
        u32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
        u32.SetWindowPos(hwnd, HWND_BOTTOM, 0, 0, 0, 0, SWP_FLAGS)
        time.sleep(settle_s)
        return "hidden->shown(可能仍渲染空白，见 docstring)"
    return "already-visible"


def frame_is_blank(frame: np.ndarray, max_colors: int = 40, max_ink: float = 0.01) -> tuple[bool, dict]:
    """判断帧是不是「什么都没画」的空白帧。返回 (是否空白, 指标)。

    实测依据（M-1）：微信 Chromium 视图没恢复时，整帧只有 9 种唯一色、几乎没有非底色像素；
    正常聊天截屏的唯一色数 > 100（文字抗锯齿产生大量过渡色）。

    判据 = 唯一色数少 **且** 非底色像素占比极低（<1%）。两个都要满足，避免误判：
    - 浅色主题的干净界面：唯一色少但仍有文字 → 有 ink，不判空白；
    - 深色主题（pane_bg ≈ (30,30,31)）：整帧都暗，所以**不能用「暗像素占比」判**，
      只能用「唯一色数 + 相对底色的 ink 占比」，两者与主题无关。
    """
    if frame is None or frame.size == 0:
        return True, {"uniq": 0, "ink": 0.0, "reason": "empty"}
    flat = frame.reshape(-1, 3)
    step = max(1, len(flat) // 4000)
    sample = flat[::step].astype(int)
    uniq = int(len(np.unique(sample, axis=0)))
    vals, cnt = np.unique(sample, axis=0, return_counts=True)
    bg = vals[cnt.argmax()]
    ink = float((np.abs(sample - bg).sum(1) > 30).mean())
    blank = uniq <= max_colors and ink <= max_ink
    return blank, {"uniq": uniq, "ink": round(ink, 4),
                   "bg": [int(v) for v in bg], "reason": "blank" if blank else "ok"}
