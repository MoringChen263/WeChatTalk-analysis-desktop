# -*- coding: utf-8 -*-
"""transcript：会话组装、去重、编号、间隔、导出。

契约（README.optimized §5.2）：
- 每行 = `#id 时间 sender 原文`，并记录相邻间隔 `gap_s` 与主动消息统计；
- `type ∈ text | image | voice | sticker | unknown`，缺失即标注，**不虚构**；
- v1 只做一对一，但 schema 预留 `speaker_map.objects[]`，v2 扩群聊不破坏契约。

时间来源的优先级（M-1 实测结论）：
1. 微信自己的灰色时间戳分隔行（「14:01」/「昨天 14:01」/「9月21日 14:01」）→ **可信时间**；
2. 没有时间戳时退回采集时刻，并在 `warnings` 里标注这批消息是估算时间。
"""
from __future__ import annotations

import difflib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from app.capture.ocr import RE_TIME_MARK

SCHEMA_VERSION = "1.0"
SOURCES = ("ocr", "capture", "import")
TYPES = ("text", "image", "voice", "sticker", "unknown")
SIDES = ("left", "right")

_WS = re.compile(r"[\s\u2005\u00a0]+")
_PUNCT = re.compile(r"[，。！？、,.!?~～…·\-—_\"'“”‘’（）()《》\[\]【】:：;；]")


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def norm_text(text: str) -> str:
    """归一化：去空白与标点，用于判重（§5.2 第 2/4 步）。"""
    return _PUNCT.sub("", _WS.sub("", text or ""))


def ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def parse_time_mark(text: str, ref: datetime | None = None) -> datetime | None:
    """微信灰字时间戳 → datetime。认不出返回 None。

    规则：「14:01」= 今天（若晚于当前时刻则算昨天）；「昨天/前天」按相对日推；
    「9月21日」按今年；「星期一」无法定位到日期（可能是上周），只取时分。
    """
    m = RE_TIME_MARK.match((text or "").strip())
    if not m:
        return None
    ref = ref or datetime.now().astimezone()
    hh, mm = int(m.group("hh")), int(m.group("mm"))
    if hh > 23 or mm > 59:
        return None
    mon, day, rel = m.group("mon"), m.group("day"), m.group("rel")
    if mon and day:
        try:
            base = ref.replace(month=int(mon), day=int(day), hour=hh, minute=mm,
                               second=0, microsecond=0)
        except ValueError:
            return None
        if base > ref + timedelta(minutes=2):  # 月份比当前大 = 去年的那天
            base = base.replace(year=base.year - 1)
        return base
    if rel == "昨天":
        base = ref - timedelta(days=1)
    elif rel == "前天":
        base = ref - timedelta(days=2)
    else:
        base = ref
    base = base.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if rel is None and base > ref + timedelta(minutes=2):
        base -= timedelta(days=1)  # 无标注但时刻在未来 = 昨天的消息
    return base


@dataclass
class Message:
    id: int
    ts: str
    gap_s: int
    sender: str
    type: str = "text"
    text: str = ""
    confidence: float | None = None
    ts_source: str = "capture"  # capture | wechat_mark | before_mark | import
    src_id: str | None = None  # 导入时保留的上游证据编号（ChatLab 的 [#1021]），采集路径为 None
    anchor_y: int | None = None  # 仅内部用：帧内 y 位置，便于排查拼接

    def line(self) -> str:
        return f"#{self.id} {self.ts} {self.sender} {self.text}"


