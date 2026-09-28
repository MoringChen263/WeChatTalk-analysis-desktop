# -*- coding: utf-8 -*-
"""左栏：会话监控。窗口选择 / 实时截图预览 / 说话人确认 / transcript / 导出。

M1 已接入真实采集链路（app.capture.collector + worker）：
开始监控 → 绑定微信窗口 → 自动等「画面停稳」的帧 → OCR → 去重 → transcript。
空白帧（微信收在托盘、Chromium 未渲染）会明确提示，不静默失败。
"""
from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from app import paths
from app.capture import importer, window_finder
from app.capture.worker import CaptureWorker
from app.ui import _qt
from app.ui._qt import (BodyLabel, CaptionLabel, ComboBox, PlainTextEdit,
                        PrimaryPushButton, PushButton, StrongBodyLabel, SubtitleLabel)


class CapturePanel(QWidget):
    """左栏。采集在 CaptureWorker 线程里跑，本类只做展示与转发。"""

    transcriptReady = Signal(object)  # 供右栏（M3 分析）消费

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config
        self.worker: CaptureWorker | None = None
        self.transcript = None
        self._build()
        self.refresh_windows()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 8, 16)
        root.setSpacing(8)

        root.addWidget(SubtitleLabel("会话监控", self))

        row = QHBoxLayout()
        row.addWidget(BodyLabel("目标窗口", self))
        self.window_box = ComboBox(self)
        self.window_box.setMinimumWidth(220)
        row.addWidget(self.window_box, 1)
        self.btn_scan = PushButton("重新扫描", self)
        self.btn_scan.clicked.connect(self.refresh_windows)
        row.addWidget(self.btn_scan)
        root.addLayout(row)

        row2 = QHBoxLayout()
        self.btn_start = PrimaryPushButton("开始监控", self)
        self.btn_start.clicked.connect(self.toggle_monitor)
        self.btn_stop = PushButton("停止", self)
        self.btn_stop.clicked.connect(self.stop_monitor)
        self.btn_stop.setEnabled(False)
        self.btn_clear = PushButton("清空/新段落", self)
        self.btn_clear.clicked.connect(lambda: self.worker and self.worker.request_flush())
        self.btn_clear.setEnabled(False)
        self.btn_export = PushButton("导出", self)
        self.btn_export.clicked.connect(self.export_transcript)
        row2.addWidget(self.btn_start)
        row2.addWidget(self.btn_stop)
        row2.addWidget(self.btn_clear)
        row2.addWidget(self.btn_export)
        root.addLayout(row2)

        # 导入已有导出文件（txt / html / ChatLab agent JSON）——与监控共用同一条 transcript 管道
        row3 = QHBoxLayout()
        self.btn_import = PushButton("导入聊天记录…", self)
        self.btn_import.clicked.connect(self.import_transcript)
        row3.addWidget(self.btn_import)
        self.lbl_import = CaptionLabel("支持 txt / html / ChatLab agent JSON；"
                                       "文件里的昵称谁是「你」必须由你点确认（不猜）", self)
        self.lbl_import.setWordWrap(True)
        row3.addWidget(self.lbl_import, 1)
        root.addLayout(row3)

        # 说话人确认（首轮必须人工点一次，不猜）
        root.addWidget(StrongBodyLabel("说话人确认", self))
        self.lbl_speaker = CaptionLabel("尚未采集，暂无建议值", self)
        self.lbl_speaker.setWordWrap(True)
        root.addWidget(self.lbl_speaker)
        side = QHBoxLayout()
        self.btn_me_right = PushButton("我 = 右侧", self)
        self.btn_me_left = PushButton("我 = 左侧", self)
        self.btn_me_right.clicked.connect(lambda: self._confirm_side("right"))
        self.btn_me_left.clicked.connect(lambda: self._confirm_side("left"))
        side.addWidget(self.btn_me_right)
        side.addWidget(self.btn_me_left)
        root.addLayout(side)

        root.addWidget(StrongBodyLabel("截图预览", self))
        self.preview = QLabel("（尚未截图）", self)
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumHeight(150)
        self.preview.setStyleSheet("border:1px solid rgba(0,0,0,.12); border-radius:8px;")
        root.addWidget(self.preview)

        root.addWidget(StrongBodyLabel("transcript（带 #id，供分析用）", self))
        self.ocr_text = PlainTextEdit(self)
        self.ocr_text.setReadOnly(True)
        self.ocr_text.setPlaceholderText("开始监控后，这里会实时出现 #id 开头的会话记录")
        root.addWidget(self.ocr_text, 1)

        self.lbl_status = CaptionLabel("就绪", self)
        self.lbl_status.setWordWrap(True)
        root.addWidget(self.lbl_status)

    # ------------------------------ 窗口发现 ------------------------------
    def refresh_windows(self) -> None:
        procs = self.config.get("capture.target_process", ["Weixin.exe", "WeChat.exe"])
        wins = window_finder.enum_windows(procs)
        self.window_box.clear()
        if not wins:
            self.window_box.addItem("未找到微信窗口（请先打开微信）", userData=0)
            return
        main = window_finder.pick_main_window(
            wins, self.config.get("capture.title_hint", "微信"),
            self.config.get("capture.window_class_fallback", ""))
        for w in wins:
            mark = "★ " if main and w.hwnd == main.hwnd else "  "
            state = "最小化" if w.iconic else ("隐藏" if not w.visible else "可见")
            self.window_box.addItem(
                f"{mark}{w.title or '(无标题)'} · {w.cls} · {w.size[0]}x{w.size[1]} · {state}",
                userData=w.hwnd)
        self.set_status(f"扫描到 {len(wins)} 个目标进程窗口，主窗口："
                        f"{'已锁定' if main else '未找到'}")

    # ------------------------------ 监控开关 ------------------------------
    def toggle_monitor(self) -> None:
        if self.worker is not None and self.worker.isRunning():
            self.stop_monitor()
            return
        hwnd = int(self.window_box.currentData() or 0)
        self.worker = CaptureWorker(self.config, hwnd, parent=self)
        self.worker.statusChanged.connect(self.set_status)
        self.worker.framePreview.connect(self.show_preview)
        self.worker.transcriptChanged.connect(self.on_transcript)
        self.worker.speakerSuggested.connect(self.on_suggestion)
        self.worker.blankFrame.connect(self.on_blank)
        self.worker.failed.connect(lambda m: _qt.notify(self, "采集出错", m, ok=False, ms=8000))
        self.worker.stopped.connect(self.on_stopped)
        self.worker.start()
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.btn_clear.setEnabled(True)
        self.set_status("正在绑定窗口…")

    def stop_monitor(self) -> None:
        if self.worker is not None:
            self.worker.stop()
            self.set_status("正在停止…")

    def on_stopped(self) -> None:
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.btn_clear.setEnabled(False)
        self.set_status("已停止监控")

    # ------------------------------ 槽 ------------------------------
    def set_status(self, text: str) -> None:
        self.lbl_status.setText(text)

    def show_preview(self, img) -> None:
        if img is None:
            return
        self.preview.setPixmap(QPixmap.fromImage(img).scaled(
            self.preview.width(), self.preview.height(),
            Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        self.preview.setToolTip("仅为「消息区」像素（整帧不落盘、不放内存）")

    def on_transcript(self, transcript) -> None:
        self.transcript = transcript
        self.ocr_text.setPlainText(transcript.to_text())
        self.transcriptReady.emit(transcript)

    def on_suggestion(self, sug: dict) -> None:
        side = "右侧" if sug.get("side") == "right" else "左侧"
        d = sug.get("detail") or {}
        if not d.get("me_total"):
            self.lbl_speaker.setText(f"本帧无可分类气泡：{d.get('note', '')}")
            return
        already = bool(((self.config.get("capture.speaker_map") or {}).get("me") or {}).get("confirmed"))
        tag = "已确认" if already else "建议值，请点一下确认"
        self.lbl_speaker.setText(
            f"底色判定「我」的气泡 {d.get('me_total')} 条 / 对方 {d.get('her_total')} 条；"
            f"与 x 位置一致率 {sug.get('agreement', 0):.0%}　→　我 = {side}（{tag}）")

    def on_blank(self, msg: str) -> None:
        self.set_status(msg.replace("\n", " "))
        _qt.notify(self, "微信窗口没有内容", msg, ok=False, ms=10000)

    def _confirm_side(self, side: str) -> None:
        if self.worker is None or not self.worker.isRunning():
            _qt.notify(self, "还没开始监控", "请先点「开始监控」", ok=False)
            return
        self.worker.request_speaker_side(side)

    # ------------------------------ 导入已有导出文件 ------------------------------
    def _ask_file(self) -> str | None:
        """选文件。抽成方法是为了给测试一个接缝（模态框没法自动化点）。"""
        from PySide6.QtWidgets import QFileDialog

        path, _ = QFileDialog.getOpenFileName(
            self, "选择聊天记录导出文件", str(Path.home()),
            "聊天导出 (*.txt *.log *.html *.htm *.json *.csv *.md);;全部文件 (*)")
        return path or None

    def _ask_me_label(self, res) -> str | None:
        """只问一个最小问题：哪个昵称是你。不按左右/语气猜（skill 边界）。"""
        from PySide6.QtWidgets import QInputDialog

        labels = [c["label"] for c in res.candidates]
        counts = {c["label"]: c["count"] for c in res.candidates}
        items = [f"{lb}（{counts[lb]} 条）" for lb in labels]
        guess = max(res.candidates, key=lambda c: c["count"])["label"] if res.candidates else ""
        pick, ok = QInputDialog.getItem(
            self, "确认说话人",
            f"文件里解析出 {len(res.candidates)} 个昵称。哪个是「你」（数据所有者）？\n"
            f"格式：{res.fmt}　路线：{res.stats.get('route')}\n"
            f"（不按左右、语气或「谁更像你」猜，必须你指定）",
            items, items.index(f"{guess}（{counts[guess]} 条）") if guess in labels else 0, False)
        return labels[items.index(pick)] if ok else None

    def import_transcript(self) -> None:
        """txt / html / ChatLab agent JSON → 同一条 transcript 管道。

        两条硬规矩（§5.2 + skill 边界）：
        - 文件里的昵称谁是「我」**必须问用户**，只有文件里显式写了 me/我/本人才自动认；
        - 认不出格式、失败信封、缺日期，一律弹出来说清楚，不静默吞掉。
        """
        path = self._ask_file()
        if not path:
            self.set_status("已取消导入（没选文件）")
            return
        max_msgs = int(self.config.get("import.max_messages", 2000))
        res = importer.import_file(path, max_messages=max_msgs)
        if not res.ok and res.needs == "me_label":
            chosen = self._ask_me_label(res)
            if chosen is None:
                self.set_status("已取消导入")
                return
            res = importer.import_file(path, me_label=chosen, max_messages=max_msgs)
        if not res.ok or res.transcript is None:
            detail = "\n".join(res.warnings[:6]) or "没有可用消息"
            _qt.notify(self, "导入失败", detail, ok=False, ms=12000)
            self.set_status(f"导入失败：{detail.splitlines()[0] if detail else ''}")
            return

        t = res.transcript
        self.transcript = t
        self.ocr_text.setPlainText(t.to_text())
        self.lbl_import.setText(
            f"已导入 {Path(path).name}：{len(t.messages)} 条，我 = {res.me_label!r}，"
            f"格式 {res.fmt} / 路线 {res.stats.get('route')}")
        self.transcriptReady.emit(t)
        warn = "；".join(t.warnings[:2])
        _qt.notify(self, "导入完成",
                   f"{len(t.messages)} 条　我={res.me_label}　"
                   f"时间 {t.messages[0].ts} ~ {t.messages[-1].ts}"
                   + (f"\n注意：{warn}" if warn else ""), ms=10000)
        self.set_status(f"已导入：{path}")

    # ------------------------------ 导出 ------------------------------
    def export_transcript(self) -> None:
        t = self.transcript
        if t is None or not t.messages:
            _qt.notify(self, "没有可导出的内容", "请先开始监控并采到消息", ok=False)
            return
        stamp = time.strftime("%Y%m%d-%H%M%S")
        base = paths.user_data_dir() / "exports"
        base.mkdir(parents=True, exist_ok=True)
        jp = t.save_json(base / f"transcript-{stamp}.json")
        mp = Path(base / f"transcript-{stamp}.md")
        mp.write_text(t.to_markdown(), encoding="utf-8")
        errs = t.validate()
        if errs:
            _qt.notify(self, "导出完成但有 schema 问题", "；".join(errs[:3]), ok=False, ms=8000)
        else:
            _qt.notify(self, "导出完成", f"{len(t.messages)} 条 → {jp.name} / {mp.name}")
        self.set_status(f"已导出：{jp}")
