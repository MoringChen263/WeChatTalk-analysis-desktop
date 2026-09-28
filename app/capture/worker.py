# -*- coding: utf-8 -*-
"""采集 worker：在独立线程里跑 Collector，用 Qt 信号把结果送回 UI 线程。

线程规则：
- `Collector` 的所有方法只在 worker 线程调用；UI 想改状态（确认说话人、清空会话）
  必须走 `request_*()`，由 worker 循环在安全的时点执行；
- 跨线程只传 QImage / 纯 dict / Transcript（Python 对象），不传 numpy 视图。
"""
from __future__ import annotations

import time

import numpy as np
from PySide6.QtCore import QThread, Signal

from app.capture import speaker_mapper
from app.capture.collector import CaptureClosed, Collector


def to_qimage(arr: np.ndarray):
    """numpy RGB → QImage（深拷贝，脱离 numpy 缓冲区生命周期）。"""
    from PySide6.QtGui import QImage

    if arr is None or arr.size == 0:
        return None
    buf = np.ascontiguousarray(arr, dtype=np.uint8)
    h, w = buf.shape[:2]
    return QImage(buf.data, w, h, 3 * w, QImage.Format.Format_RGB888).copy()


class CaptureWorker(QThread):
    statusChanged = Signal(str)          # 人类可读状态
    framePreview = Signal(object)        # QImage，节流 ≥1.5s
    transcriptChanged = Signal(object)   # Transcript
    speakerSuggested = Signal(object)    # speaker_mapper.suggest() 的 dict
    blankFrame = Signal(str)             # 空白帧提示（节流 ≥30s）
    failed = Signal(str)
    stopped = Signal()

    PREVIEW_MIN_INTERVAL = 1.5
    PREVIEW_WIDTH = 420

    def __init__(self, config, hwnd: int = 0, parent=None):
        super().__init__(parent)
        self.config = config
        self.hwnd = hwnd
        self.collector: Collector | None = None
        self._stop = False
        self._last_preview = 0.0
        self._last_blank_warn = 0.0
        self._pending_speaker: list[tuple[str, str]] = []
        self._pending_flush = False

    # --------------------------- UI 线程调用 ---------------------------
    def request_speaker_side(self, side: str, label: str = "对方") -> None:
        self._pending_speaker.append((side, label))

    def request_flush(self) -> None:
        self._pending_flush = True

    def stop(self) -> None:
        self._stop = True

    # --------------------------- worker 线程 ---------------------------
    def run(self) -> None:
        try:
            self.collector = Collector(self.config, self.hwnd)
            win = self.collector.open()
            self.statusChanged.emit(f"已绑定窗口：{win.title or win.cls}（{win.size[0]}×{win.size[1]}，"
                                    f"{'最小化' if win.iconic else ('隐藏' if not win.visible else '可见')}）"
                                    f"　{self.collector.note}")
        except Exception as e:
            self.failed.emit(f"打开采集失败：{e}")
            self.stopped.emit()
            return

        self._drain_requests()
        try:
            while not self._stop:
                self._drain_requests()
                try:
                    res = self.collector.poll(0.25)
                except CaptureClosed as e:
                    self.statusChanged.emit(f"采集结束：{e}")
                    break
                except Exception as e:  # 采集线程异常不该炸掉整个 UI
                    self.failed.emit(f"采集出错：{type(e).__name__}: {e}")
                    break
                if res is None:
                    continue
                self._handle(res)
        finally:
            try:
                t = self.collector.take()
                self.transcriptChanged.emit(t)
                self.collector.close()
            except Exception:
                pass
            self.stopped.emit()

    def _handle(self, res) -> None:
        if res.status == "blank":
            now = time.perf_counter()
            if now - self._last_blank_warn > 30:
                self._last_blank_warn = now
                self.blankFrame.emit(
                    f"连续拿到空白帧（唯一色 {res.blank_metrics.get('uniq')}、"
                    f"ink {res.blank_metrics.get('ink')}）：微信窗口当前没有渲染内容。\n"
                    f"请手动点开微信并停在要分析的聊天上，然后我会自动继续。")
            self.statusChanged.emit("等待微信窗口渲染内容…")
            return
        if res.status == "layout-unrecognized":
            self.statusChanged.emit("认不出消息区布局：把微信窗口拉大一点（或等它铺好布局）")
            return

        if res.title:
            self.statusChanged.emit(f"当前会话：{res.title}"
                                    f"　本帧 {len(res.lines)} 行 / 新增 {res.new_messages} 条"
                                    + (f"　{res.note}" if res.note else ""))
        if res.suggestion:
            self.speakerSuggested.emit(res.suggestion)
        if res.new_messages:
            self.transcriptChanged.emit(self.collector.take())
        self._emit_preview(res)

    def _emit_preview(self, res) -> None:
        now = time.perf_counter()
        if now - self._last_preview < self.PREVIEW_MIN_INTERVAL:
            return
        frame = None
        if self.collector is not None and self.collector.cap is not None:
            # 预览用「上一次停稳帧」——Collector 不保留帧，所以直接借 WGC 的 last（消息区）拼不出整窗，
            # 这里退而求其次：拿消息区 + 面板底色拼一张等尺寸图，够用户确认「截对窗口了」。
            last = self.collector.cap.last
            if last is not None:
                frame = last
        if frame is None:
            return
        self._last_preview = now
        h, w = frame.shape[:2]
        scale = max(1, int(round(w / self.PREVIEW_WIDTH)))
        small = frame[::scale, ::scale]
        img = to_qimage(np.ascontiguousarray(small))
        if img is not None:
            self.framePreview.emit(img)

    def _drain_requests(self) -> None:
        while self._pending_speaker:
            side, label = self._pending_speaker.pop(0)
            try:
                sm = self.collector.apply_speaker_side(side, label)
                ok = speaker_mapper.is_confirmed(sm)
                self.statusChanged.emit(
                    f"说话人已设为「我 = {'右侧' if side == 'right' else '左侧'}」，"
                    f"已写入配置（confirmed={ok}）+ 立即生效")
                self.transcriptChanged.emit(self.collector.take())
            except Exception as e:
                self.failed.emit(f"设置说话人失败：{e}")
        if self._pending_flush:
            self._pending_flush = False
            try:
                t = self.collector.flush(reason="手动清空")
                if t is not None:
                    self.statusChanged.emit(f"已封存一段 transcript（{len(t.messages)} 条），开始新的一段")
                self.transcriptChanged.emit(self.collector.take())
            except Exception as e:
                self.failed.emit(f"清空失败：{e}")
