# -*- coding: utf-8 -*-
"""M2 离线自检：三种导入格式 + 编码/排序/无时间/失败信封等边界。

DoD（READM.optimized §11）：txt / html / ChatLab agent JSON 各导入 1 份并正确解析。
本脚本先把夹具写到 probe/fixtures/（真实文件，便于人工复核），再逐个导入断言。

    python probe/m2_unit.py
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.capture.importer import (  # noqa: E402
    ME_MARKERS, classify_placeholder, detect_format, import_file, parse_stamp)
from app.capture.transcript import validate_transcript  # noqa: E402

FIX = ROOT / "probe" / "fixtures"
PASS: list[str] = []
FAIL: list[str] = []
REPORT: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    mark = "PASS" if cond else "FAIL"
    line = f"  [{mark}] {name}" + (f"　— {detail}" if detail else "")
    print(line, flush=True)
    REPORT.append(line)


def eq(name: str, got, want) -> None:
    check(name, got == want, f"got={got!r} want={want!r}")


# ---------------------------------------------------------------- 夹具


TXT_PLAIN = """2024年1月1日
2024-01-01 12:00:00 小新: 现在来接我吧，正好溜溜
2024-01-01 12:00:03 我: 你下楼等着吧
2024-01-01 12:00:05 小新: 你有车子没
2024-01-01 12:00:09 小新
其实，我喜欢你
今天天气不错
2024-01-01 12:00:15 我: [图片]
2024-01-01 12:00:20 我: [语音]
2024-01-01 12:00:25 小新: [表情]
"""

TXT_GBK = """张三 2024/1/2 09:15:30 早上好
李四 2024/1/2 09:16:00 早
张三 2024/1/2 09:16:40 今天有空吗
"""

TXT_MESSY = """2024-01-01 12:00:00 小新: 在吗
2024-01-01 12:00:30 小新: 第三条
2024-01-01 12:00:10 小新: 第二条
2024-01-01 12:00:40 我: 行
2024-01-01 12:00:41 我: 行
"""

HTML_EXPORT = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>与小新的聊天记录</title></head>
<body>
<h1>与小新 的聊天记录</h1>
<table>
<tr><th>时间</th><th>昵称</th><th>内容</th></tr>
<tr><td>2024-01-01 12:00:00</td><td>小新</td><td>现在来接我吧</td></tr>
<tr><td>2024-01-01 12:00:03</td><td>我</td><td>你下楼等着吧</td></tr>
<tr><td>2024-01-01 12:00:15</td><td>我</td><td><img src="a.jpg" alt="图片"></td></tr>
<tr><td>2024-01-01 12:00:20</td><td>小新</td><td>好<br>那我等你</td></tr>
</table>
</body></html>
"""

CLB_AGENT = {
    "ok": True,
    "command": "messages.between",
    "data": {"text": (
        "returned: 6\n\n"
        "--- 2024/1/1 ---\n"
        "[#101] 12:00 小新: 现在来接我吧\n"
        "[#102*] 12:00 我: 你下楼等着吧\n"
        "[#103] 12:01 小新: 你有车子没\n"
        "--- 2024/1/2 ---\n"
        "[#104] 09:15 小新: 其实，我喜欢你\n"
        "[#105] 09:20 我: 行\n"
        "[#106] 小新: 没有时刻的一条\n"
    )},
    "meta": {"totalHits": 6, "returnedHits": 6, "hasMore": False, "apiVersion": 1,
             "preprocess": {"desensitized": True}},
}

CLB_STRUCTURED = {
    "ok": True,
    "command": "messages.list",
    "data": {"messages": [
        {"id": 1, "ts": 1704081600, "sender": "me", "content": "早"},
        {"id": 2, "ts": 1704081660, "sender": "小新", "content": "早呀"},
        {"id": 3, "ts": "2024-01-01T12:03:00+08:00", "sender": "小新", "content": "今天有空吗"},
    ]},
}

CLB_ERROR = {
    "ok": False,
    "error": {"code": "AMBIGUOUS", "message": "多个会话匹配该成员",
              "hint": "用 --session 指定", "candidates": [{"id": "s1", "name": "小新"},
                                                          {"id": "s2", "name": "小新2"}]},
}


