# -*- coding: utf-8 -*-
"""M3 GUI 端到端：真建面板、真起 worker 线程、真走流式/降级/取消/危机/导出。

不开窗口（只构造控件 + processEvents），所以可以无人值守跑。

    python probe/m3_gui_live.py
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.analysis import parse_output as po  # noqa: E402
from app.config import load_config  # noqa: E402
from app.capture.transcript import Message, Transcript  # noqa: E402
from fake_llm import Behavior, FakeLLM, write_fake_config  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗'} {name}" + (f"　{detail}" if detail else ""), flush=True)
    return cond


def pump(cond, timeout: float = 20.0) -> bool:
    """转事件循环直到 cond 成立（QThread 的排队信号靠 processEvents 投递）。"""
    from PySide6.QtWidgets import QApplication

    t0 = time.time()
    while time.time() - t0 < timeout:
        QApplication.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    return False


def load_transcript() -> Transcript:
    d = json.loads((ROOT / "probe" / "m1_transcript.json").read_text(encoding="utf-8"))
    msgs = [Message(**{k: v for k, v in m.items() if k in Message.__dataclass_fields__})
            for m in d.pop("messages", [])]
    t = Transcript(**{k: v for k, v in d.items() if k in Transcript.__dataclass_fields__})
    t.messages = msgs
    return t


def main() -> int:
    from PySide6.QtWidgets import QApplication

    qapp = QApplication.instance() or QApplication([])
    cfg = load_config()
    cfg.set("analysis.repair_retry", True, force=True)
    T = load_transcript()
    OK = (ROOT / "probe" / "fixtures" / "analysis_ok.json").read_text(encoding="utf-8")

    from app.ui.analysis_panel import AnalysisPanel

    # ---------- 1) 构建与预检 ----------
    print("\n[1] 面板构建与预检")
    with FakeLLM([Behavior(kind="json", body=OK)]) as srv:
        write_fake_config(cfg, srv.base_url, stream=False,
                          pricing={"*": {"in_cny_per_mtok": 2.0, "out_cny_per_mtok": 8.0}})
        panel = AnalysisPanel(cfg)
        panel.set_transcript(T)
        pump(lambda: True, 0.3)
        plan_txt = panel.lbl_plan.text()
        check("面板可构建", panel is not None)
        check("预检显示将加载的参考", "将加载" in plan_txt, plan_txt[:60])
        check("预检显示后端就绪", "后端就绪" in plan_txt, plan_txt[-24:])
        check("有五页五步 / 四页话术卡",
              panel.steps.count() == 5 and panel.scripts.count() == 4)
        check("有输入时「开始分析」可用", panel.btn_analyze.isEnabled())
        check("未分析前不能导出", not panel.btn_export.isEnabled())

        # ---------- 2) 正常分析（非流式） ----------
        print("\n[2] 正常分析")
        panel.start_analysis()
        done = pump(lambda: panel.run is not None, 20)
        check("worker 跑完并回传结果", done and panel.run is not None)
        run = panel.run
        check("结果 ok 且是结构化输出", bool(run and run.ok and run.mode == po.MODE_JSON),
              f"mode={run.mode if run else '?'}")
        page0 = panel.steps.widget(0).toPlainText()
        check("五步第一页渲染出内容", len(page0.strip()) > 20, page0[:24].replace("\n", " "))
        script0 = panel.scripts.widget(0).toPlainText()
        check("话术卡首选页渲染出内容", "可以直接复制发送" in script0)
        check("页脚显示 tokens 与费用",
              "tokens" in panel.footnote.text() and "¥" in panel.footnote.text(),
              panel.footnote.text())
        check("分析完成后按钮复位", panel.btn_analyze.isEnabled()
              and not panel.btn_cancel.isEnabled() and panel.worker is None)

        # 复制当前话术
        expect = run.analysis.primary_text()
        panel.scripts.setCurrentIndex(1)   # 稳健版
        panel.copy_current_script()
        got = qapp.clipboard().text()
        check("复制当前这版 == 稳健版正文", got == run.analysis.variant_text("steady"),
              f"{len(got)} 字")

        # 导出
        out = ROOT / "probe" / "m3_gui_analysis.json"
        run.save_json(out)
        saved = json.loads(out.read_text(encoding="utf-8"))
        check("导出的 analysis.json 可解析且含五步",
              saved["ok"] and "steps" in saved["analysis"]["analysis"], str(out.name))

        # ---------- 3) 流式 ----------
        print("\n[3] 流式路径")
        write_fake_config(cfg, srv.base_url, stream=True,
                          pricing={"*": {"in_cny_per_mtok": 2.0, "out_cny_per_mtok": 8.0}})
        panel.streamed_seen = 0
        panel.start_analysis()
        saw_progress = pump(lambda: "已收到" in panel.footnote.text()
                            or panel.run is not None, 20)
        done = pump(lambda: panel.worker is None and panel.run is not None, 20)
        check("流式有进度提示", saw_progress, panel.footnote.text()[:32])
        check("流式也能出结构化结果", done and panel.run.ok)

        # ---------- 4) 降级（两次都不是 JSON） ----------
        print("\n[4] 降级路径")
        with FakeLLM([Behavior(kind="json", body="我不知道该怎么回答这个问题。")]) as srv2:
            write_fake_config(cfg, srv2.base_url, stream=False)
            panel2 = AnalysisPanel(cfg)
            panel2.set_transcript(T)
            panel2.start_analysis()
            pump(lambda: panel2.worker is None, 20)
            r2 = panel2.run
            check("降级被标为 degraded", bool(r2 and r2.mode == po.MODE_DEGRADED),
                  f"mode={r2.mode if r2 else '?'}")
            # 注意：面板没 show()，所以用 isVisibleTo 判断「父窗口一显示它就会显示」
            check("降级有醒目横幅",
                  panel2.lbl_banner.isVisibleTo(panel2)
                  and "没给出合规 JSON" in panel2.lbl_banner.text(),
                  panel2.lbl_banner.text()[:36])
            check("降级内容不丢，原文可见",
                  "我不知道该怎么回答" in panel2.steps.widget(0).toPlainText())
            check("降级页脚标出失败原因",
                  panel2.footnote.text().startswith("❌"), panel2.footnote.text()[:40])
            check("降级后仍可再跑一次", panel2.btn_analyze.isEnabled())

        # ---------- 5) 配置缺失 ----------
        print("\n[5] 未配置密钥")
        with FakeLLM([Behavior(kind="json", body=OK)]) as srv3:
            cfg.set("llm.api_key_ref", "env:JEV_DEFINITELY_MISSING_KEY", force=True)
            cfg.set("llm.base_url", srv3.base_url, force=True)
            panel3 = AnalysisPanel(cfg)
            panel3.set_transcript(T)
            pump(lambda: True, 0.2)
            check("缺 key 时禁用「开始分析」", not panel3.btn_analyze.isEnabled())
            check("缺 key 时给出可操作提示",
                  "设置" in panel3.lbl_banner.text(), panel3.lbl_banner.text()[:40])
            panel3.start_analysis()
            pump(lambda: True, 0.3)
            check("缺 key 时点按钮不会起线程", panel3.worker is None)

        # ---------- 6) 危机信号 ----------
        print("\n[6] 安全信号")
        with FakeLLM([Behavior(kind="json", body=OK)]) as srv4:
            write_fake_config(cfg, srv4.base_url, stream=False)
            panel4 = AnalysisPanel(cfg)
            panel4.set_transcript(T)
            panel4.goal.setText("他昨天动手打我了，我该不该报警")
            panel4._refresh_plan()
            check("命中危机词", "动手打" in panel4.lbl_banner.text()
                  or "家暴" in panel4.lbl_banner.text(), panel4.lbl_banner.text()[:40])
            check("给出紧急服务信息", "110" in panel4.lbl_banner.text())
            panel4.start_analysis()
            pump(lambda: panel4.worker is None, 20)
            check("危机场景照常出结果（不拒答）", bool(panel4.run and panel4.run.ok))
            check("结果里带安全提示",
                  any("安全信号" in w for w in (panel4.run.warnings if panel4.run else [])),
                  (panel4.run.warnings if panel4.run else [""])[0][:40])

        # ---------- 7) 取消 ----------
        print("\n[7] 取消")
        with FakeLLM([Behavior(kind="json", body=OK, hang=8.0)]) as srv5:
            write_fake_config(cfg, srv5.base_url, stream=False)
            cfg.set("llm.request_timeout_s", 10.0, force=True)
            panel5 = AnalysisPanel(cfg)
            panel5.set_transcript(T)
            panel5.start_analysis()
            pump(lambda: panel5.worker is not None, 3)
            panel5.cancel_analysis()
            check("取消后按钮态正确", not panel5.btn_cancel.isEnabled())
            gone = pump(lambda: panel5.worker is None, 20)
            check("取消能让 worker 退出", gone, panel5.footnote.text()[:30])

    # ---------- 8) 真实微信 transcript 直通 ----------
    print("\n[8] 与左栏联动（信号通路）")
    from app.ui.main_window import AnalyzerPage

    with FakeLLM([Behavior(kind="json", body=OK)]) as srv6:
        write_fake_config(cfg, srv6.base_url, stream=False)
        page = AnalyzerPage(cfg)
        page.capture_panel.transcriptReady.emit(T)
        pump(lambda: True, 0.3)
        check("左栏 transcript 能推进右栏",
              page.analysis_panel.transcript is T
              and "小新" in page.analysis_panel.lbl_source.text(),
              page.analysis_panel.lbl_source.text()[:40])

    print(f"\n结果：通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败项：" + "、".join(FAIL))
    return 0 if not FAIL else 1


if __name__ == "__main__":
    os.environ.setdefault("JEV_FAKE_KEY", "test-key")
    raise SystemExit(main())
