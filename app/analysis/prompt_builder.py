# -*- coding: utf-8 -*-
"""prompt_builder：把 skill、transcript、档案、用户诉求组装成一次请求（§5.3 / §7）。

分工：
- `system` = SKILL.md 全文 + 本次命中的 1–3 份参考 +（档案/记忆召回）+ **输出契约**；
- `user`   = 会话元信息（说话人锁定、统计、时间可信度）+ 带 `#id` 的记录 + 用户诉求。

两条不肯让步的规矩：
1. **引用必须可核对**：`facts.known` 每条以 `[#id]` 开头，id 只能来自本次真正给出去的记录
   （`Prompt.included_ids`），模型编的 id 会在解析后被剔除并告警。
2. **预算可解释**：记录超预算就按「保留最近 N 条」截断并**明说截了几条**，
   绝不悄悄丢一半历史还说结论成立。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.analysis import questions as q
from app.analysis.skill_loader import LoadedSkill

_CJK = re.compile(r"[\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uff00-\uffef]")
_ID = re.compile(r"#(\d+)")

DEFAULT_TOKEN_BUDGET = 3500


def est_tokens(text: str) -> int:
    """粗估 token：中文 ≈1.5 字/token，其余 ≈4 字/token。

    不引 tiktoken（省一个依赖、省一份打包体积）；只用于**预算截断**，
    真实用量以服务端返回的 usage 为准。
    """
    if not text:
        return 0
    cjk = len(_CJK.findall(text))
    other = len(text) - cjk
    return int(cjk / 1.5 + other / 4) + 1


# ------------------------------ 输出契约 ------------------------------
CONTRACT = """## 输出契约（必须遵守，违反即视为本次回答失败）

只输出**一个 JSON 对象**：不要 markdown 代码块围栏、不要任何解释性文字、不要前后缀。

```json
{
  "steps": {
    "emotion": "2–4 句：指出感受、触发点与冲突。认可感受，但不替未经证实的解释背书",
    "facts": {
      "known": ["[#12] 只写记录里能直接看到的事实"],
      "inferred": ["合理推测，并写明依据"],
      "unknown": ["影响判断但记录里没有的关键信息"]
    },
    "interests": "从互惠、可靠、吸引、现实可行性、可逆性、安全、机会成本角度判断",
    "advice": {"primary": "一句首选建议", "reasons": ["理由，2–4 条"]},
    "actions": {
      "next_action": "现在就能做的一个小动作",
      "watch_window": "观察窗口（多久内看什么）",
      "stop_condition": "什么情况下停止推进",
      "signals_to_report": ["值得回来反馈的具体信号"]
    }
  },
  "scripts": {
    "primary": {
      "text": "可直接复制发送的成品原文",
      "timing": "什么时机发",
      "cost": "发出去的主要代价或风险",
      "branches": {"positive": "对方积极时怎么接", "vague": "对方含糊时怎么接", "no_reply": "不回应时怎么办"}
    },
    "variants": {"steady": "稳健版", "flirty": "会撩/策略版", "assertive": "强势版（边界与筛选，不是羞辱或威胁）"}
  },
  "citations": ["[#12]", "[#15]"],
  "confidence": "high | medium | low",
  "boundaries": ["未做诊断", "未保证效果"]
}
```

引用与事实规则：
- `facts.known` 每条**必须**以 `[#id]` 开头，id 只能取下面记录里出现过的编号；**不得编造 id**。
- 记录里看不到的信息写进 `unknown`；**不为完整性而虚构**。缺失就说未知。

话术规则：
- `scripts.primary.text` 是能**直接发出去**的成品，不要写成「你可以说……」的建议口吻。
- 写话术时，你**就是用户本人**正在微信输入框里打字：不是助手、不是客服、不是在写作文。
- 不总结、不复述对方的话，也不解释自己为什么这么回；不写「作为一个……」「考虑到……」这类开头。
- 禁用「首先」「其次」「另外」「总之」「综上」，禁用「亲」「您」「希望」「祝」「加油哦」这类客套。
- 不排比、不对仗、不凑三段式；能不加标点就不加，句尾别习惯性加句号；
  感叹号和 emoji 只有用户自己平时也这么用时才用。
- 允许不完整的句子、口头语、语气词，长短错落；别每条话术都以「好」「嗯」开头。
- 首选和三个 variants 是**同一个人在四种状态下随手打的**，不是同一句话换四个正式程度的说法：
  标签只决定策略方向（稳一点／撩一点／硬一点），不决定语气正式度；长短可以差很多，其中一版可以只有几个字。
- 每条消息只承载一个主动作，不要把承接、邀约、澄清、收线堆在一条里。

