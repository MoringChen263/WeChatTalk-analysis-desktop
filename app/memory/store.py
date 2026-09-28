# -*- coding: utf-8 -*-
"""长期记忆：**只封装官方** `goutoujunshi/scripts/memory_store.py`。

两条调用路径（都在 `run_cli` 里分发，产出同一份 JSON 契约）：

- **源码运行** → 子进程 `[sys.executable, script, *argv]`，有硬超时（`DEFAULT_TIMEOUT`）；
- **打包运行** → 进程内调官方 `main()`。frozen 下 `sys.executable` 是
  `jev-chat-analyzer.exe` 且没有控制台，起子进程等于再开一个 GUI，见 `_run_inproc`（E34）。

`probe/m4_unit.py` 第 [8] 节对两条路径做**输出等价性**断言，防止哪天改动只修好一条。

为什么不做「内置同 schema 存储」（README.optimized §5.4）：
官方实现是纯标准库（sqlite3 + argparse + json），零第三方依赖、可离线、可打包；
再写一份一定会逻辑分叉，封顶/撤销规则两处不一致时最难查。

## 官方 CLI 的真实契约（实测，不是猜的）

子命令与**必须带的旗标**：

    status
    enable   --confirm          # 不带就 CONFIRMATION_REQUIRED
    pause / resume
    apply    --json '<obj>'  |  --file <path>
    undo     [--op-id <id>]
    show     [--subject-id <id>]        # 是旗标，不是位置参数
    context  [--subject-id <id>] [--max-chars 500..8000]
    forget-object <subject_id> --confirm
    revoke   --confirm [--delete]
    clear    --confirm

三条容易踩到的规则：

1. **出错时退出码 1 且 stdout 仍是一段 JSON**：`{"ok": false, "error": {"code","message"}}`。
   把 stdout 当纯文本丢掉，就等于把错误码扔了（UI 只能显示一句听不懂的话）。
2. **子进程 stdout 的编码**。官方 `emit()` 用 `ensure_ascii=False` 打中文，
   而 Windows 下 Python 的 stdout 被重定向到管道时按 `locale`（中文机是 cp936）编码。
   我们按 utf-8 解码就会得到乱码 → 所以必须给子进程设 `PYTHONIOENCODING=utf-8`。
   （进程内路径不受这条影响：`redirect_stdout(StringIO)` 本来就是 Unicode。）
3. **`user` 类别的 subject_id 是字面量 `"user"`**。官方 `list_memories` 查的是
   `subject_id IN ('user', ?)`，所以召回某对象时，「用户档案」是靠这个字面量被捞出来的。

## 关于「不重复实现一遍校验」

`validate_delta()` 的合法性规则（scope × source_type 的交叉约束）很容易写歪。
这里的做法是：**用 importlib 把官方脚本当模块加载**，直接拿它的常量和 `validate_delta`，
只用于**写入前的预检**；真正的写入永远走子进程（官方脚本是唯一权威，
也不违背 §5.4「只使用官方实现」）。模块加载失败就跳过预检，让官方脚本自己去判，
绝不静默改规则。
"""
from __future__ import annotations

import gc
import importlib.util
import io
import json
import os
import subprocess
import sys
import threading
import time
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CREATE_NO_WINDOW = 0x08000000  # Windows：别弹黑框
DEFAULT_TIMEOUT = 30.0

# 进程内调用官方 `main()` 时要独占 `sys.argv` / `sys.stdout`（都是全局的），
# 所以必须是串行的。源码运行下用不到，打包后才走这条路径。
_INPROC_LOCK = threading.Lock()

