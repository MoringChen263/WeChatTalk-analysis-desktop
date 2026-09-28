# -*- coding: utf-8 -*-
"""记忆层的**用例编排**：同意 → 建档（写入）→ 召回 → 撤销。

UI（档案页、分析面板）只调这里，不各自拼 store 调用——
否则「什么时候该弹同意窗」「写入失败算不算整体失败」这类判断会在两处慢慢长歪。

## 关于 `config.memory.enabled`

**官方库里的 `consent_enabled` 才是唯一事实来源**，`config.memory.enabled` 只是一份
**缓存镜像**，作用有两个：给界面省一次子进程（预检时不至于卡），
以及在记忆没开时**不再每次分析都刷一条「召回不可用」的噪声告警**。
真正做决定的地方（召回失败、写入）一律以官方返回码为准，
缓存读到过期信息最多是多一次白跑，不会造成误写。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.memory import consent as consent_mod
from app.memory import extract as ex
from app.memory import store as store_mod


# ------------------------------ 基础取值 ------------------------------
def script_path(config) -> Path:
    return config.memory_script()


def rules_for(config) -> store_mod.OfficialRules:
    return store_mod.load_official(script_path(config))


def max_context_chars(config) -> int:
    try:
        return int(config.get("memory.max_context_chars", 4000))
    except (TypeError, ValueError):
        return 4000


def objects(config) -> list[dict]:
    """配置里的对象列表（说话人确认时写入的 code/label）。"""
    raw = config.get("capture.speaker_map.objects", []) or []
    return [o for o in raw if isinstance(o, dict) and o.get("code")]


def current_subject(config) -> tuple[str, str]:
    """默认对象：(code, label)。没有就回 ("obj-1", "对方")。"""
    objs = objects(config)
    if objs:
        code = str(objs[0].get("code"))
        return code, str(objs[0].get("label") or code)
    return "obj-1", "对方"


def subject_label(config, code: str) -> str:
    if code == "user":
        return "我"
    for o in objects(config):
        if str(o.get("code")) == code:
            return str(o.get("label") or code)
    return code or "未知对象"


# ------------------------------ 状态与缓存 ------------------------------
def stats(config) -> dict:
    """一次 `status` 拿到界面需要的全部状态。失败也返回带 `error` 的字典。"""
    data, _err = store_mod.status_dict(script_path(config))
    sync_cache(config, data)
    return data


def sync_cache(config, st: dict) -> None:
    """把官方状态镜像进 config（只是缓存，见模块头说明）。写盘失败不该影响界面。"""
    want = bool(st.get("consent_enabled")) and not bool(st.get("paused"))
    try:
        if bool(config.get("memory.enabled", False)) != want:
            config.set("memory.enabled", want)
            config.save()
    except Exception:  # noqa: BLE001 - 缓存写不动就算了
        pass


def consent_state(config) -> tuple[bool, str]:
    """(是否需要征求同意, 原因)。原因可直接显示。"""
    rules = rules_for(config)
    return consent_mod.need_consent(rules.policy_version)


# ------------------------------ 同意 ------------------------------
@dataclass
class ConsentPlan:
    """要不要弹同意窗、要不要 enable —— 只读本地文件 + 已拿到的 status，不再起子进程。"""

    enabled: bool = False     # 官方库里 consent_enabled
    paused: bool = False
    exists: bool = False
    need: bool = False        # 需要重新征求同意
    why: str = ""
    policy_version: str = store_mod.FALLBACK_POLICY_VERSION

    @property
    def already_ok(self) -> bool:
        return self.enabled and not self.need


def consent_plan(config, st: dict | None = None) -> ConsentPlan:
    """`st` 可以传已拿到的 `stats()`，省一次 250ms 的子进程。"""
    data = st if st is not None else stats(config)
    rules = rules_for(config)
    need, why = consent_mod.need_consent(rules.policy_version)
    return ConsentPlan(
        enabled=bool(data.get("consent_enabled")), paused=bool(data.get("paused")),
        exists=bool(data.get("exists")), need=need, why=why,
        policy_version=rules.policy_version,
    )


def enable_now(config) -> store_mod.MemoryResult:
    """记下同意之后真正打开库。`--confirm` 由调用方在用户点过同意之后才走这一步。"""
    res = store_mod.enable(script_path(config), confirm=True)
    if res.ok:
        sync_cache(config, {"consent_enabled": True, "paused": False})
    return res


def ensure_consent(config, ask=None, *, st: dict | None = None) -> tuple[bool, str]:
    """确保「用户同意过 + 官方库已启用」。返回 (成功?, 说明)。

    `ask` 是注入进来的「问用户」回调：`ask(why: str) -> bool`。
    核心逻辑因此**不依赖 Qt**，探针里塞一个假回调就能把三条分支全测掉。

    分支：
    - 已同意且库已启用 → 直接 True，不打扰；
    - 需要同意 → 调 `ask`，用户点了才 `accept()` + `enable`；
    - `ask=None` 且需要同意 → 拒绝，**绝不替用户同意**。
    """
    plan = consent_plan(config, st)
    if plan.already_ok:
        return True, ""
    if plan.need:
        if ask is None:
            return False, plan.why
        if not ask(plan.why):
            return False, "已取消：没有同意就不会启用长期记忆。"
        consent_mod.accept(plan.policy_version)
    res = enable_now(config)
    return (True, "") if res.ok else (False, res.describe())


# ------------------------------ 建档（写入） ------------------------------
def review_and_write(config, candidates: list[ex.Candidate], *, parent,
                     subject_label_text: str = "") -> tuple[int, str]:
    """弹复核窗 → 写入勾选项。返回 (成功条数, 给人看的结果说明)。"""
    rules = rules_for(config)
    ex.mark_blocked(candidates, rules=rules)
    if not candidates:
        return 0, "这次没有可存的条目。"
    if not ex.writable(candidates):
        return 0, "所有候选都被官方规则拒绝了，没有可写入的内容。"

    from app.ui.memory_review_dialog import MemoryReviewDialog

    dlg = MemoryReviewDialog(candidates, rules=rules,
                             subject_label=subject_label_text, parent=parent)
    if not dlg.exec():
        return 0, "已取消，什么都没有写入。"
    items = dlg.selected()
    if not items:
        return 0, "没有勾选任何条目，什么都没有写入。"

    script = script_path(config)
    done = 0
    fails: list[str] = []
    for c in items:
        res = store_mod.apply(script, c.to_delta())
        if res.ok:
            done += 1
        else:
            fails.append(f"{c.label}（{res.message or res.code}）")
    if not fails:
        return done, f"已写入 {done} 条长期记忆。"
    if done:
        return done, (f"写入 {done} 条，{len(fails)} 条失败："
                      + "；".join(fails[:3]))
    return 0, "全部写入失败：" + "；".join(fails[:3])


def write_candidates(config, candidates: list[ex.Candidate]) -> tuple[int, str]:
    """不弹窗的写入（给探针/自动化用；界面上一律走 `review_and_write`）。"""
    script = script_path(config)
    done = 0
    fails: list[str] = []
    for c in ex.writable(candidates):
        res = store_mod.apply(script, c.to_delta())
        if res.ok:
            done += 1
        else:
            fails.append(f"{c.label}（{res.message or res.code}）")
    if fails and not done:
        return 0, "全部写入失败：" + "；".join(fails[:3])
    return done, (f"已写入 {done} 条。" + ("失败 " + str(len(fails)) + " 条。" if fails else ""))


# ------------------------------ 召回 ------------------------------
def recall_for(config, subject_code: str = "", label: str = "") -> store_mod.Recall:
    """分析前召回记忆。**任何失败都不拦分析**，只把原因带回去说明。

    缓存说「没开」就直接跳过且不产生告警——否则每次分析都刷一条噪声，
    用户会把真正重要的告警一起忽略掉。
    """
    if not bool(config.get("memory.enabled", False)):
        return store_mod.Recall(ok=False, code="SKIPPED", message="长期记忆未启用")
    code = subject_code or current_subject(config)[0]
    rc = store_mod.recall(script_path(config), subject_id=code,
                          max_chars=max_context_chars(config))
    if not rc.ok and rc.code in ("CONSENT_REQUIRED", "MEMORY_PAUSED", "NOT_INITIALIZED"):
        # 缓存过期了：这一轮没召回成，但别再重复报同样的话
        sync_cache(config, {"consent_enabled": False})
    return rc


def list_memories(config, subject_code: str = "") -> tuple[list[dict], str]:
    return store_mod.memories(script_path(config), subject_id=subject_code)


def hints_for(config, code: str) -> str:
    return store_mod.HINTS.get(code, "")


__all__ = ["ConsentPlan", "consent_plan", "consent_state", "current_subject",
           "enable_now", "ensure_consent", "hints_for", "list_memories",
           "max_context_chars", "objects", "recall_for", "review_and_write",
           "rules_for", "script_path", "stats", "subject_label", "sync_cache",
           "write_candidates"]