安全边界（§16）：
- 不诊断心理疾病，不用 MBTI、依恋或任何标签替代行为证据，不保证话术能让特定的人爱上用户。
- 不提供贬低、服从性测试、虚假时间限制、假未来、嫉妒操控、奖惩、煤气灯、孤立、跟踪或性施压方案。
- `boundaries` 字段固定包含「未做诊断」与「未保证效果」，可按需增补。
"""

REPLY_EXTRA = """本次是「这句怎么回」模式：
- 第一屏先给一条可复制成品（即 `scripts.primary.text`），随后才是时机、代价与后续分支。
- `steps` 仍要给全，但 `emotion` 与 `interests` 保持简短，把篇幅留给成品与分支。
"""

CRISIS_HEAD = """## 紧急例外（本次扫描命中：{hits}）

安全优先于关系分析，按这个顺序处理：
1. `steps.emotion` 第一段先确认**当下安全**：现在是否有人身危险、是否已离开冲突现场、身边有没有可信任的人。
2. 给出可信支持与当地紧急服务的转介方向（中国大陆：110 报警、12338 妇女维权热线、12345 政务热线；
   如涉未成年人可提 12355），并建议保留证据、告知可信任的人。
3. **不要**把它当普通恋爱咨询淡化处理，不要建议「再沟通看看」来绕开危险；不做法律意见或诊断。
4. 其余步骤照常，但所有建议必须是合法、低风险、可退出的。
"""

TRUST_NOTE = """## 时间可信度
{lines}
时间不精确时**不要基于间隔下结论**；需要间隔判断就必须在 `facts.unknown` 里说明这一点。
"""


@dataclass
class Prompt:
    system: str
    user: str
    included_ids: list[int] = field(default_factory=list)
    dropped: int = 0
    stats: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    refs: list[str] = field(default_factory=list)
    crisis: list[str] = field(default_factory=list)
    question: str = q.DEFAULT_KEY

    @property
    def system_chars(self) -> int:
        return len(self.system)

    def token_estimate(self) -> dict:
        return {
            "system": est_tokens(self.system),
            "user": est_tokens(self.user),
            "total": est_tokens(self.system) + est_tokens(self.user),
        }


def build_system(loaded: LoadedSkill, qtype: q.QuestionType, *, profile: str = "",
                 memory: str = "", crisis: list[str] | None = None,
                 memory_requested: bool = False) -> str:
    parts: list[str] = []
    if crisis:
        parts.append(CRISIS_HEAD.format(hits="、".join(crisis)).strip())

    parts.append(
        "你在为一个中国用户做关系与沟通分析。以下是你要遵循的技能总纲与参考资料；"
        "它们的方法、边界与语气**都必须照做**，不得自行换一套框架。"
    )
    if loaded.text:
        parts.append(loaded.text)
    else:
        parts.append("（本次没有加载到 skill 内容，请按通用的关系沟通原则作答，"
                     "并在 `unknown` 里说明缺少方法论参考。）")

    parts.append(f"## 本次任务类型：{qtype.label}\n{qtype.brief}")

    if profile.strip():
        parts.append("## 用户与对象档案（用户自己填写或已同意保存的，可能过时）\n"
                     + profile.strip())
    if memory.strip():
        parts.append("## 长期记忆召回（用户可随时暂停或撤销）\n" + memory.strip())
    elif memory_requested:
        parts.append("## 长期记忆\n本次没有可用的已保存档案。不要从姓名、MBTI 或旧案例推测补事实，"
                     "也不要声称已经记住什么。")

    parts.append(CONTRACT.strip())
    if qtype.reply_mode:
        parts.append(REPLY_EXTRA.strip())
    return "\n\n---\n\n".join(p for p in parts if p)


def format_records(transcript, budget_tokens: int) -> tuple[str, list[int], int, list[str]]:
    """把消息渲染成 `#id 时间 sender 内容`，超预算就保留最近 N 条。

    返回 (文本, 含入的 id 列表, 丢掉条数, 告警)。
    """
    warns: list[str] = []
    msgs = list(transcript.messages)
    if not msgs:
        return "", [], 0, ["transcript 里没有任何消息"]

    lines = [m.line() for m in msgs]
    costs = [est_tokens(s) + 2 for s in lines]
    total = sum(costs)

    keep = len(msgs)
    if total > budget_tokens:
        used = 0
        keep = 0
        for c in reversed(costs):
            if used + c > budget_tokens and keep > 0:
                break
            used += c
            keep += 1
        keep = max(1, keep)  # 至少留一条，否则等于没给输入
        warns.append(f"记录超出上下文预算，只包含最近 {keep} 条（省略了更早的 {len(msgs) - keep} 条）")

    start = len(msgs) - keep
    body = "\n".join(lines[start:])
    if start > 0:
        body = f"（更早的 {start} 条因上下文预算未包含）\n" + body
    return body, [m.id for m in msgs[start:]], start, warns


def voice_samples(transcript, limit: int = 12, max_chars: int = 60) -> list[str]:
    """用户自己最近发过的短消息，当口吻样本（让模型模仿用词、句长、标点、语气词）。

    链接和长段不是风格；非文字消息（图片/语音/表情占位）也不是。样本少于 2 条时不给：
    一两句话撑不起「习惯」，模型反而会把那一句当成唯一句式去复读。
    """
    me_code = ((transcript.speaker_map or {}).get("me") or {}).get("code") or "me"
    out: list[str] = []
    for m in reversed(transcript.messages):
        if m.sender != me_code or m.type != "text":
            continue
        t = (m.text or "").strip()
        if not t or len(t) > max_chars or "http" in t.lower():
            continue
        out.append(t)
        if len(out) >= limit:
            break
    return out if len(out) >= 2 else []


def build(transcript, qtype_key: str | None = None, *, loaded: LoadedSkill | None = None,
          goal: str = "", emotion: int | None = None, extra: str = "",
          profile: str = "", memory: str = "", crisis: list[str] | None = None,
          style: str = "",
          token_budget: int = DEFAULT_TOKEN_BUDGET) -> Prompt:
    """组装一次请求。`loaded` 为 None 时退化为「无 skill」模式（仍会给输出契约）。"""
    loaded = loaded or LoadedSkill()
    qtype = q.get(qtype_key)
    budget = int(token_budget)

    system = build_system(loaded, qtype, profile=profile, memory=memory, crisis=crisis,
                          memory_requested=bool(qtype_key == "memory"))
    body, ids, dropped, warns = format_records(transcript, budget)
    if loaded.warnings:
        warns.extend(loaded.warnings)

    sm = transcript.speaker_map or {}
    me = sm.get("me", {}) or {}
    objs = sm.get("objects", []) or []
    who = "、".join(f"{o.get('code')}（{o.get('label') or o.get('side') or '未命名'}）" for o in objs) or "未识别"
    confirmed = bool(me.get("confirmed"))
    st = transcript.stats()

    meta = [
        f"- 会话/对象：{transcript.window.get('chat_title') or transcript.window.get('title') or '未识别'}"
        f"（来源：{transcript.source}）",
        f"- 说话人锁定：我 = {me.get('side') or '未确认'}；对方 = {who}"
        + ("（用户已确认）" if confirmed else "（**未确认**，请在 `unknown` 里提示用户核对）"),
        f"- 时间范围：{st['first_ts']} ~ {st['last_ts']}（共 {st['total']} 条）",
        f"- 条数：我 {st['me']} 条 / 对方 {st['object']} 条；字数：我 {st['me_chars']} / 对方 {st['object_chars']}",
        f"- 对方平均每条 {st['avg_object_len']} 字；最长间隔 {st['max_gap_s']} 秒",
    ]
    if st["image_placeholders"]:
        meta.append(f"- 其中 {st['image_placeholders']} 条是非文字消息（图片/语音/表情占位），"
                    "不要替它们编内容")

    trust = transcript.warnings or ["时间戳来自消息内的时间分隔行，可用"]
    voice = voice_samples(transcript)
    voice_sec = (f"## 我平时是这么说话的（写话术时模仿这里的用词、句长、标点和语气词习惯）\n"
                 + "\n".join(voice)) if voice else ""
    style_sec = ("## 我对自己口吻的描述：" + style.strip()) if style.strip() else ""
    user_parts = [
        "## 本次会话\n" + "\n".join(meta),
        TRUST_NOTE.format(lines="\n".join(f"- {w}" for w in trust)).strip(),
        f"## 记录（格式：#id 时间 说话人 内容）\n{body}",
        voice_sec,
        style_sec,
        "## 用户这次的问题\n"
        f"- 问题类型：{qtype.label}\n"
        f"- 我要的结果：{goal.strip() or '未填写（按问题类型的默认目标来）'}\n"
        f"- 我现在的情绪强度：{emotion if emotion is not None else '未填写'}"
        + ("/10\n" if emotion is not None else "\n")
        + (f"- 补充：{extra.strip()}\n" if extra.strip() else "")
        + "\n请直接按输出契约给出 JSON。",
    ]

    return Prompt(
        system=system,
        user="\n\n".join(p for p in user_parts if p),
        included_ids=ids,
        dropped=dropped,
        stats=st,
        warnings=warns,
        refs=loaded.files,
        crisis=list(crisis or []),
        question=qtype.key,
    )


def filter_citations(text: str, allowed: list[int]) -> tuple[str, list[str]]:
    """剔除模型编造的 `[#id]`。返回 (修正后的文本, 被剔除的 id)。"""
    allowed_set = {int(i) for i in allowed}
    bad: list[str] = []

    def _sub(m: re.Match) -> str:
        n = int(m.group(1))
        if n in allowed_set:
            return m.group(0)
        bad.append(f"#{n}")
        return "#?"  # 只换 id 本体：外面的方括号不属于匹配范围，替换成 [#?] 会变成双括号

    return _ID.sub(_sub, text or ""), bad


__all__ = ["Prompt", "build", "build_system", "format_records", "est_tokens",
           "filter_citations", "voice_samples", "CONTRACT", "DEFAULT_TOKEN_BUDGET"]