# 官方给的错误码 → 「用户下一步该做什么」（写法与 analysis/engine.py::_guide 一致）
HINTS: dict[str, str] = {
    "NOT_INITIALIZED": "长期记忆还没启用过。点「启用记忆」，同意后会新建一个空的记忆库。",
    "CONSENT_REQUIRED": "长期记忆没启用，或同意已被撤回。点「启用记忆」并同意后才会存。",
    "MEMORY_PAUSED": "长期记忆当前是暂停状态。点「恢复」再继续。",
    "CONFIRMATION_REQUIRED": "这个操作需要你明确确认（官方脚本要求 --confirm）。",
    "INVALID_DELTA": "要写入的内容不符合官方格式：字段为空、超长，或含控制字符。",
    "SOURCE_NOT_ELIGIBLE": "来源与类别不匹配：模型推断只能写进「假设」，"
                           "聊天记录等外部素材只能写进「事件 / 假设」。",
    "MEMORY_LIMIT_REACHED": "该类别已达条数上限。先在档案页删掉或合并旧条目。",
    "NOTHING_TO_UNDO": "没有可撤销的记忆更新（撤销栈已空）。",
    "TIMEOUT": "官方脚本没在超时时间内返回，稍后重试。",
    "SPAWN_FAILED": "起不了官方脚本子进程（Python 解释器路径或权限问题）。",
    "NO_SCRIPT": "找不到官方 memory_store.py，请确认 skills/goutoujunshi 目录完整。",
    "BAD_OUTPUT": "官方脚本的输出不是预期 JSON（可能有别的进程占着记忆库）。",
    "EXIT_NONZERO": "官方脚本以非 0 退出，但没有给出结构化错误。",
}

# 只在校验无法用时兜底（官方脚本读不到才用），值取自官方 v1
FALLBACK_SCOPE_LIMITS = {"user": 30, "object": 15, "relationship": 10,
                         "event": 20, "hypothesis": 5}
FALLBACK_MAX_VALUE_CHARS = 200
FALLBACK_POLICY_VERSION = "1"

SCOPE_CN = {"user": "用户档案", "object": "对象快照", "relationship": "关系快照",
            "event": "事件", "hypothesis": "假设"}
SRC_CN = {"user_explicit": "用户明确陈述", "user_report": "用户转述",
          "chatlab": "聊天记录", "tool": "外部工具", "assistant_inference": "模型推断"}
CONF_CN = {"high": "高", "medium": "中", "low": "低"}


# ------------------------------ 结果对象 ------------------------------
@dataclass
class MemoryResult:
    """官方脚本一次调用的结果。**失败不抛异常**，一律回这个对象。"""

    ok: bool = False
    code: str = ""
    message: str = ""
    data: Any = None            # 成功时的 JSON payload（拿不到 JSON 时是原始文本）
    raw: str = ""               # 原始输出，出问题时能原样给用户看
    argv: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0

    def __bool__(self) -> bool:
        return self.ok

    def __str__(self) -> str:
        if self.ok:
            return self.message or "成功"
        return self.message or f"失败（{self.code or '原因未知'}）"

    @property
    def hint(self) -> str:
        return HINTS.get(self.code, "")

    def describe(self) -> str:
        """一行给人看：失败了就带上「下一步该怎么办」。"""
        if self.ok:
            return self.message or "成功"
        text = f"{self.message or '失败'}"
        if self.code:
            text = f"[{self.code}] {text}"
        if self.hint:
            text += f"\n→ {self.hint}"
        return text


@dataclass
class Recall:
    """召回结果。记忆不可用**不该拦住分析**，所以失败是空文本 + 原因。"""

    text: str = ""
    count: int = 0
    dropped: int = 0
    ok: bool = False
    code: str = ""
    message: str = ""

    def note(self) -> str:
        if self.ok and self.count:
            extra = f"，另有 {self.dropped} 条因字符上限未注入" if self.dropped else ""
            return f"已召回 {self.count} 条长期记忆{extra}"
        if self.ok:
            return "没有可用的长期记忆"
        return f"记忆召回不可用（{self.message or self.code}），本次按无记忆分析"


# ------------------------------ 子进程调用 ------------------------------
def _child_env() -> dict[str, str]:
    """官方脚本打的是 `ensure_ascii=False` 的中文 JSON，必须让子进程用 utf-8 编码 stdout。"""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"  # 连文件系统编码一起统一，省得读 --file 时又歪一次
    return env


def _use_inproc() -> bool:
    """打包后没有可用的 Python 解释器，只能进程内调用官方 `main()`。"""
    try:
        from app import paths

        return paths.is_frozen()
    except Exception:  # noqa: BLE001 - 判断不了就按源码运行处理
        return False


