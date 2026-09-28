# -*- coding: utf-8 -*-
"""采集编排：窗口 → 稳定帧 → 消息区 → OCR → 时间锚点 → 去重 → transcript。

设计要点（全部来自 M-1 真机验证）：
1. **不抢焦点**：`ensure_capturable()` 全程 SW_SHOWNOACTIVATE / SWP_NOACTIVATE；
2. **禁止静默失败**：拿到空白帧时不产出空 transcript，而是置 `status="blank"` 并让上层提示
   「请手动点开微信窗口」（§8.5）；
3. **时间来自微信自己的灰字时间戳**，不是采集时刻——否则 gap_s 会全部塌成 0；
4. **切会话要断流**：`title` 变了就 flush 出上一段 transcript，避免两个人的对话搅在一起。

本模块不依赖 Qt，CLI 与 GUI 共用同一份逻辑。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np

from app.capture import ocr, window_capture, speaker_mapper, window_finder
from app.capture.ocr import Line
from app.capture.transcript import Transcript, TranscriptBuilder


class CaptureClosed(RuntimeError):
    """采集线程结束（微信被关掉 / 窗口销毁）。"""


@dataclass
class FrameResult:
    ok: bool
    status: str  # ok | blank | layout-unrecognized | closed
    lines: list[Line] = field(default_factory=list)
    new_messages: int = 0
    title: str = ""
    frame_shape: tuple = ()
    chat_shape: tuple = ()
    blank_metrics: dict = field(default_factory=dict)
    suggestion: dict = field(default_factory=dict)
    note: str = ""


class Collector:
    """一次采集会话（绑定一个微信窗口）。`poll()` 由调用方循环驱动——GUI 在 worker 线程里跑，
    CLI 在主循环里跑，逻辑完全一样。"""

    def __init__(self, config, hwnd: int = 0):
        self.config = config
        self.hwnd_hint = hwnd
        self.window: window_finder.WindowInfo | None = None
        self.cap: window_capture.Capture | None = None
        self.reader = ocr.Reader()
        self.speaker_map = config.get("capture.speaker_map") or speaker_mapper.default_map()
        self.builder = TranscriptBuilder(
            speaker_map=self.speaker_map,
            source="capture",
            dup_threshold=float(config.get("capture.dedup.dup_threshold", 0.15)),
        )
        self.suggestion: dict = {}
        self.title = ""
        self.status = "init"
        self.blank_streak = 0
        self.blank_warned = False
        self.frames_seen = 0
        self.ocr_min_score = float(config.get("capture.ocr_min_score", 0.9))
        self._closed: list[Transcript] = []
        self._last_header_hash: int | None = None
        self._last_suggest_at = 0.0
        self.note = ""

    # ------------------------------ 生命周期 ------------------------------
    def open(self) -> window_finder.WindowInfo:
        procs = self.config.get("capture.target_process", ["Weixin.exe", "WeChat.exe"])
        win, candidates = window_finder.find_wechat_main_hwnd(
            procs,
            self.config.get("capture.title_hint", "微信"),
            self.config.get("capture.window_class_fallback", ""),
            hwnd_hint=self.hwnd_hint,
        )
        if win is None:
            raise RuntimeError(f"没找到微信主窗口（同进程窗口 {len(candidates)} 个），请先打开微信")
        self.window = win
        self.config.set("capture.hwnd", int(win.hwnd))
        acted = window_finder.ensure_capturable(win.hwnd)
        self.cap = window_capture.Capture(win.hwnd)
        self.status = "opened"
        self.note = f"窗口 hwnd={win.hwnd} {win.cls} {win.size} · {acted}"
        return win

    def close(self) -> None:
        if self.cap is not None:
            try:
                self.cap.stop()
            except Exception:
                pass
            try:
                self.cap.wait()
            except Exception:
                pass
            self.cap = None
        self.status = "closed"

    def alive(self) -> bool:
        return self.cap is not None and self.cap.alive()

    # ------------------------------ 主循环 ------------------------------
    def poll(self, budget_s: float = 0.25) -> FrameResult | None:
        """等到一个停稳帧就处理并返回；budget 内没有则返回 None（调用方继续循环）。"""
        if self.cap is None:
            raise RuntimeError("Collector 未 open()")
        deadline = time.perf_counter() + budget_s
        while True:
            if not self.cap.alive():
                self.status = "closed"
                raise CaptureClosed("采集线程已结束（微信窗口关了？）")
            frame = self.cap.settled()
            if frame is not None:
                self.frames_seen += 1
                return self._process(frame)
            if time.perf_counter() >= deadline:
                return None
            time.sleep(0.03)

    def _process(self, frame: np.ndarray) -> FrameResult:
        blank, metrics = window_finder.frame_is_blank(frame)
        if blank:
            self.blank_streak += 1
            self.status = "blank"
            return FrameResult(ok=False, status="blank", blank_metrics=metrics,
                               note="帧是空白的：微信可能被收进托盘，Chromium 子视图没恢复")
        self.blank_streak = 0
        self.blank_warned = False

        area = self.cap.area
        if area is None:
            self.status = "layout-unrecognized"
            return FrameResult(ok=False, status="layout-unrecognized",
                               note="认不出消息区布局（窗口太小或布局没铺好）")
        x0, y_top, x1, y_in, pane_bg, y_pane = area
        chat = frame[y_top:y_in, x0:x1]
        if chat.size == 0:
            return FrameResult(ok=False, status="layout-unrecognized", note="消息区裁出来是空的")

        # 1) OCR + 分类（低置信行按 M-1 标定的 0.90 丢掉）
        lines = self.reader.read(chat, pane_bg, ocr_min_score=self.ocr_min_score)

        # 2) 时间锚点：微信的灰字时间戳按 y 顺序喂给 builder，只影响其下方的消息
        anchors = 0
        for text, y in sorted(self.reader.grays, key=lambda g: g[1]):
            if self.builder.set_anchor(text, y):
                anchors += 1

        # 3) 去重 → 新增行 → 翻译成 sender → 落进 transcript
        new_lines = self.reader.new_lines(lines)
        added = 0
        for code, l in speaker_mapper.translate(new_lines, self.speaker_map):
            # 该行在锚点下方就用锚点时间；否则（往上滚出来的旧消息）退回采集时刻
            at, _src = self.builder.anchor_at(int(l.y_top))
            if self.builder.add(code, l.text, y=int(l.y_top), confidence=l.score, at=at) is not None:
                added += 1

        # 4) 会话名（头部像素变了才花 60ms 去 OCR）
        title = self._maybe_read_title(frame, area)

        # 5) 说话人建议值：每帧算太勤，1 秒一次足够
        now = time.perf_counter()
        if lines and now - self._last_suggest_at > 1.0:
            self.suggestion = speaker_mapper.suggest(lines, chat.shape[1])
            self._last_suggest_at = now

        self.status = "ok"
        return FrameResult(ok=True, status="ok", lines=lines, new_messages=added,
                           title=title, frame_shape=frame.shape, chat_shape=chat.shape,
                           blank_metrics=metrics, suggestion=self.suggestion,
                           note=f"锚点 {anchors} 个" if anchors else "")

    def _maybe_read_title(self, frame: np.ndarray, area) -> str:
        try:
            header = window_capture.header_area(frame, area)
        except Exception:
            return self.title
        if header.size == 0:
            return self.title
        h = hash(header[::4, ::4].tobytes())
        if h == self._last_header_hash:
            return self.title
        self._last_header_hash = h
        try:
            title = ocr.read_title(header)
        except Exception:
            return self.title
        if title and title != self.title:
            if self.title:
                self.flush(reason=f"会话切换：{self.title} → {title}")
            self.title = title
            self.builder.window["chat_title"] = title
        return self.title

    # ------------------------------ 产出 ------------------------------
    def take(self) -> Transcript:
        """取当前 transcript（不重置）。"""
        t = self.builder.build()
        t.window = dict(t.window)
        if self.window is not None:
            t.window.update({"hwnd": self.window.hwnd, "class": self.window.cls,
                             "pid": self.window.pid, "exe": self.window.exe})
        if self.title:
            t.window["chat_title"] = self.title
        warn = speaker_mapper.unconfirmed_warning(self.speaker_map)
        if warn:
            t.warnings.insert(0, warn)
        if self.blank_streak:
            t.warnings.append(f"连续 {self.blank_streak} 帧空白，期间无新增（微信可能未打开）")
        return t

    def flush(self, reason: str = "") -> Transcript | None:
        """把当前这段封存并开新的一段（切会话/手动停止时用）。"""
        if not self.builder.messages:
            self.builder = self._new_builder()
            return None
        t = self.take()
        if reason:
            t.warnings.append(f"会话边界：{reason}")
        self._closed.append(t)
        self.builder = self._new_builder()
        self.reader.reset_seen()
        return t

    def _new_builder(self) -> TranscriptBuilder:
        return TranscriptBuilder(
            speaker_map=self.speaker_map, source="capture",
            dup_threshold=float(self.config.get("capture.dedup.dup_threshold", 0.15)))

    @property
    def closed_transcripts(self) -> list[Transcript]:
        return list(self._closed)

    def apply_speaker_side(self, side: str, label: str = "对方") -> dict:
        """UI 确认说话人后调用：写 config + 立即对后续消息生效。"""
        self.speaker_map = speaker_mapper.confirm(self.config, side, label)
        self.builder.sm = self.speaker_map
        return self.speaker_map
