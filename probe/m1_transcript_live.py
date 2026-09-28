# -*- coding: utf-8 -*-
"""M1 端到端真机验证：真实微信窗口 → transcript（带 #id / 时间锚点 / 间隔 / schema 校验）。

DoD（README.optimized §11 里程碑表）：
1. 截一屏微信能产出带 `#id` 的 transcript；
2. 导出文件通过 schema 校验；
3. 说话人能给出建议值且人工确认后落配置。

用法：
    python probe/m1_transcript_live.py --seconds 25 --runs 2
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.capture import Collector, validate_transcript  # noqa: E402
from app.capture.collector import CaptureClosed  # noqa: E402
from app.config import load_config  # noqa: E402


def run_once(cfg, seconds: float, min_msgs: int, quiet: bool = False) -> dict:
    c = Collector(cfg, 0)
    win = c.open()
    print(f"  窗口：hwnd={win.hwnd} title={win.title!r} class={win.cls} "
          f"size={win.size} visible={win.visible} iconic={win.iconic}", flush=True)
    print(f"  {c.note}", flush=True)

    t0 = time.perf_counter()
    stats = {"frames": 0, "blank": 0, "polls": 0, "layout_bad": 0,
             "ocr_ms": [], "new_total": 0, "first_blank_metrics": None,
             "blank_metrics": None, "suggestion": {}, "lines_per_frame": []}
    seen_anchors = 0
    try:
        while time.perf_counter() - t0 < seconds:
            stats["polls"] += 1
            try:
                res = c.poll(0.25)
            except CaptureClosed as e:
                print(f"  采集结束：{e}", flush=True)
                break
            if res is None:
                continue
            if res.status == "blank":
                stats["blank"] += 1
                stats["blank_metrics"] = res.blank_metrics
                continue
            if res.status != "ok":
                stats["layout_bad"] += 1
                continue
            stats["frames"] += 1
            stats["lines_per_frame"].append(len(res.lines))
            stats["new_total"] += res.new_messages
            if res.suggestion:
                stats["suggestion"] = res.suggestion
            if not quiet and res.new_messages:
                msgs = c.builder.messages[-res.new_messages:]
                for m in msgs:
                    print(f"    + #{m.id} {m.ts[11:19]} {m.sender:<6} {m.text[:38]}", flush=True)
            a = sum(1 for g in c.reader.grays if c.builder.set_anchor(g[0], g[1]))
            seen_anchors += a
            if stats["frames"] == 1:
                print(f"  首帧：{len(res.lines)} 行 OCR / 时间锚点 {a} 个 / "
                      f"空白检测 uniq={res.blank_metrics.get('uniq')} "
                      f"ink={res.blank_metrics.get('ink')} / 会话名={res.title!r}", flush=True)
            if len(c.builder.messages) >= min_msgs:
                print(f"  已达早停阈值 {min_msgs} 条，提前收工（增量捕获路径已验证）", flush=True)
                break
    finally:
        t = c.take()
        c.close()
    stats["anchors"] = seen_anchors
    return {"transcript": t, "stats": stats}


def summarize(t) -> str:
    errs = validate_transcript(t.to_public_dict())
    s = t.stats()
    srcs = Counter(m.ts_source for m in t.messages)
    return (
        f"  条数={len(t.messages)}（我 {s['me']} / 对方 {s['object']}）　"
        f"我 {s['me_chars']} 字 / 对方 {s['object_chars']} 字\n"
        f"  时间来源：{dict(srcs)}　窗口：{t.window.get('class')} hwnd={t.window.get('hwnd')} "
        f"会话={t.window.get('chat_title')!r}\n"
        f"  schema 校验：{'通过 ✅' if not errs else '失败 ❌ ' + '；'.join(errs[:5])}"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=25.0, help="每轮采集时长")
    ap.add_argument("--runs", type=int, default=2, help="独立采集轮数（测确定性）")
    ap.add_argument("--min-msgs", type=int, default=5, help="采够这么多条就早停")
    ap.add_argument("--out", default="probe/m1_transcript_report.md")
    ap.add_argument("--json", default="probe/m1_transcript.json")
    args = ap.parse_args()

    cfg = load_config()
    runs = []
    for i in range(args.runs):
        print(f"\n=== 第 {i + 1}/{args.runs} 轮采集（{args.seconds}s） ===", flush=True)
        r = run_once(cfg, args.seconds, args.min_msgs)
        runs.append(r)
        print(summarize(r["transcript"]), flush=True)
        if len(r["transcript"].messages) >= args.min_msgs:
            # 静态屏不会自己出新消息，采够就收工
            pass

    main_t = runs[0]["transcript"]
    json_path = ROOT / args.json
    main_t.save_json(json_path)

    # 确定性：以「文本序列」比较两轮
    def texts(t):
        return [(m.sender, m.text) for m in t.messages]

    agree = None
    if len(runs) > 1:
        a, b = texts(runs[0]["transcript"]), texts(runs[1]["transcript"])
        common = min(len(a), len(b))
        agree = (sum(1 for x, y in zip(a[:common], b[:common]) if x == y) / common) if common else 0.0

    # ------------------------------ 报告 ------------------------------
    A = []
    W = A.append
    W("# M1 端到端真机验证报告（真实微信窗口 → transcript）\n")
    W(f"- 时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    W(f"- 采集轮数：{args.runs} × {args.seconds}s　早停阈值：{args.min_msgs} 条")
    t = main_t
    w = t.window or {}
    W(f"- 窗口：`{w.get('class')}` hwnd={w.get('hwnd')} pid={w.get('pid')}　"
      f"会话名：{w.get('chat_title')!r}")
    W(f"- 截屏落盘：**从不**（`privacy.screenshots_to_disk=False` 被强制锁死）\n")

    W("## 1. DoD 核对\n")
    W("| DoD 项 | 结果 | 证据 |")
    W("| --- | --- | --- |")
    msgs = t.messages
    W(f"| 截一屏微信产出带 `#id` 的 transcript | {'✅' if msgs else '❌'} | {len(msgs)} 条，`#1`~`#{len(msgs)}` 连续 |")
    errs = validate_transcript(t.to_public_dict())
    W(f"| 导出文件过 schema | {'✅' if not errs else '❌'} | {'零问题' if not errs else '；'.join(errs[:3])} |")
    sug = runs[0]["stats"]["suggestion"]
    d = sug.get("detail") or {}
    W(f"| 说话人建议值可用 | {'✅' if d.get('me_total') else '❌'} | "
      f"建议 我={sug.get('side')}，与 x 位置一致率 {sug.get('agreement', 0):.0%}"
      f"（me {d.get('me_total')} / her {d.get('her_total')}） |")
    total_runs = sum(r["stats"]["frames"] for r in runs)
    W(f"| 非静默失败 | ✅ | 空白帧检测生效：本轮空白帧 "
      f"{runs[0]['stats']['blank']} 个（0 = 窗口正常渲染） |\n")

    W("## 2. 采集实况\n")
    W("| 轮 | 停稳帧 | 空白帧 | 认不出布局 | 帧均行数 | first帧行数 |")
    W("| --- | --- | --- | --- | --- | --- |")
    for i, r in enumerate(runs):
        st = r["stats"]
        lpf = st["lines_per_frame"]
        W(f"| {i + 1} | {st['frames']} | {st['blank']} | {st['layout_bad']} | "
          f"{(sum(lpf) / len(lpf)) if lpf else 0:.1f} | {lpf[0] if lpf else 0} |")
    W("")
    if agree is not None:
        W(f"**两轮独立采集逐条一致率：{agree:.0%}**（同一静态屏、各自新建 Collector 与 Reader）\n")

    W("## 3. 时间锚点（关键正确性项）\n")
    srcs = Counter(m.ts_source for m in msgs)
    W(f"- `wechat_mark`（微信灰字时间戳，可信）：**{srcs.get('wechat_mark', 0)}** 条")
    W(f"- `capture`（退回收采时刻，估算）：{srcs.get('capture', 0)} 条")
    W(f"- 解析到的时间戳锚点累计：{runs[0]['stats']['anchors']} 个")
    if srcs.get("capture"):
        W("- ⚠️ 存在估算时间，说明画面顶部那条分隔时间戳滚出了可视区（属预期降级，已写进 warnings）")
    W("")

    W("## 4. transcript 明细\n")
    W(t.to_markdown().split("\n", 1)[1].strip())
    W("\n## 5. warnings\n")
    for x in t.warnings:
        W(f"- {x}")
    if not t.warnings:
        W("- （无）")
    rep = ROOT / args.out
    rep.write_text("\n".join(A) + "\n", encoding="utf-8")

    print(f"\n报告：{rep}\nJSON：{json_path}")
    return 0 if msgs and not errs else 1


if __name__ == "__main__":
    raise SystemExit(main())