def _run_inproc(script: Path, argv: list[str]) -> tuple[int, str, str]:
    """在当前进程内执行官方脚本，返回 `(退出码, stdout, stderr)`。

    **为什么必须这样（E34）**：打包后 `sys.executable` 是 `jev-chat-analyzer.exe`，
    而且 `--windowed` 的进程根本没有控制台。原来那套
    `[sys.executable, script, *argv]` 会去**启动第二个 GUI 实例**——
    既拿不到 stdout，还会多弹一个空窗口，记忆层 100% 失效。
    所以 frozen 下改成直接调官方 `main()`（同一个文件、同一份规则，不是复刻实现）。

    与子进程路径的**唯一差异是没有硬超时**：官方脚本是纯本地 sqlite 操作
    （实测毫秒级、无网络 IO），换来的是不必把整个 Python 解释器打进包里。

    复用 `load_official()` 已缓存的模块对象，不会重复执行模块级代码。
    """
    rules = load_official(script)
    if not rules.available:
        return 1, "", f"无法加载官方脚本：{rules.error}"

    out_buf, err_buf = io.StringIO(), io.StringIO()
    rc = 0
    with _INPROC_LOCK:  # sys.argv / sys.stdout 都是全局的，必须串行
        old_argv = sys.argv
        try:
            sys.argv = [str(script), *argv]
            with redirect_stdout(out_buf), redirect_stderr(err_buf):
                rc = int(rules.module.main())
        except SystemExit as e:  # argparse 参数错会 sys.exit(2)
            code = e.code
            rc = code if isinstance(code, int) else (0 if code is None else 2)
        except Exception as e:  # noqa: BLE001 - 官方 main() 只兜它自己认识的异常
            rc = 1
            err_buf.write(f"{type(e).__name__}: {e}")
        finally:
            sys.argv = old_argv
            # 官方每个子命令都写 `with connect() as conn:` —— Python 的 sqlite3
            # 上下文管理器**只提交事务，不关连接**，文件句柄要等 CPython 回收
            # 那个 Connection 才释放；而 Connection 和它的 statement cache 是引用环，
            # 不主动 gc 就一直在。后果很具体：`revoke --delete` / `forget-object`
            # 之后删库会报 WinError 32（Windows 不允许删除仍打开的文件）。
            # 子进程路径靠「进程退出，OS 强制释放句柄」掩盖了这一点，进程内没有这层保护。
            # 顺带也堵住了长时间运行下的 Connection 泄漏（实测一轮回收近千个对象）。
            gc.collect()
    return rc, out_buf.getvalue(), err_buf.getvalue()


def _try_json(text: str) -> Any:
    if not text:
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


def run_cli(script: Path, args: list[str], timeout: float = DEFAULT_TIMEOUT) -> MemoryResult:
    """调官方脚本，返回结构化结果。失败绝不抛，把原因交给 UI。"""
    t0 = time.perf_counter()
    script = Path(script)
    argv = list(args)

    def done(res: MemoryResult) -> MemoryResult:
        res.elapsed_s = round(time.perf_counter() - t0, 3)
        return res

    if not script.exists():
        return done(MemoryResult(False, "NO_SCRIPT", f"记忆脚本不存在：{script}", argv=argv))

    if _use_inproc():
        try:
            rc, out_raw, err_raw = _run_inproc(script, argv)
        except Exception as e:  # noqa: BLE001 - 进程内也不许抛到调用方
            return done(MemoryResult(False, "SPAWN_FAILED",
                                     f"进程内调用官方脚本失败：{e}", argv=argv))
    else:
        cmd = [sys.executable, str(script), *argv]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=timeout,
                               creationflags=CREATE_NO_WINDOW, env=_child_env())
        except subprocess.TimeoutExpired:
            return done(MemoryResult(False, "TIMEOUT", f"超时（>{timeout}s）：{' '.join(argv)}",
                                     argv=argv))
        except OSError as e:
            return done(MemoryResult(False, "SPAWN_FAILED", f"启动失败：{e}", argv=argv))
        rc, out_raw, err_raw = p.returncode, p.stdout, p.stderr

    out = (out_raw or "").strip()
    err = (err_raw or "").strip()
    res = MemoryResult(argv=argv, raw=out or err)
    payload = _try_json(out)

    # 官方的失败也是「退出码 1 + stdout 一段 JSON 信封」——错误码在这里，别丢
    if isinstance(payload, dict):
        err_block = payload.get("error")
        if err_block or payload.get("ok") is False:
            err_block = err_block if isinstance(err_block, dict) else {}
            res.ok = False
            res.code = str(err_block.get("code") or payload.get("code") or "EXIT_NONZERO")
            res.message = str(err_block.get("message") or payload.get("message") or "").strip() \
                or "官方脚本报错"
            res.data = payload
            return done(res)

    if rc != 0:
        res.ok = False
        res.code = "EXIT_NONZERO"
        res.message = err or out or f"退出码 {rc}"
        return done(res)

    if payload is None:
        # 官方每个子命令都走 emit() 打 JSON。退出码 0 却拿不到 JSON 说明输出被污染了，
        # **必须算失败**：否则调用方会拿到空 payload，`status` 就会回落到默认值，
        # 界面上就变成「记忆库未创建」这种谎话。宁可报错，不要瞎猜。
        res.ok = False
        res.code = "BAD_OUTPUT"
        res.message = out or err or "官方脚本没有输出 JSON"
        res.data = out
        return done(res)

    res.ok = True
    res.data = payload
    res.message = err  # 退出码 0 但 stderr 有内容 → 当提示留存，不当失败
    return done(res)


