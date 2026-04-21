# realty-line → line-api 移轉計畫

## 目標

`line-api` 成為主體服務，直接與 bridge 溝通。
`realty-line` 變成第一個 X app，所有 LINE 操作改呼叫 `line-api` 的 `/v1/*`。

---

## 現況架構

```
realty-line ──→ bridge (直接)
```

## 目標架構

```
realty-line ──→ line-api ──→ bridge
                    ↑
              任何 X app
```

---

## Phase 1：line-api 補齊功能（目前已完成）

13 個 `/v1/*` endpoints 已在 line-api 中，確認與 realty-line 版本一致：

| Endpoint | 方法 | 狀態 |
|----------|------|------|
| `/v1/status` | GET | ✅ |
| `/v1/qr` | GET | ✅ |
| `/v1/qr-status` | GET | ✅ |
| `/v1/users/logout` | POST | ✅ |
| `/v1/login-password` | POST | ✅ |
| `/v1/login-password-verify` | POST | ✅ |
| `/v1/contacts` | GET | ✅ |
| `/v1/groups` | GET | ✅ |
| `/v1/send` | POST | ✅ |
| `/v1/send-image` | POST | ✅ |
| `/v1/broadcast` | POST | ✅ |
| `/v1/schedule` | POST | ✅ |
| `/v1/messages` | GET | ✅ |

---

## Phase 2：realty-line 改呼叫 line-api

### 2-A 新增 helper（取代 _bridge_get / _bridge_post）

在 `app.py` 加入：

```python
LINE_API_URL   = os.environ.get("LINE_API_URL", "http://localhost:8000")
LINE_API_TOKEN = os.environ.get("LINE_API_TOKEN", "")  # pk_... API key

def _lineapi_get(path, timeout=8, user_id=None):
    headers = {"X-API-Key": LINE_API_TOKEN}
    return requests.get(f"{LINE_API_URL}{path}", headers=headers, timeout=timeout)

def _lineapi_post(path, payload, timeout=30, user_id=None):
    headers = {"X-API-Key": LINE_API_TOKEN}
    return requests.post(f"{LINE_API_URL}{path}", json=payload, headers=headers, timeout=timeout)
```

> `user_id` 參數暫時保留簽名相容性，line-api 已透過 API key 識別使用者，不需要再傳。

---

### 2-B 所有 `_bridge_post` / `_bridge_get` 替換清單

共 39 個呼叫點，依 bridge 路徑對應：

| Bridge 路徑 | → line-api endpoint | 呼叫點行號 |
|------------|---------------------|-----------|
| `/status` | `GET /v1/qr-status` | 187, 1138, 2738, 2761, 4557 |
| `/qr-canvas` | `GET /v1/qr` | 1113, 2789 |
| `/refresh-qr` | `GET /v1/qr?refresh=1` | 1109, 2751 |
| `/login-password` | `POST /v1/login-password` | 1182, 1250 |
| `/login-password-verify` | `POST /v1/login-password-verify` | 1230, 1285 |
| `/logout` | `POST /v1/users/logout` | 326, 1156, 2815 |
| `/me` | `GET /v1/status` | 687, 1258, 1689, 2833 |
| `/send` | `POST /v1/send` | 773, 1326, 1382, 1951, 1978, 2003, 2061, 4577 |
| `/send-image` | `POST /v1/send-image` | 2042, 2096, 1348, 4582 |
| `/contacts` | `GET /v1/contacts` | 1664, 1920, 2665, 2845 |
| `/groups` | `GET /v1/groups` | 1676, 2854 |

---

### 2-C `_resolve_bridge` / `_provision_user_bridge` 處理

這兩個函數的邏輯要**移到 line-api**，realty-line 不再需要。

realty-line 中的呼叫點替換方式：
- `_resolve_bridge()` → 刪除，line-api 自己管理
- `_provision_user_bridge()` → 改呼叫 `POST /v1/qr`（line-api 內部自動 provision）

