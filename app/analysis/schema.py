# -*- coding: utf-8 -*-
"""analysis.json 的契约（README.optimized §7.2）。

两件事：
- `ANALYSIS_SCHEMA`：JSON Schema，传给支持原生结构化输出的服务端；
- `validate()` / `lint()`：零依赖自校验。**校验器必须自己写**，不引 jsonschema——
  少一个依赖，而且这里要的报错文案是给人看的中文，不是标准库的英文堆栈。

分层：
- `validate()` 返回**硬错误**（缺了就没法渲染：比如没有可发送的成品话术）；
- `lint()` 返回**软提醒**（能渲染但不合规范：比如只有 1 条理由、引用格式不对）。
"""
from __future__ import annotations

import re

CONFIDENCE = ("high", "medium", "low")
_ID = re.compile(r"\[#\d+\]")
FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.I)

# 五步的固定标题与话术卡 Tab，UI 与渲染共用
STEP_TITLES = ("① 情绪落地", "② 事实拆分", "③ 利益判断", "④ 明确建议", "⑤ 行动收束")
SCRIPT_TABS = ("首选", "稳健", "会撩·策略", "强势")
VARIANT_KEYS = ("steady", "flirty", "assertive")

ANALYSIS_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": True,
    "required": ["steps", "scripts", "citations", "confidence", "boundaries"],
    "properties": {
        "steps": {
            "type": "object",
            "required": ["emotion", "facts", "interests", "advice", "actions"],
            "properties": {
                "emotion": {"type": "string",
                            "description": "2–4 句：感受、触发点与冲突"},
                "facts": {
                    "type": "object",
                    "required": ["known", "inferred", "unknown"],
                    "properties": {
                        "known": {"type": "array", "items": {"type": "string"},
                                  "description": "每条以 [#id] 开头的已知事实"},
                        "inferred": {"type": "array", "items": {"type": "string"}},
                        "unknown": {"type": "array", "items": {"type": "string"}},
                    },
                },
                "interests": {"type": "string"},
                "advice": {
                    "type": "object",
                    "required": ["primary", "reasons"],
                    "properties": {
                        "primary": {"type": "string"},
                        "reasons": {"type": "array", "items": {"type": "string"}},
                    },
                },
                "actions": {
                    "type": "object",
                    "required": ["next_action", "watch_window", "stop_condition",
                                 "signals_to_report"],
                    "properties": {
                        "next_action": {"type": "string"},
                        "watch_window": {"type": "string"},
                        "stop_condition": {"type": "string"},
                        "signals_to_report": {"type": "array", "items": {"type": "string"}},
                    },
                },
            },
        },
        "scripts": {
            "type": "object",
            "required": ["primary", "variants"],
            "properties": {
                "primary": {
                    "type": "object",
                    "required": ["text", "timing", "cost", "branches"],
                    "properties": {
                        "text": {"type": "string", "description": "可直接复制发送的成品原文"},
                        "timing": {"type": "string"},
                        "cost": {"type": "string"},
                        "branches": {
                            "type": "object",
                            "required": ["positive", "vague", "no_reply"],
                            "properties": {
                                "positive": {"type": "string"},
                                "vague": {"type": "string"},
                                "no_reply": {"type": "string"},
                            },
                        },
                    },
                },
                "variants": {
                    "type": "object",
                    "required": list(VARIANT_KEYS),
                    "properties": {k: {"type": "string"} for k in VARIANT_KEYS},
                },
            },
        },
        "citations": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "string", "enum": list(CONFIDENCE)},
        "boundaries": {"type": "array", "items": {"type": "string"}},
    },
}


def _s(v) -> str:
    return v.strip() if isinstance(v, str) else ""


def _l(v) -> list:
    return v if isinstance(v, list) else []


