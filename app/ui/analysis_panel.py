# -*- coding: utf-8 -*-
"""右栏：五步分析 + 话术卡（README.optimized §5.5 / §7）。

三条交互规矩：
- **花钱前先预检**：改问题类型就刷新「将加载哪些参考 / 后端与模型是否就绪」，
  配置不通当场说清楚，不等到点「开始分析」才失败。
- **降级必须可见**：模型没给出合规 JSON 时标成「纯文本降级」，绝不装作结构化成功。
- **安全信号优先**：命中危机词就常驻横幅 + 提示紧急服务，不被别的提示挤掉。
"""
from __future__ import annotations

import time

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QLineEdit, QListWidget, QListWidgetItem, QSpinBox,
    QStackedWidget, QTabWidget, QTextBrowser, QVBoxLayout, QWidget,
)

from app.analysis import parse_output as po
from app.analysis import questions as q
from app.analysis import schema as sc
from app.analysis.engine import AnalysisEngine, AnalysisRun, Plan
from app.analysis.llm_client import LLMClient
from app.analysis.worker import AnalysisWorker
from app.ui import _qt
from app.ui._qt import (BodyLabel, CaptionLabel, ComboBox, PrimaryPushButton, PushButton,
                        SubtitleLabel)

STEPS = list(sc.STEP_TITLES)
SCRIPT_TABS = list(sc.SCRIPT_TABS)
CONF_CN = {"high": "高", "medium": "中", "low": "低"}