直接呼叫點：行 1096, 1179, 1243, 1621, 2735, 2749

---

### 2-D 刪除 realty-line 內的 /v1/* 路由

以下 13 個路由從 `app.py` 移除（行號供參考，實際移轉後行號會變）：

- 行 1064–1082：`/v1/status`
- 行 1084–1125：`/v1/qr`
- 行 1128–1145：`/v1/qr-status`
- 行 1148–1159：`/v1/users/logout`
- 行 1162–1188：`/v1/login-password`
- 行 1209–1234：`/v1/login-password-verify`
- 行 1292–1300：`/v1/contacts`
- 行 1303–1310：`/v1/groups`
- 行 1313–1332：`/v1/send`
- 行 1335–1358：`/v1/send-image`
- 行 1361–1401：`/v1/broadcast`
- 行 1404–1433：`/v1/schedule`
- 行 1436–1458：`/v1/messages`

同步刪除相關 helper：
- `api_key_required` decorator（行 866–932）
- `_build_api_key_wrapper()`
- `_hash_key()`
- `_mask_key()`
- `col_api_keys` collection 定義與索引

⚠️ **保留以下兩個：**
- `_verify_attempt_check()`（行 1191）— 使用 `col_api_usage` 做登入嘗試 rate limiting，與 API key 無關
- `col_api_usage` collection — 被 `_verify_attempt_check` 依賴，不能刪除

---

### 2-E /api/* 路由改呼叫 line-api

| 路由 | 行號 | 改呼叫 |
|------|------|-------|
| `/api/line/login-password` | 1238 | `POST /v1/login-password` |
| `/api/line/login-password-verify` | 1276 | `POST /v1/login-password-verify` |
| `/api/send` | 1930 | `POST /v1/send` |
| `/api/send-group` | 1963 | `POST /v1/send` |
| `/api/send-square` | 1988 | `POST /v1/send` |
| `/api/send-direct` | 2013 | `POST /v1/send` + `POST /v1/send-image` |
| `/api/send-image` | 2080 | `POST /v1/send-image` |
| `/api/line/bridge-logout` | 2810 | `POST /v1/users/logout` |
| `/api/line/refresh` | 2823 | `GET /v1/status` + `GET /v1/contacts` + `GET /v1/groups` |
| `/api/qr-login` | 2746 | `GET /v1/qr?refresh=1` |
| `/api/qr-status` | 2757 | `GET /v1/qr-status` |
| `/api/qr-image` | 2785 | `GET /v1/qr` |

---

## Phase 3：環境變數調整

### realty-line 變更

| 變數 | 現在 | 移轉後 |
|------|------|-------|
| `LINE_BRIDGE_URL` | bridge URL | 移除 |
| `LINE_BRIDGE_TOKEN` | bridge token | 移除 |
| `ORCHESTRATOR_URL` | orchestrator URL | 移除（移到 line-api） |
| `ORCHESTRATOR_TOKEN` | orchestrator token | 移除（移到 line-api） |
| `LINE_API_URL` | （新增）| line-api 的服務 URL |
| `LINE_API_TOKEN` | （新增）| realty-line 自己的 `pk_...` API key |

### line-api 保留

- `LINE_BRIDGE_URL`
- `LINE_BRIDGE_TOKEN`
- `ORCHESTRATOR_URL`
- `ORCHESTRATOR_TOKEN`
- `MONGO_URI`
- `SECRET_KEY`

---

## Phase 3.5：Webhook Endpoints（保留在 realty-line，不動）

以下兩個 webhook 是 bridge 主動推送給 realty-line 的接收點，**不能刪除也不能移走**：

| 路由 | 行號 | 用途 |
|------|------|------|
| `POST /api/_hook/state` | 654 | 接收 bridge 登入/登出狀態變化 |
| `POST /api/_hook/messages` | 704 | 接收 bridge 批次推送訊息 |

