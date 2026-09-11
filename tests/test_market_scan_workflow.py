from pathlib import Path

import yaml


def _workflow():
    path = Path(__file__).parents[1] / ".github" / "workflows" / "market-scan.yml"
    return path, yaml.safe_load(path.read_text(encoding="utf-8"))


def test_market_scan_workflow_is_scheduled_before_daily_and_dispatchable():
    _, workflow = _workflow()
    triggers = workflow.get("on", workflow.get(True))

    assert triggers["schedule"][0]["cron"] < "30 8 * * 1-5"
    assert triggers["workflow_dispatch"]["inputs"]["report_date"]["required"] is False


def test_market_scan_workflow_is_bounded_and_uses_locked_python():
    _, workflow = _workflow()
    job = workflow["jobs"]["scan"]
    steps = job["steps"]
    python_step = next(step for step in steps if step["name"] == "Set up Python")
    install_step = next(step for step in steps if step["name"] == "Install project")

    assert workflow["concurrency"] == {
        "group": "stock-daily-report-market-scan",
        "cancel-in-progress": False,
    }
    assert job["timeout-minutes"] > 0
    assert python_step["with"]["python-version"] == "3.11"
    assert "-r requirements.lock" in install_step["run"]


def test_market_scan_workflow_reuses_and_persists_independent_artifacts():
    _, workflow = _workflow()
    steps = workflow["jobs"]["scan"]["steps"]
    generate = next(
        step["run"] for step in steps if step["name"] == "Generate market scan"
    )
    persist = next(
        step["run"] for step in steps if step["name"] == "Persist market scan history"
    )

    assert 'market-scans/${{ steps.report-date.outputs.report_date }}/scan.json' in generate
    assert "Market scan already exists; reusing immutable artifact" in generate
    assert "stock_daily_report.cli market-scan" in generate
    assert "--settings config/market_scan.yaml" in generate
    assert "--output-root ." in generate
    assert "market-scans" in persist
    assert "reports-history" in persist
    assert "reports " not in persist
    assert "snapshots" not in persist
