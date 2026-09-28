# -*- coding: utf-8 -*-
"""档案 / 记忆页：状态、清单、以及全部用户控制（README.optimized §5.4）。

三条规矩：

1. **权威状态来自官方库**。界面上的「已启用 / 已暂停 / N 条」全部读自
   `memory_store.py status`，不用 config 里的缓存当真——缓存只用来省一次子进程。
2. **子进程不进 UI 线程**。官方脚本每次调用约 250 ms（实测），一次刷新要调两次，
   同步做就是 500 ms 的卡顿。所以刷新与所有操作都丢给 `MemoryJob` 线程，
   只有**对话框和确认框**留在 UI 线程（Qt 要求）。
3. **破坏性操作必须说清后果**。删除对象、撤回同意、清空全部都写明「不可恢复」
   并显示**具体影响多少条**，而不是一句「确定吗？」。
"""
from __future__ import annotations

from PySide6.QtCore import QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QHeaderView, QInputDialog, QMessageBox, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from app.memory import consent as consent_mod
from app.memory import service as svc
from app.memory import store as store_mod
from app.ui import _qt
from app.ui._qt import (BodyLabel, CaptionLabel, ComboBox, PrimaryPushButton, PushButton,
                        SubtitleLabel)

COLUMNS = ("类别", "归属", "字段", "内容", "来源", "置信度", "更新时间")
ALL_SUBJECTS = "__all__"


class MemoryJob(QThread):
    """把一次「读状态 + 列清单」或一次写操作放到线程里跑。

    每帧只发一个 `(ok, payload)`：UI 侧不需要知道内部调了几次官方脚本。
    """

    finishedJob = Signal(object)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self._fn = fn

    def run(self) -> None:
        try:
            self.finishedJob.emit((True, self._fn()))
        except Exception as e:  # noqa: BLE001 - 子进程层的异常不该炸掉界面
            self.finishedJob.emit((False, f"{type(e).__name__}: {e}"))


