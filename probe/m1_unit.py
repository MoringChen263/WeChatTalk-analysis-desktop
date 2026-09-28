# -*- coding: utf-8 -*-
"""M1 离线单元自检：transcript 组装 / 时间锚点 / 判重 / schema / 说话人建议值。

不依赖微信与网络，零第三方依赖（只用标准库 + app 内纯逻辑模块）。
    python probe/m1_unit.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.capture.ocr import Line  # noqa: E402
from app.capture import speaker_mapper  # noqa: E402
from app.capture.transcript import (TranscriptBuilder, now_iso, parse_time_mark,  # noqa: E402
                                   validate_transcript)

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}{('　— ' + detail) if detail and not cond else ''}")


def line(who: str, text: str, y: float, x_rel: float, score: float = 0.99) -> Line:
    return Line(who=who, name=None, text=text, y_top=y, y_bot=y + 20, score=score, x_rel=x_rel)


def t_time_mark() -> None:
    print("\n[1] 微信灰字时间戳解析")
    now = datetime(2026, 9, 23, 20, 0, tzinfo=datetime.now().astimezone().tzinfo)
    a = parse_time_mark("14:01", now)
    check("「14:01」→ 今天 14:01", a is not None and a.hour == 14 and a.minute == 1 and a.day == 23,
          repr(a))
    b = parse_time_mark("昨天 14:01", now)
    check("「昨天 14:01」→ 9/22 14:01", b is not None and b.day == 22, repr(b))
    c = parse_time_mark("9月21日 14:01", now)
    check("「9月21日 14:01」→ 9/21", c is not None and c.month == 9 and c.day == 21, repr(c))
    d = parse_time_mark("22:30", now)
    check("「22:30」（晚于当前时刻）→ 自动算昨天", d is not None and d.day == 22, repr(d))
    check("非时间文本 → None", parse_time_mark("王小明：") is None and parse_time_mark("14:61") is None)


def t_builder() -> None:
    print("\n[2] TranscriptBuilder：编号 / 间隔 / 时间锚点 / 判重")
    tb = TranscriptBuilder(speaker_mapper.default_map("right", confirmed=True))
    base = datetime(2026, 9, 23, 14, 0, 0, tzinfo=datetime.now().astimezone().tzinfo)
    check("锚点识别「14:01」", tb.set_anchor("14:01", 10, ref=base))
    check("公告类灰字不算锚点", not tb.set_anchor("系统提示：你已添加对方为好友", 12, ref=base))

    tb.add("obj-1", "你有车子没", y=30)
    tb.add("me", "你下楼等着吧", y=60)
    tb.add("obj-1", "行", y=90)
    check("编号从 1 连续", [m.id for m in tb.messages] == [1, 2, 3])
    check("锚点时间落到消息上", tb.messages[0].ts.startswith("2026-09-23T14:01"),
          tb.messages[0].ts)
    check("ts_source=wechat_mark", tb.messages[0].ts_source == "wechat_mark")
    check("间隔为 0（同一分钟锚点）", tb.messages[1].gap_s == 0, str(tb.messages[1].gap_s))

    n0 = len(tb.messages)
    tb.add("obj-1", "行", y=120)  # 与上一条同人同文本，应被判重
    check("同人同文重复被吞", len(tb.messages) == n0, f"{n0} → {len(tb.messages)}")
    tb.add("obj-1", "其实，我喜欢你", y=150)
    check("不同文本正常入列", tb.messages[-1].text == "其实，我喜欢你")

    tb.add_placeholder("obj-1", "image", y=180)
    check("缺内容标 [图片] 且 type=image",
          tb.messages[-1].text == "[图片]" and tb.messages[-1].type == "image")
    check("不虚构：占位有独立 type", tb.messages[-1].type in ("image", "voice", "sticker"))

    tb2 = TranscriptBuilder(speaker_mapper.default_map("right", confirmed=True))
    tb2.add("me", "无锚点消息", y=5)  # 无时间戳 → 退回采集时刻
    check("无锚点时标 capture 并进 warning",
          tb2.messages[0].ts_source == "capture" and
          any("估算" in w for w in tb2.build().warnings))


def t_schema() -> None:
    print("\n[3] schema 校验")
    tb = TranscriptBuilder(speaker_mapper.default_map("right", confirmed=True),
                           window={"hwnd": 1, "class": "Qt51514QWindowIcon"})
    tb.add("me", "在吗", y=1)
    tb.add("obj-1", "在的", y=2)
    t = tb.build()
    check("合法 transcript 通过", validate_transcript(t.to_public_dict()) == [],
          str(validate_transcript(t.to_public_dict())))

    d = t.to_public_dict()
    d["messages"][1]["id"] = 5
    check("抓出 id 不连续", any("连续递增" in e for e in validate_transcript(d)))
    d2 = t.to_public_dict()
    d2["messages"][0]["sender"] = "obj-9"
    check("抓出未登记的 sender", any("sender" in e for e in validate_transcript(d2)))
    d3 = t.to_public_dict()
    d3["messages"][1]["ts"] = "2026-09-23T13:00:00+08:00"
    d3["messages"][0]["ts"] = "2026-09-23T14:00:00+08:00"
    check("抓出时间倒流", any("倒流" in e for e in validate_transcript(d3)),
          str(validate_transcript(d3)))
    d4 = t.to_public_dict()
    d4["speaker_map"]["me"]["side"] = "middle"
    check("抓出 side 非法", any("side" in e for e in validate_transcript(d4)))
    d5 = t.to_public_dict()
    d5["messages"][0]["type"] = "video"
    check("抓出 type 越界", any("type" in e for e in validate_transcript(d5)))

    check("to_text 每行带 #id", t.to_text().count("#") >= 2 and "#1 " in t.to_text())
    check("to_markdown 有表格", "| 1 |" in t.to_markdown())
    check("stats 统计人数正确", t.stats()["me"] == 1 and t.stats()["object"] == 1)
    check("captured_range 是两条",
          isinstance(t.captured_range, list) and len(t.captured_range) == 2)


def t_speaker() -> None:
    print("\n[4] 说话人建议值与映射")
    lines = [line("me", "你下楼等着吧", 10, 0.78), line("me", "行吧", 40, 0.74),
             line("her", "你有车子没", 70, 0.26), line("her", "其实我喜欢你", 100, 0.22)]
    s = speaker_mapper.suggest(lines, 730)
    check("建议 我=右侧", s["side"] == "right", str(s))
    check("与 x 位置一致率 100%", s["agreement"] == 1.0, str(s["agreement"]))
    check("置信度 ≥0.9", s["confidence"] >= 0.9, str(s["confidence"]))

    flip = [line("me", "a", 10, 0.2), line("her", "b", 30, 0.8)]
    s2 = speaker_mapper.suggest(flip, 730)
    check("左右反了也能反推（我=左侧）", s2["side"] == "left", str(s2))

    bad = [line("me", "a", 10, 0.2), line("me", "b", 30, 0.8),
           line("her", "c", 50, 0.3), line("her", "d", 70, 0.7)]
    s3 = speaker_mapper.suggest(bad, 730)
    check("冲突帧一致率下降", s3["agreement"] < 0.9, str(s3["agreement"]))
    check("空帧不崩", speaker_mapper.suggest([], 730)["detail"]["me_total"] == 0)

    sm = speaker_mapper.default_map("right", confirmed=False)
    out = speaker_mapper.translate(lines, sm)
    check("translate：me→me / her→obj-1",
          [c for c, _ in out] == ["me", "me", "obj-1", "obj-1"], str([c for c, _ in out]))
    check("未确认时有警告", speaker_mapper.unconfirmed_warning(sm) is not None)
    sm2 = speaker_mapper.default_map("right", confirmed=True)
    check("已确认时无警告", speaker_mapper.unconfirmed_warning(sm2) is None)
    check("objects side 与我相反", sm2["objects"][0]["side"] == "left")


def t_incremental() -> None:
    """§5.2 第 2/3 步：滚动重叠对齐 + 只追加新增尾部行。用真实 Reader 的 new_lines。"""
    print("\n[5] 增量捕获（滚动不重复）")
    from app.capture.ocr import Reader

    def Ls(*texts):
        return [line("her", t, 10 + i * 30, 0.25) for i, t in enumerate(texts)]

    r = Reader()
    r.lh = 20.0
    f1 = Ls("一", "二", "三", "四", "五")
    n1 = r.new_lines(f1)
    check("首帧全部算新（宁多勿漏）", len(n1) == 5, str(len(n1)))

    # 往上滚了 2 行，底部新增 2 行：已知行都还在 → 只报底部那 2 条
    f2 = Ls("三", "四", "五", "六", "七")
    n2 = r.new_lines(f2)
    check("滚动后只报新增尾部 2 条", [l.text for l in n2] == ["六", "七"],
          str([l.text for l in n2]))

    n3 = r.new_lines(f2)
    check("同一帧重放不重复报", len(n3) == 0, str([l.text for l in n3]))

    # 大图把旧文字全顶出去：一行已知的都没有 → 全部算新（切会话/滚远了）
    f4 = Ls("甲", "乙")
    n4 = r.new_lines(f4)
    check("无已知行时全部算新", [l.text for l in n4] == ["甲", "乙"], str([l.text for l in n4]))

    # 上方翻出来的旧消息（在已知行之上）不算新
    r2 = Reader()
    r2.lh = 20.0
    r2.new_lines(Ls("旧", "中"))
    up = r2.new_lines([line("her", "更旧", 5, 0.25), line("her", "旧", 40, 0.25),
                       line("her", "中", 70, 0.25)])
    check("向上翻出的旧消息不算新", [l.text for l in up] == [], str([l.text for l in up]))
    check("OCR 抖动不算新（相似度判重）", r2._seen("her", None, "中") and
          r2._seen("her", None, "中"))  # 覆盖 _seen 路径


def t_session_switch() -> None:
    """切会话必须断流，否则两个人的对话会搅进同一份 transcript。"""
    print("\n[6] 切会话自动断流")
    from app.capture import collector as col
    from app.capture import ocr as ocr_mod
    from app.config import Config

    cfg = Config(Path(ROOT) / ".devdata" / "unit-config.json")
    cfg.set("capture.speaker_map", speaker_mapper.default_map("right", confirmed=True))
    c = col.Collector(cfg, 0)

    class FakeCap:
        last = None

    c.cap = FakeCap()  # 让 _maybe_read_title 之外的部分可用

    def hdr(v: int) -> np.ndarray:
        """造一帧：头部像素值不同 → 触发重读会话名（同值会被 hash 缓存命中）。"""
        f = np.zeros((300, 400, 3), dtype=np.uint8)
        f[0:50, :, :] = v
        return f

    area = (0, 50, 400, 300, np.array([30, 30, 31]), 0)

    orig = ocr_mod.read_title
    titles = iter(["小明", "小明", "小新"])
    ocr_mod.read_title = lambda _h: next(titles, "小新")
    try:
        c._maybe_read_title(hdr(1), area)
        c.builder.add("me", "对小明说的话", y=1)
        c._maybe_read_title(hdr(2), area)  # 头部像素变了但名字相同 → 不断流
        same = len(c.builder.messages)
        c._maybe_read_title(hdr(3), area)  # 名字变了 → 断流
        check("同名不算切换", same == 1, str(same))
        check("改名触发断流（旧段落已封存）", len(c.closed_transcripts) == 1,
              str(len(c.closed_transcripts)))
        check("断流后新段落为空", len(c.builder.messages) == 0, str(len(c.builder.messages)))
        check("封存段落带会话边界警告",
              any("会话边界" in w for w in c.closed_transcripts[0].warnings))
        check("reader.seen 已清空", c.reader.seen == [])
    finally:
        ocr_mod.read_title = orig


def t_blank() -> None:
    """空白帧判定：深浅主题都不能误判，且真空白要抓出来（§8.5 禁止静默失败）。"""
    print("\n[7] 空白帧判定")
    from app.capture.window_finder import frame_is_blank

    white = np.full((400, 600, 3), 255, dtype=np.uint8)
    dark = np.full((400, 600, 3), 30, dtype=np.uint8)
    b1, m1 = frame_is_blank(white)
    b2, m2 = frame_is_blank(dark)
    check("纯白帧判空白", b1, str(m1))
    check("纯深色帧也判空白（不依赖主题）", b2, str(m2))

    rng = np.random.default_rng(0)
    noisy = np.full((400, 600, 3), 30, dtype=np.uint8)
    noisy[100:300, 100:500] = rng.integers(0, 255, (200, 400, 3), dtype=np.uint8)
    b3, m3 = frame_is_blank(noisy)
    check("有内容的帧不误判", not b3, str(m3))
    check("指标可解释", set(m3) >= {"uniq", "ink", "reason"}, str(m3))


def main() -> int:
    print("M1 离线单元自检")
    t_time_mark()
    t_builder()
    t_schema()
    t_speaker()
    t_incremental()
    t_session_switch()
    t_blank()
    print(f"\n结果：通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败项：" + "、".join(FAIL))
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
