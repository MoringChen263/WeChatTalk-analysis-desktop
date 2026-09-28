# -*- coding: utf-8 -*-
"""配置读写 + 凭据解析。

两条硬规则（见 README.optimized §8.4 / §10）：
1. `config.json` 里**永不出现明文密钥**，只存 `api_key_ref`（env: / dpapi: / keyring:）。
2. `config.json` 里**不出现任何绝对个人路径**。
"""
from __future__ import annotations

import base64
import copy
import ctypes
import ctypes.wintypes as wt
import json
import os
from pathlib import Path
from typing import Any

from app import paths

# 配置结构版本。**凡是「会改变行为的默认值」发生变更，就 +1 并登记进 MIGRATIONS**。
# 原因见 MIGRATIONS 的注释：不改的话，老配置里固化的旧默认值会把新默认值永久压住，
# 现象是「代码明明改了却没生效」，很难往配置上想。
CONFIG_VERSION = 3

# 版本号 → {点号键: (旧默认值, 新默认值)}
# 只在「文件里的值恰好等于旧默认值」时才替换：用户自己调过的值不动。
MIGRATIONS: dict[int, dict[str, tuple[Any, Any]]] = {
    2: {
        # v1 的 1200 撑不下五步 + 四版话术的 JSON（实测 4205 字），真机必然截断。
        "llm.max_output_tokens": (1200, 4000),
    },
    3: {
        # 0.7 是「写报告」的温度，话术要的是聊天感。前作 jev-chat-windows 真机实测：
        # 0.8 出来的回复已经像客服，1.2（DeepSeek 闲聊档位）才是活人写的字。
        # 用户手动调过（≠0.7）的值原样保留。
        "llm.temperature": (0.7, 1.2),
    },
}

# 与仓库根 config.example.json 保持一致；这里内嵌一份，保证「首次运行无需任何外部文件」
DEFAULTS: dict[str, Any] = {
    "config_version": CONFIG_VERSION,
    "llm": {
        "provider": "openai-compatible",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "api_key_ref": "env:JEV_LLM_API_KEY",
        # 1.2 = DeepSeek 闲聊档位：话术的活人感主要靠它（0.7 出来的字像报告）。
        "temperature": 1.2,
        "mode": "cloud",
        # 五步 + 四版话术的 JSON 实测要 3k+ tokens。给少了不是报错，而是
        # 「JSON 写一半」被当成格式错误——极难定位，所以默认值直接给够。
        "max_output_tokens": 4000,
        "truncation_retry_max": 8000,
        "request_timeout_s": 60,
        "daily_cost_cap": 2.0,
        "stream": True,
        "structured_output": "auto",
        "stream_read_timeout_s": 45,
        "max_retries": 0,
        "pricing": {},
    },
    "local_llm": {
        "base_url": "http://127.0.0.1:11434/v1",
        "model": "qwen2.5:7b",
        "api_key_ref": "",
    },
    "capture": {
        "target_process": ["Weixin.exe", "WeChat.exe"],
        "title_hint": "微信",
        "window_class_fallback": "",
        "hwnd": 0,
        "poll_ms": 5000,
        "scroll_frames": 0,
        "ocr_min_score": 0.9,
        "dedup": {"match_ratio": 0.85, "dup_threshold": 0.15, "k": 5},
        "speaker_map": {
            # 「我」在哪一侧：由 OCR 底色分类结果反推为**建议值**，必须用户点一次确认才落 confirmed
            "me": {"side": "right", "code": "me", "confirmed": False},
            "objects": [{"code": "obj-1", "side": "left", "label": "对方"}],
        },
    },
    "transcript": {"max_messages": 60, "token_budget": 3500},
    # 导入已有导出文件（txt/html/ChatLab agent JSON）：上限比采集宽，长历史才不至于被截断
    "import": {"max_messages": 2000},
    "memory": {
        "enabled": False,
        "max_context_chars": 4000,
        "store_script": "skills/goutoujunshi/scripts/memory_store.py",
    },
    "analysis": {
        "question_type": "default",
        "max_references": 3,
        # 单份参考与本次全部参考的字符上限（超了 head 截断并留可见标记）
        "ref_chars": 9000,
        "total_ref_chars": 24000,
        # 模型没给出合规 JSON 时是否再花一次调用做格式修复（只在渲染不出来时才触发）
        "repair_retry": True,
        # 用户自述的说话风格（设置页可填）：一句话描述口吻，只影响话术措辞，分析照旧。
        "style_note": "",
        # 「关系趋势/长记录」需要更长历史：预算放大倍数
        "wide_context_multiplier": 2.0,
    },
    "skill_source": "skills/goutoujunshi",
    "laya": {"enabled": False, "model_dir": ""},
    # 首次启动引导：完成 / 跳过 由 UI 写这里。新键，默认值 False 即「还没引导过」。
    # 已有旧配置没有这个键，cfg.get 会回退到 False，于是老用户也会看到一次（无害、可关）。
    "onboarding": {"done": False},
    "privacy": {
        "screenshots_to_disk": False,
        "log_chat_text": False,
        "cloud_upload_confirmed": False,
    },
    "update": {"enabled": True, "feed_url": ""},
}

