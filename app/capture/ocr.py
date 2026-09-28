# -*- coding: utf-8 -*-
"""消息区截图 → 谁说了什么。RapidOCR 吃 numpy，全程内存，不落盘。

来源：jev-chat-src/app/ocr.py（MIT, Copyright (c) 2026 rezoch340），见 THIRD_PARTY_NOTICES.md。
**算法一行未改**，仅做三件事：
1. 引擎加锁，供多线程（采集 worker）安全共用；
2. `Reader` 增加 `grays` 采集：面板底色上的灰字（微信的时间戳分隔行）在 transcript 层
   是**唯一可信的时间来源**，丢了就只能拿采集时刻凑，间隔会失真；
3. 注释订正 §0.1 E5：说话人**按气泡底色判**（预览绿底 = 我），不是按 x 坐标。

`who_said` 的判据（实测 M-1）：
- 框内众数颜色占比 < 45% → 图片里的字（头像/照片/表情包），丢掉；
- 绿底（G 明显高于 R、B）→ me，实测底色 (53,210,141)；
- 非绿且文字对比度 ≥ 150 → her，实测底色 (47,47,48) 深色主题 / (255,255,255) 浅色主题；
- 其余灰字（时间戳、引用块、群发言人名、系统提示）→ gray。
"""
from __future__ import annotations

import difflib
import re
import threading
from dataclasses import dataclass

import numpy as np

_ENGINE = None
_ENGINE_LOCK = threading.Lock()

# 微信的时间戳分隔行：「14:01」「昨天 14:01」「9月21日 14:01」「星期一 14:01」
RE_TIME_MARK = re.compile(
    r"^(?:(?P<mon>\d{1,2})月(?P<day>\d{1,2})日\s*)?"
    r"(?:(?P<rel>昨天|前天|星期[一二三四五六日天])\s*)?"
    r"(?P<hh>\d{1,2}):(?P<mm>\d{2})$"
)


def _engine():
    """OCR 引擎全进程共用：一个实例 ~40MB，每个会话一个 Reader，不能各带一个。
    det_limit_type 默认 'min' 会把小图放大到短边 736，裁小反而更慢；必须 'max'。"""
    global _ENGINE
    with _ENGINE_LOCK:
        if _ENGINE is None:
            from rapidocr_onnxruntime import RapidOCR

            _ENGINE = RapidOCR(intra_op_num_threads=4, det_limit_type="max", det_limit_side_len=4000)
        return _ENGINE


def probe_engine(image):
    """诊断入口：直接跑一次引擎，返回引擎原始结果 `(res, elapse)`。

    给打包冒烟测试用（`app/main.py --ocr-check` → `scripts/build.ps1` 第 7 步）：
    只有真跑一次才能证明 onnx 模型、onnxruntime 与 cv2 这一整条链路在**打包产物里**是活的
    —— `import rapidocr_onnxruntime` 成功并不代表模型文件也打进去了（E38）。
    """
    return _engine()(image, use_cls=False)


def read_title(header):
    """面板头部那一条截图 → 会话名（numpy RGB）。取最靠上的一行，同一行里取最左的
    （右边是图标按钮，OCR 不出字；下面那行是公告）。群聊的成员数「(422)」去掉，只留名字当 key。
    认不出返回 ""。一次约 60ms，所以调用方只在头部像素变了时才问。"""
    res, _ = _engine()(header, use_cls=False)
    if not res:
        return ""
    first = min(res, key=lambda r: r[0][0][1])
    row = first[0][0][1] + (first[0][2][1] - first[0][0][1])  # 框底：顶在这之上的算同一行
    text = min((r for r in res if r[0][0][1] < row), key=lambda r: r[0][0][0])[1]
    return re.sub(r"\s*[（(]\d+[)）]\s*$", "", text.strip())


def who_said(chat, box):
    """按 OCR 框里的颜色分类，**不看 x 坐标**（§0.1 E5 订正；实测它与 x 位置 100% 自洽，
    但它是主判据、x 只是佐证）。返回 (谁, 底色, 墨高)：
    先看底色平不平：框里众数颜色占比 <45% 就是图片（头像/照片/表情包）里的字 → None 丢掉。
    绿底 → me；非绿且文字对底色对比度 ≥150 → her；其余（引用块、群里的发言人名、时间戳、系统提示、
    链接卡片描述——都是灰字，对比度 80~95）→ "gray"。
    实测：气泡正文对比度 178~208，me 绿泡 142~150，灰字 ≤ 93。深浅主题都靠这套。
    墨高 = 框里最长一段连续有字的行数（OCR 框对小字有固定 padding、还会蹭到上下行，不能拿框高比大小）。"""
    xs, ys = [p[0] for p in box], [p[1] for p in box]
    reg = chat[int(min(ys)):int(max(ys)), int(min(xs)):int(max(xs))].astype(int)
    if reg.size == 0:
        return None, None, 0
    vals, cnt = np.unique(reg.reshape(-1, 3), axis=0, return_counts=True)
    bg = vals[cnt.argmax()]
    if cnt.max() / reg.shape[0] / reg.shape[1] < 0.45:
        # 文字必须落在平底色上：WGC 帧是精确像素，气泡/面板里众数颜色占 0.56~0.82，
        # 头像/照片/表情包里只有 0.1~0.3——那是图片里的字（头像上的「借仲夏夜之梦」之类），不是消息。
        # ponytail: 只对精确像素的帧成立；缩放/压缩过的截图（比如拿预览窗再截一次的图）底色会糊成几百种颜色，全会被当图片。
        return None, bg, 0
    diff = np.abs(reg @ [0.299, 0.587, 0.114] - bg @ [0.299, 0.587, 0.114])
    ink_h = best = 0
    for r in (diff > 60).any(axis=1):
        best = best + 1 if r else 0
        ink_h = max(ink_h, best)
    if bg[1] > bg[0] + 40 and bg[1] > bg[2] + 40:
        return "me", bg, ink_h
    return ("her" if diff.max() >= 150 else "gray"), bg, ink_h


