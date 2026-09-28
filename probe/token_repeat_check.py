# -*- coding: utf-8 -*-
"""重复分析成本探针 —— 回答「每次分析是否从头重发已分析过的对话」。

背景（用户提问）：连续对同一段聊天点「开始分析」，是不是每次都把之前的对话
重新发一遍、从而为同样的内容重复付钱？

分两部分：

- **A 离线（零成本）**：用**真实的** `prompt_builder.build()` 组装多轮请求，
  量出「本次发送的内容里有多少上一轮已经发过」。不使用假数据以外的任何东西。
- **B 真机（`--live`，约 ¥0.02）**：向 DeepSeek 发**两次完全相同**的请求，
  读取 `usage.prompt_cache_hit_tokens` —— DeepSeek 有自动的「上下文硬盘缓存」
  （命中价约为未命中的 1/10），这是唯一能实测「重复内容是否真的打折」的办法。
  客户端目前**只读 prompt/completion**，看不到这两个字段，所以必须直连验证。

用法：
    python probe/token_repeat_check.py            # 只跑离线
    python probe/token_repeat_check.py --live     # 追加真机缓存验证
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.analysis import prompt_builder as pb          # noqa: E402
from app.analysis import questions as q                # noqa: E402
from app.analysis.skill_loader import default_loader   # noqa: E402
from app.capture.transcript import Message, Transcript  # noqa: E402
from app.config import load_config                     # noqa: E402

# 一段像真人的中文闲聊：长短不一，含语气词与短句，贴近微信
LINES = [
    "在干嘛呢", "刚下班，累死了", "今天加班到八点", "怎么这么晚", "项目要上线了嘛",
    "那你吃饭了吗", "随便吃了点", "别老吃外卖啊", "没办法，懒得做", "周末有空吗",
    "应该有吧，怎么了", "想约你看个电影", "哦？看什么", "最近那部科幻片评价不错",
    "你说的那个我不太喜欢科幻", "那你想看什么类型的", "喜剧吧，轻松点", "行啊那就喜剧",
    "几点呢", "下午场怎么样", "可以，两点那场", "好，我订票", "我请你吧",
    "不用不用，我来", "你上次就抢着付了", "那不一样", "哪里不一样", "反正我请",
    "行吧听你的", "嘿嘿", "那周末见", "嗯嗯，周末见", "到时候我去接你",
    "不用接，我自己过去", "那我在影院门口等你", "好", "记得穿厚点，降温了",
    "你怎么知道我穿得少", "猜的", "哼", "真的降温了吗", "天气预报说的",
    "那你也多穿点", "知道了", "对了你最近睡得好吗", "还行吧，就是有点焦虑",
    "我也有一点", "你焦虑什么", "工作上的事", "说说看", "就是觉得自己没什么进步",
    "你已经很努力了", "哪有", "真的有，我看得出来", "谢谢你", "别客气",
]

# 三轮分析时聊天已经进行到第几条（模拟「每次点分析时又聊了一会儿」）
ROUND_STOPS = [16, 36, 56]
# 真机那三次用的条数：46 条 vs 49 条（前 46 条完全相同）
LIVE_STOPS = (46, 49)


def make_transcript(n: int) -> Transcript:
    """造一个「聊天进行到第 n 条」的 transcript（说话人交替）。"""
    msgs = []
    for i, text in enumerate(LINES[:n], start=1):
        msgs.append(Message(
            id=i,
            ts=f"2026-09-23 {20 + i // 60:02d}:{(i * 7) % 60:02d}",
            gap_s=12 + (i * 3) % 40,
            sender="me" if i % 2 else "obj-1",
            type="text",
            text=text,
            confidence=0.97,
            ts_source="capture",
        ))
    return Transcript(
        source="capture",
        window={"chat_title": "小新"},
        speaker_map={
            "me": {"side": "right", "code": "me", "confirmed": True},
            "objects": [{"code": "obj-1", "side": "left", "label": "对方"}],
        },
        messages=msgs,
    )


def common_prefix_len(a: str, b: str) -> int:
    n = min(len(a), len(b))
    for i in range(n):
        if a[i] != b[i]:
            return i
    return n


def section_tokens(text: str) -> int:
    return pb.est_tokens(text)


def part_a() -> dict:
    print("=" * 72)
    print("A. 离线：连续三轮分析同一段聊天，重复了多少")
    print("=" * 72)

    cfg = load_config()
    loader = default_loader(cfg)
    qtype = q.get("default")
    loaded = loader.load(qtype.key)
    budget = int(cfg.get("transcript.token_budget", pb.DEFAULT_TOKEN_BUDGET))
    print(f"  问题类型：{qtype.label}　参考文件：{len(loaded.files)} 份"
          f"　token_budget={budget}")

    # 三轮：每次点「开始分析」时，聊天又多了 20 条
    rounds = [r for r in ROUND_STOPS if r <= len(LINES)]
    prompts = []
    for r in rounds:
        t = make_transcript(r)
        p = pb.build(t, qtype.key, loaded=loaded,
                     token_budget=int(budget))
        prompts.append((r, p))

    print()
    print(f"  {'轮次':<4}{'总条数':<7}{'含入条数':<9}{'system约tokens':<15}"
          f"{'user约tokens':<14}{'记录tokens':<11}")
    rows = []
    for r, p in prompts:
        rec = p.user.split("## 记录", 1)[-1].split("## 用户这次的问题")[0]
        sys_t = section_tokens(p.system)
        usr_t = section_tokens(p.user)
        rec_t = section_tokens(rec)
        rows.append({"n": r, "sys": sys_t, "user": usr_t, "rec": rec_t,
                     "included": len(p.included_ids), "ids": list(p.included_ids)})
        print(f"  {len(rows):<4}{r:<7}{len(p.included_ids):<9}{sys_t:<15}{usr_t:<14}{rec_t:<11}")

    # 与上一轮的重叠
    print()
    print("  与上一轮相比：")
    detail = []
    for i in range(1, len(rows)):
        prev, cur = rows[i - 1], rows[i]
        ids_prev, ids_cur = set(prev["ids"]), set(cur["ids"])
        dup = len(ids_prev & ids_cur)
        new = len(ids_cur - ids_prev)
        sys_same = prompts[i - 1][1].system == prompts[i][1].system
        # 前缀缓存只认「从第 0 个 token 起连续相同」，所以要看 user 段的公共前缀
        # （system 段完全相同时，它自己就是一段可命中的前缀）
        cp = common_prefix_len(prompts[i - 1][1].user, prompts[i][1].user)
        sys_t = cur["sys"]
        user_common_t = section_tokens(prompts[i][1].user[:cp])
        hit_est = sys_t + user_common_t
        total_est = sys_t + cur["user"]
        detail.append({
            "round": i + 1, "records_sent": len(ids_cur), "records_repeated": dup,
            "records_new": new, "system_identical": sys_same,
            "user_common_chars": cp,
            "prefix_hit_tokens_est": hit_est, "total_tokens_est": total_est,
            "hit_ratio": round(hit_est / total_est, 3) if total_est else 0,
        })
        print(f"    第 {i + 1} 轮：发出 {len(ids_cur)} 条记录，其中 **{dup} 条是上一轮发过的**"
              f"（新增 {new} 条）；重复率 {dup / max(1, len(ids_cur)):.0%}")
        print(f"      system 段与上一轮{'完全相同 → 可整段命中缓存' if sys_same else '不同 → 无缓存命中'}")
        print(f"      user 段公共前缀 {cp} 字符（约 {user_common_t} tokens）")
        print(f"      可命中前缀估算 {hit_est} / 总 {total_est} tokens"
              f" = **{hit_est / max(1, total_est):.0%}**")

    # 完全相同输入重复分析（用户连点两次「开始分析」）
    print()
    t = make_transcript(rounds[-1])
    p1 = pb.build(t, qtype.key, loaded=loaded, token_budget=budget)
    p2 = pb.build(t, qtype.key, loaded=loaded, token_budget=budget)
    same = (p1.system == p2.system) and (p1.user == p2.user)
    print(f"  同一段聊天连点两次分析：两次请求{'完全一致' if same else '不一致'}"
          f"（约 {section_tokens(p1.system) + section_tokens(p1.user)} tokens 输入全部重复）")

    # 死配置核查
    print()
    print("=" * 72)
    print("附：transcript.max_messages 是否真的生效")
    print("=" * 72)
    cfg2 = load_config()
    raw = cfg2.get("transcript.max_messages", None)
    print(f"  当前值：{raw}")
    from app.capture.transcript import TranscriptBuilder
    import inspect
    src = inspect.getsource(Transcript)
    # 直接搜整个 app/ 有没有第二处读取（config.get('transcript.max_messages')）
    used = []
    root = Path(__file__).resolve().parent.parent / "app"
    for f in root.rglob("*.py"):
        for i, ln in enumerate(f.read_text(encoding="utf-8").split("\n"), 1):
            if "transcript.max_messages" in ln:
                used.append(f"{f.relative_to(root.parent)}:{i}: {ln.strip()}")
    if used:
        for u in used:
            print(f"  被读取于 {u}")
    else:
        print("  **没有任何代码读取它** —— 实际决定发多少条的是 transcript.token_budget")
    print(f"  内存里保留的最大条数由 TranscriptBuilder(max_messages=…) 决定，"
          f"采集路径用默认值 {inspect.signature(TranscriptBuilder.__init__).parameters['max_messages'].default}")

    return {"rows": rows, "detail": detail, "max_messages_raw": raw,
            "max_messages_readers": used}


def part_b_live() -> dict:
    print()
    print("=" * 72)
    print("B. 真机：DeepSeek 的前缀缓存到底吃不吃这一口")
    print("=" * 72)
    from app.analysis.llm_client import LLMClient

    cfg = load_config()
    c = LLMClient(cfg)
    if not c.api_key:
        print("  没有可用 Key（config 里未配置），跳过")
        return {"skipped": True}
    base = c.base_url
    print(f"  endpoint={base}　model={c.model}")

    loader = default_loader(cfg)
    qtype = q.get("default")
    loaded = loader.load(qtype.key)
    n_before, n_after = LIVE_STOPS
    t = make_transcript(n_before)
    p = pb.build(t, qtype.key, loaded=loaded,
                 token_budget=int(cfg.get("transcript.token_budget", 3500)))
    # 第三种情况才是真实日常：聊了几句新的再分析 —— system 一字不差，
    # 但 user 段里的条数/时间范围变了，前缀在 user 开头就断了。
    t2 = make_transcript(n_after)
    p2 = pb.build(t2, qtype.key, loaded=loaded,
                  token_budget=int(cfg.get("transcript.token_budget", 3500)))

    # 自检：第三种情况必须真的变了，否则这一路等于把第 2 次重复了一遍
    assert p.system == p2.system, "system 段本该相同"
    assert p.user != p2.user, "user 段本该不同（条数变了）"
    assert len(t2.messages) == n_after and len(t.messages) == n_before, "条数构造错误"

    cases = [
        ("第 1 次", f"同一段聊天（{n_before} 条），首次分析（缓存冷启动）", p),
        ("第 2 次", "**完全相同**的请求重发（= 连点两次「开始分析」）", p),
        ("第 3 次", f"聊了 {n_after - n_before} 条新的再分析"
                    f"（system 相同、user 变了：{n_after} 条 vs {n_before} 条）", p2),
    ]
    print(f"  system 段 {section_tokens(p.system)} tokens（三次完全相同）；"
          f"user 段 {section_tokens(p.user)} → {section_tokens(p2.user)} tokens")

    out = []
    for label, desc, pr in cases:
        body = {
            "model": c.model,
            "messages": [{"role": "system", "content": pr.system},
                         {"role": "user", "content": pr.user}],
            "max_tokens": 16,
            "stream": False,
            "temperature": 0,
        }
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            base + "/chat/completions", data=payload,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {c.api_key}"},
            method="POST")
        t0 = time.perf_counter()
        with urllib.request.urlopen(req, timeout=90) as resp:
            obj = json.loads(resp.read().decode("utf-8"))
        dt = time.perf_counter() - t0
        u = obj.get("usage") or {}
        hit = u.get("prompt_cache_hit_tokens")
        miss = u.get("prompt_cache_miss_tokens")
        print()
        print(f"  {label}：{desc}")
        print(f"    {dt:.1f}s　{json.dumps(u, ensure_ascii=False)}")
        if hit is None:
            print("    **服务端未返回缓存字段** —— 无法判定是否命中")
        else:
            total_in = hit + miss
            print(f"    缓存命中 {hit} / 未命中 {miss}　命中率 **{hit / max(1, total_in):.0%}**"
                  f"（system 段估算 {section_tokens(pr.system)} tokens）")
        out.append({"label": label, "usage": u, "elapsed": round(dt, 2),
                    "system_tokens_est": section_tokens(pr.system),
                    "user_tokens_est": section_tokens(pr.user)})
        time.sleep(3)  # 官方说缓存构建耗时是秒级
    return {"calls": out}


def main() -> int:
    live = "--live" in sys.argv
    res = {"part_a": part_a()}
    if live:
        res["part_b"] = part_b_live()
    out = Path(__file__).resolve().parent / "token_repeat_report.json"
    out.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    print()
    print(f"原始结果已写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
