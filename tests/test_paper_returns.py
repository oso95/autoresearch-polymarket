import json

import pytest

from paper_returns import summarize_agent


def test_summarize_agent_reports_total_return(tmp_path):
    agent_dir = tmp_path / "agent-001-test"
    agent_dir.mkdir()
    exec_path = agent_dir / "executions.jsonl"
    exec_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "correct": True,
                        "entry_price": 0.40,
                        "payout": 1.0,
                        "pnl_per_share": 0.60,
                        "return_pct": 1.5,
                        "market_slug": "m1",
                    }
                ),
                json.dumps(
                    {
                        "correct": False,
                        "entry_price": 0.80,
                        "payout": 0.0,
                        "pnl_per_share": -0.80,
                        "return_pct": -1.0,
                        "market_slug": "m2",
                    }
                ),
            ]
        )
        + "\n"
    )

    summary = summarize_agent(str(agent_dir))
    assert summary is not None
    assert summary["trades"] == 2
    assert summary["wins"] == 1
    assert summary["total_cost"] == pytest.approx(1.2)
    assert summary["total_payout"] == pytest.approx(1.0)
    assert summary["total_pnl"] == pytest.approx(-0.2)
    assert summary["total_return_pct"] == pytest.approx(-0.2 / 1.2)
    assert summary["last_market"] == "m2"
