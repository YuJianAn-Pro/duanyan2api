#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
duanyan2api — 书生·端砚(inkstone-desktop) OpenAI 兼容反代

将书生·端砚桌面客户端的登录态封装成本地 OpenAI API 服务。
自动从客户端凭据文件解密 token，无需手动抓包。

上游协议(逆向自 inkstone-desktop 0.2.5, 全部实测验证):
  POST {BASE}/chat/new                  {scenario:2,type:0,provider:0,agent:"local_agent"} -> data.id
  GET  {BASE}/local-agent/models        -> data.models[{id,canonical_id,...}], data.limits
  POST {BASE}/local-agent/messages      SSE: message_start/content_delta/usage/message_end
  认证: Authorization: Bearer <token> ; id: <ssoUid> ; Accept-Language: zh-CN
  登录态: %APPDATA%/inkstone-desktop/credentials/credential_*.bin (v10 = DPAPI + AES-256-GCM)

仅供学习研究使用，请勿用于商业用途。
"""

import base64
import glob
import json
import os
import sys
import threading
import time
import uuid
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

import requests

PORT = int(os.environ.get("DUANYAN_PORT", "9095"))
LOCAL_API_KEY = os.environ.get("DUANYAN_LOCAL_KEY", "sk-duanyan-local")
BASE = os.environ.get("DUANYAN_API_BASE", "https://discovery.intern-ai.org.cn/api/orbit/v1")
USER_AGENT = "DuanyanProxy/1.0"

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
CRED_FILE = os.path.join(DATA_DIR, "credentials.json")

# 端砚客户端数据目录 (Windows: %APPDATA%/inkstone-desktop)
INKSTONE_DIR = os.environ.get(
    "INKSTONE_DATA_DIR",
    os.path.join(os.environ.get("APPDATA", ""), "inkstone-desktop"),
)

# 推理请求 metadata 字段(服务端校验 client_version 与 locale)
_client_meta = {
    "locale": os.environ.get("DUANYAN_LOCALE", "zh"),
    "client_version": os.environ.get("DUANYAN_CLIENT_VERSION", "0.2.5"),
}

_chat_lock = threading.Lock()
_chat_id = None
_models_cache = {"ts": 0.0, "models": []}


class UpstreamError(Exception):
    """上游非 200/SSE 响应。envelope 为 {code,msg,...}"""

    def __init__(self, status, envelope):
        self.status = status
        self.envelope = envelope
        msg = envelope.get("msg") if isinstance(envelope, dict) else str(envelope)
        super().__init__(msg or f"upstream HTTP {status}")


# ── 登录态读取 ─────────────────────────────────────────
def _decrypt_v10(blob: bytes) -> str:
    """解密 Chromium v10 加密块: DPAPI 保护的全局密钥 + AES-256-GCM"""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    import win32crypt

    with open(os.path.join(INKSTONE_DIR, "Local State"), "r", encoding="utf-8") as f:
        local_state = json.load(f)
    enc_key = base64.b64decode(local_state["os_crypt"]["encrypted_key"])
    key = win32crypt.CryptUnprotectData(enc_key[5:], None, None, None, 0)[1]
    # blob = b"v10" + nonce(12) + ciphertext+tag
    return AESGCM(key).decrypt(blob[3:15], blob[15:], None).decode("utf-8")


def auto_read_credential():
    """从端砚客户端凭据文件解密 token/ssoUid (仅 Windows)。"""
    cred_dir = os.path.join(INKSTONE_DIR, "credentials")
    if not os.path.isdir(cred_dir):
        return None
    candidates = sorted(
        glob.glob(os.path.join(cred_dir, "credential_*.bin")),
        key=os.path.getmtime, reverse=True,
    )
    for fpath in candidates:
        try:
            with open(fpath, "rb") as f:
                data = f.read()
            if data[:3] != b"v10":
                continue
            cred = json.loads(_decrypt_v10(data))
            c = cred.get("credential", {})
            token = c.get("token", "")
            expires = c.get("expiresAtMs", 0)
            if token and (not expires or time.time() * 1000 < expires):
                return {
                    "token": token,
                    "sso_uid": c.get("user", {}).get("ssoUid", ""),
                    "expires_at": expires / 1000 if expires else 0,
                }
        except Exception:
            continue
    return None


def _manual_credential():
    """手动凭据: data/credentials.json {"token": "...", "sso_uid": "..."}"""
    try:
        with open(CRED_FILE, "r", encoding="utf-8") as f:
            creds = json.load(f)
        if creds.get("token"):
            return {"token": creds["token"], "sso_uid": creds.get("sso_uid", ""), "expires_at": 0}
    except Exception:
        pass
    return None


def get_credential():
    return auto_read_credential() or _manual_credential()


def auth_headers(accept="application/json"):
    cred = get_credential()
    if cred is None:
        return None
    return {
        "Authorization": f"Bearer {cred['token']}",
        "id": cred.get("sso_uid", ""),
        "Accept-Language": "zh-CN",
        "Accept": accept,
        "User-Agent": USER_AGENT,
    }


# ── 上游调用 ───────────────────────────────────────────
def upstream_json(method, path, body=None, timeout=60):
    headers = auth_headers()
    if headers is None:
        raise UpstreamError(401, {"code": -1, "msg": "no duanyan credential"})
    resp = requests.request(method, BASE + path, headers=headers, json=body, timeout=timeout)
    try:
        return resp.status_code, resp.json()
    except ValueError:
        return resp.status_code, {"code": -1, "msg": f"non-json upstream response: {resp.text[:200]}"}


def get_chat_id():
    """detached 推理需要 chat_id 关联；惰性创建一个本地会话并复用。"""
    global _chat_id
    with _chat_lock:
        if _chat_id:
            return _chat_id
        code, j = upstream_json(
            "POST", "/chat/new",
            {"scenario": 2, "type": 0, "provider": 0, "agent": "local_agent"},
        )
        if code == 200 and j.get("code") == 0 and isinstance(j.get("data"), dict):
            _chat_id = j["data"]["id"]
            return _chat_id
        raise UpstreamError(code, j)


def reset_chat_id():
    global _chat_id
    with _chat_lock:
        _chat_id = None


def stream_inference(payload):
    headers = auth_headers("text/event-stream")
    if headers is None:
        raise UpstreamError(401, {"code": -1, "msg": "no duanyan credential"})
    headers["Content-Type"] = "application/json"
    resp = requests.post(
        BASE + "/local-agent/messages", headers=headers,
        json=payload, stream=True, timeout=(60, 600),
    )
    if resp.status_code != 200 or not resp.headers.get("content-type", "").startswith("text/event-stream"):
        try:
            j = resp.json()
        except ValueError:
            j = {"code": -1, "msg": f"upstream HTTP {resp.status_code}"}
        raise UpstreamError(resp.status_code, j)
    return resp


# ── 协议转换 OpenAI -> 端砚 ────────────────────────────
def _text_of(content):
    """OpenAI content (str | parts) -> 纯文本"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    if isinstance(content, list):
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and part.get("type", "text") == "text":
                parts.append(part.get("text", ""))
    return "\n".join(p for p in parts if p)


