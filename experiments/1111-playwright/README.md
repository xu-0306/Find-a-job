# 1111 Playwright 可行性探針

**2026-10-11 結論：探針可執行，獨立瀏覽器的真網站驗證受阻，尚不能接入正式搜尋。**

這個獨立 Node 套件只做兩頁搜尋及兩筆職缺詳細頁的低流量驗證。正式 Python app、資料庫、排程、Push 與現有 GitHub Actions 均不使用它；Docker build context 也排除此目錄。

## 執行

在此目錄執行，需 Node.js 20 以上。Windows PowerShell 使用 `npm.cmd` / `npx.cmd`：

```powershell
npm.cmd ci --ignore-scripts --no-audit --no-fund
npx.cmd playwright install chromium
npm.cmd test
npm.cmd run probe -- --keyword 採購 --area 101600
```

`--area` 是選填的 1111 六位地區代碼；`101600` 為這次研究使用的台南市代碼。省略時不指定地區。這不是全部城市代碼的查詢工具。

可用 `--headed` 顯示瀏覽器，`--chrome` 改用電腦已安裝的 Google Chrome；預設使用 Playwright 的完整 Chromium。每次都是新 context，不使用個人瀏覽器 profile、cookie 或正式專案的設定。

```powershell
npm.cmd run probe -- --keyword 採購 --area 101600 --chrome --headed
```

結果寫入 `output/playwright/<UTC時間>/report.json`；失敗時另存 `failure.png`。成功退出碼為 0，失敗／受阻為 1。缺少瀏覽器等啟動錯誤會直接顯示於終端並退出 1。不要在此目錄執行正式專案的 `start.cmd` 或 `setup.cmd`。

## 驗證界線

- 搜尋：比對 URL 的關鍵字、地區代碼、頁碼，以及可見搜尋框／標題。只取 `mid=searchlist` 職缺連結，排除首頁推薦、隱藏卡片、其他網域及重複 ID；保留職缺與公司名稱／網址。
- 翻頁：按網站的第 2 頁按鈕，等待頁碼及新 ID 出現；記錄跨頁重複數，不把網址變了但內容未更新當成功。
- 詳細：各頁取一筆不同職缺，核對 URL、標題、公司；保存可見全文及「工作內容」區段。欄位正規化尚未做，薪資／地點等保留在全文中。詳細頁的區段擷取目前只有合成 DOM 驗證，仍需真頁確認。
- 每個等待最多 30 秒，導頁間隔 1.5 秒、一次一頁；首個失敗即停，保留已成功的階段。`not_run` 不等於通過。
- `blocked_or_loading` 表示空白頁加上 Loading／⏳ 標題，無法區分尚未完成的載入與驗證攔截。其他空頁、不同條件或版型不符均回報失敗，不當成搜尋成功且零筆。
- 沒有結果或不足兩頁的查詢也不能完成這項三階段驗證。此探針不宣稱全量、每日穩定性、廣告分類或完整篩選語義已通過。

## 本次證據

| 方式 | 2026-10-11 結果 |
|---|---|
| Playwright headless shell、完整 Chromium headless、Chrome 視窗模式，直接開搜尋網址 | 等待後仍 Loading，沒有職缺 |
| Chrome headless／視窗模式，首頁等待互動元件就緒後填採購、按搜尋 | 首頁可讀，搜尋頁仍 Loading |
| Chrome headless，開啟觀察到的公開職缺連結 | 詳細頁標題有 ⏳，可見本文為空 |
| 正式探針：採購／台南，Chrome headless | `blocked_or_loading`；搜尋失敗，翻頁與詳細未執行 |
| 離線合成 DOM 測試 | 9 項通過；所有請求由 Playwright route 攔截，不連真網站 |

原始實測輸出只留本機 `output/playwright/`，不進 Git。2026-10-10 曾在 Codex 內建瀏覽器讀到兩頁搜尋與詳細頁，那是另一個瀏覽器環境，不能替代本次獨立探針驗收。這次內建瀏覽器結構檢查還觀察到搜尋 URL／內容不一致，因此加上條件比對，沒有把該頁當成成功證據。

已檢查公開 [robots.txt](https://www.1111.com.tw/robots.txt)；它不等同網站授權。遇驗證頁即停，未加入驗證碼代解、stealth、代理輪換或 cookie 複用。後續應先確認一般瀏覽器與網站允許的資料取得方式；取得正常公開頁後再重跑此探針。


## 2026-10-11 續查：Loading 停在哪裡

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