@dataclass
class Transcript:
    version: str = SCHEMA_VERSION
    source: str = "capture"
    captured_range: list[str] = field(default_factory=list)
    window: dict = field(default_factory=dict)
    speaker_map: dict = field(default_factory=dict)
    messages: list[Message] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    # -- 导出 --
    def to_dict(self) -> dict:
        d = asdict(self)
        d["messages"] = [asdict(m) for m in self.messages]
        return d

    def to_public_dict(self) -> dict:
        """去掉内部排查字段（anchor_y），用于写盘/喂 LLM。"""
        d = self.to_dict()
        for m in d["messages"]:
            m.pop("anchor_y", None)
            if m.get("src_id") is None:
                m.pop("src_id", None)  # 采集路径没有上游编号，别在 JSON 里刷一片 null
        return d

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_public_dict(), ensure_ascii=False, indent=indent)

    def save_json(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self.to_json(), encoding="utf-8")
        return p

    def to_lines(self) -> list[str]:
        return [m.line() for m in self.messages]

    def to_text(self, header: bool = True) -> str:
        out = []
        if header:
            win = self.window or {}
            out.append(f"# 会话记录 source={self.source} 窗口={win.get('class', '?')} "
                       f"共 {len(self.messages)} 条")
            for w in self.warnings:
                out.append(f"# 注意：{w}")
            out.append("")
        out.extend(self.to_lines())
        return "\n".join(out)

    def to_markdown(self) -> str:
        out = [f"# transcript（{len(self.messages)} 条）", ""]
        if self.captured_range:
            out.append(f"- 时间范围：{' ~ '.join(self.captured_range)}")
        sm = self.speaker_map or {}
        me = sm.get("me", {})
        objs = ", ".join(f"{o.get('code')}={o.get('label') or o.get('side')}"
                         for o in sm.get("objects", []))
        out.append(f"- 说话人：我({me.get('side', '?')}，已确认={me.get('confirmed')})"
                   f"　对方：{objs or '?'}")
        out.append("")
        for w in self.warnings:
            out.append(f"> ⚠️ {w}")
        if self.warnings:
            out.append("")
        out += ["| # | 时间 | 间隔 | 说话人 | 内容 |", "| --- | --- | --- | --- | --- |"]
        for m in self.messages:
            gap = f"{m.gap_s}s" if m.gap_s else "—"
            safe = m.text.replace("|", "\\|")  # f-string 里不能放反斜杠（py3.11），先算好
            out.append(f"| {m.id} | {m.ts} | {gap} | {m.sender} | {safe} |")
        return "\n".join(out)

    # -- 统计（供 M3 prompt 与 UI 展示）--
    def stats(self) -> dict:
        me = [m for m in self.messages if m.sender == (self.speaker_map or {}).get("me", {}).get("code", "me")]
        obj = [m for m in self.messages if m not in me]
        gaps = [m.gap_s for m in self.messages if m.gap_s]
        return {
            "total": len(self.messages),
            "me": len(me),
            "object": len(obj),
            "me_chars": sum(len(m.text) for m in me),
            "object_chars": sum(len(m.text) for m in obj),
            "first_ts": self.messages[0].ts if self.messages else None,
            "last_ts": self.messages[-1].ts if self.messages else None,
            "max_gap_s": max(gaps) if gaps else 0,
            "avg_object_len": round(sum(len(m.text) for m in obj) / len(obj), 1) if obj else 0.0,
            "image_placeholders": sum(1 for m in self.messages if m.type != "text"),
        }

    def validate(self) -> list[str]:
        return validate_transcript(self.to_public_dict())


