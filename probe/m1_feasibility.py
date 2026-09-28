# -*- coding: utf-8 -*-
"""M-1 可行性验证：对真实微信窗口采样，量化 OCR 质量与说话人判定可靠性。

只读、帧只在内存、不上传、不写盘（唯一输出是纯文本报告）。
复用 jev-chat-src 的采集与判定逻辑（capture/ocr），不修改上游。

    python probe/m1_feasibility.py --samples 20 --timeout 120

产出：
    probe/m1_report.json   机器可读原始数据
    probe/m1_report.md     人可核对的报告（含逐屏 OCR 原文，供肉眼抽查准确率）
"""
from __future__ import annotations

import argparse
import ctypes
import difflib
import json
import os
import statistics
import sys
import time
import traceback
from collections import Counter

import numpy as np

# ---- 复用上游：把 jev-chat-src 加进 sys.path，直接用它的实现，不复制粘贴 ----
SRC = os.environ.get("JEV_SRC", r"D:\Jev\jev-chat-src")
if os.path.isdir(SRC) and SRC not in sys.path:
    sys.path.insert(0, SRC)

from app import capture as up_capture  # noqa: E402
from app import ocr as up_ocr  # noqa: E402

import ctypes.wintypes as wt  # noqa: E402
from windows_capture import WindowsCapture  # noqa: E402

u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
u32.SetProcessDPIAware()

SETTLE = 0.25
MAX_WAIT = 1.0

WECHAT_EXES = ("weixin.exe", "wechat.exe")
MAIN_CLASSES = ("Qt51514QWindowIcon", "WeChatMainWndForPC")  # 4.x / 3.x
CHILD_TOOL_TITLES = ("Weixin", "图片和视频", "WxTrayIconMessageWindow")


def find_wechat_main_hwnd() -> tuple[int, dict]:
    """比上游更宽：**不筛 IsWindowVisible**（微信收进托盘时主窗口就是隐藏的，
    上游会直接抛错）。按进程名 + 类名/标题锁定主窗口，排除工具窗/看图窗/托盘消息窗。
    同进程还有 'Weixin'（工具窗）、'图片和视频'（看图窗）、面积可能更大 → 不能按面积挑。"""
    def exe_of(pid):
        h = k32.OpenProcess(0x1000, False, pid)
        if not h:
            return ""
        buf, size = ctypes.create_unicode_buffer(1024), ctypes.c_uint(1024)
        ok = k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
        k32.CloseHandle(h)
        return os.path.basename(buf.value).lower() if ok else ""

    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def cb(hwnd, _):
        pid = ctypes.c_ulong()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if exe_of(pid.value) not in WECHAT_EXES:
            return True
        t = ctypes.create_unicode_buffer(512)
        u32.GetWindowTextW(hwnd, t, 512)
        c = ctypes.create_unicode_buffer(256)
        u32.GetClassNameW(hwnd, c, 256)
        r = wt.RECT()
        u32.GetWindowRect(hwnd, ctypes.byref(r))
        found.append({
            "hwnd": int(hwnd), "title": t.value, "class": c.value,
            "visible": bool(u32.IsWindowVisible(hwnd)),
            "iconic": bool(u32.IsIconic(hwnd)),
            "rect": [r.left, r.top, r.right, r.bottom],
            "area": max(0, r.right - r.left) * max(0, r.bottom - r.top),
        })
        return True

    u32.EnumWindows(cb, 0)
    cands = [w for w in found
             if w["class"] in MAIN_CLASSES or w["title"] == "微信"]
    cands = [w for w in cands if w["title"] not in CHILD_TOOL_TITLES and w["area"] > 0]
    if not cands:
        raise RuntimeError(f"没找到微信主窗口。枚举到的同进程窗口：{found}")
    cands.sort(key=lambda w: (w["title"] != "微信", -w["area"]))
    return cands[0]["hwnd"], {"candidates": cands}


def ensure_capturable(hwnd: int) -> bool:
    """把窗口弄到「DWM 会渲染」的状态，且**不抢焦点、不改变大小位置**：
    - 最小化（IsIconic）→ SW_SHOWNOACTIVATE 还原
    - 隐藏（收进托盘）→ 同样 SW_SHOWNOACTIVATE 唤起，再压到所有窗口最底层，别挡用户干活
    返回是否动过手。Windows 不渲染隐藏/最小化的窗口，什么截图法都拿不到画面。"""
    moved = False
    if u32.IsIconic(hwnd):
        u32.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE
        moved = True
    if not u32.IsWindowVisible(hwnd):
        u32.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE：显示但不激活
        moved = True
    if moved:
        u32.SetWindowPos(hwnd, 1, 0, 0, 0, 0, 0x13)  # HWND_BOTTOM, NOSIZE|NOMOVE|NOACTIVATE
        time.sleep(0.6)  # 等布局铺好，否则 chat_area 会认错
    return moved