def convert_messages(openai_messages):
    """OpenAI messages -> (system_strs, upstream_messages)

    端砚协议(实测):
      - messages 数组只允许 user/assistant 角色 (system 放顶层 system 字符串数组)
      - 每条消息 content 是 block 数组 [{"type":"text","text":...}], 且不能为空
      - assistant 空 content 会被 422 拒绝, 此处直接丢弃该消息
    """
    system_strs = []
    upstream = []
    for m in openai_messages or []:
        role = m.get("role", "user")
        text = _text_of(m.get("content"))
        if role in ("system", "developer"):
            if text:
                system_strs.append(text)
            continue
        if role == "assistant" and not text and not m.get("tool_calls"):
            continue
        upstream.append({"role": role, "content": [{"type": "text", "text": text}] if text else []})
    return system_strs, upstream


def convert_tools(openai_tools):
    """OpenAI tools(function) -> 端砚 tools 格式"""
    tools = []
    for t in openai_tools or []:
        fn = t.get("function", {}) if isinstance(t, dict) else {}
        if fn.get("name"):
            tools.append({
                "name": fn.get("name"),
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
            })
    return tools


def fetch_models(force=False):
    """拉取并缓存模型能力列表 (TTL 300s)"""
    if not force and _models_cache["models"] and time.time() - _models_cache["ts"] < 300:
        return _models_cache["models"]
    code, j = upstream_json("GET", "/local-agent/models")
    if code != 200 or j.get("code") != 0:
        raise UpstreamError(code, j)
    data = j.get("data") or {}
    _models_cache["models"] = data.get("models", [])
    _models_cache["ts"] = time.time()
    return _models_cache["models"]


def resolve_model(model):
    """detached 推理要求显式模型: Auto 解析为其 canonical_id 对应的实体模型。"""
    model = (model or "Auto").strip()
    models = fetch_models()
    for m in models:
        if m.get("id") == model:
            return m.get("canonical_id") or model if model.lower() == "auto" else model
    for m in models:
        if m.get("canonical_id") == model:
            return model
    return model