def validate_transcript(d: dict) -> list[str]:
    """零依赖的结构校验（与 §5.2 schema 对应）。返回问题列表，空 = 通过。"""
    errs: list[str] = []

    def need(cond: bool, msg: str) -> None:
        if not cond:
            errs.append(msg)

    need(isinstance(d, dict), "顶层不是对象")
    if not isinstance(d, dict):
        return errs
    need(d.get("version") == SCHEMA_VERSION, f"version 应为 {SCHEMA_VERSION}，实际 {d.get('version')!r}")
    need(d.get("source") in SOURCES, f"source 非法：{d.get('source')!r}")
    cr = d.get("captured_range")
    need(isinstance(cr, list) and len(cr) == 2, "captured_range 应为长度 2 的数组")
    need(isinstance(d.get("window"), dict), "window 应为对象")
    sm = d.get("speaker_map")
    need(isinstance(sm, dict), "speaker_map 应为对象")
    if isinstance(sm, dict):
        me = sm.get("me") or {}
        need(me.get("side") in SIDES, f"speaker_map.me.side 应为 left/right，实际 {me.get('side')!r}")
        need(isinstance(me.get("confirmed"), bool), "speaker_map.me.confirmed 应为布尔")
        objs = sm.get("objects")
        need(isinstance(objs, list) and len(objs) >= 1, "speaker_map.objects 至少 1 项")
        for o in objs or []:
            need(o.get("side") in SIDES, f"objects.side 非法：{o.get('side')!r}")
            need(bool(o.get("code")), "objects.code 不能为空")
    msgs = d.get("messages")
    need(isinstance(msgs, list), "messages 应为数组")
    codes = {"me"} | {o.get("code") for o in (sm or {}).get("objects", []) if isinstance(o, dict)}
    last_id, last_dt = 0, None
    for i, m in enumerate(msgs or []):
        tag = f"messages[{i}]"
        if not isinstance(m, dict):
            errs.append(f"{tag} 不是对象")
            continue
        need(isinstance(m.get("id"), int) and m["id"] == last_id + 1,
             f"{tag}.id 应从 1 连续递增，实际 {m.get('id')!r}")
        last_id = m.get("id") if isinstance(m.get("id"), int) else last_id
        need(m.get("sender") in codes, f"{tag}.sender 未在 speaker_map 里登记：{m.get('sender')!r}")
        need(m.get("type") in TYPES, f"{tag}.type 非法：{m.get('type')!r}")
        need(isinstance(m.get("text"), str) and m["text"] != "", f"{tag}.text 不能为空")
        need(isinstance(m.get("gap_s"), int) and m["gap_s"] >= 0, f"{tag}.gap_s 应为非负整数")
        ts = m.get("ts")
        try:
            dt = datetime.fromisoformat(ts)
            need(dt.tzinfo is not None, f"{tag}.ts 缺时区")
            if last_dt is not None and dt < last_dt:
                errs.append(f"{tag}.ts 早于上一条（时间倒流）")
            last_dt = dt
        except (TypeError, ValueError):
            errs.append(f"{tag}.ts 不是合法 ISO8601：{ts!r}")
    ws = d.get("warnings")
    need(isinstance(ws, list) and all(isinstance(w, str) for w in (ws or [])), "warnings 应为字符串数组")
    return errs


