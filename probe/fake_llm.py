# -*- coding: utf-8 -*-
"""可编排的假 LLM 服务端：让 M3 的每条路径都能离线测（不需要真 key、不花钱）。

用 `http.server` 起在 127.0.0.1 的随机端口上，按脚本逐次返回预设行为：
正常 JSON、流式 SSE、按档位拒绝 response_format、5xx、超时、错误信封……
`calls` 记录每次收到的请求体，测试可以断言「真的把 json_schema 换成 prompt_only 了」。

用法：
    srv = FakeLLM([Behavior(kind="json", body=ANALYSIS_JSON)])
    srv.start()
    ... 用 srv.base_url 建客户端 ...
    srv.stop()
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


@dataclass
class Behavior:
    """一次请求该怎么回应。

    kind:
      json    —— 返回 choices[0].message.content = body（非流式）
      text    —— 同 json（语义上表示"内容不是 JSON"）
      sse     —— 流式：chunks 逐块吐出（不给就是按 body 切段）
      status  —— 直接返回 HTTP 错误，detail 是正文
      hang    —— 不回应，用来触发超时
    """

    kind: str = "json"
    body: str = ""
    status: int = 200
    detail: str = ""
    chunks: list[str] | None = None
    usage: dict = field(default_factory=lambda: {"prompt_tokens": 1200,
                                                 "completion_tokens": 300,
                                                 "total_tokens": 1500})
    model: str = "fake-model"
    delay: float = 0.0
    hang: float = 0.0
    # 流式：吐完第 stall_after 块后停住 stall_for 秒（测「生成中途取消」）
    stall_after: int = -1
    stall_for: float = 0.0
    # 命中这些 response_format.type 就返回 400（模拟"只支持某几档"的服务端）
    reject_formats: tuple[str, ...] = ()
    # 结束原因。设成 "length" 就是「输出被长度上限截断」——真机最容易踩、
    # 也最容易被误判成「模型 JSON 写错」的一种失败，必须能离线复现。
    finish_reason: str = "stop"


class FakeLLM:
    def __init__(self, script: list[Behavior] | None = None):
        self.script: list[Behavior] = list(script or [])
        self.calls: list[dict] = []
        self.headers_seen: list[dict] = []
        self._srv: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # --------------------------- 生命周期 ---------------------------
    @property
    def base_url(self) -> str:
        host, port = self._srv.server_address[:2]
        return f"http://{host}:{port}/v1"

    def start(self) -> "FakeLLM":
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):  # 静音
                pass

            def _next(self) -> Behavior:
                if not outer.script:
                    return Behavior(kind="json", body="{}")
                return outer.script.pop(0) if len(outer.script) > 1 else outer.script[0]

            def do_GET(self):  # /models
                if self.path.endswith("/models"):
                    self._send_json({"data": [{"id": "fake-model"}, {"id": "fake-mini"}]})
                else:
                    self.send_error(404)

            def do_POST(self):
                if not self.path.endswith("/chat/completions"):
                    # 真实服务端在路径不对时给 404（用来测「base_url 漏了 /v1」的提示）
                    return self._send_json({"error": {"message": "unknown path"}}, status=404)
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode("utf-8", errors="replace")
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    payload = {"_raw": raw}
                outer.calls.append(payload)
                outer.headers_seen.append({k.lower(): v for k, v in self.headers.items()})

                b = self._next()
                if b.delay:
                    time.sleep(b.delay)
                if b.hang:
                    time.sleep(b.hang)  # 超过客户端读超时 → 触发 timeout 分支
                    return

                rf = (payload.get("response_format") or {}).get("type")
                if rf and rf in b.reject_formats:
                    return self._send_json(
                        {"error": {"message": f"response_format type '{rf}' is not supported "
                                              f"by this model"}},
                        status=400)

                if b.kind == "status":
                    return self._send_json({"error": {"message": b.detail or "boom"}},
                                           status=b.status)

                if b.kind == "sse" or payload.get("stream"):
                    chunks = b.chunks
                    if chunks is None:
                        body = b.body
                        step = max(1, len(body) // 4) or 1
                        chunks = [body[i:i + step] for i in range(0, len(body), step)]
                    if rf and b.reject_formats and rf in b.reject_formats:
                        return self._send_json({"error": {"message": "not supported"}}, status=400)
                    return self._send_sse(b, chunks)

                return self._send_json({
                    "id": "chatcmpl-fake", "object": "chat.completion", "model": b.model,
                    "choices": [{"index": 0, "finish_reason": b.finish_reason,
                                 "message": {"role": "assistant", "content": b.body}}],
                    "usage": b.usage,
                })

            def _send_json(self, obj, status: int = 200):
                data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _send_sse(self, b: Behavior, chunks: list[str]):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                for i, c in enumerate(chunks):
                    if b.stall_after >= 0 and i == b.stall_after:
                        self.wfile.flush()
                        time.sleep(b.stall_for or 30.0)  # 生成中途卡住
                        return
                    frame = {"id": "chatcmpl-fake", "model": b.model,
                             "choices": [{"index": 0, "delta": {"content": c},
                                          "finish_reason": None}]}
                    self.wfile.write(f"data: {json.dumps(frame, ensure_ascii=False)}\n\n"
                                     .encode("utf-8"))
                    self.wfile.flush()
                last = {"id": "chatcmpl-fake", "model": b.model,
                        "choices": [{"index": 0, "delta": {},
                                     "finish_reason": b.finish_reason}],
                        "usage": b.usage}
                self.wfile.write(f"data: {json.dumps(last, ensure_ascii=False)}\n\n"
                                 .encode("utf-8"))
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._srv.daemon_threads = True
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._srv is not None:
            self._srv.shutdown()
            self._srv.server_close()
            self._srv = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()


def write_fake_config(config, base_url: str, *, stream: bool = False,
                      pricing: dict | None = None) -> None:
    """把假服务器的地址写进配置（测试夹具）。"""
    config.set("llm.mode", "cloud", force=True)
    config.set("llm.base_url", base_url, force=True)
    config.set("llm.model", "fake-model", force=True)
    config.set("llm.api_key_ref", "env:JEV_FAKE_KEY", force=True)
    config.set("llm.stream", stream, force=True)
    config.set("llm.max_retries", 0, force=True)
    config.set("llm.request_timeout_s", 3.0, force=True)
    config.set("llm.stream_read_timeout_s", 3.0, force=True)
    if pricing is not None:
        config.set("llm.pricing", pricing, force=True)


__all__ = ["FakeLLM", "Behavior", "write_fake_config"]
