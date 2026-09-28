# -*- coding: utf-8 -*-
"""从一次分析结果里提取**候选记忆**（README.optimized §5.4「建档」）。

## 硬规则：什么能自动存，什么不能

官方 `validate_delta` 卡死了 scope × source_type 的交叉关系（实测原文）：

| scope | 允许的 source_type |
| --- | --- |
| `user` | 只有 `user_explicit` —— 「用户稳定档案只接受用户明确陈述」 |
| `object` / `relationship` | `user_explicit` / `user_report` |
| `event` / `hypothesis` | 都可以，但 `assistant_inference` **只能**进 `hypothesis` |

所以自动提取**只产出两类**：

- `event`（source_type=`chatlab`）：模型在「已知事实」里指名的、带 `[#id]` 证据的客观发生；
- `hypothesis`（source_type=`assistant_inference`）：模型的推断，永远带置信度与「模型推断」标签。

**模型推断绝不写进 `object`/`relationship`**——那不是「对方的事实」，是模型的说法。
真正的档案（用户自己的情况、对象快照、关系状态）只能由用户手动填写（`manual_candidate`），
因为只有用户能说「这是我知道的」而不是「这是模型猜的」。

## 为什么要在这里做写入前预检

官方脚本当然会拦，但拦截发生在**子进程里**，用户看到的只有一句错误码。
这里用官方模块的 `validate_delta` 先过一遍（`store.check_delta`），
把不合法的候选**在复核窗里就标出来并说清原因**，用户能当场改，
而不是点「保存」之后拿到一串红字。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.analysis import parse_output as po
from app.memory import store as store_mod

LEADING_CITES = re.compile(r"^\s*((?:\[#\d+\]\s*)+)")
ANY_CITE = re.compile(r"\[#(\d+)\]")
MD = re.compile(r"[*_`]+")
CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

MAX_HYPOTHESES = 5           # 与官方 SCOPE_LIMITS['hypothesis'] 一致
DEFAULT_MAX_EVENTS = 6
FIELD_CHARS = 18


@dataclass
class Candidate:
    """一条待写入的记忆。`checked` 只是 UI 上的勾选初值，不代表用户已同意。"""

    scope: str
    subject_id: str           # 归属：user 档案固定是 "user"，其余是对象编号
    field: str
    value: str
    source_type: str
    confidence: str = "low"
    source_ref: str = ""
    occurred_at: str = ""
    reason: str = ""          # 为什么建议存这条（给用户看的原话）
    checked: bool = True
    blocked: str = ""         # 非空 = 官方规则不接受，不能写

    def to_delta(self) -> dict:
        return {
            "scope": self.scope, "subject_id": self.subject_id, "field": self.field,
            "value": self.value, "source_type": self.source_type,
            "confidence": self.confidence, "source_ref": self.source_ref,
            "occurred_at": self.occurred_at,
        }

    @property
    def label(self) -> str:
        tag = store_mod.SCOPE_CN.get(self.scope, self.scope)
        return f"{tag}｜{self.field}"

    @property
    def meta(self) -> str:
        bits = [store_mod.SRC_CN.get(self.source_type, self.source_type)]
        conf = store_mod.CONF_CN.get(self.confidence, self.confidence)
        if conf:
            bits.append(f"置信度 {conf}")
        if self.source_ref:
            bits.append(self.source_ref)
        return "·".join(bits)


# ------------------------------ 文本处理 ------------------------------
def clean(text: str) -> str:
    """去掉 markdown 记号与控制字符 —— 记忆是要被反复注入 prompt 的，别把记号一起存进去。"""
    if not isinstance(text, str):
        return ""
    text = CTRL.sub(" ", text)
    text = MD.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def split_cites(text: str) -> tuple[str, list[int]]:
    """剥掉开头的**全部** `[#id]`，返回 (正文, id 列表)。

    模型经常写 `[#2] [#3] 对方主动安排见面…`（一条事实挂多个证据）。
    只剥第一个的话，`[#3]` 会留在正文里，进而被当成 field 标签——实测踩过这个坑。
    """
    m = LEADING_CITES.match(text or "")
    if not m:
        return (text or "").strip(), []
    ids = [int(x) for x in ANY_CITE.findall(m.group(1))]
    return text[m.end():].strip(), ids


def short_label(text: str, n: int = FIELD_CHARS) -> str:
    """从一句话里取一个短标签当 field。取不到就在标点处断。"""
    body, _ = split_cites(text)          # 编号不属于标签
    t = clean(body).strip("　 、，。；：!?！？[]")
    if len(t) <= n:
        return t or "未命名"
    cut = t[:n]
    for sep in ("，", "。", "；", "、", "：", " "):
        idx = cut.rfind(sep)
        if idx >= 4:
            return cut[:idx]
    return cut


def fit_value(text: str, max_chars: int) -> tuple[str, bool]:
    """裁到官方长度上限。返回 (值, 是否被裁)。被裁时留省略号，不假装完整。"""
    t = clean(text)
    if len(t) <= max_chars:
        return t, False
    keep = max(1, max_chars - 1)
    return t[:keep] + "…", True


# ------------------------------ 提取 ------------------------------
def _steps_node(run) -> dict:
    data = getattr(run, "analysis", None)
    data = getattr(data, "data", None) or {}
    node = data.get("steps") if isinstance(data, dict) else None
    return node if isinstance(node, dict) else {}


def _ts_of(transcript, msg_id: int) -> str:
    """取某条消息的时间戳，用于事件去重（官方事件按 subject+field+occurred_at 去重）。"""
    if transcript is None:
        return ""
    for m in getattr(transcript, "messages", []) or []:
        if getattr(m, "id", None) == msg_id:
            return clean(str(getattr(m, "ts", "") or ""))
    return ""


def from_analysis(run, *, subject_id: str, transcript=None, rules=None,
                  max_hypotheses: int = MAX_HYPOTHESES,
                  max_events: int = DEFAULT_MAX_EVENTS) -> list[Candidate]:
    """从一次 `AnalysisRun` 提取候选记忆。分析失败/降级时返回空列表（没东西可存）。"""
    analysis = getattr(run, "analysis", None)
    if analysis is None or not getattr(analysis, "ok", False):
        return []
    steps = _steps_node(run)
    facts = steps.get("facts") if isinstance(steps.get("facts"), dict) else {}

    conf = str(getattr(analysis, "confidence", "") or "low")
    if conf not in ("high", "medium", "low"):
        conf = "low"
    max_chars = rules.max_value_chars if rules is not None and rules.available else \
        store_mod.FALLBACK_MAX_VALUE_CHARS

    out: list[Candidate] = []

    # ---- 事件：带证据编号的已知事实 ----
    known = facts.get("known") if isinstance(facts.get("known"), list) else []
    seen_fields: set[str] = set()
    for raw in known[: max(0, max_events) * 2]:
        if not isinstance(raw, str):
            continue
        body, ids = split_cites(raw)
        value, cut = fit_value(body, max_chars)
        if not value:
            continue
        label = short_label(value)
        if label in seen_fields:      # 同一次分析里标签撞了就别写两条，否则会互相覆盖
            continue
        seen_fields.add(label)
        msg_id = ids[0] if ids else 0
        cand = Candidate(
            scope="event", subject_id=subject_id, field=label, value=value,
            source_type="chatlab", confidence="high",
            source_ref=" ".join(f"#{i}" for i in ids),
            occurred_at=_ts_of(transcript, msg_id),
            reason=("这条在记录里有明确出处"
                    + (f"（消息 {'、'.join(f'#{i}' for i in ids)}）" if ids else "")
                    + "，属于「发生过什么」，可以当事实留下来。"
                    + ("（原句偏长，已按上限截断）" if cut else "")),
        )
        out.append(cand)
        if len([c for c in out if c.scope == "event"]) >= max_events:
            break

    # ---- 假设：模型的推断 ----
    inferred = facts.get("inferred") if isinstance(facts.get("inferred"), list) else []
    for raw in inferred[: max(0, max_hypotheses) * 2]:
        if not isinstance(raw, str):
            continue
        value, cut = fit_value(raw, max_chars)
        if not value:
            continue
        out.append(Candidate(
            scope="hypothesis", subject_id=subject_id, field=short_label(value), value=value,
            source_type="assistant_inference", confidence=conf,
            source_ref="", occurred_at="",
            reason=("这是**模型的推断**，不是事实。存下来只会当「假设」用，"
                    "每次召回都会标着「模型推断」和置信度。"
                    + ("（原句偏长，已按上限截断）" if cut else "")),
        ))
        if len([c for c in out if c.scope == "hypothesis"]) >= max_hypotheses:
            break

    return mark_blocked(out, rules=rules)


def manual_candidate(scope: str, field: str, value: str, *, subject_id: str,
                     source_ref: str = "", notes: str = "", rules=None) -> Candidate:
    """用户自己填的一条。

    `user_explicit` 是唯一对**所有** scope 都合法的 source_type（官方交叉规则里它没有限制），
    所以手动填写一律用它 —— 用户说的就是「用户明确陈述」。
    """
    max_chars = rules.max_value_chars if rules is not None and rules.available else \
        store_mod.FALLBACK_MAX_VALUE_CHARS
    val, cut = fit_value(value, max_chars)
    cand = Candidate(
        scope=scope, subject_id=subject_id, field=short_label(field, 64), value=val,
        source_type="user_explicit", confidence="high", source_ref=source_ref,
        occurred_at="", reason=(notes or "你手动填写的，按「用户明确陈述」存。")
                    + ("（内容偏长，已按上限截断）" if cut else ""),
    )
    return mark_blocked([cand], rules=rules)[0]


def mark_blocked(cands: list[Candidate], *, rules=None) -> list[Candidate]:
    """用官方 `validate_delta` 预检每一条。不合法的标 `blocked` 并说清原因。"""
    if rules is None or not rules.available:
        return cands
    for c in cands:
        if c.blocked:
            continue
        if not c.value.strip():
            c.blocked = "内容为空，官方不接受"
            continue
        reason = rules.check_delta(c.to_delta())
        if reason:
            c.blocked = reason
    return cands


def writable(cands: list[Candidate]) -> list[Candidate]:
    return [c for c in cands if not c.blocked]


__all__ = ["Candidate", "DEFAULT_MAX_EVENTS", "MAX_HYPOTHESES", "clean", "fit_value",
           "from_analysis", "manual_candidate", "mark_blocked", "short_label",
           "split_cites", "writable"]
