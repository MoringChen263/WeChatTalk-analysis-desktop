# -*- coding: utf-8 -*-
"""说话人映射：把 OCR 的 me/her 翻译成 transcript 的 sender 码，并做「首轮必须用户确认」。

背景（§0.1 E5）：上游 `ocr.who_said()` 的实际判据是**气泡底色**（绿底=我），
不是计划书原来写的「按中心 x 分左右」。M-1 实测两者 100% 自洽（72 行零冲突），
所以：
- 本模块用底色分类结果**反推**左右侧作为**建议值**；
- 建议值**不自动生效**：`confirmed=false` 时所有 transcript 都带警告，
  必须由用户在 UI 上点一次「我 = 右侧/左侧」才落进 config（skill 边界要求：不猜）。

config 形态（§10）：
    "capture": { "speaker_map": { "me": {"side": "right", "code": "me", "confirmed": true},
                                  "objects": [{"code": "obj-1", "side": "left", "label": "对方"}] } }
"""
from __future__ import annotations

from collections import Counter

RIGHT_BIAS = 0.5  # 气泡中心 x / 聊天区宽度 超过它算右半边


def default_map(side: str = "right", confirmed: bool = False, label: str = "对方") -> dict:
    other = "left" if side == "right" else "right"
    return {
        "me": {"side": side, "code": "me", "confirmed": bool(confirmed)},
        "objects": [{"code": "obj-1", "side": other, "label": label}],
    }


def suggest(lines, chat_w: int = 0) -> dict:
    """从一帧的 OCR 行反推左右侧建议值。

    `lines`：`ocr.Line` 列表（也兼容旧元组）。
    返回 {"side", "confidence", "agreement", "detail"}：
    - side：底色判为 me 的气泡，中心 x 多数落在哪半边；
    - agreement：底色判定与 x 位置一致的比例（M-1 实测 1.0）；< 0.9 时 UI 要更强调人工确认。
    """
    pairs = [(_who(l), _x_rel(l)) for l in lines if _who(l) in ("me", "her") and _x_rel(l) is not None]
    if not pairs:
        return {"side": "right", "confidence": 0.0, "agreement": 0.0,
                "detail": {"me_total": 0, "her_total": 0, "me_right": 0, "her_left": 0,
                           "note": "本帧无可分类气泡，无法给出建议"}}

    sides = Counter("right" if x >= RIGHT_BIAS else "left" for w, x in pairs if w == "me")
    me_side = sides.most_common(1)[0][0] if sides else "right"
    me_total = sum(1 for w, _ in pairs if w == "me")
    her_total = sum(1 for w, _ in pairs if w == "her")
    me_right = sum(1 for w, x in pairs if w == "me" and x >= RIGHT_BIAS)
    her_left = sum(1 for w, x in pairs if w == "her" and x < RIGHT_BIAS)
    if me_side == "right":
        # 「我」在右侧 ⇒ 对方的正确位置是**左侧**，所以 her_left 才是自洽的那部分
        hits = me_right + her_left
    else:
        hits = (me_total - me_right) + (her_total - her_left)
    total = me_total + her_total
    agreement = round(hits / total, 4) if total else 0.0
    support = sides.get(me_side, 0) / me_total if me_total else 0.0
    return {
        "side": me_side,
        "confidence": round(min(0.5 * support + 0.5 * agreement, 1.0), 4),
        "agreement": agreement,
        "detail": {"me_total": me_total, "her_total": her_total,
                   "me_right": me_right, "her_left": her_left,
                   "support": round(support, 4),
                   "median_x": round(_median([x for w, x in pairs if w == "me"]), 4)},
    }


def translate(lines, speaker_map: dict) -> list[tuple[str, object]]:
    """OCR 行 → [(sender_code, Line)]。"""
    sm = speaker_map or default_map()
    me_code = (sm.get("me") or {}).get("code", "me")
    obj_code = (sm.get("objects") or [{}])[0].get("code", "obj-1")
    out = []
    for l in lines:
        who = _who(l)
        if who == "me":
            out.append((me_code, l))
        elif who == "her":
            out.append((obj_code, l))
    return out


def is_confirmed(speaker_map: dict) -> bool:
    return bool(((speaker_map or {}).get("me") or {}).get("confirmed"))


def confirm(config, side: str, label: str = "对方") -> dict:
    """用户在 UI 上确认后调用：写进 config 并返回新的 map。"""
    assert side in ("left", "right"), side
    sm = default_map(side, confirmed=True, label=label)
    config.set("capture.speaker_map", sm)
    config.save()
    return sm


def unconfirmed_warning(speaker_map: dict) -> str | None:
    if is_confirmed(speaker_map):
        return None
    side = ((speaker_map or {}).get("me") or {}).get("side", "right")
    return (f"说话人未确认（当前按建议值「我 = {'右侧' if side == 'right' else '左侧'}」处理），"
            f"请在左栏点一次「我 = 右侧/左侧」后再用于分析")


# --------------------------- 兼容取值 ---------------------------
def _who(l) -> str:
    return getattr(l, "who", None) if not isinstance(l, tuple) else l[0]


def _x_rel(l):
    if isinstance(l, tuple):
        return l[6] if len(l) > 6 else None
    return getattr(l, "x_rel", None)


def _median(vals) -> float:
    if not vals:
        return 0.0
    vals = sorted(vals)
    n = len(vals)
    return vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2
