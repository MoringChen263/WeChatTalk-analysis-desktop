# -*- coding: utf-8 -*-
"""importer：把用户**已有的**聊天导出文件归一化进同一条 transcript 管道（§5.2 / §11 M2）。

支持三种格式：

1. `txt`  —— 各类导出器的纯文本排版（多种风格，按策略链自动选最合适的一种）；
2. `html` —— 导出网页，优先按 `<table>` 单元格解析，失败则剥标签回退到 txt 策略；
3. `chatlab` —— ChatLab CLI `--format agent` 的**响应信封**，正文在 `data.text`
   （`--- 2026/6/1 ---` 日期块 + `[#1*] 09:00 老王: 内容`）；同时兼容 `--format json`
   的结构化 `data.messages`，以及 `{ok:false,error:{...}}` 的失败信封。

三条硬规则（来自 §5.2 与 goutoujunshi 的契约边界）：

- **不猜说话人**。文件里只有昵称/ID，谁是「我」必须由用户指定（`me_label`）。
  没指定时只返回候选给 UI，绝不产出「已确认」的 speaker_map。唯一的例外是文件里
  **显式**写着 `me` / `我` / `本人`——那是元数据，照读不叫猜。
- **不虚构内容**。图片/语音/表情只做占位标注并置 `type`，绝不脑补文字。
- **禁止静默失败**。格式认不出、时间缺日期、编码退化、超长截断、失败信封，
  全部进 `warnings` 并且 `ok=False` 时给出 `needs` 让上层去问人。

关于左右：导入文件没有几何信息，按 §5.2 的固定契约记 `me=right / obj=left`，
这只是**标签占位**，会显式写进 warnings，不代表真实位置。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from html.parser import HTMLParser
from pathlib import Path

from app.capture.transcript import Message, Transcript, TranscriptBuilder
from app.capture.transcript import SOURCES as _SOURCES  # noqa: F401  (便于调用方校验)

# 显式标记「数据所有者」的昵称写法——读元数据，不是猜
ME_MARKERS = {"me", "我", "本人", "自己", "我(me)", "我（me）", "myself", "owner"}
UNKNOWN_LABEL = "（无昵称）"
NOMINAL_WARN = ("导入文件没有左右信息，按 §5.2 固定契约记 me=right / 对象=left，"
                "这只是标签占位，不代表真实位置")
PLACEHOLDER_TYPES = {
    "[图片]": "image", "[照片]": "image", "[图片消息]": "image", "[img]": "image",
    "[语音]": "voice", "[语音消息]": "voice",
    "[表情]": "sticker", "[动画表情]": "sticker", "[贴纸]": "sticker",
    "[视频]": "unknown", "[文件]": "unknown", "[链接]": "unknown",
    "[位置]": "unknown", "[转账]": "unknown", "[名片]": "unknown",
    "[微信红包]": "unknown", "[聊天记录]": "unknown", "[系统消息]": "unknown",
}

# ---------------------------------------------------------------- 数据结构


@dataclass
class RawMsg:
    """解析出来的原始一条：还没编排号、还没映射说话人。"""
    ts: datetime | None
    sender: str
    text: str
    type_: str = "text"
    src_id: str | None = None
    src_date: date | None = None  # 只有时刻没有日期时，记下上层给的日期上下文


@dataclass
class ImportResult:
    path: str
    fmt: str
    ok: bool
    needs: str | None = None            # None | "me_label"
    candidates: list[dict] = field(default_factory=list)  # [{"label","count","is_me_marker"}]
    me_label: str | None = None
    transcript: Transcript | None = None
    warnings: list[str] = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    def preview_lines(self, limit: int = 20, raw: list[RawMsg] | None = None) -> list[str]:
        """只读预览（对应 skill 的「先只读预览」原则）：不引用全文，只给结构。"""
        out = []
        src = raw or []
        for i, m in enumerate(src[:limit], 1):
            t = m.ts.isoformat(timespec="minutes") if m.ts else "（无日期）"
            out.append(f"{i:>3}. {t}  {m.sender or UNKNOWN_LABEL}: {m.text[:40]}")
        if len(src) > limit:
            out.append(f"…（共 {len(src)} 条）")
        return out


# ---------------------------------------------------------------- 编码


def read_text(path: Path) -> tuple[str, str, list[str]]:
    """读文件 → (文本, 编码, 警告)。中文导出常见 GBK，必须逐级降级并如实上报。"""
    raw = path.read_bytes()
    warn: list[str] = []
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig", "replace"), "utf-8-sig", warn
    for enc in ("utf-8", "gb18030", "utf-16"):
        try:
            return raw.decode(enc), enc, warn
        except UnicodeDecodeError:
            continue
    warn.append("文本编码不认识（utf-8/gb18030/utf-16 都失败），按 gb18030 强行替换解码，"
                "可能有乱码")
    return raw.decode("gb18030", "replace"), "gb18030(replace)", warn


def _sniff_charset(raw: bytes) -> str | None:
    m = re.search(rb'charset\s*=\s*["\']?\s*([\w-]+)', raw[:4096], re.I)
    if m:
        enc = m.group(1).decode("ascii", "ignore").strip().lower()
        return {"gb2312": "gb18030", "gbk": "gb18030"}.get(enc, enc)
    return None


def read_html(path: Path) -> tuple[str, str, list[str]]:
    raw = path.read_bytes()
    warn: list[str] = []
    enc = _sniff_charset(raw)
    if enc:
        try:
            return raw.decode(enc, "replace"), enc, warn
        except LookupError:
            warn.append(f"页面声明的编码 {enc!r} 本机不认，回退自动探测")
    text, used, w = read_text(path)
    return text, used, warn + w


# ---------------------------------------------------------------- 基础解析


def parse_date(s: str) -> date | None:
    """2024-01-01 / 2024/1/1 / 2024.1.1 / 2024年1月1日 → date。"""
    m = re.match(r"^\s*(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*日?\s*$", s or "")
    if not m:
        return None
    y, mo, d = (int(x) for x in m.groups())
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def parse_time(s: str) -> dtime | None:
    m = re.match(r"^\s*(\d{1,2}):(\d{2})(?::(\d{2}))?\s*$", s or "")
    if not m:
        return None
    hh, mm, ss = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
    if hh > 23 or mm > 59 or ss > 59:
        return None
    return dtime(hh, mm, ss)


def _combine(d: date | None, t: dtime | None) -> datetime | None:
    if d is None or t is None:
        return None
    return datetime.combine(d, t).astimezone()


def parse_stamp(s: str, default_date: date | None = None) -> datetime | None:
    """把一整串时间戳字符串（可能是 date、time、datetime、unix 秒/毫秒）解析成 datetime。"""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        sec = float(s)
        if sec > 1e11:  # 毫秒
            sec /= 1000.0
        try:
            return datetime.fromtimestamp(sec).astimezone()
        except (OSError, OverflowError, ValueError):
            return None
    s = str(s).strip()
    if not s:
        return None
    if re.fullmatch(r"\d{10,13}", s):
        return parse_stamp(int(s))
    # 纯日期
    d = parse_date(s)
    if d:
        return _combine(d, dtime(0, 0, 0))
    # 纯时刻
    t = parse_time(s)
    if t:
        return _combine(default_date, t)
    # datetime 串
    m = re.match(r"^\s*(?P<d>\d{4}[-/.年]\s*\d{1,2}[-/.月]\s*\d{1,2}日?)"
                 r"[ T]+(?P<t>\d{1,2}:\d{2}(?::\d{2})?)\s*$", s)
    if m:
        return _combine(parse_date(m.group("d")), parse_time(m.group("t")))
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def classify_placeholder(text: str) -> str:
    """整条就是占位符 → 给出 type；否则 text。绝不因为「像图片」就改判。"""
    return PLACEHOLDER_TYPES.get((text or "").strip(), "text")


# ---------------------------------------------------------------- txt


# 策略链：从上往下第一个「命中行数最多」的胜出。每条都要求昵称非空。
#
# 昵称一律用 `[^:：\s]`（不含空白与冒号）——这一条是踩出来的：
#   1) 若允许昵称含空格，`2024-01-01 12:00:00 小新` 会把整个后半句吃成昵称；
#   2) 「有冒号」的分支必须排在「无冒号」分支前面，否则惰性匹配会让
#      `小新: 现在来接我吧` 变成昵称「小」+ 内容「: 现在来接我吧」。
# 于是每个日期型策略都是 `(有冒号的昵称+内容 | 无冒号的昵称 + 尾巴)` 两分支。
_NAME_COLON = r"(?:(?P<name1>[^:：\s]{1,32}?)\s*[:：]\s*(?P<content1>.*)|(?P<name2>[^:：\s]{1,32})\s*(?P<content2>.*))$"

_TXT_STRATEGIES: list[tuple[str, re.Pattern]] = [
    ("日期 时间 昵称 [内容]", re.compile(
        r"^\s*(?P<date>\d{4}[-/.]\d{1,2}[-/.]\d{1,2})[ T]+(?P<time>\d{1,2}:\d{2}(?::\d{2})?)\s+"
        + _NAME_COLON)),
    ("[日期 时间] 昵称: 内容", re.compile(
        r"^\s*[\[\(](?P<date>\d{4}[-/.]\d{1,2}[-/.]\d{1,2})[ T]+(?P<time>\d{1,2}:\d{2}(?::\d{2})?)[\]\)]\s*"
        + _NAME_COLON)),
    ("昵称 日期 时间 [内容]", re.compile(
        r"^\s*(?P<name3>[^\s:：\d][^:：\s]{0,31})[ ]+"
        r"(?P<date>\d{4}[-/.]\d{1,2}[-/.]\d{1,2})[ T]+(?P<time>\d{1,2}:\d{2}(?::\d{2})?)\s*"
        r"(?:[:：]\s*)?(?P<content3>.*)$")),
    ("时刻 昵称: 内容", re.compile(
        r"^\s*(?P<time>\d{1,2}:\d{2}(?::\d{2})?)[ ]+(?P<name>[^:：\s]{1,32}?)\s*[:：]\s*(?P<content>.*)$")),
    ("昵称: 内容", re.compile(
        r"^\s*(?P<name>[^:：\s]{1,32})[:：]\s*(?P<content>.*)$")),
]

# 各策略把匹配到的分组归一化成 (name, content)
_GROUP_ALIAS = {"name": ("name1", "name2", "name3"), "content": ("content1", "content2", "content3")}


def _pick_group(g: dict, kind: str) -> str:
    for key in _GROUP_ALIAS[kind]:
        if key in g and g.get(key) is not None:
            return g[key]
    return g.get(kind) or ""

_DATE_LINE = re.compile(r"^\s*[-—=*\s]*(?P<d>\d{4}[-/.年]\s*\d{1,2}[-/.月]\s*\d{1,2}\s*日?)[-—=*\s]*$")


def _txt_scan(lines: list[str], pat: re.Pattern) -> int:
    n = 0
    for ln in lines:
        if pat.match(ln):
            n += 1
            if n >= 400:
                break
    return n


def parse_txt(text: str, default_date: date | None = None,
              known_labels: set[str] | None = None) -> tuple[list[RawMsg], list[str], str]:
    """→ (消息列表, 警告, 用到的策略名)。"""
    warn: list[str] = []
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    nonempty = [ln for ln in lines if ln.strip()]
    if not nonempty:
        return [], ["文件里没有任何非空行"], "无"

    # 选策略：命中行数最多者；平手时取靠前的（更严格的排版）
    best, best_n = None, 0
    for name, pat in _TXT_STRATEGIES:
        n = _txt_scan(lines, pat)
        if n > best_n:
            best, best_n = (name, pat), n
    if best is None or best_n < 2:
        return [], [f"认不出排版：没有任何一种已知格式命中 ≥2 行（最相似的是 "
                    f"{best[0] if best else '无'}，命中 {best_n} 行）"], "无"
    sname, pat = best

    msgs: list[RawMsg] = []
    cur_date = default_date
    cur: RawMsg | None = None
    date_only = 0
    for raw_ln in lines:
        ln = raw_ln.rstrip()
        if not ln.strip():
            continue
        dm = _DATE_LINE.match(ln)
        if dm and not pat.match(ln):
            d = parse_date(dm.group("d"))
            if d:
                cur_date, date_only = d, date_only + 1
                cur = None
                continue
        m = pat.match(ln)
        if not m:
            if cur is not None:  # 续行：属于上一条气泡（微信消息本来就允许多行）
                cur.text += ("\n" if cur.text else "") + ln.strip()
                if cur.type_ == "text":
                    cur.type_ = classify_placeholder(cur.text)
            else:
                warn.append(f"文件开头有无法归属的行，已忽略：{ln.strip()[:30]!r}")
            continue
        g = m.groupdict()
        d = parse_date(g.get("date") or "") if g.get("date") else cur_date
        t = parse_time(g.get("time") or "") if g.get("time") else None
        if g.get("date"):
            cur_date = d
        content = _pick_group(g, "content").strip()
        cur = RawMsg(ts=_combine(d, t), sender=_pick_group(g, "name").strip() or UNKNOWN_LABEL,
                     text=content, type_=classify_placeholder(content))
        msgs.append(cur)

    if date_only:
        warn.append(f"识别到 {date_only} 个「只有日期」的分隔行，其后的行按该日期补全")
    if msgs and all(m.ts is None for m in msgs) and default_date is None:
        warn.append("文件中没有任何日期信息（只有时刻或什么都没有），"
                    "需要上层提供参考日期才能算出间隔")
    if known_labels is not None:
        known_labels.update(m.sender for m in msgs)
    return msgs, warn, sname


# ---------------------------------------------------------------- html


_BLOCK_TAGS = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "table", "section", "article"}
IMG_PLACEHOLDER = "[图片]"


class _HTMLCollector(HTMLParser):
    """极简收集器：既要 <table> 的行列，也要剥标签后的文本流（回退用）。

    不引 bs4/lxml —— 默认构建要控体积（§10），标准库足够。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []   # 每个 <tr> 的单元格文本
        self.lines: list[str] = []        # 剥标签后的行（<table> 路线失败时的回退输入）
        self._row: list[str] | None = None  # 当前行的单元格文本
        self._in_cell = False
        self._buf: list[str] = []         # 当前单元格 / 当前行的文字碎片

    # -- 工具 --
    def _flush_line(self) -> None:
        s = "".join(self._buf).strip()
        if s:
            self.lines.append(s)
        self._buf = []

    def _flush_cell(self) -> None:
        """单元格收尾：挂到当前行（表格路线），同时进 lines（纯文本回退路线）。"""
        if not self._in_cell:
            return
        text = "".join(self._buf).strip()
        if self._row is not None:
            self._row.append(text)
        if text:
            self.lines.append(text)  # 保证 <br> 换行的内容一字不丢
        self._buf = []
        self._in_cell = False

    def _flush_row(self) -> None:
        self._flush_cell()
        if self._row is not None:
            self.rows.append(self._row)
        self._row = None

    # -- 回调 --
    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._flush_row()
            self._row = []
        elif tag in ("td", "th"):
            self._flush_cell()
            self._in_cell = True
        elif tag == "img":
            alt = dict(attrs).get("alt") or ""
            self._buf.append(f"[{alt}]" if alt else IMG_PLACEHOLDER)
        elif tag == "br":
            if self._in_cell:
                self._buf.append("\n")  # 单元格内换行保留原样，别丢字
            else:
                self._flush_line()
        elif tag in _BLOCK_TAGS and not self._in_cell:
            self._flush_line()

    def handle_endtag(self, tag):
        if tag == "tr":
            self._flush_row()
        elif tag in ("td", "th"):
            self._flush_cell()
        elif tag in _BLOCK_TAGS and tag != "br" and not self._in_cell:
            self._flush_line()

    def handle_data(self, data):
        if data.strip():
            self._buf.append(data)

    def close(self):  # noqa: A003 - 与 HTMLParser 对齐
        super().close()
        self._flush_line()
        self._flush_row()


