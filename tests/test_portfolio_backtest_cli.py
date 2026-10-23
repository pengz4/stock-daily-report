from stock_daily_report import cli


def test_portfolio_backtest_cli_wires_local_inputs_and_prints_report(
    monkeypatch, tmp_path, capsys
):
    settings = object()
    scan_settings = object()
    watchlist = object()
    provider = object()
    expected = tmp_path / "portfolio-report.json"
    calls = {}

    monkeypatch.setattr(
        cli, "load_portfolio_backtest_settings", lambda path: settings, raising=False
    )
    monkeypatch.setattr(
        cli, "load_market_scan_settings", lambda path: scan_settings
    )
    monkeypatch.setattr(cli, "load_watchlist", lambda path: watchlist)
    monkeypatch.setattr(
        cli, "FixtureMarketDataProvider", lambda path: provider
    )

    def run(*args, **kwargs):
        calls["args"] = args
        calls["kwargs"] = kwargs
        return expected

    monkeypatch.setattr(cli, "run_portfolio_backtest", run, raising=False)

    status = cli.main(
        [
            "portfolio-backtest",
            "--settings",
            str(tmp_path / "portfolio.yaml"),
            "--market-scan-settings",
            str(tmp_path / "market-scan.yaml"),
            "--watchlist",
            str(tmp_path / "watchlist.yaml"),
            "--fixture-directory",
            str(tmp_path / "bars"),
            "--output-root",
            str(tmp_path),
        ]
    )

    assert status == 0
    assert capsys.readouterr().out.strip() == str(expected)
    assert calls["args"][:3] == (settings, scan_settings, watchlist)
    assert calls["args"][3] is provider
    assert calls["kwargs"]["output_root"] == tmp_path
