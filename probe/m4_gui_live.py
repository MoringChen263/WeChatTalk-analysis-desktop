# -*- coding: utf-8 -*-
"""M4 GUI 端到端：真建面板、真起线程、真弹窗（把 exec 换成自动确认）、真写临时记忆库。

不开可见窗口（只构造控件 + processEvents），所以能无人值守跑。

    python probe/m4_gui_live.py

**全程指向临时目录**：`GOUTOUJUNSHI_MEMORY_DIR` 与 `JEV_DATA_DIR` 都改成 tempfile，
末尾断言用户真实记忆目录没被动过。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

_TMP = Path(tempfile.mkdtemp(prefix="jev-m4-gui-"))
os.environ["GOUTOUJUNSHI_MEMORY_DIR"] = str(_TMP / "memory")
os.environ["JEV_DATA_DIR"] = str(_TMP / "data")
os.environ.setdefault("JEV_FAKE_KEY", "test-key")

from app.capture.transcript import Message, Transcript  # noqa: E402
from app.config import Config  # noqa: E402
from app.memory import consent as mconsent  # noqa: E402
from app.memory import extract as mex  # noqa: E402
from app.memory import service as msvc  # noqa: E402
from app.memory import store as mstore  # noqa: E402
from fake_llm import Behavior, FakeLLM, write_fake_config  # noqa: E402

SCRIPT = ROOT / "skills" / "goutoujunshi" / "scripts" / "memory_store.py"
_REAL_DIR = Path(os.environ.get("LOCALAPPDATA") or str(Path.home())) / "goutoujunshi"
_REAL_BEFORE = _REAL_DIR.exists()

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗'} {name}" + (f"　{detail}" if detail else ""), flush=True)
    return cond


def section(title: str) -> None:
    print(f"\n{title}")


def pump(cond, timeout: float = 25.0) -> bool:
    """转事件循环直到 cond 成立（QThread 的排队信号靠 processEvents 投递）。"""
    from PySide6.QtWidgets import QApplication

    t0 = time.time()
    while time.time() - t0 < timeout:
        QApplication.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    return False


def idle(panel, timeout: float = 25.0) -> bool:
    """等档案页的后台任务跑完。"""
    return pump(lambda: panel._job is None, timeout)


class _Patched:
    """临时把某个对象的属性换掉，退出时还原。"""

    def __init__(self, obj, name: str, value):
        self.obj, self.name, self.value = obj, name, value

    def __enter__(self):
        self.old = getattr(self.obj, self.name)
        setattr(self.obj, self.name, self.value)
        return self

    def __exit__(self, *exc):
        setattr(self.obj, self.name, self.old)
        return False


def auto_exec(dialog_cls, *, accept: bool = True, mutate=None):
    """把对话框的 exec() 换成「自动点确定」，这样流程测试不用人点。

    `mutate(dlg)` 用来在「点确定」之前改控件（比如取消勾选某一行）。
    """

    def fake_exec(self):
        if mutate is not None:
            mutate(self)
        if not accept:
            return 0
        self.accept()
        return int(self.result())

    return _Patched(dialog_cls, "exec", fake_exec)


def load_transcript() -> Transcript:
    d = json.loads((ROOT / "probe" / "m1_transcript.json").read_text(encoding="utf-8"))
    msgs = [Message(**{k: v for k, v in m.items() if k in Message.__dataclass_fields__})
            for m in d.pop("messages", [])]
    t = Transcript(**{k: v for k, v in d.items() if k in Transcript.__dataclass_fields__})
    t.messages = msgs
    return t


def status() -> dict:
    return mstore.status_dict(SCRIPT)[0]


def main() -> int:  # noqa: C901 - 探针本来就是一条长流水
    from PySide6.QtWidgets import QApplication

    qapp = QApplication.instance() or QApplication([])
    cfg = Config(_TMP / "config.json")
    cfg.load()
    cfg.set("capture.speaker_map.objects",
            [{"code": "obj-1", "label": "小新", "side": "left"}], force=True)
    cfg.set("memory.enabled", False, force=True)

    import app.ui.profile_panel as pp
    from app.ui.consent_dialog import ConsentDialog
    from app.ui.memory_review_dialog import ManualEntryDialog, MemoryReviewDialog
    from app.ui.profile_panel import ProfilePanel

    # ---------- 1) 初始状态：未同意 / 库未创建 ----------
    section("[1] 档案页初始状态")
    panel = ProfilePanel(cfg)
    panel.refresh()
    idle(panel)
    check("档案页可构建并刷出状态", bool(panel._st), str(panel._st)[:60])
    check("显示未同意", "未同意" in panel.lbl_consent.text(), panel.lbl_consent.text())
    check("显示记忆库尚未创建", "尚未创建" in panel.lbl_status.text(),
          panel.lbl_status.text())
    check("显示条数与撤销栈", "条数：0 / 200" in panel.lbl_status.text()
          and "撤销栈：0 / 20" in panel.lbl_status.text(), panel.lbl_status.text())
    check("显示库位置", "memory.sqlite3" in panel.detail.text(), panel.detail.text()[:60])
    check("未启用时「暂停」不可点", not panel.btn_pause.isEnabled())
    check("未启用时「手动记一条」不可点", not panel.btn_manual.isEnabled())
    check("库不存在时「撤销上一条」不可点", not panel.btn_undo.isEnabled())
    check("库不存在时「清空全部」不可点", not panel.btn_clear.isEnabled())
    check("首次「启用记忆」可点", panel.btn_enable.isEnabled())

    # ---------- 2) 同意窗门禁 ----------
    section("[2] 同意窗（没勾选不许同意）")
    dlg = ConsentDialog(mstore.load_official(SCRIPT))
    check("同意按钮初始不可点", not dlg.btn_ok.isEnabled())
    check("说明正文里写清库位置", str(mstore.db_dir()) in dlg.view.toPlainText())
    check("说明正文里写清条数上限", "200" in dlg.view.toPlainText())
    dlg.chk.setChecked(True)
    check("勾选后同意按钮可点", dlg.btn_ok.isEnabled())
    dlg.chk.setChecked(False)
    check("取消勾选后又不可点", not dlg.btn_ok.isEnabled())
    check("默认焦点在「取消」上（回车不会误同意）",
          dlg.focusWidget() is dlg.btn_cancel, type(dlg.focusWidget()).__name__)

    # ---------- 3) 启用闭环 ----------
    section("[3] 同意 → 启用")
    before_asked = []
    ok, why = msvc.ensure_consent(cfg, ask=lambda r: (before_asked.append(r), False)[1],
                                  st=status())
    check("拒绝同意就不启用", not ok and not mstore.db_path().exists(), f"{ok} {why}")
    check("拒绝后状态仍是未创建", not status()["exists"])

    with auto_exec(ConsentDialog):
        panel.on_enable()
        idle(panel)
    check("同意并启用后库被创建", mstore.db_path().exists())
    check("同意记录落盘", mconsent.load().accepted)
    check("界面显示已同意", "已同意" in panel.lbl_consent.text(), panel.lbl_consent.text())
    check("界面显示已启用", "已启用" in panel.lbl_status.text(), panel.lbl_status.text())
    check("启用后「暂停」可点", panel.btn_pause.isEnabled())
    check("启用后「手动记一条」可点", panel.btn_manual.isEnabled())

    # ---------- 4) 手动记一条 ----------
    section("[4] 手动记一条")
    md = ManualEntryDialog(rules=mstore.load_official(SCRIPT), config=cfg,
                          default_subject="obj-1", default_label="小新")
    md.scope_box.setCurrentIndex(0)   # user
    check("选了用户档案 → 归属被强制为 user", md._subject() == "user", md._subject())
    check("选了用户档案 → 归属控件被禁用", not md.subject_box.isEnabled())
    md.field.setText("")
    md.value.setPlainText("慢热，需要确定性才敢投入")
    md._validate()
    check("没填字段名时不许提交", not md.btn_ok.isEnabled(), md.lbl_check.text())
    md.field.setText("我的性格")
    md._validate()
    check("填好后可提交", md.btn_ok.isEnabled(), md.lbl_check.text())
    check("校验提示里报了字数上限", "/200 字" in md.lbl_check.text(), md.lbl_check.text())

    md.value.setPlainText("字" * 500)
    md._validate()
    check("超长不拦提交（会按上限截断，提示里说清）",
          md.btn_ok.isEnabled() and "截断" not in md.lbl_check.text())
    md.value.setPlainText("慢热，需要确定性才敢投入")
    md._validate()
    md.accept()
    check("对话框交出了候选", md.candidate() is not None)
    n, msg = msvc.write_candidates(cfg, [md.candidate()])
    check("手动条目写入成功", n == 1, msg)
    rows, _e = mstore.memories(SCRIPT, "user")
    check("写入的是 user 档案且来源是「用户明确陈述」",
          rows and rows[0]["subject_id"] == "user"
          and rows[0]["source_type"] == "user_explicit",
          str(rows[:1])[:80])

    # 归属切换到对象时控件要恢复
    md2 = ManualEntryDialog(rules=mstore.load_official(SCRIPT), config=cfg,
                           default_subject="obj-1", default_label="小新")
    md2.scope_box.setCurrentIndex(2)   # object
    check("选了对象快照 → 归属控件可用且是 obj-1",
          md2.subject_box.isEnabled() and md2._subject() == "obj-1", md2._subject())

    # ---------- 5) 表格与筛选 ----------
    section("[5] 清单与筛选")
    msvc.write_candidates(cfg, [mex.manual_candidate("object", "工作", "在深圳做设计",
                                                     subject_id="obj-1"),
                                mex.manual_candidate("relationship", "目前状态", "还在互相试探",
                                                     subject_id="obj-1"),
                                mex.manual_candidate("object", "工作", "做教育行业",
                                                     subject_id="obj-2")])
    panel.refresh()
    idle(panel)
    check("刷新后表格有 4 行", panel.table.rowCount() == 4, str(panel.table.rowCount()))
    check("筛选下拉含「全部/我/两个对象」", panel.filter_box.count() == 4,
          [panel.filter_box.itemText(i) for i in range(panel.filter_box.count())])
    check("对象名取自配置里的 label（不是裸 code）",
          any("小新" in panel.filter_box.itemText(i) for i in range(panel.filter_box.count())),
          [panel.filter_box.itemText(i) for i in range(panel.filter_box.count())])

    idx = panel.filter_box.findData("obj-1")
    panel.filter_box.setCurrentIndex(idx)
    check("筛到 obj-1 只剩该对象的 2 条", panel.table.rowCount() == 2,
          str(panel.table.rowCount()))
    panel.filter_box.setCurrentIndex(0)
    check("筛回全部又变 4 行", panel.table.rowCount() == 4)
    check("表格首列是中文类别", panel.table.item(0, 0).text() in
          ("用户档案", "对象快照", "关系快照", "事件", "假设"), panel.table.item(0, 0).text())

    # ---------- 6) 撤销上一条 ----------
    section("[6] 撤销上一条")
    n_before = status()["memory_count"]
    check("撤销栈已记下 4 次写入", status()["undo_count"] == 4,
          str(status()["undo_count"]))
    check("「撤销上一条」已可点", panel.btn_undo.isEnabled())
    with _Patched(panel, "_confirm", lambda *a, **k: True):
        panel.on_undo()
        idle(panel)
    check("撤销后少了一条", status()["memory_count"] == n_before - 1,
          f"{n_before} → {status()['memory_count']}")
    with _Patched(panel, "_confirm", lambda *a, **k: False):
        n_now = status()["memory_count"]
        panel.on_undo()
        idle(panel)
    check("确认框选「取消」时什么都不做", status()["memory_count"] == n_now)

    # ---------- 7) 删除某个对象 ----------
    section("[7] 删除某个对象")
    # 上面那次撤销撤掉的正好是最后写入的 obj-2，先补回来才测得出「只删一个对象」
    msvc.write_candidates(cfg, [mex.manual_candidate("object", "工作", "做教育行业",
                                                     subject_id="obj-2")])
    got: dict = {}

    class _FakeInput:
        @staticmethod
        def getItem(parent, title, label, items, cur, editable):
            got["items"] = list(items)
            return ("小新（obj-1）", True)

    with _Patched(pp, "QInputDialog", _FakeInput), \
            _Patched(panel, "_confirm", lambda *a, **k: True):
        panel.on_forget_object()
        idle(panel)
    check("选择框列出的是「标签（code）」", "小新（obj-1）" in got.get("items", []),
          str(got.get("items")))
    left = {r["subject_id"] for r in mstore.memories(SCRIPT)[0]}
    check("obj-1 被清空、别的对象与 user 保留",
          "obj-1" not in left and {"obj-2", "user"} <= left, str(left))
    check("撤销栈被官方一并清空", status()["undo_count"] == 0)
    check("接口层级：库还在", status()["exists"])

    # ---------- 8) 撤回同意 ----------
    section("[8] 撤回同意")
    n_before = status()["memory_count"]
    with _Patched(panel, "_confirm", lambda *a, **k: True):
        panel.on_revoke()
        idle(panel)
    check("撤回后同意记录被清掉", not mconsent.load().accepted)
    check("撤回后官方库也标记未启用", not status()["consent_enabled"])
    check("撤回后数据仍保留（不是删库）", status()["memory_count"] == n_before,
          f"{n_before} → {status()['memory_count']}")
    panel.refresh()
    idle(panel)
    check("界面显示已存在但同意已撤回", "同意已撤回" in panel.lbl_status.text(),
          panel.lbl_status.text())
    check("撤回后「暂停」不可点", not panel.btn_pause.isEnabled())
    check("撤回后召回被拒", mstore.context(SCRIPT, "obj-1").code == "CONSENT_REQUIRED")
    check("撤回后重新「启用记忆」可点，且文案提示要先确认说明",
          panel.btn_enable.isEnabled() and "需确认说明" in panel.btn_enable.text(),
          panel.btn_enable.text())

    # ---------- 9) 清空全部 ----------
    section("[9] 清空全部")
    with auto_exec(ConsentDialog):
        panel.on_enable()
        idle(panel)
    check("重新启用后库还在", mstore.db_path().exists())
    with _Patched(panel, "_confirm", lambda *a, **k: True):
        panel.on_clear()
        idle(panel)
    check("清空后库文件被删除", not mstore.db_path().exists())
    panel.refresh()
    idle(panel)
    check("界面回到尚未创建", "尚未创建" in panel.lbl_status.text(),
          panel.lbl_status.text())
    check("清空后表格为空", panel.table.rowCount() == 0)

    # ---------- 10) 分析面板：召回注入 + 建档入口 ----------
    section("[10] 分析面板：召回与建档")
    from app.ui.analysis_panel import AnalysisPanel

    T = load_transcript()
    OK = (ROOT / "probe" / "fixtures" / "analysis_ok.json").read_text(encoding="utf-8")

    with FakeLLM([Behavior(kind="json", body=OK)]) as srv:
        write_fake_config(cfg, srv.base_url, stream=False)
        # 库已在 [9] 被清空 → 重新同意 + 启用，再手工塞一条档案进去
        mconsent.accept(mstore.load_official(SCRIPT).policy_version)
        mstore.enable(SCRIPT, confirm=True)
        msvc.write_candidates(cfg, [mex.manual_candidate("object", "工作", "在深圳做设计",
                                                         subject_id="obj-1")])
        cfg.set("memory.enabled", True, force=True)

        ap = AnalysisPanel(cfg)
        check("预检里交代了长期记忆状态",
              "长期记忆：已启用" in ap.lbl_plan.text(), ap.lbl_plan.text()[-60:])
        ap.set_transcript(T)
        check("未分析前「存进记忆」不可点", not ap.btn_remember.isEnabled())

        ap.start_analysis()
        pump(lambda: ap.run is not None and ap.worker is None, 30)
        check("分析跑完", ap.run is not None and ap.run.ok,
              getattr(ap.run, "mode", "?"))
        check("召回被注入了本次提示",
              any("已召回 1 条长期记忆" in w for w in ap.run.warnings),
              str(ap.run.warnings[:2]))
        check("分析成功后「存进记忆」可点", ap.btn_remember.isEnabled())

        n_before = status()["memory_count"]
        with auto_exec(MemoryReviewDialog):
            ap.on_remember()
        n_after = status()["memory_count"]
        check("「存进记忆」真的写进了库", n_after > n_before,
              f"{n_before} → {n_after}")
        rows, _e = mstore.memories(SCRIPT, "obj-1")
        added = [r for r in rows if r["source_type"] in ("chatlab", "assistant_inference")]
        check("写入的是「事件 / 假设」而不是档案类",
              added and all(r["scope"] in ("event", "hypothesis") for r in added),
              str([(r["scope"], r["source_type"]) for r in added]))
        check("事件带上了消息编号", any(r["source_ref"] for r in added),
              str([r["source_ref"] for r in added])[:60])
        check("召回文本又会带上这些新条目",
              "事件" in mstore.recall(SCRIPT, "obj-1").text)

        # 取消复核窗 → 什么都不写
        n_mid = status()["memory_count"]
        with auto_exec(MemoryReviewDialog, accept=False):
            ap.on_remember()
        check("复核窗点取消 → 一条都不写", status()["memory_count"] == n_mid)

        # 复核窗里取消勾选 → 也不写
        n_mid = status()["memory_count"]
        with auto_exec(MemoryReviewDialog, mutate=lambda d: d._check_all(False)):
            ap.on_remember()
        check("复核窗里全不选 → 一条都不写", status()["memory_count"] == n_mid)

    # ---------- 11) 复核窗的细节 ----------
    section("[11] 复核窗：勾选、编辑、越界拦截")
    cands = mex.from_analysis(ap.run, subject_id="obj-1", transcript=T,
                              rules=mstore.load_official(SCRIPT))
    check("从分析里提出了候选", len(cands) >= 3, str(len(cands)))
    rd = MemoryReviewDialog(cands, rules=mstore.load_official(SCRIPT),
                           subject_label="小新")
    check("表格行数等于候选数", rd.table.rowCount() == len(cands))
    check("只在候选数 > 0 时可提交", rd.btn_ok.isEnabled())
    check("初始全选，按钮显示条数", f"写入勾选的 {len(cands)} 条" in rd.btn_ok.text(),
          rd.btn_ok.text())
    check("每行带了「为什么要存」的提示",
          bool(rd.table.item(0, 1).toolTip()), rd.table.item(0, 1).toolTip()[:40])
    rd._check_all(False)
    check("全不选后按钮禁用", not rd.btn_ok.isEnabled(), rd.btn_ok.text())
    rd._check_all(True)

    rd.accept()
    check("正常情况可以提交", rd.result() == 1 and len(rd.selected()) == len(cands),
          f"result={rd.result()} n={len(rd.selected())}")

    # 把内容改成空 → 必须拦住并说清是哪一行
    rd2 = MemoryReviewDialog(cands, rules=mstore.load_official(SCRIPT))
    rd2.table.item(0, 3).setText("")
    rd2.accept()
    check("内容被改空 → 拦住不放行", rd2.result() == 0, f"result={rd2.result()}")
    check("并说清是哪一行不对", "不符合官方规则" in rd2.lbl_reason.text()
          and "第 1 行" in rd2.lbl_reason.text(), rd2.lbl_reason.text()[:60])

    # 编辑成合法内容 → 写进去的应该是我改过的
    rd3 = MemoryReviewDialog(cands, rules=mstore.load_official(SCRIPT))
    rd3.table.item(0, 2).setText("自检改过的字段")
    rd3.table.item(0, 3).setText("自检改过的内容")
    rd3.accept()
    picked = rd3.selected()
    check("编辑后的内容被采纳",
          picked and picked[0].field == "自检改过的字段"
          and picked[0].value == "自检改过的内容",
          f"{picked[0].field}/{picked[0].value}" if picked else "empty")

    # 被官方拒绝的候选必须灰掉、不可勾
    bad = mex.manual_candidate("object", "越界", "x", subject_id="obj-1")
    bad.source_type = "assistant_inference"
    mex.mark_blocked([bad], rules=mstore.load_official(SCRIPT))
    rd4 = MemoryReviewDialog([bad], rules=mstore.load_official(SCRIPT))
    check("越界候选被标成不可勾（—）", rd4.table.item(0, 0).text() == "—",
          rd4.table.item(0, 0).text())
    check("越界候选的提交按钮禁用", not rd4.btn_ok.isEnabled())
    check("越界原因写在提示里", "只接受" in rd4.table.item(0, 0).toolTip(),
          rd4.table.item(0, 0).toolTip()[:40])

    # ---------- 12) 装配：右栏预检会跟着记忆开关变 ----------
    section("[12] 装配与联动")
    from app.ui.main_window import AnalyzerPage

    page = AnalyzerPage(cfg)
    check("主页面里左栏右栏都在",
          hasattr(page, "capture_panel") and hasattr(page, "analysis_panel"))
    page.analysis_panel.set_transcript(T)
    before = page.analysis_panel.lbl_plan.text()
    cfg.set("memory.enabled", not bool(cfg.get("memory.enabled", False)), force=True)
    page.analysis_panel._refresh_plan()
    after = page.analysis_panel.lbl_plan.text()
    check("预检会随记忆开关变化", before != after, after[-46:])
    check("两种状态的文案都在预期里",
          ("长期记忆：已启用" in before and "长期记忆：未启用" in after)
          or ("长期记忆：未启用" in before and "长期记忆：已启用" in after),
          f"{before[-20:]} → {after[-20:]}")

    check("没有碰用户真实的记忆目录", _REAL_DIR.exists() == _REAL_BEFORE,
          f"{_REAL_DIR} 从 {_REAL_BEFORE} 变成了 {_REAL_DIR.exists()}")
    print(f"\n结果：通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败项：" + "、".join(FAIL))
    return 0 if not FAIL else 1


if __name__ == "__main__":
    code = main()
    shutil.rmtree(_TMP, ignore_errors=True)
    raise SystemExit(code)
