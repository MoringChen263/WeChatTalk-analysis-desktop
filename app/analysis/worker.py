# -*- coding: utf-8 -*-
"""分析 worker：在独立线程里跑一轮分析，流式增量经信号回 UI。

线程规则（与 §5.6 / capture worker 一致）：
- `AnalysisEngine` 只在 worker 线程里跑；LLM 调用全程在这条线程；
- `on_delta` 在 worker 线程被调用，只做 `Signal.emit`（Qt 会排队到 UI 线程），
  绝不在信号处理里碰控件；
- 取消用 `threading.Event`，流式每读到一行都会检查，所以取消延迟 ≤ 单次读超时。
"""
from __future__ import annotations

import threading
import traceback

from PySide6.QtCore import QThread, Signal

from app.analysis.engine import AnalysisEngine


class AnalysisWorker(QThread):
    planReady = Signal(object)    # engine.Plan —— 花钱之前的预检
    startedAnalysis = Signal(str)  # 人读的「正在做什么」
    delta = Signal(str)           # 流式增量（累积由 UI 负责）
    finished = Signal(object)      # engine.AnalysisRun（成功或失败都在这里）
    failed = Signal(str)           # 未预期异常
    cancelled = Signal()

    def __init__(self, config, transcript, question: str, *, goal: str = "",
                 emotion: int | None = None, extra: str = "", profile: str = "",
                 memory: str = "", recall=None, parent=None):
        super().__init__(parent)
        self.config = config
        self.transcript = transcript
        self.question = question
        self.goal = goal
        self.emotion = emotion
        self.extra = extra
        self.profile = profile
        self.memory = memory
        # 召回长期记忆要起子进程（实测约 250 ms），所以放在 worker 线程里做，
        # 由 UI 侧传一个无参可调用进来；返回 `memory.store.Recall`。
        self.recall = recall
        self._cancel = threading.Event()
        self.engine = AnalysisEngine(config)

    def cancel(self) -> None:
        """UI 线程调用。已发出的话术不做回滚，只是停止等待。"""
        self._cancel.set()

    @property
    def cancelled_by_user(self) -> bool:
        return self._cancel.is_set()

    def _resolve_memory(self) -> tuple[str, list[str]]:
        """在 worker 线程里召回记忆。**任何失败都不拦分析**，只把原因带回去说明。"""
        if self.recall is None:
            return self.memory, []
        self.startedAnalysis.emit("正在召回长期记忆…")
        try:
            rc = self.recall()
        except Exception as e:  # noqa: BLE001 - 记忆坏了不该连分析一起坏
            return self.memory, [f"记忆召回出错（已忽略，不影响本次分析）："
                                 f"{type(e).__name__}: {e}"]
        code = getattr(rc, "code", "")
        if getattr(rc, "ok", False):
            # 有记忆就给正文和条数；没有记忆就不出声，免得每次都刷一条空告警
            return (rc.text, [rc.note()]) if getattr(rc, "count", 0) else (self.memory, [])
        if code == "SKIPPED":
            return self.memory, []
        return self.memory, [rc.note()]

    def run(self) -> None:
        try:
            self.engine.loader.load(self.question)  # 预热一次，避免首帧卡在文件 IO
            memory, notes = self._resolve_memory()
            self.startedAnalysis.emit("正在读取 skill 并组装上下文…")
            run = self.engine.analyze(
                self.transcript, self.question, goal=self.goal, emotion=self.emotion,
                extra=self.extra, profile=self.profile, memory=memory,
                cancel=self._cancel, on_delta=self.delta.emit,
            )
            if notes:
                run.warnings[:0] = notes  # 放最前面：这是「本次输入里有什么」的交代
            self.finished.emit(run)
        except Exception as e:  # 分析线程不该炸掉整个 UI
            self.failed.emit(f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}")
        finally:
            if self._cancel.is_set():
                self.cancelled.emit()


__all__ = ["AnalysisWorker"]
