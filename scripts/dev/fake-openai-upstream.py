"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：scripts/dev/fake-openai-upstream.py
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class FakeOpenAIHandler(BaseHTTPRequestHandler):
    """本地验收用 OpenAI-compatible fake upstream。

    这个脚本只用于开发和 PRD 验收，不是产品主链路。它返回稳定的 Chat、
    Streaming Chat 和 Embeddings 响应，方便验证 Gateway Provider Adapter。
    """

    def do_GET(self) -> None:  # noqa: N802
        """do_GET：说明该定义的测试或脚本职责。
        
        修改时需保持主流程、异常流程、权限边界和敏感信息脱敏的覆盖。
        """

        if self.path in {"/v1", "/v1/"}:
            self._json({"status": "ok", "object": "endpoint"})
            return
        # 路由和模型判断决定实际上游调用，必须保持可解释和可追踪。
        if self.path == "/v1/models":
            self._json({"object": "list", "data": [{"id": "default-chat"}, {"id": "default-embedding"}]})
            return
        if self.path == "/health":
            self._json({"status": "ok"})
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        """do_POST：说明该定义的测试或脚本职责。
        
        修改时需保持主流程、异常流程、权限边界和敏感信息脱敏的覆盖。
        """

        body = self._read_json_body()
        if self.path == "/v1/chat/completions":
            if body.get("stream") is True:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n')
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
                return
            self._json(
                {
                    "id": "chatcmpl_live_prd009",
                    "object": "chat.completion",
                    "model": body.get("model"),
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "live pong"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 4, "completion_tokens": 6, "total_tokens": 10},
                }
            )
            return
        if self.path == "/v1/embeddings":
            self._json(
                {
                    "object": "list",
                    "model": body.get("model"),
                    "data": [{"object": "embedding", "index": 0, "embedding": [0.01, 0.02]}],
                    "usage": {"prompt_tokens": 2, "total_tokens": 2},
                }
            )
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        """log_message：说明该定义的测试或脚本职责。
        
        修改时需保持主流程、异常流程、权限边界和敏感信息脱敏的覆盖。
        """

        return

    def _read_json_body(self) -> dict[str, Any]:
        """_read_json_body：说明该定义的测试或脚本职责。
        
        修改时需保持主流程、异常流程、权限边界和敏感信息脱敏的覆盖。
        """

        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8") if length else "{}"
        try:
            body = json.loads(raw or "{}")
            return body if isinstance(body, dict) else {}
        # 数据解析失败只返回安全摘要，原始异常不直接暴露给用户。
        except json.JSONDecodeError:
            return {}

    def _json(self, payload: dict[str, Any]) -> None:
        """_json：说明该定义的测试或脚本职责。
        
        修改时需保持主流程、异常流程、权限边界和敏感信息脱敏的覆盖。
        """

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode("utf-8"))


def main() -> None:
    """main：说明该定义的测试或脚本职责。
    
    修改时需保持主流程、异常流程、权限边界和敏感信息脱敏的覆盖。
    """

    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18080)
    args = parser.parse_args()
    ThreadingHTTPServer((args.host, args.port), FakeOpenAIHandler).serve_forever()


if __name__ == "__main__":
    main()
