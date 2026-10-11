# 1111 實驗來源與 Playwright／Patchright 探針

**2026-10-11 最新決策：1111 接入為可實際啟用的實驗來源，後台預設關閉。以下獨立探針結果保留為歷史證據；尚未部署至目前運行中的後端。**

正式 Python app 在管理者啟用並保存後，才以 source.mjs 啟動隔離的 Patchright／Chrome，供聊天搜尋、續查、每日搜尋及職缺詳細使用。518 仍預設開啟，兩者均以開關按鈕呈現。現有 Docker 映像不包含此選用環境。

## 選用來源環境與界線

- 在欲部署的 checkout 安裝 Node.js 20+、Google Chrome，於本目錄執行 npm.cmd ci --ignore-scripts --no-audit --no-fund。來源使用有視窗的 Chrome，需要可開啟瀏覽器的桌面環境，不會自動安裝依賴。
- Python 透過 JSON stdin/stdout 呼叫 source.mjs；一次一個瀏覽器操作、60 秒總上限，取消時清理自己啟動的程序。每次使用全新臨時 profile；不沿用私人 cookie、不點驗證框、不自動重試。
- 搜尋支援全國或單一縣市／行政區，使用 1111 官方 /api/v1/codeCategories/city 目錄精確對應名稱；多地區或無法唯一對應時明示不支援，不能默默改查全國。每輪最多前五頁，完整性仍依來源分頁 metadata 與職缺 ID 核對。
- 搜尋卡片的薪資／工作地點尚未可靠解析，保留未知與提醒；不保證最低薪資篩選。詳細內容經職缺及公司身分核對後提供。
- 載入逾時、人機驗證或不支援格式會回傳來源診斷，保留其他來源結果，不當成成功零筆，也不完成該来源的每日基線。
- npm.cmd test 執行本機合成頁面的 probe.test.mjs 與 source.test.mjs；GitHub Actions 也跑這些離線檢查，不連線 1111。合併 main 不觸發部署。

穩定性尚未解決：先前完整探針三輪為兩輪成功、一輪人工驗證受阻；本次新橋接兩輪載入失敗，直接 runSource 與 Python→Node main 診斷各成功讀到一頁完整 30 筆（失敗輪未保存足夠資料以確認成因），不能據此宣稱無人操作穩定。固定 profile、跨日成功率及其他運行環境留待後續研究。

## 獨立探針

probe.mjs 仍可單獨執行兩頁搜尋及兩筆職缺詳細頁的低流量驗證，與 Python 的來源呼叫分開；結果只寫入被 Git 忽略的 output 目錄。
## 執行

在此目錄執行，需 Node.js 20 以上。Windows PowerShell 使用 `npm.cmd` / `npx.cmd`：

```powershell
npm.cmd ci --ignore-scripts --no-audit --no-fund
npx.cmd playwright install chromium
npm.cmd test
npm.cmd run probe -- --keyword 採購 --area 101600
```

`--area` 是選填的 1111 六位地區代碼；`101600` 為這次研究使用的台南市代碼。省略時不指定地區。這不是全部城市代碼的查詢工具。

可用 `--headed` 顯示瀏覽器，`--chrome` 改用電腦已安裝的 Google Chrome；預設使用 Playwright 的完整 Chromium。Playwright 使用新 context；Patchright 使用全新臨時 persistent profile。兩者均不使用個人瀏覽器 profile、cookie 或正式專案的設定。

```powershell
npm.cmd run probe -- --keyword 採購 --area 101600 --chrome --headed
```

Patchright 使用下列指令，不需另外下載瀏覽器；`--chrome` 使用本機已安裝的 Chrome：

```powershell
npm.cmd run probe -- --keyword 採購 --area 101600 --patchright --chrome --headed
$env:PROBE_DRIVER = 'patchright'
npm.cmd test
```

Patchright 鎖定 1.63.0、原 Playwright 保持 1.64.0；Patchright Console API 有限制，不能以沒有 console 訊息判斷沒有錯誤。官方啟動建議見 [Patchright Node.js](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright-nodejs)。

結果寫入 `output/playwright/<UTC時間>/report.json`；失敗時嘗試另存 `failure.png`，截圖失敗也會寫進報告。成功退出碼為 0，失敗／受阻為 1。缺少瀏覽器等啟動錯誤會直接顯示於終端並退出 1。不要在此目錄執行正式專案的 `start.cmd` 或 `setup.cmd`。