def validate(d: dict) -> list[str]:
    """硬错误：缺这些就渲染不出可用的五步 + 话术卡。"""
    errs: list[str] = []
    if not isinstance(d, dict):
        return ["顶层不是 JSON 对象"]

    steps = d.get("steps")
    if not isinstance(steps, dict):
        errs.append("缺少 steps")
        steps = {}
    if not _s(steps.get("emotion")):
        errs.append("steps.emotion 为空")
    facts = steps.get("facts") if isinstance(steps.get("facts"), dict) else {}
    for k in ("known", "inferred", "unknown"):
        if not isinstance(facts.get(k), list):
            errs.append(f"steps.facts.{k} 不是数组")
    if not _s(steps.get("interests")):
        errs.append("steps.interests 为空")
    advice = steps.get("advice") if isinstance(steps.get("advice"), dict) else {}
    if not _s(advice.get("primary")):
        errs.append("steps.advice.primary 为空")
    if not _l(advice.get("reasons")):
        errs.append("steps.advice.reasons 为空")
    actions = steps.get("actions") if isinstance(steps.get("actions"), dict) else {}
    for k in ("next_action", "watch_window", "stop_condition"):
        if not _s(actions.get(k)):
            errs.append(f"steps.actions.{k} 为空")
    if not _l(actions.get("signals_to_report")):
        errs.append("steps.actions.signals_to_report 为空")

    scripts = d.get("scripts")
    if not isinstance(scripts, dict):
        # 单独点出「顶层缺 scripts」：模型把 scripts 嵌进 steps 里（或反过来）时，
        # 只说「scripts.primary.text 为空」会让它以为漏了个字段去补，
        # 而真正的问题是**层级写错了**，补字段解决不了。
        errs.append("缺少 scripts（必须与 steps 平级，不能嵌在 steps 里面）")
        scripts = {}
    prim = scripts.get("primary") if isinstance(scripts.get("primary"), dict) else {}
    if not _s(prim.get("text")):
        errs.append("scripts.primary.text 为空（没有可发送的成品话术）")
    branches = prim.get("branches") if isinstance(prim.get("branches"), dict) else {}
    for k in ("positive", "vague", "no_reply"):
        if not _s(branches.get(k)):
            errs.append(f"scripts.primary.branches.{k} 为空")
    variants = scripts.get("variants") if isinstance(scripts.get("variants"), dict) else {}
    for k in VARIANT_KEYS:
        if not _s(variants.get(k)):
            errs.append(f"scripts.variants.{k} 为空")

    if d.get("confidence") not in CONFIDENCE:
        errs.append("confidence 必须是 high/medium/low 之一")
    if not _l(d.get("boundaries")):
        errs.append("boundaries 为空（应含「未做诊断」「未保证效果」）")
    return errs


def lint(d: dict, allowed_ids: list[int] | None = None) -> list[str]:
    """软提醒：能渲染，但不合规范或需要用户知道的地方。"""
    out: list[str] = []
    if not isinstance(d, dict):
        return ["顶层不是 JSON 对象"]
    steps = d.get("steps") if isinstance(d.get("steps"), dict) else {}
    facts = steps.get("facts") if isinstance(steps.get("facts"), dict) else {}
    known = [x for x in _l(facts.get("known")) if isinstance(x, str)]

    if not known:
        out.append("没有给出任何带证据编号的已知事实（facts.known 为空）")
    else:
        no_cite = [x for x in known if not _ID.match(x.strip())]
        if no_cite:
            out.append(f"{len(no_cite)} 条已知事实没有以 [#id] 开头")
        if allowed_ids:
            allowed = {f"[#{i}]" for i in allowed_ids}
            bad = sorted({m for x in known for m in _ID.findall(x)} - allowed)
            if bad:
                out.append("引用了本次记录里不存在的编号：" + "、".join(bad))
        if not _l(facts.get("unknown")):
            out.append("facts.unknown 为空——记录里通常总有关键未知，别假装信息完整")

    advice = steps.get("advice") if isinstance(steps.get("advice"), dict) else {}
    reasons = _l(advice.get("reasons"))
    if len(reasons) < 2:
        out.append(f"steps.advice.reasons 只有 {len(reasons)} 条，规范是 2–4 条")

    prim = ((d.get("scripts") or {}).get("primary") or {})
    text = _s(prim.get("text"))
    if text:
        if re.search(r"(你可以说|建议你|不妨说|或许可以)", text):
            out.append("scripts.primary.text 写成了建议口吻，应给可直接发送的成品")
        if len(text) > 200:
            out.append(f"scripts.primary.text 有 {len(text)} 字，偏长，容易被对方读成小作文")

    boundaries = " ".join(x for x in _l(d.get("boundaries")) if isinstance(x, str))
    if "诊断" not in boundaries:
        out.append("boundaries 里没有「未做诊断」")
    if "保证" not in boundaries:
        out.append("boundaries 里没有「未保证效果」")

    if not _l(d.get("citations")):
        out.append("citations 为空")
    return out


__all__ = ["ANALYSIS_SCHEMA", "validate", "lint", "CONFIDENCE", "STEP_TITLES",
           "SCRIPT_TABS", "VARIANT_KEYS"]
