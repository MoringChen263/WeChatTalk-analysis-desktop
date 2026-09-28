# -*- coding: utf-8 -*-
"""parse_output：把模型输出变成可渲染的 `Analysis`（README.optimized §7.2）。

处理顺序（逐级放宽，每一步都留痕，UI 要如实标注当前处在哪一级）：
1. 直接 `json.loads` → `mode="json"`；
2. 去围栏 / 取第一个括号平衡的对象 / 修尾逗号 → `mode="repaired"`；
3. 解析成功但**硬字段缺失** → `mode="partial"`（能渲染多少渲染多少）；
4. 完全解析不了 → `mode="degraded"`，原样文本按纯文本渲染，绝不假装结构化成功。

另外：模型偶尔会编造 `[#99]` 这种记录里不存在的编号，这里统一过滤成 `[#?]` 并记进
`citations_dropped`——宁可显示一个问号，也不能让用户以为真有第 99 条。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from app.analysis import schema as sc
from app.analysis.prompt_builder import filter_citations

MODE_JSON = "json"
MODE_REPAIRED = "repaired"
MODE_PARTIAL = "partial"
MODE_DEGRADED = "degraded"

MODE_LABEL = {
    MODE_JSON: "结构化输出",
    MODE_REPAIRED: "结构化输出（已修复格式）",
    MODE_PARTIAL: "部分结构化（有字段缺失）",
    MODE_DEGRADED: "纯文本降级（JSON 解析失败）",
}

_TRAILING_COMMA = re.compile(r",\s*([}\]])")
_ZERO_WIDTH = re.compile(r"[\u200b-\u200f\ufeff]")
_MISSING = "（模型未给出）"


@dataclass
class Analysis:
    data: dict = field(default_factory=dict)
    raw: str = ""
    mode: str = MODE_JSON
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    citations_dropped: list[str] = field(default_factory=list)

    # --------------------------- 取字段 ---------------------------
    @property
    def steps(self) -> dict:
        v = self.data.get("steps")
        return v if isinstance(v, dict) else {}

    @property
    def scripts(self) -> dict:
        v = self.data.get("scripts")
        return v if isinstance(v, dict) else {}

    @property
    def primary(self) -> dict:
        v = self.scripts.get("primary")
        return v if isinstance(v, dict) else {}

    @property
    def variants(self) -> dict:
        v = self.scripts.get("variants")
        return v if isinstance(v, dict) else {}

    def primary_text(self) -> str:
        t = self.primary.get("text")
        return t.strip() if isinstance(t, str) else ""

    def variant_text(self, key: str) -> str:
        t = self.variants.get(key)
        return t.strip() if isinstance(t, str) else ""

    def script_text(self, tab_index: int) -> str:
        """话术卡第 tab_index 页的可复制正文（0=首选，其余对应 variants）。"""
        if tab_index <= 0:
            return self.primary_text()
        keys = sc.VARIANT_KEYS
        return self.variant_text(keys[tab_index - 1]) if tab_index - 1 < len(keys) else ""

    @property
    def confidence(self) -> str:
        v = self.data.get("confidence")
        return v if v in sc.CONFIDENCE else ""

    @property
    def boundaries(self) -> list[str]:
        return [x for x in (self.data.get("boundaries") or []) if isinstance(x, str)]

    @property
    def citations(self) -> list[str]:
        return [x for x in (self.data.get("citations") or []) if isinstance(x, str)]

    @property
    def ok(self) -> bool:
        """够不够渲染出「五步 + 一张能发的话术卡」。"""
        return bool(self.primary_text())

    @property
    def well_formed(self) -> bool:
        """顶层是不是**同时**具备 steps 与 scripts。

        契约要求两者平级。只看 `ok`（有没有一条能发的话术）是不够的：
        模型把 scripts 嵌进 steps 里（或反之）时，`ok` 仍会为真，
        但五步或话术卡会整块消失——渲染出 12 行「模型未给出」，
        用户还以为模型就是没写。这种「结构错位」值得多花一次调用让模型重做。
        """
        return (isinstance(self.data.get("steps"), dict)
                and isinstance(self.data.get("scripts"), dict))

    # --------------------------- 渲染 ---------------------------
    def _bullets(self, items, empty: str = _MISSING) -> str:
        vals = [x.strip() for x in (items or []) if isinstance(x, str) and x.strip()]
        return "\n".join(f"- {v}" for v in vals) if vals else f"- {empty}"

    def steps_markdown(self) -> list[str]:
        """五页，与 schema.STEP_TITLES 对齐。"""
        if self.mode == MODE_DEGRADED:
            head = (f"> ⚠️ **模型这次没给出合规 JSON**（{'; '.join(self.errors[:2]) or '格式不合规'}），"
                    f"下面是它的原始输出，未经结构化整理。\n\n")
            pages = [head + self.raw.strip()]
            pages += ["> ⚠️ 本次为纯文本降级，这一栏没有结构化内容。" for _ in range(4)]
            return pages

        s = self.steps
        facts = s.get("facts") if isinstance(s.get("facts"), dict) else {}
        advice = s.get("advice") if isinstance(s.get("advice"), dict) else {}
        actions = s.get("actions") if isinstance(s.get("actions"), dict) else {}

        known = self._bullets(facts.get("known"), "（没有给出带证据编号的事实）")
        inferred = self._bullets(facts.get("inferred"))
        unknown = self._bullets(facts.get("unknown"))
        reasons = self._bullets(advice.get("reasons"))

        pages = [
            _text(s.get("emotion")),
            f"**已知事实**\n{known}\n\n**合理推测**\n{inferred}\n\n**关键未知**\n{unknown}",
            _text(s.get("interests")),
            f"**首选建议**\n\n> {_text(advice.get('primary'))}\n\n**理由**\n{reasons}",
            "\n\n".join([
                f"**现在能做的一步**\n{_text(actions.get('next_action'))}",
                f"**观察窗口**\n{_text(actions.get('watch_window'))}",
                f"**停止条件**\n{_text(actions.get('stop_condition'))}",
                "**值得回来反馈的信号**\n" + self._bullets(actions.get("signals_to_report")),
            ]),
        ]
        return pages

    def scripts_markdown(self) -> list[str]:
        """四页，与 schema.SCRIPT_TABS 对齐：首选 / 稳健 / 会撩 / 强势。"""
        if self.mode == MODE_DEGRADED:
            return ["> ⚠️ 纯文本降级：话术卡没有结构化内容，请在上面的原文里找。"
                    for _ in range(len(sc.SCRIPT_TABS))]

        prim = self.primary
        branches = prim.get("branches") if isinstance(prim.get("branches"), dict) else {}
        pages = [self._primary_page()]
        for key in sc.VARIANT_KEYS:
            body = self.variant_text(key)
            if not body:
                pages.append(f"> {_MISSING}")
            elif key == "assertive":
                pages.append(f"> {body}\n\n---\n\n**注意**：强势版指的是边界与快速筛选，"
                             "不是羞辱、威胁或控制。")
            else:
                pages.append(f"> {body}")
        # 分支被渲染在「首选」页里，这里只是把 timing/cost 也带上，避免用户漏看
        _ = branches
        return pages

    def _primary_page(self) -> str:
        prim = self.primary
        branches = prim.get("branches") if isinstance(prim.get("branches"), dict) else {}
        text = self.primary_text() or _MISSING
        return "\n\n".join([
            "**可以直接复制发送**\n\n> " + text.replace("\n", "\n> "),
            f"**何时发**　{_text(prim.get('timing'))}",
            f"**主要代价**　{_text(prim.get('cost'))}",
            "**对方积极**\n" + _text(branches.get("positive")),
            "**对方含糊**\n" + _text(branches.get("vague")),
            "**对方不回应**\n" + _text(branches.get("no_reply")),
        ])

    def flat_text(self) -> str:
        """给剪贴板/导出的纯文本全量渲染。"""
        lines = [f"【{t}】\n{p}" for t, p in zip(sc.STEP_TITLES, self.steps_markdown())]
        lines.append("【话术卡】")
        for t, p in zip(sc.SCRIPT_TABS, self.scripts_markdown()):
            lines.append(f"— {t} —\n{p}")
        meta = []
        if self.confidence:
            meta.append(f"置信度：{self.confidence}")
        if self.citations:
            meta.append("引用：" + "、".join(self.citations))
        if self.boundaries:
            meta.append("边界：" + "、".join(self.boundaries))
        if meta:
            lines.append("— 元信息 —\n" + "\n".join(meta))
        return "\n\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "mode_label": MODE_LABEL.get(self.mode, self.mode),
            "errors": self.errors,
            "warnings": self.warnings,
            "citations_dropped": self.citations_dropped,
            "analysis": self.data,
            "raw": self.raw,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)


def _text(v) -> str:
    if isinstance(v, str) and v.strip():
        return v.strip()
    if isinstance(v, (int, float)):
        return str(v)
    return _MISSING


# ------------------------------ 提取与修复 ------------------------------
def strip_fences(text: str) -> str:
    t = _ZERO_WIDTH.sub("", text or "").strip()
    t = sc.FENCE.sub("", t)
    t = t.strip()
    if t.startswith("```"):  # 只包了开头没包结尾的情况
        t = t.split("\n", 1)[-1] if "\n" in t else t
    return t.strip()


def find_json_object(text: str) -> str | None:
    """扫出第一个括号平衡的 `{...}`，正确跳过字符串里的花括号与转义引号。"""
    start = text.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
        start = text.find("{", start + 1)
    return None


def _close_brackets(s: str) -> str:
    """补齐未闭合的括号（含被切断的字符串）。

    只处理「末尾少写/被切断」这一种：它是截断和模型手滑最常见的表现，
    补出来的 JSON 语义仍然正确，所以值得零成本试一次。
    中间缺分隔符（`{"a":1 "b":2}`）**不在这里硬修**——那要靠猜内容，属于编造。
    """
    stack: list[str] = []
    in_str = esc = False
    for ch in s:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]" and stack:
            stack.pop()
    if not stack and not in_str:
        return s
    out = s
    if in_str:
        out += '"'  # 字符串写到一半就被切了
    out += "".join("}" if c == "{" else "]" for c in reversed(stack))
    return out


def loose_repair(s: str) -> str:
    """能安全做的宽松修复：尾逗号、中文引号当键引号、未闭合括号。"""
    out = _TRAILING_COMMA.sub(r"\1", s)
    out = out.replace("“", '"').replace("”", '"')  # 模型偶尔用中文引号包字符串
    out = _close_brackets(out)
    return out


def parse(text: str, allowed_ids: list[int] | None = None) -> Analysis:
    """模型输出 → Analysis。逐级放宽，但每一级都留痕。"""
    raw = text or ""
    cleaned = strip_fences(raw)
    errs: list[str] = []

    data: dict | None = None
    mended = False          # 是否靠「补括号/去尾逗号」才解析出来
    mode = MODE_JSON
    try:
        obj = json.loads(cleaned)
        data = obj if isinstance(obj, dict) else None
        if data is None:
            errs.append("JSON 顶层不是对象")
    except json.JSONDecodeError as e0:
        errs.append(f"JSON 语法错误：{e0}")
        if cleaned.lstrip().startswith("{"):
            # 整段本来就是 JSON，不是「散文里夹 JSON」。
            # 这时**绝不能**退到 find_json_object 去往后找下一个 `{`：
            # 外层只要少一个 `}`，扫描就会落到内层键上，返回一个「括号平衡但语义错位」的
            # 碎片。实测踩过：`{"steps":{...,"scripts":{...}` 少外层 `}` 时，捞回来的是
            # steps 的内容 + scripts，里面恰好有 scripts.primary.text → 被当成「有结果」，
            # 修复重试反被绕过，用户拿到「五步全缺、只剩话术卡」的残缺品，
            # 而界面还显示「部分结构化」像是正常情况。
            cand = cleaned
        else:
            cand = find_json_object(cleaned)   # 前面有散文时才需要定位
        if cand is None:
            errs.append("输出里找不到 JSON 对象")
        else:
            for i, attempt in enumerate((cand, loose_repair(cand))):
                try:
                    obj = json.loads(attempt)
                except json.JSONDecodeError:
                    continue
                data = obj if isinstance(obj, dict) else None
                mode = MODE_REPAIRED
                mended = i == 1        # 是靠「补括号/去尾逗号」才解析出来的
                break
            else:
                errs.append("补齐括号后仍不是合法 JSON")

    if data is None:
        return Analysis(data={}, raw=raw, mode=MODE_DEGRADED,
                        errors=errs or ["无法解析出 JSON"], warnings=[])

    data = _unwrap(data)
    if data is None:
        # 括号扫描在截断输出上可能捞到某个内部小对象（例如只剩 steps.facts）。
        # 那不是分析结果，渲染出来只会是一堆「模型未给出」——不如降级保留原文。
        return Analysis(data={}, raw=raw, mode=MODE_DEGRADED,
                        errors=["提取到的 JSON 不是分析结构（缺 steps / scripts），"
                                "已按纯文本降级"],
                        warnings=[])

    if mended and not (isinstance(data.get("steps"), dict)
                       and isinstance(data.get("scripts"), dict)):
        # 原输出是残缺的（要靠补括号才解析出来），补完**顶层结构仍然不合契约**。
        # 这种「结构」不可信：按它渲染会得到一屏「模型未给出」，而模型实际写出来的内容
        # 反而被丢掉。所以宁可降级，把原文完整留给用户看。
        # 注意区分：如果补完结构是完整的（well_formed），那就是一次成功的零成本修复，
        # 不该降级——模型只是手滑少写了一个 `}`。
        return Analysis(data={}, raw=raw, mode=MODE_DEGRADED,
                        errors=["输出不完整（靠补齐括号才解析出 JSON，且顶层缺 steps / scripts），"
                                "已按纯文本降级"],
                        warnings=[])

    data, dropped = _filter_citations(data, allowed_ids)
    data, n_cleaned = _humanize_scripts(data)
    hard = sc.validate(data)
    if hard:
        mode = MODE_PARTIAL
    warned = sc.lint(data, allowed_ids)
    warns: list[str] = []
    if n_cleaned:
        warns.append(f"话术成品已做口语化清洗（剥掉编号/外层引号/句尾句号等包装痕迹，{n_cleaned} 条）")
    if dropped:
        warns.append("模型引用了记录里不存在的编号，已标为 [#?]：" + "、".join(sorted(set(dropped))))
    if mode == MODE_REPAIRED:
        warns.append("模型输出外面包了多余文字或围栏，已提取其中的 JSON")
    return Analysis(data=data, raw=raw, mode=mode, errors=hard, warnings=warns + warned,
                    citations_dropped=sorted(set(dropped)))


# ------------------------------ 话术成品的收尾清洗 ------------------------------
# 移植自 jev-chat-windows 实测有效的 _clean：模型偶尔把成品包上引号/方括号、
# 带上编号列表或照抄「me:」前缀，句尾还习惯性挂句号——微信里没人这么打字。
_LEAD_LIST = re.compile(r"^\s*(?:\d+[.)、]|[-*•])\s*")
_ME_PREFIX = re.compile(r"^(?:me|我)\s*[:：]\s*", re.IGNORECASE)
_WRAP = " \t[]\"'“”‘’,，"


def clean_script_text(x: str) -> str:
    """剥掉一条话术成品两端的包装痕迹。？！～ 是语气，保留。"""
    s = (x or "").strip()
    m = _LEAD_LIST.match(s)
    if m and not re.match(r"\d", s[m.end():]):
        # 剥「1. / 2、/- 」这类列表前缀；但「3、2、1 上号」这种正文本身以数字开头的不能剥
        s = s[m.end():]
    s = s.strip(_WRAP)
    s = _ME_PREFIX.sub("", s)
    return s[:-1] if s.endswith("。") else s


def _humanize_scripts(data: dict) -> tuple[dict, int]:
    """对话术成品做收尾清洗，返回 (data, 清洗条数)。

    只动真正会被复制发送的字段：`scripts.primary.text` 与 `scripts.variants.*`；
    `timing`/`cost`/`branches` 是分析说明不是成品，不清洗。清完为空的字段保留原值。
    """
    scripts = data.get("scripts")
    if not isinstance(scripts, dict):
        return data, 0
    n = 0
    prim = scripts.get("primary")
    if isinstance(prim, dict) and isinstance(prim.get("text"), str):
        cleaned = clean_script_text(prim["text"])
        if cleaned and cleaned != prim["text"]:
            prim["text"] = cleaned
            n += 1
    variants = scripts.get("variants")
    if isinstance(variants, dict):
        for k, v in variants.items():
            if isinstance(v, str):
                cleaned = clean_script_text(v)
                if cleaned and cleaned != v:
                    variants[k] = cleaned
                    n += 1
    return data, n


def _unwrap(node) -> dict | None:
    """确认/还原成真正的分析对象。

    - 本身有 steps 或 scripts → 直接用；
    - 外面包了一层信封（如 `{"analysis": {...}}`）→ 往里找第一个带 steps+scripts 的对象；
    - 都不是 → 返回 None（由调用方降级）。
    """
    if not isinstance(node, dict):
        return None
    if "steps" in node or "scripts" in node:
        return node
    for v in node.values():
        if isinstance(v, dict) and "steps" in v and "scripts" in v:
            return v
    return None


def _filter_citations(node, allowed_ids: list[int] | None) -> tuple[object, list[str]]:
    """递归过滤所有字符串里的编造 `[#id]`。"""
    allowed = list(allowed_ids or [])
    dropped: list[str] = []

    def walk(v):
        if isinstance(v, str):
            fixed, bad = filter_citations(v, allowed)
            dropped.extend(bad)
            return fixed
        if isinstance(v, list):
            return [walk(x) for x in v]
        if isinstance(v, dict):
            return {k: walk(x) for k, x in v.items()}
        return v

    return walk(node), dropped


__all__ = ["Analysis", "parse", "strip_fences", "find_json_object", "loose_repair",
           "clean_script_text", "MODE_JSON", "MODE_REPAIRED", "MODE_PARTIAL",
           "MODE_DEGRADED", "MODE_LABEL"]