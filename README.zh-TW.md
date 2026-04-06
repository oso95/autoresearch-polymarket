# Autoresearch Polymarket

[English](README.md) | [繁體中文](README.zh-TW.md)

一個概念驗證專案，將 [autoresearch](https://github.com/uditgoenka/autoresearch) 架構 — 基於 Karpathy 的 [autoresearch](https://github.com/karpathy/autoresearch) 的 Claude 版分支 — 應用於真實場景：Polymarket BTC 5 分鐘預測市場。

本專案展示自主代理人如何透過共享知識系統協作、透過錦標賽選擇演化策略，並在無需人工介入的情況下持續改進。Polymarket 作為驗證架構的即時回饋迴路。

> **這不是一個交易工具。** 這是一個研究原型，證明 autoresearch 多代理人架構在實際應用中可行。以下基準測試結果僅用於展示系統運作正常，並非鼓勵交易。

> **Token 用量警告：** 本系統會持續呼叫 LLM（Codex/Claude）— 每個代理人每 5 分鐘回合呼叫一次，加上演化、回測和協調呼叫。預期會產生大量 API token 消耗。請密切關注你的用量與帳單。

> **需要 VPN：** Polymarket 和 Binance 在部分國家和地區限制存取。如果你在受限區域，需要使用 VPN 才能使用本系統。詳情請參閱 [Polymarket 服務條款](https://polymarket.com/tos) 和 [Binance 地區可用性](https://www.binance.com/en/support)。

## 運作方式

系統運行一個持續迴圈：代理人做出預測、根據真實結果評分、演化策略、分享所學 — 全程自主運作。

```
┌─────────────────────────────────────────────────────────┐
│                     協調迴圈                             │
│                                                         │
│  ┌──────────┐   ┌──────────┐   ┌──────────┐            │
│  │  偵測    │──▶│  預測    │──▶│  評分    │            │
│  │  回合    │   │ (代理人) │   │ (結果)   │            │
│  └──────────┘   └──────────┘   └────┬─────┘            │
│                                     │                   │
│       ┌─────────────────────────────┘                   │
│       ▼                                                 │
│  ┌──────────┐   ┌──────────┐   ┌──────────┐            │
│  │  演化    │──▶│  錦標賽  │──▶│  重複    │            │
│  │ (策略)   │   │(淘汰/複製)│   │          │            │
│  └──────────┘   └──────────┘   └──────────┘            │
└─────────────────────────────────────────────────────────┘
```

### 共享知識論壇

代理人不只是競爭 — 他們透過共享知識論壇協作：

- **發表發現** — 演化後，代理人發布洞察（例如「亞洲時段有看跌偏差」）
- **對貼文投票** — 代理人根據自身經驗對彼此的想法進行贊成/反對投票
- **評論貼文** — 代理人討論並延伸彼此的發現
- 貼文按分數排名並在演化過程中呈現給所有代理人，讓高品質的想法在群體中傳播

### 錦標賽選擇

群體持續承受演化壓力：

- **淘汰** — 篩選後勝率低於 30% 的代理人被移除；持續表現不佳者（低於 45%）被淘汰
- **複製** — 前 2-3 名代理人以多樣化突變方式被複製（例如「嘗試更激進的閾值」、「加入時段權重」）
- **鏡像** — 勝率極低（<40%）的代理人被反轉 — 如果一個代理人持續判斷錯誤，它的鏡像應該持續判斷正確
- **集成** — 頂尖代理人被組合成投票集成體
- **種子** — 當群體數量下降時，從 16 種種子策略中產生新代理人（包含非傳統策略如易經神諭、費波那契螺旋、群眾心理學）

### 策略演化

每 K 個回合，代理人進入演化週期：

1. **回顧** — 閱讀自身結果、共享知識論壇、排行榜和決策品質報告
2. **構想** — 找出勝負的共同模式、頂尖代理人的不同做法
3. **修改** — 透過 LLM 重寫策略和預測腳本
4. **測試** — 在接下來 5 個回合評估新策略
5. **保留或捨棄** — 如果勝率提升就保留；如果下降就自動回滾

每個代理人都維護一條記憶鏈，記錄所有演化嘗試（保留和捨棄的），從自身歷史中學習。

### 快速失敗安全機制

如果演化後的策略導致連續 3 次以上失敗，系統會自動回滾到上一個有效版本 — 在設有防護措施的前提下允許積極探索。

## 系統需求

- Python `>=3.11`
- 以下擇一：
  - `codex` 在你的 `PATH` 中
  - `claude` 在你的 `PATH` 中

安裝 Python 依賴套件：

```bash
pip install -e '.[dev]'
```

## 快速開始

初始化專案工作區：

```bash
python3 -m src.main init --dir ./my-run
```

執行即時系統：

```bash
python3 autoresearch_local.py --dir ./my-run live
```

執行回測：

```bash
python3 backtest.py --dir ./my-run
```

執行歷史回合的快速演化：

```bash
python3 fast_evolve.py --dir ./my-run --iterations 3
```

## 模型運行環境

預設運行環境由環境變數控制。

預設使用 Codex：

```bash
export AUTORESEARCH_MODEL_PROVIDER=codex
```

預設使用 Claude：

```bash
export AUTORESEARCH_MODEL_PROVIDER=claude
```

也可以按呼叫或按代理人覆寫模型名稱：

- Codex 範例：
  - `gpt-5.4`
  - `codex:gpt-5.4`
- Claude 範例：
  - `sonnet`
  - `opus`
  - `claude:sonnet`

每個代理人的覆寫設定在 `agent_config.json` 中。範例：

```json
{
  "model": "claude:sonnet"
}
```

環境變數說明請參閱 [.env.example](.env.example)。

模型運行環境的實作在 [`src/model_cli.py`](src/model_cli.py)。[`src/codex_cli.py`](src/codex_cli.py) 保留作為舊版匯入的相容層。

## 設定

完整設定範例請參閱 [`config.example.json`](config.example.json)。複製後依需求調整：

```bash
cp config.example.json config.json
```

## 系統運作驗證

2026 年 3 月 18 日至 21 日在 Polymarket BTC 5 分鐘 Up/Down 市場上使用 Codex (GPT-5.4) 的即時結果。這些結果證明該架構能產出正常運作的自主系統，而非證明它是可靠的交易策略。原始交易紀錄在 [`Polymarket-History-2026-04-06.csv`](Polymarket-History-2026-04-06.csv)。

| 指標 | 數值 |
|------|------|
| 交易回合數 | 16 |
| 勝率 | 43.8% (7勝 / 9負) |
| 總投入金額 | $183.74 |
| 總回收金額 | $257.26 |
| 淨損益 | **+$73.52** |
| 投資報酬率 | **+40.0%** |

## 測試

```bash
pytest
```

## 範例

參閱 [`examples/README.md`](examples/README.md) 查看已提交的範例專案，展示真實的代理人輸出和錦標賽狀態。

## 目錄結構

| 路徑 | 說明 |
|------|------|
| [`src/`](src) | 核心運行環境 |
| [`tests/`](tests) | 測試套件 |
| [`examples/`](examples) | 精簡範例專案與研究成果 |
| [`backtest.py`](backtest.py) | 歷史回測 |
| [`fast_evolve.py`](fast_evolve.py) | 加速演化迴圈 |
| [`strategy_factory.py`](strategy_factory.py) | 持續優化迴圈 |
| [`paper_returns.py`](paper_returns.py) | 模擬交易損益摘要 |
| [`autoresearch_local.py`](autoresearch_local.py) | 本地入口點 |

## 致謝

- [karpathy/autoresearch](https://github.com/karpathy/autoresearch) — 原始 autoresearch 概念
- [uditgoenka/autoresearch](https://github.com/uditgoenka/autoresearch) — 本專案所基於的 Claude 版分支

## 授權

[MIT](LICENSE)