def _cell_stamp(cells: list[str]) -> int:
    """返回「哪一格是时间戳」的下标，找不到 -1。"""
    for i, c in enumerate(cells[:3]):
        c = (c or "").strip()
        if not c:
            continue
        if re.search(r"\d{1,2}:\d{2}", c) or parse_date(c):
            return i
    return -1


def parse_html(text: str, default_date: date | None = None,
               known_labels: set[str] | None = None) -> tuple[list[RawMsg], list[str], str]:
    warn: list[str] = []
    col = _HTMLCollector()
    try:
        col.feed(text)
        col.close()
    except Exception as e:  # HTMLParser 极少抛，但截断的页面会
        warn.append(f"HTML 解析中途出错（{type(e).__name__}: {e}），已用已读到的部分继续")

    # 1) 表格路线：时间 / 昵称 / 内容 三列（最常见的导出形态）
    body = [r for r in col.rows if any((c or "").strip() for c in r)]
    if len(body) >= 2:
        good = [r for r in body if _cell_stamp(r) >= 0 and len(r) >= 2]
        if len(good) >= max(2, int(len(body) * 0.6)):
            msgs: list[RawMsg] = []
            cur_date = default_date
            for r in body:
                i = _cell_stamp(r)
                if i < 0:
                    continue
                stamp = r[i].strip()
                sd = parse_date(stamp)
                st = parse_time(stamp)
                m2 = re.search(r"(\d{1,2}:\d{2}(?::\d{2})?)", stamp)
                if st is None and m2:
                    st = parse_time(m2.group(1))
                if sd is None:
                    m2d = re.search(r"(\d{4}[-/.]\d{1,2}[-/.]\d{1,2})", stamp)
                    sd = parse_date(m2d.group(1)) if m2d else cur_date
                else:
                    cur_date = sd
                sender = (r[i + 1] if len(r) > i + 1 else "").strip() or UNKNOWN_LABEL
                content = " ".join(x.strip() for x in r[i + 2:] if x and x.strip()).strip()
                msgs.append(RawMsg(ts=_combine(sd, st), sender=sender, text=content,
                                   type_=classify_placeholder(content)))
            if known_labels is not None:
                known_labels.update(m.sender for m in msgs)
            warn.append(f"按 <table> 解析出 {len(msgs)} 行（时间列下标={_cell_stamp(good[0])}）")
            return msgs, warn, "<table> 时间/昵称/内容"

    # 2) 回退：剥标签成文本流，交给 txt 策略链
    plain = "\n".join(col.lines)
    msgs, w, sname = parse_txt(plain, default_date, known_labels)
    warn += w
    warn.append(f"<table> 结构不成立，已剥标签后按纯文本策略解析（命中「{sname}」）")
    return msgs, warn, f"text-fallback({sname})"