def frame_hash(chat: np.ndarray) -> int:
    """低成本内容指纹：抽样 + 均值，够判断「这一屏跟上一屏是不是同一屏」。"""
    return int(np.asarray(chat[::16, ::16], dtype=np.int64).sum())


class Sampler:
    """采集直接复用上游 app.capture.Capture（settled/alive/stop），不自己写一份 WGC 循环。"""

    def __init__(self, args):
        self.args = args
        self.hwnd = 0
        self.cap = None

    def run(self) -> dict:
        self.hwnd, disc = find_wechat_main_hwnd()
        title = ctypes.create_unicode_buffer(256)
        u32.GetWindowTextW(self.hwnd, title, 256)
        cls = ctypes.create_unicode_buffer(256)
        u32.GetClassNameW(self.hwnd, cls, 256)
        r = wt.RECT()
        u32.GetWindowRect(self.hwnd, ctypes.byref(r))
        before = bool(u32.IsWindowVisible(self.hwnd))
        revived = ensure_capturable(self.hwnd)
        self.meta = {
            "hwnd": int(self.hwnd),
            "title": title.value,
            "class": cls.value,
            "rect": [r.left, r.top, r.right, r.bottom],
            "visible_before": before,
            "had_to_revive": revived,
            "discovery": disc,
        }

        self.cap = up_capture.Capture(self.hwnd, settle=SETTLE, max_wait=MAX_WAIT)
        return self.collect()

    def collect(self) -> dict:
        args = self.args
        deadline = time.perf_counter() + args.timeout
        samples: list[dict] = []
        seen_frames: dict[int, int] = {}
        reader_lh = None
        skipped_area = 0
        last_chat = last_area = None
        idle_since = None
        reused = False
        blank_frames = 0
        blank_detect = args.blank_detect
        blank_hint = False

        while len(samples) < args.samples and time.perf_counter() < deadline:
            if not self.cap.alive():
                raise RuntimeError("采集线程结束（微信窗口关了？）")
            ensure_capturable(self.hwnd)
            full = self.cap.settled()
            if full is None:
                # 静态屏不触发新帧（上游只在内容变化时才交帧）。为了测重复采样一致性与
                # 确定性，隔 reuse_after 秒复用上一帧再 OCR 一轮，样本标 reused_frame=true。
                if last_chat is None:
                    time.sleep(0.05)
                    continue
                if idle_since is None:
                    idle_since = time.perf_counter()
                if time.perf_counter() - idle_since < args.reuse_after:
                    time.sleep(0.05)
                    continue
                idle_since = time.perf_counter()
                chat, (x0, y_top, x1, y_in, pane_bg, y_pane) = last_chat, last_area
                reused = True
            else:
                area = up_capture.chat_area(full)
                if area is None:
                    skipped_area += 1
                    continue
                x0, y_top, x1, y_in, pane_bg, y_pane = area
                chat = full[y_top:y_in, x0:x1]
                last_chat, last_area = chat, area
                reused = False
                idle_since = time.perf_counter()
                if blank_detect and blank_frames == 0:
                    flat = full.reshape(-1, 3)[::31]
                    uniq = len(np.unique(flat, axis=0))
                    dark = float((flat.mean(1) < 200).mean())
                    if uniq <= 40 and dark <= 0.02:
                        blank_frames += 1
                        print(f"\n!! 抓到的是【空白帧】：唯一色={uniq} 暗像素占比={dark:.3f}")
                        print("   微信 4.x 的真实界面画在内嵌 Chromium 子窗口（Chrome_WidgetWin_0）里。")
                        print("   若主窗口是被『从托盘强行唤起』的，Chromium 视图不会恢复 → 帧全白、OCR 0 行。")
                        print("   处理：请手动点开微信主窗口（进入任意有消息的聊天），再重跑本探针。\n")
                        blank_hint = True
            fh = frame_hash(chat)
            repeat = seen_frames.get(fh, 0)
            seen_frames[fh] = repeat + 1

            t = time.perf_counter()
            res, _ = up_ocr._engine()(chat, use_cls=False)
            ocr_ms = (time.perf_counter() - t) * 1000.0
            W = chat.shape[1]

            lines, raw, dropped, gray = [], [], 0, 0
            for box, text, score in sorted(res or [], key=lambda r: r[0][0][1]):
                kind, bg, ink_h = up_ocr.who_said(chat, box)
                xc = float((box[0][0] + box[2][0]) / 2.0)
                rec = {
                    "kind": kind if kind else "dropped",
                    "text": text,
                    "score": round(float(score), 4),
                    "x_center": round(xc, 1),
                    "x_rel": round(xc / W, 4),
                    "bg": [int(v) for v in bg] if bg is not None else None,
                    "ink_h": int(ink_h),
                }
                raw.append(rec)
                if kind is None:
                    dropped += 1
                    continue
                if kind == "gray":
                    gray += 1
                lines.append(rec)

            # OCR 确定性：同一份像素连跑 3 次，结果应完全一致（这比「重截一屏」更干净）
            det = None
            if len(samples) == 0 and raw:
                base = [r["text"] for r in raw]
                reps = []
                for _ in range(2):
                    res2, _ = up_ocr._engine()(chat, use_cls=False)
                    cur = [r[1] for r in (res2 or [])]
                    m = sum(1 for t2 in base if any(similar(t2, u) for u in cur))
                    reps.append(round(m / len(base), 4) if base else None)
                det = {"n": len(base), "rates": reps}

            if reader_lh is None:
                real = [l["ink_h"] for l in lines if l["kind"] in ("me", "her") and l["ink_h"] > 0]
                if len(real) >= 3:
                    reader_lh = float(statistics.median(real))

            samples.append({
                "idx": len(samples),
                "ts": time.strftime("%H:%M:%S"),
                "frame_repeat": repeat,
                "reused_frame": bool(reused),
                "frame_hash": fh,
                "area": [int(x0), int(y_top), int(x1), int(y_in)],
                "pane_bg": [int(v) for v in pane_bg],
                "y_pane": int(y_pane),
                "chat_wh": [int(chat.shape[1]), int(chat.shape[0])],
                "ocr_ms": round(ocr_ms, 1),
                "raw_n": len(res or []),
                "dropped_image": dropped,
                "gray_n": gray,
                "lines": lines,
                "raw": raw,
                "determinism": det,
            })
            print(f"[{len(samples)}/{args.samples}] {time.strftime('%H:%M:%S')} "
                  f"OCR原始={len(res or []):3d} 丢弃={dropped:3d} 灰={gray:3d} 气泡={len(lines) - gray:3d} "
                  f"{ocr_ms:6.1f}ms 首行={raw[0]['text'][:24] if raw else '(空)'}", flush=True)

        try:
            self.cap.stop()
        except Exception:
            pass
        return {"meta": self.meta, "samples": samples, "reader_lh": reader_lh,
                "skipped_unrecognized_area": skipped_area,
                "blank_frames": blank_frames, "blank_hint": blank_hint}