def build_payload(model, messages, tools, tool_choice, thinking_enabled):
    system_strs, upstream_msgs = convert_messages(messages)
    payload = {
        "protocol_version": "2.0",
        "inference_kind": "detached",
        "inference_id": str(uuid.uuid4()),
        "request_id": str(uuid.uuid4()),
        "chat_id": get_chat_id(),
        "model": resolve_model(model),
        "messages": upstream_msgs,
        "tools": convert_tools(tools),
        "thinking": {"enabled": bool(thinking_enabled)},
        "system": system_strs,
        "tool_choice": {"type": "auto"},
        "metadata": dict(_client_meta),
    }
    if tool_choice and isinstance(tool_choice, dict) and tool_choice.get("type") == "function":
        payload["tool_choice"] = {"type": "function", "name": tool_choice.get("function", {}).get("name", "")}
    return payload


# ── SSE -> OpenAI 转换 ────────────────────────────────
STOP_REASON_MAP = {
    "end_turn": "stop", "stop_sequence": "stop",
    "max_tokens": "length", "tool_use": "tool_calls",
}


def error_body(status, message, err_type):
    return {"error": {"message": message, "type": err_type, "code": str(status)}}


def map_upstream_error(status, envelope):
    """上游错误 -> OpenAI 统一错误格式(不透传上游原始 HTML/明文)"""
    msg = envelope.get("msg", "") if isinstance(envelope, dict) else str(envelope)
    s = str(msg).lower()
    if status == 401 or "auth" in s or "token" in s or "credential" in s:
        return 401, error_body(401, "端砚登录态失效, 请重新打开端砚客户端登录", "upstream_auth_error")
    if status == 429:
        return 429, error_body(429, "端砚上游限流, 请稍后重试", "upstream_rate_limit_error")
    if status in (408, 504):
        return 504, error_body(504, "端砚上游超时", "upstream_timeout_error")
    if status >= 500:
        return 502, error_body(502, f"端砚上游服务错误: {msg}", "upstream_error")
    return 400, error_body(status, f"端砚上游拒绝请求: {msg}", "upstream_invalid_request")


def openai_chunk(chunk_id, model, delta=None, finish=None, usage=None):
    chunk = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}],
    }
    if usage is not None:
        chunk["usage"] = usage
    return chunk


def openai_first_chunk(chunk_id, model):
    return openai_chunk(chunk_id, model, delta={"role": "assistant", "content": ""})


def _sse_data_lines(resp):
    """迭代上游 SSE 的 data 行 (bytes)"""
    buf = b""
    for chunk in resp.iter_content(chunk_size=None):
        if not chunk:
            continue
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            line = line.strip()
            if line.startswith(b"data:"):
                yield line[5:].strip()
    tail = buf.strip()
    if tail.startswith(b"data:"):
        yield tail[5:].strip()


def _parse_usage(u):
    return {
        "prompt_tokens": u.get("input_tokens", 0),
        "completion_tokens": u.get("output_tokens", 0),
        "total_tokens": u.get("input_tokens", 0) + u.get("output_tokens", 0),
    }


def _collect_sse(resp):
    """聚合完整回答(非流式): 返回 (text, usage, stop_reason)"""
    text_parts, usage, stop = [], None, "stop"
    for data in _sse_data_lines(resp):
        if not data or data == b"[DONE]":
            break
        try:
            ev = json.loads(data)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        etype = ev.get("type")
        if etype == "content_delta":
            text_parts.append(ev.get("text", ""))
        elif etype == "usage":
            usage = _parse_usage(ev.get("usage", {}))
        elif etype == "message_end":
            stop = STOP_REASON_MAP.get(ev.get("stop_reason", "end_turn"), "stop")
        elif etype == "error":
            raise UpstreamError(500, {"code": -1, "msg": ev.get("message") or json.dumps(ev, ensure_ascii=False)})
    return "".join(text_parts), usage, stop


# ── 业务处理 ──────────────────────────────────────────
def handle_models():
    out = []
    for m in fetch_models():
        out.append({
            "id": m.get("id", ""),
            "object": "model",
            "created": 1700000000,
            "owned_by": "internai",
            "canonical_id": m.get("canonical_id", ""),
            "display_name": m.get("display_name") or m.get("name", ""),
        })
    return {"object": "list", "data": out}


