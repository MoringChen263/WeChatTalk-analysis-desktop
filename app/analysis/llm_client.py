# -*- coding: utf-8 -*-
"""llm_client：OpenAI 兼容的 chat/completions 客户端（README.optimized §5.3 / §7.3）。

只用**标准库 urllib**——不引 requests/httpx/openai：
- 少一个依赖就少一份打包体积与许可证负担（§11.3 体积目标 < 400 MB）；
- 这几家的 `/chat/completions` 都是普通 HTTP + SSE，stdlib 完全够用。

结构化输出走**自适应阶梯**（很多兼容服务只支持其中某一档）：
1. `response_format={"type":"json_schema", ...}` —— 最严格；
2. 被拒就换 `{"type":"json_object"}` —— OpenAI/DeepSeek 的 JSON 模式；
3. 再被拒就**不带** `response_format`，完全靠提示词约束（§7.2 的降级路线）。
降级到哪一档会写进 `LLMResult.response_format_tier`，UI 要如实显示。

取消与超时：
- `cancel` 是 `threading.Event`。**流式**请求把 socket 读超时压到 `CANCEL_POLL_S`（2 秒）一轮，
  每轮检查一次取消，所以「取消」在 2 秒内生效；非流式在一次 POST 期间没有插入点，
  取消只在读响应体的分块之间生效（best-effort），UI 会如实提示「取消中」。
- 非流式读响应体分块进行，总时长受 `llm.request_timeout_s` 约束；
- 流式用「空闲上限」判断卡死：超过 `llm.stream_read_timeout_s` 没新数据才判超时，
  已经吐出的部分会保留（宁可拿半截去走修复/降级，也不整个丢掉）。
"""
from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from app import paths

DEFAULT_TIMEOUT = 60.0
DEFAULT_STREAM_READ_TIMEOUT = 45.0
RETRY_BACKOFF_S = 1.5
CANCEL_POLL_S = 2.0  # 带取消需求时的 socket 轮询间隔：决定「取消」多久生效

# 结构化输出的三档，按优先级从紧到松
TIER_JSON_SCHEMA = "json_schema"
TIER_JSON_OBJECT = "json_object"
TIER_PROMPT_ONLY = "prompt_only"

_FORMAT_HINTS = ("response_format", "json_schema", "json_object", "structured", "unsupported")


def _complains_about_format(err: "LLMError") -> bool:
    """服务端的报错正文是否在抱怨结构化输出参数。用于避免误降档。"""
    blob = f"{err.detail} {err.message}".lower()
    return any(h in blob for h in _FORMAT_HINTS)


