# -*- coding: utf-8 -*-
"""M4 离线单元自检：记忆封装层 / 官方契约 / 同意门控 / 提取 / 封顶 / 撤销。

不联网、不花钱。**全程写在临时目录里**（`GOUTOUJUNSHI_MEMORY_DIR` 指向 tempfile），
末尾还会断言用户真实的记忆目录没有被动过 —— 自检绝不该拿用户的数据做实验。

    python probe/m4_unit.py
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 这一行必须在 import app.* 之前：所有路径解析都读环境变量
_TMP = Path(tempfile.mkdtemp(prefix="jev-m4-unit-"))
os.environ["GOUTOUJUNSHI_MEMORY_DIR"] = str(_TMP / "memory")
os.environ["JEV_DATA_DIR"] = str(_TMP / "data")
os.environ.setdefault("JEV_FAKE_KEY", "test-key")

from app.analysis import parse_output as po  # noqa: E402
from app.config import Config  # noqa: E402
from app.memory import consent as mconsent  # noqa: E402
from app.memory import extract as mex  # noqa: E402
from app.memory import service as msvc  # noqa: E402
from app.memory import store as mstore  # noqa: E402

SCRIPT = ROOT / "skills" / "goutoujunshi" / "scripts" / "memory_store.py"
FIXTURE = ROOT / "probe" / "fixtures" / "analysis_ok.json"

PASS: list[str] = []
FAIL: list[str] = []
VERBOSE = "-v" in sys.argv or "--verbose" in sys.argv

# 用户真实记忆目录在跑之前存不存在（跑完必须还是原样）
_REAL_DIR = Path(os.environ.get("LOCALAPPDATA") or str(Path.home())) / "goutoujunshi"
_REAL_BEFORE = _REAL_DIR.exists()


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


def fresh_db() -> None:
    """每个大节换一个干净的库，免得前面的数据影响后面的封顶断言。"""
    shutil.rmtree(os.environ["GOUTOUJUNSHI_MEMORY_DIR"], ignore_errors=True)
    Path(os.environ["GOUTOUJUNSHI_MEMORY_DIR"]).mkdir(parents=True, exist_ok=True)
    for name in ("-wal", "-shm"):
        p = Path(str(mstore.db_path()) + name)
        p.unlink(missing_ok=True)


def w(scope: str, field: str, value: str, *, subject: str = "obj-1",
      source: str = "", ref: str = "", occurred: str = ""):
    """直接拼一条 delta 写入（绕开 UI，测官方契约本身）。"""
    delta = {"scope": scope, "subject_id": subject, "field": field, "value": value,
             "source_type": source or _default_source(scope), "confidence": "medium"}
    if ref:
        delta["source_ref"] = ref
    if occurred:
        delta["occurred_at"] = occurred
    return mstore.apply(SCRIPT, delta)


def _default_source(scope: str) -> str:
    return {"user": "user_explicit", "object": "user_report",
            "relationship": "user_report", "event": "chatlab",
            "hypothesis": "assistant_inference"}.get(scope, "user_explicit")


class _Msg:
    def __init__(self, mid: int, ts: str):
        self.id, self.ts = mid, ts


class _Tx:
    messages = [_Msg(2, "2026-09-20T21:12:00+08:00"), _Msg(6, "2026-09-21T09:00:00+08:00")]


class _Run:
    def __init__(self, analysis):
        self.analysis = analysis


# ============================== [1] 封装层与官方契约 ==============================
def t_store() -> None:
    section("[1] 封装层与官方契约")
    rules = mstore.load_official(SCRIPT)
    check("官方脚本可当模块加载（用于预检）", rules.available, rules.error)
    eq("策略版本取自官方", rules.policy_version, "1")
    eq("scope 五类", sorted(rules.scope_limits), ["event", "hypothesis", "object",
                                                "relationship", "user"])
    eq("单条上限取自官方", rules.max_value_chars, 200)
    eq("source_type 五类", sorted(rules.source_types),
       ["assistant_inference", "chatlab", "tool", "user_explicit", "user_report"])

    # 官方在库不存在时**不返回 memory_count**：status_dict 必须补齐，否则界面显示 None
    fresh_db()
    st, err = mstore.status_dict(SCRIPT)
    check("status 在库不存在时仍成功", st["ok"] and err == "", err)
    eq("库不存在时 exists=False", st["exists"], False)
    eq("库不存在时条数补 0（不是 None）", st["memory_count"], 0)
    eq("库不存在时撤销栈补 0", st["undo_count"], 0)
    check("status 报了库的绝对路径", str(st["path"]).endswith("memory.sqlite3"), st["path"])

    # 错误分类：NO_SCRIPT / TIMEOUT / 官方错误信封
    res = mstore.run_cli(_TMP / "nope.py", ["status"])
    eq("脚本不存在 → NO_SCRIPT", res.code, "NO_SCRIPT")
    check("NO_SCRIPT 有可操作提示", bool(res.hint))

    slow = _TMP / "slow.py"
    slow.write_text("import time; time.sleep(5)\n", encoding="utf-8")
    res = mstore.run_cli(slow, [], timeout=0.7)
    eq("超时 → TIMEOUT", res.code, "TIMEOUT")

    envelope = _TMP / "die.py"
    envelope.write_text(
        "import json,sys\n"
        "print(json.dumps({'ok': False, 'error': {'code': 'MEMORY_PAUSED',"
        " 'message': '长期记忆当前已暂停'}}, ensure_ascii=False))\n"
        "sys.exit(1)\n", encoding="utf-8")
    res = mstore.run_cli(envelope, [])
    check("退出码 1 但错误码从 stdout 信封里捞出来", not res.ok and res.code == "MEMORY_PAUSED"
          and res.message == "长期记忆当前已暂停", f"{res.code}/{res.message}")
    check("错误码有对应的人话提示", "暂停" in res.hint, res.hint)

    garbage = _TMP / "garbage.py"
    garbage.write_text("print('这不是 JSON')\n", encoding="utf-8")
    res = mstore.run_cli(garbage, [])
    check("退出码 0 但输出不是 JSON → 算失败（BAD_OUTPUT），不假装成功",
          not res.ok and res.code == "BAD_OUTPUT", f"{res.ok}/{res.code}")

    # 破坏性操作必须显式带 --confirm（不带就是 CONFIRMATION_REQUIRED）
    for name, fn in (("enable", lambda: mstore.enable(SCRIPT, confirm=False)),
                     ("clear", lambda: mstore.clear(SCRIPT, confirm=False)),
                     ("revoke", lambda: mstore.revoke(SCRIPT, confirm=False)),
                     ("forget-object", lambda: mstore.forget_object(SCRIPT, "obj-1",
                                                                   confirm=False))):
        r = fn()
        eq(f"{name} 不带 --confirm 会被拒", r.code, "CONFIRMATION_REQUIRED")

    # 中文往返不歪（子进程 stdout 编码踩过的坑）
    mstore.enable(SCRIPT, confirm=True)
    r = w("hypothesis", "中文键", "中文值：可能是在回避表态，不一定是没兴趣", subject="obj-1")
    check("中文写入成功", r.ok, r.message)
    rows, _e = mstore.memories(SCRIPT, "obj-1")
    eq("中文读写往返一致", rows[0]["value"] if rows else None,
       "中文值：可能是在回避表态，不一定是没兴趣")

    # show 是 --subject-id 旗标；且会顺带带出 user 档案
    w("user", "我的性格", "慢热", subject="user")
    w("object", "工作", "在深圳做设计", subject="obj-2")
    rows1, _e = mstore.memories(SCRIPT, "obj-1")
    subs = sorted({r["subject_id"] for r in rows1})
    eq("show --subject-id 只回该对象 + user", subs, ["obj-1", "user"])
    rows_all, _e = mstore.memories(SCRIPT)
    eq("show 不带旗标回全部", sorted({r["subject_id"] for r in rows_all}),
       ["obj-1", "obj-2", "user"])


# ============================== [2] 类别 × 来源交叉规则 ==============================
def t_cross() -> None:
    section("[2] 类别 × 来源交叉规则（写入前预检）")
    rules = mstore.load_official(SCRIPT)
    cases = [
        ("user", "user_explicit", True, "用户档案只接受用户明确陈述"),
        ("user", "chatlab", False, "聊天记录不能写进用户档案"),
        ("object", "user_report", True, "对象事实可来自用户转述"),
        ("object", "assistant_inference", False, "模型推断不能写进对象快照"),
        ("relationship", "tool", False, "外部工具不能写进关系快照"),
        ("event", "chatlab", True, "事件可来自聊天记录"),
        ("hypothesis", "assistant_inference", True, "模型推断只能进假设"),
        ("hypothesis", "user_explicit", True, "假设也可以由用户自己提出"),
    ]
    for scope, source, legal, why in cases:
        reason = rules.check_delta({"scope": scope, "subject_id": "obj-1", "field": "f",
                                    "value": "v", "source_type": source})
        eq(f"{scope} × {source} → {'放行' if legal else '拦住'}（{why}）",
           not reason, legal)

    # 字段级规则
    bad = [
        ("未知 scope", {"scope": "nope", "subject_id": "x", "field": "f", "value": "v",
                        "source_type": "user_explicit"}),
        ("空 value", {"scope": "hypothesis", "subject_id": "x", "field": "f", "value": "",
                      "source_type": "assistant_inference"}),
        ("超长 value", {"scope": "hypothesis", "subject_id": "x", "field": "f",
                        "value": "字" * 201, "source_type": "assistant_inference"}),
        ("控制字符", {"scope": "hypothesis", "subject_id": "x", "field": "f",
                      "value": "a\x01b", "source_type": "assistant_inference"}),
        ("未知 confidence", {"scope": "hypothesis", "subject_id": "x", "field": "f",
                             "value": "v", "source_type": "assistant_inference",
                             "confidence": "maybe"}),
    ]
    for name, delta in bad:
        check(f"预检拦住：{name}", bool(rules.check_delta(delta)))
    check("预检不误伤：空 source_ref 是合法的",
          not rules.check_delta({"scope": "hypothesis", "subject_id": "x", "field": "f",
                                 "value": "v", "source_type": "assistant_inference",
                                 "source_ref": ""}))


# ============================== [3] 同意门控 ==============================
def t_consent() -> None:
    section("[3] 同意门控")
    rules = mstore.load_official(SCRIPT)
    mconsent.forget()
    need, why = mconsent.need_consent(rules.policy_version)
    check("没同意过 → 需要同意", need and "没同意过" in why, why)

    c = mconsent.accept(rules.policy_version)
    check("同意记录落了盘", mconsent.load().accepted and c.at, c.at)
    need, _ = mconsent.need_consent(rules.policy_version)
    check("同意过 → 不再问", not need)

    need, why = mconsent.need_consent("999")   # 模拟官方升策略版本
    check("策略版本变了 → 重新问", need and "版本" in why, why)

    p = mconsent.load()
    p.text_hash = "deadbeef"
    mconsent._file().write_text(json.dumps(p.to_dict(), ensure_ascii=False), encoding="utf-8")
    need, why = mconsent.need_consent(rules.policy_version)
    check("说明文案变了 → 重新问", need and "文案" in why, why)

    mconsent.forget()
    need, _ = mconsent.need_consent(rules.policy_version)
    check("撤回同意 → 又开始问", need)

    text = mconsent.policy_text(rules)
    check("说明里写清了库位置", str(mstore.db_dir()) in text)
    check("说明里写清了上限（取自官方常量）", "200" in text and "20" in text)


# ============================== [4] 候选提取 ==============================
def t_extract() -> None:
    section("[4] 候选提取（只产出事件与假设）")
    rules = mstore.load_official(SCRIPT)
    eq("多编号全部剥掉（踩过的坑）", mex.split_cites("[#2] [#3] 对方主动安排见面"),
       ("对方主动安排见面", [2, 3]))
    eq("单编号剥离", mex.split_cites("[#6] 对方说喜欢我"), ("对方说喜欢我", [6]))
    eq("没有编号就原样", mex.split_cites("没有编号的事实"), ("没有编号的事实", []))
    eq("编号不进标签", mex.short_label("[#2] [#3] 对方主动安排见面"), "对方主动安排见面")
    eq("标签在标点处断", mex.short_label("对方主动安排你们见面并确认你有没有车，说明她在往前推"),
       "对方主动安排你们见面并确认你有没有车")
    eq("去掉 markdown 记号", mex.clean("**慢热**，`需要`确定性"), "慢热，需要确定性")
    t, cut = mex.fit_value("字" * 500, rules.max_value_chars)
    check("超长截断到官方上限并留省略号", len(t) == 200 and cut and t.endswith("…"),
          f"len={len(t)} cut={cut}")

    raw = FIXTURE.read_text(encoding="utf-8")
    analysis = po.parse(raw, allowed_ids=[1, 2, 3, 4, 5, 6])
    check("夹具解析成结构化结果", analysis.ok, analysis.mode)

    cands = mex.from_analysis(_Run(analysis), subject_id="obj-1", transcript=_Tx(),
                              rules=rules)
    scopes = [c.scope for c in cands]
    eq("只产出 event 与 hypothesis", sorted(set(scopes)), ["event", "hypothesis"])
    events = [c for c in cands if c.scope == "event"]
    hyps = [c for c in cands if c.scope == "hypothesis"]
    check("已知事实 → 事件", len(events) >= 2, len(events))
    check("推断 → 假设", len(hyps) >= 1, len(hyps))
    check("事件都带证据编号", all(c.source_ref for c in events),
          [c.source_ref for c in events])
    check("带多个编号时全部记下", any(" " in c.source_ref for c in events),
          [c.source_ref for c in events])
    check("事件取到了消息时间（用于去重）", any(c.occurred_at for c in events),
          [(c.source_ref, c.occurred_at) for c in events])
    check("假设一律标 assistant_inference 且带置信度",
          all(c.source_type == "assistant_inference" and c.confidence in
              ("high", "medium", "low") for c in hyps))
    check("候选都没有被预检拦住", all(not c.blocked for c in cands),
          [c.blocked for c in cands if c.blocked])
    check("没有把模型推断塞进档案类",
          not [c for c in cands if c.scope in ("user", "object", "relationship")])

    class _Bad:
        analysis = po.parse("模型今天不想输出 JSON", [])

    if _Bad.analysis.ok:
        _Bad.analysis.mode = po.MODE_DEGRADED
    eq("分析失败/降级 → 不提候选", mex.from_analysis(_Bad(), subject_id="obj-1",
                                                    transcript=_Tx(), rules=rules), [])

    man = mex.manual_candidate("object", "工作", "在深圳做设计", subject_id="obj-1",
                               rules=rules)
    eq("手动填写用 user_explicit（对所有 scope 都合法）", man.source_type, "user_explicit")
    check("手动填写默认通过预检", not man.blocked, man.blocked)
    bad = mex.manual_candidate("object", "工作", "x", subject_id="obj-1", rules=rules)
    bad.source_type = "assistant_inference"   # 人为越界
    mex.mark_blocked([bad], rules=rules)
    check("越界的候选被标记而不是被静默写入", bool(bad.blocked), bad.blocked)


# ============================== [5] 写入与封顶 ==============================
def t_limits() -> None:
    section("[5] 写入与封顶（DoD：超限正确封顶）")
    fresh_db()
    mstore.enable(SCRIPT, confirm=True)

    # event：超过上限自动剪掉最旧的
    for i in range(25):
        r = w("event", f"事件{i}", f"第 {i} 条事件", occurred=f"2026-09-{i % 28 + 1:02d}")
        if not r.ok:
            check(f"第 {i + 1} 条事件能写入", False, f"{r.code} {r.message}")
            break
    st, _ = mstore.status_dict(SCRIPT)
    eq("event 封顶 20 条（超出自动剪最旧）", st["memory_count"], 20)

    rows, _e = mstore.memories(SCRIPT, "obj-1")
    fields = {r["field"] for r in rows}
    check("留下的是最新的 20 条（最旧的 事件0~4 被剪掉）",
          "事件24" in fields and "事件0" not in fields, sorted(fields)[:5])

    # hypothesis：上限 5，同样自动剪
    for i in range(8):
        w("hypothesis", f"假设{i}", f"第 {i} 条推断")
    rows, _e = mstore.memories(SCRIPT, "obj-1")
    hyps = [r for r in rows if r["scope"] == "hypothesis"]
    eq("hypothesis 封顶 5 条", len(hyps), 5)
    eq("事件不受假设影响（仍 20 条）", len([r for r in rows if r["scope"] == "event"]), 20)

    # user 档案：硬上限，超了报错而不是悄悄丢
    n_ok, first_err = 0, ""
    for i in range(40):
        r = w("user", f"字段{i}", f"值 {i}", subject="user")
        if r.ok:
            n_ok += 1
        elif not first_err:
            first_err = r.code
    st, _ = mstore.status_dict(SCRIPT)
    # 官方是先查条数再插（`scope_count > LIMIT` 才拦），所以实际能到 LIMIT+1 条才报错
    check("user 档案到达硬上限后报 MEMORY_LIMIT_REACHED",
          first_err == "MEMORY_LIMIT_REACHED", f"首次失败={first_err} 成功={n_ok}")
    check("硬上限附近停住（官方实际是 30+1=31 条）", 30 <= n_ok <= 31, n_ok)
    eq("报错后库里条数不再增长", len([r for r in mstore.memories(SCRIPT)[0]
                                    if r["scope"] == "user"]), n_ok)

    # 总行数上限
    check("总行数未超过 200", st["memory_count"] <= 200, st["memory_count"])

    # object 也是硬上限
    fresh_db()
    mstore.enable(SCRIPT, confirm=True)
    n_obj = 0
    for i in range(20):
        r = w("object", f"对象字段{i}", f"值 {i}", subject="obj-1")
        if r.ok:
            n_obj += 1
    obj_err = ""
    r = w("object", "再一条", "超了", subject="obj-1")
    if not r.ok:
        obj_err = r.code
    check("object 档案到达上限后报错", obj_err == "MEMORY_LIMIT_REACHED" or n_obj >= 15,
          f"成功={n_obj} 错误={obj_err}")


# ============================== [6] 撤销与删除 ==============================
def t_undo() -> None:
    section("[6] 撤销与删除")
    fresh_db()
    mstore.enable(SCRIPT, confirm=True)

    # 覆盖写入 → 撤销应恢复旧值（而不是把整条删掉）
    w("object", "目前状态", "暧昧但未确认", subject="obj-1")
    r2 = w("object", "目前状态", "已经在一起了", subject="obj-1")
    eq("同字段覆盖不新增行", mstore.status_dict(SCRIPT)[0]["memory_count"], 1)
    eq("覆盖时 pruned=0（是更新不是剪枝）", (r2.data or {}).get("pruned"), 0)
    rows, _e = mstore.memories(SCRIPT, "obj-1")
    eq("覆盖后是新值", rows[0]["value"], "已经在一起了")

    un = mstore.undo(SCRIPT)
    check("撤销成功", un.ok, un.message)
    rows, _e = mstore.memories(SCRIPT, "obj-1")
    eq("撤销恢复了旧值（不是删掉整条）", rows[0]["value"] if rows else None, "暧昧但未确认")

    # 撤销是「一次退一步」：上面两次 apply 就是两条操作记录，所以要再撤一次才清空
    un = mstore.undo(SCRIPT)
    check("再撤销一次把这条也退掉", un.ok, un.message)
    eq("撤销后没有残留", mstore.status_dict(SCRIPT)[0]["memory_count"], 0)

    un = mstore.undo(SCRIPT)
    check("撤销到空栈后报 NOTHING_TO_UNDO", not un.ok and un.code == "NOTHING_TO_UNDO",
          f"{un.code} {un.message}")

    # forget-object：删该对象全部 + 清撤销栈
    w("object", "工作", "在深圳做设计", subject="obj-1")
    w("object", "工作", "换工作了", subject="obj-2")
    w("hypothesis", "猜测", "可能只是忙", subject="obj-1")
    eq("撤销栈有 3 条", mstore.status_dict(SCRIPT)[0]["undo_count"], 3)
    fo = mstore.forget_object(SCRIPT, "obj-1", confirm=True)
    check("删对象成功", fo.ok, fo.message)
    eq("该对象清空", len(mstore.memories(SCRIPT, "obj-1")[0]), 0)
    eq("别的对象不受影响", len(mstore.memories(SCRIPT, "obj-2")[0]), 1)
    eq("撤销栈被一并清空（官方行为）", mstore.status_dict(SCRIPT)[0]["undo_count"], 0)

    # revoke（不删数据）→ 读写被停，但数据还在
    rv = mstore.revoke(SCRIPT, confirm=True, delete=False)
    check("撤回同意成功", rv.ok, rv.message)
    rows, err = mstore.memories(SCRIPT, "obj-2")
    check("撤回后数据仍在（只是停止读写）", len(rows) == 1, f"rows={len(rows)} err={err}")
    ctx = mstore.context(SCRIPT, "obj-2")
    check("撤回后召回被拒（CONSENT_REQUIRED）",
          not ctx.ok and ctx.code == "CONSENT_REQUIRED", f"{ctx.code} {ctx.message}")
    ap = w("hypothesis", "新的", "写不进去")
    check("撤回后写入被拒", not ap.ok and ap.code in ("CONSENT_REQUIRED", "NOT_INITIALIZED"),
          ap.code)

    # clear → 删库文件
    mstore.enable(SCRIPT, confirm=True)
    path = mstore.db_path()
    check("库文件存在", path.exists())
    cl = mstore.clear(SCRIPT, confirm=True)
    check("清空成功", cl.ok, cl.message)
    check("库文件被删除", not path.exists())
    st, _ = mstore.status_dict(SCRIPT)
    check("清空后回到未创建状态", not st["exists"] and st["memory_count"] == 0)

    cl2 = mstore.clear(SCRIPT, confirm=True)
    check("对不存在的库再清空也不报错", cl2.ok, cl2.message)

    # 暂停 / 恢复
    mstore.enable(SCRIPT, confirm=True)
    eq("暂停成功", mstore.pause(SCRIPT).ok, True)
    ctx = mstore.context(SCRIPT, "obj-1")
    check("暂停后召回被拒（MEMORY_PAUSED）",
          not ctx.ok and ctx.code == "MEMORY_PAUSED", f"{ctx.code} {ctx.message}")
    eq("恢复成功", mstore.resume(SCRIPT).ok, True)
    check("恢复后召回可用", mstore.context(SCRIPT, "obj-1").ok)


# ============================== [7] service 编排 ==============================
def t_service() -> None:
    section("[7] service 编排（建档 / 召回 / 同意）")
    fresh_db()
    mconsent.forget()
    cfg = Config(_TMP / "config.json")
    cfg.load()

    plan = msvc.consent_plan(cfg, {"consent_enabled": False, "paused": False,
                                   "exists": False})
    check("还没同意 → need=True", plan.need and not plan.already_ok)

    asked: list[str] = []
    ok, why = msvc.ensure_consent(cfg, ask=lambda r: (asked.append(r), False)[1],
                                  st={"consent_enabled": False, "paused": False,
                                      "exists": False})
    check("ask 拒绝时不启用，也不会偷偷建库",
          not ok and not mstore.db_path().exists(), f"{ok} {why}")
    check("ask 被调用了一次", len(asked) == 1, asked)

    ok, why = msvc.ensure_consent(cfg, ask=lambda r: (asked.append(r), True)[1],
                                  st={"consent_enabled": False, "paused": False,
                                      "exists": False})
    check("ask 同意后启用成功", ok and mstore.db_path().exists(), f"{ok} {why}")
    eq("同意记录已落盘", mconsent.load().accepted, True)

    before = len(asked)
    ok, why = msvc.ensure_consent(cfg, ask=lambda r: (asked.append(r), True)[1])
    check("已同意且已启用 → 不再问用户", ok and len(asked) == before, f"{ok} {why}")

    # 召回：缓存说没开就静默跳过（不刷噪声告警）
    cfg.set("memory.enabled", False, force=True)
    rc = msvc.recall_for(cfg, "obj-1")
    check("记忆未启用 → 跳过且标记 SKIPPED", not rc.ok and rc.code == "SKIPPED", rc.code)

    # 建档 → 召回
    cand = mex.manual_candidate("object", "工作", "在深圳做设计", subject_id="obj-1")
    n, msg = msvc.write_candidates(cfg, [cand])
    check("write_candidates 写入成功", n == 1, msg)
    cfg.set("memory.enabled", True, force=True)
    rc = msvc.recall_for(cfg, "obj-1")
    check("召回拿到内容", rc.ok and rc.count == 1, f"{rc.ok} {rc.count} {rc.code}")
    check("召回文本含字段与来源标签", "工作" in rc.text and "用户明确陈述" in rc.text, rc.text)
    check("召回 note 人话可读", "已召回 1 条" in rc.note(), rc.note())

    # 缓存过期（库里其实被撤回了）→ 不该反复报同一句
    mstore.revoke(SCRIPT, confirm=True, delete=False)
    rc = msvc.recall_for(cfg, "obj-1")
    check("库里已撤回 → 召回如实报错", not rc.ok and rc.code == "CONSENT_REQUIRED", rc.code)
    check("并且把缓存降级（下轮不再刷同一条）",
          cfg.get("memory.enabled") is False, cfg.get("memory.enabled"))

    # 官方模块读不到时，预检退化成「不拦」，而不是把写入全堵死
    missing = mstore.load_official(_TMP / "没有这个文件.py")
    check("读不到官方模块时 degraded 而不是异常", not missing.available and missing.error)
    eq("降级时预检不拦（交给官方脚本自己判）",
       missing.check_delta({"scope": "user", "subject_id": "user", "field": "f",
                            "value": "v", "source_type": "chatlab"}), "")
    eq("降级时上限用兜底值", missing.scope_limits["hypothesis"], 5)


# ============================== [8] 打包后的记忆层 ==============================
_HEX32 = re.compile(r"^[0-9a-f]{32}$", re.I)
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


def _norm(payload, root: str = ""):
    """把天然易变的字段抹平（时间戳 / 行 id / 记忆库路径），只比结构与语义。

    按**值的形态**判断，不按字段名 —— 官方 payload 里 `id` 是 32 位裸 hex，
    `created_at`/`observed_at`/`updated_at` 是 ISO，`status.path` 是库文件绝对路径。
    按字段名判断（比如「含 at 就当时间戳」）会误伤 `category` 这类名字。
    """
    if isinstance(payload, dict):
        return {k: _norm(v, root) for k, v in payload.items()}
    if isinstance(payload, list):
        return [_norm(x, root) for x in payload]
    if isinstance(payload, str):
        if root and root in payload:
            return "<memdir>"          # 两条路径用的是各自的临时库，路径必然不同
        if _HEX32.match(payload) or _UUID.match(payload):
            return "<id>"
        if _ISO.match(payload):
            return "<ts>"
    return payload


def _both(seq: list, tag: str):
    """同一串命令在两条路径上各跑一遍，**各用独立的空库**（保证前置状态一致）。

    返回 `(子进程侧结果列表, 进程内侧结果列表)`。
    """
    real = mstore._use_inproc
    runs = []
    try:
        for inproc in (False, True):
            d = _TMP / "dual" / tag / ("inproc" if inproc else "subproc")
            shutil.rmtree(d, ignore_errors=True)
            d.mkdir(parents=True, exist_ok=True)
            os.environ["GOUTOUJUNSHI_MEMORY_DIR"] = str(d)
            mstore._rules_cache.clear()
            mstore._use_inproc = (lambda: True) if inproc else (lambda: False)
            runs.append([mstore.run_cli(SCRIPT, a) for a in seq])
    finally:
        mstore._use_inproc = real
    return runs[0], runs[1]


def t_frozen() -> None:
    section("[8] 打包后的记忆层：进程内调用必须与子进程**输出等价**（E34）")

    from app import paths

    # ---------- 8.1 路径选择 ----------
    check("源码运行走子进程路径", mstore._use_inproc() is False,
          f"is_frozen={paths.is_frozen()}")

    _had_frozen = hasattr(sys, "frozen")
    _old_frozen = getattr(sys, "frozen", None)
    _had_meipass = hasattr(sys, "_MEIPASS")
    _old_meipass = getattr(sys, "_MEIPASS", None)
    try:
        sys.frozen = True                       # type: ignore[attr-defined]
        sys._MEIPASS = str(ROOT)                # type: ignore[attr-defined]
        check("伪造 frozen 后自动切到进程内路径（不再看 sys.executable）",
              mstore._use_inproc() is True)
        eq("frozen 下 app_root 指向 _MEIPASS（skills/ 才能被打包里找到）",
           str(paths.app_root()), str(ROOT))
        check("frozen 下 skill 目录 = _MEIPASS/skills，官方脚本找得到",
              paths.skill_dir("goutoujunshi") == ROOT / "skills" / "goutoujunshi",
              str(paths.skill_dir("goutoujunshi")))
    finally:
        if _had_frozen:
            sys.frozen = _old_frozen             # type: ignore[attr-defined]
        else:
            del sys.frozen                       # type: ignore[attr-defined]
        if _had_meipass:
            sys._MEIPASS = _old_meipass          # type: ignore[attr-defined]
        elif hasattr(sys, "_MEIPASS"):
            del sys._MEIPASS                     # type: ignore[attr-defined]

    # ---------- 8.2 逐命令等价性 ----------
    cn_delta = json.dumps({"scope": "hypothesis", "subject_id": "obj-1",
                           "field": "答应见面",
                           "value": "她说周末可以出来，但不能太晚",
                           "source_type": "assistant_inference",
                           "confidence": "high"}, ensure_ascii=False)
    illegal = json.dumps({"scope": "object", "subject_id": "obj-1", "field": "x",
                          "value": "y", "source_type": "assistant_inference"})
    en = ["enable", "--confirm"]

    seqs = [
        ("status", [["status"]]),
        ("enable_ok", [en, ["status"]]),
        ("enable_noconfirm", [["enable"]]),
        ("forget_noconfirm", [en, ["forget-object", "obj-1"]]),
        ("write_cn", [en, ["apply", "--json", cn_delta], ["show", "--subject-id", "obj-1"]]),
        ("recall_cn", [en, ["apply", "--json", cn_delta],
                       ["context", "--subject-id", "obj-1", "--max-chars", "4000"]]),
        ("undo", [en, ["apply", "--json", cn_delta], ["undo"],
                  ["show", "--subject-id", "obj-1"]]),
        ("illegal_cross", [en, ["apply", "--json", illegal]]),
        ("argparse_err", [["bogus-command"]]),
        ("paused", [en, ["pause"], ["status"], ["resume"], ["status"]]),
        ("destroy", [en, ["apply", "--json", cn_delta],
                     ["forget-object", "obj-1", "--confirm"],
                     ["revoke", "--confirm", "--delete"]]),
    ]
    root = str(_TMP)
    kept = {}
    for tag, seq in seqs:
        a_list, b_list = _both(seq, tag)
        kept[tag] = b_list                       # 留进程内结果给 8.3 用
        bad = [i for i, (a, b) in enumerate(zip(a_list, b_list))
               if a.ok != b.ok or a.code != b.code
               or _norm(a.data, root) != _norm(b.data, root)]
        detail = ""
        if bad:
            i = bad[0]
            detail = (f"第 {i + 1} 步 {seq[i]}："
                      f"subproc(ok={a_list[i].ok},code={a_list[i].code}) "
                      f"inproc(ok={b_list[i].ok},code={b_list[i].code})")
        check(f"[{tag}] 两条路径逐字段等价（{len(seq)} 步）", not bad, detail)

    # ---------- 8.3 正向证明：进程内路径真的能干活，而不是「一样地失败」----------
    real_use = mstore._use_inproc
    st, ap, sh = kept["write_cn"]
    check("进程内：启用成功", st.ok and st.data.get("consent_enabled") is True, st.code)
    check("进程内：中文写入成功",
          ap.ok and ap.data["memory"]["value"] == "她说周末可以出来，但不能太晚",
          str(ap.data)[:140])
    check("进程内：中文**读回没歪**（StringIO 不做 cp936 转码）",
          sh.ok and sh.data["memories"][0]["value"] == "她说周末可以出来，但不能太晚",
          str(sh.data)[:140])
    check("进程内：模型推断写进假设、且标着来源",
          sh.ok and sh.data["memories"][0]["source_type"] == "assistant_inference"
          and sh.data["memories"][0]["scope"] == "hypothesis", str(sh.data)[:140])

    en_bad = kept["enable_noconfirm"]
    check("进程内：官方错误码原样透出（缺 --confirm）",
          not en_bad[0].ok and en_bad[0].code == "CONFIRMATION_REQUIRED", en_bad[0].code)
    cross_bad = kept["illegal_cross"]
    check("进程内：类别×来源越界仍被官方拦下",
          not cross_bad[1].ok and cross_bad[1].code == "SOURCE_NOT_ELIGIBLE",
          cross_bad[1].code)
    arg_bad = kept["argparse_err"]
    check("进程内：argparse 报错被吞成退出码而不是抛 SystemExit",
          not arg_bad[0].ok and arg_bad[0].code == "EXIT_NONZERO", arg_bad[0].code)
    un, sh_after_undo = kept["undo"][2], kept["undo"][3]
    check("进程内：撤销真的退掉了（读回 0 条）",
          un.ok and sh_after_undo.ok and sh_after_undo.data.get("count") == 0,
          str(sh_after_undo.data)[:100])
    want_null = kept["destroy"]
    check("进程内：forget-object 走完能删空对象",
          want_null[2].ok and want_null[2].data.get("deleted") == 1
          and want_null[2].data.get("undo_history_cleared") is True,
          str(want_null[2].data)[:100])
    _destroy_db = _TMP / "dual" / "destroy" / "inproc" / "memory.sqlite3"
    check("进程内：revoke --delete 连库一起删",
          want_null[3].ok and not _destroy_db.exists(), str(want_null[3].data)[:100])

    # E35 专项：进程内连续调用会攒下 sqlite Connection（官方 `with connect()` 不 close，
    # 且 Connection 与 statement cache 互为引用环），不主动 gc 就删不掉库文件。
    # 这条用例把引用环故意堆起来，防止将来有人顺手删掉 `_run_inproc` 里的 gc.collect()。
    mstore._use_inproc = lambda: True
    try:
        d35 = _TMP / "e35"
        shutil.rmtree(d35, ignore_errors=True)
        d35.mkdir(parents=True, exist_ok=True)
        os.environ["GOUTOUJUNSHI_MEMORY_DIR"] = str(d35)
        for _ in range(3):
            mstore.run_cli(SCRIPT, ["enable", "--confirm"])
            mstore.run_cli(SCRIPT, ["apply", "--json", cn_delta])
        rv = mstore.run_cli(SCRIPT, ["revoke", "--confirm", "--delete"])
        check("进程内连续写入 3 轮后仍能删库（E35：连接不 gc 会 WinError 32）",
              rv.ok and rv.data.get("deleted") is True
              and not (d35 / "memory.sqlite3").exists(),
              f"{rv.code} {str(rv.data)[:120]}")
    finally:
        mstore._use_inproc = real_use
        os.environ["GOUTOUJUNSHI_MEMORY_DIR"] = str(_TMP / "memory")

    # ---------- 8.4 副作用隔离：全局状态必须原样还回去 ----------
    argv_before = list(sys.argv)
    stdout_before = sys.stdout
    mstore._use_inproc = lambda: True
    try:
        mstore.run_cli(SCRIPT, ["enable"])
    finally:
        mstore._use_inproc = real_use
    eq("调完 sys.argv 原样还原", sys.argv, argv_before)
    check("调完 sys.stdout 没被换掉（否则界面里后续 print 全没了）",
          sys.stdout is stdout_before)

    # 进程内路径**绝不能**再起子进程 —— 打包后那会开出第二个 GUI 窗口
    import subprocess as _sp

    real_run = _sp.run

    def _boom(*_a, **_k):
        raise AssertionError("进程内路径不该起子进程")

    _sp.run = _boom
    mstore._use_inproc = lambda: True
    try:
        r = mstore.run_cli(SCRIPT, ["status"])
        check("进程内路径不会再起子进程（打包后 sys.executable 是 exe）", r.ok, r.code)
    except AssertionError as e:
        check("进程内路径不会再起子进程（打包后 sys.executable 是 exe）", False, str(e))
    finally:
        _sp.run = real_run
        mstore._use_inproc = real_use

    # timeout 在进程内没有意义（本地 sqlite 无网络 IO），但不该报 TIMEOUT
    mstore._use_inproc = lambda: True
    try:
        r = mstore.run_cli(SCRIPT, ["status"], timeout=0.001)
        check("进程内忽略 timeout 参数且不误报 TIMEOUT（本地 sqlite 毫秒级）",
              r.ok and r.code != "TIMEOUT", f"ok={r.ok} code={r.code!r}")
        check("耗时被如实记下来", r.elapsed_s > 0, f"{r.elapsed_s}s")
        ids = set()
        for _ in range(5):
            mstore.run_cli(SCRIPT, ["status"])
            ids.add(id(mstore.load_official(SCRIPT).module))
        eq("连续 5 次进程内调用只加载一份官方模块（复用缓存，不重复 exec）",
           len(ids), 1)
    finally:
        mstore._use_inproc = real_use

    # 还原工作目录，免得影响后面的节
    os.environ["GOUTOUJUNSHI_MEMORY_DIR"] = str(_TMP / "memory")


def main() -> int:
    print("M4 离线单元自检")
    print(f"临时目录：{_TMP}")
    t_store()
    t_cross()
    t_consent()
    t_extract()
    t_limits()
    t_undo()
    t_service()
    t_frozen()

    check("没有碰用户真实的记忆目录", _REAL_DIR.exists() == _REAL_BEFORE,
          f"{_REAL_DIR} 从 {_REAL_BEFORE} 变成了 {_REAL_DIR.exists()}")
    print(f"\n结果：通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败项：" + "、".join(FAIL))
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
