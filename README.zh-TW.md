# Autoresearch Polymarket

[English](README.md) | [繁體中文](README.zh-TW.md)

一個概念驗證專案，將 [autoresearch](https://github.com/uditgoenka/autoresearch) 架構 — 基於 Karpathy 的 [autoresearch](https://github.com/karpathy/autoresearch) 的 Claude 版分支 — 應用於真實場景：Polymarket BTC 5 分鐘預測市場。

本專案展示自主代理人如何透過共享知識系統協作、透過錦標賽選擇演化策略，並在無需人工介入的情況下持續改進。Polymarket 作為驗證架構的即時回饋迴路。

> **這不是一個交易工具。** 這是一個研究原型，證明 autoresearch 多代理人架構在實際應用中可行。以下基準測試結果僅用於展示系統運作正常，並非鼓勵交易。

> **Token 用量警告：** 本系統會持續呼叫 LLM（Codex/Claude）— 每個代理人每 5 分鐘回合呼叫一次，加上演化、回測和協調呼叫。預期會產生大量 API token 消耗。請密切關注你的用量與帳單。

> **需要 VPN：** Polymarket 和 Binance 在部分國家和地區限制存取。如果你在受限區域，需要使用 VPN 才能使用本系統。詳情請參閱 [Polymarket 服務條款](https://polymarket.com/tos) 和 [Binance 地區可用性](https://www.binance.com/en/support)。

## 架構

本系統實作了多種 autoresearch 模式：

- **多代理人錦標賽** — 代理人互相競爭，表現不佳者被淘汰替換
- **共享知識庫** — 代理人發布發現，供其他代理人閱讀與參考
- **策略演化** — 基於表現數據，由 LLM 驅動的策略突變
- **複製、鏡像與集成** — 表現最佳者被複製、反轉或組合
- **回測與快速演化** — 離線迴圈加速策略探索

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