class ProfilePanel(QWidget):
    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config
        self._job: MemoryJob | None = None
        self._st: dict = {}
        self._rows: list[dict] = []
        self._rules_ok = True
        self._rules_err = ""
        self._build()

    # ------------------------------ 构建 ------------------------------
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(8)

        root.addWidget(SubtitleLabel("本机档案与记忆", self))

        self.lbl_consent = BodyLabel("", self)
        self.lbl_consent.setWordWrap(True)
        root.addWidget(self.lbl_consent)

        self.lbl_status = BodyLabel("", self)
        self.lbl_status.setWordWrap(True)
        root.addWidget(self.lbl_status)

        row1 = QHBoxLayout()
        self.btn_enable = PrimaryPushButton("启用记忆", self)
        self.btn_enable.clicked.connect(self.on_enable)
        self.btn_pause = PushButton("暂停", self)
        self.btn_pause.clicked.connect(self.on_pause)
        self.btn_resume = PushButton("恢复", self)
        self.btn_resume.clicked.connect(self.on_resume)
        self.btn_undo = PushButton("撤销上一条", self)
        self.btn_undo.clicked.connect(self.on_undo)
        self.btn_refresh = PushButton("刷新", self)
        self.btn_refresh.clicked.connect(self.refresh)
        for b in (self.btn_enable, self.btn_pause, self.btn_resume, self.btn_undo,
                  self.btn_refresh):
            row1.addWidget(b)
        row1.addStretch(1)
        root.addLayout(row1)

        row2 = QHBoxLayout()
        self.btn_manual = PushButton("手动记一条", self)
        self.btn_manual.clicked.connect(self.on_manual)
        self.btn_forget = PushButton("删除某个对象", self)
        self.btn_forget.clicked.connect(self.on_forget_object)
        self.btn_revoke = PushButton("撤回同意", self)
        self.btn_revoke.clicked.connect(self.on_revoke)
        self.btn_clear = PushButton("清空全部", self)
        self.btn_clear.clicked.connect(self.on_clear)
        self.btn_open = PushButton("打开数据目录", self)
        self.btn_open.clicked.connect(self.on_open_dir)
        for b in (self.btn_manual, self.btn_forget, self.btn_revoke, self.btn_clear,
                  self.btn_open):
            row2.addWidget(b)
        row2.addStretch(1)
        root.addLayout(row2)

        filter_row = QHBoxLayout()
        filter_row.addWidget(BodyLabel("只看", self))
        self.filter_box = ComboBox(self)
        self.filter_box.currentIndexChanged.connect(lambda *_: self._fill_table())
        filter_row.addWidget(self.filter_box)
        filter_row.addStretch(1)
        root.addLayout(filter_row)

        self.table = QTableWidget(0, len(COLUMNS), self)
        self.table.setHorizontalHeaderLabels(list(COLUMNS))
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        header = self.table.horizontalHeader()
        for i in range(len(COLUMNS)):
            header.setSectionResizeMode(i, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(0, 90)
        self.table.setColumnWidth(1, 80)
        self.table.setColumnWidth(2, 160)
        self.table.setColumnWidth(4, 96)
        self.table.setColumnWidth(5, 70)
        self.table.setColumnWidth(6, 150)
        root.addWidget(self.table, 1)

        self.lbl_busy = CaptionLabel("", self)
        root.addWidget(self.lbl_busy)

        self.detail = CaptionLabel("", self)
        self.detail.setWordWrap(True)
        root.addWidget(self.detail)

        self._render_state()

    # ------------------------------ 刷新 ------------------------------
    def refresh(self) -> None:
        """读官方状态 + 列全部记忆。两次子进程一起放进后台线。"""
        if self._busy():
            return

        def job():
            st = svc.stats(self.config)
            rows, err = svc.list_memories(self.config)   # 不带 subject-id = 全部
            rules = svc.rules_for(self.config)
            return {"st": st, "rows": rows, "err": err,
                    "rules_ok": rules.available, "rules_err": rules.error}

        # recollect=False：刷新本身不需要再回读一次，否则会自己触发自己
        self._run(job, "正在读取记忆状态…", self._on_refreshed, recollect=False)

    def _on_refreshed(self, ok, payload) -> None:
        if not ok:
            self.detail.setText(f"读取记忆状态失败：{payload}")
            return
        self._st = payload.get("st") or {}
        self._rows = payload.get("rows") or []
        self._rules_ok = bool(payload.get("rules_ok"))
        self._rules_err = str(payload.get("rules_err") or "")
        self._sync_filter()
        self._render_state()
        self._fill_table()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        """切到本页就刷一次：别让用户看到一个可能是几分钟前的状态。"""
        super().showEvent(event)
        self.refresh()

    # ------------------------------ 渲染 ------------------------------
    def _sync_filter(self) -> None:
        """筛选下拉：全部 / 我 / 实际出现过的对象（用记忆里的 subject_id，不用配置猜）。"""
        present = {str(r.get("subject_id") or "") for r in self._rows}
        codes = sorted(present - {"", "user"})
        want = [ALL_SUBJECTS, "user"] + codes
        now = [self.filter_box.itemData(i) for i in range(self.filter_box.count())]
        if now == want:
            return
        keep = self.filter_box.currentData()
        self.filter_box.blockSignals(True)
        self.filter_box.clear()
        self.filter_box.addItem("全部", userData=ALL_SUBJECTS)
        self.filter_box.addItem("我（用户档案）", userData="user")
        for c in codes:
            self.filter_box.addItem(f"{svc.subject_label(self.config, c)}（{c}）", userData=c)
        idx = self.filter_box.findData(keep)
        self.filter_box.setCurrentIndex(idx if idx >= 0 else 0)
        self.filter_box.blockSignals(False)

    def _render_state(self) -> None:
        st = self._st or {}
        rules_version = store_mod.FALLBACK_POLICY_VERSION
        try:
            rules_version = svc.rules_for(self.config).policy_version
        except Exception:  # noqa: BLE001
            pass
        need, why = consent_mod.need_consent(rules_version)
        c = consent_mod.load()

        if need:
            if c.accepted:
                self.lbl_consent.setText(f"同意记录：需要重新确认 —— {why}")
            else:
                self.lbl_consent.setText("同意记录：未同意（首次启用会弹一次说明）")
        else:
            self.lbl_consent.setText(f"同意记录：已同意（{c.at or '时间未记录'}）")

        if not st.get("exists"):
            state = "记忆库：尚未创建"
        elif st.get("consent_enabled") and st.get("paused"):
            state = "记忆库：已暂停"
        elif st.get("consent_enabled"):
            state = "记忆库：已启用"
        else:
            state = "记忆库：已存在，但同意已撤回"
        bits = [state, f"条数：{st.get('memory_count', 0)} / 200",
                f"撤销栈：{st.get('undo_count', 0)} / 20"]
        self.lbl_status.setText("　".join(bits))
        self.detail.setText("位置：" + str(st.get("path") or store_mod.db_path())
                            + (("　（读取失败：" + str(st.get("error")) + "）")
                               if st.get("error") else ""))

        if not self._rules_ok:
            self.detail.setText(self.detail.text()
                                + "　｜ 注意：读不到官方校验模块，写入前预检已停用（"
                                + self._rules_err[:60] + "）")

        enabled = bool(st.get("consent_enabled"))
        paused = bool(st.get("paused"))
        exists = bool(st.get("exists"))
        self.btn_enable.setEnabled(not enabled or need)
        # 需要征求意见时把话说在前面：这个按钮点下去会先弹说明，不是直接就开
        self.btn_enable.setText("启用记忆（需确认说明）" if need else "启用记忆")
        self.btn_pause.setEnabled(enabled and not paused)
        self.btn_resume.setEnabled(enabled and paused)
        self.btn_undo.setEnabled(exists and int(st.get("undo_count") or 0) > 0)
        self.btn_manual.setEnabled(enabled and not paused)
        self.btn_forget.setEnabled(exists and bool(self._subject_codes()))
        self.btn_revoke.setEnabled(exists)
        self.btn_clear.setEnabled(exists)

    def _fill_table(self) -> None:
        who = self.filter_box.currentData() or ALL_SUBJECTS
        rows = self._rows if who == ALL_SUBJECTS else \
            [r for r in self._rows if str(r.get("subject_id") or "") == who]
        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            scope = str(r.get("scope") or "")
            sid = str(r.get("subject_id") or "")
            vals = (
                store_mod.SCOPE_CN.get(scope, scope),
                "我" if sid == "user" else svc.subject_label(self.config, sid),
                str(r.get("field") or ""),
                str(r.get("value") or ""),
                store_mod.SRC_CN.get(str(r.get("source_type") or ""),
                                     str(r.get("source_type") or "")),
                store_mod.CONF_CN.get(str(r.get("confidence") or ""),
                                     str(r.get("confidence") or "")),
                str(r.get("updated_at") or r.get("observed_at") or "")[:19].replace("T", " "),
            )
            for col, text in enumerate(vals):
                item = QTableWidgetItem(text)
                item.setToolTip(text)
                self.table.setItem(i, col, item)

    def _subject_codes(self) -> list[str]:
        return sorted({str(r.get("subject_id") or "") for r in self._rows} - {"", "user"})

    # ------------------------------ 线程调度 ------------------------------
    def _busy(self) -> bool:
        if self._job is not None and self._job.isRunning():
            _qt.notify(self, "请稍等", "上一个记忆操作还没结束。", ok=False)
            return True
        return False

    def _run(self, fn, label: str, on_done=None, *, recollect: bool = True) -> None:
        """跑一个后台任务。`recollect=True` 表示写操作，完成后要回读一次真实状态。

        `recollect` 必须显式传，**不能靠比较回调对象来判断**：
        绑定方法是每次属性访问都新建的对象，`on_done is self._on_refreshed` 永远为假，
        那样刷新会自己触发自己，变成无限刷新。
        """
        self.lbl_busy.setText(label)
        self._set_buttons(False)
        job = MemoryJob(fn, self)
        job.finishedJob.connect(lambda res: self._on_job(res, on_done, recollect))
        self._job = job
        job.start()

    def _on_job(self, res, on_done, recollect: bool) -> None:
        self.lbl_busy.setText("")
        ok, payload = res
        # 先松手再回读：线程的 finished 信号可能还没跑到，isRunning() 仍是 True，
        # 不清掉就会把紧接着的 refresh() 当成「上一次还没完」而拒掉。
        self._job = None
        self._set_buttons(True)
        if on_done is not None:
            on_done(ok, payload)
        elif ok:
            _qt.notify(self, "完成", str(payload))
        else:
            _qt.notify(self, "操作失败", str(payload)[:200], ok=False, ms=8000)
        # 写操作后一律回读一次真实状态，不用本地推测
        if recollect:
            self.refresh()
        else:
            self._render_state()

    def _set_buttons(self, on: bool) -> None:
        for b in (self.btn_enable, self.btn_pause, self.btn_resume, self.btn_undo,
                  self.btn_refresh, self.btn_manual, self.btn_forget, self.btn_revoke,
                  self.btn_clear, self.btn_open):
            b.setEnabled(on)
        if on:
            self._render_state()

    # ------------------------------ 确认框 ------------------------------
    def _confirm(self, title: str, text: str) -> bool:
        """破坏性操作的确认。默认按钮是「取消」，不会一回车就把东西删了。"""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(title)
        box.setText(text)
        yes = box.addButton("确认执行", QMessageBox.ButtonRole.DestructiveRole)
        cancel = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel)
        box.exec()
        return box.clickedButton() is yes

    def _ask_consent(self, why: str) -> bool:
        from app.ui.consent_dialog import ConsentDialog

        dlg = ConsentDialog(svc.rules_for(self.config), self, reason=(why or ""))
        return bool(dlg.exec())

    # ------------------------------ 操作 ------------------------------
    def on_enable(self) -> None:
        if self._busy():
            return
        plan = svc.consent_plan(self.config, self._st)
        if plan.already_ok:
            _qt.notify(self, "已经启用", "长期记忆正在生效中。")
            return
        if plan.need and not self._ask_consent(plan.why):
            _qt.notify(self, "已取消", "没有同意就不会启用长期记忆。", ok=False)
            return
        if plan.need:
            consent_mod.accept(plan.policy_version)
        self._run(lambda: svc.enable_now(self.config), "正在启用记忆库…",
                  lambda ok, res: _qt.notify(
                      self, "已启用" if ok and getattr(res, "ok", False) else "启用失败",
                      "长期记忆已开启。分析时会自动带上已存的档案。"
                      if ok and getattr(res, "ok", False) else str(res)[:200],
                      ok=bool(ok and getattr(res, "ok", False)), ms=6000))

    def on_pause(self) -> None:
        if self._busy():
            return
        script = svc.script_path(self.config)
        self._run(lambda: store_mod.pause(script), "正在暂停…",
                  lambda ok, res: _qt.notify(self, "已暂停", "已停止读写，已存内容保留。",
                                             ok=bool(ok and getattr(res, "ok", False))))

    def on_resume(self) -> None:
        if self._busy():
            return
        script = svc.script_path(self.config)
        self._run(lambda: store_mod.resume(script), "正在恢复…",
                  lambda ok, res: _qt.notify(self, "已恢复", "长期记忆重新生效。",
                                             ok=bool(ok and getattr(res, "ok", False))))

    def on_undo(self) -> None:
        if self._busy():
            return
        n = int((self._st or {}).get("memory_count") or 0)
        if not self._confirm("撤销上一条记忆更新",
                            f"会退回**最近一次**写入/覆盖前的状态（当前共 {n} 条）。\n"
                            "这一步只影响那一次操作，不是清空。\n\n确认执行？"):
            return
        script = svc.script_path(self.config)
        self._run(lambda: store_mod.undo(script), "正在撤销…",
                  lambda ok, res: _qt.notify(
                      self, "已撤销" if ok and getattr(res, "ok", False) else "撤销失败",
                      "已退回上一次更新前的状态。" if ok and getattr(res, "ok", False)
                      else str(res)[:200], ok=bool(ok and getattr(res, "ok", False))))

    def on_forget_object(self) -> None:
        if self._busy():
            return
        codes = self._subject_codes()
        if not codes:
            _qt.notify(self, "没有可删除的对象", "现在只存了「我」的档案。", ok=False)
            return
        labels = [f"{svc.subject_label(self.config, c)}（{c}）" for c in codes]
        item, ok = QInputDialog.getItem(self, "删除某个对象", "选择要永久删除的对象：",
                                        labels, 0, False)
        if not ok or not item:
            return
        code = codes[labels.index(item)]
        cnt = len([r for r in self._rows if str(r.get("subject_id") or "") == code])
        if not self._confirm("永久删除对象档案",
                            f"将删除 {svc.subject_label(self.config, code)} 的**全部 {cnt} 条**记忆，"
                            "并清空撤销栈。\n**此操作不可恢复，也无法撤销。**\n\n"
                            "只想暂时不用，请改点「暂停」。\n\n确认删除？"):
            return
        script = svc.script_path(self.config)
        self._run(lambda: store_mod.forget_object(script, code, confirm=True),
                  f"正在删除 {code} 的记忆…",
                  lambda ok, res: _qt.notify(
                      self, "已删除" if ok and getattr(res, "ok", False) else "删除失败",
                      f"已永久删除 {code} 的记忆。" if ok and getattr(res, "ok", False)
                      else str(res)[:200], ok=bool(ok and getattr(res, "ok", False))))

    def on_revoke(self) -> None:
        if self._busy():
            return
        n = int((self._st or {}).get("memory_count") or 0)
        if not self._confirm("撤回长期记忆同意",
                            "撤回后**立即停止**读写记忆，本次仍保留已存的 "
                            f"{n} 条（想连内容一起删，请用「清空全部」）。\n"
                            "以后想再用，要重新看一遍说明并同意。\n\n确认撤回？"):
            return
        script = svc.script_path(self.config)
        consent_mod.forget()

        def job():
            return store_mod.revoke(script, confirm=True, delete=False)

        self._run(job, "正在撤回同意…",
                  lambda ok, res: _qt.notify(
                      self, "已撤回" if ok and getattr(res, "ok", False) else "撤回失败",
                      "已停止读写记忆。" if ok and getattr(res, "ok", False)
                      else str(res)[:200], ok=bool(ok and getattr(res, "ok", False))))

    def on_clear(self) -> None:
        if self._busy():
            return
        n = int((self._st or {}).get("memory_count") or 0)
        path = str((self._st or {}).get("path") or store_mod.db_path())
        if not self._confirm("清空全部长期记忆",
                            f"将**删除整个记忆库文件**（当前 {n} 条），并同时撤回同意。\n"
                            f"{path}\n\n**此操作不可恢复：撤销栈也会一起没了。**\n\n"
                            "确认清空？"):
            return
        script = svc.script_path(self.config)
        consent_mod.forget()

        def job():
            # 库不存在时官方 clear 也是直接成功（删文件），所以不必先判 exists
            res = store_mod.clear(script, confirm=True)
            if not res.ok and res.code == "NOT_INITIALIZED":
                return store_mod.MemoryResult(True, "", "记忆库本来就不存在")
            return res

        self._run(job, "正在清空记忆库…",
                  lambda ok, res: _qt.notify(
                      self, "已清空" if ok and getattr(res, "ok", False) else "清空失败",
                      "本机记忆已全部删除。" if ok and getattr(res, "ok", False)
                      else str(res)[:200], ok=bool(ok and getattr(res, "ok", False))))

    def on_open_dir(self) -> None:
        target = store_mod.db_dir()
        target.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

    def on_manual(self) -> None:
        """手动记一条。

        这里**不再弹复核窗**：`ManualEntryDialog` 已经把类别、归属、长度校验和
        「将写入什么」摆给用户看过了，再叠一层确认只是让人多点一次。
        模型自动提取的那条路（`review_and_write`）才需要复核，因为内容是模型写的。
        """
        if self._busy():
            return
        from app.ui.memory_review_dialog import ManualEntryDialog

        code, label = svc.current_subject(self.config)
        dlg = ManualEntryDialog(rules=svc.rules_for(self.config), config=self.config,
                               default_subject=code, default_label=label, parent=self)
        if not dlg.exec():
            return
        cand = dlg.candidate()
        if cand is None:
            return
        ok, msg = svc.write_candidates(self.config, [cand])
        _qt.notify(self, f"已写入 {ok} 条" if ok else "没有写入", msg, ok=bool(ok), ms=6000)
        self.refresh()


__all__ = ["COLUMNS", "MemoryJob", "ProfilePanel"]