class LLMError(Exception):
    """带机器可读 code 的调用失败。UI 按 code 给不同的引导文案。"""

    def __init__(self, code: str, message: str, *, detail: str = "",
                 status: int | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail
        self.status = status

    def __str__(self) -> str:
        extra = f"（HTTP {self.status}）" if self.status else ""
        return f"{self.message}{extra}"


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    @property
    def ok(self) -> bool:
        return bool(self.total_tokens)

    def to_dict(self) -> dict:
        return {"prompt": self.prompt_tokens, "completion": self.completion_tokens,
                "total": self.total_tokens}


@dataclass
class LLMResult:
    text: str
    usage: Usage = field(default_factory=Usage)
    model: str = ""
    elapsed_s: float = 0.0
    streamed: bool = False
    response_format_tier: str = TIER_PROMPT_ONLY
    attempts: int = 1
    cost_cny: float | None = None
    warnings: list[str] = field(default_factory=list)
    finish_reason: str = ""

    @property
    def truncated(self) -> bool:
        """是不是因为 max_tokens 到顶被服务端截断。

        必须与「JSON 写错了」严格区分：截断是**长度不够**，
        把同样的请求原样重发（哪怕换成「修复格式」的说法）还是会在同一个位置被切断，
        唯一有效的动作是**加大 max_tokens**。
        """
        return self.finish_reason == "length"


# ------------------------------ 成本 ------------------------------
def estimate_cost(usage: Usage, model: str, pricing: dict | None) -> float | None:
    """按 config 里的单价估算费用（人民币）。没配单价就返回 None——**不编造金额**。"""
    if not pricing or not usage.ok:
        return None
    rate = None
    for key, val in pricing.items():  # 精确匹配优先，其次取 "*" 兜底
        if key != "*" and key and key.lower() in (model or "").lower():
            rate = val
            break
    if rate is None:
        rate = pricing.get("*")
    if not isinstance(rate, dict):
        return None
    try:
        cin = float(rate.get("in_cny_per_mtok", 0.0))
        cout = float(rate.get("out_cny_per_mtok", 0.0))
    except (TypeError, ValueError):
        return None
    return round(usage.prompt_tokens / 1e6 * cin + usage.completion_tokens / 1e6 * cout, 6)


class CostTracker:
    """当天累计用量。落在用户数据目录，跨进程可见（用户可能开多个窗口）。"""

    def __init__(self, directory: Path | None = None, cap_cny: float | None = None):
        self.dir = Path(directory) if directory else paths.user_data_dir()
        self.cap = float(cap_cny or 0) or None
        self.path = self.dir / f"llm-cost-{time.strftime('%Y-%m-%d')}.json"

    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {"spent_cny": 0.0, "tokens": 0, "calls": 0}

    def spent_cny(self) -> float:
        return float(self._read().get("spent_cny") or 0.0)

    def check(self, priced: bool) -> None:
        """调用前检查。超上限抛 LLMError；没配单价则无法判定金额（由调用方给一条告警）。"""
        if self.cap is None or not priced:
            return
        spent = self.spent_cny()
        if spent >= self.cap:
            raise LLMError("budget_cap",
                           f"今日 LLM 费用已达上限（已用 ¥{spent:.4f} / 上限 ¥{self.cap:.2f}）。"
                           "可在设置里调高，或改用本地模型。")

    def add(self, cost: float | None, tokens: int) -> None:
        rec = self._read()
        rec["spent_cny"] = round(float(rec.get("spent_cny") or 0.0) + float(cost or 0.0), 6)
        rec["tokens"] = int(rec.get("tokens") or 0) + int(tokens or 0)
        rec["calls"] = int(rec.get("calls") or 0) + 1
        rec["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass  # 记账失败不该让分析失败


# ------------------------------ 客户端 ------------------------------
class LLMClient:
    def __init__(self, config, *, tracker: CostTracker | None = None):
        self.config = config
        mode = (config.get("llm.mode", "cloud") or "cloud").lower()
        prefix = "local_llm" if mode in ("local", "ollama") else "llm"
        self.mode = mode
        self.scope = prefix
        self.base_url = (config.get(f"{prefix}.base_url", "") or "").rstrip("/")
        self.model = config.get(f"{prefix}.model", "") or ""
        self.api_key = config.api_key(prefix) if prefix == "llm" else ""
        self.pricing = config.get(f"{prefix}.pricing", None) or config.get("llm.pricing", None)
        self.stream = bool(config.get("llm.stream", True))
        self.temperature = float(config.get("llm.temperature", 1.2))
        self.max_output_tokens = int(config.get("llm.max_output_tokens", 4000))
        self.timeout = float(config.get("llm.request_timeout_s", DEFAULT_TIMEOUT))
        self.read_timeout = float(config.get("llm.stream_read_timeout_s",
                                            DEFAULT_STREAM_READ_TIMEOUT))
        self.max_retries = int(config.get("llm.max_retries", 1))
        self.tracker = tracker if tracker is not None else CostTracker(
            cap_cny=config.get("llm.daily_cost_cap") or None)

    # --------------------------- 对外接口 ---------------------------
    @property
    def endpoint(self) -> str:
        return f"{self.base_url}/chat/completions"

    def ready(self) -> tuple[bool, str]:
        """能不能调用。返回 (ok, 原因)。UI 用它决定是否禁用「开始分析」。"""
        if not self.base_url:
            return False, f"没有配置 `{self.scope}.base_url`"
        if not self.model:
            return False, f"没有配置 `{self.scope}.model`"
        if self.mode not in ("local", "ollama") and not self.api_key:
            return False, "没有可用的 API Key（可在设置里填，或用环境变量 JEV_LLM_API_KEY）"
        return True, ""

    def chat(self, messages: list[dict], *, json_schema: dict | None = None,
             stream: bool | None = None, on_delta=None,
             cancel: threading.Event | None = None,
             max_tokens: int | None = None,
             temperature: float | None = None) -> LLMResult:
        ok, why = self.ready()
        if not ok:
            raise LLMError("no_key" if "Key" in why else "disabled", why)

        self.tracker.check(self.pricing is not None)
        use_stream = self.stream if stream is None else bool(stream)
        t0 = time.perf_counter()

        # 结构化输出阶梯：先试最严的，被拒就下一档。
        # structured_output=off 表示用户明确要求「只靠提示词约束」，就别去争原生 schema。
        structured = str(self.config.get("llm.structured_output", "auto") or "auto").lower()
        tiers = [TIER_JSON_SCHEMA, TIER_JSON_OBJECT, TIER_PROMPT_ONLY] if json_schema \
            and structured != "off" else [TIER_PROMPT_ONLY]
        last_err: LLMError | None = None
        tier_used = TIER_PROMPT_ONLY
        attempts = 0
        for i, tier in enumerate(tiers):
            payload = self._payload(messages, use_stream, tier, json_schema,
                                    max_tokens, temperature)
            attempts += 1
            try:
                res = self._request(payload, use_stream, on_delta, cancel)
                tier_used = tier
                break
            except LLMError as e:
                # 只有「服务端明说不认 response_format」才降档；网络/超时不降档（走重试）。
                # 参数写错（比如 max_tokens 超限）也会返回 400，但正文不会提 response_format，
                # 那种情况必须原样报错，不能悄悄换档位重试。
                if (e.code == "http" and e.status in (400, 404, 415, 422)
                        and i < len(tiers) - 1 and _complains_about_format(e)):
                    last_err = e
                    continue
                raise
        else:  # pragma: no cover - 上面的 break 一定命中或抛错
            raise last_err or LLMError("http", "调用失败")

        text = res.text
        usage = res.usage
        cost = estimate_cost(usage, self.model, self.pricing)
        self.tracker.add(cost, usage.total_tokens)
        used_max = int(max_tokens or self.max_output_tokens)
        warns: list[str] = []
        if self.pricing is None:
            warns.append("未配置 `llm.pricing` 单价：本次无法估算费用"
                         + ("，每日成本上限也不会生效" if self.tracker.cap else ""))
        if res.truncated:
            warns.append(f"输出被 max_tokens={used_max} 截断（finish_reason=length），"
                         f"拿到的是**不完整的**内容。请调大 `llm.max_output_tokens`。")
        if tier_used != TIER_JSON_SCHEMA and json_schema:
            warns.append(
                "该服务不支持原生 JSON Schema，已退到"
                + ("JSON 模式" if tier_used == TIER_JSON_OBJECT else "仅提示词约束")
                + "（解析失败会自动走修复与降级）")

        return LLMResult(
            text=text, usage=usage, model=self.model,
            elapsed_s=round(time.perf_counter() - t0, 2),
            streamed=use_stream, response_format_tier=tier_used,
            attempts=attempts, cost_cny=cost, warnings=warns,
            # 必须带出来：截断判定全靠它。漏传过一次，结果 truncated 恒为 False，
            # 「输出超限」被误当成「模型 JSON 写错」，白白误导排查方向。
            finish_reason=res.finish_reason,
        )

    def list_models(self) -> list[str]:
        """探测服务端可用模型（设置面板用）。失败返回空列表，不抛。"""
        url = f"{self.base_url}/models"
        req = self._mk_request(url, None)
        try:
            with urllib.request.urlopen(req, timeout=min(self.timeout, 20)) as r:
                data = json.loads(r.read().decode("utf-8"))
            return [m.get("id", "") for m in data.get("data", []) if m.get("id")]
        except Exception:
            return []

    # --------------------------- 内部 ---------------------------
    def _mk_request(self, url: str, body: dict | None):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return urllib.request.Request(url, data=data, headers=headers,
                                      method="POST" if body is not None else "GET")

    def _payload(self, messages, use_stream: bool, tier: str, schema: dict | None,
                 max_tokens: int | None, temperature: float | None) -> dict:
        p: dict = {
            "model": self.model,
            "messages": messages,
            "max_tokens": int(max_tokens or self.max_output_tokens),
            "temperature": self.temperature if temperature is None else float(temperature),
            "stream": bool(use_stream),
        }
        if tier == TIER_JSON_SCHEMA and schema:
            p["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "analysis", "schema": schema, "strict": False},
            }
        elif tier == TIER_JSON_OBJECT:
            p["response_format"] = {"type": "json_object"}
        if use_stream:
            p["stream_options"] = {"include_usage": True}  # 不支持的服务端会忽略
        return p

    def _request(self, payload: dict, use_stream: bool, on_delta, cancel) -> LLMResult:
        last: LLMError | None = None
        for attempt in range(self.max_retries + 1):
            if cancel is not None and cancel.is_set():
                raise LLMError("cancelled", "已取消")
            try:
                return self._once(payload, use_stream, on_delta, cancel)
            except LLMError as e:
                last = e
                retryable = e.code in ("network", "timeout") or (e.code == "http"
                                                                 and (e.status or 0) >= 500)
                if not retryable or attempt >= self.max_retries:
                    raise
                if cancel is not None and cancel.wait(RETRY_BACKOFF_S):
                    raise LLMError("cancelled", "已取消")
        raise last or LLMError("http", "调用失败")

    def _poll_interval(self, cancel, use_stream: bool) -> float:
        """socket 读超时。

        **流式**才做短轮询：流式是「读一行→检查取消→再读一行」的循环，把 socket 超时
        压到 2 秒就能让「取消」在 2 秒内生效。
        **非流式**一次 POST 只能阻塞等响应头，中途没有可插入检查点；此时如果也用短超时，
        只会把「模型首字节慢」误判成超时，而我们**绝不能靠重发 POST 来轮询**——
        那会让服务端把同一个请求算两次钱。所以非流式保持配置的原语义，
        取消只在读取响应体的分块之间（best-effort）生效，UI 也据此提示「取消中」。
        """
        if cancel is None or not use_stream:
            return self.timeout
        return max(0.5, min(CANCEL_POLL_S, self.timeout, self.read_timeout))

    def _once(self, payload: dict, use_stream: bool, on_delta, cancel) -> LLMResult:
        req = self._mk_request(self.endpoint, payload)
        poll = self._poll_interval(cancel, use_stream)
        try:
            with urllib.request.urlopen(req, timeout=poll) as r:
                if use_stream:
                    return self._read_stream(r, on_delta, cancel, poll)
                return self._parse_response(self._read_body(r, cancel))
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")[:800]
            except Exception:
                pass
            raise LLMError(self._http_code(e.code), self._http_hint(e.code, body),
                           detail=body, status=e.code) from e
        except urllib.error.URLError as e:
            if isinstance(getattr(e, "reason", None), socket.timeout):
                raise LLMError("timeout", f"请求超时（{self.timeout:.0f} 秒）："
                                          f"{self.endpoint}") from e
            raise LLMError("network", f"连不上服务端：{getattr(e, 'reason', e)}",
                           detail=str(e)) from e
        except socket.timeout as e:
            raise LLMError("timeout", f"请求超时（{self.timeout:.0f} 秒）") from e
        except OSError as e:
            raise LLMError("network", f"网络异常：{e}") from e

    def _read_body(self, resp, cancel) -> str:
        """分块读响应体：既能中途响应取消，也保证总时长不超过 request_timeout_s。"""
        parts: list[bytes] = []
        t0 = time.monotonic()
        while True:
            if cancel is not None and cancel.is_set():
                raise LLMError("cancelled", "已取消")
            try:
                part = resp.read(65536)
            except socket.timeout as e:
                if cancel is not None and cancel.is_set():
                    raise LLMError("cancelled", "已取消") from e
                raise LLMError("timeout", f"读取响应超时（{self.timeout:.0f} 秒）") from e
            if not part:
                break
            parts.append(part)
            if time.monotonic() - t0 > self.timeout:
                raise LLMError("timeout", f"读取响应超过 {self.timeout:.0f} 秒上限")
        return b"".join(parts).decode("utf-8", errors="replace")

    def _read_stream(self, resp, on_delta, cancel, poll) -> LLMResult:
        chunks: list[str] = []
        usage = Usage()
        model = self.model
        finish = ""
        last_data = time.monotonic()
        while True:
            if cancel is not None and cancel.is_set():
                raise LLMError("cancelled", "已取消")
            try:
                raw = resp.readline()
            except socket.timeout as e:
                idle = time.monotonic() - last_data
                if idle > self.read_timeout:
                    if chunks:  # 已经吐了一部分：保留已得内容，别丢掉
                        break
                    raise LLMError("timeout",
                                   f"服务端 {self.read_timeout:.0f} 秒没有返回任何数据") from e
                continue  # 还没到空闲上限：回去检查取消
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace").strip()
            if not line or line.startswith(":") or not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                continue
            last_data = time.monotonic()
            model = obj.get("model") or model
            if obj.get("usage"):
                u = obj["usage"]
                usage = Usage(int(u.get("prompt_tokens") or 0),
                              int(u.get("completion_tokens") or 0),
                              int(u.get("total_tokens") or 0))
            for ch in obj.get("choices") or []:
                # finish_reason 只在最后一个 chunk 出现，必须边读边记
                if ch.get("finish_reason"):
                    finish = str(ch["finish_reason"])
                delta = (ch.get("delta") or {}).get("content")
                if delta:
                    chunks.append(delta)
                    if on_delta is not None:
                        on_delta(delta)
        text = "".join(chunks)
        if not text.strip():
            if finish == "length":
                # 推理型模型可能把整个额度花在思考链上，正文一个字都没吐出来
                raise LLMError("truncated",
                               "输出被 max_tokens 截断：模型还没开始写正文就用完了额度"
                               "（推理型模型会把额度花在思考上，需要更大的上限）")
            raise LLMError("empty", "服务端返回了空内容（可能是模型拒绝或额度问题）")
        return LLMResult(text=text, usage=usage, model=model, streamed=True,
                         finish_reason=finish)


    @staticmethod
    def _parse_response(raw: str) -> LLMResult:
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as e:
            raise LLMError("http", f"返回内容不是合法 JSON：{e}", detail=raw[:500]) from e
        usage = Usage()
        if obj.get("usage"):
            u = obj["usage"]
            usage = Usage(int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0),
                          int(u.get("total_tokens") or 0))
        choices = obj.get("choices") or []
        if not choices:
            err = obj.get("error") if isinstance(obj.get("error"), dict) else None
            msg = (err or {}).get("message") or f"返回里没有 choices：{raw[:200]}"
            raise LLMError("http", f"服务端返回错误：{msg}", detail=raw[:500])
        text = (choices[0].get("message") or {}).get("content") or ""
        finish = str(choices[0].get("finish_reason") or "")
        if not text.strip():
            if finish == "length":
                raise LLMError("truncated",
                               "输出被 max_tokens 截断：模型还没开始写正文就用完了额度"
                               "（推理型模型会把额度花在思考上，需要更大的上限）")
            raise LLMError("empty", "服务端返回了空内容")
        return LLMResult(text=text, usage=usage, model=obj.get("model") or "",
                         streamed=False, finish_reason=finish)

    @staticmethod
    def _http_code(status: int) -> str:
        if status in (401, 403):
            return "no_key"
        if status == 429:
            return "rate_limit"
        if status in (402,):
            return "quota"
        return "http"

    @staticmethod
    def _http_hint(status: int, body: str) -> str:
        hints = {
            400: "服务端判定请求不合规（可能是该模型不支持当前参数或结构化输出档位）",
            401: "API Key 无效或未授权",
            403: "该 Key 无权使用这个模型",
            404: "接口路径不存在，检查 base_url 是否漏了 /v1",
            422: "请求参数被拒绝",
            429: "触发限流，稍后重试或降低频率",
            402: "账户额度不足",
        }
        tail = body.strip().replace("\n", " ")[:200]
        return f"{hints.get(status, f'HTTP {status}')}" + (f"：{tail}" if tail else "")


__all__ = ["LLMClient", "LLMError", "LLMResult", "Usage", "CostTracker", "estimate_cost",
           "TIER_JSON_SCHEMA", "TIER_JSON_OBJECT", "TIER_PROMPT_ONLY"]