⚠️ 行 687 的 `_bridge_get("/me")` 在 state change webhook 中，改呼叫 `GET /v1/status`
⚠️ 行 773 的 `_bridge_post("/send")` 在訊息回調 lambda 中，改呼叫 `POST /v1/send`

---

## Phase 4：MongoDB Collections 策略

兩個服務共用同一個 MongoDB（`realty_line` database）。

| Collection | 由誰寫入 | 由誰讀取 | 策略 |
|-----------|--------|--------|------|
| `col_line_cache` | line-api（從 bridge refresh） | line-api（/v1/contacts 等） | 留在 line-api |
| `col_messages` | realty-line（bridge webhook） | realty-line（訊息頁） | 留在 realty-line |
| `col_bridge_events` | realty-line（webhook） | 無（稽核用） | 留在 realty-line |
| `col_api_keys` | 兩者共用同一 collection | line-api（認證）、realty-line（CRUD UI） | 共用 collection；realty-line 的 `/api/keys/*` 直接讀寫，line-api 做認證 |
| `col_api_usage` | line-api + realty-line 共用 | line-api + realty-line | **保留在兩邊**：realty-line 的 `_verify_attempt_check()` 仍需寫入 |
| `col_schedules` | 兩者共用 | realty-line 寫入、realty-line 執行排程 | **排程執行暫留 realty-line**（`_run_scheduled_posts` + APScheduler）；line-api 只提供寫入 endpoint |
| `col_users` | 兩者共用 | 兩者共用 | 共用，不動 |

---

## Phase 5：E2EE 路由（暫緩）

以下路由目前直接操作 bridge，不在本次移轉範圍：

- `/api/line/e2ee-*`（行 2941–3991，共 17 個路由）

這些是研究性功能，等 E2EE 穩定後再考慮移到 line-api。

---

## 已知設計決策

| 項目 | 決策 | 原因 |
|------|------|------|
| API key CRUD (`/api/keys/*`) | 保留在 realty-line | 用 `@login_required`（cookie），是 UI 管理介面；兩服務共用 `col_api_keys` collection |
| 排程執行 (`_run_scheduled_posts`) | 保留在 realty-line | 依賴 `col_tag_map`（黑名單）、`col_line_cache`（聯絡人名稱）、`col_users`、`col_schedules`，均為 realty-line 專屬；未來視需求再移 |
| `col_api_usage` | 兩邊共用 | realty-line 的 `_verify_attempt_check` 用它做 rate limiting |
| `/refresh-qr` 替換方式 | `GET /v1/qr?refresh=1` | line-api 的 `/v1/qr` 是 GET，透過 query string 觸發 refresh |
| bridge `WEBHOOK_URL` 方向 | 維持指向 realty-line | bridge 主動 push 給 realty-line 的 `/api/_hook/state` 和 `/api/_hook/messages`；line-api 不需要接收這些 webhook，不改 `WEBHOOK_URL` |
| `MONGO_DB_NAME`（line-api） | 建議改用 env var | line-api/db.py 目前硬編碼 `"realty_line"`；上線後如需多環境應改 `os.environ.get("MONGO_DB_NAME", "realty_line")` |

---

## 執行順序

- [ ] Phase 1：line-api 確認上線穩定
- [ ] Phase 2-A：加入 `_lineapi_get` / `_lineapi_post` helper
- [ ] Phase 2-B：替換 38 個 bridge 呼叫點（含 `/refresh-qr` → `GET /v1/qr?refresh=1`）
- [ ] Phase 2-C：處理 provision 邏輯（`_resolve_bridge` / `_provision_user_bridge` 移除）
- [ ] Phase 2-D：刪除 realty-line 內的 /v1/* 路由 + `api_key_required` decorator
- [ ] Phase 2-E：更新 /api/* 路由
- [ ] Phase 3：Zeabur 環境變數調整
- [ ] Phase 3.5：確認 webhook endpoints 內的 bridge 呼叫也已替換
- [ ] Phase 4：確認 collections 讀寫正確
- [ ] Phase 5：E2EE（暫緩）
