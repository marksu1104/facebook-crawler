# facebook-crawler

在電腦上追蹤 Facebook 社團的新收錄貼文，依自訂關鍵字比對，透過 Telegram 傳送通知與原始貼文連結。可設定二手物品、排球臨打、活動或其他主題。

目前提供單一社團的本機操作介面，使用 Python、FastAPI、Selenium 與原生 HTML/CSS/JavaScript。

## 執行需求

- Windows、macOS 或 Linux 桌面環境，Python 3.10 以上，以及 Google Chrome 或 Microsoft Edge。Linux 也可使用 Chromium。
- macOS 建議使用 Python 官網的安裝程式。Linux 需有可開啟瀏覽器的桌面工作階段；部分發行版需先安裝 `python3-venv`。
- 首次啟動需連線安裝 Python 套件。Selenium 會管理瀏覽器驅動程式。
- 持續追蹤可連續運行數天，需保持電腦開機、程式與追蹤瀏覽器執行，並維持網路連線。

## 開始使用

1. 下載或 clone 此 repo，依下表啟動。程式會自動建立 `.venv` 並安裝套件，後續啟動會檢查套件清單是否變更。
2. 等待安裝完成，瀏覽器會開啟 [本機操作頁面](http://127.0.0.1:8766/)。
3. 填入可查看的 Facebook 社團首頁或 `share/g` 分享網址，設定關鍵字與排除詞，再儲存。
4. 按「開啟 Facebook 追蹤視窗」，在獨立視窗親自登入並完成驗證，將社團排序切換為「新貼文」。
5. 回到操作頁按「已登入，確認社團」，再按「讀取貼文預覽」。首次讀取建立追蹤起點，已收錄的舊文保留在預覽中。
6. 完成下方 Telegram 設定並測試手機送達，再按「開始追蹤」。

| 系統 | 啟動方式 |
| --- | --- |
| Windows | 雙擊 `start.cmd`，`verify.cmd` 也可啟動 |
| macOS | 雙擊 `start.command`，或在專案目錄執行 `sh start.command` |
| Linux | 在專案目錄執行 `sh start.sh` |

macOS 若提示檔案沒有執行權限，可在專案目錄執行 `chmod +x start.command` 再雙擊。三平台共用 `start.py`，也可用 Python 3.10 以上執行 `python3 start.py`。移動專案至其他作業系統時，請重新建立該系統的 `.venv`。

啟動程式後需手動確認社團並開始追蹤。首次有效讀取會保存追蹤起點，之後重開程式並確認同一社團時，會沿用起點與去重紀錄。電腦睡眠會暫停檢查，恢復後按排程繼續；程式結束或關機後需重新啟動。可按「停止追蹤」停止排程，按頁面底部「結束程式」或在啟動視窗按 Ctrl+C 結束程式。

## Telegram 手機通知

1. 在 Telegram 的 [BotFather](https://t.me/BotFather) 傳送 `/newbot`，建立 Bot 並取得 Token。
2. 開啟自己的 Bot 聊天室，親自傳送 `/start`。
3. 在操作頁展開「手機通知」，填入 Token，勾選「記住這台電腦的 Token」並儲存。
4. 按「讀取聊天清單」，核對並選擇自己的私人聊天，再儲存接收對象。
5. 按「發送測試訊息」，確認手機收到。也可在貼文預覽按「發送這篇測試通知」，驗證原文連結。

Windows 使用目前使用者的 DPAPI 加密保存 Token。macOS 使用 Keychain，Linux 使用 Secret Service 相容鑰匙圈（例如 GNOME Keyring）保存加密金鑰，以 Fernet 加密 Token 後存入本機資料庫。相同系統帳戶重開程式時會自動載入。Linux 需啟用鑰匙圈服務與桌面的 D-Bus 工作階段；安全儲存無法使用時，介面會停用「記住」選項，Token 可供本次執行使用。

Token 欄位儲存後清空，設定 API 只回傳設定與保存狀態。取消勾選並儲存會移除保存的 Token，本次執行仍可使用；「清除 Token」會同時清除本次設定與保存的 Token。macOS／Linux 鑰匙圈會保留加密金鑰，供之後重新設定時使用。

聊天清單顯示曾向 Bot 傳送 `/start` 的私人聊天室，選擇前請核對接收對象。

## 比對與追蹤行為

- 關鍵字支援逗號或換行分隔，可選擇符合任一或全部關鍵字。包含任何排除詞的貼文會被排除。關鍵字留空代表符合所有通過其他條件的貼文。
- 「不想看到的詞」比對貼文內文。「更多比對條件 → 排除的發文者」可每行填一個完整顯示名稱，英文忽略大小寫。儲存後會重新比對已收錄貼文，後續自動通知也會套用名單。
- 每次預設最多讀取 20 篇，可在「讀取與追蹤設定」調整為 1–50 篇。讀取會分批向下載入，實際數量受頁面、載入次數與時間上限影響。
- 首次讀取只建立起點。後續首次收錄、符合條件且有原文連結的貼文才會自動通知。同一貼文依穩定 ID 去重。
- 預設每 120 秒檢查並持續運行，直到手動停止或遇到需處理的登入與存取限制。「限定次數與時間」模式可用於短時間測試。
- 每輪新符合貼文會合併成 Telegram 通知，作者以粗體顯示，內容預覽保留段落，時間列在預覽下方，可按「開啟原文」查看貼文。超過 Telegram 單則長度限制時分段發送，並標示第幾則，每篇預覽與連結完整保留。
- 通知另外標示發送時間與時區，預設為 `Asia/Taipei`，可在「讀取與追蹤設定」調整。精確發文時間取自同一篇貼文的結構化資料，或原文標頭的時間元素。頁面僅提供相對時間時，會明確標示精確時間未取得，並提供首次偵測時間。
- 手動檢查、停止再開始或修改設定，皆沿用已生效的冷卻時間。
- 修改比對條件會重新計算已收錄結果，並重新建立追蹤起點。變更比對條件或接收設定後，先前排隊中的通知保留供手動選擇。
- 持續模式遇到社團載入逾時或 Telegram 連線暫時異常時會延後重試，連續失敗時拉長間隔，最長每小時一次。Telegram 要求冷卻時會遵守服務指定的等待時間。
- 發送成功才將同則摘要內的貼文一起標記為已通知。尚未送出的通知會保存到下一輪；重啟時，發送途中但送達狀態不明的訊息會保留供使用者核對。
- 遇到登入、驗證、存取限制或讀取異常時會暫停，等待使用者親自處理並確認。帳號密碼與驗證碼直接在 Facebook 視窗輸入。

Facebook 頁面可能變更，截斷或尚未載入的內容可能漏讀。「首次收錄」表示程式第一次取得該貼文，可能包含稍後才載入的舊文。此工具無法保證完整抓取、即時送達或帳號安全，使用者需自行確認平台規範及使用權限。

## 驗證狀態

- 自動化測試涵蓋假瀏覽器、模擬 Telegram transport、DOM、Windows DPAPI 與鑰匙圈加密，包含跨日排程、延後重試、合併通知、分段途中失敗、舊資料庫升級與重啟後的去重。
- GitHub Actions 在 Windows、macOS 與 Ubuntu 執行測試，包含啟動操作頁、乾淨結束程式與實際 Chrome 啟動。登入 Facebook 的人工驗證目前完成於 Windows。
- 已在實際登入的社團驗證多篇主文、原文連結、背景讀取與修改條件後的重新比對。
- 已完成 Telegram 實際通知與格式確認。首次設定時請使用「發送測試訊息」確認自己的手機送達。

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

macOS／Linux 可使用下列開發指令，Node.js 僅供 JavaScript 測試使用。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python verify.py --no-browser
.venv/bin/python -m pytest -q
npm ci --ignore-scripts
npm test
```

| 路徑 | 用途 |
| --- | --- |
| `start.py`、`start.cmd`、`start.command`、`start.sh` | 三平台環境準備與啟動 |
| `verify.py` | 本機服務與重複啟動檢查 |
| `app/pilot.py` | 設定、SQLite、操作流程、排程與通知狀態 |
| `app/pilot_browser.py` | 手動登入與有限次數的社團讀取 |
| `app/post_extractor.js`、`app/post_metadata.js` | 可見主文與已載入貼文資料的擷取 |
| `app/feed_traffic.py` | 目前瀏覽器文件已接收的 feed 回應 |
| `app/models.py`、`app/browser.py` | 關鍵字比對與原文網址正規化 |
| `app/notifications.py` | 合併通知、預覽長度與時區時間格式 |
| `app/telegram.py`、`app/token_store.py` | Telegram 通知與系統安全儲存 |
| `app/static/pilot.*` | 操作介面 |
| `tests/` | Python 與 JavaScript 測試 |

一般追蹤條件可直接從介面調整。Facebook 版面相容性集中在擷取腳本與瀏覽器模組。`scripts/read-pilot-structure.ps1 -ValidateExtractor` 可檢查本程式已確認的社團視窗，回傳數量及欄位狀態。

## 本機資料與 Git

設定、Token 密文、收錄貼文及獨立瀏覽器登入資料預設存於 `data/pilot/`。這些內容，以及本機封存的 `legacy/`、測試產物 `artifacts/`、環境檔與憑證，皆由 `.gitignore` 排除。

公開或分享時請使用 Git 追蹤的程式檔案。`.gitignore` 只影響待加入的檔案，已存在於舊 commit 的資料仍會留在 Git 歷史。