# ---------------------------------------------------------------- chatlab

_CLB_DAY = re.compile(r"^\s*[-—=*]{1,}\s*(?P<d>[\d]{4}[-/.年]\s*\d{1,2}[-/.月]\s*\d{1,2}\s*日?)"
                      r"\s*[-—=*]{1,}\s*$")
_CLB_MSG = re.compile(
    r"^\s*\[#(?P<id>\d+(?:-\d+)?)(?P<star>\*?)\]\s*"
    r"(?:(?P<time>\d{1,2}:\d{2}(?::\d{2})?)\s+)?"
    r"(?:(?P<name>[^:：\s]{1,32}?)\s*[:：]\s*)?(?P<content>.*)$")
_CLB_SENDER_KEYS = ("sender", "senderName", "from", "member", "memberName", "name",
                    "author", "talker", "nickname")
_CLB_TIME_KEYS = ("ts", "timestamp", "time", "datetime", "date", "sendTime", "createdAt")
_CLB_TEXT_KEYS = ("content", "text", "message", "body", "msg")


def parse_chatlab(obj: dict, default_date: date | None = None,
                  known_labels: set[str] | None = None) -> tuple[list[RawMsg], list[str], str, dict]:
    """解析 ChatLab CLI 的 JSON 信封。→ (消息, 警告, 用到的路线, 元信息)"""
    warn: list[str] = []
    meta_out: dict = {}
    if not isinstance(obj, dict):
        return [], ["ChatLab JSON 顶层不是对象"], "无", meta_out

    meta = obj.get("meta") or {}
    if isinstance(meta, dict):
        meta_out = {k: meta.get(k) for k in ("totalHits", "returnedHits", "hasMore",
                                             "timeRange", "apiVersion", "preprocess")
                    if k in meta}
        if meta.get("hasMore"):
            warn.append("上游 meta.hasMore=true：这批只是页码中的一页，不是完整会话，"
                        "分析前应翻页或用更窄的时间范围")

    if obj.get("ok") is False:
        err = obj.get("error") or {}
        if isinstance(err, dict):
            detail = "；".join(f"{k}={err[k]}" for k in ("code", "message", "hint") if err.get(k))
            cands = err.get("candidates")
            if cands:
                detail += f"；候选={cands}"
        else:
            detail = str(err)
        warn.append(f"ChatLab 返回失败信封 ok=false：{detail or '（无 error 细节）'}")
        return [], warn, "失败信封", meta_out

    data = obj.get("data")
    if isinstance(data, str):
        text = data
        data = {}
    elif isinstance(data, dict):
        text = data.get("text") or data.get("content") or ""
    else:
        return [], ["ChatLab JSON 里没有 data 字段（既不是对象也不是字符串）"], "无", meta_out

    # 路线 1：结构化 messages 数组（--format json）
    arr = None
    if isinstance(data, dict):
        for k in ("messages", "items", "rows", "list"):
            if isinstance(data.get(k), list):
                arr = data[k]
                break
    if arr:
        msgs: list[RawMsg] = []
        for it in arr:
            if not isinstance(it, dict):
                continue
            sender = next((str(it[k]).strip() for k in _CLB_SENDER_KEYS if it.get(k)), "")
            stamp = next((it[k] for k in _CLB_TIME_KEYS if it.get(k) not in (None, "")), None)
            content = next((str(it[k]) for k in _CLB_TEXT_KEYS if it.get(k) not in (None, "")), "")
            src_id = it.get("id") or it.get("messageId")
            msgs.append(RawMsg(ts=parse_stamp(stamp, default_date),
                               sender=sender or UNKNOWN_LABEL, text=content,
                               type_=classify_placeholder(content),
                               src_id=str(src_id) if src_id is not None else None))
        if known_labels is not None:
            known_labels.update(m.sender for m in msgs)
        warn.append(f"按结构化 data.messages 解析出 {len(msgs)} 条")
        return msgs, warn, "--format json（data.messages）", meta_out

    # 路线 2：data.text 紧凑正文（--format agent，文档里的推荐格式）
    if not text.strip():
        return [], ["ChatLab data 里既没有 messages 数组也没有 text 正文"], "无", meta_out

    msgs = []
    cur_date = default_date
    cur: RawMsg | None = None
    days = 0
    starred = 0
    for ln in text.replace("\r\n", "\n").split("\n"):
        if not ln.strip():
            continue
        if ln.lstrip().startswith("returned:") or ln.lstrip().startswith("total:"):
            continue
        dm = _CLB_DAY.match(ln)
        if dm:
            d = parse_date(dm.group("d"))
            if d:
                cur_date, days = d, days + 1
                cur = None
                continue
        m = _CLB_MSG.match(ln)
        if not m:
            if cur is not None:
                cur.text += ("\n" if cur.text else "") + ln.strip()
                if cur.type_ == "text":
                    cur.type_ = classify_placeholder(cur.text)
            else:
                warn.append(f"正文里有无法归属的行，已忽略：{ln.strip()[:30]!r}")
            continue
        g = m.groupdict()
        t = parse_time(g.get("time") or "") if g.get("time") else None
        content = (g.get("content") or "").strip()
        name = (g.get("name") or "").strip()
        cur = RawMsg(ts=_combine(cur_date, t), sender=name or UNKNOWN_LABEL, text=content,
                     type_=classify_placeholder(content),
                     src_id=g.get("id") + ("*" if g.get("star") else ""),
                     src_date=cur_date)
        if g.get("star"):
            starred += 1
        msgs.append(cur)
    if starred:
        warn.append(f"{starred} 条消息带星标 `[#id*]`：上游标注其正文被预处理过"
                    f"（截断/脱敏），引用时按原文核对")
    if days:
        warn.append(f"正文含 {days} 个日期分块（`--- 日期 ---`），已按分块补全每条的时间")
    if msgs and all(m.ts is None for m in msgs) and default_date is None:
        warn.append("正文里没有任何日期（只有时刻），需要上层提供参考日期")
    if known_labels is not None:
        known_labels.update(m.sender for m in msgs)
    return msgs, warn, "--format agent（data.text）", meta_out


