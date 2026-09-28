# -*- coding: utf-8 -*-
"""问题类型目录 + 危机信号扫描（README.optimized §5.3 / §16）。

**权威来源是 skill 的 `SKILL.md`「按需加载」表**，这里只是它的可执行镜像。
因此 `skill_loader.validate_catalog()` 会在启动/自检时逐条核对路径是否存在——
文档里路径写错、skill 升级改名，都会**当场报错**，不会静默退化成「没有参考可用」。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# --- skill 内参考文件的相对路径（逐字照抄 SKILL.md 的「按需加载」表） ---
R_SCRIPT = "references/practical/实战话术编排器：从一句回复到后续分支.md"
R_VIBE = "references/practical/场景感、松弛感与社交校准：从接话到关系推进.md"
R_NATURAL = "references/practical/自然流、内在状态与结构化互动：伦理能力转译.md"
R_SYSTEM20 = "references/knowledge/20-经典社交体系的机制、证据与风险边界.md"
R_PUA = "references/knowledge/05-PUA操控与伦理替代.md"
R_ONLINE = "references/knowledge/09-在线约会与数字关系.md"
R_CHATLAB = "references/practical/ChatLab聊天记录分析适配.md"
R_MEMORY = "references/practical/长期记忆与关系档案.md"
R_FIRSTMEET = "references/practical/主动表达、第一次见面与自然接触.md"
R_IMBALANCE = "references/practical/关系投入失衡：互惠判断、降级投入与退出决策.md"
R_ATTACH = "references/knowledge/03-依恋理论与情绪调节.md"
R_MBTI = "references/knowledge/04-MBTI人格与匹配.md"
R_CONFLICT = "references/knowledge/07-沟通冲突与修复.md"
R_CONSENT = "references/knowledge/08-同意边界性与亲密.md"
R_LAW = "references/knowledge/17-中国法律安全与危机转介.md"
R_MARRIAGE = "references/knowledge/11-婚姻家庭与生命周期.md"
R_MONEY = "references/knowledge/12-金钱家务育儿与双方家庭.md"
R_BREAKUP = "references/knowledge/15-分手背叛与关系修复.md"
R_EVIDENCE = "references/knowledge/01-证据分级与内容边界.md"
R_BOOKS = "references/knowledge/19-核心书单与论文索引.md"
R_KICKOFF = "references/practical/00-导读与使用分级.md"


@dataclass(frozen=True)
class QuestionType:
    """一种用户可选的提问类型。

    `refs` 顺序即加载优先级；**上限 3 份**（SKILL.md：默认只读当前问题直接需要的 1–3 份）。
    `brief` 会写进 system prompt，告诉模型这次该用哪套方法，而不是让它自己猜。
    """

    key: str
    label: str
    refs: tuple[str, ...]
    brief: str
    reply_mode: bool = False    # True = 「这句怎么回」：第一屏先给可复制成品
    wide_context: bool = False  # True = 需要更长历史（看趋势不能只看最近几条）


QUESTION_TYPES: tuple[QuestionType, ...] = (
    QuestionType(
        key="reply",
        label="这句怎么回",
        refs=(R_SCRIPT,),
        reply_mode=True,
        brief="用户要一条现在就能发出去的回复。第一屏先给一条可复制成品，"
              "再写发送时机、主要代价，以及积极／含糊／不回应的三种后续。"
              "每条消息只承载一个主动作，不堆叠承接、邀约、澄清和收线。",
    ),
    QuestionType(
        key="invite",
        label="邀约/开场/第一次见面",
        refs=(R_FIRSTMEET, R_VIBE),
        reply_mode=True,
        brief="目标是主动一次：具体邀约、低强度、可退出、逐步看反馈。"
              "不把沉默当同意，不把邀约写成必须完成的漏斗。",
    ),
    QuestionType(
        key="imbalance",
        label="怠慢/投入失衡/要不要退",
        refs=(R_IMBALANCE,),
        brief="按持续主动、兑现、投入、边界、冲突修复来判断互惠，"
              "不凭单次回复或表情定性。用户想退出时不强行推进。",
    ),
    QuestionType(
        key="conflict",
        label="冲突/吵架",
        refs=(R_CONFLICT, R_SCRIPT),
        reply_mode=True,
        brief="先接住情绪再处理事实。区分「要一个动作」「要被理解」「要一个解释」；"
              "不提供贬低、服从性测试、煤气灯或奖惩方案。",
    ),
    QuestionType(
        key="attachment",
        label="依恋/焦虑",
        refs=(R_ATTACH,),
        brief="用依恋框架解释焦虑来源时，只做**行为**层面的解释，不诊断心理疾病、"
              "不用标签替代行为证据，不替对方读心。",
    ),
    QuestionType(
        key="trend",
        label="关系趋势/长记录",
        refs=(R_CHATLAB,),
        wide_context=True,
        brief="长记录先看趋势而非单点：主动比、回复间隔、话题深度、冲突后的修复速度。"
              "趋势只用于判断，不构成升级权利。",
    ),
    QuestionType(
        key="pua",
        label="冷读/PUA 担心",
        refs=(R_PUA, R_SYSTEM20),
        brief="把冷读改写成「观察事实 + 暂定假设 + 邀请纠正」；"
              "识别到操控手法时，先说明它为什么有害，再给伦理替代方案。",
    ),
    QuestionType(
        key="mbti",
        label="MBTI/人设匹配",
        refs=(R_MBTI,),
        brief="MBTI 只作为用户自述的暂定框架，用真实行为校正；"
              "不把它当预测工具，不据此断定对方想法。",
    ),
    QuestionType(
        key="boundary",
        label="同意/性/亲密边界",
        refs=(R_CONSENT,),
        brief="边界与同意优先于任何推进目标。明确拒绝、僵住、躲避或撤回时立即停止；"
              "只给合法、低风险、可退出的方案。",
    ),
    QuestionType(
        key="crisis",
        label="安全/法律/危机",
        refs=(R_LAW,),
        brief="先确认当下安全，再谈关系。给可信支持与当地紧急服务的转介方向，"
              "不越界提供法律意见，不诊断。",
    ),
    QuestionType(
        key="memory",
        label="跨任务档案/记忆",
        refs=(R_MEMORY,),
        brief="需要跨任务档案时先说明档案现状与用户可控的暂停／撤销方式；"
              "没有保存过就直说没有，不从姓名、MBTI 或旧案例推测补事实。",
    ),
    QuestionType(
        key="default",
        label="默认（实战话术编排器）",
        refs=(R_SCRIPT,),
        brief="按 SKILL.md 的五步走：情绪落地 → 事实拆分 → 利益判断 → 明确建议 → 行动收束。",
    ),
)

BY_KEY: dict[str, QuestionType] = {q.key: q for q in QUESTION_TYPES}
DEFAULT_KEY = "default"
MAX_REFS = 3


def get(key: str | None) -> QuestionType:
    """取问题类型；未知 key 退回默认（不抛，UI 传来的 key 不该让分析直接失败）。"""
    return BY_KEY.get((key or "").strip(), BY_KEY[DEFAULT_KEY])


def label_of(key: str | None) -> str:
    return get(key).label


def pick_refs(key: str | None, extra: list[str] | None = None) -> list[str]:
    """该类型要加载的参考文件（去重、截到 3 份，extra 优先）。"""
    out: list[str] = []
    for p in list(extra or []) + list(get(key).refs):
        if p and p not in out:
            out.append(p)
    return out[:MAX_REFS]


def route_table() -> list[tuple[str, str, tuple[str, ...]]]:
    """(key, label, refs)，给自检和文档用。"""
    return [(q.key, q.label, q.refs) for q in QUESTION_TYPES]


# ------------------------------ 危机信号扫描 ------------------------------
# 这是**提示词层面的加急通道**，不是分类器，也不做医学/法律判断：
# 命中就把安全类参考强制加进上下文 + 在 UI 打横幅 + 要求模型先确认安全。
# 宁可多报一次，也不要在真实危险面前按普通恋爱咨询处理。
_CRISIS = (
    ("家暴或人身暴力", re.compile(r"家暴|家庭暴力|动手打|打了?我|殴打|掐脖|推搡|扇耳光|拿刀|威胁我")),
    ("跟踪或骚扰", re.compile(r"跟踪|尾随|堵门|蹲我|骚扰|被威胁|恐吓")),
    ("胁迫或非自愿", re.compile(r"下药|灌醉|偷拍|强迫我|胁迫|勒索|裸照|不雅照|要挟")),
    ("自伤或轻生", re.compile(r"自残|自伤|割腕|想死|不想活|自杀|活不下去")),
    ("涉及未成年人", re.compile(r"未成年|没成年|初中生|高中生|14\s*岁|15\s*岁|16\s*岁|17\s*岁")),
    ("财务控制或诈骗", re.compile(r"借钱不还|骗钱|杀猪盘|网恋投资|要我转账|贷款")),
)


def scan_crisis(*texts: str) -> list[str]:
    """扫描 transcript / 用户目标里的危机词，返回命中的原因列表（可能为空）。"""
    blob = "\n".join(t for t in texts if t)
    if not blob:
        return []
    return [name for name, pat in _CRISIS if pat.search(blob)]


def crisis_refs() -> list[str]:
    """命中危机时强制追加的参考（法律安全 + 同意边界）。"""
    return [R_LAW, R_CONSENT]


__all__ = [
    "QuestionType", "QUESTION_TYPES", "BY_KEY", "DEFAULT_KEY", "MAX_REFS",
    "get", "label_of", "pick_refs", "route_table", "scan_crisis", "crisis_refs",
    "R_SCRIPT", "R_EVIDENCE", "R_BOOKS", "R_KICKOFF",
]
