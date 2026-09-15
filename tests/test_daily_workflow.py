from pathlib import Path

import yaml


def _workflow():
    workflow_path = (
        Path(__file__).parents[1] / ".github" / "workflows" / "daily-report.yml"
    )
    return yaml.safe_load(workflow_path.read_text(encoding="utf-8"))


def test_daily_workflow_serializes_the_full_deployment_lifecycle():
    workflow = _workflow()

    assert workflow["concurrency"] == {
        "group": "stock-daily-report-pages",
        "cancel-in-progress": False,
    }
    assert workflow["jobs"]["generate"]["concurrency"] == {
        "group": "stock-daily-report-history",
        "cancel-in-progress": False,
    }
    assert workflow["jobs"]["deploy"]["needs"] == "generate"
    assert workflow["jobs"]["notify"]["needs"] == ["generate", "deploy"]


def test_daily_workflow_passes_repository_configuration_paths():
    workflow = _workflow()
    steps = workflow["jobs"]["generate"]["steps"]
    command = next(
        step["run"]
        for step in steps
        if step["name"] == "Generate report artifacts"
    )

    assert "--settings config/settings.yaml" in command
    assert "--watchlist config/watchlist.yaml" in command


def test_daily_workflow_reuses_an_existing_immutable_report():
    workflow = _workflow()
    steps = workflow["jobs"]["generate"]["steps"]
    command = next(
        step["run"]
        for step in steps
        if step["name"] == "Generate report artifacts"
    )

    assert 'report_path="reports/${report_date}/report.json"' in command
    assert 'snapshot_path="snapshots/${report_date}/input.json"' in command
    assert (
        "Report already exists with current scan; reusing immutable artifacts"
        in command
    )


def test_daily_workflow_regenerates_when_a_new_scan_is_available():
    workflow = _workflow()
    steps = workflow["jobs"]["generate"]["steps"]
    command = next(
        step["run"]
        for step in steps
        if step["name"] == "Generate report artifacts"
    )

    assert 'scan_path="market-scans/${report_date}/scan.json"' in command
    assert "market_rankings" in command
    assert "--reuse-existing-snapshot" in command
    assert "Report already exists with current scan; reusing immutable artifacts" in command


def test_daily_workflow_reuse_requires_matching_market_state_fingerprint():
    workflow = _workflow()
    steps = workflow["jobs"]["generate"]["steps"]
    command = next(
        step["run"]
        for step in steps
        if step["name"] == "Generate report artifacts"
    )

    assert "report.market_state is not None" in command
    assert "scan.market_state is not None" in command
    assert "report.market_state.rule_version == scan.market_state.rule_version" in command
    assert "rankings.config_hash == scan.config_hash" in command


def test_daily_workflow_compares_exact_deep_selection_metadata():
    workflow = _workflow()
    steps = workflow["jobs"]["generate"]["steps"]
    command = next(
        step["run"]
        for step in steps
        if step["name"] == "Generate report artifacts"
    )

    assert "load_watchlist" in command
    assert "stock.name" in command
    assert "stock.group" in command
    assert "expected_deep" in command
    assert "deep_metadata == expected_deep" in command
    assert 'stock.priority == "extended"' in command
    assert "pool_ok" in command