class AnalysisPanel(QWidget):
    """右栏整体。`set_transcript` 由左栏的 transcriptReady 信号驱动。"""

    analysisFinished = Signal(object)  # AnalysisRun，主窗口可用来落盘/统计

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config
        self.transcript = None
        self.worker: AnalysisWorker | None = None
        self.run: AnalysisRun | None = None
        self.history: list = []  # 本次会话分析历史（重跑不再覆盖丢失）
        self._streamed = 0
        self._t0 = 0.0
        self._subject_label = ""
        self._keys = [qt.key for qt in q.QUESTION_TYPES]
        self._build()
        self._refresh_plan()

    # ------------------------------ 构建 ------------------------------
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 16, 16, 16)
        root.setSpacing(8)

        head = QHBoxLayout()
        head.addWidget(SubtitleLabel("分析", self))
        self.lbl_source = CaptionLabel("还没有会话记录：先在左栏采集或导入", self)
        head.addWidget(self.lbl_source)
        head.addStretch(1)
        root.addLayout(head)

        row1 = QHBoxLayout()
        row1.addWidget(BodyLabel("问题类型", self))
        self.type_box = ComboBox(self)
        for qt in q.QUESTION_TYPES:
            self.type_box.addItem(qt.label)
        self.type_box.setCurrentIndex(self._keys.index(q.DEFAULT_KEY)
                                      if q.DEFAULT_KEY in self._keys else 0)
        self.type_box.currentIndexChanged.connect(self._on_type_changed)
        row1.addWidget(self.type_box, 2)
        row1.addWidget(BodyLabel("情绪强度", self))
        self.emotion = QSpinBox(self)
        self.emotion.setRange(0, 10)
        self.emotion.setValue(5)
        self.emotion.setToolTip("0 = 很平静，10 = 非常难受。会影响建议的颗粒度。")
        row1.addWidget(self.emotion)
        row1.addStretch(1)
        root.addLayout(row1)

        goal_row = QHBoxLayout()
        goal_row.addWidget(BodyLabel("我要的结果", self))
        self.goal = QLineEdit(self)
        self.goal.setPlaceholderText("例：想约她周末看展 / 想确认是不是该退出了")
        goal_row.addWidget(self.goal, 1)
        root.addLayout(goal_row)

        extra_row = QHBoxLayout()
        extra_row.addWidget(BodyLabel("补充说明", self))
        self.extra = QLineEdit(self)
        self.extra.setPlaceholderText("记录里看不出来、但会影响判断的情况（可留空）")
        extra_row.addWidget(self.extra, 1)
        root.addLayout(extra_row)

        act = QHBoxLayout()
        self.btn_analyze = PrimaryPushButton("开始分析", self)
        self.btn_analyze.clicked.connect(self.start_analysis)
        self.btn_cancel = PushButton("取消", self)
        self.btn_cancel.clicked.connect(self.cancel_analysis)
        self.btn_cancel.setEnabled(False)
        self.btn_copy_all = PushButton("复制全文", self)
        self.btn_copy_all.clicked.connect(self.copy_all)
        self.btn_copy_all.setEnabled(False)
        self.btn_export = PushButton("导出 analysis.json", self)
        self.btn_export.clicked.connect(self.export_analysis)
        self.btn_export.setEnabled(False)
        self.btn_remember = PushButton("存进记忆", self)
        self.btn_remember.clicked.connect(self.on_remember)
        self.btn_remember.setEnabled(False)
        self.btn_remember.setToolTip("把这次分析里的「事件」和「假设」挑出来，你确认后再存进本机档案")
        for b in (self.btn_analyze, self.btn_cancel, self.btn_copy_all, self.btn_export,
                  self.btn_remember):
            act.addWidget(b)
        act.addStretch(1)
        root.addLayout(act)

        # ---- 本次分析历史：每次分析都留一条，重跑不会丢上一次结论 ----
        hist_head = QHBoxLayout()
        self.lbl_history = CaptionLabel("本次分析历史（0）", self)
        hist_head.addWidget(self.lbl_history)
        hist_head.addStretch(1)
        root.addLayout(hist_head)

        self.history_list = QListWidget(self)
        self.history_list.setMaximumHeight(120)
        self.history_list.setToolTip("每次分析都会留一条；点任意一条可回看，再跑新的也不会丢。")
        self.history_list.itemClicked.connect(self._on_history_clicked)
        root.addWidget(self.history_list)

        hist_btn = QHBoxLayout()
        self.btn_export_all = PushButton("导出全部历史", self)
        self.btn_export_all.clicked.connect(self.export_all_history)
        self.btn_export_all.setEnabled(False)
        self.btn_clear_hist = PushButton("清空", self)
        self.btn_clear_hist.clicked.connect(self.clear_history)
        self.btn_clear_hist.setEnabled(False)
        hist_btn.addWidget(self.btn_export_all)
        hist_btn.addWidget(self.btn_clear_hist)
        hist_btn.addStretch(1)
        root.addLayout(hist_btn)

        self.lbl_plan = CaptionLabel("", self)
        self.lbl_plan.setWordWrap(True)
        root.addWidget(self.lbl_plan)

        self.lbl_banner = BodyLabel("", self)
        self.lbl_banner.setWordWrap(True)
        self.lbl_banner.setVisible(False)
        root.addWidget(self.lbl_banner)

        self.lbl_warn = CaptionLabel("", self)
        self.lbl_warn.setWordWrap(True)
        root.addWidget(self.lbl_warn)

        self.steps = QTabWidget(self)
        for name in STEPS:
            view = QTextBrowser(self)
            view.setOpenExternalLinks(True)
            view.setPlaceholderText("等待分析…")
            self.steps.addTab(view, name)

        script_head = QHBoxLayout()
        script_head.addWidget(BodyLabel("话术卡", self))
        self.btn_copy_script = PrimaryPushButton("复制当前这版", self)
        self.btn_copy_script.clicked.connect(self.copy_current_script)
        self.btn_copy_script.setToolTip("复制当前 Tab 里那条能直接发送的成品")
        script_head.addWidget(self.btn_copy_script)
        script_head.addStretch(1)

        self.scripts = QTabWidget(self)
        for name in SCRIPT_TABS:
            view = QTextBrowser(self)
            view.setPlaceholderText("等待生成…")
            self.scripts.addTab(view, name)

        # ---- 查看方式：单页长图（默认） / 分步标签 ----
        # P1-4：五步+话术原藏在 5 个 tab 里，用户不翻就只看到「步骤一」。
        # 默认单页可滚动，一次看完；想逐条细看可切分步标签。
        toggle = QHBoxLayout()
        toggle.addWidget(BodyLabel("查看方式", self))
        self.view_mode = ComboBox(self)
        self.view_mode.addItem("单页长图（推荐）")
        self.view_mode.addItem("分步标签")
        self.view_mode.setCurrentIndex(0)
        self.view_mode.setToolTip("单页：五步+话术一次看完；分步：用标签切换单个步骤。")
        self.view_mode.currentIndexChanged.connect(self._on_view_mode_changed)
        toggle.addWidget(self.view_mode)
        toggle.addStretch(1)
        root.addLayout(toggle)

        self.view_stack = QStackedWidget(self)
        # 0：单页长图
        self.wide_view = QTextBrowser(self)
        self.wide_view.setOpenExternalLinks(True)
        self.wide_view.setPlaceholderText("等待分析…")
        self.view_stack.addWidget(self.wide_view)
        # 1：分步标签（steps + 话术卡）
        self.tabbed = QWidget(self)
        tv = QVBoxLayout(self.tabbed)
        tv.setContentsMargins(0, 0, 0, 0)
        tv.addWidget(self.steps, 3)
        tv.addLayout(script_head)
        tv.addWidget(self.scripts, 2)
        self.view_stack.addWidget(self.tabbed)
        root.addWidget(self.view_stack, 3)

        self.footnote = CaptionLabel("", self)
        self.footnote.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.footnote.setWordWrap(True)
        root.addWidget(self.footnote)

    # ------------------------------ 外部接口 ------------------------------
    def set_transcript(self, transcript) -> None:
        """左栏拿到新 transcript 时调用。"""
        self.transcript = transcript
        n = len(getattr(transcript, "messages", []) or [])
        title = (getattr(transcript, "window", {}) or {}).get("chat_title") or "未识别"
        src = "已导入" if getattr(transcript, "source", "") == "import" else "实时采集"
        self.lbl_source.setText(f"当前输入：{title}　{n} 条（{src}）")
        self._refresh_plan()

    def current_question_type(self) -> str:
        idx = self.type_box.currentIndex()
        return self._keys[idx] if 0 <= idx < len(self._keys) else q.DEFAULT_KEY

    # ------------------------------ 预检 ------------------------------
    def _on_type_changed(self) -> None:
        self._refresh_plan()

    def _refresh_plan(self) -> None:
        """不花钱的预检：会加载哪些参考、后端是否就绪、有没有命中安全信号。

        这里**故意不起子进程**问记忆状态（一次约 250 ms，改个问题类型就卡一下不值），
        只看配置里的镜像值。真实条数在召回之后由页脚交代 —— 宁可少说，不要说个过期的数。
        """
        plan: Plan = AnalysisEngine(self.config).plan(
            self.current_question_type(), self.transcript,
            goal=self.goal.text(), extra=self.extra.text())
        bits = [f"将加载 {len(plan.refs)} 份参考"]
        if plan.titles:
            bits.append("、".join(plan.titles))
        bits.append(("后端就绪：" + (plan.model or "?")) if plan.ready
                    else ("后端未就绪：" + (plan.reason or "未知原因")))
        bits.append("长期记忆：已启用，分析时会带上已存档案"
                    if bool(self.config.get("memory.enabled", False))
                    else "长期记忆：未启用")
        for note in plan.notes:
            bits.append(note)
        self.lbl_plan.setText("　|　".join(bits))
        self.lbl_plan.setStyleSheet("" if plan.ready else "color:#c0392b;")

        if plan.crisis:
            self._banner("⚠️ 检测到安全相关信号：" + "、".join(plan.crisis)
                         + "。本次会强制加载安全类参考并按紧急例外处理；如当下有现实危险，"
                           "请优先联系可信的人与当地紧急服务（110 / 12338）。", level="danger")
        elif not plan.ready:
            self._banner("还不能调用模型：" + (plan.reason or "配置不完整")
                         + "　→　点右上角「设置」填写。", level="warn")
        else:
            self._banner("", level="")

        has_input = bool(self.transcript and getattr(self.transcript, "messages", None))
        self.btn_analyze.setEnabled(has_input and plan.ready and self.worker is None)

    def _banner(self, text: str, level: str = "warn") -> None:
        colors = {"danger": "#c0392b", "warn": "#b9770e", "": ""}
        self.lbl_banner.setText(text)
        self.lbl_banner.setVisible(bool(text))
        self.lbl_banner.setStyleSheet(f"color:{colors[level]};" if colors.get(level) else "")

    # ------------------------------ 分析 ------------------------------
    def start_analysis(self) -> None:
        if self.worker is not None:
            return
        if not self.transcript or not getattr(self.transcript, "messages", None):
            _qt.notify(self, "还没有输入", "先在左栏采集一段聊天，或导入一个导出文件。", ok=False)
            return
        # 按钮禁用只是一道 UI 防线；直接调用时（脚本、快捷键、配置在预检后被改）
        # 还必须再校验一次，否则会在没 Key 的情况下白起一条线程。
        ready, why = LLMClient(self.config).ready()
        if not ready:
            self._refresh_plan()
            _qt.notify(self, "还不能调用模型", why + "　→　点右上角「设置」填写。",
                       ok=False, ms=8000)
            return
        self._streamed = 0
        self._t0 = time.perf_counter()
        self.footnote.setText("正在组装上下文…")
        self.btn_analyze.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        self.btn_remember.setEnabled(False)  # 上一轮的结果不再是「这次的」，别存错
        self._banner("", level="")

        # 召回放进 worker 线程：官方脚本要起子进程，同步做会让界面顿一下
        from app.memory import service as svc

        subject_code, subject_label = svc.current_subject(self.config)
        self._subject_label = subject_label
        self.worker = AnalysisWorker(
            self.config, self.transcript, self.current_question_type(),
            goal=self.goal.text(), emotion=self.emotion.value(), extra=self.extra.text(),
            recall=lambda: svc.recall_for(self.config, subject_code, subject_label))
        self.worker.startedAnalysis.connect(self._on_status)
        self.worker.delta.connect(self._on_delta)
        self.worker.finished.connect(self._on_finished)
        self.worker.failed.connect(self._on_failed)
        self.worker.cancelled.connect(lambda: self.footnote.setText("已取消本次分析"))
        self.worker.start()

    def cancel_analysis(self) -> None:
        if self.worker is not None:
            self.worker.cancel()
            self.btn_cancel.setEnabled(False)
            self.footnote.setText("取消中（等当前这一小块返回）…")

    def _on_status(self, text: str) -> None:
        self.footnote.setText(text)

    def _on_delta(self, chunk: str) -> None:
        # 输出是流式 JSON，把半截 JSON 打在界面上只会被误读，所以显示「进度」而不是「内容」
        self._streamed += len(chunk)
        self.footnote.setText(f"模型正在输出…已收到 {self._streamed} 字"
                              f"（{time.perf_counter() - self._t0:.1f}s）")

    def _on_failed(self, msg: str) -> None:
        self._reset_buttons()
        self._banner("分析线程异常：" + msg.splitlines()[0], level="danger")
        self.footnote.setText("失败")
        _qt.notify(self, "分析失败", msg.splitlines()[0], ok=False, ms=8000)

    def _reset_buttons(self) -> None:
        self.worker = None
        self.btn_cancel.setEnabled(False)
        self._refresh_plan()

    def _on_finished(self, run: AnalysisRun) -> None:
        self._reset_buttons()
        self.run = run
        if run.ok or run.analysis.data or run.analysis.raw.strip():
            # 有产出才进历史：纯失败（连降级输出都没有）不值得留一条
            self._append_history(run)
            self._render(run)
            if run.error:
                self.footnote.setText(f"❌ {run.error}　{self.footnote.text()}")
        else:
            self._render_failure(run)
        self.analysisFinished.emit(run)

    # ------------------------------ 查看方式切换 ------------------------------
    def _on_view_mode_changed(self, idx: int) -> None:
        # 0 = 单页长图，1 = 分步标签
        self.view_stack.setCurrentIndex(0 if idx == 0 else 1)

    def _wide_markdown(self, steps_md, scripts_md) -> str:
        """把五步 + 话术卡拼成一份可滚动的单页 Markdown。"""
        parts = []
        for i, page in enumerate(steps_md):
            title = STEPS[i] if i < len(STEPS) else f"步骤 {i + 1}"
            parts.append(f"## {title}\n\n{page}")
        if scripts_md:
            parts.append("## 话术卡")
            for j, page in enumerate(scripts_md):
                name = SCRIPT_TABS[j] if j < len(SCRIPT_TABS) else f"话术 {j + 1}"
                parts.append(f"### {name}\n\n{page}")
        return "\n\n".join(parts)

    # ------------------------------ 渲染 ------------------------------
    def _render(self, run: AnalysisRun) -> None:
        a = run.analysis
        steps_md = a.steps_markdown()
        scripts_md = a.scripts_markdown()
        for i, page in enumerate(steps_md):
            self.steps.widget(i).setMarkdown(page)
        for i, page in enumerate(scripts_md):
            self.scripts.widget(i).setMarkdown(page)
        self.wide_view.setMarkdown(self._wide_markdown(steps_md, scripts_md))
        self.steps.setCurrentIndex(0)
        self.scripts.setCurrentIndex(0)
        self.btn_copy_all.setEnabled(True)
        self.btn_export.setEnabled(True)
        # 只有结构化成功的结果才谈得上「把结论存下来」；降级输出没有可提取的字段
        self.btn_remember.setEnabled(bool(run.ok))

        bits = []
        if a.confidence:
            bits.append("置信度 " + CONF_CN.get(a.confidence, a.confidence))
        if run.cost_cny is not None:
            bits.append(f"约 ¥{run.cost_cny:.4f}")
        bits.append(f"{run.usage.total_tokens} tokens" if run.usage.total_tokens
                    else "用量未知")
        bits.append(f"{run.elapsed_s:.1f}s")
        if run.calls > 1:
            bits.append(f"{run.calls} 次调用")
        self.footnote.setText(po.MODE_LABEL.get(run.mode, run.mode) + "　" + "　".join(bits))

        crisis = [w for w in run.warnings if "安全信号" in w]
        if crisis:
            self._banner(crisis[0], level="danger")
        elif run.mode == po.MODE_DEGRADED:
            self._banner("模型这次没给出合规 JSON，下面是它的原始输出（未结构化整理）。",
                         level="danger")
        elif run.mode == po.MODE_PARTIAL:
            self._banner("模型漏了字段，未列出的部分已标注「模型未给出」；这次结论请打折看。",
                         level="warn")
        elif a.citations_dropped:
            self._banner("模型引用了记录里不存在的编号，已标为 [#?]（说明它有推测成分）。",
                         level="warn")
        else:
            self._banner("", level="")

        warns = [w for w in run.warnings if w not in crisis]
        self.lbl_warn.setText(("提示：" + "；".join(warns[:3])) if warns else "")

    def _render_failure(self, run: AnalysisRun) -> None:
        err = run.error
        msg = str(err) if err else "未知原因"
        guide = "　".join(run.warnings) if run.warnings else ""
        step0 = (f"> ❌ **这次没成**：{msg}\n\n"
                 + (f"{guide}\n\n" if guide else "")
                 + "没有产出结果。先按上面的提示处理，再点一次「开始分析」。")
        self.steps.widget(0).setMarkdown(step0)
        for i in range(1, len(STEPS)):
            self.steps.widget(i).setMarkdown("> 本次没有结果。")
        for i in range(len(SCRIPT_TABS)):
            self.scripts.widget(i).setMarkdown("> 本次没有结果。")
        self.wide_view.setMarkdown(step0)
        # 页面已经被换成「没有结果」了，再让「复制全文 / 导出 / 存进记忆」可点就是骗人
        self.btn_copy_all.setEnabled(False)
        self.btn_export.setEnabled(False)
        self.btn_remember.setEnabled(False)
        self.footnote.setText("失败")
        self._banner(msg, level="danger")

    # ------------------------------ 分析历史 ------------------------------
    def _qtype_label(self) -> str:
        idx = self.type_box.currentIndex()
        if 0 <= idx < len(q.QUESTION_TYPES):
            return q.QUESTION_TYPES[idx].label
        return q.DEFAULT_KEY

    def _append_history(self, run: AnalysisRun) -> None:
        tag = po.MODE_LABEL.get(run.mode, run.mode)
        cost = f" ¥{run.cost_cny:.4f}" if run.cost_cny is not None else ""
        head = (f"{time.strftime('%H:%M:%S')} · {self._qtype_label()} · {tag}"
                f"{cost} · {run.usage.total_tokens} tok")
        item = QListWidgetItem(head, self.history_list)
        item.setData(Qt.ItemDataRole.UserRole, run)
        self.history.append(run)
        self.history_list.setCurrentItem(item)
        self.lbl_history.setText(f"本次分析历史（{len(self.history)}）")
        self.btn_export_all.setEnabled(True)
        self.btn_clear_hist.setEnabled(True)

    def _on_history_clicked(self, item: QListWidgetItem) -> None:
        run = item.data(Qt.ItemDataRole.UserRole)
        if not run:
            return
        self.run = run
        if run.ok or run.analysis.data or run.analysis.raw.strip():
            self._render(run)
        else:
            self._render_failure(run)

    def export_all_history(self) -> None:
        if not self.history:
            _qt.notify(self, "还没有历史", "先跑至少一次分析。", ok=False)
            return
        from PySide6.QtWidgets import QFileDialog

        from app import paths

        default = str(paths.user_data_dir() / f"analysis-history-{time.strftime('%Y%m%d-%H%M%S')}.json")
        path, _ = QFileDialog.getSaveFileName(self, "导出全部分析历史", default, "JSON (*.json)")
        if not path:
            return
        import json
        from pathlib import Path

        data = [r.to_dict() for r in self.history]
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        _qt.notify(self, "已导出", f"{len(data)} 次分析已写入 {path}")

    def clear_history(self) -> None:
        self.history.clear()
        self.history_list.clear()
        self.lbl_history.setText("本次分析历史（0）")
        self.btn_export_all.setEnabled(False)
        self.btn_clear_hist.setEnabled(False)

    # ------------------------------ 复制 / 导出 ------------------------------
    def copy_current_script(self) -> None:
        idx = self.scripts.currentIndex()
        text = self.run.analysis.script_text(idx) if (self.run and self.run.analysis) else ""
        if not text:
            _qt.notify(self, "还没有话术", "先跑一次分析。", ok=False)
            return
        QApplication.clipboard().setText(text)
        tab = SCRIPT_TABS[idx] if 0 <= idx < len(SCRIPT_TABS) else ""
        _qt.notify(self, "已复制", f"「{tab}」{len(text)} 字已进剪贴板。")

    def copy_all(self) -> None:
        if not (self.run and self.run.analysis):
            return
        QApplication.clipboard().setText(self.run.analysis.flat_text())
        _qt.notify(self, "已复制", "五步 + 话术卡全文已进剪贴板。")

    def export_analysis(self) -> None:
        if not (self.run and self.run.analysis):
            return
        from PySide6.QtWidgets import QFileDialog

        from app import paths

        default = str(paths.user_data_dir() / f"analysis-{time.strftime('%Y%m%d-%H%M%S')}.json")
        path, _ = QFileDialog.getSaveFileName(self, "导出 analysis.json", default, "JSON (*.json)")
        if not path:
            return
        self.run.save_json(path)
        _qt.notify(self, "已导出", path)

    # ------------------------------ 建档（存进长期记忆） ------------------------------
    def on_remember(self) -> None:
        """把本次分析里的「事件 / 假设」挑成候选，让用户确认后再写。

        为什么只挑这两类：官方规则不允许模型推断写进档案类，
        真正的档案只能由用户自己在档案页填写 —— 见 `memory/extract.py` 的表。
        """
        from app.memory import extract as ex
        from app.memory import service as svc

        if not (self.run and getattr(self.run.analysis, "ok", False)):
            _qt.notify(self, "还没有可存的结果", "先跑一次成功的分析。", ok=False)
            return

        rules = svc.rules_for(self.config)
        subject_code, subject_label = svc.current_subject(self.config)
        cands = ex.from_analysis(self.run, subject_id=subject_code,
                                transcript=self.transcript, rules=rules)
        if not cands:
            _qt.notify(self, "这次没有可存的条目",
                       "分析的「已知事实」里没有带编号的证据，「推断」也是空的，"
                       "所以没有可以安全存下来的东西。", ok=False, ms=7000)
            return

        # 用户在这一刻表达了「想记住」，才在这里征求同意——比开机就弹窗合适得多
        ok, why = svc.ensure_consent(
            self.config, ask=lambda reason: self._ask_consent(reason))
        if not ok:
            _qt.notify(self, "没有启用长期记忆", why, ok=False, ms=8000)
            return

        n, msg = svc.review_and_write(self.config, cands, parent=self,
                                     subject_label_text=subject_label)
        _qt.notify(self, f"已写入 {n} 条长期记忆" if n else "没有写入", msg,
                   ok=bool(n), ms=7000)
        if n:
            self._refresh_plan()

    def _ask_consent(self, reason: str) -> bool:
        from app.memory import service as svc
        from app.ui.consent_dialog import ConsentDialog

        dlg = ConsentDialog(svc.rules_for(self.config), self, reason=(reason or ""))
        return bool(dlg.exec())


__all__ = ["AnalysisPanel", "STEPS", "SCRIPT_TABS"]