## 驗證界線

- 搜尋：比對 URL 的關鍵字、地區代碼、頁碼，以及可見搜尋框／標題。只取 `.job-card__content` 內帶 `mid=searchlist` 的職缺標題連結，排除首頁推薦、隱藏卡片、其他網域及重複 ID；保留職缺與公司名稱／網址。
- 完整性：以公開 HTML／頁面翻頁回應的分頁筆數及 ID 清單為依據，等待 DOM 中擷取的完整 ID 集合一致；不以非空、短暫穩定或固定 30 筆代替完整性。
- 翻頁：按網站的第 2 頁按鈕，等待頁碼、該頁完整 ID 集合及新 ID 出現；記錄跨頁重複數，不把網址變了但內容未更新當成功。
- 詳細：各頁取一筆不同職缺，核對 URL、標題、公司；保存可見全文及「工作內容」區段。欄位正規化尚未做，薪資／地點等保留在全文中。詳細頁區段擷取已核對本輪兩筆真實職缺，未驗全部版型。
- 每個導頁／DOM 等待最多 30 秒，失敗診斷及截圖各最多另用 5 秒，導頁間隔 1.5 秒、一次一頁；首個失敗即停，保留已成功的階段。`not_run` 不等於通過。
- `blocked_or_loading` 表示空白頁加上 Loading／⏳ 標題，無法區分尚未完成的載入與驗證攔截。其他空頁、不同條件或版型不符均回報失敗，不當成搜尋成功且零筆。
- 沒有結果或不足兩頁的查詢也不能完成這項三階段驗證。此探針不宣稱全量、每日穩定性、廣告分類或完整篩選語義已通過。

## 2026-10-11 完整兩頁與三輪穩定性驗證

**完整兩頁可取得，但全新 profile 的無人操作仍不穩定：本次 3 輪成功 2 輪、受阻 1 輪。** 一輪完整性驗證後再重跑兩次；每輪全新臨時 profile，使用同一版探針、同一主機／網路、Patchright 1.63.0 + Chrome 154.0.8037.98 headed，輪間 20 秒，沒有人工點驗證、失敗後重試或刪除失敗樣本。

| 輪次 | 第 1 頁 | 第 2 頁 | 跨頁重複 | 詳細頁 | 流程耗時 |
|---|---:|---:|---:|---|---:|
| 1 | 30 / 30 | 30 / 30 | 0 | 2 筆通過 | 15.434 秒 |
| 2 | 未取得 | 未執行 | — | 未執行 | 30.291 秒 |
| 3 | 30 / 30 | 30 / 30 | 0 | 2 筆通過 | 15.979 秒 |

第二輪搜尋頁停在 Loading；截圖顯示 Cloudflare「驗證您是人類」及未勾選方框，未進入職缺頁。程式以 `blocked_or_loading` 收尾，保留搜尋失敗、翻頁／詳細 `not_run`。成功輪次各自核對 60 個不重複職缺 ID；兩次擷取可能包含相同職缺，不能把兩輪加成 120 個不同職缺。

### 完整性如何判定

這是 1111 頁面資料格式的局部適配，沒有新增 API 抓取客戶端或依賴，也不把每頁 30 筆寫死：

1. 首頁只解析公開 HTML `#__NUXT_DATA__` 中 `apiJob.result` 的分頁欄位與職缺 ID；不讀登入或瀏覽器儲存。
2. 翻頁仍按 UI 按鈕，只監聽頁面自行發出的 `/api/v1/search/jobs/` 回應。該回應提供第二頁的分頁資料與 ID；原 HTML 的 SSR 資料翻頁後仍是第一頁，不能重用為第二頁證據。
3. 根據來源的 `page / limit / totalCount` 計算該頁應有筆數，要求來源 ID 唯一、實際擷取筆數一致，而且兩份 ID 集合完全相等。缺 metadata、混到舊頁或有漏卡都不能通過。
4. 同時保留查詢條件、卡片公司資料、第二頁確有新 ID，以及兩筆詳細身份／非空描述檢查。每階段仍有 30 秒上限。

