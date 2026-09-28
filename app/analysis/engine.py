# -*- coding: utf-8 -*-
"""engine：一轮分析的编排（README.optimized §4.2 数据流 / §7.2 修复与降级）。

    transcript → 危机扫描 → 载入 skill + 按需参考 → 组装 prompt
               → LLM（结构化输出阶梯）→ 解析 → 需要时一次修复重试 → 渲染 / 降级

两个刻意的取舍：
- **修复重试要花钱，所以只在「渲染不出来」时才做**（`Analysis.ok` 为假）。
  能渲染的部分结构化结果，宁可加横幅提示，也不再多打一次 LLM。
- **失败不抛异常**，统一回 `AnalysisRun(ok=False, error=...)`：
  UI 线程只想拿到「为什么失败 + 该怎么办」，不想处理栈。
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field

from app.analysis import parse_output as po
from app.analysis import prompt_builder as pb
from app.analysis import questions as q
from app.analysis import schema as sc
from app.analysis.llm_client import LLMClient, LLMError, LLMResult, Usage
from app.analysis.skill_loader import LoadedSkill, SkillLoader, default_loader

DEFAULT_REPAIR = True
# 五步 + 四版话术的 JSON 实测要 3k+ tokens 才写得完（真机实测 4205 字被 1200 截断）。
# 上限给不够的表现**不是**报错，而是「JSON 写了一半」→ 被当成格式错误，极难定位。
DEFAULT_MAX_OUTPUT = 4000
# 被截断时最多放大到这个上限再试一次。推理型模型会把额度花在思考链上，需要留足余量。
DEFAULT_TRUNCATION_CEILING = 8000


@dataclass
class Plan:
    """花钱之前的预检结果，UI 用它显示「将加载什么、大概多少钱」。"""

    ready: bool = False
    reason: str = ""
    question: str = q.DEFAULT_KEY
    label: str = ""
    refs: list[str] = field(default_factory=list)
    missing_refs: list[str] = field(default_factory=list)
    titles: list[str] = field(default_factory=list)
    crisis: list[str] = field(default_factory=list)
    backend: str = ""
    model: str = ""
    notes: list[str] = field(default_factory=list)


@dataclass
class AnalysisRun:
    ok: bool = False
    analysis: po.Analysis = field(default_factory=po.Analysis)
    prompt: pb.Prompt | None = None
    llm: LLMResult | None = None
    error: LLMError | None = None
    elapsed_s: float = 0.0
    calls: int = 0
    usage: Usage = field(default_factory=Usage)
    cost_cny: float | None = None
    warnings: list[str] = field(default_factory=list)
    crisis: list[str] = field(default_factory=list)
    refs: list[str] = field(default_factory=list)
    repaired: bool = False
    enlarged: bool = False        # 是否靠「加大 max_tokens 后重试」才拿到完整结果
    finish_reason: str = ""       # 最后一次调用的 finish_reason（length = 被截断）

    @property
    def mode(self) -> str:
        return self.analysis.mode if self.analysis else po.MODE_DEGRADED

    def headline(self) -> str:
        a = self.analysis
        if not self.ok:
            return f"分析失败：{self.error or '未知原因'}"
        head = f"{po.MODE_LABEL.get(a.mode, a.mode)}　用时 {self.elapsed_s:.1f}s"
        if self.usage.total_tokens:
            head += f"　tokens {self.usage.total_tokens}"
        if self.cost_cny is not None:
            head += f"　约 ¥{self.cost_cny:.4f}"
        if self.repaired:
            head += "　（含一次格式修复）"
        if self.enlarged:
            head += "　（输出超限后已放大上限重试）"
        return head

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "headline": self.headline(),
            "elapsed_s": self.elapsed_s,
            "calls": self.calls,
            "usage": self.usage.to_dict(),
            "cost_cny": self.cost_cny,
            "model": (self.llm.model if self.llm else ""),
            "response_format_tier": (self.llm.response_format_tier if self.llm else ""),
            "repaired": self.repaired,
            "enlarged": self.enlarged,
            "finish_reason": self.finish_reason,
            "crisis": self.crisis,
            "refs": self.refs,
            "warnings": self.warnings,
            "error": ({"code": self.error.code, "message": str(self.error)}
                      if self.error else None),
            "analysis": self.analysis.to_dict(),
        }

    def save_json(self, path) -> None:
        from pathlib import Path

        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")


REPAIR_INSTRUCTION = """你上一条回复**没法直接用**（可能是 JSON 语法坏了，也可能是层级写错）。请重做一次，只输出一个 JSON 对象。