# 不允许被配置文件覆盖的常量项（防呆）
FORCED: dict[str, Any] = {"privacy.screenshots_to_disk": False}


# ----------------------------- DPAPI -----------------------------
class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> _DataBlob:
    buf = ctypes.create_string_buffer(data, len(data))
    return _DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def dpapi_protect(text: str) -> str:
    """用当前 Windows 用户的 DPAPI 加密，返回 base64。仅该用户可解。"""
    out = _DataBlob()
    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(_blob(text.encode("utf-8"))), None, None, None, None, 0, ctypes.byref(out))
    if not ok:
        raise OSError("CryptProtectData 失败")
    try:
        return base64.b64encode(ctypes.string_at(out.pbData, out.cbData)).decode("ascii")
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def dpapi_unprotect(b64: str) -> str:
    raw = base64.b64decode(b64)
    out = _DataBlob()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(_blob(raw)), None, None, None, None, 0, ctypes.byref(out))
    if not ok:
        raise OSError("CryptUnprotectData 失败（换了 Windows 用户或换了机器？）")
    try:
        return ctypes.string_at(out.pbData, out.cbData).decode("utf-8")
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def resolve_key(ref: str) -> str:
    """把 api_key_ref 解析成真正的密钥。解析不了返回空串（不要抛，让上层提示用户）。"""
    if not ref:
        return ""
    kind, _, rest = ref.partition(":")
    try:
        if kind == "env":
            return os.environ.get(rest, "")
        if kind == "dpapi":
            return dpapi_unprotect(rest)
        if kind == "keyring":
            import keyring  # 可选依赖
            service, _, account = rest.partition("/")
            return keyring.get_password(service, account) or ""
    except Exception:
        return ""
    return ""  # 未知前缀：当作没配


