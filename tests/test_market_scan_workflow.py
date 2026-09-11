from pathlib import Path

import yaml


def _workflow():
    path = Path(__file__).parents[1] / ".github" / "workflows" / "market-scan.yml"
    return path, yaml.safe_load(path.read_text(encoding="utf-8"))


def _daily_workflow():
    path = Path(__file__).parents[1] / ".github" / "workflows" / "daily-report.yml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_market_scan_workflow_is_scheduled_before_daily_and_dispatchable():
    _, workflow = _workflow()
    triggers = workflow.get("on", workflow.get(True))

    assert triggers["schedule"][0]["cron"] < "30 8 * * 1-5"
    assert triggers["workflow_dispatch"]["inputs"]["report_date"]["required"] is False
    assert (
        "Current Shanghai date"
        in triggers["workflow_dispatch"]["inputs"]["report_date"]["description"]
    )


def test_market_scan_workflow_is_bounded_and_uses_locked_python():
    _, workflow = _workflow()
    job = workflow["jobs"]["scan"]
    steps = job["steps"]
    python_step = next(step for step in steps if step["name"] == "Set up Python")
    install_step = next(step for step in steps if step["name"] == "Install project")

    assert "concurrency" not in workflow
    assert job["concurrency"] == {
        "group": "stock-daily-report-history",
        "cancel-in-progress": False,
    }
    daily = _daily_workflow()
    assert "concurrency" not in daily
    assert daily["jobs"]["generate"]["concurrency"] == job["concurrency"]
    assert "needs" not in job
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

    assert 'report_date="${{ steps.report-date.outputs.report_date }}"' in generate
    assert "stock_daily_report.cli market-scan" in generate
    assert "if [ -f" not in generate
    assert "--settings config/market_scan.yaml" in generate
    assert "--data-settings config/settings.yaml" in generate
    assert "--output-root ." in generate
    assert "market-scans" in persist
    assert "reports-history" in persist
    assert "reports " not in persist
    assert "snapshots" not in persist