必须满足：
- 不要 markdown 围栏、不要任何解释文字、不要前后缀；
- 外层是一个对象，字段为 steps / scripts / citations / confidence / boundaries；
- **steps 与 scripts 必须平级**——不要把 scripts 嵌在 steps 里面（这是最常见的错法）；
- 所有字符串都在双引号内，不留尾逗号，括号要配对闭合；
- **不要改变结论、事实与话术内容**，这次只修格式。

上次被判不合用的具体原因：
{errors}
"""


class AnalysisEngine:
    def __init__(self, config, loader: SkillLoader | None = None):
        self.config = config
        self.loader = loader or default_loader(config)
        self.repair = bool(config.get("analysis.repair_retry", DEFAULT_REPAIR))

    # --------------------------- 预检 ---------------------------
    def plan(self, question: str | None = None, transcript=None, goal: str = "",
             extra: str = "") -> Plan:
        qtype = q.get(question)
        crisis = q.scan_crisis(transcript.to_text(header=False) if transcript else "", goal, extra)
        refs_extra = q.crisis_refs() if crisis else []
        loaded = self.loader.load(qtype.key, extra=refs_extra)
        client = LLMClient(self.config)
        ready, reason = client.ready()
        p = Plan(
            ready=ready and self.loader.exists(),
            reason=reason if not ready else ("" if self.loader.exists()
                                             else f"找不到 SKILL.md：{self.loader.skill_md_path}"),
            question=qtype.key, label=qtype.label,
            refs=loaded.files, missing_refs=loaded.missing, titles=loaded.titles,
            crisis=crisis, backend=client.base_url, model=client.model,
            notes=list(loaded.warnings),
        )
        if crisis:
            p.notes.append("扫描到安全相关信号：" + "、".join(crisis)
                           + "。本次会强制加载安全类参考，并按紧急例外处理。")
        return p

    def budget_tokens(self, question: str | None) -> int:
        base = int(self.config.get("transcript.token_budget", pb.DEFAULT_TOKEN_BUDGET))
        if q.get(question).wide_context:
            return int(base * float(self.config.get("analysis.wide_context_multiplier", 2.0)))
        return base

    # --------------------------- 主流程 ---------------------------
    def analyze(self, transcript, question: str | None = None, *, goal: str = "",
                emotion: int | None = None, extra: str = "", profile: str = "",
                memory: str = "", cancel: threading.Event | None = None,
                on_delta=None, token_budget: int | None = None) -> AnalysisRun:
        t0 = time.perf_counter()
        run = AnalysisRun()
        if transcript is None or not transcript.messages:
            run.error = LLMError("no_input", "还没有会话记录：先在左栏采集或导入一段聊天记录。")
            return run

        qtype = q.get(question)
        crisis = q.scan_crisis(transcript.to_text(header=False), goal, extra)
        refs_extra = q.crisis_refs() if crisis else []
        loaded = self.loader.load(qtype.key, extra=refs_extra)

        prompt = pb.build(
            transcript, qtype.key, loaded=loaded, goal=goal, emotion=emotion, extra=extra,
            profile=profile, memory=memory, crisis=crisis,
            style=str(self.config.get("analysis.style_note", "") or ""),
            token_budget=int(token_budget or self.budget_tokens(qtype.key)),
        )
        run.prompt = prompt
        run.crisis = crisis
        run.refs = loaded.files
        run.warnings.extend(prompt.warnings)

        client = LLMClient(self.config)
        ready, why = client.ready()
        if not ready:
            run.error = LLMError("no_key" if "Key" in why else "disabled", why)
            run.warnings.extend(_guide(run.error))  # 把「下一步该怎么办」一并带出去
            run.elapsed_s = round(time.perf_counter() - t0, 2)
            return run

        messages = [{"role": "system", "content": prompt.system},
                    {"role": "user", "content": prompt.user}]
        usage = Usage()
        cost: float | None = 0.0

        # 显式传输出上限：默认值如果不够，表现为「JSON 写了一半」，
        # 会被下游当成格式错误，排查成本极高（README E28 就是踩的这个坑）。
        limit = int(self.config.get("llm.max_output_tokens", DEFAULT_MAX_OUTPUT))
        ceiling = int(self.config.get("llm.truncation_retry_max", DEFAULT_TRUNCATION_CEILING))

        try:
            res = client.chat(messages, json_schema=sc.ANALYSIS_SCHEMA, on_delta=on_delta,
                              cancel=cancel, max_tokens=limit)
        except LLMError as e:
            run.error = e
            run.elapsed_s = round(time.perf_counter() - t0, 2)
            run.warnings.extend(_guide(e))
            return run

        run.calls += 1
        run.llm = res
        run.finish_reason = res.finish_reason
        usage = _add_usage(usage, res.usage)
        cost = _add_cost(cost, res.cost_cny)
        run.warnings.extend(res.warnings)

        analysis = po.parse(res.text, prompt.included_ids)
        cancelled = cancel is not None and cancel.is_set()

        # ① 截断 → 先救**长度**。
        # 让模型「修 JSON 格式」对截断毫无用处：重发还是同一个上限，
        # 照样在同一个字符位置被切断，那一笔钱纯属白花。
        if res.truncated and not analysis.ok and not cancelled and limit < ceiling:
            bigger = min(limit * 2, ceiling)
            run.warnings.append(
                f"首次输出在 max_tokens={limit} 处被截断，已把上限放大到 {bigger} 重试一次。")
            try:
                res2 = client.chat(messages, json_schema=sc.ANALYSIS_SCHEMA,
                                   on_delta=on_delta, cancel=cancel, max_tokens=bigger)
            except LLMError as e:
                run.warnings.append(f"放大上限后重试失败：{e}")
            else:
                run.calls += 1
                run.llm = res2
                run.finish_reason = res2.finish_reason
                usage = _add_usage(usage, res2.usage)
                cost = _add_cost(cost, res2.cost_cny)
                run.warnings.extend(res2.warnings)
                analysis2 = po.parse(res2.text, prompt.included_ids)
                analysis = analysis2
                res = res2           # 后续修复重试要接在这次（更宽的）输出后面
                limit = bigger
                if analysis2.ok:
                    run.enlarged = True

        # ② 渲不出来才修：能渲染的部分结构化结果不值得再多打一次（多一次调用就多一份钱）。
        # 除了「渲不出来」，**结构错位**（顶层缺 steps 或 scripts）也要修：
        # 那种情况下 `ok` 仍为真，但五步或话术卡会整块消失，用户会以为模型就是没写。
        if self.repair and (not analysis.ok or not analysis.well_formed) and not cancelled:
            fixed = self._try_repair(client, messages, res.text, analysis, prompt,
                                     on_delta=on_delta, cancel=cancel, max_tokens=limit)
            if fixed is not None:
                res2, analysis2 = fixed
                run.calls += 1
                run.finish_reason = res2.finish_reason
                usage = _add_usage(usage, res2.usage)
                cost = _add_cost(cost, res2.cost_cny)
                run.warnings.extend(res2.warnings)
                if analysis2.ok and analysis2.mode != po.MODE_DEGRADED:
                    run.repaired = True
                    analysis = analysis2
                else:
                    run.warnings.append("格式修复重试也没拿到合规 JSON，已按降级处理")

        run.analysis = analysis
        run.usage = usage
        run.cost_cny = cost
        run.elapsed_s = round(time.perf_counter() - t0, 2)
        run.warnings.extend(analysis.warnings)

        if analysis.mode == po.MODE_PARTIAL:
            run.warnings.append("模型漏了字段，未列出的部分已标注为「模型未给出」：" 
                                + "；".join(analysis.errors[:3]))
        if crisis:
            run.warnings.append("本次命中安全信号并按紧急例外处理：" + "、".join(crisis)
                                + "。如果当下有现实危险，请优先联系可信的人与当地紧急服务。")

        run.ok = analysis.ok
        if not run.ok and run.error is None:
            if run.finish_reason == "length":
                # 「长度不够」和「格式写错」是两回事，提示必须说清是哪一种，
                # 否则用户会去改提示词，而真正该动的是 max_output_tokens。
                run.error = LLMError(
                    "truncated",
                    f"模型输出被上限（max_tokens={limit}）截断，JSON 只写了一半。"
                    f"这不是格式问题而是长度不够：请调大 `llm.max_output_tokens`。")
            else:
                run.error = LLMError("bad_output",
                                     "模型没有给出可用的五步分析（JSON 不合规且修复失败）")
        return run

    # --------------------------- 内部 ---------------------------
    def _try_repair(self, client: LLMClient, messages: list[dict], raw: str,
                    analysis: po.Analysis, prompt: pb.Prompt, *, on_delta=None,
                    cancel=None, max_tokens: int | None = None):
        """一次修复重试。返回 (LLMResult, Analysis) 或 None。

        修复请求的输出长度和原请求同级（还是要吐完整的五步 + 四版话术），
        所以必须沿用**已经放大过的**上限，不能退回默认值——否则修复必然再次截断。
        """
        errs = "；".join(analysis.errors or ["JSON 解析失败"]) or "JSON 解析失败"
        repair_msgs = list(messages) + [
            {"role": "assistant", "content": raw[:6000]},
            {"role": "user", "content": REPAIR_INSTRUCTION.format(errors=errs)},
        ]
        try:
            res2 = client.chat(repair_msgs, json_schema=sc.ANALYSIS_SCHEMA,
                               on_delta=on_delta, cancel=cancel, temperature=0.2,
                               max_tokens=max_tokens)
        except LLMError:
            return None
        return res2, po.parse(res2.text, prompt.included_ids)


def _add_usage(a: Usage, b: Usage) -> Usage:
    return Usage(a.prompt_tokens + b.prompt_tokens,
                 a.completion_tokens + b.completion_tokens,
                 a.total_tokens + b.total_tokens)


def _add_cost(a: float | None, b: float | None) -> float | None:
    if a is None or b is None:
        return None  # 有任意一次没配单价就整体不显示金额，别给半截数字
    return round(a + b, 6)


def _guide(e: LLMError) -> list[str]:
    """把错误码翻成「用户下一步该做什么」。"""
    return {
        "no_key": ["去「设置」填 API Key（会加密存进 Windows 凭据），"
                   "或设置环境变量 JEV_LLM_API_KEY 后重启。"],
        "disabled": ["检查「设置」里的 base_url 与模型名。"],
        "timeout": ["请求超时：可换更快的模型，或在设置里调大 request_timeout_s。"],
        "network": ["连不上服务端：检查网络/代理，或确认本地模型（如 Ollama）已启动。"],
        "rate_limit": ["触发限流：等一会儿再试，或把 llm.max_retries 调大。"],
        "quota": ["账户额度不足：去服务商充值，或改用本地模型。"],
        "budget_cap": ["今日费用已达上限：可在设置里调高 daily_cost_cap，或改用本地模型。"],
        "cancelled": ["已取消。"],
        "empty": ["服务端返回空内容：可能是内容被安全策略拦下，换种说法或换模型再试。"],
        # 截断的提示必须指向「调大上限」，而不是「改提示词」或「换模型」——
        # 用户按错误的方向去调只会白费功夫。
        "truncated": ["输出被长度上限截断：把 `llm.max_output_tokens` 调大"
                      "（默认 4000；推理型模型要更大，它会先把额度花在思考上）。"],
    }.get(e.code, [])


__all__ = ["AnalysisEngine", "AnalysisRun", "Plan", "REPAIR_INSTRUCTION"]