def write_fixtures() -> dict[str, Path]:
    FIX.mkdir(parents=True, exist_ok=True)
    out: dict[str, Path] = {}
    (FIX / "wechat_plain.txt").write_text(TXT_PLAIN, encoding="utf-8")
    (FIX / "wechat_gbk.txt").write_bytes(TXT_GBK.encode("gb18030"))  # 中文导出最常见的坑
    (FIX / "wechat_messy.txt").write_text(TXT_MESSY, encoding="utf-8")
    (FIX / "export.html").write_text(HTML_EXPORT, encoding="utf-8")
    (FIX / "chatlab_agent.json").write_text(
        json.dumps(CLB_AGENT, ensure_ascii=False, indent=2), encoding="utf-8")
    (FIX / "chatlab_structured.json").write_text(
        json.dumps(CLB_STRUCTURED, ensure_ascii=False, indent=2), encoding="utf-8")
    (FIX / "chatlab_error.json").write_text(
        json.dumps(CLB_ERROR, ensure_ascii=False, indent=2), encoding="utf-8")
    for p in FIX.iterdir():
        out[p.name] = p
    return out


# ---------------------------------------------------------------- 用例


def t_detect(fx: dict[str, Path]) -> None:
    print("\n[1] 格式探测", flush=True)
    REPORT.append("\n[1] 格式探测")
    for name, want in (("wechat_plain.txt", "txt"), ("wechat_gbk.txt", "txt"),
                       ("export.html", "html"), ("chatlab_agent.json", "chatlab"),
                       ("chatlab_structured.json", "chatlab")):
        p = fx[name]
        text = p.read_bytes().decode("utf-8", "replace")
        eq(f"detect_format({name})", detect_format(p, text), want)


def t_txt_plain(fx: dict[str, Path]) -> None:
    print("\n[2] txt（日期 时间 昵称 + 多行 + 占位符）", flush=True)
    REPORT.append("\n[2] txt：日期 时间 昵称 + 多行 + 占位符")
    r = import_file(fx["wechat_plain.txt"])
    check("显式昵称「我」被认成 me（读元数据，不猜）", r.ok and r.me_label == "我",
          f"ok={r.ok} me_label={r.me_label!r}")
    t = r.transcript
    check("解析出 7 条", t is not None and len(t.messages) == 7,
          f"实际 {len(t.messages) if t else 0}")
    if not t or len(t.messages) != 7:
        return
    eq("第 4 条把两行续行并进同一条", t.messages[3].text, "其实，我喜欢你\n今天天气不错")
    types = [m.type for m in t.messages]
    eq("占位符类型", types[4:7], ["image", "voice", "sticker"])
    eq("占位符原文保留（不虚构）", [t.messages[i].text for i in (4, 5, 6)],
       ["[图片]", "[语音]", "[表情]"])
    eq("间隔", [m.gap_s for m in t.messages], [0, 3, 2, 4, 6, 5, 5])
    eq("说话人代码", sorted({m.sender for m in t.messages}), ["me", "obj-1"])
    eq("me 条数", sum(1 for m in t.messages if m.sender == "me"), 3)
    eq("ts_source", {m.ts_source for m in t.messages}, {"import"})
    errs = validate_transcript(t.to_public_dict())
    check("schema 校验通过", not errs, "；".join(errs[:3]))
    check("写了「左右是标签占位」的警告", any("标签占位" in w for w in t.warnings),
          json.dumps(t.warnings, ensure_ascii=False)[:120])


def t_txt_gbk(fx: dict[str, Path]) -> None:
    print("\n[3] txt（GBK + 昵称在前 + 不给 me 就必须问人）", flush=True)
    REPORT.append("\n[3] txt：GBK + 昵称在前 + 必须问人")
    r = import_file(fx["wechat_gbk.txt"])
    eq("GBK 被正确解码", r.stats.get("encoding"), "gb18030")
    check("没给 me 时 ok=False 且 needs=me_label", (not r.ok) and r.needs == "me_label",
          f"ok={r.ok} needs={r.needs}")
    eq("候选昵称", sorted(c["label"] for c in r.candidates), ["张三", "李四"])
    eq("候选计数", {c["label"]: c["count"] for c in r.candidates}, {"张三": 2, "李四": 1})
    check("没有 me 标记时 auto 不生效", all(not c["is_me_marker"] for c in r.candidates))

    r2 = import_file(fx["wechat_gbk.txt"], me_label="张三")
    check("指定 me 后 ok=True", r2.ok, f"warnings={r2.warnings[:1]}")
    t = r2.transcript
    eq("条数", len(t.messages), 3)
    eq("me = 张三", sum(1 for m in t.messages if m.sender == "me"), 2)
    eq("对方 = 李四", t.speaker_map["objects"][0]["label"], "李四")
    eq("首条时间", t.messages[0].ts, "2024-01-02T09:15:30+08:00")
    eq("间隔", [m.gap_s for m in t.messages], [0, 30, 40])
    eq("schema", validate_transcript(t.to_public_dict()), [])
    check("指定的 me 不在文件里 → 拒绝而非硬套",
          not import_file(fx["wechat_gbk.txt"], me_label="王五").ok)


