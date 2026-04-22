# LINE API — 介接文件

line-api 是 LINE 橋接服務的 REST 層，部署在 Zeabur。  
Base URL：`https://line-api.needsai.loan`

---

## 認證

所有 `/v1/*` 端點需要 `X-API-Key` header：

```
X-API-Key: pk_live_xxxxxxxxxxxxxxxx
```

Admin key 可加 `X-User-Id` 委派給其他用戶：

```
X-API-Key: pk_live_xxxxxxxxxxxxxxxx
X-User-Id: 69e7d916fe76b38a30c72524
```

**Scope 說明**

| Scope | 可用端點 |
|-------|---------|
| `read` | GET 系列、contacts/refresh、groups/refresh |
| `send` | /send、/send-image、/broadcast、/schedule |
| `admin` | 全部，含 QR、logout、委派 X-User-Id |

---

## 架構說明

```
呼叫端 (line-codex / 其他)
    │  X-API-Key
    ▼
line-api (Zeabur)
    │  Bearer token
    ▼
bridge-N.needsai.loan  ← Cloudflare Tunnel
    │
    ▼
Mac mini (192.168.1.110)
    └── Docker: bridge-1..10 (每個 user 一個 container)
            └── Playwright + LINE Chrome Extension
```

Bridge 回傳格式注意：`/contacts`、`/groups` 直接回 JSON array，不是 `{"contacts": [...]}`。  
`extract_list()` 在 `bridge.py` 已統一處理，新端點請用它。

---

## 端點

### GET /v1/status

LINE 連線狀態。

```bash
curl https://line-api.needsai.loan/v1/status \
  -H "X-API-Key: pk_live_..."
```

**Response**
```json
{
  "ok": true,
  "line_logged_in": true,
  "line_status": "logged_in",
  "line_status_at": "2026-04-22T10:00:00",
  "line_mid": "U1234...",
  "line_name": "王小明",
  "provisioned": true
}
```

---

### GET /v1/qr-status

QR 登入狀態（掃碼流程用）。

**Response**
```json
{
  "ok": true,
  "state": "waiting",
  "logged_in": false,
  "pin": null
}
```
`state` 可能值：`waiting` / `pin_required` / `logged_in`

---

### GET /v1/qr *(admin)*

取得 QR code 圖片（PNG）。若尚未 provision bridge 會自動建立。

```bash
curl https://line-api.needsai.loan/v1/qr \
  -H "X-API-Key: pk_live_..." \
  -o qr.png
```

加 `?refresh=1` 強制重整 QR。

---

### POST /v1/login-password *(admin)*

密碼登入。

**Request**
```json
{ "email": "you@example.com", "password": "..." }
```

---

### POST /v1/login-password-verify *(admin)*

輸入簡訊驗證碼。限 5 次 / 10 分鐘。

**Request**
```json
{ "code": "123456" }
```

---

### POST /v1/users/logout *(admin)*

登出 LINE。

---

### GET /v1/contacts

從快取讀取聯絡人（不呼叫 bridge）。

**Response**
```json
{
  "ok": true,
  "count": 799,
  "contacts": [
    { "mid": "Uxxxx", "name": "王小明" }
  ]
}
```

---

### POST /v1/contacts/refresh

從 bridge 拉最新聯絡人並更新快取。  
> 需要 bridge 在線（Mac mini + Docker 運行中）

**Response**
```json
{ "ok": true, "contacts_count": 799 }
```

---

### GET /v1/groups

從快取讀取群組。

**Response**
```json
{
  "ok": true,
  "count": 12,
  "groups": [
    { "mid": "Cxxxx", "name": "家族群", "type": "group" }
  ]
}
```

---

### POST /v1/groups/refresh

從 bridge 拉最新群組並更新快取。

**Response**
```json
{ "ok": true, "groups_count": 12 }
```

---

### GET /v1/messages

讀取訊息記錄（從 MongoDB，非即時）。

**Query params**

| 參數 | 說明 | 預設 |
|------|------|------|
| `peer` | 指定對象 MID | 全部 |
| `limit` | 筆數上限 | 50（最多 200）|
| `since` | Unix ms timestamp，只回更新的 | — |

**Response**
```json
{
  "ok": true,
  "count": 50,
  "messages": [
    {
      "id": "msg_id",
      "from": "Uxxx",
      "to": "Uyyy",
      "peer": "Uxxx",
      "text": "你好",
      "content_type": "text",
      "created_time": 1714000000000
    }
  ]
}
```

---

### GET /v1/chats

聊天列表（每個 peer 最新一則，含名稱解析）。

**Query params**：`limit`（預設 100，最多 500）

**Response**
```json
{
  "ok": true,
  "chats": [
    {
      "peer": "Uxxx",
      "peer_name": "王小明",
      "last_text": "好的",
      "last_time": 1714000000000,
      "last_content_type": "text",
      "count": 42
    }
  ]
}
```

---

### POST /v1/send *(send scope)*

傳送文字訊息。

**Request**
```json
{ "to": "Uxxx", "text": "你好" }
```

**Response**
```json
{ "ok": true }
```

---

### POST /v1/send-image *(send scope)*

傳送圖片。

**Request**
```json
{
  "to": "Uxxx",
  "image_base64": "iVBORw0KGgo...",
  "mime_type": "image/jpeg"
}
```
`image_base64` 可含或不含 `data:image/jpeg;base64,` 前綴。

---

### POST /v1/broadcast *(send scope)*

批次傳送給多人（最多 50 個 peer）。

**Request**
```json
{
  "to": ["Uxxx", "Uyyy"],
  "text": "公告訊息"
}
```

**Response**
```json
{
  "ok": true,
  "total": 2,
  "success": 2,
  "results": [
    { "to": "Uxxx", "ok": true },
    { "to": "Uyyy", "ok": true }
  ]
}
```

---

### POST /v1/schedule *(send scope)*

排程傳送（台灣時間）。

**Request**
```json
{
  "to": ["Uxxx"],
  "text": "早安",
  "send_at": "2026-05-01T09:00:00"
}
```

`send_at` 接受台灣本地時間（自動 -8h 轉 UTC）或含 timezone 的 ISO 格式。

**Response**
```json
{
  "ok": true,
  "schedule_id": "664a...",
  "send_at": "2026-05-01T01:00:00"
}
```

---

## 錯誤代碼

| HTTP | error | 說明 |
|------|-------|------|
| 401 | `missing or invalid api key` | API key 錯誤或未帶 |
| 401 | `api key user not found` | key 對應的 user 不存在 |
| 403 | `insufficient_scope` | scope 不足 |
| 429 | — | Rate limit |
| 502 | `bridge_error` | Bridge 回應非 2xx |
| 503 | `bridge_unreachable` | 無法連到 bridge（Mac mini / Docker 可能離線）|

---

## 開發備注

- **Bridge 格式**：`/contacts`、`/groups` 回傳裸 array，用 `extract_list(response, key)` 解析
- **用戶快取**：聯絡人 / 群組存在 `realty_line.line_cache`，`user_id` 為 MongoDB ObjectId 字串
- **Bridge token**：存在 `realty_line.users[user_id].bridge_token`，Cloudflare Tunnel 轉發到 Mac mini
