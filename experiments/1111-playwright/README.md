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

## 公開程式參考

查核日期：2026-10-11。此次自行實作，沒有複製下列爬蟲程式。

| 專案 | 授權與維護觀察 | 可參考內容 |
|---|---|---|
| [SamJim0729/job_scraper](https://github.com/SamJim0729/job_scraper) | 未見 LICENSE／明確授權；2025-02-26 提交 | Selenium 的搜尋→ID→詳細頁流程；舊 selectors 未證明仍可用 |
| [kobojp/scraper_104_1111](https://github.com/kobojp/scraper_104_1111) | 未見 LICENSE／明確授權；1111 程式最後更新 2020-07-08，2026 更新為 README | 舊版 job-bank 網址與解析流程，不能直接套用現站 |
| [ori0nz/1111spider](https://github.com/ori0nz/1111spider) | [MIT](https://github.com/ori0nz/1111spider/blob/master/LICENSE)；2017-06-16 | 很舊的 Node／Cheerio 實作，有授權但需重寫網站適配 |
| [apify/crawlee](https://github.com/apify/crawlee) | [Apache-2.0](https://github.com/apify/crawlee/blob/master/LICENSE.md) | 活躍的通用 Playwright 爬取框架；沒有現成 1111 adapter，這次小型探針不需要引入 |

直接使用 [Playwright Library](https://playwright.dev/docs/library)，鎖定依賴於 `package-lock.json`，以 Node 原生測試工具驗證。公開可看原始碼，不代表已有可直接採用的授權或今天仍能執行。
