# -*- coding: utf-8 -*-
"""M3 离线单元自检：skill 加载 / 路由 / prompt 预算 / 输出契约 / 解析 / LLM 客户端 / 编排。

不联网、不花钱，全部路径靠本地假服务器（probe/fake_llm.py）覆盖。

    python probe/m3_unit.py
"""
from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.analysis import llm_client as lc  # noqa: E402
from app.analysis import parse_output as po  # noqa: E402
from app.analysis import prompt_builder as pb  # noqa: E402
from app.analysis import questions as q  # noqa: E402
from app.analysis import schema as sc  # noqa: E402
from app.analysis.engine import AnalysisEngine  # noqa: E402
from app.analysis.llm_client import CostTracker, LLMClient, LLMError, Usage  # noqa: E402
from app.analysis.skill_loader import SkillLoader  # noqa: E402
from app.capture.transcript import Message, Transcript  # noqa: E402
from app.config import load_config  # noqa: E402
from fake_llm import Behavior, FakeLLM, write_fake_config  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []
VERBOSE = "-v" in sys.argv or "--verbose" in sys.argv


def check(name: str, cond: bool, detail: str = "") -> bool:
    (PASS if cond else FAIL).append(name)
    if not cond:
        print(f"  ✗ {name}　{detail}")
    elif VERBOSE:
        print(f"  ✓ {name}" + (f"　{detail}" if detail else ""))
    return cond


def eq(name: str, got, want) -> bool:
    return check(name, got == want, f"got={got!r} want={want!r}")


def section(title: str) -> None:
    print(f"\n{title}")


SKILL = SkillLoader(ROOT / "skills" / "goutoujunshi")
OK_JSON = (ROOT / "probe" / "fixtures" / "analysis_ok.json").read_text(encoding="utf-8")


def make_transcript(n: int = 6, title: str = "小新") -> Transcript:
    t = Transcript(source="capture", window={"chat_title": title},
                   speaker_map={"me": {"code": "me", "side": "right", "confirmed": True},
                                "objects": [{"code": "obj-1", "label": "对方", "side": "left"}]})
    for i in range(n):
        t.messages.append(Message(
            id=i + 1, ts=f"2026-09-23T14:{i:02d}:00+08:00", gap_s=30 + i,
            sender="me" if i % 3 == 0 else "obj-1",
            text=f"这是第 {i + 1} 条消息，用来测预算与引用编号。" * 3))
    t.captured_range = [t.messages[0].ts, t.messages[-1].ts]
    return t


# ============================== skill_loader ==============================
def t_skill() -> None:
    section("[1] skill_loader")
    idx = SKILL.index()
    eq("references 索引 43 份", len(idx), 43)
    body = SKILL.skill_md()
    check("SKILL.md 去掉 frontmatter", not body.startswith("---") and "description:" not in body[:200])
    check("SKILL.md 正文含核心原则", "先接住情绪" in body)
    eq("问题类型路由路径全部存在", SKILL.validate_catalog(), [])

    refs, missing = SKILL.route("pua")
    eq("pua 路由到 2 份", len(refs), 2)
    eq("路由没有缺失", missing, [])
    loaded = SKILL.load("reply")
    check("load 带上了 SKILL.md", "SKILL.md（技能总纲" in loaded.text)
    check("load 带上了参考标题", "实战话术编排器" in loaded.text)
    eq("load 记录加载了哪些文件", len(loaded.files), 1)

    bad = SkillLoader(ROOT / "skills" / "goutoujunshi")
    out = bad.load("reply", extra=["references/knowledge/99-不存在.md"])
    check("缺失参考进 missing 并告警", out.missing and out.warnings
          and "不存在" in out.warnings[0], str(out.warnings[:1]))

    small = SkillLoader(ROOT / "skills" / "goutoujunshi", ref_chars=500)
    truncated = small.load("reply")
    check("单份超限会截断并留标记", truncated.truncated and "被截断" in truncated.text)

    tiny = SkillLoader(ROOT / "skills" / "goutoujunshi", ref_chars=500, total_chars=4000)
    none = tiny.load("invite")
    check("总预算用尽后剩下的参考会被跳过并告警",
          len(none.files) < 2 and any("预算已用尽" in w for w in none.warnings),
          f"files={len(none.files)} warns={none.warnings[:1]}")