def handle_chat(req):
    model = req.get("model", "Auto")
    stream = bool(req.get("stream", False))
    thinking = bool(req.get("reasoning_effort") and req.get("reasoning_effort") != "none")
    chunk_id = "chatcmpl-" + uuid.uuid4().hex[:24]

    def _build():
        return build_payload(model, req.get("messages"), req.get("tools"),
                             req.get("tool_choice"), thinking)

    resp = stream_inference(_build())

    if not stream:
        text, usage, stop = _collect_sse(resp)
        return {
            "id": chunk_id,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": stop,
            }],
            "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

    return ("sse", chunk_id, model, resp)


# ── HTTP 服务器 ──────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "DuanyanProxy/1.0"

    def log_message(self, fmt, *args):
        sys.stdout.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), fmt % args))
        sys.stdout.flush()

    def _send_json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_sse_start(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True

    def _send_sse(self, obj):
        self.wfile.write(b"data: " + json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n\n")
        self.wfile.flush()

    def _check_key(self):
        auth = self.headers.get("Authorization", "") or self.headers.get("api-key", "")
        key = auth[7:] if auth.startswith("Bearer ") else auth
        return key == LOCAL_API_KEY

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, api-key")
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()

    def do_GET(self):
        p = urlparse(self.path).path
        if p == "/health":
            cred = get_credential()
            self._send_json(200, {
                "status": "ok",
                "auth_source": "auto" if auto_read_credential() else ("manual" if cred else "none"),
                "token_valid": bool(cred),
                "model": "Auto",
            })
            return
        if p == "/v1/models":
            if not self._check_key():
                self._send_json(401, error_body(401, "invalid api key", "authentication_error"))
                return
            try:
                self._send_json(200, handle_models())
            except UpstreamError as e:
                code, body = map_upstream_error(e.status, e.envelope)
                self._send_json(code, body)
            return
        self._send_json(404, error_body(404, "not found", "invalid_request_error"))

    def do_POST(self):
        p = urlparse(self.path).path
        if p != "/v1/chat/completions":
            self._send_json(404, error_body(404, "not found", "invalid_request_error"))
            return
        if not self._check_key():
            self._send_json(401, error_body(401, "invalid api key", "authentication_error"))
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json(400, error_body(400, "invalid json body", "invalid_request_error"))
            return

        try:
            result = handle_chat(req)
        except UpstreamError as e:
            code, body = map_upstream_error(e.status, e.envelope)
            self._send_json(code, body)
            return
        except Exception as e:
            self._send_json(502, error_body(502, f"proxy internal error: {e}", "upstream_error"))
            return

        if not isinstance(result, tuple):
            self._send_json(200, result)
            return

        _, chunk_id, model, resp = result
        self._send_sse_start()
        try:
            self._send_sse(openai_first_chunk(chunk_id, model))
            usage, stop = None, "stop"
            for data in _sse_data_lines(resp):
                if not data or data == b"[DONE]":
                    break
                try:
                    ev = json.loads(data)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                etype = ev.get("type")
                if etype == "content_delta":
                    self._send_sse(openai_chunk(chunk_id, model, delta={"content": ev.get("text", "")}))
                elif etype == "thinking_delta":
                    self._send_sse(openai_chunk(chunk_id, model, delta={"reasoning_content": ev.get("text") or ev.get("thinking", "")}))
                elif etype == "usage":
                    usage = _parse_usage(ev.get("usage", {}))
                elif etype == "message_end":
                    stop = STOP_REASON_MAP.get(ev.get("stop_reason", "end_turn"), "stop")
                elif etype == "error":
                    break
            self._send_sse(openai_chunk(chunk_id, model, delta={}, finish=stop, usage=usage))
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            resp.close()


# ── main ─────────────────────────────────────────────
def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    cred = get_credential()
    print(f"duanyan2api  http://127.0.0.1:{PORT}/v1")
    print(f"Local API Key: {LOCAL_API_KEY}")
    if cred:
        src = "auto(inkstone-desktop)" if auto_read_credential() else "manual"
        exp = time.strftime("%Y-%m-%d %H:%M", time.localtime(cred["expires_at"])) if cred.get("expires_at") else "unknown"
        print(f"Credential: {src} uid={cred.get('sso_uid')} expires={exp}")
    else:
        print("Credential: MISSING - 请先登录端砚客户端, 或在 data/credentials.json 手动填写 token")
    try:
        print("Models: " + ", ".join(m["id"] for m in handle_models()["data"]))
    except Exception as e:
        print(f"Models: 加载失败 ({e})")
    server.serve_forever()


if __name__ == "__main__":
    main()