def run_json(script: Path, args: list[str], timeout: float = DEFAULT_TIMEOUT) -> MemoryResult:
    """`run_cli` 的别名，保留旧名字以免已有调用点失效。"""
    return run_cli(script, args, timeout)


# ------------------------------ 官方模块（只用于预检） ------------------------------
_rules_cache: dict[str, "OfficialRules"] = {}


@dataclass
class OfficialRules:
    """把官方脚本当模块加载，拿到权威常量与 `validate_delta`。"""

    module: Any = None
    error: str = ""

    @property
    def available(self) -> bool:
        return self.module is not None

    @property
    def policy_version(self) -> str:
        return str(getattr(self.module, "POLICY_VERSION", FALLBACK_POLICY_VERSION))

    @property
    def scope_limits(self) -> dict[str, int]:
        return dict(getattr(self.module, "SCOPE_LIMITS", FALLBACK_SCOPE_LIMITS))

    @property
    def max_value_chars(self) -> int:
        return int(getattr(self.module, "MAX_VALUE_CHARS", FALLBACK_MAX_VALUE_CHARS))

    @property
    def source_types(self) -> set[str]:
        return set(getattr(self.module, "SOURCE_TYPES", set(SRC_CN)))

    @property
    def scopes(self) -> set[str]:
        return set(self.scope_limits)

    def check_delta(self, delta: dict) -> str:
        """写入前预检。通过返回 ""，否则是人话原因（不是栈）。"""
        if not self.available:
            return ""  # 读不到官方模块就不拦，交给官方脚本自己判
        try:
            self.module.validate_delta(delta)
            return ""
        except Exception as e:  # noqa: BLE001 - 官方自定义异常，不引进依赖
            code = getattr(e, "code", "") or type(e).__name__
            return f"{e}（{code}）"


def load_official(script: Path) -> OfficialRules:
    """把官方脚本按路径 import 成模块（纯标准库、无副作用，实测安全）。结果会缓存。"""
    script = Path(script)
    key = str(script)
    if key in _rules_cache:
        return _rules_cache[key]
    if not script.exists():
        rules = OfficialRules(error=f"找不到 {script}")
        _rules_cache[key] = rules
        return rules
    try:
        spec = importlib.util.spec_from_file_location("goutoujunshi_memory_store", script)
        if spec is None or spec.loader is None:
            raise ImportError("无法为该路径建立 import spec")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        rules = OfficialRules(module=module)
    except Exception as e:  # noqa: BLE001 - 加载失败只降级预检，不影响主流程
        rules = OfficialRules(error=f"{type(e).__name__}: {e}")
    _rules_cache[key] = rules
    return rules


def check_delta(script: Path, delta: dict) -> str:
    return load_official(script).check_delta(delta)


