# Find a Job · 找工作小幫手

一個可在自己的電腦上執行的 AI 求職小幫手，透過聊天尋找職缺、整理求職條件，並追蹤每日新職缺。

A self-hosted AI job-search companion for finding jobs through chat, managing search preferences, and checking daily job updates.

## 繁體中文

### 功能

- 聊天搜尋台灣 104／518 職缺、查看公司評價與收藏工作。
- 確認求職條件後，每日搜尋並提供新職缺通知。
- 每位使用者有自己的專屬連結，對話、收藏與條件分開保存。
- A／B 兩種外觀、像素角色陪伴，以及後台設定與備份還原。

### 執行方式（Windows）

1. 安裝 **Python 3.13**，安裝時包含 Python Launcher（`py`）。首次建置需要網路連線。
2. 下載本專案並解壓縮，或使用 Git：

   ```powershell
   git clone https://github.com/xu-0306/Find-a-job.git
   cd Find-a-job
   ```

3. 雙擊 **`setup.cmd`**，或在專案目錄執行：

   ```powershell
   .\setup.cmd
   ```

   腳本會建立 `.venv`、安裝依賴，並在首次使用時請你設定管理員密碼。也會嘗試安裝 Tailscale，供需要外部連線時使用。完成後會直接啟動服務並開啟後台，不必立即再執行 `start.cmd`。