# ============================== questions ==============================
def t_questions() -> None:
    section("[2] 问题类型与危机扫描")
    check("问题类型 ≥ 8 种", len(q.QUESTION_TYPES) >= 8, str(len(q.QUESTION_TYPES)))
    check("未知 key 退回默认", q.get("不存在的类型").key == q.DEFAULT_KEY)
    refs = q.pick_refs("pua", ["a.md", "b.md"])
    eq("pick_refs 上限 3 份且 extra 优先", len(refs), 3)
    eq("pick_refs 去重", len(q.pick_refs("pua", [q.R_PUA])), 2)

    eq("普通聊天不误报", q.scan_crisis("今天想约她看电影，她说明天有空"), [])
    hits = q.scan_crisis("他昨天动手打我了")
    check("暴力被识别", any("暴力" in h for h in hits), str(hits))
    hits2 = q.scan_crisis("我想死，撑不下去了")
    check("自伤被识别", any("自伤" in h for h in hits2), str(hits2))
    check("危机参考含法律安全", q.R_LAW in q.crisis_refs())


# ============================== prompt_builder ==============================
def t_prompt() -> None:
    section("[3] prompt 组装与预算")
    t = make_transcript(6)
    loaded = SKILL.load("reply")
    p = pb.build(t, "reply", loaded=loaded, goal="想约她看展", emotion=7)
    check("system 含输出契约", "# 输出契约（必须遵守" in p.system)
    check("system 含技能总纲", "SKILL.md（技能总纲" in p.system)
    check("system 含本次任务类型", "本次任务类型：这句怎么回" in p.system)
    check("user 含带编号记录", "#1 " in p.user and "#6 " in p.user)
    check("user 含用户诉求", "想约她看展" in p.user and "7/10" in p.user)
    check("user 含说话人锁定", "我 = right" in p.user)
    eq("全部消息都含入", p.included_ids, [1, 2, 3, 4, 5, 6])
    check("中文字符更贵（同样 8 字）",
          pb.est_tokens("你好世界你好世界") > pb.est_tokens("abcdefgh"),
          f"{pb.est_tokens('你好世界你好世界')} vs {pb.est_tokens('abcdefgh')}")

    big = make_transcript(60)
    p2 = pb.build(big, "reply", loaded=loaded, token_budget=200)
    check("超预算会截断并告警", p2.dropped > 0 and any("超出上下文预算" in w for w in p2.warnings),
          f"dropped={p2.dropped}")
    check("截断保留的是最近 N 条", p2.included_ids[-1] == 60 and p2.included_ids[0] > 1,
          f"{p2.included_ids[:2]}...{p2.included_ids[-1:]}")

    fixed, bad = pb.filter_citations("见 [#2] 与 [#99] 和 [#7]", [2, 7])
    eq("编造引用被替换", bad, ["#99"])
    check("合法引用保留", "[#2]" in fixed and "[#7]" in fixed and "[#99]" not in fixed)

    check("默认类型用默认预算", AnalysisEngine(load_config()).budget_tokens("default") == 3500)
    check("长记录类型预算翻倍", AnalysisEngine(load_config()).budget_tokens("trend") == 7000)

    crisis_prompt = pb.build(t, "reply", loaded=SKILL.load("reply"), crisis=["家暴或人身暴力"])
    check("危机时 system 带紧急例外", "紧急例外" in crisis_prompt.system and "110" in crisis_prompt.system)