# --------------------------- 指标计算 ---------------------------
def similar(a: str, b: str) -> bool:
    if a == b:
        return True
    if difflib.SequenceMatcher(None, a, b).ratio() >= 0.75:
        return True
    return len(a) == len(b) >= 3 and sum(x != y for x, y in zip(a, b)) <= 1


def metrics(data: dict) -> dict:
    samples = data["samples"]
    if not samples:
        return {"error": "没采到任何样本"}

    all_lines = [l for s in samples for l in s["lines"]]
    scores = [l["score"] for l in all_lines]
    bubbles = [l for l in all_lines if l["kind"] in ("me", "her")]

    kind_counter = Counter(l["kind"] for l in all_lines)

    # 1) 「绿 = 我」假设是否成立：me 行底色应偏绿
    me_bgs = [tuple(l["bg"]) for l in bubbles if l["kind"] == "me" and l["bg"]]
    green_ok = sum(1 for r, g, b in me_bgs if g > r + 40 and g > b + 40)
    me_bg_mode = Counter(me_bgs).most_common(1)[0][0] if me_bgs else None

    # 2) 说话人判定与 x 位置的自洽率（这才是「左右判定」的可测代理）
    #    微信里「我」永远在右；底色判 me 而行中心在左 = 两个信号打架
    consistency = {"me_on_right": 0, "me_total": 0, "her_on_left": 0, "her_total": 0}
    for l in bubbles:
        right = l["x_rel"] > 0.5
        if l["kind"] == "me":
            consistency["me_total"] += 1
            consistency["me_on_right"] += int(right)
        else:
            consistency["her_total"] += 1
            consistency["her_on_left"] += int(not right)
    total_b = consistency["me_total"] + consistency["her_total"]
    agree_rate = (consistency["me_on_right"] + consistency["her_on_left"]) / total_b if total_b else None

    # 3) 重复采样一致性（同一屏 OCR 两次，结果应完全一致）
    rep = []
    by_hash: dict[int, list] = {}
    for s in samples:
        by_hash.setdefault(s["frame_hash"], []).append(s)
    for _, group in by_hash.items():
        if len(group) < 2:
            continue
        base = [l["text"] for l in group[0]["lines"] if l["kind"] in ("me", "her")]
        for other in group[1:]:
            cur = [l["text"] for l in other["lines"] if l["kind"] in ("me", "her")]
            matched = sum(1 for t in base if any(similar(t, u) for u in cur))
            rep.append({
                "a": group[0]["idx"], "b": other["idx"],
                "n": len(base), "matched": matched,
                "rate": round(matched / len(base), 4) if base else None,
            })

    # 4) 跨屏去重能力：相邻样本间有多少行被判定为「同一行」（不滚动时应接近 100%）
    dedup = []
    for i in range(1, len(samples)):
        prev = [l["text"] for l in samples[i - 1]["lines"] if l["kind"] in ("me", "her")]
        cur = [l["text"] for l in samples[i]["lines"] if l["kind"] in ("me", "her")]
        if not cur:
            continue
        dup = sum(1 for t in cur if any(similar(t, p) for p in prev))
        dedup.append({"i": i, "n": len(cur), "dup": dup, "rate": round(dup / len(cur), 4)})

    low_conf = [l for l in all_lines if l["score"] < 0.6]
    determinism = [s["determinism"] for s in samples if s.get("determinism")]
    return {
        "samples": len(samples),
        "distinct_frames": len(by_hash),
        "reused_frames": sum(1 for s in samples if s.get("reused_frame")),
        "raw_ocr_total": sum(s.get("raw_n", 0) for s in samples),
        "dropped_as_image": sum(s.get("dropped_image", 0) for s in samples),
        "gray_lines": sum(s.get("gray_n", 0) for s in samples),
        "lines_total": len(all_lines),
        "bubble_lines": len(bubbles),
        "kinds": dict(kind_counter),
        "skipped_unrecognized_area": data.get("skipped_unrecognized_area", 0),
        "reader_lh": data.get("reader_lh"),
        "determinism": determinism,
        "score": {
            "mean": round(statistics.fmean(scores), 4) if scores else None,
            "median": round(statistics.median(scores), 4) if scores else None,
            "min": round(min(scores), 4) if scores else None,
            "below_0.6": len(low_conf),
            "below_0.6_rate": round(len(low_conf) / len(all_lines), 4) if all_lines else None,
        },
        "speaker": {
            "me_green_ratio": round(green_ok / len(me_bgs), 4) if me_bgs else None,
            "me_bg_mode": list(me_bg_mode) if me_bg_mode else None,
            "x_agreement_rate": round(agree_rate, 4) if agree_rate is not None else None,
            "detail": consistency,
        },
        "repeatability": rep,
        "dedup_across_frames": dedup,
        "ocr_ms": {
            "mean": round(statistics.fmean([s["ocr_ms"] for s in samples]), 1),
            "max": round(max(s["ocr_ms"] for s in samples), 1),
        },
    }


