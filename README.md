# duanyan2api

把 [书生·端砚](https://chat.intern-ai.org.cn)（Shanghai AI Lab「书生」系列的桌面客户端，内部代号 `inkstone-desktop`）的登录态反代成本地 **OpenAI 兼容 API**。启动后，任何支持 OpenAI 接口的工具（LobeChat / NextChat / OpenAI SDK / LangChain / Cherry Studio 等）都能直接调用端砚背后的 10 个模型。

> ⚠️ **仅供学习与协议研究使用**，请勿用于商业用途或大规模滥用；请遵守上海人工智能实验室的服务条款。使用本项目产生的一切后果由使用者自行承担。客户端重新登录后 token 自动更新，无需改代码。

## 特性

- **零配置登录态**：自动解密端砚客户端凭据文件（DPAPI + AES-256-GCM），打开客户端登录一次即可，无需抓包
- **OpenAI 兼容**：`/v1/models`、`/v1/chat/completions`，`stream=true/false` 均支持
- **实时流式**：上游 SSE 逐 chunk 转发，不缓冲整段回答
- **多模型**：Auto（自动路由到默认模型）、书生-S2、GLM-5.2、Kimi-K2.7-Code、Qwen3.8-Max 等
- **统一错误格式**：上游错误统一映射为 OpenAI error JSON，不会把 HTML 错误页漏给客户端

## 支持的模型

| 请求 model | 说明 |
|---|---|
| `Auto` | 自动路由（解析为默认模型 qwen3.7-plus） |
| `书生-Agents-A1` / `书生-S2` / `书生-S2-Preview-397B` / `书生-Atria-Dawn-Preview` | 书生系 |
| `Qwen3.7-Plus` / `Qwen3.8-Flash` / `Qwen3.8-Max` | Qwen 系 |
| `GLM-5.2` | 智谱 GLM |
| `Kimi-K2.7-Code` | Kimi 代码模型 |

模型列表动态来自上游 `/local-agent/models`，以实际返回为准（启动时会在控制台打印）。

## 快速开始

### 前置条件

1. Windows 10/11（自动读取凭据依赖 DPAPI）
2. 安装 [书生·端砚桌面客户端](https://chat.intern-ai.org.cn) 并**登录一次**（保持安装即可，不需要一直开着）
3. Python 3.10+

### 安装运行

```bash
pip install -r requirements.txt

# Windows 下自动解密凭据还需要 pywin32（requirements.txt 已含）
python server.py
```

看到如下输出即成功：

```
duanyan2api  http://127.0.0.1:9095/v1
Local API Key: sk-duanyan-local
Credential: auto(inkstone-desktop) uid=xxxx expires=2026-10-13 15:59
Models: Auto, 书生-Agents-A1, 书生-S2, ...
```

### 测试

```bash
curl http://127.0.0.1:9095/v1/chat/completions \
  -H "Authorization: Bearer sk-duanyan-local" \
  -H "Content-Type: application/json" \
  -d '{"model": "书生-S2", "messages": [{"role": "user", "content": "你好"}], "stream": false}'
```

### OpenAI SDK 接入

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:9095/v1",
    api_key="sk-duanyan-local",
)

resp = client.chat.completions.create(
    model="GLM-5.2",
    messages=[{"role": "user", "content": "用一句话介绍你自己"}],
    stream=True,
)
for chunk in resp:
    print(chunk.choices[0].delta.content or "", end="")
```

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DUANYAN_PORT` | `9095` | 本地监听端口（仅 127.0.0.1） |
| `DUANYAN_LOCAL_KEY` | `sk-duanyan-local` | 本地 API Key，**公网部署务必修改** |
| `INKSTONE_DATA_DIR` | `%APPDATA%/inkstone-desktop` | 客户端数据目录（便携版/自定义安装时指定） |
| `DUANYAN_CLIENT_VERSION` | `0.2.5` | 上游 metadata.client_version |
| `DUANYAN_LOCALE` | `zh` | 上游 metadata.locale（`zh` / `en`） |
| `DUANYAN_API_BASE` | `https://discovery.intern-ai.org.cn/api/orbit/v1` | 上游基座地址 |

### 手动凭据（Mac/Linux 或解密失败时）

在 `data/credentials.json` 填入 token 即可（token 抓包位置见下文「逆向过程」第 3 步）：

```json
{ "token": "eyJhbGciOi...", "sso_uid": "123456789" }
```

---

## 逆向过程梳理（如何从客户端到协议）

> 记录完整的分析方法，供学习交流。目标客户端：书生·端砚 Windows 桌面版 0.2.5（Electron）。

### 1. 定位客户端与数据目录

端砚桌面版是 Electron 应用（`package.json` 中 `name: "inkstone-desktop"`）。运行时数据在：

```
%APPDATA%\inkstone-desktop\
├── Local State          ← Chromium 系加密配置（os_crypt.encrypted_key）
├── credentials\
│   └── credential_<uuid>.bin   ← 登录态凭据（加密）
├── Local Storage / IndexedDB   ← 前端数据
└── logs\                        ← electron-log 日志
```

`credentials` 目录的存在说明登录态是**文件化**存储的，这是自动解密的突破口。

### 2. 解包 Electron 应用拿到主进程代码

安装目录下 `resources\app.asar` 用 asar 工具解开：

```bash
npx @electron/asar extract app.asar app_extracted
```

得到 `out/main/index-*.js`（主进程 bundle，约 3.5MB，压缩混淆）、`out/renderer/`（前端）、`out/preload/`。

### 3. 从 bundle 里 grep 出 API 基座与端点

对主进程 bundle 做字符串扫描：

```bash
grep -oE "https?://[a-z0-9.-]+intern-ai[a-z0-9./_-]*" out/main/index-*.js | sort -u
```

关键发现：

- `"MAIN_VITE_CORE_API_BASE_URL": "https://discovery.intern-ai.org.cn/api/orbit/v1"` — orbit API 基座
- 端点字符串：`/chat/new`、`/local-agent/models`、`/local-agent/messages`、`/chat/{id}/local-turns`（含 `/checkpoint`、`/complete`）

再搜认证头构造（类名 Core Cloud Gateway）：

```js
headers(extra) {
  return {
    Authorization: `Bearer ${this.options.authority.token}`,
    id: this.options.authority.ssoUid,
    "Accept-Language": this.options.authority.locale,
    ...extra
  };
}
url(path2) { return `${this.baseUrl}${path2}`; }
```

即认证是**双头**：`Authorization: Bearer <token>` + `id: <ssoUid>`。

### 4. 解密登录态凭据

`credential_*.bin` 以 `v10` 开头 —— 与 Chrome Cookie 加密同源（App-Bound 之前的经典 v10 方案）：

```
blob = "v10" + nonce(12B) + AES-256-GCM(ciphertext+tag)
key  = DPAPI_Unprotect(base64decode(Local State.os_crypt.encrypted_key)[5:])
```

解密后得到明文 JSON：

```json
{
  "version": 1,
  "credential": {
    "token": "eyJhbGciOi...",
    "environment": "production",
    "expiresAtMs": 1791878378000,
    "user": { "ssoUid": "123456789", "username": "..." }
  }
}
```

`token` + `ssoUid` 正好对应第 3 步的两个认证头。**token 有效期约 30 天，客户端重新登录自动续期**（服务每次请求现读凭据，无需重启）。

### 5. 推理端点与 SSE 事件协议

在 bundle 中定位 `openInferenceResponse`：

```js
const response = await abortable(this.options.fetch(this.url("/local-agent/messages"), {
  method: "POST",
  headers: this.headers({ Accept: "text/event-stream", "Content-Type": "application/json" }),
  ...
```

从事件消费代码反推出 SSE 事件结构：

```js
event["event"] === "message"   →  event["answer"]        // 图像分析路径的旧事件
// 实测主推理流 (data: JSON 行):
{"type":"message_start","response_id":"resp_...","model":"Qwen3.8-Flash"}
{"type":"content_delta","text":"好"}
{"type":"usage","usage":{"input_tokens":31,"output_tokens":1}}
{"type":"message_end","stop_reason":"end_turn"}
```

### 6. 服务端 schema 驱动的 payload 破解（最省力的一步）

推理请求体的字段无法全部从混淆代码里读出，但**服务端校验是逐字段报错的**（HTTP 422 + code 422003）。写一个自适应循环：每次按报错信息补一个字段，直到拿到 SSE 流：

```
round0: missing required field thinking      → thinking: {}
round1: thinking is missing required field enabled → {"enabled": false}
round2: tool_choice must be an object        → {"type": "auto"}
round3: metadata is missing required field locale  → "zh"（必须是 zh/en，不能 zh-CN）
round4: system must be an array              → []
round5: messages[0].content must be an array → [{"type":"text","text":...}]
...
```

再加上负面反馈（`unsupported field lifecycle_owner` → 删掉；`message role must be user or assistant` → system 消息不能混进 messages）与少量变体探测，最终得到完整 payload 结构：

```jsonc
{
  "protocol_version": "2.0",
  "inference_kind": "detached",          // 独立推理，无需 turns 三件套
  "inference_id": "<uuid>",
  "request_id": "<uuid>",
  "chat_id": 62414,                      // POST /chat/new 返回的会话 id
  "model": "Qwen3.8-Flash",              // detached 不接受 "Auto"，需解析成实体模型
  "messages": [
    {"role": "user", "content": [{"type": "text", "text": "你好"}]}
  ],
  "tools": [],
  "thinking": { "enabled": false },
  "system": ["你是助手"],                // 注意：字符串数组，不是 block！
  "tool_choice": { "type": "auto" },
  "metadata": { "locale": "zh", "client_version": "0.2.5" }
}
```

### 7. 协议规则速查（踩坑记录）

| 规则 | 说明 |
|---|---|
| system 是 `string[]` | 传 block 数组会报 `invalid JSON request`（400/422003） |
| messages 只允许 user/assistant | system 角色混入报 `message role must be user or assistant` |
| content 不能为空 | 空 assistant 消息报 `messages[N].content is empty`（代理层直接丢弃） |
| detached 需显式模型 | 传 `Auto` 报 `detached inference requires an explicit concrete model`，需解析为其 canonical_id（qwen3.7-plus） |
| `lifecycle_owner` 只在校验层存在 | 客户端在发送前 `delete providerRequest["lifecycle_owner"]`，透传会被拒 |
| locale 只能 `zh` / `en` | 传 `zh-CN` 会报错 |
| chat/new 复用 | detached 推理只需一个 chat_id 关联，可创建一次反复使用 |

### 8. 组件关系图

```
OpenAI 客户端
   │  POST /v1/chat/completions (OpenAI 格式)
   ▼
duanyan2api (本仓库)
   │  1. DPAPI+AES-GCM 解密 %APPDATA%/inkstone-desktop/credentials/*.bin
   │  2. OpenAI messages → 端砚 payload (system 分离/content blocks/模型解析)
   │  3. POST {orbit}/local-agent/messages  (SSE)
   ▼
discovery.intern-ai.org.cn  (orbit API)
   │  SSE: message_start / content_delta / usage / message_end
   ▼
duanyan2api 逐 chunk 转换回 OpenAI chunk 流 → 客户端
```

## API 说明

### `GET /health`

```json
{ "status": "ok", "auth_source": "auto", "token_valid": true, "model": "Auto" }
```

### `GET /v1/models`

OpenAI 格式模型列表（含 `canonical_id` 附加字段）。

### `POST /v1/chat/completions`

标准 OpenAI Chat Completions。支持 `stream`、`system` 角色、多轮 messages；`tools` 会转换格式透传（上游行为以实际为准）。`reasoning_effort` 非 `none` 时会打开上游 `thinking.enabled`。

错误格式：

```json
{
  "error": {
    "message": "端砚登录态失效, 请重新打开端砚客户端登录",
    "type": "upstream_auth_error",
    "code": "401"
  }
}
```

## 常见问题

- **`Credential: MISSING`**：没装客户端或没登录过。登录一次端砚；便携版用 `INKSTONE_DATA_DIR` 指向数据目录；或走手动凭据。
- **401 upstream_auth_error**：token 过期（约 30 天）。重新打开端砚客户端登录即可，无需重启本服务。
- **429**：上游限流，稍后重试。
- **`detached inference requires an explicit concrete model`**：请求了未知模型名，用 `/v1/models` 返回的 id。

## License

MIT