# ============================== schema / parse ==============================
def t_contract() -> None:
    section("[4] 输出契约与解析")
    d = json.loads(OK_JSON)
    eq("合规样例零硬错误", sc.validate(d), [])
    check("合规样例仅有可接受的软提醒", len(sc.lint(d, [1, 2, 3, 5, 6])) == 0,
          str(sc.lint(d, [1, 2, 3, 5, 6])))

    broken = json.loads(OK_JSON)
    del broken["scripts"]["primary"]["text"]
    check("缺成品话术被拦下", any("primary.text" in e for e in sc.validate(broken)))
    broken2 = json.loads(OK_JSON)
    broken2["confidence"] = "非常确定"
    check("非法置信度被拦下", any("confidence" in e for e in sc.validate(broken2)))
    broken3 = json.loads(OK_JSON)
    broken3["steps"]["facts"]["known"] = ["没有引用编号的事实"]
    check("无引用编号只算软提醒", not sc.validate(broken3)
          and any("没有以 [#id] 开头" in w for w in sc.lint(broken3)))

    a = po.parse(OK_JSON, [1, 2, 3, 5, 6])
    eq("纯 JSON → json", a.mode, po.MODE_JSON)
    check("解析后可渲染", a.ok and len(a.primary_text()) > 10)

    fenced = "好的，我来分析：\n```json\n" + OK_JSON + "\n```\n希望有帮助。"
    a2 = po.parse(fenced, [1, 2, 3, 5, 6])
    eq("围栏+前后文字 → repaired", a2.mode, po.MODE_REPAIRED)
    check("提取后仍可渲染", a2.ok)

    # 在最后一个 } 前塞一个尾逗号（模型最常见的格式毛病）
    comma = OK_JSON.rstrip()
    comma = comma[:-1].rstrip() + ",}"
    a3 = po.parse(comma, [1, 2, 3, 5, 6])
    eq("尾逗号被修复", a3.mode, po.MODE_REPAIRED)
    check("修复后能解析且字段齐", a3.data.get("confidence") == "medium"
          and not sc.validate(a3.data))

    a4 = po.parse(OK_JSON[:len(OK_JSON) // 2], [1])
    eq("截断 JSON → degraded", a4.mode, po.MODE_DEGRADED)
    check("降级保留原文", a4.raw and not a4.ok)
    check("降级页把原文放第一页", "「我喜欢你」" in a4.steps_markdown()[0])
    eq("降级仍是五页/四页", (len(a4.steps_markdown()), len(a4.scripts_markdown())), (5, 4))

    wrapped = po.parse(json.dumps({"analysis": json.loads(OK_JSON)}, ensure_ascii=False),
                       [1, 2, 3, 5, 6])
    check("外面包一层信封也能认出来", wrapped.mode == po.MODE_JSON and wrapped.ok, wrapped.mode)

    partial = json.loads(OK_JSON)
    del partial["steps"]["actions"]
    a5 = po.parse(json.dumps(partial, ensure_ascii=False), [1, 2, 3, 5, 6])
    eq("缺字段 → partial", a5.mode, po.MODE_PARTIAL)
    check("partial 仍能出话术卡", a5.ok and a5.primary_text())

    withbad = json.loads(OK_JSON)
    withbad["steps"]["facts"]["known"][0] = "[#77] 引用不存在的一条"
    a6 = po.parse(json.dumps(withbad, ensure_ascii=False), [1, 2, 3, 5, 6])
    eq("编造引用被记录", a6.citations_dropped, ["#77"])
    check("正文里替换成 #?", "#77" not in a6.steps_markdown()[1]
          and "[#?]" in a6.steps_markdown()[1])

    eq("括号扫描跳过字符串里的花括号", po.find_json_object('x {"a": "}{"} y'), '{"a": "}{"}')
    eq("取不到对象返回 None", po.find_json_object("完全没有"), None)
    check("flat_text 含五步与话术卡",
          "【① 情绪落地】" in a.flat_text() and "【话术卡】" in a.flat_text())


# ============================== 活人感（话术清洗 + 口吻样本） ==============================
def t_humanize() -> None:
    section("[4b] 活人感：话术收尾清洗 + 口吻样本")
    eq("剥外层引号", po.clean_script_text('"明天有空吗"'), "明天有空吗")
    eq("剥编号列表", po.clean_script_text("1. 明天有空吗"), "明天有空吗")
    eq("剥 me: 前缀", po.clean_script_text("me: 别急 我看这速度今晚能聊到天亮"),
       "别急 我看这速度今晚能聊到天亮")
    eq("句尾句号剥掉", po.clean_script_text("知道了。"), "知道了")
    eq("？！～ 是语气不剥", po.clean_script_text("真的吗？"), "真的吗？")
    eq("正文以数字开头的不误剥", po.clean_script_text("3、2、1 上号"), "3、2、1 上号")
    eq("正文内层的引号不动", po.clean_script_text('他说"明天见"，我回：好'),
       '他说"明天见"，我回：好')

    d = json.loads(OK_JSON)
    d["scripts"]["primary"]["text"] = '"在忙吗"'
    d["scripts"]["variants"]["steady"] = "1、先问问在不在。"
    a = po.parse(json.dumps(d, ensure_ascii=False), [1, 2, 3, 5, 6])
    eq("解析时清洗成品", a.primary_text(), "在忙吗")
    eq("variants 也清洗", a.variant_text("steady"), "先问问在不在")
    check("清洗有告警留痕", any("口语化清洗" in w for w in a.warnings), str(a.warnings[:2]))

    # 口吻样本：me 的短文字消息进样本；长句/链接/非文字/对方消息不进
    t = Transcript(source="capture", window={"chat_title": "小新"},
                   speaker_map={"me": {"code": "me", "side": "right", "confirmed": True},
                                "objects": [{"code": "obj-1", "label": "对方", "side": "left"}]})
    for i, (sender, typ, text) in enumerate([
            ("me", "text", "笑死"), ("obj-1", "text", "你干嘛"),
            ("me", "text", "在忙吗 https://x.com/a"),
            ("me", "text", "长" * 61), ("me", "image", "[图片]"),
            ("me", "text", "那行 明天见")]):
        t.messages.append(Message(id=i + 1, ts=f"2026-09-23T15:{i:02d}:00+08:00",
                                  gap_s=60, sender=sender, type=typ, text=text))
    eq("口吻样本只挑 me 的短文字（最近的在前）",
       pb.voice_samples(t), ["那行 明天见", "笑死"])
    eq("样本不足 2 条不给", pb.voice_samples(make_transcript(6)), [])

    p = pb.build(t, "reply", loaded=SKILL.load("reply"), style="说话直接，几乎不打句号")
    check("user 带口吻样本", "我平时是这么说话的" in p.user and "那行 明天见" in p.user)
    check("user 带说话风格", "我对自己口吻的描述：说话直接" in p.user)
    check("契约含活人感规则", "就是用户本人" in p.system and "不排比" in p.system)
    p2 = pb.build(make_transcript(6), "reply", loaded=SKILL.load("reply"))
    check("没有样本/风格就不给对应小节",
          "我平时是这么说话的" not in p2.user and "我对自己口吻的描述" not in p2.user)


# ============================== llm_client ==============================
def t_client() -> None:
    section("[5] LLM 客户端（本地假服务器）")
    cfg = load_config()
    msgs = [{"role": "user", "content": "hi"}]

    # 未配置密钥
    cfg.set("llm.api_key_ref", "env:JEV_DEFINITELY_MISSING", force=True)
    cfg.set("llm.base_url", "http://127.0.0.1:1/v1", force=True)
    c0 = LLMClient(cfg)
    ready, why = c0.ready()
    check("缺 Key 时 ready=False 且给原因", not ready and "Key" in why, why)
    try:
        c0.chat(msgs)
        check("缺 Key 时调用抛错", False)
    except LLMError as e:
        eq("缺 Key 的错误码", e.code, "no_key")

    with FakeLLM([Behavior(kind="json", body='{"a":1}')]) as s:
        write_fake_config(cfg, s.base_url, stream=False)
        c = LLMClient(cfg)
        r = c.chat(msgs)
        eq("非流式取到正文", r.text, '{"a":1}')
        eq("usage 被解析", r.usage.total_tokens, 1500)
        eq("未传 schema 时档位是 prompt_only", r.response_format_tier, lc.TIER_PROMPT_ONLY)

        r2 = c.chat(msgs, json_schema=sc.ANALYSIS_SCHEMA)
        eq("支持 json_schema 的服务端用最严档", r2.response_format_tier, lc.TIER_JSON_SCHEMA)
        check("请求里真的带了 schema",
              (s.calls[-1].get("response_format") or {}).get("type") == "json_schema")

    with FakeLLM([Behavior(kind="sse", body=OK_JSON)]) as s:
        write_fake_config(cfg, s.base_url, stream=True)
        got: list[str] = []
        c = LLMClient(cfg)
        r = c.chat(msgs, on_delta=got.append)
        check("流式收到多块增量", len(got) >= 3, f"{len(got)} 块")
        eq("流式拼接结果完整", r.text, OK_JSON)
        check("流式标记为 streamed", r.streamed)

    with FakeLLM([Behavior(kind="json", body='{"ok":1}',
                           reject_formats=("json_schema", "json_object"))]) as s:
        write_fake_config(cfg, s.base_url, stream=False)
        c = LLMClient(cfg)
        r = c.chat(msgs, json_schema={"type": "object"})
        eq("阶梯降到 prompt_only", r.response_format_tier, lc.TIER_PROMPT_ONLY)
        eq("阶梯请求了 3 次", len(s.calls), 3)
        eq("三次档位依次变松",
           [(x.get("response_format") or {}).get("type") for x in s.calls],
           ["json_schema", "json_object", None])
        check("降档有告警", any("不支持原生 JSON Schema" in w for w in r.warnings))

    with FakeLLM([Behavior(kind="status", status=400, detail="max_tokens must be <= 8192")]) as s:
        write_fake_config(cfg, s.base_url, stream=False)
        c = LLMClient(cfg)
        try:
            c.chat(msgs, json_schema={"type": "object"})
            check("无关 400 应原样报错", False)
        except LLMError as e:
            eq("无关 400 的错误码", e.code, "http")
            eq("无关 400 不降档（只请求 1 次）", len(s.calls), 1)

    with FakeLLM([Behavior(kind="status", status=503, detail="overloaded")]) as s:
        write_fake_config(cfg, s.base_url, stream=False)
        lc.RETRY_BACKOFF_S = 0.05  # 测试里不真等 1.5 秒
        cfg.set("llm.max_retries", 1, force=True)
        c = LLMClient(cfg)
        try:
            c.chat(msgs)
            check("5xx 应该抛错", False)
        except LLMError as e:
            eq("5xx 错误码", e.code, "http")
        eq("5xx 按 max_retries 重试了一次", len(s.calls), 2)
        cfg.set("llm.max_retries", 0, force=True)

    with FakeLLM([Behavior(kind="status", status=429, detail="rate limited")]) as s:
        write_fake_config(cfg, s.base_url, stream=False)
        try:
            LLMClient(cfg).chat(msgs)
            check("429 应抛错", False)
        except LLMError as e:
            eq("429 错误码", e.code, "rate_limit")

    with FakeLLM([Behavior(kind="status", status=404, detail="not found")]) as s:
        cfg.set("llm.base_url", s.base_url.replace("/v1", ""), force=True)
        cfg.set("llm.api_key_ref", "env:JEV_FAKE_KEY", force=True)
        try:
            LLMClient(cfg).chat(msgs)
            check("404 应抛错", False)
        except LLMError as e:
            check("404 提示检查 /v1", "/v1" in e.message, e.message)

    with FakeLLM([Behavior(kind="json", body='{"x":1}', hang=6.0)]) as s:
        write_fake_config(cfg, s.base_url, stream=False)
        c = LLMClient(cfg)
        c.timeout = 1.5
        try:
            c.chat(msgs)
            check("超时应抛错", False)
        except LLMError as e:
            eq("超时错误码", e.code, "timeout")

    # 取消：流式生成中途按住（非流式在等响应头期间没有插入点，见 llm_client 的 docstring）
    with FakeLLM([Behavior(kind="sse", body=OK_JSON, stall_after=2, stall_for=30.0)]) as s:
        write_fake_config(cfg, s.base_url, stream=True)
        c = LLMClient(cfg)
        ev = threading.Event()
        threading.Timer(0.5, ev.set).start()
        try:
            c.chat(msgs, cancel=ev)
            check("流式取消应抛错", False)
        except LLMError as e:
            eq("流式取消错误码", e.code, "cancelled")

    with FakeLLM([Behavior(kind="json", body='{"x":1}')]) as s:
        write_fake_config(cfg, s.base_url, stream=False)
        ev0 = threading.Event()
        ev0.set()
        try:
            LLMClient(cfg).chat(msgs, cancel=ev0)
            check("调用前取消应抛错", False)
        except LLMError as e:
            eq("调用前取消的错误码", e.code, "cancelled")
        eq("调用前取消不该发出请求", len(s.calls), 0)

    eq("按单价算费用", lc.estimate_cost(Usage(1_000_000, 1_000_000, 2_000_000), "m",
                                        {"*": {"in_cny_per_mtok": 2, "out_cny_per_mtok": 8}}), 10.0)
    eq("没配单价返回 None", lc.estimate_cost(Usage(1000, 1000, 2000), "m", None), None)
    eq("模型名匹配优先于通配",
       lc.estimate_cost(Usage(1_000_000, 0, 1_000_000), "deepseek-chat",
                        {"deepseek": {"in_cny_per_mtok": 1, "out_cny_per_mtok": 0},
                         "*": {"in_cny_per_mtok": 9, "out_cny_per_mtok": 9}}), 1.0)

    # 每日上限
    import tempfile

    d = Path(tempfile.mkdtemp(prefix="jev-cost-"))
    tr = CostTracker(d, cap_cny=0.01)
    tr.add(0.02, 100)
    try:
        tr.check(True)
        check("超上限应抛 budget_cap", False)
    except LLMError as e:
        eq("超上限错误码", e.code, "budget_cap")
    check("未配单价时不拦（无法判定金额）", tr.check(False) is None)
    eq("累计写入可读回", round(tr.spent_cny(), 3), 0.02)


# ============================== engine ==============================
def t_engine() -> None:
    section("[6] engine 编排（假服务器）")
    cfg = load_config()
    t = make_transcript(6)

    with FakeLLM([Behavior(kind="json", body=OK_JSON)]) as s:
        write_fake_config(cfg, s.base_url, stream=False,
                          pricing={"*": {"in_cny_per_mtok": 2, "out_cny_per_mtok": 8}})
        e = AnalysisEngine(cfg)
        plan = e.plan("reply", t, goal="想约她看展")
        check("预检就绪并列出参考", plan.ready and len(plan.refs) == 1, str(plan.refs))
        run = e.analyze(t, "reply", goal="想约她看展", emotion=6)
        check("正常一轮 ok", run.ok and run.mode == po.MODE_JSON)
        eq("只调了一次", run.calls, 1)
        eq("用量记到 run", run.usage.total_tokens, 1500)
        check("费用按单价算出", run.cost_cny and run.cost_cny > 0, str(run.cost_cny))
        check("引用编号是本次真给出去的", all(i in range(1, 7) for i in
                                     [int(x[2:-1]) for x in run.analysis.citations]),
              str(run.analysis.citations))
        check("headline 可读", "结构化输出" in run.headline(), run.headline())
        check("run.to_dict 可序列化", json.dumps(run.to_dict(), ensure_ascii=False)[:10] == '{"ok": true'[:10])

    with FakeLLM([Behavior(kind="json", body="我不太想输出 JSON。"),
                  Behavior(kind="json", body=OK_JSON)]) as s:
        write_fake_config(cfg, s.base_url, stream=False)
        run = AnalysisEngine(cfg).analyze(t, "reply")
        check("渲不出来时会修一次并成功", run.ok and run.repaired)
        eq("修复后共 2 次调用", run.calls, 2)

    with FakeLLM([Behavior(kind="json", body="我就不输出 JSON")]) as s:
        write_fake_config(cfg, s.base_url, stream=False)
        run = AnalysisEngine(cfg).analyze(t, "reply")
        check("两次都不合规 → 降级且不算 ok", not run.ok and run.mode == po.MODE_DEGRADED)
        eq("降级仍保留原文", bool(run.analysis.raw), True)
        check("错误码是 bad_output", run.error and run.error.code == "bad_output")
        eq("降级也调了 2 次（含修复）", run.calls, 2)

    with FakeLLM([Behavior(kind="json", body=OK_JSON)]) as s:
        write_fake_config(cfg, s.base_url, stream=False)
        run = AnalysisEngine(cfg).analyze(t, "reply", extra="他昨天动手打我了")
        check("危机命中并强制加载安全参考",
              run.crisis and q.R_LAW in run.refs and len(run.refs) <= 3, str(run.refs))
        check("危机提示进了 warnings", any("安全信号" in w for w in run.warnings))
        check("危机时 system 里有紧急例外",
              "紧急例外" in (run.prompt.system if run.prompt else ""))

    with FakeLLM([Behavior(kind="json", body=OK_JSON)]) as s:
        write_fake_config(cfg, s.base_url, stream=False)
        run = AnalysisEngine(cfg).analyze(Transcript(), "reply")
        check("空 transcript → no_input", not run.ok and run.error
              and run.error.code == "no_input")
        eq("空输入不该发起调用", len(s.calls), 0)

    with FakeLLM([Behavior(kind="json", body=OK_JSON)]) as s:
        write_fake_config(cfg, s.base_url, stream=False)
        big = make_transcript(80)
        run = AnalysisEngine(cfg).analyze(big, "trend")
        check("长记录类型用了更大预算", run.prompt and run.prompt.dropped == 0
              and len(run.prompt.included_ids) == 80,
              f"included={len(run.prompt.included_ids) if run.prompt else 0}")
        run2 = AnalysisEngine(cfg).analyze(big, "reply")
        check("普通类型会按预算截断", run2.prompt and run2.prompt.dropped > 0,
              f"dropped={run2.prompt.dropped if run2.prompt else '?'}")

    with FakeLLM([Behavior(kind="json", body=OK_JSON)]) as s:
        cfg.set("llm.api_key_ref", "env:JEV_DEFINITELY_MISSING", force=True)
        cfg.set("llm.base_url", s.base_url, force=True)
        run = AnalysisEngine(cfg).analyze(t, "reply")
        check("缺 Key 时给可操作引导", not run.ok and run.error.code == "no_key"
              and any("设置" in w for w in run.warnings), str(run.warnings[:1]))
        eq("缺 Key 不发起调用", len(s.calls), 0)

    with FakeLLM([Behavior(kind="json", body=OK_JSON, reject_formats=("json_schema",))]) as s:
        write_fake_config(cfg, s.base_url, stream=False)
        run = AnalysisEngine(cfg).analyze(t, "reply")
        check("服务端不支持 schema 也能出结果", run.ok and run.llm.response_format_tier
              == lc.TIER_JSON_OBJECT, run.llm.response_format_tier if run.llm else "?")


# 真机（deepseek-chat）第一次跑就撞出来的三种坏输出。原来的假服务器只会返回
# 「永远合规的小 JSON」，所以这些路径一条都没被覆盖到——正是它们让 M3 真机第一次
# 跑出「部分结构化」而没人知道为什么。
_REAL = json.loads(OK_JSON)
# ① 平级结构，只少写一个外层 `}`（模型手滑最常见）
FLAT_MINUS_BRACE = json.dumps(_REAL, ensure_ascii=False)[:-1]
# ② scripts / citations / confidence / boundaries 全被塞进 steps 里面，JSON 本身合法
_NESTED = {"steps": {**_REAL["steps"], "scripts": _REAL["scripts"],
                     "citations": _REAL.get("citations"),
                     "confidence": _REAL.get("confidence"),
                     "boundaries": _REAL.get("boundaries")}}
NESTED_SCRIPTS = json.dumps(_NESTED, ensure_ascii=False)
# ③ 错位 + 少一个 `}`：真机就是这一种，也是最能骗过旧解析器的一种
NESTED_MINUS_BRACE = NESTED_SCRIPTS[:-1]


def t_realworld() -> None:
    section("[7] 真机踩出来的坑（截断 / 结构错位 / 碎片捞取）")

    # ---- ① 补未闭合括号：零成本救回完整结果 ----
    a = po.parse(FLAT_MINUS_BRACE, allowed_ids=[1, 2, 3, 4, 5, 6])
    check("少一个 } 时能补括号救回完整结果", a.ok and a.well_formed,
          f"ok={a.ok} well={a.well_formed} mode={a.mode}")
    eq("补括号会留痕（不假装是原样）", a.mode, po.MODE_REPAIRED)

    # ---- ② 结构错位必须被识别出来，不能只看「有没有一条话术」----
    # 合法但层级写错（数据是完整的，只是放错了位置）→ partial + 明确指出位置错
    rn = po.parse(NESTED_SCRIPTS, allowed_ids=[1, 2, 3, 4, 5, 6])
    check("合法但错位：well_formed=False 且 ok=False",
          not rn.well_formed and not rn.ok, f"ok={rn.ok} well={rn.well_formed}")
    check("合法但错位：报错直指「必须与 steps 平级」",
          any("平级" in e for e in rn.errors), str(rn.errors[:2]))

    # 错位 + 少 `}`（真机实况）：原输出本身就是残缺的 → 按纯文本降级。
    # 关键：**不拿补出来的残缺结构去渲染**，否则会得到一屏「模型未给出」，
    # 而模型真正写出来的内容反而被丢掉。
    rb = po.parse(NESTED_MINUS_BRACE, allowed_ids=[1, 2, 3, 4, 5, 6])
    check("残缺输出按纯文本降级，且不留骨架数据",
          rb.mode == po.MODE_DEGRADED and not rb.ok and rb.data == {},
          f"mode={rb.mode} data={list(rb.data.keys())}")
    check("降级保留模型原文", bool(rb.raw.strip()))

    # ---- ③ 不再退而求其次捞内部碎片 ----
    # 旧行为：外层括号不平衡时会往后找下一个 `{`，捞回一个「括号平衡但语义错位」的
    # 片段；② 那种输入捞回来的片段恰好带 scripts.primary.text，于是被当成「有结果」，
    # 修复重试反被绕过。现在整段本来就是 JSON 的情况下不再往后找。
    frag = po.find_json_object(NESTED_MINUS_BRACE)
    frag_keys: list = ["<没捞到>"]
    if frag:
        try:
            frag_keys = list(json.loads(po._close_brackets(frag)).keys())
        except (ValueError, TypeError):
            frag_keys = ["<无法解析>"]
    check("find_json_object 单独用时确实会捞到错位片段（顶层没有 steps）",
          "steps" not in frag_keys, f"捞到的顶层键={frag_keys[:6]}")

    # ---- ④ _close_brackets 的边界 ----
    eq("已平衡的内容原样返回", po._close_brackets('{"a":1}'), '{"a":1}')
    eq("字符串里的花括号不参与配对", po._close_brackets('{"a":"}"}'), '{"a":"}"}')
    eq("补被切断的字符串", po._close_brackets('{"a":"没写完'), '{"a":"没写完"}')
    eq("补嵌套括号", po._close_brackets('{"a":[1,{"b":2'), '{"a":[1,{"b":2}]}')
    eq("转义引号不误判", po._close_brackets('{"a":"\\""}'), '{"a":"\\""}')

    # ---- ⑤ finish_reason 必须一路透传（漏传过一次，截断检测全失效）----
    cfg = load_config()
    cfg.set("llm.max_output_tokens", 4000, force=True)
    cfg.set("llm.truncation_retry_max", 8000, force=True)
    t = make_transcript(6)
    cut = OK_JSON[:150]          # 半截 JSON

    for stream in (False, True):
        with FakeLLM([Behavior(kind="sse" if stream else "json", body=cut,
                               finish_reason="length")]) as s:
            write_fake_config(cfg, s.base_url, stream=stream)
            res = LLMClient(cfg).chat([{"role": "user", "content": "x"}], stream=stream)
            tag = "流式" if stream else "非流式"
            check(f"{tag}：finish_reason=length 能透传到 LLMResult", res.truncated,
                  f"finish={res.finish_reason!r}")
            check(f"{tag}：截断会给出明确告警", any("截断" in w for w in res.warnings),
                  str(res.warnings))

    with FakeLLM([Behavior(kind="json", body="   ", finish_reason="length")]) as s:
        write_fake_config(cfg, s.base_url, stream=False)
        try:
            LLMClient(cfg).chat([{"role": "user", "content": "x"}])
            check("空内容 + length 应当报错", False)
        except LLMError as e:
            eq("空内容 + length 报 truncated 而不是 empty", e.code, "truncated")

    # ---- ⑥ 截断 → 放大上限重试（而不是傻乎乎去「修 JSON 格式」）----
    with FakeLLM([Behavior(kind="json", body=cut, finish_reason="length"),
                  Behavior(kind="json", body=OK_JSON)]) as s:
        write_fake_config(cfg, s.base_url, stream=False)
        run = AnalysisEngine(cfg).analyze(t, "reply")
        check("截断后自动放大上限重试", run.enlarged and run.calls == 2,
              f"enlarged={run.enlarged} calls={run.calls}")
        check("放大重试后拿到完整结果", run.ok and run.analysis.well_formed,
              f"ok={run.ok} well={run.analysis.well_formed}")
        eq("第二次调用确实用了更大的 max_tokens", s.calls[-1].get("max_tokens"), 8000)
        check("放大重试有告警留痕", any("放大" in w for w in run.warnings),
              str(run.warnings[:3]))

    # ---- ⑦ 结构错位 → 触发一次修复重试 ----
    with FakeLLM([Behavior(kind="json", body=NESTED_SCRIPTS),
                  Behavior(kind="json", body=OK_JSON)]) as s:
        write_fake_config(cfg, s.base_url, stream=False)
        run = AnalysisEngine(cfg).analyze(t, "reply")
        check("结构错位会触发修复重试", run.repaired and run.calls == 2,
              f"repaired={run.repaired} calls={run.calls}")
        check("修复后是完整结构", run.ok and run.analysis.well_formed,
              f"ok={run.ok} well={run.analysis.well_formed}")

    # ---- ⑧ 截断一次就够：别在放大也救不回来时反复烧钱 ----
    with FakeLLM([Behavior(kind="json", body=cut, finish_reason="length")]) as s:
        write_fake_config(cfg, s.base_url, stream=False)
        run = AnalysisEngine(cfg).analyze(t, "reply")
        check("一直截断时调用次数有上限（放大 1 次 + 修复 1 次）", run.calls <= 3,
              f"calls={run.calls}")
        check("一直截断时不谎报成功", not run.ok, f"ok={run.ok}")


def main() -> int:
    os.environ.setdefault("JEV_FAKE_KEY", "test-key")
    print("M3 离线单元自检")
    t_skill()
    t_questions()
    t_prompt()
    t_contract()
    t_humanize()
    t_client()
    t_engine()
    t_realworld()
    print(f"\n结果：通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败项：" + "、".join(FAIL))
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
