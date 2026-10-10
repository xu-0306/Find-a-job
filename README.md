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

### Data

Data is stored locally in `data/`; use the admin backup feature to keep a copy. Do not commit API keys, databases, `.env`, certificates, or private backups to GitHub. Accounts isolate users from one another, but the administrator can still read local data, and chat content is sent to your configured model provider.
