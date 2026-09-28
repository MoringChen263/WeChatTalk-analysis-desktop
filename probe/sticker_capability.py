# -*- coding: utf-8 -*-
"""表情包/表情识别能力盘点（离线、可复跑）。

回答一个问题：**这个项目对聊天里的「表情」到底识别到什么程度？**

把「表情」拆成四种形态分别测，因为它们的命运完全不同：

  A. Emoji 字符（😂 / 🤣 / ❤️）—— Unicode 文本
  B. 具名表情（[微笑] / [捂脸] / [旺柴]）—— 导出文件里常带具体名字
  C. 表情包 / 贴纸（[表情] / [动画表情]）—— 图片，导出时只剩占位文字
  D. 表情包里的**画面内容**（是哭是笑、什么梗）

两条管道也分开看，因为能力差别极大：

  * **导入路径**（importer 解析导出文件）—— 能看到文字占位
  * **采集路径**（截屏 + OCR）—— 图里没有文字可读

用法：
    python probe/sticker_capability.py            # 观测 + 断言
    python probe/sticker_capability.py --verbose  # 打印每条消息细节

不写盘、不联网、不碰用户数据目录（JEV_DATA_DIR 指向临时目录）。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, ".")

_TMP = tempfile.mkdtemp(prefix="jev-sticker-")
os.environ["JEV_DATA_DIR"] = _TMP
os.environ["GOUTOUJUNSHI_MEMORY_DIR"] = str(Path(_TMP) / "mem")

import numpy as np  # noqa: E402

from app.capture import collector, importer  # noqa: E402
from app.capture.ocr import who_said  # noqa: E402
from app.capture.transcript import TranscriptBuilder  # noqa: E402

VERBOSE = "--verbose" in sys.argv
PASS = FAIL = 0
NOTES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f"  <- {detail}" if detail else ""))


def head(t: str) -> None:
    print()
    print("=" * 74)
    print(t)
    print("=" * 74)


def build_txt() -> Path:
    """一份覆盖各类「表情」写法的导出文件。"""
    txt = "\n".join([
        "2026-09-01 21:00:03 小新: 今天好累啊",
        "2026-09-01 21:00:40 我: 怎么了",
        "2026-09-01 21:01:02 小新: 😂🤣",
        "2026-09-01 21:02:00 小新: 你自己体会吧 😂",
        "2026-09-01 21:03:00 小新: [表情]",
        "2026-09-01 21:03:10 小新: [表情]",
        "2026-09-01 21:03:20 小新: [表情]",
        "2026-09-01 21:04:00 我: [动画表情]",
        "2026-09-01 21:05:00 小新: [图片]",
        "2026-09-01 21:06:00 小新: [微笑]",
        "2026-09-01 21:07:00 小新: [捂脸]",
        "2026-09-01 21:08:00 小新: [旺柴]",
        "2026-09-01 21:09:00 我: [语音]",
    ]) + "\n"
    p = Path(_TMP) / "chat.txt"
    p.write_text(txt, encoding="utf-8")
    return p


# ---------------------------------------------------------------- A/B/C/D
head("A. 导入路径：占位符 → type 映射")
for raw, want in (("[表情]", "sticker"), ("[动画表情]", "sticker"), ("[贴纸]", "sticker"),
                  ("[图片]", "image"), ("[语音]", "voice"), ("[视频]", "unknown"),
                  ("[微笑]", "text"), ("[捂脸]", "text"), ("[旺柴]", "text")):
    got = importer.classify_placeholder(raw)
    mark = "" if got == want else f"  <- 期望 {want}"
    print(f"  {raw:<10} -> {got:<8}{mark}")

head("B. 端到端：表情在 transcript 里的最终形态")
res = importer.import_file(build_txt(), me_label="我")
t = res.transcript
check("导入成功", res.ok, str(res.warnings)[:120])
if t:
    if VERBOSE:
        for m in t.messages:
            print(f"    #{m.id} type={m.type:<8} {m.sender:<6} {m.text!r}")
    kinds = {}
    for m in t.messages:
        kinds[m.type] = kinds.get(m.type, 0) + 1
    print(f"  type 分布：{kinds}")

    check("emoji 字符原样保留（当普通文字）",
          any(m.text == "😂🤣" and m.type == "text" for m in t.messages))
    check("[表情] 标记为 sticker",
          any(m.text == "[表情]" and m.type == "sticker" for m in t.messages))
    check("[动画表情] 标记为 sticker",
          any(m.text == "[动画表情]" and m.type == "sticker" for m in t.messages))
    check("具名表情 [捂脸] 不在词表 → 按 text 处理（模型能直接读到表情名）",
          any(m.text == "[捂脸]" and m.type == "text" for m in t.messages))

    n_expr = sum(1 for m in t.messages if m.text == "[表情]")
    check("同一人连发 3 个 [表情] 不会被去重吞掉（type≠text 跳过判重）", n_expr == 3,
          f"实际入库 {n_expr} 条")

    st = t.stats()
    named = sum(1 for m in t.messages if m.text in ("[微笑]", "[捂脸]", "[旺柴]"))
    # 非 text = 4 条 sticker([表情]×3 + [动画表情]) + 1 条 image([图片]) + 1 条 voice([语音])
    check("image_placeholders 只数 type≠text 的（=6）",
          st["image_placeholders"] == 6, f"实际 {st['image_placeholders']}")
    check("具名表情被算作文字，不进 image_placeholders（统计口径不一致）", named == 3,
          f"实际 {named}")
    print(f"  image_placeholders = {st['image_placeholders']}；"
          f"另有具名表情 {named} 条被当成文字、未计入")

head("C. prompt 里表情长什么样（模型实际看到的内容）")
if t:
    lines = [ln for ln in t.to_lines() if "[表情]" in ln or "😂" in ln or "[捂脸]" in ln]
    for ln in lines:
        print("  " + ln.split(" ", 2)[-1])
    check("Message.line() 不渲染 type（模型只能靠字面量区分）",
          not any("sticker" in ln for ln in t.to_lines()))
    print("  ↑ 可见：模型能读出「[表情] / [动画表情] / [捂脸]」的字面差别，"
          "但读不到画面内容")

head("D. 采集路径：截屏 OCR 遇到表情包")
rng = np.random.default_rng(7)
chat = np.full((120, 300, 3), 245, dtype=np.uint8)
chat[20:60, 20:140] = np.array([255, 255, 255], dtype=np.uint8)                     # 平底色气泡
chat[20:60, 160:280] = rng.integers(0, 255, size=(40, 120, 3), dtype=np.uint8)      # 杂色 = 表情包
box_bubble = [(30, 30), (130, 30), (130, 50), (30, 50)]
box_sticker = [(170, 30), (270, 30), (270, 50), (170, 50)]

k_bubble, _, _ = who_said(chat, box_bubble)
k_sticker, _, _ = who_said(chat, box_sticker)
print(f"  气泡上的字   -> who_said = {k_bubble!r}")
print(f"  表情包上的字 -> who_said = {k_sticker!r}")
check("表情包区域的文字被丢弃（众数颜色占比 <45% 判为图片）", k_sticker is None,
      f"实际 {k_sticker!r}")

tb = TranscriptBuilder({"me": {"code": "me"}, "objects": [{"code": "obj-1"}]})
tb.add("obj-1", "你好")
check("采集器写库不传 type_ → 采集路径永远没有非 text 消息",
      tb.build().messages[0].type == "text")

src = Path(__file__).resolve().parent.parent / "app" / "capture" / "transcript.py"
app_files = list((Path(__file__).resolve().parent.parent / "app").rglob("*.py"))
callers = []
for f in app_files:
    body = f.read_text(encoding="utf-8", errors="replace")
    if f.name != "transcript.py" and "add_placeholder" in body:
        callers.append(f.name)
print(f"  add_placeholder 在 app/ 下的调用者：{callers or '（无）'}")
check("add_placeholder 是死代码（定义了但采集路径从不调用）", not callers)
NOTES.append("采集路径的表情包**整条消失**：既无文字可读，又没有人给它写占位")

head("E. 词表覆盖度")
table = importer.PLACEHOLDER_TYPES
print(f"  PLACEHOLDER_TYPES 共 {len(table)} 条：")
for k in sorted(table):
    print(f"    {k:<12} -> {table[k]}")
missing = [r for r in ("[微笑]", "[捂脸]", "[旺柴]", "[破涕为笑]", "[GIF]", "[视频号]")
           if r not in table]
check("具名表情/GIF 等常见写法不在词表（会落到 text，不影响分析但计数漏算）",
      len(missing) > 0, f"未覆盖 {missing}")

head("结论")
for n in NOTES:
    print(f"  * {n}")
print()
print(f"观测项合计：{PASS + FAIL} 项，符合预期 {PASS} 项，与预期不符 {FAIL} 项")
print("（本探针盘点的是**当前行为**；若将来接通表情占位或图片理解，FAIL 会浮现提醒更新）")
return_code = 0 if FAIL == 0 else 1
sys.exit(return_code)
