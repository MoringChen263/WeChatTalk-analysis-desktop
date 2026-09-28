# -*- coding: utf-8 -*-
"""M3 真机验证：用真模型跑一轮完整分析（需要 API Key，没配就明确跳过）。

    set JEV_LLM_API_KEY=sk-xxx
    python probe/m3_live.py                # 用真实微信 transcript + 真模型
    python probe/m3_live.py --type trend   # 指定问题类型

产出：
    probe/m3_live_report.md   人读报告（含五步 + 话术卡全文）
    probe/m3_live.json        结构化结果（analysis.json 同款）
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.analysis import parse_output as po  # noqa: E402
from app.analysis import questions as q  # noqa: E402
from app.analysis import schema as sc  # noqa: E402
from app.analysis.engine import AnalysisEngine  # noqa: E402
from app.analysis.llm_client import LLMClient  # noqa: E402
from app.capture.transcript import Message, Transcript  # noqa: E402
from app.config import load_config  # noqa: E402


def load_transcript(path: Path) -> Transcript:
    d = json.loads(path.read_text(encoding="utf-8"))
    msgs = [Message(**{k: v for k, v in m.items() if k in Message.__dataclass_fields__})
            for m in d.pop("messages", [])]
    t = Transcript(**{k: v for k, v in d.items() if k in Transcript.__dataclass_fields__})
    t.messages = msgs
    return t


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--type", default="reply", help="问题类型 key")
    ap.add_argument("--goal", default="我想认真回应她，但还没想好要不要确认关系")
    ap.add_argument("--emotion", type=int, default=7)
    ap.add_argument("--transcript", default="probe/m1_transcript.json")
    ap.add_argument("--out", default="probe/m3_live_report.md")
    ap.add_argument("--json", dest="json_out", default="probe/m3_live.json")
    ap.add_argument("--timeout", type=float, default=120.0)
    args = ap.parse_args()

    cfg = load_config()
    client = LLMClient(cfg)
    ready, why = client.ready()

    tpath = ROOT / args.transcript
    if not tpath.exists():
        print(f"找不到 transcript：{tpath}\n先跑 probe/m1_transcript_live.py 采一份。")
        return 1

    print(f"后端：{client.base_url}　模型：{client.model}　模式：{client.mode}")
    if not ready:
        print(f"\n跳过真机验证：{why}")
        print("配置方式（任选其一）：")
        print("  1) set JEV_LLM_API_KEY=sk-xxx        然后重跑本脚本")
        print("  2) 打开应用 → 右上角「设置」→ 填 Key → 保存到本机凭据（DPAPI 加密）")
        print("  3) 本地模型：设置里切「本地」，起好 Ollama 后填 base_url 与模型名")
        print("\n离线路径已由 probe/m3_unit.py（112 项）与 probe/m3_gui_live.py（31 项）覆盖。")
        return 0

    t = load_transcript(tpath)
    print(f"输入：{len(t.messages)} 条，会话 {t.window.get('chat_title')}，"
          f"我 = {(t.speaker_map.get('me') or {}).get('side')}"
          f"（已确认={(t.speaker_map.get('me') or {}).get('confirmed')}）")

    engine = AnalysisEngine(cfg)
    plan = engine.plan(args.type, t, goal=args.goal)
    print(f"问题类型：{plan.label}　将加载 {len(plan.refs)} 份参考")
    for f in plan.refs:
        print(f"    · {f}")
    if plan.crisis:
        print(f"⚠️ 安全信号：{'、'.join(plan.crisis)}")

    streamed = {"n": 0, "last": 0.0}

    def on_delta(chunk: str) -> None:
        streamed["n"] += len(chunk)
        now = time.monotonic()
        if now - streamed["last"] > 1.0:  # 节流：按秒刷新，否则几千个 chunk 会把日志刷爆
            streamed["last"] = now
            print(f"\r  模型输出中… {streamed['n']} 字", end="", flush=True)

    t0 = time.perf_counter()
    run = engine.analyze(t, args.type, goal=args.goal, emotion=args.emotion,
                         on_delta=on_delta)
    print()
    print("=" * 60)
    print(run.headline())
    print(f"调用 {run.calls} 次　模式 {po.MODE_LABEL.get(run.mode, run.mode)}"
          + ("　（含一次格式修复）" if run.repaired else "")
          + ("　（输出超限后放大上限重试）" if run.enlarged else ""))
    if run.finish_reason:
        print(f"finish_reason：{run.finish_reason}"
              + ("　⚠️ 被长度上限截断" if run.finish_reason == "length" else ""))
    if run.llm:
        print(f"结构化输出档位：{run.llm.response_format_tier}")
        print(f"用量：{run.usage.to_dict()}　实际耗时 {time.perf_counter() - t0:.1f}s")
        if run.llm.streamed:
            print(f"流式分块累计 {streamed['n']} 字")
    if run.cost_cny is not None:
        print(f"估算费用：¥{run.cost_cny:.4f}")
    for w in run.warnings:
        print(f"提示：{w}")

    if not run.ok:
        print(f"\n❌ 没拿到可用结果：{run.error}")
        print("降级原文预览：")
        print(run.analysis.raw[:600])
        (ROOT / args.json_out).write_text(run.analysis.to_json(), encoding="utf-8")
        return 2

    a = run.analysis
    print(f"schema 硬错误：{sc.validate(a.data) or '无'}")
    lint = sc.lint(a.data, run.prompt.included_ids if run.prompt else None)
    print(f"规范软提醒：{lint or '无'}")

    lines = [f"# M3 真机验证报告（{time.strftime('%Y-%m-%d %H:%M:%S')}）", ""]
    lines.append(f"- 后端：`{client.base_url}`　模型：`{client.model}`")
    lines.append(f"- 问题类型：{plan.label}　情绪强度 {args.emotion}/10")
    lines.append(f"- 用户目标：{args.goal}")
    lines.append(f"- 加载参考：{len(plan.refs)} 份")
    for f in plan.refs:
        lines.append(f"  - `{f}`")
    lines.append(f"- 输出模式：{po.MODE_LABEL.get(run.mode, run.mode)}"
                 f"　档位 {run.llm.response_format_tier if run.llm else '?'}")
    lines.append(f"- 用量：{run.usage.to_dict()}　耗时 {run.elapsed_s}s"
                 f"　调用 {run.calls} 次　费用 "
                 + (f"¥{run.cost_cny:.4f}" if run.cost_cny is not None else "（未配单价）"))
    if run.crisis:
        lines.append(f"- ⚠️ 安全信号：{'、'.join(run.crisis)}")
    if run.warnings:
        lines += ["", "## 提示"] + [f"- {w}" for w in run.warnings]
    lines += ["", "## 输入记录", "", "| # | 时间 | 说话人 | 内容 |", "| --- | --- | --- | --- |"]
    for m in t.messages:
        lines.append(f"| {m.id} | {m.ts} | {m.sender} | {m.text.replace('|', chr(92) + '|')} |")
    lines += ["", "## 五步", ""]
    for title, page in zip(sc.STEP_TITLES, a.steps_markdown()):
        lines += [f"### {title}", "", page, ""]
    lines += ["## 话术卡", ""]
    for title, page in zip(sc.SCRIPT_TABS, a.scripts_markdown()):
        lines += [f"### {title}", "", page, ""]
    lines += ["## 元信息", "",
              f"- 置信度：{a.confidence or '未给出'}",
              f"- 引用：{'、'.join(a.citations) or '无'}",
              f"- 边界：{'、'.join(a.boundaries) or '无'}"]

    out = ROOT / args.out
    out.write_text("\n".join(lines), encoding="utf-8")
    run.save_json(ROOT / args.json_out)
    print(f"\n已写出：{out}")
    print(f"已写出：{ROOT / args.json_out}")
    print("\n可直接发送的成品话术：")
    print("  " + a.primary_text())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
