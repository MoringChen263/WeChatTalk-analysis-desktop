# -*- coding: utf-8 -*-
"""入口。

    python -m app.main            启动界面
    python -m app.main --selftest 不开窗口，跑自检（M0 的验收手段，也便于 CI）
"""
from __future__ import annotations

import argparse
import sys
import traceback

from app import paths
from app.config import load_config
from app.version import APP_DISPLAY_NAME, __version__


def selftest() -> int:
    """M0 DoD：config 可读写、依赖齐全、关键路径存在、窗口发现可用。不开窗口。"""
    ok = True
    print(f"== {APP_DISPLAY_NAME} v{__version__} 自检 ==")

    # 1) 依赖
    deps = []
    for mod in ("PySide6", "qfluentwidgets", "numpy", "cv2", "rapidocr_onnxruntime",
                "windows_capture"):
        try:
            __import__(mod)
            deps.append((mod, "OK"))
        except Exception as e:
            deps.append((mod, f"FAIL {type(e).__name__}"))
            ok = False
    for name, st in deps:
        print(f"  依赖 {name:<24} {st}")

    # 2) config 读写往返
    cfg = load_config()
    print(f"  配置来源：{cfg.loaded_from}")
    cfg.set("analysis.question_type", "reply")
    p = cfg.save()
    cfg2 = load_config()
    roundtrip = cfg2.get("analysis.question_type") == "reply"
    print(f"  配置读写往返：{'OK' if roundtrip else 'FAIL'}（{p}）")
    cfg.set("analysis.question_type", "default")
    cfg.save()
    ok = ok and roundtrip

    # 3) 隐私红线
    cfg.set("privacy.screenshots_to_disk", True)  # 试着写进去，应被强制改回
    forced = cfg.get("privacy.screenshots_to_disk") is False
    print(f"  截图不落盘红线：{'OK（强制 false）' if forced else 'FAIL'}")
    ok = ok and forced

    # 4) skill 与官方记忆脚本
    skill = cfg.skill_path()
    skill_md = skill / "SKILL.md"
    mem = cfg.memory_script()
    print(f"  skill 目录：{skill}  SKILL.md={'OK' if skill_md.exists() else 'MISSING'}")
    print(f"  官方记忆脚本：{mem}  {'OK' if mem.exists() else 'MISSING'}")

    # 5) 窗口发现（只读，不唤起、不改动微信）
    from app.capture import window_finder
    procs = cfg.get("capture.target_process", ["Weixin.exe", "WeChat.exe"])
    wins = window_finder.enum_windows(procs)
    main_win = window_finder.pick_main_window(wins, cfg.get("capture.title_hint", "微信"),
                                              cfg.get("capture.window_class_fallback", ""))
    if main_win:
        state = "最小化" if main_win.iconic else ("隐藏" if not main_win.visible else "可见")
        desc = (f"hwnd={main_win.hwnd} {main_win.title!r} {main_win.cls} "
                f"{main_win.size} {state}")
    else:
        desc = "未找到（微信没开？）"
    print(f"  枚举到同进程窗口 {len(wins)} 个；主窗口：{desc}")

    # 6) 空白帧探测（M-1 的产物，必须有）
    import numpy as np
    blank = window_finder.frame_is_blank(np.full((300, 400, 3), 255, dtype=np.uint8))
    print(f"  空白帧探测：{'OK' if blank else 'FAIL'}")
    ok = ok and blank

    # 7) 导入管道（M2 的产物）：拿仓库里的夹具真跑一遍，确认三种格式都能进同一条管道
    from app.capture import importer
    from app.capture.transcript import validate_transcript
    fix = paths.app_root() / "probe" / "fixtures"
    cases = [("wechat_plain.txt", None), ("export.html", None),
             ("chatlab_agent.json", None), ("wechat_gbk.txt", "张三")]
    for name, me in cases:
        f = fix / name
        if not f.exists():
            print(f"  导入 {name:<22} SKIP（夹具不存在，先跑 probe/m2_unit.py）")
            continue
        r = importer.import_file(f, me_label=me)
        errs = validate_transcript(r.transcript.to_public_dict()) if r.ok else ["ok=False"]
        good = r.ok and not errs
        ok = ok and good
        n = len(r.transcript.messages) if r.ok else 0
        print(f"  导入 {name:<22} {'OK' if good else 'FAIL'}　"
              f"{r.fmt}/{r.stats.get('route')}　{n} 条"
              + ("" if good else f"　{errs[:1] or r.warnings[:1]}"))

    # 8) 分析层（M3 的产物）：路由路径、prompt 组装、输出契约、后端就绪诊断
    from app.analysis import questions as aq
    from app.analysis import parse_output as po
    from app.analysis import prompt_builder as pb
    from app.analysis import schema as asc
    from app.analysis.engine import AnalysisEngine
    from app.analysis.llm_client import LLMClient
    from app.analysis.skill_loader import default_loader

    loader = default_loader(cfg)
    bad_routes = loader.validate_catalog()
    n_refs = len(loader.index())
    print(f"  skill 参考索引：{n_refs} 份　"
          f"问题类型路由：{'OK' if not bad_routes else 'FAIL ' + '、'.join(bad_routes[:3])}")
    ok = ok and bool(n_refs) and not bad_routes

    qtypes = len(aq.QUESTION_TYPES)
    print(f"  问题类型：{qtypes} 种（SKILL.md 按需加载表的可执行镜像）")

    # 用一份真 transcript 过一遍 prompt 组装与渲染
    sample = _sample_transcript()
    if sample is None:
        print("  prompt 组装：SKIP（没有可用的 transcript，先跑 probe/m1_transcript_live.py）")
    else:
        prompt = pb.build(sample, "reply", loaded=loader.load("reply"))
        tok = prompt.token_estimate()
        good = ("# 输出契约" in prompt.system and prompt.included_ids
                and all(rid in [m.id for m in sample.messages] for rid in prompt.included_ids))
        print(f"  prompt 组装：{'OK' if good else 'FAIL'}　"
              f"system {len(prompt.system)} 字 / user {len(prompt.user)} 字　"
              f"估算 {tok['total']} tokens　含 {len(prompt.included_ids)} 条记录")
        ok = ok and good

        fix = paths.app_root() / "probe" / "fixtures" / "analysis_ok.json"
        if fix.exists():
            a = po.parse(fix.read_text(encoding="utf-8"), prompt.included_ids)
            pages_ok = len(a.steps_markdown()) == 5 and len(a.scripts_markdown()) == 4
            print(f"  输出契约：{asc.validate(a.data) or 'OK'}　"
                  f"五步/话术卡渲染：{'OK' if pages_ok else 'FAIL'}　"
                  f"可发送成品 {len(a.primary_text())} 字　模式 {a.mode}")
            ok = ok and not asc.validate(a.data) and pages_ok
        else:
            print("  输出契约：SKIP（缺 probe/fixtures/analysis_ok.json）")

        bad = po.parse("模型今天不想输出 JSON", [])
        print(f"  降级路径：{'OK' if bad.mode == po.MODE_DEGRADED and not bad.ok else 'FAIL'}"
              f"（mode={bad.mode}）")
        ok = ok and bad.mode == po.MODE_DEGRADED

        plan = AnalysisEngine(cfg).plan("reply", sample)
        client = LLMClient(cfg)
        ready, why = client.ready()
        print(f"  后端预检：{'就绪' if ready else '未就绪（' + why + '）'}　"
              f"模型 {client.model or '?'}　参考 {len(plan.refs)} 份")

    # 9) 记忆层（M4 的产物）：官方契约、类别×来源预检、同意门控、建档/召回/撤销闭环
    #    全程指向临时目录 —— 自检绝不该碰用户真实的记忆库，也不该改用户真实配置
    ok = _selftest_memory(cfg, mem, ok) and ok

    print("== 自检结果：", "全部通过" if ok else "有项目失败", "==")
    return 0 if ok else 1


