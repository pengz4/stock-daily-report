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
    job = workflow["jobs"]["market-scan"]
    steps = job["steps"]
    python_step = next(step for step in steps if step["name"] == "Set up Python")
    install_step = next(step for step in steps if step["name"] == "Install project")

    assert "concurrency" not in workflow
    assert job["concurrency"] == {
        "group": "stock-daily-report-history",
        "cancel-in-progress": False,
    }
    daily = _daily_workflow()
    assert daily["concurrency"]["group"] != job["concurrency"]["group"]
    assert daily["jobs"]["generate"]["concurrency"] == job["concurrency"]
    assert "needs" not in job
    watchlist_job = workflow["jobs"]["watchlist-scan"]
    assert watchlist_job["concurrency"] == job["concurrency"]
    assert job["timeout-minutes"] > 0
    assert python_step["with"]["python-version"] == "3.11"
    assert "-r requirements.lock" in install_step["run"]


def test_market_scan_workflow_reuses_and_persists_independent_artifacts():
    _, workflow = _workflow()
    steps = workflow["jobs"]["market-scan"]["steps"]
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


def test_watchlist_scan_merges_history_and_serializes_history_writers():
    _, workflow = _workflow()
    watchlist_job = workflow["jobs"]["watchlist-scan"]
    persist = next(
        step["run"]
        for step in watchlist_job["steps"]
        if step["name"] == "Persist watchlist scan history"
    )

    assert "rm -rf .report-history/watchlist-scans" not in persist
    assert "cp -a watchlist-scans/. .report-history/watchlist-scans/" in persist
    assert watchlist_job["concurrency"] == {
        "group": "stock-daily-report-history",
        "cancel-in-progress": False,
    }


def test_history_publish_retries_fail_the_job_when_push_never_succeeds():
    _, workflow = _workflow()

    for job_name, step_name in (
        ("watchlist-scan", "Persist watchlist scan history"),
        ("market-scan", "Persist market scan history"),
    ):
        step = next(
            step["run"]
            for step in workflow["jobs"][job_name]["steps"]
            if step["name"] == step_name
        )
        assert "push_succeeded=false" in step
        assert 'if [ "$push_succeeded" != true ]; then' in step
        assert "exit 1" in step


def test_daily_workflow_restores_same_date_market_scan_history_when_available():
    workflow = _daily_workflow()
    steps = workflow["jobs"]["generate"]["steps"]
    merge = next(
        step["run"] for step in steps if step["name"] == "Merge previous report history"
    )

    assert "if [ -d .report-history/market-scans ]; then" in merge
    assert "cp -a .report-history/market-scans ./market-scans" in merge
