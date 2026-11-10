# facebook-crawler

在電腦上追蹤 Facebook 社團的新收錄貼文，依自訂關鍵字比對，透過 Telegram 傳送通知與原始貼文連結。可設定二手物品、排球臨打、活動或其他主題。

目前提供單一社團的本機操作介面，使用 Python、FastAPI、Selenium 與原生 HTML/CSS/JavaScript。

## 執行需求

- Windows、Python 3.10 以上，以及 Microsoft Edge 或 Google Chrome。
- 首次啟動需連線安裝 Python 套件。Selenium 會管理瀏覽器驅動程式。
- 日常使用需保持電腦開機、程式與追蹤瀏覽器執行。

## 開始使用

1. 下載或 clone 此 repo，雙擊 `start.cmd`。`verify.cmd` 也可啟動同一個程式。
2. 等待安裝完成，瀏覽器會開啟 [本機操作頁面](http://127.0.0.1:8766/)。
3. 填入可查看的 Facebook 社團首頁或 `share/g` 分享網址，設定關鍵字與排除詞，再儲存。
4. 按「開啟 Facebook 追蹤視窗」，在獨立視窗親自登入並完成驗證，將社團排序切換為「新貼文」。
5. 回到操作頁按「已登入，確認社團」，再按「讀取貼文預覽」。首次讀取建立追蹤起點，已收錄的舊文保留在預覽中。
6. 完成下方 Telegram 設定並測試手機送達，再按「開始追蹤」。

啟動程式後需手動開始追蹤。電腦睡眠、關機或程式結束即停止。可按頁面底部「結束程式」，或在啟動視窗按 Ctrl+C。

## Telegram 手機通知

1. 在 Telegram 的 [BotFather](https://t.me/BotFather) 傳送 `/newbot`，建立 Bot 並取得 Token。
2. 開啟自己的 Bot 聊天室，親自傳送 `/start`。
3. 在操作頁展開「手機通知」，填入 Token，勾選「記住這台電腦的 Token」並儲存。
4. 按「讀取聊天清單」，核對並選擇自己的私人聊天，再儲存接收對象。
5. 按「發送測試訊息」，確認手機收到。也可在貼文預覽按「發送這篇測試通知」，驗證原文連結。

Windows 使用目前使用者的 DPAPI 加密保存 Token，同一個 Windows 帳戶重開程式時會自動載入。Token 欄位儲存後清空，設定 API 只回傳設定與保存狀態。取消勾選並儲存會移除保存的副本，本次執行仍可使用；「清除 Token」會同時清除本次設定與保存的副本。其他作業系統目前僅保留本次執行的 Token。

聊天清單顯示曾向 Bot 傳送 `/start` 的私人聊天室，選擇前請核對接收對象。

## 比對與追蹤行為

- 關鍵字支援逗號或換行分隔，可選擇符合任一或全部關鍵字。包含任何排除詞的貼文會被排除。關鍵字留空代表符合所有通過其他條件的貼文。
- 每次預設最多讀取 20 篇，可在「讀取與追蹤設定」調整為 1–50 篇。讀取會分批向下載入，實際數量受頁面、載入次數與時間上限影響。
- 首次讀取只建立起點。後續首次收錄、符合條件且有原文連結的貼文才會自動通知。同一貼文依穩定 ID 去重。
- 預設每 120 秒檢查，最多 3 次、最長 10 分鐘，每輪最多自動通知 3 則。達到次數或時間上限即停止，可手動再次開始。超出通知上限的貼文保留供預覽與手動發送。
- 手動檢查、停止再開始或修改設定，皆沿用已生效的冷卻時間。
- 修改比對條件會重新計算已收錄結果，並重新建立追蹤起點。
- 發送成功才記錄為已通知。送達狀態不明時保留紀錄，待使用者核對。Telegram 要求冷卻時會暫停。
- 遇到登入、驗證、存取限制或讀取異常時會暫停，等待使用者親自處理並確認。帳號密碼與驗證碼直接在 Facebook 視窗輸入。

Facebook 頁面可能變更，截斷或尚未載入的內容可能漏讀。「首次收錄」表示程式第一次取得該貼文，可能包含稍後才載入的舊文。此工具無法保證完整抓取、即時送達或帳號安全，使用者需自行確認平台規範及使用權限。

## 驗證狀態

- 已通過假瀏覽器、模擬 Telegram transport、DOM 與 Windows DPAPI 的自動化測試。
- 已在實際登入的社團驗證多篇主文、原文連結、背景讀取與修改條件後的重新比對。
- **Telegram 通知實際送達手機仍待確認。** 可使用「發送測試訊息」與單篇貼文測試功能完成驗證。

## 開發與自訂

```powershell
# 安裝執行與測試套件
py -3 -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements-dev.txt

# 啟動，可用 --port 或 --data-dir 指定埠號與獨立資料目錄
.venv/Scripts/python.exe verify.py --no-browser

# Python 測試
powershell -NoProfile -File scripts/check.ps1

# 選用的前端與擷取腳本測試
npm ci --ignore-scripts
npm test
```

| 路徑 | 用途 |
| --- | --- |
| `verify.py`、`scripts/start.ps1` | 本機啟動與重複啟動檢查 |
| `app/pilot.py` | 設定、SQLite、操作流程、排程與通知狀態 |
| `app/pilot_browser.py` | 手動登入與有限次數的社團讀取 |
| `app/post_extractor.js`、`app/post_metadata.js` | 可見主文與已載入貼文資料的擷取 |
| `app/feed_traffic.py` | 目前瀏覽器文件已接收的 feed 回應 |
| `app/models.py`、`app/browser.py` | 關鍵字比對與原文網址正規化 |
| `app/telegram.py`、`app/token_store.py` | Telegram 通知與 Windows Token 加密 |
| `app/static/pilot.*` | 操作介面 |
| `tests/` | Python 與 JavaScript 測試 |

一般追蹤條件可直接從介面調整。Facebook 版面相容性集中在擷取腳本與瀏覽器模組。`scripts/read-pilot-structure.ps1 -ValidateExtractor` 可檢查本程式已確認的社團視窗，回傳數量及欄位狀態。

## 本機資料與 Git

設定、Token 密文、收錄貼文及獨立瀏覽器登入資料預設存於 `data/pilot/`。這些內容，以及本機封存的 `legacy/`、測試產物 `artifacts/`、環境檔與憑證，皆由 `.gitignore` 排除。

公開或分享時請使用 Git 追蹤的程式檔案。`.gitignore` 只影響待加入的檔案，已存在於舊 commit 的資料仍會留在 Git 歷史。