實際載入觀察曾是 3 → 10 → 停留約 2 秒 → 14 → 30，因此只等筆數短暫穩定並不足以證明完成。Playwright／Patchright 各 **16 項離線測試通過**，新增每頁 3 筆／尾頁 2 筆、延遲補卡、永久部分頁、同筆數錯 ID、來源重複 ID 及 stale SSR → API 更新的反例；gpt-6.1-sol 子代理唯讀複核無必要修正。

### 本機證據與界線

- 完整批次及獨立斷言摘要：`output/playwright/2026-10-11T08-52-47-894Z-full-pages-stability/runs.json`、`summary.json`。記錄同一探針 SHA256，三輪之間程式未變。
- 第 1 輪：`output/playwright/2026-10-11T08-52-48-423Z/report.json`。
- 第 2 輪：`output/playwright/2026-10-11T08-53-25-926Z/report.json`、`failure.png`。
- 第 3 輪：`output/playwright/2026-10-11T08-54-17-619Z/report.json`。
- 載入序列／來源依據：`output/playwright/2026-10-11T08-46-16-173Z-page-readiness/readiness.json`、`2026-10-11T08-47-28-136Z-pagination-source/source.json`。
- 正式保全：`output/playwright/full-pages-production-before.json`、`full-pages-production-after.json`；51 追蹤檔 SHA256、main HEAD／乾淨狀態、後端 PID 808800／啟動時間與 8041／8088 監聽埠均不變。

本輪只修改既有實驗探針、測試與文件並同步本機記憶。沒有接入正式功能、變更正式依賴、CI、部署或重啟後端；原始輸出維持 Git 忽略。三次短期同機測試不足以推估長期成功率；跨日、其他關鍵字／地區、無頭模式與全部職缺的詳細資料均未驗。此結果尚不能支持每日無人操作穩定可用。

## 2026-10-11 Patchright 初步小量驗證（歷史）

**搜尋、翻頁、兩筆詳細資料在本次獨立實測通過。** 使用 Patchright 1.63.0、已安裝的 Chrome 154.0.8037.98、視窗模式及全新臨時 persistent profile；沒有人工點驗證、沿用個人 profile 或匯入 cookie。

| 項目 | 實測結果 |
|---|---|
| 採購／台南市搜尋 | URL、搜尋框與標題一致；第 1 頁取得 10 筆初期樣本 |
| 翻至第 2 頁 | 透過頁面按鈕操作；取得 30 筆，與本次第 1 頁樣本重複 0 筆 |
| 詳細頁 | 得生製藥「採購專員」及詠益自動化「採購助理.採購主管」；網址、標題、公司及非空職缺描述均核對通過 |
| 離線回歸 | Playwright、Patchright 各 13 項通過；涵蓋廣告排除、錯誤條件、假翻頁、空描述、DOM 讀取逾時與關閉頁面 |
| 隔離 | 變更限本實驗套件；原 Python app、依賴、CI 與部署不使用它 |

此早期版本的第一頁樣本在初期渲染時擷取；另一次稍晚的診斷可讀到 30 筆。當時 `passed` 只代表兩頁小量樣本與兩筆詳細資料可取得，**不代表已完整收齊兩頁 60 筆**，也不代表網站顯示的全部職缺或每日無人操作均可取得。薪資、地點等在詳細全文內，尚未正規化為獨立欄位。

本輪修正都保留失敗檢查：
- Patchright 使用專用 persistent context 的初始頁並置於前景；兩套驅動都記錄版本與執行模式。
- 網站側欄推薦也帶 `mid=searchlist` 與 `h2`。依實際 DOM 限定 `.job-card__content`；真正搜尋卡片缺公司仍失敗，不用略過缺欄位的方式製造成功。
- 詳細頁在「工作內容」後另有「職缺描述」子標題；以該標題後、下一標題前的可見內容核對，空內容不能借用薪資或導覽文字。
- 為 DOM 評估加有界逾時，避免瀏覽器讀取卡住時整個探針無限等待；診斷與截圖各自記錄失敗。

