# -*- coding: utf-8 -*-
"""写入长期记忆前的**复核窗**（README.optimized §5.4「建档」）。

为什么要这一层：记忆一旦写进去，以后每次分析都会被它影响。让模型自己往里塞东西，
等于让它悄悄改写自己的输入。所以规则是——**候选先摆出来，用户点过才算**。

这个窗要做到三件事：

1. **看得见类别**：每条都标 `事件` / `假设`，并写清「这是模型推断」还是「记录里有出处」；
2. **能改**：字段与内容两列可直接编辑（模型给的句子经常太长或带了没用的修饰）；
3. **不骗人**：被官方规则拒绝的候选**单独灰掉并写明原因**，不混在可写的那堆里，
   也不在点「保存」之后才报错。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QFormLayout, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QPlainTextEdit, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from app.memory import extract as ex
from app.memory import store as store_mod
from app.ui._qt import BodyLabel, CaptionLabel, PrimaryPushButton, SubtitleLabel

COLUMNS = ("存", "类别", "字段", "内容", "来源 / 置信度")


class MemoryReviewDialog(QDialog):
    """展示候选记忆，返回用户勾选（可能改过）的那部分。"""

    def __init__(self, candidates: list[ex.Candidate], *, rules=None,
                 subject_label: str = "", parent=None):
        super().__init__(parent)
        self.candidates = list(candidates)
        self.rules = rules
        self._result: list[ex.Candidate] = []
        self.setWindowTitle("存进长期记忆")
        self.setMinimumSize(860, 560)
        self._build(subject_label)

    # ------------------------------ 构建 ------------------------------
    def _build(self, subject_label: str) -> None:
        root = QVBoxLayout(self)
        root.addWidget(SubtitleLabel("这次分析里，有这些值得记住", self))

        writable = len(ex.writable(self.candidates))
        blocked = len(self.candidates) - writable
        who = f"对象：{subject_label}　" if subject_label else ""
        head = (f"{who}共 {len(self.candidates)} 条候选，其中 {writable} 条可写入"
                + (f"，{blocked} 条被官方规则拒绝（已灰掉）" if blocked else "") + "。")
        lbl = BodyLabel(head, self)
        lbl.setWordWrap(True)
        root.addWidget(lbl)

        self.table = QTableWidget(len(self.candidates), len(COLUMNS), self)
        self.table.setHorizontalHeaderLabels(list(COLUMNS))
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(0, 36)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(1, 76)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        self.table.setColumnWidth(2, 170)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(4, 150)
        self._fill()
        self.table.currentCellChanged.connect(lambda *_: self._show_reason())
        root.addWidget(self.table, 1)

        self.lbl_reason = CaptionLabel("", self)
        self.lbl_reason.setWordWrap(True)
        root.addWidget(self.lbl_reason)

        row = QHBoxLayout()
        self.btn_all = QPushButton("全选", self)
        self.btn_all.clicked.connect(lambda: self._check_all(True))
        self.btn_none = QPushButton("全不选", self)
        self.btn_none.clicked.connect(lambda: self._check_all(False))
        row.addWidget(self.btn_all)
        row.addWidget(self.btn_none)
        row.addStretch(1)
        self.lbl_count = QLabel("", self)
        row.addWidget(self.lbl_count)
        self.btn_cancel = QPushButton("取消", self)
        self.btn_cancel.clicked.connect(self.reject)
        self.btn_ok = PrimaryPushButton("写入勾选的条目", self)
        self.btn_ok.clicked.connect(self.accept)
        row.addWidget(self.btn_cancel)
        row.addWidget(self.btn_ok)
        root.addLayout(row)

        self.table.itemChanged.connect(lambda *_: self._sync())
        self._show_reason()
        self._sync()

    def _fill(self) -> None:
        self.table.blockSignals(True)
        for r, c in enumerate(self.candidates):
            check = QTableWidgetItem()
            if c.blocked:
                check.setText("—")
                check.setFlags(Qt.ItemFlag.NoItemFlags)
                check.setToolTip(c.blocked)
            else:
                check.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
                check.setCheckState(Qt.CheckState.Checked if c.checked
                                    else Qt.CheckState.Unchecked)
            self.table.setItem(r, 0, check)

            tag = QTableWidgetItem(store_mod.SCOPE_CN.get(c.scope, c.scope))
            tag.setFlags(Qt.ItemFlag.ItemIsEnabled)
            tag.setToolTip(c.reason)
            self.table.setItem(r, 1, tag)

            for col, text in ((2, c.field), (3, c.value)):
                item = QTableWidgetItem(text)
                if c.blocked:
                    item.setFlags(Qt.ItemFlag.ItemIsEnabled)
                else:
                    item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsEditable)
                item.setToolTip(c.reason)
                self.table.setItem(r, col, item)

            meta = QTableWidgetItem(c.meta)
            meta.setFlags(Qt.ItemFlag.ItemIsEnabled)
            meta.setToolTip(c.reason)
            self.table.setItem(r, 4, meta)
        self.table.blockSignals(False)

    # ------------------------------ 交互 ------------------------------
    def _show_reason(self) -> None:
        r = self.table.currentRow()
        if not (0 <= r < len(self.candidates)):
            self.lbl_reason.setText("选中某一行可以看到「为什么要存这条」。")
            return
        c = self.candidates[r]
        if c.blocked:
            self.lbl_reason.setText(f"⚠ 这条不能写入：{c.blocked}")
        else:
            self.lbl_reason.setText("理由：" + c.reason)

    def _check_all(self, on: bool) -> None:
        self.table.blockSignals(True)
        for r, c in enumerate(self.candidates):
            if c.blocked:
                continue
            item = self.table.item(r, 0)
            if item is not None:
                item.setCheckState(Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)
        self.table.blockSignals(False)
        self._sync()

    def _row_checked(self, r: int) -> bool:
        item = self.table.item(r, 0)
        return bool(item and item.checkState() == Qt.CheckState.Checked)

    def _sync(self) -> None:
        n = sum(1 for r in range(len(self.candidates)) if self._row_checked(r))
        self.lbl_count.setText(f"已勾选 {n} 条")
        self.btn_ok.setEnabled(n > 0)
        self.btn_ok.setText(f"写入勾选的 {n} 条" if n else "写入勾选的条目")

    # ------------------------------ 结果 ------------------------------
    def _collect(self) -> tuple[list[ex.Candidate], list[str]]:
        """把用户改过的字段/内容读回来，并对编辑后的值再跑一次官方预检。

        返回 (可写入的条目, 问题列表)。**不静默丢弃**——改坏了必须让用户知道是哪一行。
        """
        out: list[ex.Candidate] = []
        problems: list[str] = []
        for r, c in enumerate(self.candidates):
            if c.blocked or not self._row_checked(r):
                continue
            field_item = self.table.item(r, 2)
            value_item = self.table.item(r, 3)
            cand = ex.Candidate(
                scope=c.scope, subject_id=c.subject_id,
                field=(field_item.text().strip() if field_item else c.field),
                value=(value_item.text().strip() if value_item else c.value),
                source_type=c.source_type, confidence=c.confidence,
                source_ref=c.source_ref, occurred_at=c.occurred_at,
                reason=c.reason, checked=True,
            )
            ex.mark_blocked([cand], rules=self.rules)  # 改过之后要重新过一遍官方的规则
            if cand.blocked:
                problems.append(f"第 {r + 1} 行「{cand.field or '（空字段）'}」：{cand.blocked}")
                continue
            out.append(cand)
        return out, problems

    def accept(self) -> None:  # type: ignore[override]
        """先校验再放行。改坏了就停在这里说清楚，别等写入时才弹错。"""
        items, problems = self._collect()
        if problems:
            self.lbl_reason.setText("⚠ 有改动不符合官方规则，先改回来：\n" + "\n".join(problems[:5]))
            self.btn_ok.setEnabled(True)
            return
        if not items:
            self.lbl_reason.setText("⚠ 没有可写入的条目：至少要勾选一条。")
            return
        self._result = items
        super().accept()

    def selected(self) -> list[ex.Candidate]:
        return list(getattr(self, "_result", []))


SCOPE_CHOICES = (
    ("user", "用户档案 —— 关于你自己、由你明确说明的稳定信息"),
    ("object", "对象快照 —— 你确认过的、关于对方的事实"),
    ("relationship", "关系快照 —— 你们目前是什么关系"),
    ("event", "事件 —— 发生过的具体事情"),
    ("hypothesis", "假设 —— 你的猜测（存下来也会标成假设，不当事实用）"),
)


class ManualEntryDialog(QDialog):
    """手动添加一条记忆。

    自动提取**只**产出「事件」和「假设」，因为官方规则不允许把模型推断写进
    档案类（user / object / relationship）。用户自己的情况只有用户能说，
    所以这三类留一个手动入口。

    两个容易写错的地方这里自动处理：`user` 类别的 subject_id 必须是字面量 `"user"`；
    手动填写一律用 `user_explicit`（它是唯一对所有 scope 都合法的来源类型）。
    """

    def __init__(self, *, rules=None, config=None, default_subject: str = "",
                 default_label: str = "", parent=None):
        super().__init__(parent)
        self.rules = rules
        self.config = config
        self.default_subject = default_subject
        self.default_label = default_label
        self._result: ex.Candidate | None = None
        self.setWindowTitle("手动添加一条记忆")
        self.setMinimumWidth(600)
        self._build()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.addWidget(SubtitleLabel("手动记一条", self))
        tip = BodyLabel("你写的会被当作「用户明确陈述」存进本机。类别不同，将来的用法也不同。",
                        self)
        tip.setWordWrap(True)
        root.addWidget(tip)

        form = QFormLayout()
        self.scope_box = QComboBox(self)
        for key, label in SCOPE_CHOICES:
            self.scope_box.addItem(label, userData=key)
        self.scope_box.currentIndexChanged.connect(self._on_scope_changed)
        form.addRow("类别", self.scope_box)

        self.subject_box = QComboBox(self)
        self.subject_box.setEditable(True)
        self._fill_subjects()
        form.addRow("归属对象", self.subject_box)

        self.field = QLineEdit(self)
        self.field.setPlaceholderText("短标签，例：工作 / 目前状态 / 我最在意的事")
        form.addRow("字段", self.field)

        self.value = QPlainTextEdit(self)
        self.value.setPlaceholderText("要记住的内容（上限见下方校验提示）")
        self.value.setFixedHeight(96)
        form.addRow("内容", self.value)
        root.addLayout(form)

        self.lbl_check = CaptionLabel("", self)
        self.lbl_check.setWordWrap(True)
        root.addWidget(self.lbl_check)

        row = QHBoxLayout()
        row.addStretch(1)
        btn_cancel = QPushButton("取消", self)
        btn_cancel.clicked.connect(self.reject)
        self.btn_ok = PrimaryPushButton("存进记忆", self)
        self.btn_ok.clicked.connect(self.accept)
        row.addWidget(btn_cancel)
        row.addWidget(self.btn_ok)
        root.addLayout(row)

        self.field.textChanged.connect(self._validate)
        self.value.textChanged.connect(self._validate)
        self.subject_box.currentTextChanged.connect(lambda *_: self._validate())
        self._on_scope_changed()
        self._validate()

    def _fill_subjects(self) -> None:
        try:
            from app.memory import service as svc

            objs = svc.objects(self.config) if self.config is not None else []
        except Exception:  # noqa: BLE001
            objs = []
        self.subject_box.clear()
        for o in objs:
            self.subject_box.addItem(f"{o.get('label') or o.get('code')}（{o.get('code')}）",
                                     userData=str(o.get("code")))
        if not objs:
            self.subject_box.addItem(self.default_label or "对方", userData=self.default_subject or "obj-1")

    def _scope(self) -> str:
        return str(self.scope_box.currentData() or "user")

    def _on_scope_changed(self) -> None:
        # user 档案的 subject_id 官方规定就是字面量 "user"，让用户改反而会写出错的
        is_user = self._scope() == "user"
        self.subject_box.setEnabled(not is_user)
        self._validate()

    def _subject(self) -> str:
        if self._scope() == "user":
            return "user"
        data = self.subject_box.currentData()
        text = self.subject_box.currentText()
        if isinstance(text, str) and text.endswith("）") and "（" in text:
            text = text[text.rfind("（") + 1:-1]
        return str(data or text or self.default_subject or "obj-1")

    def _candidate(self) -> ex.Candidate:
        return ex.manual_candidate(
            self._scope(), self.field.text(), self.value.toPlainText(),
            subject_id=self._subject(), rules=self.rules)

    def _validate(self) -> None:
        cand = self._candidate()
        if cand.blocked:
            self.lbl_check.setText("⚠ 不能这样存：" + cand.blocked)
            self.btn_ok.setEnabled(False)
            return
        if not self.field.text().strip():
            self.lbl_check.setText("填一个字段名（当作去重用的键）。")
            self.btn_ok.setEnabled(False)
            return
        self.lbl_check.setText(f"可以存：将写入 {cand.label}　{cand.meta}　"
                               f"{len(cand.value)}/{self._limit()} 字")
        self.btn_ok.setEnabled(True)

    def _limit(self) -> int:
        if self.rules is not None and self.rules.available:
            return self.rules.max_value_chars
        return store_mod.FALLBACK_MAX_VALUE_CHARS

    def accept(self) -> None:  # type: ignore[override]
        cand = self._candidate()
        if cand.blocked or not self.field.text().strip():
            self._validate()
            return
        self._result = cand
        super().accept()

    def candidate(self) -> ex.Candidate | None:
        return self._result


__all__ = ["COLUMNS", "ManualEntryDialog", "MemoryReviewDialog", "SCOPE_CHOICES"]