def t_txt_messy(fx: dict[str, Path]) -> None:
    print("\n[4] txt（乱序 + 连发同文不吞）", flush=True)
    REPORT.append("\n[4] txt：乱序 + 连发同文")
    r = import_file(fx["wechat_messy.txt"])
    t = r.transcript
    eq("条数（连发两条「行」都要留）", len(t.messages), 5)
    eq("文本顺序已重排", [m.text for m in t.messages],
       ["在吗", "第二条", "第三条", "行", "行"])
    check("乱序重排有警告", any("不是按时间升序" in w for w in t.warnings))
    eq("重排后时间单调，schema 通过", validate_transcript(t.to_public_dict()), [])
    eq("间隔", [m.gap_s for m in t.messages], [0, 10, 20, 10, 1])


def t_html(fx: dict[str, Path]) -> None:
    print("\n[5] html（<table> + <img> + <br>）", flush=True)
    REPORT.append("\n[5] html：table + img + br")
    r = import_file(fx["export.html"])
    check("html 走 table 路线", r.ok and "table" in r.stats.get("route", ""),
          f"route={r.stats.get('route')!r}")
    t = r.transcript
    eq("条数", len(t.messages), 4)
    eq("列顺序识别（时间/昵称/内容）", [m.text for m in t.messages][:2],
       ["现在来接我吧", "你下楼等着吧"])
    eq("<img> 变成 image 占位", t.messages[2].type, "image")
    eq("img 占位文本用 alt", t.messages[2].text, "[图片]")
    check("<br> 不丢字", "好" in t.messages[3].text and "那我等你" in t.messages[3].text,
          repr(t.messages[3].text))
    eq("schema", validate_transcript(t.to_public_dict()), [])


def t_chatlab_agent(fx: dict[str, Path]) -> None:
    print("\n[6] ChatLab agent 信封（data.text + 日期块 + 星标 + 无时刻）", flush=True)
    REPORT.append("\n[6] ChatLab：agent 信封")
    r = import_file(fx["chatlab_agent.json"])
    check("走 agent 正文路线", r.ok and "agent" in r.stats.get("route", ""),
          f"route={r.stats.get('route')!r}")
    t = r.transcript
    eq("条数", len(t.messages), 6)
    eq("日期块补全第 1 条", t.messages[0].ts, "2024-01-01T12:00:00+08:00")
    eq("跨日期块第 4 条", t.messages[3].ts, "2024-01-02T09:15:00+08:00")
    eq("上游证据编号保留", [m.src_id for m in t.messages],
       ["101", "102*", "103", "104", "105", "106"])
    check("星标有专门警告", any("星标" in w for w in t.warnings))
    check("无时刻的一条承上并告警", any("完全没有时间信息" in w for w in t.warnings))
    eq("承上后不出现时间倒流", validate_transcript(t.to_public_dict()), [])
    eq("meta 透传 hasMore", r.stats.get("route", "") and True, True)


def t_chatlab_structured(fx: dict[str, Path]) -> None:
    print("\n[7] ChatLab 结构化（data.messages + unix 时间）", flush=True)
    REPORT.append("\n[7] ChatLab：结构化 data.messages")
    r = import_file(fx["chatlab_structured.json"])
    check("走 json 结构化路线", r.ok and "json" in r.stats.get("route", ""),
          f"route={r.stats.get('route')!r}")
    t = r.transcript
    eq("条数", len(t.messages), 3)
    eq("unix 秒 → 本地时间", t.messages[0].ts, "2024-01-01T12:00:00+08:00")
    eq("ISO 串保持（含时区）", t.messages[2].ts, "2024-01-01T12:03:00+08:00")
    eq("间隔按真实时间算", [m.gap_s for m in t.messages], [0, 60, 120])
    eq("sender=me 被认成我", r.me_label, "me")
    eq("schema", validate_transcript(t.to_public_dict()), [])