前段失敗與後段成功均保留：
- `2026-10-11T07-28-11-467Z/report.json`：最初 DOM 評估卡住，代理關閉本次專屬 Chrome 後收尾；不是網站自行關閉。
- `2026-10-11T07-34-44-204Z/report.json`：加上有界等待後，DOM 讀取與截圖仍逾時，約 40 秒自動結束。
- `2026-10-11T07-37-06-661Z-patchright-diagnostic/`：約 4 秒的短觀察顯示 Cloudflare「驗證您是人類」；不能以這段短觀察斷言長期受阻。
- `2026-10-11T07-40-34-179Z-patchright-diagnostic-30s/`：等待 30 秒後已出現真實搜尋頁；觀察到 ALTCHA 與 CF solve HTTP 200。這些回應本身不作通過依據。
- `2026-10-11T07-45-11-692Z-patchright-inspect/structure.json`：正常卡片與側欄廣告的 DOM 結構證據。
- `2026-10-11T07-47-15-991Z/report.json`：搜尋與翻頁通過，詳細頁舊解析器漏讀子標題內容。
- `2026-10-11T07-49-23-791Z/report.json`：最終新 profile 完整小量流程通過，耗時約 15.16 秒；沒有額外預熱搜尋。

以上路徑皆位於本機 `output/playwright/`。摘要及獨立斷言核對在 `patchright-validation-summary.json`；原服務與追蹤檔的前後比較在 `patchright-production-before.json`、`patchright-production-after.json`。原始頁面資料、截圖、profile 與 node_modules 不進 Git。

這次支持「Patchright 的此種啟動方式可在本機完成一次小量流程」，但沒有單變量比較可把成功完全歸因於 Patchright、初始頁或前景狀態。先前的驗證頁判斷仍成立；「所有獨立瀏覽器均無法取得資料」則已被本次成功取代。後續若要正式整合，再處理完整載入、欄位正規化與重複運行穩定性。

## 早期 Playwright 證據（Patchright 實測前）

| 方式 | 2026-10-11 結果 |
|---|---|
| Playwright headless shell、完整 Chromium headless、Chrome 視窗模式，直接開搜尋網址 | 等待後仍 Loading，沒有職缺 |
| Chrome headless／視窗模式，首頁等待互動元件就緒後填採購、按搜尋 | 首頁可讀，搜尋頁仍 Loading |
| Chrome headless，開啟觀察到的公開職缺連結 | 詳細頁標題有 ⏳，可見本文為空 |
| 正式探針：採購／台南，Chrome headless | `blocked_or_loading`；搜尋失敗，翻頁與詳細未執行 |
| 離線合成 DOM 測試 | 9 項通過；所有請求由 Playwright route 攔截，不連真網站 |

原始實測輸出只留本機 `output/playwright/`，不進 Git。2026-10-10 曾在 Codex 內建瀏覽器讀到兩頁搜尋與詳細頁，那是另一個瀏覽器環境，不能替代本次獨立探針驗收。這次內建瀏覽器結構檢查還觀察到搜尋 URL／內容不一致，因此加上條件比對，沒有把該頁當成成功證據。