# ------------------------------ 数据目录 ------------------------------
def db_dir() -> Path:
    """记忆库所在目录。优先问官方模块，读不到才按官方规则复刻一份。"""
    override = os.environ.get("GOUTOUJUNSHI_MEMORY_DIR")
    if override:
        return Path(override).expanduser().resolve()
    script = _default_script_hint()
    if script is not None:
        rules = load_official(script)
        if rules.available:
            try:
                return Path(rules.module.memory_dir())
            except Exception:  # noqa: BLE001
                pass
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "goutoujunshi"
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or str(Path.home()))
        return base / "goutoujunshi"
    base = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
    return base / "goutoujunshi"


def db_path() -> Path:
    return db_dir() / "memory.sqlite3"


def _default_script_hint() -> Path | None:
    """`db_dir()` 想用官方规则时得先有个脚本路径；这里只在能推断时给出。"""
    try:
        from app import paths

        return paths.skill_dir("goutoujunshi") / "scripts" / "memory_store.py"
    except Exception:  # noqa: BLE001
        return None


# ------------------------------ 语义化封装 ------------------------------
# UI 只调这些，不自己拼字符串、不自己判断该不该带 --confirm。

def status(script: Path) -> MemoryResult:
    """永远安全：库还不存在时官方也会回 `{"exists": false}`（退出码 0）。"""
    return run_cli(script, ["status"])


# 库不存在时官方**只回 exists/consent_enabled/paused/policy_version/path**，
# 没有 memory_count / undo_count。直接 `.get()` 会拿到 None，界面就会显示「条数：None」。
STATUS_DEFAULTS: dict[str, Any] = {
    "exists": False, "consent_enabled": False, "paused": False,
    "memory_count": 0, "undo_count": 0,
    "policy_version": FALLBACK_POLICY_VERSION, "consent_at": None,
}


def status_dict(script: Path) -> tuple[dict, str]:
    """`status` 的字典版，**默认值已补齐**。返回 (状态, 错误文本)。

    调用方（界面、服务层、自检）一律用这个，不要直接去拆 `status().data`。
    """
    res = status(script)
    out: dict[str, Any] = dict(STATUS_DEFAULTS)
    out["path"] = str(db_path())
    out["ok"] = res.ok
    out["error"] = "" if res.ok else res.describe()
    if res.ok and isinstance(res.data, dict):
        for k in STATUS_DEFAULTS:
            if k in res.data:
                out[k] = res.data[k]
        if res.data.get("path"):
            out["path"] = res.data["path"]
    return out, out["error"]


def enable(script: Path, *, confirm: bool) -> MemoryResult:
    """启用并记录同意。`confirm` 必须是「用户真的点过同意」，不是默认值。"""
    args = ["enable"] + (["--confirm"] if confirm else [])
    return run_cli(script, args)


def pause(script: Path) -> MemoryResult:
    return run_cli(script, ["pause"])


def resume(script: Path) -> MemoryResult:
    return run_cli(script, ["resume"])


def apply(script: Path, delta: dict) -> MemoryResult:
    """写入一条记忆。走 `--json`（列表形式传参，不经 shell，中文不会被转义坏）。"""
    payload = json.dumps(delta, ensure_ascii=False)
    return run_cli(script, ["apply", "--json", payload])


def undo(script: Path, op_id: str = "") -> MemoryResult:
    args = ["undo"] + (["--op-id", op_id] if op_id else [])
    return run_cli(script, args)


def show(script: Path, subject_id: str = "") -> MemoryResult:
    """列出当前生效的记忆。注意官方是 `--subject-id` 旗标，不是位置参数。"""
    args = ["show"] + (["--subject-id", subject_id] if subject_id else [])
    return run_cli(script, args)


def memories(script: Path, subject_id: str = "") -> tuple[list[dict], str]:
    """便捷版：返回 (列表, 错误文本)。UI 列表用这个，不用自己拆 payload。"""
    res = show(script, subject_id)
    if not res.ok:
        return [], res.message or res.code
    data = res.data if isinstance(res.data, dict) else {}
    rows = data.get("memories")
    return (rows if isinstance(rows, list) else []), ""


