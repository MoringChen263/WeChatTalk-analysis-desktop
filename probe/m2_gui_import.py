# -*- coding: utf-8 -*-
"""M2 GUI 端到端：真建窗口，走左栏「导入」按钮的完整路径（含问「哪个昵称是你」那一支）。

模态对话框没法自动点，所以用面板自己留的接缝 `_ask_file` / `_ask_me_label` 替换掉。
跑 4 个文件 × 2 条分支（自动认 me / 必须问人），断言 transcript 真的进了面板并发出信号。

    python probe/m2_gui_import.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FIX = ROOT / "probe" / "fixtures"
PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"　— {detail}" if detail else ""),
          flush=True)


def main() -> int:
    from PySide6.QtWidgets import QApplication

    from app.config import load_config
    from app.ui import _qt
    from app.ui.main_window import MainWindow

    cfg = load_config()
    app = QApplication(sys.argv)
    _qt.apply_theme()
    win = MainWindow(cfg)
    win.show()
    panel = win.page.capture_panel
    check("左栏有「导入聊天记录」按钮", hasattr(panel, "btn_import"))

    got: list = []
    panel.transcriptReady.connect(lambda t: got.append(t))

    # 1) 自动认 me 的两份：txt（昵称「我」）/ chatlab agent（sender=me）
    for name, want_n in (("wechat_plain.txt", 7), ("chatlab_agent.json", 6)):
        p = FIX / name
        n_before = len(got)
        panel._ask_file = lambda p=p: str(p)  # type: ignore[assignment]
        panel._ask_me_label = lambda res: (_ for _ in ()).throw(  # type: ignore[assignment]
            AssertionError(f"{name} 不该弹确认框（文件里已显式标了 me）"))
        panel.import_transcript()
        t = panel.transcript
        check(f"{name} 导入并进面板", t is not None and len(t.messages) == want_n,
              f"{len(t.messages) if t else 0} 条")
        check(f"{name} 文本框里是 #id 开头的记录",
              bool(t) and panel.ocr_text.toPlainText().startswith("# 会话记录"))
        check(f"{name} 发出了 transcriptReady 信号", len(got) == n_before + 1,
              f"共 {len(got)} 次")

    # 2) 必须问人的一份（GBK、张三/李四）：走确认框，选「张三」→ 必须在确认框里出现人数
    p = FIX / "wechat_gbk.txt"
    asked: dict = {}

    def fake_ask(res):
        asked["labels"] = [c["label"] for c in res.candidates]
        asked["counts"] = {c["label"]: c["count"] for c in res.candidates}
        asked["fmt"] = res.fmt
        return "张三"

    panel._ask_file = lambda: str(p)  # type: ignore[assignment]
    panel._ask_me_label = fake_ask  # type: ignore[assignment]
    panel.import_transcript()
    t = panel.transcript
    check("确认框里给出了两个候选昵称与条数", asked.get("labels") == ["张三", "李四"]
          and asked.get("counts") == {"张三": 2, "李四": 1}, str(asked))
    check("选「张三」后 me 判定正确", t is not None
          and sum(1 for m in t.messages if m.sender == "me") == 2)
    check("GBK 文件在 GUI 路径也能读", t is not None and "早上好" in t.messages[0].text,
          t.messages[0].text if t else "")

    # 3) 取消确认框 → 不改动上一次结果、不崩
    before = len(panel.transcript.messages) if panel.transcript else -1
    panel._ask_me_label = lambda res: None  # type: ignore[assignment]
    panel.import_transcript()
    check("取消确认框不污染已有 transcript",
          panel.transcript is not None and len(panel.transcript.messages) == before,
          f"{before} 条")

    # 4) 失败信封 → 明确报错、transcript 不变
    p2 = FIX / "chatlab_error.json"
    panel._ask_file = lambda: str(p2)  # type: ignore[assignment]
    panel._ask_me_label = lambda res: (_ for _ in ()).throw(AssertionError("不该问 me"))  # type: ignore[assignment]
    panel.import_transcript()
    st = panel.lbl_status.text()
    check("失败信封在状态栏明说失败", "导入失败" in st, st[:70])
    check("失败时不覆盖原 transcript", panel.transcript is not None
          and len(panel.transcript.messages) == before)

    # 5) 不选文件（取消）→ 明确说明取消，且不残留上一次的失败文案
    panel._ask_file = lambda: None  # type: ignore[assignment]
    panel.import_transcript()
    st = panel.lbl_status.text()
    check("取消选文件 → 状态栏改说「已取消」且不再显示旧失败",
          "已取消" in st and "导入失败" not in st, st[:70])

    print(f"\n结果：通过 {len(PASS)} 项，失败 {len(FAIL)} 项", flush=True)
    if FAIL:
        print("失败项：" + "、".join(FAIL), flush=True)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