def _selftest_memory(cfg, mem, ok: bool) -> bool:
    """第 9 节。所有写操作都发生在临时目录里，跑完还原环境变量。"""
    import os
    import tempfile
    from pathlib import Path

    from app.memory import consent as mconsent
    from app.memory import extract as mex
    from app.memory import service as msvc
    from app.memory import store as mstore

    old_mem = os.environ.get("GOUTOUJUNSHI_MEMORY_DIR")
    old_data = os.environ.get("JEV_DATA_DIR")
    tmp = tempfile.mkdtemp(prefix="jev-selftest-mem-")
    os.environ["GOUTOUJUNSHI_MEMORY_DIR"] = tmp
    os.environ["JEV_DATA_DIR"] = str(Path(tmp) / "data")
    try:
        rules = mstore.load_official(mem)
        print(f"  官方校验模块：{'OK' if rules.available else 'FAIL ' + rules.error}　"
              f"策略 v{rules.policy_version}　scope {len(rules.scope_limits)} 类　"
              f"单条上限 {rules.max_value_chars} 字")
        ok = ok and rules.available

        # 类别 × 来源的交叉规则：模型推断**不能**写进对象档案（官方硬规则）
        crossed = rules.check_delta({"scope": "object", "subject_id": "obj-1", "field": "x",
                                     "value": "y", "source_type": "assistant_inference"})
        legal = not rules.check_delta({"scope": "hypothesis", "subject_id": "obj-1",
                                       "field": "x", "value": "y",
                                       "source_type": "assistant_inference"})
        print(f"  类别×来源预检：{'OK' if crossed and legal else 'FAIL'}　"
              f"（越界被拦：{crossed or '没拦住！'}）")
        ok = ok and bool(crossed) and legal

        # 同意门控：同意前要问，同意后不再问，撤回后又开始问
        need0, _ = mconsent.need_consent(rules.policy_version)
        rec = mconsent.accept(rules.policy_version)
        need1, _ = mconsent.need_consent(rules.policy_version)
        mconsent.forget()
        need2, _ = mconsent.need_consent(rules.policy_version)
        gate = need0 and not need1 and need2
        print(f"  同意门控：{'OK' if gate else 'FAIL'}　"
              f"未同意={need0}　已同意={not need1}　撤回后={need2}　记录于 {rec.at}")
        ok = ok and gate

        # 不替用户同意：ask 回调返回 False 时必须拒绝，且不动库
        plan = msvc.consent_plan(cfg, {"consent_enabled": False, "paused": False,
                                       "exists": False})
        refused, why2 = msvc.ensure_consent(cfg, ask=lambda _r: False,
                                            st={"consent_enabled": False, "paused": False,
                                                "exists": False})
        no_silent = plan.need and not refused
        print(f"  不替用户同意：{'OK' if no_silent else 'FAIL'}（{why2}）")
        ok = ok and no_silent

        # 建档 → 召回 → 撤销 闭环（全在临时库里）
        def snap() -> dict:
            return mstore.status_dict(mem)[0]

        st0 = snap()
        en = mstore.enable(mem, confirm=True)
        cand = mex.manual_candidate("hypothesis", "自检条目", "这是自检写入的临时条目",
                                    subject_id="obj-1", rules=rules)
        done, msg = msvc.write_candidates(cfg, [cand])
        rc = mstore.recall(mem, subject_id="obj-1")
        st1 = snap()
        un = mstore.undo(mem)
        st2 = snap()
        loop = (en.ok and done == 1 and rc.ok and rc.count == 1
                and st1.get("memory_count") == 1 and un.ok and st2.get("memory_count") == 0)
        print(f"  建档→召回→撤销：{'OK' if loop else 'FAIL'}　"
              f"启用={en.ok}　写入={done} 条（{msg}）　召回 {rc.count} 条　"
              f"条数 {st0.get('memory_count')}→{st1.get('memory_count')}"
              f"→{st2.get('memory_count')}　撤销栈 {st1.get('undo_count')}"
              f"→{st2.get('undo_count')}")
        ok = ok and loop

        # 召回渲染：多写几条才测得出截断（只放一条的话，截断分支根本不会走到）
        for i in range(1, 4):
            msvc.write_candidates(cfg, [mex.manual_candidate(
                "event", f"自检事件{i}", f"自检写入的第 {i} 条事件，用来撑满字符上限",
                subject_id="obj-1", rules=rules)])
        rows, _err = mstore.memories(mem, "obj-1")
        text1, n1, d1 = mstore.format_memories(rows, max_chars=4000)
        _text2, n2, d2 = mstore.format_memories(rows, max_chars=8)
        trunc = bool(rows) and d1 == 0 and n1 == len(rows) and n2 <= 1 and d2 >= n1 - n2
        print(f"  召回渲染：{'OK' if trunc else 'FAIL'}　共 {len(rows)} 条　"
              f"不截断 {n1} 条/丢 {d1}　限 8 字时 {n2} 条/丢 {d2}")
        ok = ok and trunc

        # 提取层：多编号剥离（踩过的坑）与长度封顶
        body, ids = mex.split_cites("[#2] [#3] 对方主动安排了见面")
        capped = mex.manual_candidate("object", "长文本", "字" * 500, subject_id="obj-1",
                                      rules=rules)
        ext_ok = (body.startswith("对方") and ids == [2, 3]
                  and len(capped.value) == rules.max_value_chars)
        print(f"  提取层：{'OK' if ext_ok else 'FAIL'}　"
              f"多编号 {ids}　超长值封到 {len(capped.value)} 字")
        ok = ok and ext_ok

        rv = mstore.revoke(mem, confirm=True, delete=True)
        print(f"  撤回并删除库：{'OK' if rv.ok else 'FAIL'}　"
              f"（自检用的是临时目录：{tmp}）")
        ok = ok and rv.ok
        return ok
    finally:
        for key, val in (("GOUTOUJUNSHI_MEMORY_DIR", old_mem), ("JEV_DATA_DIR", old_data)):
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val


