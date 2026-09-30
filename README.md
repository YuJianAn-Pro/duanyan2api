# duanyan2api

Reverse-proxy the login session of **Shusheng Duanyan** (书生·端砚, the desktop chat client of Shanghai AI Lab's InternLM, codename `inkstone-desktop`) into a local **OpenAI-compatible API**. Any OpenAI SDK / ChatGPT-style frontend can then use the 10 models behind it.

> ⚠️ For learning and protocol research only. Do not abuse or use commercially. Your token stays local; nothing is uploaded.

## Features

- Zero-config auth: automatically decrypts the client's credential file (DPAPI + AES-256-GCM), no packet sniffing needed. Re-login in the client refreshes the token automatically.
- OpenAI-compatible: `GET /v1/models`, `POST /v1/chat/completions`, stream & non-stream.
- Real-time SSE passthrough (no whole-answer buffering).
- Unified OpenAI-style error format, upstream HTML never leaks to clients.

## Models

`Auto`, `书生-Agents-A1`, `书生-S2`, `书生-S2-Preview-397B`, `书生-Atria-Dawn-Preview`, `Qwen3.7-Plus`, `Qwen3.8-Flash`, `Qwen3.8-Max`, `GLM-5.2`, `Kimi-K2.7-Code`

The list is fetched live from upstream `/local-agent/models` and printed at startup.

## Quick start

Requirements: Windows 10/11 (credential auto-decrypt uses DPAPI), the Duanyan desktop client installed and logged in at least once, Python 3.10+.

```bash
pip install -r requirements.txt
python server.py        # or start.bat
```

```
duanyan2api  http://127.0.0.1:9095/v1
Local API Key: sk-duanyan-local
Credential: auto(inkstone-desktop) uid=123456789 expires=2026-10-13
```

Test:

```bash
curl http://127.0.0.1:9095/v1/chat/completions \
  -H "Authorization: Bearer sk-duanyan-local" \
  -H "Content-Type: application/json" \
  -d '{"model": "Auto", "messages": [{"role": "user", "content": "hello"}]}'
```

OpenAI SDK:

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:9095/v1", api_key="sk-duanyan-local")
resp = client.chat.completions.create(
    model="GLM-5.2",
    messages=[{"role": "user", "content": "hi"}],
    stream=True,
)
for chunk in resp:
    print(chunk.choices[0].delta.content or "", end="")
```

## Configuration (env vars)

| Variable | Default | Description |
|---|---|---|
| `DUANYAN_PORT` | `9095` | Local port (127.0.0.1 only) |
| `DUANYAN_LOCAL_KEY` | `sk-duanyan-local` | Local API key, change it if exposed |
| `INKSTONE_DATA_DIR` | `%APPDATA%\inkstone-desktop` | Client data dir (portable installs) |
| `DUANYAN_CLIENT_VERSION` | `0.2.5` | Upstream metadata field |
| `DUANYAN_LOCALE` | `zh` | `zh` or `en` |
| `DUANYAN_API_BASE` | `https://discovery.intern-ai.org.cn/api/orbit/v1` | Upstream base URL |

Manual credential (macOS/Linux or if auto-decrypt fails): put `{"token": "...", "sso_uid": "..."}` into `data/credentials.json`. The token can be captured from the client's request headers (`Authorization: Bearer <token>` + `id: <ssoUid>`).

## How it works (brief)

1. The client stores its login in `%APPDATA%\inkstone-desktop\credentials\credential_*.bin` — Chromium "v10" format: a DPAPI-protected AES key from `Local State`, then AES-256-GCM on the blob. Decrypting yields `token`, `ssoUid` and expiry.
2. The Electron main bundle reveals the upstream API base (`discovery.intern-ai.org.cn/api/orbit/v1`), endpoints (`/chat/new`, `/local-agent/models`, `/local-agent/messages`) and the auth header builder (`Authorization: Bearer` + `id`).
3. Chat requests are converted OpenAI → Duanyan payload (system prompt goes into a top-level `string[]`, messages accept only user/assistant with non-empty block content, `Auto` resolves to its canonical concrete model) and sent to `/local-agent/messages`, which replies with SSE events `message_start / content_delta / usage / message_end` that are mapped back to OpenAI chunks.

## Troubleshooting

- `Credential: MISSING` — install & log in to the client once, or set `INKSTONE_DATA_DIR`, or use the manual credential file.
- `401 upstream_auth_error` — token expired (~30 days); re-login in the client, no restart needed.
- `429` — upstream rate limit, retry later.

## License

MIT
