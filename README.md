# tw-sector-market

台股籌碼板塊監測：**槓桿先行（權證爆量）→ 籌碼集中（集保大戶）→ 價格**，觀察名單依板塊分組，對應美股板塊週報。

網頁：https://sh-marketlab.github.io/tw-sector-market/

## 架構
```
config/watchlist.json   觀察名單（板塊、權值錨 / 訊號標的、對應美股分組）
config/params.json      訊號參數（可直接在 GitHub 網頁改）
scripts/fetch.py        抓原始資料並累積到 data/（冪等，重跑不會重複）
scripts/build.py        抓股價（yfinance）＋計算訊號 → docs/data.json
scripts/notify.py       有新訊號 → 開 GitHub Issue（指派給 owner → email）
scripts/backtest.py     新訊號記到 data/signal_log.csv，算之後 5/10/20 日報酬與超額報酬 → docs/backtest.json
docs/index.html         網頁
docs/backtest.html      回測紀錄頁
.github/workflows/update.yml  平日台北 18:00、隔天 08:30 自動執行
```

## 資料來源（皆免費）
| 資料 | 來源 | 歷史 |
|---|---|---|
| 股價 / 成交量 | yfinance（.TW / .TWO） | 2 年，每次重抓 |
| 認購/認售權證成交金額 | 證交所 OpenAPI t187ap37_L / t187ap42_L、公開資訊觀測站 t187ap37_O / t187ap42_O | **只有當日**，從啟用日開始累積 |
| 三大法人買賣超 | 證交所 T86、櫃買中心 dailyTrade | 可回補（手動執行時填天數） |
| 集保股權分散表 | 集保 opendata 1-5 | **只有當週**，每週存一份全市場 csv.gz |

## 第一次啟用
1. Settings → Pages → Source 選 `Deploy from a branch`，Branch 選 `main` / `/docs`。
2. Actions → update → Run workflow，`insti_days` 填 `90` 回補三大法人。
3. 權證 z 分數需累積 21 個交易日、集保週變化需 2 週；未滿前頁面會標示並以現股量暫代。

## Email 通知
有**新出現**的訊號（完整訊號、槓桿＋籌碼、權證來源的槓桿、籌碼集中、法人進駐）時，Actions 會開一則 Issue 並指派給 repo owner，GitHub 會寄信。
現股量替代的「槓桿爆量」只顯示不寄。收不到信請檢查 GitHub → Settings → Notifications → Email 是否勾選 Participating / Assigned。

## 回測紀錄
- 每次出現**新**訊號（含不寄信的）就記一筆到 `data/signal_log.csv`；同檔同訊號 `backtest_cooldown_days` 個交易日內重複出現只記第一次。
- 第一次執行會用三大法人歷史**回推**過去的「法人進駐」，不用等幾週才有樣本。
- 進場價＝訊號日的**次一交易日收盤**；比較基準為加權指數 ^TWII。結果在 `backtest.html`。
- 改了法人參數想重算回推紀錄：本機執行 `python scripts/backtest.py --rebackfill /tmp/x.json docs/data.json`。