已檢查公開 [robots.txt](https://www.1111.com.tw/robots.txt)；它不等同網站授權。遇驗證頁即停，未加入驗證碼代解、stealth、代理輪換或 cookie 複用。後續應先確認一般瀏覽器與網站允許的資料取得方式；取得正常公開頁後再重跑此探針。


## 2026-10-11 續查：Loading 停在哪裡（Patchright 實測前）

**已定位到 1111 驗證頁中的 Cloudflare Turnstile 階段，尚未確認 Cloudflare 作出判定的具體訊號。** 這與職缺卡片 selector 或正式後端無關：搜尋文件本身被驗證閘門取代，還沒有職缺 DOM 可解析。

本輪只使用獨立探針的全新瀏覽器環境。沒有讀取個人 browser profile／cookie／IndexedDB，沒有修改驗證程式或人工點選驗證碼。

| 本輪比較 | 結果 |
|---|---|
| Chrome 154 headless，全新 context；先首頁再搜尋 | 首頁正常；搜尋 30 秒仍 Loading |
| Chrome 154 一般視窗，全新臨時 persistent context；先首頁再搜尋 | 搜尋 30 秒仍 Loading |
| Chrome 154 一般視窗，全新臨時 persistent context；搜尋頁 bringToFront | 60 秒仍 Loading；實測 visibilityState=visible、hasFocus=true |

這裡的 persistent context 是新建的臨時一般瀏覽器環境，結束即關閉；不是複製或沿用使用者已驗證的 profile。因此結果排除了「只要不是無痕就能過」，沒有驗證「使用者完成驗證後的既有狀態可持續多久」。

三次網路紀錄一致：
- 搜尋 document 回 HTTP 200，但內容是 captcha-gate；document.readyState=complete、可見本文 0 字、職缺連結 0 筆。
- ALTCHA 與 Cloudflare challenge 端點均回 200；ALTCHA solve 端點也有 200 回應。這是 HTTP 層的觀察，不記錄或宣稱 token 內容。
- Turnstile 腳本與驗證 iframe 正常載入。沒有捕捉到未處理的 pageerror；未看到 Cloudflare 結果送回 1111 的 solve/cf 請求，也未看到最後 auth/claim 放行請求。
- 公開頁面程式先隱藏 loader，再等待 Turnstile callback／error-callback；此等待段沒有自行設定 timeout。最後量到 loader 為 display:none，錯誤訊息也隱藏，因而呈現空白本文與 Loading 標題。這說明介面表現，不代表已證實 Cloudflare 的判定理由。
- 每次搜尋記錄 27 個 document/script/fetch/xhr 回應，未達紀錄上限；去識別摘要及 assertions 在本機 output/playwright/diagnostic-20261011/summary.json 與 output/playwright/verify-diagnostics.mjs。

已排除的誤判：
- PAT 路徑回 401 是可預期的回退流程，不等於封鎖。[Cloudflare PAT 說明](https://developers.cloudflare.com/cloudflare-challenges/reference/private-access-tokens/)
- 某些 *.challenges.cloudflare.com 子網域 DNS lookup failure 是非阻塞測試。這次 brunhild 子網域失敗符合該類型，不能據此判斷 DNS 壞掉；主驗證網域、腳本與 iframe 都有成功回應。[Cloudflare 排錯說明](https://developers.cloudflare.com/cloudflare-challenges/troubleshooting/challenge-solve-issues/)
- 保持前景與延長至 60 秒仍失敗，並非單純頁面未取得焦點。使用一般視窗也仍是自動化瀏覽器；Cloudflare 明列 Playwright 等框架不支援解正式 challenge。[支援的瀏覽器環境](https://developers.cloudflare.com/cloudflare-challenges/reference/supported-browsers/)

### 為何之前內建瀏覽器可以

先前結果只證明「當時那個內建瀏覽器環境能進入職缺頁」。獨立 Playwright 每輪使用不帶既有驗證狀態的新 context；不同的瀏覽器版本、工作階段、網路環境或自動化訊號可能造成不同結果，但沒有同時對照就不能指定哪一項是根因。[Playwright context 隔離說明](https://playwright.dev/docs/browser-contexts)

本輪內建瀏覽器工具在載入連線模組時回 EPERM，未能重跑同條件比較；沒有改用其他機制存取該瀏覽器、複製它的驗證資料或查看私人儲存。歷史成功與本輪失敗不構成矛盾，也不能推論內建環境永遠可用。

### 可行路徑與目前判斷

1. **先驗證有人操作的正常瀏覽器流程。** 最有判別力的下一步是使用者在一般 Chrome 開同一查詢，若網站要求驗證，由使用者完成；之後只讀已正常顯示的頁面。可再評估獨立、專用的有狀態瀏覽器輔助工具，但持久化本身已證明不足以解決問題，這條路目前仍待驗。適合人工查詢，不能直接承諾每日無人值守。
2. **若目標是每日自動搜尋，向 1111 確認可授權的資料介面或合作供應。** 目前沒有驗證過可直接採用的全站 API；不以更換 Playwright／Selenium／Crawlee 當成驗證閘門的解法。
3. **既有網路搜尋可作職缺連結補充。** 不需依賴此處的互動搜尋頁，但覆蓋率、時效與完整分頁另有限制，不能當成完整 1111 搜尋的同等替代。本輪未接入，也未使用正式服務中的金鑰。

不更改全機 DNS、代理、Tailscale 或防護設定；不加入 stealth、驗證碼代解、驗證資料匯出或測試 sitekey 替換。正式功能維持不含 1111。

### 本機證據

診斷目錄：output/playwright/diagnostic-20261011/
- 2026-10-11T06-37-30-060Z/diagnostic.json：headless 新 context。
- 2026-10-11T06-41-11-063Z/diagnostic.json：一般視窗、新臨時 persistent context。
- 2026-10-11T06-44-20-000Z/diagnostic.json：前景／焦點與 60 秒等待。
- summary.json：三份結果的機器檢查摘要。

僅保存公開 DOM 摘要、資源狀態與去識別錯誤；沒有保存完整 HAR、HTTP headers、cookie 或回傳的驗證 token。動態 challenge URL 已遮蔽。研究原始輸出依然不進 Git。

## 公開程式參考

查核日期：2026-10-11。此次自行實作，沒有複製下列爬蟲程式。

| 專案 | 授權與維護觀察 | 可參考內容 |
|---|---|---|
| [SamJim0729/job_scraper](https://github.com/SamJim0729/job_scraper) | 未見 LICENSE／明確授權；2025-02-26 提交 | Selenium 的搜尋→ID→詳細頁流程；舊 selectors 未證明仍可用 |
| [kobojp/scraper_104_1111](https://github.com/kobojp/scraper_104_1111) | 未見 LICENSE／明確授權；1111 程式最後更新 2020-07-08，2026 更新為 README | 舊版 job-bank 網址與解析流程，不能直接套用現站 |
| [ori0nz/1111spider](https://github.com/ori0nz/1111spider) | [MIT](https://github.com/ori0nz/1111spider/blob/master/LICENSE)；2017-06-16 | 很舊的 Node／Cheerio 實作，有授權但需重寫網站適配 |
| [apify/crawlee](https://github.com/apify/crawlee) | [Apache-2.0](https://github.com/apify/crawlee/blob/master/LICENSE.md) | 活躍的通用 Playwright 爬取框架；沒有現成 1111 adapter，這次小型探針不需要引入 |

直接使用 [Playwright Library](https://playwright.dev/docs/library)，鎖定依賴於 `package-lock.json`，以 Node 原生測試工具驗證。公開可看原始碼，不代表已有可直接採用的授權或今天仍能執行。

## 2026-10-11 現成反機器人驗證方案研究（Patchright 實測前）

**有現成候選，但本輪沒有證實任何一款能完成目前 1111 的搜尋、翻頁與詳細頁。** 使用者指定 Luna max 查核開源工具及 1111 專用服務；主代理查核代管瀏覽器、費率及關鍵限制。本輪僅閱讀公開資料，未安裝、試跑、註冊、付費或接入正式功能。

這裡的「破解」實際涵蓋三件不同的事：減少瀏覽器自動化訊號、委託服務處理驗證、直接購用職缺擷取結果。前述本機紀錄顯示 1111 還有 ALTCHA 與最後放行流程，單獨宣稱 Turnstile 支援或取得一個 token，不能當成取得職缺的證據。Cloudflare 說明 token 有效期 300 秒且只能驗證一次；這是 token 規則，不是網站 session 的有效期。[官方驗證文件](https://developers.cloudflare.com/turnstile/get-started/server-side-validation/)

### 候選比較

| 候選 | 已核實的功能／成本 | 對此專案的判斷 |
|---|---|---|
| [Patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright) | Apache-2.0；Playwright 的 Chromium 修改版，有 Node 套件；官方明載 Console API 不可用，部分 Playwright 測試仍不通過 | 本機開源實驗首選，適配成本相對低，但需獨立版本與瀏覽器依賴；不能只換 import 就當完全相容。作者的 Cloudflare 通過宣稱不是 1111 實測 |
| [Browserless](https://docs.browserless.io/examples/cloudflare) | 官方提供處理 Cloudflare／Turnstile 後由 Playwright 接續同一瀏覽器的介面；[費率](https://www.browserless.io/pricing)列免費 1,000 units/月、每 session 最長 2 分鐘；每次成功代解 10 units，瀏覽器時間與代理流量另耗 units | 適合保留既有 Node 探針做代管瀏覽器對照。免費額度不是 1,000 次代解；兩分鐘 session 限制需注意。1111 未驗 |
| [Bright Data Browser API](https://brightdata.com/products/scraping-browser) | 支援 Playwright 互動與自動處理 CAPTCHA；[費率](https://brightdata.com/pricing/scraping-browser)列 PAYG US$8/GB、免費 1GB/月 | 同類代管備選；按流量計價，圖片與重試也影響成本。需要多步互動時應評估 Browser API，而非只回傳單頁的 Web Unlocker。1111 未驗 |
| [CapSolver](https://www.capsolver.com/products/cloudflare)／[2Captcha](https://2captcha.com/p/cloudflare-turnstile) | 官方列 Turnstile 支援；[CapSolver 費率](https://docs.capsolver.com/en/pricing/) US$1.20/1,000 tokens；2Captcha US$1.45/1,000 次 | 只是驗證處理的一環，不提供完整職缺流程。報價不是每千筆職缺總成本，也不能證明 1111 會接受結果；先列後備 |
| Apify [Corvuslab 1111](https://apify.com/corvuslab/taiwan1111-jobs-scraper)／[ScrapingMonkey 1111](https://apify.com/scrapingmonkey/1111-scraper) | 兩者均為社群維護的專用 Actor，頁面標價 from US$1/1,000 results，宣稱可輸出結構化職缺及分頁 | 最接近直接購用資料，但不是 1111 官方 API，亦未見可供複用的公開程式授權。本次頁面皆無評價，使用人數很少；範例輸出屬作者材料，不能證明目前可用 |
| [Camoufox](https://github.com/daijro/camoufox) | Firefox 修改版，有 Node/TypeScript 介面；瀏覽器 MPL-2.0、套件 MIT，另含其他授權元件。官方仍標示開發中、可能不適合穩定生產使用 | 須調整瀏覽器及啟動方式，依賴相容性需獨立核對；現有 Node 26.7 不需僅為此升級。成本高於 Patchright，不優先。1111 未驗 |
| [rebrowser-patches](https://github.com/rebrowser/rebrowser-patches) | 可修改 Playwright／Puppeteer 自動化行為；README 最新完整測試為 Playwright 1.52.0，修補啟用時 page.pause 不可用 | 與本探針 1.64.0 有版本落差，發布套件的授權檔也需先核對；不優先。1111 未驗 |
| [FlareSolverr](https://github.com/FlareSolverr/FlareSolverr) | MIT；README 有 Turnstile 互動介面／結果欄位，同時 Captcha Solvers 段警告外掛 solver 目前不可用 | 不可概括說完全不支援 Turnstile；但不是已證實可完成 1111 雙重驗證與翻頁的現成解法，列次選 |

費率為 2026-10-11 查到的公開標價，計費單位不同，不能直接當成每千筆職缺的同等成本；帳戶實際額度、超額與方案條件仍以供應商為準。供應商支援說明只是候選依據，並非獨立成功率測量。本輪限定官方 repo／文件及 Actor 頁面搜尋，未找到可重現目前 1111 驗證閘門成功的公開證據；這不代表不存在。

### 建議順序與既有驗收

1. **希望保留本機工具與低成本：先把 Patchright 列為下一個隔離實驗。** 保留原探針作對照，使用自己的依賴及臨時瀏覽器資料；Console API 的限制需反映在診斷結果，不能把沒有 console 訊息當沒有錯誤。
2. **希望保留 Playwright 流程且接受外部服務：優先評估 Browserless，Bright Data 作備選。** 這些服務控制整個瀏覽器，比單獨回傳驗證答案更貼近現有流程；是否能處理 1111 仍須小量實測，不能承諾成功。
3. **只需要職缺資料：Apify 專用 Actor 可另做小樣本資料核對。** 它可能減少自行維護解析器的工作，但輸出資料可用不等於我們的 Playwright 已通過；必須分開記錄。

下一輪仍沿用既有驗收：採購／台南的正確搜尋條件、第二頁有新職缺 ID、兩筆詳細頁的職缺／公司及工作內容可核對。不得以 HTTP 200、solver 回成功、範例 JSON 或單次 CAPTCHA 通過替代。三階段通過也只代表當次可行，不代表每日穩定。

代管瀏覽器／Actor 會由第三方處理查詢與頁面內容；代解 API 需傳送網站 URL 與驗證參數。本輪沒有傳送個人登入狀態、履歷或正式服務資料，也未呼叫任何付費執行 API。僅補充研究紀錄及本機專案記憶；原探針、正式 app、CI 與部署均未變。