def _sample_transcript():
    """优先用真机采集的样例，其次用导入管道造一份，都没有就返回 None。"""
    import json

    from app.capture.transcript import Message, Transcript

    p = paths.app_root() / "probe" / "m1_transcript.json"
    if p.exists():
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            msgs = [Message(**{k: v for k, v in m.items() if k in Message.__dataclass_fields__})
                    for m in d.pop("messages", [])]
            t = Transcript(**{k: v for k, v in d.items()
                              if k in Transcript.__dataclass_fields__})
            t.messages = msgs
            return t
        except Exception:
            pass
    return None


def _run_selftest() -> int:
    """跑自检，并把结果**额外落一份文件**。

    打包成 `--windowed` 之后进程没有控制台，`exe --selftest` 只会「跑完什么都没看到」。
    所以结果双通道：有 stdout（源码运行）就原样打；不管有没有都写进
    `<数据目录>/selftest.txt`，让打包产物也是可验收的。
    """
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    rc = 1
    with redirect_stdout(buf):  # selftest 里的 print 全部接走，不必逐个改
        try:
            rc = selftest()
        except Exception:
            traceback.print_exc(file=buf)
    report = buf.getvalue()

    raw = sys.__stdout__  # 原始 stdout，不受上面重定向影响；windowed 下是 None
    if raw is not None:
        raw.write(report)
        raw.flush()

    try:
        p = paths.user_data_dir() / "selftest.txt"
        p.write_text(report + f"\n== 退出码 {rc} ==\n", encoding="utf-8")
    except OSError:
        p = None
    if raw is not None and p is not None:
        raw.write(f"自检报告已写入：{p}\n")
        raw.flush()
    return rc


