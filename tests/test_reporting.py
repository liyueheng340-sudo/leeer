"""Report parity: the shared writer produces the report tree for the CLI and the
programmatic API alike (#1037)."""

import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.reporting import write_report_tree


@pytest.fixture
def cwd_tmp_path():
    """write_report_tree enforces a cwd-internal save_path (path-traversal guard);
    pytest's default tmp_path lives outside the cwd, so tests use a cwd-relative dir."""
    p = Path.cwd() / f".test_reporting_tmp_{os.getpid()}"
    p.mkdir(parents=True, exist_ok=True)
    yield p
    shutil.rmtree(p, ignore_errors=True)


def _state():
    return {
        "market_report": "MKT",
        "news_report": "NEWS",
        "investment_debate_state": {"judge_decision": "RM PLAN"},
        "trader_investment_plan": "TRADE",
        "risk_debate_state": {"judge_decision": "PM DECISION"},
    }


@pytest.mark.unit
def test_write_report_tree_creates_files(cwd_tmp_path):
    out = write_report_tree(_state(), "AAPL", cwd_tmp_path)
    assert out.name == "complete_report.md"
    assert (cwd_tmp_path / "1_analysts" / "market.md").read_text() == "MKT"
    assert (cwd_tmp_path / "1_analysts" / "news.md").read_text() == "NEWS"
    assert (cwd_tmp_path / "2_research" / "manager.md").read_text() == "RM PLAN"
    assert (cwd_tmp_path / "3_trading" / "trader.md").read_text() == "TRADE"
    assert (cwd_tmp_path / "5_portfolio" / "decision.md").read_text() == "PM DECISION"
    complete = out.read_text()
    assert "Trading Analysis Report: AAPL" in complete
    assert "MKT" in complete and "PM DECISION" in complete


@pytest.mark.unit
def test_save_reports_explicit_path(cwd_tmp_path):
    # Unbound: with an explicit save_path, the method doesn't touch self/config.
    out = TradingAgentsGraph.save_reports(None, _state(), "AAPL", save_path=cwd_tmp_path)
    assert (cwd_tmp_path / "complete_report.md").exists()
    assert out == cwd_tmp_path / "complete_report.md"


@pytest.mark.unit
def test_save_reports_defaults_under_results_dir(cwd_tmp_path):
    mock_self = SimpleNamespace(config={"results_dir": str(cwd_tmp_path)})
    out = TradingAgentsGraph.save_reports(mock_self, _state(), "AAPL")
    assert out.exists()
    assert out.parent.parent.name == "reports"  # results_dir/reports/AAPL_<stamp>/...
    assert out.parent.name.startswith("AAPL_")