def write_reports(data: dict, m: dict, outdir: str) -> tuple[str, str]:
    os.makedirs(outdir, exist_ok=True)
    jp = os.path.join(outdir, "m1_report.json")
    with open(jp, "w", encoding="utf-8") as f:
        json.dump({"meta": data["meta"], "metrics": m, "samples": data["samples"]},
                  f, ensure_ascii=False, indent=2)

    L = []
    A = L.append
    A("# M-1 可行性验证报告\n")
    A(f"- 采样时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    mm = data["meta"]
    A(f"- 目标窗口：`{mm['title']}` / class `{mm['class']}` / hwnd {mm['hwnd']}")
    A(f"- 窗口矩形：{mm['rect']}　样本数：{m.get('samples')}　不同屏数：{m.get('distinct_frames')}\n")

    A("## 1. OCR 质量\n")
    A(f"- 采样 {m['samples']} 轮（其中复用同一帧的 {m['reused_frames']} 轮）／不同屏 {m['distinct_frames']} 个")
    A(f"- OCR 原始框总数：{m['raw_ocr_total']}；被当图片丢弃：{m['dropped_as_image']}；灰字（时间戳/系统提示/引用）：{m['gray_lines']}")
    A(f"- 可分类行数：{m['lines_total']}（其中气泡行 {m['bubble_lines']}）")
    A(f"- 消息区认不出的帧：{m['skipped_unrecognized_area']}；测得正常字高 lh={m['reader_lh']}")
    sc = m["score"]
    A(f"- 置信度：均值 {sc['mean']} / 中位 {sc['median']} / 最低 {sc['min']}")
    A(f"- 低置信（<0.6）行：{sc['below_0.6']} 行（{sc['below_0.6_rate']}）")
    A(f"- 单帧 OCR 耗时：均值 {m['ocr_ms']['mean']} ms，最大 {m['ocr_ms']['max']} ms\n")

    A("## 1b. OCR 确定性（同一份像素连跑 3 次）\n")
    if m.get("determinism"):
        for d in m["determinism"]:
            A(f"- {d['n']} 行样本：两次复跑一致率 {d['rates']}")
    else:
        A("- （样本为空，无法测）")
    A("")

    A("## 2. 说话人判定\n")
    sp = m["speaker"]
    A(f"- 分类分布：{m['kinds']}")
    A(f"- 「绿底=我」假设成立率：{sp['me_green_ratio']}（me 行底色众数 {sp['me_bg_mode']}）")
    A(f"- 与 x 位置的自洽率：**{sp['x_agreement_rate']}**（细节 {sp['detail']}）\n")

    A("## 3. 重复采样一致性（同一屏 OCR 两次）\n")
    if m["repeatability"]:
        for r in m["repeatability"]:
            A(f"- 屏#{r['a']} vs #{r['b']}：{r['matched']}/{r['n']} 行一致 → {r['rate']}")
    else:
        A("- （本次没有重复屏，无法测；如需测可保持窗口静止多采几轮）")
    A("")

    A("## 4. 跨屏去重（不滚动时应接近 100%）\n")
    for d in m["dedup_across_frames"]:
        A(f"- 样本#{d['i']}：{d['dup']}/{d['n']} 行判为已见 → {d['rate']}")
    A("")

    A("## 5. 逐屏 OCR 原文（肉眼核对准确率用）\n")
    for s in data["samples"]:
        A(f"### 样本 #{s['idx']}　{s['ts']}　chat={s['chat_wh']}　OCR {s['ocr_ms']}ms"
          f"　复用帧={s.get('reused_frame')}\n")
        rows = s.get("raw") or []
        if not rows:
            A("_（这一屏 OCR 一个字都没认出来——窗口可能停在空白会话，或消息区认错）_\n")
            continue
        A("| 判定 | 置信 | x/W | 底色 | 墨高 | 文本 |")
        A("| --- | --- | --- | --- | --- | --- |")
        for l in rows:
            txt = str(l["text"]).replace("|", "\\|")
            A(f"| {l['kind']} | {l['score']} | {l['x_rel']} | {l['bg']} | {l['ink_h']} | {txt} |")
        A("")

    A("## 6. 阈值建议\n")
    A("见 `m1_report.json`；本报告的自动建议由调用方填入 §10 config。\n")

    mp = os.path.join(outdir, "m1_report.md")
    with open(mp, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    return jp, mp


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples", type=int, default=20)
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument("--reuse-after", type=float, default=4.0,
                    help="静态屏下隔多少秒复用上一帧再采样一轮（测一致性与确定性）")
    ap.add_argument("--blank-detect", action="store_true", default=True,
                    help="检测空白帧（显示微信 Chromium 视图未恢复的情况）")
    ap.add_argument("--outdir", default=os.path.join(os.path.dirname(os.path.abspath(__file__))))
    args = ap.parse_args()

    print(f"复用上游源码：{SRC}")
    if not os.path.isdir(SRC):
        print("找不到 jev-chat-src，请用 JEV_SRC 环境变量指定", file=sys.stderr)
        return 2

    s = Sampler(args)
    try:
        data = s.run()
    except Exception:
        traceback.print_exc()
        return 1
    m = metrics(data)
    jp, mp = write_reports(data, m, args.outdir)
    print("\n=== 指标 ===")
    print(json.dumps({k: v for k, v in m.items() if k not in ("samples_detail",)}, ensure_ascii=False, indent=2))
    print(f"\n报告：{jp}\n      {mp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