# ---------------------------------------------------------------- 格式探测


def detect_format(path: Path, text: str) -> str:
    ext = path.suffix.lower()
    stripped = text.lstrip("\ufeff \t\r\n")
    if ext in (".json", ".jsonl") or stripped.startswith("{"):
        try:
            obj = json.loads(text)
            if isinstance(obj, dict) and any(k in obj for k in ("ok", "data", "command", "meta")):
                return "chatlab"
            if isinstance(obj, list) and obj and isinstance(obj[0], dict):
                return "chatlab"  # 裸消息数组也按 chatlab 结构化路线走
        except (json.JSONDecodeError, ValueError):
            pass
        return "unknown"
    if ext in (".html", ".htm") or re.search(r"<\s*(html|table|div|p|body)\b", stripped[:2048], re.I):
        return "html"
    if ext in (".txt", ".log", ".md", ".csv", ".text") or len(stripped) > 0:
        return "txt"
    return "unknown"


# ---------------------------------------------------------------- 说话人映射


def _label_kind(label: str) -> bool:
    return label.strip().lower() in ME_MARKERS


def build_speaker_map(me_label: str | None, labels: list[str], confirmed: bool) -> dict:
    """me=right / 对象=left，对象按出现顺序编 obj-1、obj-2…（§5.2 预留群聊扩展）。"""
    others = [x for x in labels if x != me_label]
    return {
        "me": {"side": "right", "code": "me", "label": me_label or "", "confirmed": confirmed},
        "objects": [{"code": f"obj-{i}", "side": "left", "label": lb, "source_label": lb}
                    for i, lb in enumerate(others, 1)],
    }