class TranscriptBuilder:
    """帧 → transcript 的累积器。负责：时间锚点解析、行级判重、编号、间隔计算。"""

    def __init__(self, speaker_map: dict, source: str = "capture", window: dict | None = None,
                 dup_threshold: float = 0.15, max_messages: int = 600):
        self.sm = speaker_map or {}
        self.source = source
        self.window = window or {}
        self.dup_threshold = dup_threshold
        self.max_messages = max_messages
        self.messages: list[Message] = []
        self.warnings: list[str] = []
        self._anchor: datetime | None = None  # 最近一次微信时间戳
        self._anchor_y: int = -1
        self._fallback_used = 0
        self._before_mark = 0
        self._dup_dropped = 0

    # -- 时间锚点 --
    def set_anchor(self, text: str, y: int, ref: datetime | None = None) -> bool:
        dt = parse_time_mark(text, ref)
        if dt is None:
            return False
        self._anchor, self._anchor_y = dt, y
        return True

    def anchor_at(self, y: int) -> tuple[datetime | None, str]:
        """取 y 位置对应的可信时间：优先该 y 之上的最近锚点，无锚点则回退采集时刻。"""
        if self._anchor is not None and y >= self._anchor_y:
            return self._anchor, "wechat_mark"
        return None, "capture"

    # -- 追加 --
    def add(self, sender: str, text: str, y: int | None = None, type_: str = "text",
            confidence: float | None = None, at: datetime | None = None,
            ts_source: str | None = None) -> Message | None:
        text = (text or "").strip()
        if not text:
            return None
        if self._is_dup(sender, text, at):
            self._dup_dropped += 1
            return None
        if self._anchor is not None and ts_source is None:
            if y is not None and y >= self._anchor_y:
                at, ts_source = self._anchor, "wechat_mark"
            elif at is None:
                # 画面顶部的历史消息：它在时间戳**之前**，但我们不知道早多久。
                # 绝不能拿采集时刻凑（那会让时间倒流），退成「锚点 -1 秒」当估算上界。
                at = self._anchor - timedelta(seconds=1)
                ts_source = "before_mark"
                self._before_mark += 1
        if at is None:
            at, ts_source = datetime.now().astimezone(), "capture"
            self._fallback_used += 1
        prev = self.messages[-1] if self.messages else None
        gap = 0
        if prev is not None:
            try:
                gap = max(0, int((at - datetime.fromisoformat(prev.ts)).total_seconds()))
            except ValueError:
                gap = 0
        msg = Message(id=len(self.messages) + 1, ts=at.isoformat(timespec="seconds"), gap_s=gap,
                      sender=sender, type=type_, text=text, confidence=confidence,
                      ts_source=ts_source or "capture", anchor_y=y)
        self.messages.append(msg)
        if len(self.messages) > self.max_messages:
            self.messages = self.messages[-self.max_messages:]
            for i, m in enumerate(self.messages, 1):
                m.id = i
        return msg

    def add_placeholder(self, sender: str, kind: str = "image", y: int | None = None,
                        at: datetime | None = None) -> Message | None:
        """缺失即标注、不虚构（§5.2）。`kind` 只能是 image/voice/sticker/unknown。"""
        kind = kind if kind in TYPES else "unknown"
        label = {"image": "[图片]", "voice": "[语音]", "sticker": "[表情]"}.get(kind, "[未识别消息]")
        return self.add(sender, label, y=y, type_=kind, confidence=0.0,
                        at=at, ts_source="capture")

    def _is_dup(self, sender: str, text: str, at: datetime | None) -> bool:
        """§5.2 第 4 步：同一 sender、Levenshtein 归一距离 < dup_threshold 且时间戳同分钟 → 重复。
        ponytail: 同一人一分钟内连发两条一模一样的（「在吗」「在吗」）会被吞一条——
        对触发分析无害，且比重复报两遍更安全。"""
        if not self.messages:
            return False
        last = self.messages[-1]
        if last.sender != sender or last.type != "text":
            return False
        if 1.0 - ratio(norm_text(last.text), norm_text(text)) >= self.dup_threshold:
            return False
        if at is None or last.ts_source != "wechat_mark":
            return True
        try:
            if abs((at - datetime.fromisoformat(last.ts)).total_seconds()) < 60:
                return True
        except ValueError:
            return True
        return False

    # -- 产出 --
    def build(self) -> Transcript:
        msgs = self.messages
        if msgs:
            rng = [msgs[0].ts, msgs[-1].ts]
        else:
            rng = [now_iso(), now_iso()]
        warn = list(self.warnings)
        if self._before_mark:
            warn.append(f"{self._before_mark} 条消息位于已知时间戳之前（画面顶部的历史消息），"
                        f"时间为「锚点 -1 秒」的估算上界，且首条可能是上一段对话的结尾")
        if self._fallback_used:
            warn.append(f"{self._fallback_used} 条无微信时间戳，时间用采集时刻估算（间隔仅供参考）")
        if self._dup_dropped:
            warn.append(f"按相似度判重丢弃 {self._dup_dropped} 条疑似重复行")
        if not msgs:
            warn.append("未识别到任何消息（可能是空白帧或未打开聊天窗口）")
        t = Transcript(source=self.source, captured_range=rng, window=self.window,
                       speaker_map=_normalize_sm(self.sm, bool(msgs)), messages=msgs, warnings=warn)
        return t

    def plain(self) -> str:
        """不落盘的即时预览文本（UI 用，带警告头）。"""
        t = self.build()
        return t.to_text()


def _normalize_sm(sm: dict, has_msgs: bool) -> dict:
    """补齐 speaker_map 结构，保证过 schema。"""
    sm = dict(sm or {})
    me = dict(sm.get("me") or {})
    me.setdefault("side", "right")
    me.setdefault("code", "me")
    me["confirmed"] = bool(me.get("confirmed"))
    objs = list(sm.get("objects") or [])
    if not objs:
        objs = [{"code": "obj-1", "side": "left" if me["side"] == "right" else "right", "label": "对方"}]
    out = {"me": me, "objects": objs}
    if not me["confirmed"] and has_msgs:
        pass  # 由调用方补 warning，避免这里埋字符串
    return out
