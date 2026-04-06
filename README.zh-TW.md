# Autoresearch Polymarket

[English](README.md) | [繁體中文](README.zh-TW.md)

針對 Polymarket BTC 5 分鐘市場的自主策略探索系統。

本專案運行一個本地代理人錦標賽，代理人會：

- 讀取即時與歷史市場數據
- 做出 `Up` / `Down` 預測
- 隨時間演化策略
- 回測、複製、鏡像並集成表現最佳的策略

> **免責聲明：** 本專案僅為概念驗證（POC），供研究與教育用途。使用風險自負。作者不對任何財務損失負責。過去的表現不保證未來的結果。

> **Token 用量警告：** 本系統會持續呼叫 LLM（Codex/Claude）— 每個代理人每 5 分鐘回合呼叫一次，加上演化、回測和協調呼叫。預期會產生大量 API token 消耗。請密切關注你的用量與帳單。

> **需要 VPN：** Polymarket 和 Binance 在部分國家和地區限制存取。如果你在受限區域，需要使用 VPN 才能使用本系統。詳情請參閱 [Polymarket 服務條款](https://polymarket.com/tos) 和 [Binance 地區可用性](https://www.binance.com/en/support)。

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

## 基準測試結果

2026 年 3 月 18 日至 21 日在 Polymarket BTC 5 分鐘 Up/Down 市場上使用 Codex (GPT-5.4) 的即時交易結果。原始交易紀錄在 [`Polymarket-History-2026-04-06.csv`](Polymarket-History-2026-04-06.csv)。

| 指標 | 數值 |
|------|------|
| 交易回合數 | 16 |
| 勝率 | 43.8% (7勝 / 9負) |
| 總投入金額 | $183.74 |
| 總回收金額 | $257.26 |
| 淨損益 | **+$73.52** |
| 投資報酬率 | **+40.0%** |

系統的優勢來自進場定價和部位大小，而非單純的方向準確度 — 贏的交易金額遠大於輸的交易金額。

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

## 授權

[MIT](LICENSE)