def _make_ocr_sample(path) -> bool:
    """现场合成一张中英文图。打包冒烟测试不该依赖外部素材（probe/ 不进包）。"""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:  # noqa: BLE001
        return False
    img = Image.new("RGB", (900, 260), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    font = None
    for name in ("msyh.ttc", "msyhbd.ttc", "simhei.ttf", "simsun.ttc"):
        try:
            font = ImageFont.truetype(name, 60)
            break
        except OSError:
            continue
    if font is None:
        font = ImageFont.load_default()
    draw.text((40, 40), "微信聊天记录 2026", fill=(25, 25, 25), font=font)
    draw.text((40, 150), "OCR check 12345", fill=(25, 25, 25), font=font)
    img.save(path)
    return True


def _run_ocr_check(image: str = "") -> int:
    """在**当前进程**里真跑一次 OCR 并把结果落盘。

    为什么需要这个入口：`--windowed` 打包后没有任何命令能看到 OCR 到底能不能用，
    而「`import rapidocr_onnxruntime` 成功」**完全不代表** onnx 模型也打进了包 ——
    那正是最容易漏、又只在实际使用时才炸的一类问题（E38）。
    结果写 `<数据目录>/ocr_check.json`，由 `scripts/build.ps1` 判定 PASS/FAIL。
    """
    import json as _json
    import tempfile
    from pathlib import Path

    import numpy as np

    src = Path(image) if image else Path(tempfile.gettempdir()) / "jev-ocr-check.png"
    report = {"image": str(src), "texts": [], "scores": [], "elapsed_s": 0.0,
              "ok": False, "error": ""}

    def _num(v):
        """引擎返回的 score / elapse 可能是标量，也可能是 list（预处理+cls 分开计时）。"""
        if isinstance(v, (list, tuple)):
            v = v[0] if v else 0.0
        try:
            return round(float(v), 4)
        except (TypeError, ValueError):
            return None

    if not image and not _make_ocr_sample(src):
        report["error"] = "PIL 不可用，无法合成测试图"
    else:
        try:
            from PIL import Image

            from app.capture import ocr as ocr_mod

            arr = np.array(Image.open(src).convert("RGB"))
            res, elapse = ocr_mod.probe_engine(arr)
            report["texts"] = [str(r[1]) for r in (res or [])]
            report["scores"] = [_num(r[2]) for r in (res or [])]
            report["elapsed_s"] = _num(elapse) or 0.0
            report["ok"] = bool(report["texts"])
        except Exception as e:  # noqa: BLE001 - 诊断入口要如实报错而不是抛
            report["error"] = f"{type(e).__name__}: {e}"

    out = paths.user_data_dir() / "ocr_check.json"
    try:
        out.write_text(_json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass

    raw = sys.__stdout__
    if raw is not None:
        raw.write(f"OCR 自检：{'OK' if report['ok'] else 'FAIL'}　"
                  f"识别 {len(report['texts'])} 段　{report['texts']}　"
                  f"{report['error']}\n　→ {out}\n")
        raw.flush()
    return 0 if report["ok"] else 1


def main() -> int:
    ap = argparse.ArgumentParser(prog="jev-chat-analyzer")
    ap.add_argument("--selftest", action="store_true", help="不开窗口，跑自检")
    ap.add_argument("--ocr-check", metavar="IMAGE", nargs="?", const="", default=None,
                    help="对一张图真跑一次 OCR（不给图片就现场合成一张），"
                         "结果写入 <数据目录>/ocr_check.json")
    args = ap.parse_args()

    if args.selftest:
        return _run_selftest()
    if args.ocr_check is not None:
        return _run_ocr_check(args.ocr_check)

    from PySide6.QtWidgets import QApplication

    from app import logging_setup as logs
    from app.ui import _qt
    from app.ui.main_window import MainWindow
    from app.ui.onboarding import maybe_show_onboarding

    # 日志与崩溃兜底要在**建窗口之前**装好：窗口起不来时才能留下线索
    logs.setup()
    logs.install_excepthook()

    cfg = load_config()
    app = QApplication(sys.argv)
    app.setApplicationName("jev-chat-analyzer")
    _qt.apply_theme()

    win = MainWindow(cfg)
    win.show()
    # 启动即扫一次窗口，用户一进来就能看到微信在不在
    try:
        win.page.capture_panel.refresh_windows()
    except Exception as e:
        logs.warn(f"启动扫描窗口失败：{e}", exc_info=True)
    # 首次启动引导：没引导过就弹一次（任何异常都静默跳过，不挡主界面）
    maybe_show_onboarding(cfg, win)
    # 这里以前是 print —— 打包成 windowed 后会被静默丢掉（sys.stdout is None）
    logs.log(f"配置：{cfg.loaded_from}　数据目录：{paths.user_data_dir()}")
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