4. 往後日常使用，只需雙擊 **`start.cmd`**：

   ```powershell
   .\start.cmd
   ```

   預設管理後台：[http://localhost:8041/admin](http://localhost:8041/admin)。若已修改連接埠，請以啟動視窗顯示的網址為準。使用時保留視窗，按 **Ctrl+C** 停止；重新執行 `start.cmd` 會重啟本專案服務並中斷進行中的請求。

### 第一次設定

1. 用剛設定的管理員密碼登入後台。
2. 填入自己的模型 API 網址、模型名稱、連線格式與金鑰，執行連線測試。網路搜尋／評價功能可另設定 Tavily 或 Brave。
3. 在後台新增使用者並建立專屬連結，開啟連結即可進入前台。連結只顯示一次，請妥善保存；遺失時可由管理員重設。
4. 在前台確認自己的求職條件後，開始每日搜尋。若要從其他裝置連線，依後台引導登入 Tailscale 並啟用 Funnel；只在本機使用可略過外部連線設定。

模型與搜尋服務需自行提供 API 金鑰，費用依供應商計算。服務需保持運行，才能執行排程與發送通知。

### 資料

資料儲存在本機 `data/`；請使用後台備份功能保存資料。API 金鑰、資料庫、`.env`、憑證與私人備份不應提交到 GitHub。帳號隔離的是其他使用者；管理員仍可讀取本機資料，聊天內容會交給你設定的模型供應商。

### 開發與 CI（不重啟正式服務）

在另一個 Git worktree 開發，保留正在提供服務的目錄。例如從已確認的 `main` 建立開發分支：

```powershell
git worktree add ..\Find-a-job-dev -b codex/my-change main
cd ..\Find-a-job-dev
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\dev.cmd --init-admin
.\dev.cmd
```

`--init-admin` 只設定開發環境的管理員密碼；請自行輸入，沒有預設密碼。後台預設為 `http://127.0.0.1:8141/admin`。需要另一個埠可用 `dev.cmd --port 8142`，或 `--port 0` 由系統選擇，實際埠記在本機 `.worktree-ports.json`。埠已被占用就退出，不會終止其他程序；按 Ctrl+C 關閉本次開發服務。

開發入口要求使用本目錄的 `.venv`，資料固定放在本目錄 `.dev-data/`；忽略繼承的正式資料目錄、對外網址與代理設定，也不載入 `.env`。自動每日搜尋、實際 Push 發送及 Tailscale/Funnel 控制停用。前後台與手動操作仍可測試；若自行設定測試用模型或搜尋 API，手動操作仍可能產生外部請求與費用。不要把正式資料庫、設定、備份或登入資料複製進開發環境。

開發時使用 `dev.cmd`，不要使用會啟停正式模式或安裝 Tailscale 的 `setup.cmd`／`start.cmd`。Git worktree 隔離程式檔案，但不會自動隔離共用的環境變數、資料與系統服務。現有執行中的目錄也不應切換分支或直接 `git pull`，靜態檔更新可能立即影響使用者。

本機離線檢查需要 Python 3.13 與 Node.js 24：

```powershell
.\.venv\Scripts\python.exe scripts/check.py
```

GitHub Actions 對 `main`、`codex/**` 的 push、以 `main` 為目標的 PR，以及手動執行啟動相同檢查。測試使用暫存資料與合成回應，不需要 API secrets；第三方網站即時測試另行進行。CI 僅驗證程式，不部署、不連接本機正式服務，也不包含私人文件及歷史探測腳本。

正式部署維持手動：選定通過 CI 的提交，先準備新版本與依賴、確認資料庫相容性，在維護時段等待工作結束並建立可還原的完整快照，再切換及重啟正式服務，核對登入與主要功能。不要把開發資料帶入正式環境。現有後台匯出備份會移除 session 與 Push 訂閱，不能視為完整部署快照；資料庫升級後也不能直接以舊程式回滾。這一版沒有新增自動部署、完整快照或回滾工具。

## English

### Features

- Search Taiwan's 104 and 518 job boards through chat, check company reviews, and save favorites.
- Confirm your search preferences to receive daily job updates and notifications.
- Separate conversations, favorites, and preferences for each user, with a personal access link.
- Two visual themes, a pixel companion, admin settings, and backup/restore.

### Run on Windows

1. Install **Python 3.13**, including the Python Launcher (`py`). An internet connection is required for setup.
2. Download and extract this repository, or clone it:

   ```powershell
   git clone https://github.com/xu-0306/Find-a-job.git
   cd Find-a-job
   ```

3. Double-click **`setup.cmd`**, or run it from the project directory:

   ```powershell
   .\setup.cmd
   ```

   Setup creates `.venv`, installs dependencies, and asks you to create an admin password on first use. It also attempts to install Tailscale for optional remote access. When setup finishes, it starts the server and opens the admin page automatically; there is no need to run `start.cmd` again immediately.

4. For later launches, double-click **`start.cmd`**:

   ```powershell
   .\start.cmd
   ```

   The default admin page is [http://localhost:8041/admin](http://localhost:8041/admin). If you changed the port, use the URL printed in the startup window. Keep that window open while using the app; press **Ctrl+C** to stop. Running `start.cmd` again restarts this project's server and interrupts active requests.

### First-time configuration

1. Sign in to the admin page with the password you created.
2. Enter your model API URL, model name, API format, and key, then run the connection test. Configure Tavily or Brave separately for web search and review lookup.
3. Create a user and their personal access link in the admin page. Open that link to use the app. Links are shown only once; the admin can reset a lost link.
4. Confirm your job-search preferences in the app to enable daily searches. For access from other devices, follow the admin page's Tailscale sign-in and Funnel setup steps. Remote access is optional for local use.

Bring your own model and search API keys; provider fees may apply. Keep the server running for scheduled searches and notifications.

### Isolated development and CI

Use a separate Git worktree with its own Python 3.13 `.venv`, then run `dev.cmd --init-admin` once and `dev.cmd` to serve on loopback port 8141. Use `--port 0` to allocate a free port. This entry point uses only that checkout's `.dev-data`, ignores production data/origin/peer environment settings and `.env`, disables automatic searches, Push delivery and Funnel, and never stops another server. Do not copy production data or run the production `setup.cmd` / `start.cmd` for development.

With Node.js 24 available, run `.venv\Scripts\python.exe scripts/check.py` for the same offline regression checks as GitHub Actions. CI runs on pushes to `main` / `codex/**`, pull requests to `main`, and manual dispatch, without production secrets or deployment access. Production updates remain manual, with a maintenance window, a complete recovery snapshot, database compatibility checks and post-start verification. No automated deployment or rollback tool is included.

### Data

Data is stored locally in `data/`; use the admin backup feature to keep a copy. Do not commit API keys, databases, `.env`, certificates, or private backups to GitHub. Accounts isolate users from one another, but the administrator can still read local data, and chat content is sent to your configured model provider.