# ----------------------------- Config -----------------------------
def _dig(d: dict, dotted: str) -> Any:
    cur: Any = d
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _poke(d: dict, dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    cur = d
    for p in parts[:-1]:
        nxt = cur.get(p)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[p] = nxt
        cur = nxt
    cur[parts[-1]] = value


def migrate(user: dict) -> tuple[dict, list[str]]:
    """把老配置里「固化的旧默认值」升级成新默认值，返回 (新配置, 迁移说明)。

    为什么必须做：配置文件里存着的值**无法区分**「用户主动设的」和「上一版的默认值」。
    不做迁移的话，改了默认值对已有配置完全无效——而且现象是「代码明明改了却没生效」，
    几乎不会有人先怀疑到配置头上。

    只替换「恰好等于旧默认值」的项：用户自己调过的值（比如故意设成 2000）原样保留。
    """
    try:
        ver = int(user.get("config_version") or 1)
    except (TypeError, ValueError):
        ver = 1
    if ver >= CONFIG_VERSION:
        return user, []
    out = copy.deepcopy(user)
    notes: list[str] = []
    for v in range(ver + 1, CONFIG_VERSION + 1):
        for dotted, (old, new) in (MIGRATIONS.get(v) or {}).items():
            if _dig(out, dotted) == old:
                _poke(out, dotted, new)
                notes.append(f"{dotted}: {old} → {new}")
    out["config_version"] = CONFIG_VERSION
    return out, notes


def _deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


class Config:
    """点号路径读写：cfg.get("capture.poll_ms") / cfg.set("llm.model", "x")。"""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else paths.config_path()
        self.data: dict[str, Any] = copy.deepcopy(DEFAULTS)
        self.loaded_from: str = "defaults"
        self.migrations: list[str] = []      # 本次加载做过哪些默认值迁移（可显示给人看）

    # -- io --
    def load(self) -> "Config":
        if self.path.exists():
            with open(self.path, "r", encoding="utf-8") as f:
                user = json.load(f)
            user, self.migrations = migrate(user)
            self.data = _deep_merge(DEFAULTS, user)
            self.loaded_from = str(self.path)
        else:
            self.loaded_from = "defaults"
        self._apply_forced()
        return self

    def save(self) -> Path:
        self._apply_forced()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 只写「与默认值不同」的键，不写全量。
        # 写全量的话，默认值以后怎么改都影响不到已有配置——文件里的旧值会一直压住 DEFAULTS。
        # 实测踩过：max_output_tokens 从 1200 改成 4000，可 .devdata/config.json 里
        # 固化的 1200 仍然生效，真机照旧被截断，而且看起来像「改了没生效」的灵异问题。
        text = json.dumps(dict(self._diff(DEFAULTS, self.data),
                               config_version=CONFIG_VERSION),   # 始终记版本，迁移才有依据
                          ensure_ascii=False, indent=2)
        tmp = self.path.with_suffix(".json.tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
            tmp.replace(self.path)  # 原子替换，避免写到一半断电留下坏配置
        except OSError:
            # 某些受管/重定向目录不允许 rename（沙箱、同步盘、杀软占用）→ 退回直接写。
            tmp.unlink(missing_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                f.write(text)
        return self.path

    @staticmethod
    def _diff(base: dict, cur: dict) -> dict:
        """递归取差集：只保留用户真正改过（或默认值里没有）的键。"""
        out: dict = {}
        for k, v in (cur or {}).items():
            if k not in base:
                out[k] = copy.deepcopy(v)          # 默认值里没有的键：一律保留
            elif isinstance(v, dict) and isinstance(base.get(k), dict):
                sub = Config._diff(base[k], v)
                if sub:
                    out[k] = sub
            elif v != base.get(k):
                out[k] = copy.deepcopy(v)          # 改过的值
        return out

    def _apply_forced(self) -> None:
        for k, v in FORCED.items():
            self.set(k, v, force=True)

    # -- access --
    def get(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self.data
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def set(self, dotted: str, value: Any, force: bool = False) -> None:
        if dotted in FORCED and not force:
            return
        parts = dotted.split(".")
        cur = self.data
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = value

    # -- credentials --
    def api_key(self, which: str = "llm") -> str:
        return resolve_key(self.get(f"{which}.api_key_ref", "") or "")

    def store_api_key_dpapi(self, key: str, which: str = "llm") -> None:
        """把密钥加密后写进 config 的 api_key_ref（明文永不落盘）。"""
        self.set(f"{which}.api_key_ref", "dpapi:" + dpapi_protect(key))
        self.save()

    # -- 便捷 --
    def skill_path(self) -> Path:
        p = Path(self.get("skill_source", "skills/goutoujunshi"))
        return p if p.is_absolute() else (paths.app_root() / p)

    def memory_script(self) -> Path:
        p = Path(self.get("memory.store_script", "skills/goutoujunshi/scripts/memory_store.py"))
        return p if p.is_absolute() else (paths.app_root() / p)


def load_config(path: Path | None = None) -> Config:
    return Config(path).load()