def similar(a, b):
    """同一段像素挪个位置 OCR 会抖（「傻逼了」↔「傻逼」、「不好意思」↔「不好竟思」），按相似度判同一条。"""
    if a == b or difflib.SequenceMatcher(None, a, b).ratio() >= 0.75:
        return True
    return len(a) == len(b) >= 3 and sum(x != y for x, y in zip(a, b)) <= 1  # 短句错一个字


@dataclass
class Line:
    """一帧里的一条气泡文本（同一气泡的多行已合并）。

    上游用裸元组 (who, name, text, top, bottom, score)；这里改成 dataclass，
    因为 transcript 层还要 `x_rel` 去反推左右侧（speaker_mapper.suggest），
    元组加到 7 项会变成 positional 灾难。字段语义与上游逐一对齐。
    """

    who: str  # "me" | "her"
    name: str | None  # 群聊发言人，单聊 None
    text: str
    y_top: float
    y_bot: float
    score: float
    x_rel: float  # 文本中心 x / 聊天区宽度（0~1）

    def as_tuple(self):
        return (self.who, self.name, self.text, self.y_top, self.y_bot, self.score)


class Reader:
    """一个会话一个 Reader：lh/seen 各自算各自的，切走再切回来不会把旧消息当新的重报一遍。"""

    def __init__(self):
        self.ocr = _engine()
        self.lh = None  # 正常气泡字高，头一帧定
        self.seen = []  # [(who, name, text)]，累计，封顶 500
        self.grays = []  # 本帧面板底色上的灰字 [(text, y_top)]，可能是时间戳；每帧覆盖

    def read(self, chat, pane_bg, ocr_min_score: float = 0.0) -> list["Line"]:
        """→ [Line]（同一气泡的多行已合并）。who ∈ me/her；name 群聊里是发言人，单聊 None。
        ocr_min_score > 0 时丢掉低置信行（阈值由 M-1 标定为 0.90，见 §0.1 E7）。"""
        res, _ = self.ocr(chat, use_cls=False)
        W = chat.shape[1]
        # 群聊：每条 her 气泡上方一行灰色发言人名（靠左、短、不带冒号、印在面板底色上），从上往下扫，名字带给后面的气泡。
        # 引用块/时间戳/公告带冒号，链接卡片灰字印在气泡底色上，都不会被当成名字。
        # ponytail: 名字行被 OCR 漏掉时会挂到上一个人头上。
        name, raw, grays = None, [], []
        for box, text, score in sorted(res or [], key=lambda r: r[0][0][1]):
            if score is not None and score < ocr_min_score:
                continue
            kind, bg, h = who_said(chat, box)
            if kind == "gray":
                on_pane = bg is not None and np.abs(bg - pane_bg).sum() <= 6
                if on_pane:
                    grays.append((text, int(box[0][1])))
                if on_pane and box[0][0] < 0.25 * W and len(text) <= 16 and not re.search("[:：]", text):
                    name = text
                continue
            if kind is None or (self.lh and h < 0.6 * self.lh):
                continue  # 字比正常气泡小得多 = 图片消息（截图/表情包）里的字，不是气泡
            x_rel = float(((box[0][0] + box[2][0]) / 2.0) / max(1, W))
            raw.append((kind, name if kind == "her" else None, text,
                        box[0][1], box[2][1], float(score or 0.0), x_rel))
        self.grays = grays
        if not self.lh and len(raw) >= 3:
            self.lh = float(np.median([r[5] for r in raw]))
        # 同一气泡的多行合并：同人、上一行底到这一行顶的间距不到半个字高（不同气泡之间至少隔一个字高）
        lines: list[list] = []
        for who, nm, text, top, bottom, score, x_rel in raw:
            if lines and lines[-1][0] == who and lines[-1][1] == nm and top - lines[-1][4] < 0.6 * (self.lh or 1):
                lines[-1][2] += text
                lines[-1][4] = bottom
                lines[-1][6] = (lines[-1][6] + x_rel) / 2.0  # 多行取均值
            else:
                lines.append([who, nm, text, top, bottom, score, x_rel])
        return [Line(*row) for row in lines]

    def new_lines(self, lines: list["Line"]) -> list["Line"]:
        """去重（滚动不重复）→ 这一帧里真正新出现的行。
        本帧有已知行时只要已知行下方的：往上滚翻出来的旧消息在已知行上方，不算。
        本帧一行已知的都没有（大图把旧文字全顶出去了、切了聊天、滚远了）：全算，宁可多算不能漏。
        ponytail: 同一人连发两句一模一样的会吞一句——对触发分析无害。"""
        known_y = [l.y_top for l in lines if self._seen(l.who, l.name, l.text)]
        floor = max(known_y) if known_y else -1
        new = [l for l in lines if l.y_top > floor and not self._seen(l.who, l.name, l.text)]
        self.seen.extend((l.who, l.name, l.text) for l in lines if not self._seen(l.who, l.name, l.text))
        del self.seen[:-500]
        return new

    def _seen(self, who, name, text):
        # 名字不参与判重：名字行滚出画面后同一条消息会从 her(LO) 变成 her，不能算新消息
        return any(w == who and similar(t, text) for w, _, t in self.seen)

    def reset_seen(self):
        """切会话/换窗口时清空，避免拿上一个会话的消息判重。"""
        self.seen.clear()
        self.lh = None