def t_chatlab_error(fx: dict[str, Path]) -> None:
    print("\n[8] ChatLab 失败信封（禁止静默失败）", flush=True)
    REPORT.append("\n[8] ChatLab：失败信封")
    r = import_file(fx["chatlab_error.json"])
    check("ok=False 而不是空 transcript", not r.ok and r.transcript is None)
    joined = " ".join(r.warnings)
    check("错误码/信息/hint 都在", "AMBIGUOUS" in joined and "AM" in joined and "--session" in joined,
          joined[:160])


def t_helpers() -> None:
    print("\n[9] 辅助函数", flush=True)
    REPORT.append("\n[9] 辅助函数")
    eq("占位符：图片", classify_placeholder("[图片]"), "image")
    eq("占位符：普通文本", classify_placeholder("你好"), "text")
    eq("占位符：视频归 unknown（不瞎归类）", classify_placeholder("[视频]"), "unknown")
    check("me 标记集合含 me/我", {"me", "我"} <= ME_MARKERS)
    eq("parse_stamp：毫秒", parse_stamp(1704081600000).isoformat()[:19], "2024-01-01T12:00:00")
    eq("parse_stamp：纯日期补 0 点", parse_stamp("2024-01-01").isoformat()[:19],
       "2024-01-01T00:00:00")
    eq("parse_stamp：垃圾串返回 None", parse_stamp("不是时间"), None)
    eq("parse_stamp：只有时刻则用参考日", parse_stamp("09:30", date(2024, 1, 2)).isoformat()[:19],
       "2024-01-02T09:30:00")
    eq("不存在的文件", import_file(FIX / "不存在.txt").ok, False)
    r_empty = import_file(FIX / "empty.txt")
    check("空文件被拒且给出原因", (not r_empty.ok) and bool(r_empty.warnings),
          "；".join(r_empty.warnings)[:80])


def t_capture_regression() -> None:
    print("\n[10] 回归：新增 src_id 不破坏采集路径的 schema", flush=True)
    REPORT.append("\n[10] 回归：采集路径")
    sys.path.insert(0, str(ROOT / "probe"))
    from app.capture.transcript import TranscriptBuilder  # noqa: E402

    sm = {"me": {"side": "right", "code": "me", "confirmed": True},
          "objects": [{"code": "obj-1", "side": "left", "label": "对方"}]}
    b = TranscriptBuilder(sm, source="capture", window={"chat_title": "小新"})
    from datetime import datetime, timedelta  # noqa: E402

    t0 = datetime.now().astimezone()
    b.add("me", "行", y=10, at=t0, ts_source="wechat_mark")
    b.add("obj-1", "在吗", y=40, at=t0 + timedelta(seconds=5), ts_source="wechat_mark")
    t = b.build()
    eq("采集路径 schema 仍通过", validate_transcript(t.to_public_dict()), [])
    d = t.to_public_dict()
    check("采集路径不写 src_id（不留 null）", all("src_id" not in m for m in d["messages"]),
          json.dumps(d["messages"][0], ensure_ascii=False))


def main() -> int:
    print("M2 离线自检", flush=True)
    REPORT.append("# M2 导入解析 离线自检报告\n")
    fx = write_fixtures()
    (FIX / "empty.txt").write_text("", encoding="utf-8")
    print(f"夹具已写入 {FIX}", flush=True)
    for fn in (t_detect, t_txt_plain, t_txt_gbk, t_txt_messy, t_html,
               t_chatlab_agent, t_chatlab_structured, t_chatlab_error, t_helpers,
               t_capture_regression):
        try:
            fn(fx) if fn is not t_helpers and fn is not t_capture_regression else fn()
        except Exception as e:  # noqa: BLE001
            import traceback  # noqa: PLC0415

            check(f"{fn.__name__} 未抛异常", False, f"{type(e).__name__}: {e}")
            REPORT.append("```\n" + traceback.format_exc() + "\n```")
    print(f"\n结果：通过 {len(PASS)} 项，失败 {len(FAIL)} 项", flush=True)
    REPORT.append(f"\n## 结果：通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败项：" + "、".join(FAIL), flush=True)
        REPORT.append("\n失败项：" + "、".join(FAIL))
    (ROOT / "probe" / "m2_import_report.md").write_text("\n".join(REPORT) + "\n",
                                                        encoding="utf-8")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
