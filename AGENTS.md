# codex-image-service — AI Agent 使用指引

這個服務把 Codex CLI 的 `$imagegen` 包成 HTTP API：畫圖、給參考圖改圖、看圖回答。每張圖用的是背後那個 ChatGPT 帳號的額度。

## 連線與金鑰

- 本機跑（`docker compose -f docker-compose.local.yml up -d --build`）的網址是 `http://localhost:8000`；別人架的服務用對方給的網址，例如 `https://example.com/codex-image`。
- 每個請求都要帶 `Authorization: Bearer <金鑰>`，金鑰開頭是 `cimg_`，在 `/admin` 的 API Keys 頁發。金鑰從環境變數讀（例如 `CODEX_IMAGE_KEY`），**不要寫進程式碼或 commit**。
- 一張圖大約 70～180 秒。呼叫端可能逾時（Cloudflare Worker、nginx 預設、GitHub Actions 的單步驟）時，用排隊版。

| 端點 | 用途 |
|---|---|
| `POST /v1/images/generate` | 同步畫圖，等到好才回，回傳圖片網址 |
| `POST /v1/images/jobs` | 排隊版，馬上回 `202` 和 `id` |
| `GET /v1/images/jobs/{id}` | 查排隊狀態：`queued`／`running`／`succeeded`／`failed`／`expired` |
| `POST /v1/vision` | 看圖回答：`{prompt, images_base64}` → `{text}` |
| `GET /health` | `{"status":"ok"}`，不用金鑰 |

## 請求欄位

`/v1/images/generate` 與 `/v1/images/jobs` 一樣：

| 欄位 | 說明 | 預設 |
|---|---|---|
| `prompt` | 圖片描述 | 必填 |
| `size` | `寬x高`，例如 `1024x1024`、`1536x1024` | `1024x1024` |
| `quality` | `low`／`medium`／`high`／`auto` | `medium` |
| `count` | 1～4 張（改圖模式固定 1） | `1` |
| `reference_images_base64` | 1～4 張參考圖的 base64，有給就是改圖模式 | 無 |

回傳的 `images[].url` 是下載網址，預設保留 7 天（`expires_at`），要留就自己下載存檔。

## 範例

```bash
# 同步畫圖
curl -sS --fail --max-time 650 -X POST http://localhost:8000/v1/images/generate \
  -H "Authorization: Bearer $CODEX_IMAGE_KEY" -H "Content-Type: application/json" \
  -d '{"prompt":"a cute orange cat, watercolor","size":"1024x1024","quality":"medium"}'

# 排隊版：送出後輪詢
curl -sS -X POST http://localhost:8000/v1/images/jobs \
  -H "Authorization: Bearer $CODEX_IMAGE_KEY" -H "Content-Type: application/json" \
  -d '{"prompt":"a cute orange cat, watercolor"}'
curl -sS http://localhost:8000/v1/images/jobs/<id> -H "Authorization: Bearer $CODEX_IMAGE_KEY"
```

```python
import os, time, httpx

base = os.getenv("CODEX_IMAGE_BASE_URL", "http://localhost:8000")
headers = {"Authorization": f"Bearer {os.environ['CODEX_IMAGE_KEY']}"}

job = httpx.post(f"{base}/v1/images/jobs", headers=headers,
                 json={"prompt": "a cute orange cat, watercolor"}).json()
for _ in range(80):                      # 最多等大約 10 分鐘
    time.sleep(5 if _ < 18 else 10)      # 前 90 秒每 5 秒，之後每 10 秒
    st = httpx.get(f"{base}/v1/images/jobs/{job['id']}", headers=headers).json()
    if st["status"] in ("succeeded", "failed", "expired"):
        break
if st["status"] == "succeeded":
    png = httpx.get(st["images"][0]["url"]).content
    open("cat.png", "wb").write(png)
else:
    print("沒畫出來：", st.get("error"))
```

## 錯誤處理

| 狀況 | 意義 | 建議 |
|---|---|---|
| `401`／`403` | 金鑰沒帶或不對 | 檢查 `Authorization: Bearer` 標頭 |
| `503` | 排隊滿了 | 稍後再送 |
| `failed`，error 提到額度或 limit | 帳號畫圖額度用完 | 換備援（例如官方 Gemini 生圖），或等額度重置；`/admin` 首頁看得到各帳號剩多少 |
| 等很久沒結果 | 一張本來就要 1～3 分鐘 | 用排隊版，不要拉長同步請求的逾時 |

## 安裝（需要人工的步驟）

1. 在這台電腦裝好 Codex CLI，並**由人自己**完成 `codex login`（用 ChatGPT 帳號登入）。
2. `cp .env.example .env`，至少改 `ADMIN_PASSWORD`、`ADMIN_SESSION_SECRET`。
3. `docker compose -f docker-compose.local.yml up -d --build`，`curl http://localhost:8000/health` 回 `{"status":"ok"}`。
4. 開 `http://localhost:8000/admin` 登入，發一把金鑰。

### 一個帳號還是多個帳號

用哪個帳號只看 `CODEX_HOMES` 有沒有設，跟電腦是哪一台無關：

- **沒設 `CODEX_HOMES`**：`docker-compose.local.yml` 會把這台電腦的 `~/.codex/auth.json` 唯讀掛進去，用的就是上面 `codex login` 的那一個帳號。
- **多個帳號輪流**：每個帳號先各登入一次（由人操作）：
  ```bash
  mkdir -p ~/codex-homes/甲 && CODEX_HOME=~/codex-homes/甲 codex login
  mkdir -p ~/codex-homes/乙 && CODEX_HOME=~/codex-homes/乙 codex login
  ```
  再在 `.env` 設 `CODEX_HOMES=/host_codex_homes/甲:/host_codex_homes/乙`（`~/codex-homes` 在容器裡掛成 `/host_codex_homes`）。每次請求換下一個帳號，失敗會換帳號重試，週額度剩不到 `CODEX_MIN_QUOTA_PERCENT`（預設 5）% 的帳號先跳過。
- `docker-compose.yml`（掛在 nginx 後面那份）**不會**掛主機的 `auth.json`，用它就一定要設 `CODEX_HOMES`，不然沒有帳號可用。

分辨一台機器現在用哪種：`docker inspect <容器名> --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}'` 看是哪份 compose，再看環境變數裡有沒有 `CODEX_HOMES`。

只在自己電腦上用，程式連 `localhost:8000` 就好。GitHub Actions 這類在別台機器上跑的程式連不到 `localhost`，要讓它們用，服務需要一個外面連得到的網址（最好是 HTTPS）：自己的網域加 nginx（README 的 Production 一節），或 Cloudflare Tunnel、Tailscale Funnel、ngrok 這類通道，不用自己開 port（這幾種作者沒有實測過）。