def context(script: Path, subject_id: str = "", max_chars: int = 4000) -> MemoryResult:
    """召回指纹（官方已按 max_chars 裁过）。未启用时会是 CONSENT_REQUIRED。"""
    chars = max(500, min(8000, int(max_chars)))  # 官方 argparse choices=range(500,8001)
    args = ["context", "--max-chars", str(chars)]
    if subject_id:
        args += ["--subject-id", subject_id]
    return run_cli(script, args)


def forget_object(script: Path, subject_id: str, *, confirm: bool) -> MemoryResult:
    """永久删除某对象的全部记忆，并清空撤销栈（不可撤销）。"""
    args = ["forget-object", subject_id] + (["--confirm"] if confirm else [])
    return run_cli(script, args)


def revoke(script: Path, *, confirm: bool, delete: bool = False) -> MemoryResult:
    """撤回长期记忆同意。`delete=True` 会连库文件一起删掉。"""
    args = ["revoke"] + (["--delete"] if delete else []) + (["--confirm"] if confirm else [])
    return run_cli(script, args)


def clear(script: Path, *, confirm: bool) -> MemoryResult:
    """清空全部记忆（删库文件，不可撤销）。"""
    args = ["clear"] + (["--confirm"] if confirm else [])
    return run_cli(script, args)


# ------------------------------ 召回渲染 ------------------------------
def format_memories(rows: list[dict], max_chars: int = 4000) -> tuple[str, int, int]:
    """把官方 memories 渲染成注入 prompt 的几行字。

    返回 (文本, 实际条数, 因字符上限丢掉的条数)。
    顺序沿用官方的 user → object → relationship → event → hypothesis，
    **优先保留前面的**（档案比单条事件重要），裁也是从尾部裁。
    """
    lines: list[str] = []
    for row in rows:
        scope = str(row.get("scope") or "")
        tag = SCOPE_CN.get(scope, scope)
        sid = str(row.get("subject_id") or "")
        # user 档案的 subject_id 是字面量 "user"，显示出来没有信息量
        head = f"- [{tag}" + (f"·{sid}" if sid and scope != "user" else "") + "]"
        meta = "·".join(x for x in (SRC_CN.get(str(row.get("source_type") or ""),
                                             str(row.get("source_type") or "")),
                                    CONF_CN.get(str(row.get("confidence") or ""),
                                                str(row.get("confidence") or "")),
                                    str(row.get("source_ref") or "")) if x)
        # field 是从 value 截出来的标签，直接拼会读成「A：A，…」；重复时只留正文
        fld = str(row.get("field") or "")
        val = str(row.get("value") or "")
        body = val if (fld and val.startswith(fld)) else (f"{fld}：{val}" if fld else val)
        line = f"{head} {body}"
        when = str(row.get("occurred_at") or "")
        if when:
            line += f"（{when}）"
        if meta:
            line += f"（{meta}）"
        lines.append(line)

    kept: list[str] = []
    used = 0
    for line in lines:
        if kept and used + len(line) + 1 > max_chars:
            break
        kept.append(line)
        used += len(line) + 1
    dropped = len(lines) - len(kept)
    if dropped:
        kept.append(f"（另有 {dropped} 条记忆因字符上限未注入）")
    return "\n".join(kept), len(kept) - (1 if dropped else 0), dropped


def recall(script: Path, subject_id: str = "", max_chars: int = 4000) -> Recall:
    """分析前召回记忆。**任何失败都不拦分析**，只把原因带回去说明。"""
    res = context(script, subject_id=subject_id, max_chars=max_chars)
    if not res.ok:
        return Recall(ok=False, code=res.code, message=res.message)
    data = res.data if isinstance(res.data, dict) else {}
    rows = data.get("memories")
    rows = rows if isinstance(rows, list) else []
    text, count, dropped = format_memories(rows, max_chars=max_chars)
    return Recall(text=text, count=count, dropped=dropped, ok=True)


__all__ = [
    "CONF_CN", "HINTS", "MemoryResult", "OfficialRules", "Recall", "SCOPE_CN", "SRC_CN",
    "apply", "check_delta", "clear", "context", "db_dir", "db_path", "enable",
    "forget_object", "format_memories", "load_official", "memories", "pause", "recall",
    "resume", "revoke", "run_cli", "run_json", "show", "status", "undo",
]