def _candidates(msgs: list[RawMsg]) -> list[dict]:
    from collections import Counter

    cnt = Counter(m.sender for m in msgs)
    return [{"label": lb, "count": n, "is_me_marker": _label_kind(lb)}
            for lb, n in cnt.most_common()]


def _pick_auto_me(cands: list[dict]) -> str | None:
    """只有文件**显式**写了 me/我/本人 才自动认；其它情况一律返回 None 去问人。"""
    marked = [c["label"] for c in cands if c["is_me_marker"]]
    if len(marked) == 1 and len(cands) >= 2:
        return marked[0]
    return None


# ---------------------------------------------------------------- 主入口


def import_file(path: str | Path, me_label: str | None = None,
                default_date: date | None = None, max_messages: int = 2000,
                source_meta: dict | None = None) -> ImportResult:
    """导入一个导出文件。→ ImportResult。

    `me_label` 为 None 时不猜：能自动认出的只有文件里显式写了 me/我/本人的情况，
    否则 `ok=False, needs="me_label"`，并给 `candidates` 让 UI 问用户。
    """
    p = Path(path)
    if not p.exists():
        return ImportResult(path=str(p), fmt="unknown", ok=False, needs=None,
                            warnings=[f"文件不存在：{p}"])
    text, enc, warn = (read_html(p) if p.suffix.lower() in (".html", ".htm") else read_text(p))
    if enc not in ("utf-8", "utf-8-sig"):
        warn.append(f"文本编码按 {enc} 解码")
    fmt = detect_format(p, text)
    if fmt == "unknown":
        return ImportResult(path=str(p), fmt=fmt, ok=False,
                            warnings=warn + [f"认不出文件格式（后缀 {p.suffix!r}）："
                                             f"只支持 txt / html / ChatLab agent JSON，"
                                             f"不假装支持其它格式"])
    labels: set[str] = set()
    meta_out: dict = {}
    try:
        if fmt == "chatlab":
            obj = json.loads(text)
            if isinstance(obj, list):  # 裸数组：包成信封
                obj = {"ok": True, "data": {"messages": obj}}
            msgs, w, route, meta_out = parse_chatlab(obj, default_date, labels)
        elif fmt == "html":
            msgs, w, route = parse_html(text, default_date, labels)
        else:
            msgs, w, route = parse_txt(text, default_date, labels)
    except json.JSONDecodeError as e:
        return ImportResult(path=str(p), fmt=fmt, ok=False,
                            warnings=warn + [f"JSON 解析失败（第 {e.lineno} 行）：{e.msg}"])
    warn += w

    stats = {"raw_count": len(msgs), "encoding": enc, "route": route, "fmt": fmt}
    cands = _candidates(msgs)
    stats["senders"] = {c["label"]: c["count"] for c in cands}
    if not msgs:
        return ImportResult(path=str(p), fmt=fmt, ok=False, candidates=cands,
                            warnings=warn + ["没有解析出任何消息"], stats=stats)

    auto = _pick_auto_me(cands)
    chosen = me_label or auto
    if auto and not me_label:
        warn.append(f"文件里显式把 {auto!r} 标为数据所有者（me），据此认定「我」，未再追问")
    if chosen is None:
        # 只问一个最小问题：哪个昵称是你
        return ImportResult(path=str(p), fmt=fmt, ok=False, needs="me_label",
                            candidates=cands, me_label=None,
                            warnings=warn + [f"解析出 {len(msgs)} 条，涉及 {len(cands)} 个昵称；"
                                             f"「我」是哪一个需要你确认（不猜）"],
                            stats=stats)
    if chosen not in {c["label"] for c in cands}:
        return ImportResult(path=str(p), fmt=fmt, ok=False, needs="me_label", candidates=cands,
                            warnings=warn + [f"指定的「我」={chosen!r} 不在文件里的昵称中："
                                             f"{[c['label'] for c in cands]}"], stats=stats)

    # -- 时间补齐与排序 --
    # 无时间的行按「承上」处理：继承上一条已知时间，顺序天然保持单调，
    # 绝不能统一塞到文件末尾（那会让 schema 的「时间倒流」校验当场拦下）。
    dated = [m for m in msgs if m.ts is not None]
    undated = [m for m in msgs if m.ts is None]
    if undated:
        warn.append(f"{len(undated)} 条消息完全没有时间信息，按「承上」继承上一条已知时间"
                    f"（这些条的间隔不可信）")
        last = dated[0].ts if dated else datetime.now().astimezone()
        for m in msgs:
            if m.ts is None:
                m.ts = last
            else:
                last = m.ts
    out_of_order = any(msgs[i].ts < msgs[i - 1].ts for i in range(1, len(msgs)))  # type: ignore[operator]
    if out_of_order:
        msgs.sort(key=lambda m: m.ts)  # type: ignore[arg-type,return-value]
        warn.append("文件里消息不是按时间升序，已重排（原顺序被调整，引用编号以本文件为准）")

    sm = build_speaker_map(chosen, [c["label"] for c in cands], confirmed=True)
    win = {"chat_title": (source_meta or {}).get("chat_title", ""),
           "origin": "import", "file": p.name, "format": fmt}
    b = TranscriptBuilder(sm, source="import", window=win, dup_threshold=0.0,
                          max_messages=max_messages)
    b.warnings += warn
    for m in msgs:
        msg = b.add("me" if m.sender == chosen else _code_of(sm, m.sender), m.text,
                    type_=m.type_, at=m.ts, ts_source="import")
        if msg is not None:
            msg.src_id = m.src_id  # 保留上游证据编号（ChatLab 契约要能引用 [#id]）
    if len(msgs) > max_messages:
        warn.append(f"消息超过上限 {max_messages} 条，只保留最后 {max_messages} 条")
    b.warnings.append(NOMINAL_WARN)
    t = b.build()
    stats["messages"] = len(t.messages)
    stats["max_messages"] = max_messages
    stats["date_range"] = [t.messages[0].ts, t.messages[-1].ts]
    return ImportResult(path=str(p), fmt=fmt, ok=True, me_label=chosen, candidates=cands,
                        transcript=t, warnings=t.warnings, stats=stats)


def _code_of(sm: dict, label: str) -> str:
    for o in sm.get("objects", []):
        if o.get("label") == label or o.get("source_label") == label:
            return o["code"]
    return "obj-1"


def load_default_date_hint(path: str | Path) -> date | None:
    """没有日期信息时的兜底参考日：文件修改时间。调用方应把这件事写进 warnings。"""
    try:
        return datetime.fromtimestamp(Path(path).stat().st_mtime).date()
    except OSError:
        return None


__all__ = ["ImportResult", "RawMsg", "import_file", "detect_format", "parse_txt",
           "parse_html", "parse_chatlab", "build_speaker_map", "read_text",
           "load_default_date_hint", "classify_placeholder", "parse_stamp"]
