# -*- coding: utf-8 -*-
"""长期记忆的**同意门控**（README.optimized §5.4 / §8）。

两条职责分开，不要混：
- **官方库里的 `consent_enabled`** 才是「记忆到底开没开」的唯一事实来源（`enable/revoke` 维护它）。
- 本模块只记「用户**看过并同意过**这份说明，且在哪个版本上同意的」，落在
  `user_data_dir()/memory_consent.json`。

为什么还要单独记一份：

1. 只说「库存在 = 同意过」是不够的——用户可能早就用 skill 建过库，但那是**别处**的同意，
   本应用第一次写入前仍必须让他看到「这个程序会往哪里写、怎么写、怎么删」。
2. 说明文案会变。`POLICY_VERSION` 或文案哈希对不上时**重新征求同意**，
   而不是拿一份三年前的同意一直往下用。

同意是**可撤回**的：撤回时这里和官方库两边都要动（见 `forget()` 与 `store.revoke`）。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app import paths

CONSENT_FILE = "memory_consent.json"

# 给用户看的原话。三条必须说清：存什么、存哪、怎么删。
# 改这段文字会让 text_hash 变化 → 下次启用时重新征求同意（这是有意的）。
POLICY_TEXT = """\
长期记忆会把「关于你和这个对象的稳定信息」存在本机，下次分析时自动带上，
这样你不用每次都重新交代背景。具体是：

【存什么】
· 用户档案、对象快照、关系快照：只存你在应用里**明确填写或确认**的内容，不猜；
· 事件：从聊天记录里读到的客观发生（带原始消息编号，可追溯）；
· 假设：模型根据记录做的推断，**永远标着「模型推断」和置信度**，不会当成事实。

【存在哪】
· 只存本机：{db_dir}
· 纯本地 SQLite 文件，不联网、不上传、不参与任何云同步。
· 单条不超过 {max_value_chars} 字；总条数不超过 {max_rows} 条；最早的会被自动清理。

【你能随时反悔】
· 「暂停」：立刻停止读写，已存内容保留；
· 「撤销上一条」：退回最近一次写入（保留最近 {max_ops} 次操作历史）；
· 「删除某个对象」/「清空全部」/「撤回同意」：永久删除，不可恢复。

【边界】
· 不做心理诊断，不给法律意见；
· 涉及人身安全的内容请以现实中的求助为先，记忆功能不是安全保障。
"""


@dataclass
class Consent:
    accepted: bool = False
    at: str = ""                 # 用户点同意的时间（本地时区 ISO）
    policy_version: str = ""
    text_hash: str = ""
    path: str = ""

    def to_dict(self) -> dict:
        return {"accepted": self.accepted, "at": self.at,
                "policy_version": self.policy_version, "text_hash": self.text_hash}


def _file() -> Path:
    return paths.user_data_dir() / CONSENT_FILE


def text_hash() -> str:
    return hashlib.sha256(POLICY_TEXT.encode("utf-8")).hexdigest()[:16]


def policy_text(rules=None) -> str:
    """把说明文案里的占位符填成真实数字（数字来自官方脚本，不自己编）。"""
    from app.memory import store as store_mod

    limits = rules.scope_limits if rules is not None and rules.available else store_mod.FALLBACK_SCOPE_LIMITS
    max_rows = 200
    max_ops = 20
    if rules is not None and rules.available:
        max_rows = int(getattr(rules.module, "MAX_ROWS", max_rows))
        max_ops = int(getattr(rules.module, "MAX_OPERATIONS", max_ops))
    return POLICY_TEXT.format(
        db_dir=str(store_mod.db_dir()),
        max_value_chars=(rules.max_value_chars if rules is not None and rules.available
                         else store_mod.FALLBACK_MAX_VALUE_CHARS),
        max_rows=max_rows,
        max_ops=max_ops,
    )


def load() -> Consent:
    p = _file()
    if not p.exists():
        return Consent(path=str(p))
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("consent file is not an object")
    except (OSError, ValueError):
        # 文件坏了就当没同意过——宁可多问一次，也不拿一份读不懂的记录当同意
        return Consent(path=str(p))
    return Consent(
        accepted=bool(raw.get("accepted")),
        at=str(raw.get("at") or ""),
        policy_version=str(raw.get("policy_version") or ""),
        text_hash=str(raw.get("text_hash") or ""),
        path=str(p),
    )


def need_consent(policy_version: str) -> tuple[bool, str]:
    """要不要弹同意窗。返回 (需要?, 原因——可直接显示给用户)。"""
    c = load()
    if not c.accepted:
        return True, "还没同意过长期记忆说明"
    if c.policy_version != str(policy_version):
        return True, (f"记忆策略版本变了（你上次同意的是 {c.policy_version or '未知'}，"
                      f"当前是 {policy_version}），需要重新确认")
    if c.text_hash != text_hash():
        return True, "说明文案有更新，需要重新确认一次"
    return False, ""


def accept(policy_version: str) -> Consent:
    """记下同意。**只应在用户真的点过「同意」之后调用。**"""
    p = _file()
    c = Consent(accepted=True,
                at=datetime.now().astimezone().isoformat(timespec="seconds"),
                policy_version=str(policy_version), text_hash=text_hash(), path=str(p))
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    text = json.dumps(c.to_dict(), ensure_ascii=False, indent=2)
    try:
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(p)
    except OSError:
        tmp.unlink(missing_ok=True)
        p.write_text(text, encoding="utf-8")
    return c


def forget() -> None:
    """撤回同意时清掉本地记录（官方库那边的 `consent_enabled` 由 `store.revoke` 处理）。"""
    _file().unlink(missing_ok=True)


__all__ = ["CONSENT_FILE", "POLICY_TEXT", "Consent", "accept", "forget", "load",
           "need_consent", "policy_text", "text_hash"]
